"""TradeStation v3 REST client.

Handles OAuth token refresh and the endpoints the bot needs:
  - accounts + balances
  - quotes (underlying and option)
  - option expirations + strikes
  - place / cancel orders  (single-leg options, market or limit)

Sim vs live is decided purely by the base URL (settings.api_base).
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Optional

import requests

from .config import SIGNIN_BASE, Settings

log = logging.getLogger("ts_client")


class TradeStationError(RuntimeError):
    pass


class TradeStationClient:
    def __init__(self, settings: Settings):
        self.s = settings
        self.base = settings.api_base
        self._access_token: Optional[str] = None
        self._token_expiry: float = 0.0          # epoch seconds
        self._lock = threading.Lock()
        self._session = requests.Session()
        # keep-alive pool so order POSTs reuse a warm TLS connection (no handshake per call)
        adapter = requests.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=16)
        self._session.mount("https://", adapter)

    def warm(self) -> None:
        """Pre-fetch a token + open the TLS connection so the first order is fast."""
        try:
            self._token()
            self._session.get(f"{self.base}/marketdata/quotes/SPY",
                              headers=self._headers(), timeout=10)
        except Exception as e:  # noqa: BLE001
            log.warning("warm-up failed (non-fatal): %s", e)

    # ── auth ──────────────────────────────────────────────────────────────────
    def _refresh_access_token(self) -> None:
        if not (self.s.client_id and self.s.client_secret and self.s.refresh_token):
            raise TradeStationError(
                "Missing credentials. Set TS_CLIENT_ID / TS_CLIENT_SECRET / "
                "TS_REFRESH_TOKEN in .env (run scripts/get_refresh_token.py for the token)."
            )
        resp = self._session.post(
            f"{SIGNIN_BASE}/oauth/token",
            data={
                "grant_type": "refresh_token",
                "client_id": self.s.client_id,
                "client_secret": self.s.client_secret,
                "refresh_token": self.s.refresh_token,
            },
            timeout=15,
        )
        if resp.status_code != 200:
            raise TradeStationError(f"Token refresh failed {resp.status_code}: {resp.text}")
        data = resp.json()
        self._access_token = data["access_token"]
        # Access tokens last 1200s (20 min); refresh early at 15 min to be safe.
        expires_in = int(data.get("expires_in", 1200))
        self._token_expiry = time.time() + min(expires_in, 1200) - 300
        log.info("Access token refreshed (valid ~%ss).", expires_in)

    def _token(self) -> str:
        with self._lock:
            if not self._access_token or time.time() >= self._token_expiry:
                self._refresh_access_token()
            return self._access_token  # type: ignore[return-value]

    def keepalive(self) -> None:
        """Proactively refresh the token BEFORE it expires so the order hot path
        never pays a refresh round-trip. Called from a background loop."""
        with self._lock:
            if not self._access_token or time.time() >= self._token_expiry - 120:
                self._refresh_access_token()

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._token()}", "Content-Type": "application/json"}

    # ── low-level request with bounded, SAFE retries ───────────────────────────
    # IMPORTANT: order POSTs are NEVER retried on a network timeout — the order may
    # have reached the broker and a blind retry could double-place. We only retry
    # POSTs on 429/503 (request was rejected before processing, so it's safe).
    def _request(self, method: str, path: str, **kwargs) -> Any:
        url = path if path.startswith("http") else f"{self.base}{path}"
        idempotent = method.upper() in ("GET", "DELETE")
        backoffs = [0.4, 0.9, 1.8]
        last_err = ""
        # Compute headers ONCE up front (one token check). Only recompute on a 401,
        # so a 429/503 retry sequence never triggers extra token refreshes.
        headers = self._headers()
        for attempt in range(4):
            try:
                resp = self._session.request(method, url, headers=headers, timeout=20, **kwargs)
            except (requests.Timeout, requests.ConnectionError) as e:
                last_err = f"network error: {e}"
                if idempotent and attempt < 3:
                    time.sleep(backoffs[attempt])
                    continue
                # do NOT retry a non-idempotent POST (order) on timeout — could double-fire
                raise TradeStationError(f"{method} {url} {last_err}")

            if resp.status_code == 401 and attempt == 0:
                log.warning("401 — forcing token refresh and retrying.")
                with self._lock:
                    self._token_expiry = 0.0
                headers = self._headers()   # refreshed token
                continue
            if resp.status_code in (429, 503):
                reset = resp.headers.get("X-RateLimit-Reset", "?")
                last_err = f"{resp.status_code} (reset {reset})"
                if attempt < 3:
                    log.warning("%s on %s — backing off %ss.", resp.status_code, path, backoffs[attempt])
                    time.sleep(backoffs[attempt])
                    continue
                raise TradeStationError(f"Rate limited/again: {last_err}")
            if not resp.ok:
                raise TradeStationError(f"{method} {url} -> {resp.status_code}: {resp.text}")
            return resp.json() if resp.content else {}
        raise TradeStationError(last_err or "request failed")

    # ── accounts ───────────────────────────────────────────────────────────────
    def get_accounts(self) -> list[dict]:
        data = self._request("GET", "/brokerage/accounts")
        return data.get("Accounts", [])

    def resolve_account_id(self) -> str:
        """Use configured account, else auto-pick (prefer a SIM/Margin/Cash account)."""
        if self.s.broker.account_id:
            return self.s.broker.account_id
        accounts = self.get_accounts()
        if not accounts:
            raise TradeStationError("No accounts returned for these credentials.")
        return accounts[0]["AccountID"]

    def get_balances(self, account_id: str) -> dict:
        data = self._request("GET", f"/brokerage/accounts/{account_id}/balances")
        balances = data.get("Balances", [])
        if not balances:
            raise TradeStationError(f"No balance returned for account {account_id}.")
        return balances[0]

    def get_equity(self, account_id: str) -> float:
        bal = self.get_balances(account_id)
        for key in ("Equity", "CashBalance", "BuyingPower"):
            if bal.get(key) not in (None, ""):
                return float(bal[key])
        raise TradeStationError("Could not read account equity.")

    def get_positions(self, account_id: str) -> list[dict]:
        data = self._request("GET", f"/brokerage/accounts/{account_id}/positions")
        return data.get("Positions", [])

    def held_position(self, account_id: str, symbol: str) -> tuple[int, float]:
        """(abs_quantity, average_price) the broker actually holds for `symbol`.
        Returns (0, 0.0) if the position isn't held — used to confirm an entry filled
        before attaching broker-side exits (so we never place SELLTOCLOSE on 0 contracts)."""
        for p in self.get_positions(account_id):
            if str(p.get("Symbol")) == str(symbol):
                try:
                    qty = abs(int(float(p.get("Quantity") or 0)))
                except (TypeError, ValueError):
                    qty = 0
                try:
                    avg = float(p.get("AveragePrice") or 0.0)
                except (TypeError, ValueError):
                    avg = 0.0
                return qty, avg
        return 0, 0.0

    # ── market data ────────────────────────────────────────────────────────────
    def get_quote(self, symbol: str) -> dict:
        data = self._request("GET", f"/marketdata/quotes/{symbol}")
        quotes = data.get("Quotes", [])
        if not quotes:
            raise TradeStationError(f"No quote for {symbol}. ({data.get('Errors')})")
        return quotes[0]

    def get_quotes(self, symbols: list[str]) -> dict[str, dict]:
        """Batch quote fetch — returns {symbol: quote}. One request for many symbols."""
        if not symbols:
            return {}
        joined = ",".join(symbols)
        data = self._request("GET", f"/marketdata/quotes/{joined}")
        return {q.get("Symbol", ""): q for q in data.get("Quotes", [])}

    def get_option_expirations(self, underlying: str) -> list[dict]:
        data = self._request("GET", f"/marketdata/options/expirations/{underlying}")
        return data.get("Expirations", [])

    def get_option_strikes(self, underlying: str, expiration_mmddyyyy: str) -> list[float]:
        data = self._request(
            "GET",
            f"/marketdata/options/strikes/{underlying}",
            params={"expiration": expiration_mmddyyyy},
        )
        out: list[float] = []
        for row in data.get("Strikes", []):
            # each row is a list (1 element for single-leg); take the first leg's strike
            try:
                out.append(float(row[0]))
            except (TypeError, ValueError, IndexError):
                continue
        return sorted(out)

    # ── orders ─────────────────────────────────────────────────────────────────
    def place_option_order(
        self,
        account_id: str,
        option_symbol: str,
        quantity: int,
        trade_action: str,                 # BUYTOOPEN | SELLTOCLOSE | SELLTOOPEN | BUYTOCLOSE
        order_type: str = "Market",        # Market | Limit
        limit_price: Optional[float] = None,
        time_in_force: str = "DAY",
        route: str = "Intelligent",
    ) -> dict:
        body: dict = {
            "AccountID": account_id,
            "Symbol": option_symbol,
            "Quantity": str(int(quantity)),
            "OrderType": order_type,
            "TradeAction": trade_action,
            "TimeInForce": {"Duration": time_in_force},
            "Route": route,
        }
        if order_type == "Limit":
            if limit_price is None:
                raise TradeStationError("Limit order requires a limit_price.")
            body["LimitPrice"] = f"{limit_price:.2f}"

        log.info("Placing order: %s", body)
        data = self._request("POST", "/orderexecution/orders", json=body)
        if data.get("Errors"):
            raise TradeStationError(f"Order rejected: {data['Errors']}")
        return data

    def place_exit_bracket(self, account_id: str, option_symbol: str, quantity: int,
                           tp_price: Optional[float], sl_price: float,
                           stop_type: str = "StopMarket", time_in_force: str = "GTC") -> tuple:
        """Attach broker-side exits to an OPEN long option position: an OCO of
        [SELLTOCLOSE Limit @ tp_price] + [SELLTOCLOSE stop @ sl_price]. If tp_price is
        None, place just the protective stop. Returns (ok, [order_ids], message)."""
        def leg(otype, **px):
            o = {"AccountID": account_id, "Symbol": option_symbol, "Quantity": str(int(quantity)),
                 "OrderType": otype, "TradeAction": "SELLTOCLOSE",
                 "TimeInForce": {"Duration": time_in_force}, "Route": "Intelligent"}
            o.update(px)
            return o
        sl = leg(stop_type, StopPrice=f"{sl_price:.2f}")
        if stop_type == "StopLimit":
            sl["LimitPrice"] = f"{max(0.01, sl_price - 0.10):.2f}"
        if tp_price:
            body = {"Type": "OCO", "Orders": [leg("Limit", LimitPrice=f"{tp_price:.2f}"), sl]}
            data = self._request("POST", "/orderexecution/ordergroups", json=body)
        else:
            data = self._request("POST", "/orderexecution/orders", json=sl)
        orders = data.get("Orders", [])
        ids = [o.get("OrderID") for o in orders if o.get("OrderID")]
        failed = any(o.get("Error") or "fail" in (o.get("Message", "") or "").lower() for o in orders)
        msg = "; ".join(o.get("Message", "") for o in orders)[:200]
        return (bool(ids) and not failed), ids, msg

    def cancel_order(self, order_id: str) -> dict:
        return self._request("DELETE", f"/orderexecution/orders/{order_id}")

    def get_orders(self, account_id: str) -> list[dict]:
        data = self._request("GET", f"/brokerage/accounts/{account_id}/orders")
        return data.get("Orders", [])

    def get_order(self, account_id: str, order_id: str) -> Optional[dict]:
        for o in self.get_orders(account_id):
            if str(o.get("OrderID")) == str(order_id):
                return o
        return None

    def order_fill_price(self, account_id: str, order_id: Optional[str]) -> Optional[float]:
        """The ACTUAL executed price of an order (None if not filled / unknown)."""
        if not order_id:
            return None
        o = self.get_order(account_id, order_id)
        if not o:
            return None
        for k in ("FilledPrice", "AverageFillPrice", "AvgFillPrice"):
            v = o.get(k)
            if v not in (None, "", "0", 0):
                try:
                    return float(v)
                except (TypeError, ValueError):
                    pass
        for lg in (o.get("Legs") or []):
            v = lg.get("ExecutionPrice")
            if v not in (None, "", "0", 0):
                try:
                    return float(v)
                except (TypeError, ValueError):
                    pass
        return None

    def order_exec_qty(self, account_id: str, order_id: Optional[str]) -> int:
        """Executed (filled) contract count on an order — authoritative for THIS order, unlike the
        account position endpoint which lags aggregation and commingles other strategies' size."""
        if not order_id:
            return 0
        o = self.get_order(account_id, order_id)
        if not o:
            return 0
        total = 0
        for lg in (o.get("Legs") or []):
            for k in ("ExecQuantity", "FilledQuantity"):
                v = lg.get(k)
                if v not in (None, ""):
                    try:
                        total += int(float(v))
                        break
                    except (TypeError, ValueError):
                        pass
        return total
