"""MarketCache — the low-latency engine.

A background loop keeps, for each configured symbol, the data an order needs
ALREADY in memory:
  - the target expiration (0DTE / NEXT)
  - the ATM (+offset) CALL and PUT option symbols
  - their live bid/ask/mid quotes
  - account equity / buying power

So when a webhook arrives, the hot path is just: dict read + arithmetic + one
order POST over a warm keep-alive connection. No expirations/strikes/quote/balance
round-trips on the critical path.

If the cache is cold or stale (loop died, symbol not configured), callers fall
back to the slower resolve_contract path so trading never silently stops.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from .config import Settings
from .options import build_option_symbol, pick_expiration, select_strike
from .ts_client import TradeStationClient, TradeStationError

log = logging.getLogger("cache")


@dataclass
class HotContract:
    underlying: str
    expiration: str
    underlying_price: float
    call_symbol: str
    put_symbol: str
    call_quote: dict
    put_quote: dict
    updated: float = field(default_factory=time.monotonic)

    def symbol_for(self, right: str) -> str:
        return self.call_symbol if right == "call" else self.put_symbol

    def quote_for(self, right: str) -> dict:
        return self.call_quote if right == "call" else self.put_quote


def _quote_dict(q: dict) -> dict:
    bid = float(q.get("Bid", 0) or 0)
    ask = float(q.get("Ask", 0) or 0)
    last = float(q.get("Last", 0) or 0)
    mid = round((bid + ask) / 2, 2) if (bid and ask) else (last or ask or bid)
    return {"bid": bid, "ask": ask, "mid": mid, "last": last}


def _chunks(items: list, n: int):
    for i in range(0, len(items), n):
        yield items[i:i + n]


class MarketCache:
    def __init__(self, settings: Settings, client: TradeStationClient):
        self.s = settings
        self.client = client
        self._lock = threading.Lock()
        self._hot: dict[str, HotContract] = {}
        self._chain: dict[str, tuple] = {}      # symbol -> (expiration_date, strikes, ts)
        self._equity: float = 0.0
        self._equity_ts: float = 0.0
        # real TradeStation account P&L (from balances) — authoritative, not bot-calculated
        self._realized: Optional[float] = None
        self._unrealized: Optional[float] = None
        self._todays: Optional[float] = None
        self._running = False

    # ── reads (hot path) ───────────────────────────────────────────────────────
    def get(self, symbol: str) -> Optional[HotContract]:
        with self._lock:
            hot = self._hot.get(symbol.upper())
        if hot and (time.monotonic() - hot.updated) <= self.s.cache.stale_after_sec:
            return hot
        return None

    def equity(self) -> float:
        with self._lock:
            return self._equity

    def pnl(self) -> dict:
        """Real TradeStation account P&L from the latest balances poll (None until primed)."""
        with self._lock:
            return {"realized": self._realized, "unrealized": self._unrealized, "todays": self._todays}

    def quote_for_symbol(self, option_symbol: str) -> Optional[dict]:
        with self._lock:
            for hot in self._hot.values():
                if hot.call_symbol == option_symbol:
                    return hot.call_quote
                if hot.put_symbol == option_symbol:
                    return hot.put_quote
        return None

    # ── refreshers ───────────────────────────────────────────────────────────--
    def _refresh_chain(self, symbol: str) -> None:
        exp = pick_expiration(self.client, symbol, self.s.defaults.expiration)
        strikes = self.client.get_option_strikes(symbol, exp.strftime("%m-%d-%Y"))
        with self._lock:
            self._chain[symbol] = (exp, strikes, time.monotonic())

    def _refresh_hot(self, symbol: str) -> None:
        with self._lock:
            chain = self._chain.get(symbol)
        if not chain:
            return
        exp, strikes, _ = chain
        if not strikes:
            return
        # underlying price (one quote), then compute call/put strikes
        uq = self.client.get_quote(symbol)
        price = float(uq.get("Last") or uq.get("Close") or 0) or (
            (float(uq.get("Bid", 0) or 0) + float(uq.get("Ask", 0) or 0)) / 2
        )
        if price <= 0:
            return
        d = self.s.defaults
        call_strike = select_strike(strikes, price, "call", d.strike_selection, d.strike_offset)
        put_strike = select_strike(strikes, price, "put", d.strike_selection, d.strike_offset)
        call_sym = build_option_symbol(symbol, exp, "call", call_strike)
        put_sym = build_option_symbol(symbol, exp, "put", put_strike)
        quotes = self.client.get_quotes([call_sym, put_sym])
        hot = HotContract(
            underlying=symbol,
            expiration=exp.isoformat(),
            underlying_price=price,
            call_symbol=call_sym,
            put_symbol=put_sym,
            call_quote=_quote_dict(quotes.get(call_sym, {})),
            put_quote=_quote_dict(quotes.get(put_sym, {})),
        )
        with self._lock:
            self._hot[symbol] = hot

    def _refresh_hot_batch(self, symbols: list[str]) -> None:
        """Batched hot refresh for ALL symbols at once: ONE quotes request for every
        underlying, then chunked quotes requests for all option legs. This keeps the
        request count tiny (~2-3/cycle) regardless of how many symbols are configured,
        so a large symbol list stays under the TradeStation quote rate limit."""
        with self._lock:
            chains = {s: self._chain.get(s) for s in symbols}
        ready = [s for s in symbols if chains.get(s) and chains[s][1]]
        if not ready:
            return
        # 1) every underlying price in ONE request
        uq = self.client.get_quotes(ready)
        d = self.s.defaults
        plan: dict[str, tuple] = {}          # symbol -> (price, exp, call_sym, put_sym)
        option_syms: list[str] = []
        for s in ready:
            q = uq.get(s, {})
            price = float(q.get("Last") or q.get("Close") or 0) or (
                (float(q.get("Bid", 0) or 0) + float(q.get("Ask", 0) or 0)) / 2
            )
            if price <= 0:
                continue
            exp, strikes, _ = chains[s]
            cs = select_strike(strikes, price, "call", d.strike_selection, d.strike_offset)
            ps = select_strike(strikes, price, "put", d.strike_selection, d.strike_offset)
            call_sym = build_option_symbol(s, exp, "call", cs)
            put_sym = build_option_symbol(s, exp, "put", ps)
            plan[s] = (price, exp, call_sym, put_sym)
            option_syms += [call_sym, put_sym]
        if not option_syms:
            return
        # 2) all option legs in chunked batched requests
        oq: dict[str, dict] = {}
        for chunk in _chunks(option_syms, 25):
            oq.update(self.client.get_quotes(chunk))
        # 3) publish hot contracts
        with self._lock:
            for s, (price, exp, call_sym, put_sym) in plan.items():
                self._hot[s] = HotContract(
                    underlying=s, expiration=exp.isoformat(), underlying_price=price,
                    call_symbol=call_sym, put_symbol=put_sym,
                    call_quote=_quote_dict(oq.get(call_sym, {})),
                    put_quote=_quote_dict(oq.get(put_sym, {})),
                )

    def _refresh_equity(self) -> None:
        account_id = self.client.resolve_account_id()
        bal = self.client.get_balances(account_id)   # one call gives equity AND real P&L

        def _num(d, key):
            v = (d or {}).get(key)
            try:
                return float(v) if v not in (None, "") else None
            except (TypeError, ValueError):
                return None

        eq = None
        for k in ("Equity", "CashBalance", "BuyingPower"):
            eq = _num(bal, k)
            if eq is not None:
                break
        bd = bal.get("BalanceDetail") or {}
        realized = _num(bd, "RealizedProfitLoss")
        unrealized = _num(bd, "UnrealizedProfitLoss")
        todays = _num(bal, "TodaysProfitLoss")
        with self._lock:
            if eq is not None:
                self._equity = eq
            self._realized = realized
            self._unrealized = unrealized
            self._todays = todays
            self._equity_ts = time.monotonic()

    def prime(self) -> None:
        """Synchronous first fill so the first webhook is already hot."""
        symbols = [s.upper() for s in self.s.cache.symbols]
        for sym in symbols:
            try:
                self._refresh_chain(sym)
            except TradeStationError as e:
                log.warning("prime chain %s failed: %s", sym, e)
        try:
            self._refresh_hot_batch(symbols)
        except TradeStationError as e:
            log.warning("prime hot-batch failed: %s", e)
        try:
            self._refresh_equity()
        except TradeStationError as e:
            log.warning("prime equity failed: %s", e)
        log.info("Cache primed for %d symbols | %d hot | equity $%.0f",
                 len(symbols), len(self._hot), self._equity)

    # ── background loop ─────────────────────────────────────────────────────────
    async def run(self) -> None:
        self._running = True
        c = self.s.cache
        symbols = [s.upper() for s in c.symbols]
        last_chain = {s: 0.0 for s in symbols}
        last_equity = 0.0
        log.info("MarketCache started for %s (hot=%ss chain=%ss equity=%ss)",
                 symbols, c.hot_refresh_sec, c.chain_refresh_sec, c.equity_refresh_sec)
        while self._running:
            now = time.monotonic()
            # strikes can't be batched (one symbol per call) — refresh only when stale
            for sym in symbols:
                if now - last_chain[sym] >= c.chain_refresh_sec:
                    try:
                        await asyncio.to_thread(self._refresh_chain, sym)
                        last_chain[sym] = now
                    except Exception:  # noqa: BLE001 — never let the loop die
                        log.exception("chain refresh failed for %s", sym)
            # ONE batched hot refresh for every symbol (tiny request count)
            try:
                await asyncio.to_thread(self._refresh_hot_batch, symbols)
            except Exception:  # noqa: BLE001
                log.exception("batched hot refresh failed")
            if now - last_equity >= c.equity_refresh_sec:
                try:
                    await asyncio.to_thread(self._refresh_equity)
                    last_equity = now
                except Exception:  # noqa: BLE001
                    log.exception("cache equity refresh failed")
            await asyncio.sleep(c.hot_refresh_sec)

    def stop(self) -> None:
        self._running = False
