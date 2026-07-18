"""Broker-side exits attached at entry — survives a bot/server outage.

Two modes (execution.exit_mode):

  "bracket"  pure broker exit. After an entry fills, attach an OCO of
                 SELLTOCLOSE Limit  @ entry*(1 + take_profit_pct)   (per-group TP)
                 SELLTOCLOSE <Stop> @ entry*(1 - stop_loss_pct)     (per-group SL)
             priced off the REAL fill. The bot does NOT manage exits (no trail/trim);
             it only reconciles when a leg fills.

  "hybrid"   broker holds a death-proof STOP backstop ONLY, set BELOW the bot's own
             stop (entry*(1 - (stop_loss_pct + backstop_extra_pct))). The bot keeps
             managing trail/trim/break-even/take-profit in app/risk.py; the broker stop
             only fires if the bot/server is down. On a bot-side close the backstop is
             cancelled; on a trim it is resized to the remaining quantity.

Either way the entry must be CONFIRMED FILLED at the broker before we attach (so we
never place a SELLTOCLOSE on 0 contracts), and we protect the quantity actually held.

  reconcile(): a stored leg filled -> record the trade (real fill) + mark closed + cancel siblings
  cancel():    cancel the stored leg(s) on a TradingView exit / reverse / orphan cleanup
  resize():    (hybrid) cancel + re-place the stop at a new remaining quantity after a trim
The account-level daily-loss kill-switch still flattens + cancels everything.
"""
from __future__ import annotations

import logging
import threading
import time

from .chaser import order_status
from .groups import effective_risk
from .orders import real_pnl_prices, round_to_tick
from .tradelog import record_exit
from .ts_client import TradeStationError

log = logging.getLogger("bracket")


class BracketManager:
    def __init__(self, settings, client, store, notifier):
        self.s = settings
        self.client = client
        self.store = store
        self.notifier = notifier

    def _notify(self, text: str) -> None:
        if self.notifier is not None and self.s.telegram.enabled:
            self.notifier.send(text)

    def _set_ids(self, symbol, strategy_id, ids) -> None:
        self.store.update_open(symbol, strategy_id, bracket_ids=",".join(str(i) for i in ids))

    # ── price levels for the current mode ─────────────────────────────────────
    def _levels(self, pos: dict, fill: float):
        """Return (tp_price, sl_price). Per-signal tp_pct/sl_pct on the position override the
        symbol's config-group values; otherwise the group values apply."""
        r = effective_risk(self.s, pos.get("symbol"))
        sl_pct = pos.get("sl_pct") if pos.get("sl_pct") is not None else r.stop_loss_pct
        tp_pct = pos.get("tp_pct") if pos.get("tp_pct") is not None else r.take_profit_pct
        if self.s.execution.exit_mode == "hybrid":
            # The broker holds BOTH the stop and the take-profit, at the real levels, evaluated on
            # the broker's OWN fill data — so neither can be false-fired by a garbage sim quote
            # (the bot-side quote stop/TP are skipped in hybrid; see risk._manage). The bot keeps
            # only the dynamic exits (trim / break-even).
            sl = round_to_tick(fill * (1 - sl_pct))
            tp = round_to_tick(fill * (1 + tp_pct)) if tp_pct else None
            return tp, sl
        # pure bracket: stop at the bot stop level, plus the take-profit limit if set
        sl = round_to_tick(fill * (1 - sl_pct))
        tp = round_to_tick(fill * (1 + tp_pct)) if tp_pct else None
        return tp, sl

    # ── attach (background, after entry CONFIRMED filled) ──────────────────────
    def spawn_attach(self, **kw) -> None:
        threading.Thread(target=self._attach, kwargs=kw, daemon=True, name="bracket").start()

    def _attach(self, account_id, symbol, strategy_id, option_symbol, quantity) -> None:
        try:
            # Size the protective exit off the ENTRY ORDER's executed quantity — NOT the account
            # position endpoint. The position endpoint lags order aggregation: a 2-lot market order
            # can be fully Filled while the position still reports 1 for a moment. Sizing off that
            # lagging value protected 1 of 2 and left the other contract naked (a real client
            # incident: client had to manually exit the unprotected leg). The order's ExecQuantity is
            # authoritative for THIS entry (also correct under commingling — it's only this order's fills).
            fill, exec_qty, held_avg = None, 0, 0.0
            for _ in range(60):
                pos = self.store.get_open(symbol, strategy_id)
                if not pos:
                    return                            # position already gone (e.g. chase gave up)
                oid = pos.get("order_id")
                o = self.client.get_order(account_id, oid)
                exec_qty = self.client.order_exec_qty(account_id, oid)
                fill = self.client.order_fill_price(account_id, oid)
                _, held_avg = self.client.held_position(account_id, option_symbol)
                st = order_status(o)
                if exec_qty > 0 and (fill or held_avg):
                    # full fill in, OR the entry order is terminal (a partial is now final)
                    if (not quantity or exec_qty >= int(quantity)) or st in ("filled", "dead"):
                        break
                time.sleep(0.5)
            pos = self.store.get_open(symbol, strategy_id)
            if not pos:
                return
            if exec_qty <= 0:
                # entry never filled within the window — nothing to protect, don't place
                # a doomed SELLTOCLOSE. If a chase is still working it will mark closed itself.
                self.store.log_event(kind="bracket", action="attach", symbol=symbol,
                                     strategy_id=strategy_id, option_symbol=option_symbol, ok=False,
                                     detail="entry not filled in time — exit not attached")
                self._notify(f"⚠️ <b>Broker exit not set</b> {symbol} · {option_symbol}\n"
                             f"entry not confirmed filled — manage manually if it fills.")
                return
            # protect THIS entry's executed share (handles a genuine partial fill: exec < requested)
            quantity = min(int(quantity), exec_qty) if quantity else exec_qty
            fill = fill or held_avg or (pos.get("entry_price") or 0.0)
            if not fill:
                return
            # store the real entry; reseed peak/trough off it (they were seeded to the pre-fill
            # reference at open) so trailing/break-even baselines aren't skewed.
            self.store.update_open(symbol, strategy_id, entry_price=fill, peak_price=fill, trough_price=fill)

            tp_price, sl_price = self._levels(pos, fill)
            ok, ids, msg = self.client.place_exit_bracket(
                account_id, option_symbol, quantity, tp_price, sl_price,
                self.s.execution.bracket_stop_type)
            kind = "OCO TP/SL" if tp_price else "stop backstop"
            if ok and ids:
                self._set_ids(symbol, strategy_id, ids)
                self.store.log_event(kind="bracket", action="attach", symbol=symbol,
                                     strategy_id=strategy_id, option_symbol=option_symbol, ok=True,
                                     detail=f"{kind}: TP {tp_price or '—'} / SL {sl_price} (entry {fill})")
                self._notify(f"🎯 <b>Broker exit set</b> {symbol} · {option_symbol}\n"
                             f"entry {fill} · TP {tp_price or '—'} · SL {sl_price} ({kind})")
            else:
                self.store.log_event(kind="bracket", action="attach", symbol=symbol,
                                     strategy_id=strategy_id, option_symbol=option_symbol, ok=False,
                                     detail=f"attach FAILED: {msg}")
                self._notify(f"⚠️ <b>Broker exit FAILED</b> {symbol} · {option_symbol}\n{msg}")
        except Exception:  # noqa: BLE001
            log.exception("bracket attach failed")

    # ── resize the stop after a partial trim (hybrid) ─────────────────────────
    def resize(self, account_id, pos, new_qty) -> None:
        """After the bot trims, cancel the old stop and re-place it for the remaining qty
        so a later backstop fire can't try to sell more than we hold."""
        try:
            new_qty = int(new_qty)
            self.cancel(account_id, pos)
            if new_qty <= 0:
                self.store.update_open(pos["symbol"], pos["strategy_id"], bracket_ids="")
                return
            fill = pos.get("entry_price") or 0.0
            if fill <= 0:
                self.store.update_open(pos["symbol"], pos["strategy_id"], bracket_ids="")
                return
            _, sl_price = self._levels(pos, fill)
            ok, ids, _msg = self.client.place_exit_bracket(
                account_id, pos["option_symbol"], new_qty, None, sl_price,
                self.s.execution.bracket_stop_type)
            self._set_ids(pos["symbol"], pos["strategy_id"], ids if (ok and ids) else [])
        except Exception:  # noqa: BLE001
            log.exception("bracket resize failed (non-fatal)")

    # ── cancel the leg(s) (exit signal / reverse / orphan cleanup) ────────────
    def cancel(self, account_id, pos) -> None:
        for oid in [i for i in (pos.get("bracket_ids") or "").split(",") if i]:
            try:
                self.client.cancel_order(oid)
            except TradeStationError:
                pass

    # ── reconcile: did a leg fill? record + close + cancel sibling(s) ──────────
    def reconcile(self, account_id, pos) -> bool:
        ids = [i for i in (pos.get("bracket_ids") or "").split(",") if i]
        if not ids:
            return False
        filled_id, reason = None, "broker exit"
        for oid in ids:
            o = self.client.get_order(account_id, oid)
            if order_status(o) == "filled":
                otype = (o.get("OrderType") or (o.get("Legs") or [{}])[0].get("OrderType") or "").lower()
                reason = "take-profit (broker)" if "limit" in otype else "stop-loss (broker backstop)"
                filled_id = oid
                break
        if not filled_id:
            return False
        for oid in ids:                                # cancel the sibling(s)
            if oid != filled_id:
                try:
                    self.client.cancel_order(oid)
                except TradeStationError:
                    pass
        entry_real, exit_real = real_pnl_prices(self.client, account_id, pos, filled_id)
        qty = pos.get("quantity") or 0
        record_exit(self.store, self.s, pos, entry_real, exit_real, reason,
                    exit_method="broker bracket", client=self.client, account_id=account_id)
        self.store.mark_closed(pos["symbol"], pos["strategy_id"])
        self.store.log_event(kind="exit", action="exit", symbol=pos["symbol"], strategy_id=pos["strategy_id"],
                             option_symbol=pos["option_symbol"], ok=True, detail=reason)
        self._notify(f"✅ <b>{reason}</b> {pos['symbol']} · {pos['option_symbol']}\n"
                     f"exit {exit_real} · P&L ${round((exit_real - entry_real) * qty * 100, 2)}")
        return True
