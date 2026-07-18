"""Order engine: contract sizing, fill-type -> order-type/price mapping,
and the high-level open_position / close_position flows used by the webhook
and the demo script.
"""
from __future__ import annotations

import logging
import math
import time
from typing import Optional

from .config import Settings
from .models import OrderResult
from .options import get_option_quote, resolve_contract
from .ts_client import TradeStationClient, TradeStationError


def real_pnl_prices(client, account_id: str, pos: dict, exit_order_id: Optional[str]) -> tuple[float, float]:
    """Return (entry_fill, exit_fill) from the broker's ACTUAL executed prices so P&L
    matches TradeStation exactly. Falls back to the stored entry / live bid only if a
    fill price isn't available yet."""
    entry = client.order_fill_price(account_id, pos.get("order_id")) or (pos.get("entry_price") or 0.0)
    exit_p = None
    for _ in range(8):                      # market exits fill in ~1s; poll briefly for the fill price
        exit_p = client.order_fill_price(account_id, exit_order_id)
        if exit_p:
            break
        time.sleep(0.5)
    if not exit_p:
        q = get_option_quote(client, pos.get("option_symbol"))
        exit_p = q.get("bid") or q.get("mid") or (pos.get("entry_price") or 0.0)
    return float(entry or 0.0), float(exit_p or 0.0)

log = logging.getLogger("orders")


def option_tick(price: float) -> float:
    # OPRA penny-pilot: $0.01 increments under $3.00, $0.05 at/above.
    return 0.01 if price < 3.0 else 0.05


def round_to_tick(price: float) -> float:
    tick = option_tick(price)
    return round(round(price / tick) * tick, 2)


def compute_contracts(budget_usd: float, option_ask: float, max_contracts: int) -> int:
    """Size off the ASK so we don't oversize when crossing the spread."""
    if option_ask <= 0:
        raise TradeStationError("Option ask is zero/unavailable; cannot size.")
    per_contract = option_ask * 100.0
    qty = math.floor(budget_usd / per_contract)
    qty = min(qty, max_contracts)
    return max(qty, 0)


def parse_order_response(data: dict) -> tuple[bool, Optional[str], str]:
    """TradeStation reports per-order rejections in Orders[].Error/Message, not as a
    top-level error. Return (accepted, order_id, message). Treats a failed/rejected
    order as NOT accepted so we never record a phantom position."""
    o = (data.get("Orders") or [{}])[0]
    order_id = o.get("OrderID")
    msg = (o.get("Message") or "").strip()
    err = o.get("Error") or data.get("Errors")
    status = (o.get("Status") or o.get("StatusDescription") or "").lower()
    bad_status = status in ("rejected", "expired", "cancelled", "canceled", "broken", "tradestation rejected")
    # Rejections can arrive as a free-text Message with no Error field (e.g. "Insufficient buying
    # power", "Order rejected"). Scan broadly so we never record a phantom position on a rejection.
    failed = bool(err) or bad_status or any(
        w in msg.lower() for w in ("fail", "reject", "cannot", "can not", "not allowed", "denied",
                                   "insufficient", "buying power", "margin", "unable", "invalid",
                                   "halt", "not permitted", "no position", "too small")
    )
    if err and not msg:
        msg = str(err)
    return (order_id is not None and not failed), order_id, msg


def fill_to_order(fill: str, quote: dict) -> tuple[str, Optional[float]]:
    """Map a fill type to (OrderType, limit_price)."""
    f = fill.lower()
    if f == "market":
        return "Market", None
    if f == "ask":
        price = quote["ask"]
    elif f == "bid":
        price = quote["bid"]
    else:  # mid (default)
        price = quote["mid"]
    if not price:
        return "Market", None
    return "Limit", round_to_tick(price)


class OrderEngine:
    def __init__(self, settings: Settings, client: TradeStationClient):
        self.s = settings
        self.client = client

    def _budget(self, account_id: str, dollars: Optional[float], percent: Optional[float]) -> float:
        sz = self.s.sizing
        mode = sz.mode
        if dollars is not None:
            return float(dollars)
        if percent is not None:
            return self.client.get_equity(account_id) * (float(percent) / 100.0)
        if mode == "percent":
            return self.client.get_equity(account_id) * (sz.percent / 100.0)
        return float(sz.dollars)

    def _budget_from_equity(self, equity: float, dollars: Optional[float], percent: Optional[float]) -> float:
        """Budget without any network call (uses pre-cached equity)."""
        sz = self.s.sizing
        if dollars is not None:
            return float(dollars)
        if percent is not None:
            return equity * (float(percent) / 100.0)
        if sz.mode == "percent":
            return equity * (sz.percent / 100.0)
        return float(sz.dollars)

    # ── fast paths (hot): take pre-fetched quote + equity; only the POST hits the network
    def place_entry_fast(
        self,
        account_id: str,
        option_symbol: str,
        quote: dict,
        equity: float,
        fill: str,
        dollars: Optional[float] = None,
        percent: Optional[float] = None,
        max_contracts: Optional[int] = None,
    ) -> OrderResult:
        budget = self._budget_from_equity(equity, dollars, percent)
        cap = int(max_contracts) if max_contracts else self.s.sizing.max_contracts
        qty = compute_contracts(budget, quote["ask"], cap)
        if qty < 1:
            return OrderResult(
                ok=False,
                message=f"Budget ${budget:.0f} too small for 1 contract at ask ${quote['ask']:.2f}.",
                option_symbol=option_symbol,
            )
        if qty == cap and math.floor(budget / (quote["ask"] * 100.0)) > cap:
            # be loud when the cap binds — "why did it buy 10 not 20?" must be answerable from the log
            log.info("Sizing %s: budget $%.0f wants %d contracts at ask %.2f — CAPPED at max_contracts=%d.",
                     option_symbol, budget, math.floor(budget / (quote["ask"] * 100.0)), quote["ask"], cap)
        order_type, limit_price = fill_to_order(fill, quote)
        data = self.client.place_option_order(
            account_id=account_id, option_symbol=option_symbol, quantity=qty,
            trade_action="BUYTOOPEN", order_type=order_type, limit_price=limit_price,
            time_in_force=self.s.defaults.time_in_force,
        )
        ok, order_id, msg = parse_order_response(data)
        entry_ref = limit_price if limit_price is not None else (quote.get("mid") or quote.get("ask"))
        return OrderResult(
            ok=ok,
            message=f"BUYTOOPEN {qty}x {option_symbol} ({order_type}). {msg}",
            order_id=order_id, option_symbol=option_symbol, quantity=qty if ok else 0,
            limit_price=limit_price, entry_ref_price=entry_ref, order_type=order_type, raw=data,
        )

    def place_exit_fast(
        self,
        account_id: str,
        option_symbol: str,
        quantity: int,
        fill: str,
        quote: Optional[dict] = None,
    ) -> OrderResult:
        # market exit needs no quote at all -> fastest. Limit exits need a price.
        if fill.lower() == "market" or not quote:
            order_type, limit_price = "Market", None
        else:
            order_type, limit_price = fill_to_order(fill, quote)
        data = self.client.place_option_order(
            account_id=account_id, option_symbol=option_symbol, quantity=quantity,
            trade_action="SELLTOCLOSE", order_type=order_type, limit_price=limit_price,
            time_in_force=self.s.defaults.time_in_force,
        )
        ok, order_id, msg = parse_order_response(data)
        return OrderResult(
            ok=ok,
            message=f"SELLTOCLOSE {quantity}x {option_symbol} ({order_type}). {msg}",
            order_id=order_id, option_symbol=option_symbol, quantity=quantity,
            limit_price=limit_price, order_type=order_type, raw=data,
        )

    def open_position(
        self,
        account_id: str,
        underlying: str,
        right: str,                      # call | put
        expiration_mode: str,
        strike_selection: str,
        strike_offset: int,
        fill: str,
        dollars: Optional[float] = None,
        percent: Optional[float] = None,
        max_contracts: Optional[int] = None,
    ) -> OrderResult:
        contract = resolve_contract(
            self.client, underlying, right, expiration_mode, strike_selection, strike_offset
        )
        quote = contract["quote"]
        budget = self._budget(account_id, dollars, percent)
        cap = int(max_contracts) if max_contracts else self.s.sizing.max_contracts
        qty = compute_contracts(budget, quote["ask"], cap)
        if qty == cap and math.floor(budget / (quote["ask"] * 100.0)) > cap:
            log.info("Sizing %s: budget $%.0f wants %d contracts at ask %.2f — CAPPED at max_contracts=%d.",
                     contract["option_symbol"], budget, math.floor(budget / (quote["ask"] * 100.0)), quote["ask"], cap)
        if qty < 1:
            return OrderResult(
                ok=False,
                message=f"Budget ${budget:.0f} too small for 1 contract at ask ${quote['ask']:.2f}.",
                option_symbol=contract["option_symbol"],
            )

        order_type, limit_price = fill_to_order(fill, quote)
        data = self.client.place_option_order(
            account_id=account_id,
            option_symbol=contract["option_symbol"],
            quantity=qty,
            trade_action="BUYTOOPEN",
            order_type=order_type,
            limit_price=limit_price,
            time_in_force=self.s.defaults.time_in_force,
        )
        ok, order_id, msg = parse_order_response(data)
        # reference premium for risk tracking: the limit we sent, else live mid
        entry_ref = limit_price if limit_price is not None else (quote.get("mid") or quote.get("ask"))
        return OrderResult(
            ok=ok,
            message=f"BUYTOOPEN {qty}x {contract['option_symbol']} ({order_type}). {msg}",
            order_id=order_id,
            option_symbol=contract["option_symbol"],
            quantity=qty if ok else 0,
            limit_price=limit_price,
            entry_ref_price=entry_ref,
            order_type=order_type,
            raw=data,
        )

    def close_position(
        self,
        account_id: str,
        option_symbol: str,
        quantity: int,
        fill: str,
    ) -> OrderResult:
        quote = get_option_quote(self.client, option_symbol)
        order_type, limit_price = fill_to_order(fill, quote)
        data = self.client.place_option_order(
            account_id=account_id,
            option_symbol=option_symbol,
            quantity=quantity,
            trade_action="SELLTOCLOSE",
            order_type=order_type,
            limit_price=limit_price,
            time_in_force=self.s.defaults.time_in_force,
        )
        ok, order_id, msg = parse_order_response(data)
        return OrderResult(
            ok=ok,
            message=f"SELLTOCLOSE {quantity}x {option_symbol} ({order_type}). {msg}",
            order_id=order_id,
            option_symbol=option_symbol,
            quantity=quantity,
            limit_price=limit_price,
            order_type=order_type,
            raw=data,
        )
