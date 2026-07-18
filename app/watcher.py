"""Watcher / reconciliation module (§3.16).

Periodically compares the bot's tracked state against the broker's ACTUAL positions
and orders, and alerts (Telegram + dashboard + event log) on discrepancies:
  - unmanaged  : broker holds an option position the bot isn't managing (NO STOP) — critical
  - missing    : bot tracks a position the broker doesn't actually hold
  - qty_mismatch : tracked qty != broker qty (partial fill/close)
  - order      : a broker order is rejected/expired
  - connection : broker/API unreachable

To avoid noise from transient timing, a discrepancy must persist 2 consecutive
cycles before it alerts, and each issue alerts only once until it clears.
"""
from __future__ import annotations

import asyncio
import logging
import time

from .ts_client import TradeStationError

log = logging.getLogger("watcher")


def _is_option(sym: str) -> bool:
    # bot option-symbol format: "ROOT YYMMDD[C|P]STRIKE" -> a space then date+C/P
    if not sym or " " not in sym:
        return False
    tail = sym.split(" ", 1)[1]
    return ("C" in tail or "P" in tail) and any(ch.isdigit() for ch in tail)


def _pos_qty(p: dict) -> int:
    for k in ("Quantity", "LongQuantity", "ShortQuantity"):
        v = p.get(k)
        if v not in (None, "", 0, "0"):
            try:
                return int(float(v))
            except (ValueError, TypeError):
                continue
    return 0


class Watcher:
    def __init__(self, settings, client, store, notifier, account_id_getter):
        self.s = settings
        self.client = client
        self.store = store
        self.notifier = notifier
        self._acct = account_id_getter
        self._running = False
        self._prev: set[str] = set()
        self._alerted: set[str] = set()
        self._fails = 0
        self.connection_ok = True
        self.last_run = 0.0
        self.last_findings: list[dict] = []

    def _notify(self, text: str) -> None:
        if self.notifier is not None and self.s.telegram.enabled and self.s.watcher.alert:
            self.notifier.send(text)

    def _log(self, action: str, symbol: str, detail: str) -> None:
        try:
            self.store.log_event(kind="watcher", action=action, symbol=symbol, ok=False, detail=detail)
        except Exception:  # noqa: BLE001
            log.exception("watcher log_event failed")

    def tick(self) -> None:
        acct = self._acct()
        if not acct:
            return
        try:
            broker_pos = self.client.get_positions(acct)
            if not self.connection_ok:
                self.connection_ok = True
                self._fails = 0
                self._notify("✅ <b>Broker connection restored</b>")
        except TradeStationError as e:
            self._fails += 1
            if self.connection_ok and self._fails >= 2:
                self.connection_ok = False
                self._notify(f"⚠️ <b>Watcher: broker/API connection issue</b>\n{str(e)[:140]}")
                self._log("connection", "", "broker/API unreachable")
            return

        # broker option positions: symbol -> qty
        bpos: dict[str, int] = {}
        for p in broker_pos:
            sym = p.get("Symbol", "")
            qty = _pos_qty(p)
            if _is_option(sym) and qty != 0:
                bpos[sym] = qty
        # bot-tracked positions: symbol -> qty
        tracked: dict[str, int] = {}
        for pos in self.store.all_open():
            os_ = pos.get("option_symbol")
            if os_:
                tracked[os_] = pos.get("quantity") or 0

        findings: list[dict] = []
        for sym, q in tracked.items():
            if sym not in bpos:
                findings.append({"type": "missing", "symbol": sym,
                                 "detail": f"bot tracks {q}x {sym} but broker holds none"})
        for sym, q in bpos.items():
            if sym not in tracked:
                findings.append({"type": "unmanaged", "symbol": sym,
                                 "detail": f"broker holds {q}x {sym} — NOT managed by bot (no stop)"})
            elif tracked[sym] != q:
                findings.append({"type": "qty_mismatch", "symbol": sym,
                                 "detail": f"qty mismatch {sym}: bot {tracked[sym]} vs broker {q}"})
        # NOTE: we deliberately do NOT scan broker order history for rejected/expired orders. Those
        # are already alerted at placement (notify_errors), 0DTE expiry is normal, and re-alerting the
        # whole backlog on every restart floods the client. The watcher's job is POSITION reconciliation.

        # alert: must persist 2 cycles, once per issue until it clears
        current = {f"{f['type']}:{f['symbol']}" for f in findings}
        confirmed = current & self._prev
        bykey = {f"{f['type']}:{f['symbol']}": f for f in findings}
        for k in sorted(confirmed - self._alerted):
            f = bykey.get(k)
            if not f:
                continue
            emoji = {"unmanaged": "🚨"}.get(f["type"], "⚠️")
            self._notify(f"{emoji} <b>Watcher: {f['type']}</b>\n{f['detail']}")
            self._log(f["type"], f.get("symbol", ""), f["detail"])
            # AUTO-FLATTEN a confirmed unmanaged broker orphan: a long option the bot isn't managing
            # has NO stop and can ride to expiry (exactly what happened to a client). Closing a long
            # is always safe (can't oversell). Confirmed over 2 cycles already, so it isn't a fresh
            # entry mid-tracking. Short/negative qty (shouldn't happen — bot only buys) is left alerted.
            if (f["type"] == "unmanaged" and getattr(self.s.watcher, "auto_flatten_unmanaged", True)):
                self._flatten_orphan(acct, f["symbol"], bpos.get(f["symbol"], 0))
            # AUTO-CLEAN a confirmed phantom: the bot tracks a position the broker doesn't hold
            # (qty drift / out-of-band close). Mark it closed locally (and drop any resting exit
            # legs) so the risk monitor stops trying to exit contracts that don't exist.
            if f["type"] == "missing":
                self._clean_missing(acct, f["symbol"])

        self._alerted = confirmed
        self._prev = current
        self.last_findings = findings
        self.last_run = time.time()

    def _clean_missing(self, account_id: str, option_symbol: str) -> None:
        """The bot tracks a position the broker doesn't hold (confirmed 2 cycles) — reconcile it:
        cancel any resting bracket legs and mark the local row closed."""
        try:
            for pos in self.store.all_open():
                if pos.get("option_symbol") != option_symbol:
                    continue
                for oid in [i for i in (pos.get("bracket_ids") or "").split(",") if i]:
                    try:
                        self.client.cancel_order(oid)
                    except TradeStationError:
                        pass
                self.store.mark_closed(pos["symbol"], pos["strategy_id"])
                self._notify(f"🧹 <b>Watcher: cleaned phantom</b>\nbot tracked {pos.get('quantity')}x "
                             f"{option_symbol} but broker holds none — marked closed.")
                self._log("auto_clean", option_symbol, "phantom position marked closed (broker flat)")
        except Exception:  # noqa: BLE001
            log.exception("watcher clean_missing failed")

    def _flatten_orphan(self, account_id: str, option_symbol: str, qty: int) -> None:
        """Market-close a broker option position the bot isn't managing. Long only (SELLTOCLOSE)."""
        try:
            q = int(qty)
        except (TypeError, ValueError):
            q = 0
        if q <= 0:   # not a long (bot only opens longs) — don't auto-trade a short, just keep alerting
            return
        try:
            from .orders import parse_order_response
            data = self.client.place_option_order(
                account_id=account_id, option_symbol=option_symbol, quantity=q,
                trade_action="SELLTOCLOSE", order_type="Market",
                time_in_force=self.s.defaults.time_in_force)
            ok, oid, msg = parse_order_response(data)
            if ok:
                self._notify(f"🧹 <b>Watcher: auto-closed orphan</b>\nmarket-sold {q}x {option_symbol} "
                             f"(unmanaged, no stop). order {oid}")
                self._log("auto_flatten", option_symbol, f"auto-closed orphan {q}x — order {oid}")
            else:
                self._notify(f"⚠️ <b>Watcher: orphan auto-close FAILED</b>\n{option_symbol}: {msg}\nclose it manually.")
                self._log("auto_flatten", option_symbol, f"orphan auto-close failed: {msg}")
        except TradeStationError as e:
            self._notify(f"⚠️ <b>Watcher: orphan auto-close error</b>\n{option_symbol}: {str(e)[:120]}")
            self._log("auto_flatten", option_symbol, f"orphan auto-close error: {e}")
        except Exception:  # noqa: BLE001
            log.exception("watcher orphan flatten failed")

    async def run(self) -> None:
        if not self.s.watcher.enabled:
            log.info("Watcher disabled.")
            return
        self._running = True
        log.info("Watcher started (every %ss).", self.s.watcher.interval_sec)
        await asyncio.sleep(min(15.0, self.s.watcher.interval_sec))   # let startup settle
        while self._running:
            try:
                await asyncio.to_thread(self.tick)
            except Exception:  # noqa: BLE001 — never let the loop die
                log.exception("watcher tick error")
            await asyncio.sleep(self.s.watcher.interval_sec)

    def stop(self) -> None:
        self._running = False
