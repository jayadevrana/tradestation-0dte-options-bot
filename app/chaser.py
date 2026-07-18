"""Order-level retry / limit-chase (§3.9).

Runs in a background thread for LIMIT orders only (market orders fill instantly).
After a limit order is placed, polls its fill status; if it hasn't filled within
retry.interval_sec it cancels, re-prices toward the marketable side, and re-submits
— up to retry.max_attempts — then optionally falls back to a market order.

  entry mode: on total failure (no fill, no fallback) cancels + marks the position
              closed (it never really opened).
  exit  mode: on fill, marks the position closed + records the trade; on total
              failure leaves the position open and alerts ("exit unfilled").

Never blocks the webhook (own thread). Every attempt is logged + alerted.
"""
from __future__ import annotations

import logging
import threading
import time

from .options import get_option_quote
from .orders import option_tick, parse_order_response, real_pnl_prices, round_to_tick
from .tradelog import record_exit
from .ts_client import TradeStationError

log = logging.getLogger("chaser")


def order_status(o: dict | None) -> str:
    if not o:
        return "unknown"
    s = (o.get("StatusDescription") or o.get("Status") or "").lower()
    if "filled" in s and "partial" not in s:
        return "filled"
    if "partial" in s:
        return "partial"
    if any(x in s for x in ("reject", "expir", "cancel", "out", "broken")):
        return "dead"
    return "working"   # received / queued / sent / working / open


def reprice(quote: dict, trade_action: str, prev_limit: float, step_ticks: int) -> float:
    base = prev_limit or quote.get("mid") or quote.get("ask") or 0.05
    tick = option_tick(base)
    if trade_action == "BUYTOOPEN":                       # chase UP toward the ask
        target = round_to_tick(base + step_ticks * tick)
        ask = round_to_tick(quote.get("ask") or target)
        return min(target, ask) if ask else target
    target = round_to_tick(base - step_ticks * tick)      # SELLTOCLOSE: chase DOWN toward the bid
    bid = round_to_tick(quote.get("bid") or target)
    return max(target, bid) if bid else target


class Chaser:
    def __init__(self, settings, client, store, notifier):
        self.s = settings
        self.client = client
        self.store = store
        self.notifier = notifier

    def applies(self, kind: str) -> bool:  # kind: "entry" | "exit"
        r = self.s.retry
        return r.enabled and (r.applies_to == "both" or r.applies_to == kind + "s")

    def spawn(self, **kw) -> None:
        threading.Thread(target=self._run, kwargs=kw, daemon=True, name="chaser").start()

    def _notify(self, text: str) -> None:
        if self.notifier is not None and self.s.telegram.enabled:
            self.notifier.send(text)

    def _log(self, action: str, kw: dict, detail: str) -> None:
        try:
            self.store.log_event(kind="retry", action=action, symbol=kw["symbol"],
                                 strategy_id=kw["strategy_id"], option_symbol=kw["option_symbol"],
                                 ok=True, detail=detail)
        except Exception:  # noqa: BLE001
            log.exception("chaser log_event failed")

    def _run(self, mode, account_id, symbol, strategy_id, option_symbol,
             trade_action, quantity, order_id, limit_price,
             max_attempts=None, interval_sec=None) -> None:
        kw = {"symbol": symbol, "strategy_id": strategy_id, "option_symbol": option_symbol}
        r = self.s.retry
        # per-basket retry override (falls back to global retry config)
        max_att = int(max_attempts) if max_attempts else r.max_attempts
        interval = float(interval_sec) if interval_sec else r.interval_sec
        oid, price = order_id, limit_price
        try:
            attempts = 0
            while attempts < max_att:
                time.sleep(interval)
                st = order_status(self.client.get_order(account_id, oid))
                if st == "filled":
                    if mode == "exit":
                        self._finalize_exit(account_id, symbol, strategy_id, option_symbol, oid,
                                            "signal-exit", retry_count=attempts)
                    self._log("filled", kw, f"limit filled after {attempts} retr(ies)")
                    self._notify(f"✅ <b>Chase filled</b> {symbol} · {option_symbol} (after {attempts} retries)")
                    return
                # working / partial / dead -> cancel + reprice + resubmit
                if st != "dead":
                    try:
                        self.client.cancel_order(oid)
                        time.sleep(0.4)
                    except TradeStationError:
                        pass
                quote = get_option_quote(self.client, option_symbol)
                price = reprice(quote, trade_action, price, r.price_step_ticks * (attempts + 1))
                data = self.client.place_option_order(
                    account_id=account_id, option_symbol=option_symbol, quantity=quantity,
                    trade_action=trade_action, order_type="Limit", limit_price=price,
                    time_in_force=self.s.defaults.time_in_force)
                ok, new_oid, _ = parse_order_response(data)
                if ok and new_oid:
                    oid = new_oid
                if mode == "entry":
                    self.store.update_open(symbol, strategy_id, order_id=oid)
                self._log("retry", kw, f"retry {attempts + 1}/{max_att} @ {price} ({st})")
                attempts += 1

            # attempts exhausted
            if r.market_fallback:
                try:
                    self.client.cancel_order(oid)
                    time.sleep(0.4)
                except TradeStationError:
                    pass
                data = self.client.place_option_order(
                    account_id=account_id, option_symbol=option_symbol, quantity=quantity,
                    trade_action=trade_action, order_type="Market",
                    time_in_force=self.s.defaults.time_in_force)
                ok, moid, _ = parse_order_response(data)
                if mode == "entry" and ok and moid:
                    self.store.update_open(symbol, strategy_id, order_id=moid)
                if mode == "exit" and ok:
                    self._finalize_exit(account_id, symbol, strategy_id, option_symbol, moid,
                                        "signal-exit (mkt fallback)", retry_count=max_att)
                self._log("market_fallback", kw, "market fallback after retries")
                self._notify(f"⏱️ <b>Chase → market</b> {symbol} · {option_symbol}\nunfilled after {max_att} retries — sent market order")
            else:
                if mode == "entry":
                    try:
                        self.client.cancel_order(oid)
                    except TradeStationError:
                        pass
                    self.store.mark_closed(symbol, strategy_id)
                    self._notify(f"⚠️ <b>Entry unfilled</b> {symbol} · {option_symbol} — cancelled after {max_att} retries")
                else:
                    self._notify(f"⚠️ <b>Exit unfilled</b> {symbol} · {option_symbol} — STILL OPEN after {max_att} retries; manage manually")
                self._log("unfilled", kw, "gave up after retries (no fallback)")
        except Exception:  # noqa: BLE001 — never let the chase thread crash the bot
            log.exception("chaser run error")

    def _finalize_exit(self, account_id, symbol, strategy_id, option_symbol, exit_order_id, reason,
                       retry_count=None) -> None:
        pos = self.store.get_open(symbol, strategy_id)
        if not pos:
            return
        entry_real, exit_real = real_pnl_prices(self.client, account_id, pos, exit_order_id)
        record_exit(self.store, self.s, pos, entry_real, exit_real, reason,
                    exit_method="limit-chase", client=self.client, account_id=account_id,
                    retry_count=retry_count)
        self.store.mark_closed(symbol, strategy_id)
