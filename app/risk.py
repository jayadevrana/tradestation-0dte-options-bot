"""Risk monitor — the always-on watcher that enforces exits.

Per tick (every execution.poll_interval_sec):
  1. Roll the daily session baseline at a new trading day.
  2. Daily-loss kill-switch: if day PnL <= -daily_max_loss_usd, cancel all open
     orders + market-close every position + HALT new entries for the day.
  3. For each open position, on the live option premium:
       - 15% stop loss  -> close remaining
       - partial trims  -> sell a fraction at each profit target
       - trailing stop  -> close remaining once price falls trail_pct off the peak

The 'halted' flag and the day-start equity baseline are persisted in the Store,
so a restart preserves a tripped kill-switch and the loss baseline.

Design note: stops/trailing are managed HERE on the streamed/polled premium and
exit with a marketable order — deliberately, because native option stop orders
fill catastrophically in 0DTE liquidity gaps. For production, also attach a
far-out resting broker OCO stop at entry as a death-proof backstop.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import math
import statistics
import time
from collections import namedtuple
from typing import Optional

_Trim = namedtuple("_Trim", "at_profit_pct close_pct")   # per-signal single-level trim override

from .config import Settings
from .groups import effective_risk
from .options import get_option_quote
from .orders import OrderEngine, real_pnl_prices
from .schedule import flatten_due
from .state import Store
from .tradelog import record_exit
from .ts_client import TradeStationClient, TradeStationError

log = logging.getLogger("risk")


class RiskMonitor:
    def __init__(self, settings: Settings, client: TradeStationClient,
                 engine: OrderEngine, store: Store, notifier=None, bracket=None):
        self.s = settings
        self.client = client
        self.engine = engine
        self.store = store
        self.notifier = notifier
        self.bracket = bracket
        self._running = False
        self._breach: dict[str, int] = {}   # debounce: consecutive ticks a trigger has held
        self._close_rejected: set[str] = set()   # positions whose close the broker rejected (alert-dedupe)
        self._bids: dict[str, list] = {}    # recent bids per position (median-smoothed decision price)
        self._lasts: dict[str, list] = {}   # recent LAST-TRADED prices per position (max-drawdown stat only)
        self._exit_at: dict[str, float] = {}  # monotonic time of the last exit order per position (cooldown)

    def _notify(self, text: str) -> None:
        if self.notifier is not None:
            self.notifier.send(text)

    def _confirm(self, key: str, name: str, breached: bool, need: int) -> bool:
        """Debounce a bid-threshold trigger: only True after it holds `need` ticks in a row.
        A single bad/stale quote tick (e.g. a garbage low bid) can't fire an exit."""
        ck = f"{key}:{name}"
        if not breached:
            self._breach.pop(ck, None)
            return False
        self._breach[ck] = self._breach.get(ck, 0) + 1
        return self._breach[ck] >= max(1, need)

    # ── session / halt state ──────────────────────────────────────────────────
    @staticmethod
    def _today() -> str:
        return dt.date.today().isoformat()

    def is_halted(self) -> bool:
        return self.store.get_meta("halted") == self._today()

    def entries_halted(self) -> bool:
        """New entries blocked — either the hard daily-loss flatten OR the soft
        max-losing-trades stop-for-the-day (open positions still managed)."""
        return self.is_halted() or self.store.get_meta("entries_halted") == self._today()

    def daily_loss_limit(self) -> float:
        """Effective daily-loss limit: the dashboard runtime override if set, else config."""
        v = self.store.get_meta("daily_max_loss_override")
        try:
            return float(v) if v not in (None, "") else float(self.s.risk.daily_max_loss_usd)
        except (TypeError, ValueError):
            return float(self.s.risk.daily_max_loss_usd)

    def daily_trailing_limit(self) -> float:
        """Effective daily TRAILING stop ($ off the day's peak P&L): dashboard override else config.
        0 = off."""
        v = self.store.get_meta("daily_trailing_override")
        try:
            return float(v) if v not in (None, "") else float(getattr(self.s.risk, "daily_trailing_stop_usd", 0) or 0)
        except (TypeError, ValueError):
            return float(getattr(self.s.risk, "daily_trailing_stop_usd", 0) or 0)

    def _ensure_session(self, account_id: str) -> None:
        """Anchor the day-start equity baseline and clear yesterday's halts."""
        if self.store.get_meta("session_date") == self._today():
            return
        equity = self.client.get_equity(account_id)
        self.store.set_meta("session_date", self._today())
        self.store.set_meta("session_start_ts", str(time.time()))
        self.store.set_meta("bod_equity", str(equity))
        self.store.set_meta("day_peak_pnl", "0")     # reset the daily-trailing high-water mark
        self.store.set_meta("halted", "")
        self.store.set_meta("entries_halted", "")
        log.info("New session %s — baseline equity $%.2f, halts cleared.", self._today(), equity)

    def day_pnl(self, account_id: str) -> float:
        bod = self.store.get_meta("bod_equity")
        if not bod:
            return 0.0
        return self.client.get_equity(account_id) - float(bod)

    # ── exits ─────────────────────────────────────────────────────────────────
    def _sell(self, account_id: str, pos: dict, qty: int, fill: str, reason: str):
        log.info("EXIT %s: sell %sx %s (%s)", reason, qty, pos["option_symbol"], fill)
        return self.engine.close_position(
            account_id=account_id,
            option_symbol=pos["option_symbol"],
            quantity=qty,
            fill=fill,
        )

    def _cancel_working(self, account_id: str, option_symbol: str) -> None:
        """Cancel any still-working (unfilled) order for this option so a new exit can't STACK on
        top of a resting one. TradeStation rejects a sell when open sell orders already cover the
        held quantity ('you are long N with N remaining on sell orders'); stacking those every tick
        is what produced a rejection flood. Cancelling first guarantees one-at-a-time exits."""
        try:
            for o in self.client.get_orders(account_id):
                st = (o.get("StatusDescription") or o.get("Status") or "").lower()
                legs = o.get("Legs") or [{}]
                sym = (legs[0].get("Symbol") if legs else "") or o.get("Symbol", "")
                if o.get("OrderID") and sym == option_symbol and st in (
                        "received", "open", "queued", "sent", "working", ""):
                    try:
                        self.client.cancel_order(o["OrderID"])
                    except TradeStationError:
                        pass
        except TradeStationError:
            pass

    def _close_all(self, account_id: str, pos: dict, reason: str) -> None:
        key = self.store.make_key(pos["symbol"], pos["strategy_id"])
        self._exit_at[key] = time.monotonic()   # start the exit cooldown (don't re-fire every tick)
        # Hybrid: drop the broker stop backstop FIRST so it can't fire after we close.
        if self.bracket is not None and pos.get("bracket_ids"):
            self.bracket.cancel(account_id, pos)
        # Clear any resting sell so this close can't be rejected for "remaining on sell orders".
        self._cancel_working(account_id, pos["option_symbol"])
        # Clamp to what the broker ACTUALLY holds — the tracked qty can drift (partial fills,
        # duplicate entries, out-of-band closes). Selling a phantom qty just gets rejected; and if
        # the broker is already flat there is nothing to close — reconcile the local row instead.
        try:
            held, _ = self.client.held_position(account_id, pos["option_symbol"])
        except TradeStationError:
            held = None                            # can't verify -> proceed with the tracked qty
        if held is not None:
            if held <= 0:
                log.warning("close %s: broker already flat (tracked %s) — reconciling local state.",
                            pos["option_symbol"], pos.get("quantity"))
                self._close_rejected.discard(key)
                self.store.mark_closed(pos["symbol"], pos["strategy_id"])
                try:
                    entry_real, exit_real = real_pnl_prices(self.client, account_id, pos, None)
                    record_exit(self.store, self.s, pos, entry_real, exit_real,
                                reason + " (already flat at broker)", exit_method="reconcile",
                                client=self.client, account_id=account_id)
                except Exception:  # noqa: BLE001
                    log.exception("reconcile-close logging failed (non-fatal)")
                return
            pos = dict(pos, quantity=min(int(pos.get("quantity") or held), int(held)))
        # Protective full exits (stop / break-even / trailing / take-profit / EOD flatten) ALWAYS
        # go out as MARKET — a stop must fill. Never rest a limit here (a per-basket limit exit_fill
        # applies to signal exits + trims, not to a protective stop that could otherwise never fill).
        res = self._sell(account_id, pos, pos["quantity"], "market", reason)
        # If the broker REJECTED the close, do NOT mark closed unless the broker is genuinely
        # flat — marking closed on a rejected sell orphans an UNPROTECTED position (watcher then
        # flags "unmanaged, no stop"). Keep it tracked + protected and retry on the next tick.
        if not getattr(res, "ok", True):
            try:
                held, _ = self.client.held_position(account_id, pos["option_symbol"])
            except TradeStationError:
                held = pos.get("quantity") or 1       # on error, assume still held (safer: don't orphan)
            if held > 0:
                if key not in self._close_rejected:
                    self._close_rejected.add(key)
                    self._notify(f"⚠️ <b>EXIT REJECTED</b> {pos['symbol']} · {pos['option_symbol']}\n"
                                 f"broker rejected the close — still holding {held}. Keeping it tracked "
                                 f"+ protected; retrying. ({reason})")
                self.store.log_event(kind="error", action="exit", symbol=pos["symbol"],
                                     strategy_id=pos["strategy_id"], option_symbol=pos["option_symbol"],
                                     ok=False, detail=f"close rejected, still held {held}: {reason}")
                return
            # broker is flat (e.g. the backstop already filled) -> fall through and reconcile-close
        self._close_rejected.discard(key)
        self.store.mark_closed(pos["symbol"], pos["strategy_id"])
        try:
            entry_real, exit_real = real_pnl_prices(self.client, account_id, pos,
                                                    getattr(res, "order_id", None))
            record_exit(self.store, self.s, pos, entry_real, exit_real, reason,
                        exit_method="monitor (market)", client=self.client, account_id=account_id)
            self.store.log_event(kind="exit", action="exit", symbol=pos["symbol"],
                                 strategy_id=pos["strategy_id"], option_symbol=pos["option_symbol"],
                                 ok=True, detail=reason)
        except Exception:  # noqa: BLE001
            log.exception("risk close logging failed (non-fatal)")
        self._notify(f"🛑 <b>EXIT</b> {pos['symbol']} · {pos['option_symbol']}\n"
                     f"closed {pos['quantity']} — {reason} · Market")

    def halt_and_flatten(self, account_id: str, reason: str) -> None:
        log.warning("KILL-SWITCH: %s — cancelling orders and flattening all positions.", reason)
        self._notify(f"🚨 <b>DAILY LOSS LIMIT HIT</b>\n{reason}\nFlattening all positions and halting for the day.")
        # cancel resting orders first so stops/targets don't re-fire mid-flatten
        try:
            for o in self.client.get_orders(account_id):
                status = (o.get("StatusDescription") or o.get("Status") or "").lower()
                if o.get("OrderID") and status in ("received", "open", "queued", "sent", ""):
                    try:
                        self.client.cancel_order(o["OrderID"])
                    except TradeStationError as e:
                        log.error("cancel failed for %s: %s", o.get("OrderID"), e)
        except TradeStationError as e:
            log.error("could not list orders during flatten: %s", e)
        # market-close every tracked open position. Only mark closed if the sell was ACCEPTED and
        # the broker is actually flat — a rejected flatten that we mark closed would orphan a naked
        # position at the worst possible moment (the exact event the kill-switch exists to protect).
        for pos in self.store.all_open():
            try:
                res = self._sell(account_id, pos, pos["quantity"], "market", "kill-switch flatten")
                # give the fill a moment to REPORT before checking — on live, checking ~100ms after
                # placing flagged an already-filled sell as "not confirmed" (noisy false alarm)
                time.sleep(2.0)
                still_held = 0
                try:
                    still_held, _ = self.client.held_position(account_id, pos["option_symbol"])
                except TradeStationError:
                    still_held = 0 if getattr(res, "ok", True) else (pos.get("quantity") or 1)
                if getattr(res, "ok", True) and still_held <= 0:
                    self.store.mark_closed(pos["symbol"], pos["strategy_id"])
                else:
                    log.error("KILL-SWITCH: flatten of %s not confirmed (ok=%s held=%s) — keeping tracked, will retry.",
                              pos["option_symbol"], getattr(res, "ok", True), still_held)
                    self._notify(f"⚠️ <b>Flatten not confirmed</b> {pos['symbol']} · {pos['option_symbol']}\n"
                                 f"still holding {still_held} — will retry next tick.")
            except TradeStationError as e:
                log.error("flatten failed for %s: %s", pos["option_symbol"], e)
        # Halt for the day (blocks entries + normal management). Any position that FAILED to flatten
        # is retried every tick by the is_halted branch in tick(), so a stuck position is never
        # abandoned even though we're halted.
        self.store.set_meta("halted", self._today())
        try:
            self.store.log_event(kind="halt", action="halt", ok=True, detail=reason)
        except Exception:  # noqa: BLE001
            log.exception("halt log_event failed (non-fatal)")

    def _eod_flatten(self, account_id: str) -> None:
        """At/after the configured flatten time on a trading day, force-close EVERY open position
        (market) so a 0DTE never rides into expiry. Retries each tick until flat; alerts once."""
        try:
            if not flatten_due(self.s):
                return
            open_pos = self.store.all_open()
            if not open_pos:
                return
            if self.store.get_meta("eod_alerted") != self._today():
                self.store.set_meta("eod_alerted", self._today())
                log.warning("EOD flatten: force-closing %d open position(s) before expiry.", len(open_pos))
                self._notify(f"⏰ <b>End-of-day flatten</b>\n"
                             f"closing {len(open_pos)} position(s) at market before 0DTE expiry.")
            for pos in open_pos:
                try:
                    self._close_all(account_id, pos, "end-of-day flatten")
                except Exception:  # noqa: BLE001
                    log.exception("EOD flatten failed for %s", pos.get("option_symbol"))
        except Exception:  # noqa: BLE001 — never let the EOD check crash the tick
            log.exception("eod flatten check failed (non-fatal)")

    # ── per-position management ───────────────────────────────────────────────
    def _manage(self, account_id: str, pos: dict) -> None:
        # exit mode is per-position (stamped from the symbol's basket at entry); fall back to
        # the global config for legacy/un-stamped positions.
        mode = pos.get("exit_mode") or self.s.execution.exit_mode
        # Does a broker-side exit (OCO/backstop) actually rest for this position? If the attach
        # failed or never completed (e.g. the broker API timed out at entry), bracket_ids is empty
        # and the broker holds NO stop. In that case the bot MUST manage the stop itself, whatever
        # the configured mode — otherwise a position with a failed attach rides with no stop of any
        # kind (this is exactly how a client position ran to -43% unprotected).
        has_broker = bool(pos.get("bracket_ids"))
        # Pure bracket WITH a live broker OCO: the broker owns the exit; we only reconcile fills.
        if mode == "bracket" and has_broker and self.bracket is not None:
            self.bracket.reconcile(account_id, pos)
            return
        # Hybrid WITH a live broker backstop: check if it fired (bot was down) — if so we're done.
        # Otherwise fall through and manage trail/trim/break-even, and the bot stop/TP are skipped
        # below ONLY because the broker holds them.
        if mode == "hybrid" and has_broker and self.bracket is not None:
            if self.bracket.reconcile(account_id, pos):
                return
        quote = get_option_quote(self.client, pos["option_symbol"])
        # Thin/sim 0DTE quotes print garbage on EITHER side: the bid showed 0.86 on an option that
        # traded at 1.59 (phantom stop); the last-trade showed 1.5+ on a 0.88 option (phantom take-
        # profit). bid lies sometimes, last lies sometimes — but they rarely lie TOGETHER. So only
        # ACT when bid and last corroborate; if they disagree materially, skip this tick (the broker
        # backstop, on the broker's own data, still protects against a real crash).
        bid = float(quote.get("bid") or 0)
        ask = float(quote.get("ask") or 0)
        last = float(quote.get("last") or 0)
        if bid > 0 and last > 0:
            if abs(bid - last) / max(bid, last) > self.s.execution.max_quote_disagreement:
                return                              # bid vs last disagree -> unreliable garbage tick
            tick_px = bid                           # corroborated -> bid is the realizable sell price
        elif last > 0:
            tick_px = last
        elif bid > 0:
            if ask > 0 and (ask - bid) / ((bid + ask) / 2.0) > self.s.execution.max_spread_ratio:
                return                              # no last + implausibly wide quote -> unreliable
            tick_px = bid
        else:
            return                                  # no usable price
        # Smooth with the MEDIAN of the last N prices so a single bad print can't fire an exit;
        # a sustained move still moves the median.
        key = self.store.make_key(pos["symbol"], pos["strategy_id"])
        buf = self._bids.setdefault(key, [])
        buf.append(tick_px)
        nkeep = max(3, int(self.s.execution.exit_smoothing_ticks or 5))
        if len(buf) > nkeep:
            del buf[: len(buf) - nkeep]
        # Decision price: on LIVE the quote is real (and already bid/last-corroborated above), so use
        # the RAW tick — median smoothing there just lags fast moves: peaks read below the chart's
        # real high ("not reading max") and exits fire ticks after a collapse, filling far below the
        # decision price. Keep the median ONLY on SIM, where single garbage prints are common.
        current = tick_px if self.s.is_live else statistics.median(buf)
        entry = pos.get("entry_price") or 0.0
        if entry <= 0:
            return

        # Running peak (drives trailing/break-even) tracks the smoothed decision price.
        peak = max(pos.get("peak_price") or entry, current)
        # Max-drawdown TROUGH tracks the LAST TRADED price only (median-smoothed) — never the bid.
        # On thin 0DTE the bid prints garbage toward 0, which used to record phantom drawdowns
        # (e.g. −84% DD on a trade that closed −1.8%). LTP is a real execution, so it reflects how
        # far the option actually traded. Ticks with no last-trade don't move the trough.
        trough = pos.get("trough_price") or entry
        if last > 0:
            lbuf = self._lasts.setdefault(key, [])
            lbuf.append(last)
            if len(lbuf) > nkeep:
                del lbuf[: len(lbuf) - nkeep]
            trough = min(trough, statistics.median(lbuf))
        if peak != pos.get("peak_price") or trough != pos.get("trough_price"):
            self.store.update_open(pos["symbol"], pos["strategy_id"], peak_price=peak, trough_price=trough)

        # Exit cooldown: after we place ANY exit order (stop/trim/BE/trailing/TP), give it time to
        # fill before firing another — otherwise a market order that rests unfilled on thin sim 0DTE
        # gets re-placed every poll (3s), stacking sells until the broker rejects them and the watcher
        # floods. Re-attempts (if the position is still open) resume after the cooldown, cancel-first.
        cooldown = max(10.0, float(self.s.execution.poll_interval_sec) * 3)
        if key in self._exit_at and (time.monotonic() - self._exit_at[key]) < cooldown:
            return

        pnl_pct = (current - entry) / entry
        r = effective_risk(self.s, pos["symbol"])   # per-group risk if the symbol is in a group
        # per-signal webhook overrides (tp_pct/sl_pct) take precedence over the group config
        sl_pct = pos.get("sl_pct") if pos.get("sl_pct") is not None else r.stop_loss_pct
        tp_pct = pos.get("tp_pct") if pos.get("tp_pct") is not None else r.take_profit_pct
        # Debounce every bid-threshold trigger: require N consecutive ticks so a single bad
        # quote tick can't fire an irreversible market exit (the false "-46% stop" bug).
        n = self.s.execution.exit_confirm_ticks

        # 1) hard stop loss. In HYBRID the broker holds the stop (evaluated on real fill data), so
        # we DON'T quote-fire it here — that's what produced false "-37% stop" exits on garbage sim
        # quotes. Monitor/bracket modes keep the bot-side stop. CRITICAL: if the broker exit never
        # attached (has_broker is False), the bot ALWAYS runs its own stop, even in hybrid — a
        # position with no broker stop must never be left unprotected.
        if mode != "hybrid" or not has_broker:
            if self._confirm(key, "stop", current <= entry * (1 - sl_pct), n):
                self._close_all(account_id, pos, f"stop {pnl_pct:+.1%}"
                                + ("" if has_broker else " [no broker stop — bot fallback]"))
                return

        # 1b) break-even stop: once up trigger_pct, exit if price falls back to entry.
        # per-signal be_at overrides the group breakeven config.
        be = r.breakeven
        if pos.get("be_at") is not None:
            be_on, be_trig = (float(pos["be_at"]) > 0), float(pos["be_at"])
        else:
            be_on, be_trig = be.enabled, be.trigger_pct
        be_hit = be_on and peak >= entry * (1 + be_trig) and current <= entry
        if self._confirm(key, "breakeven", be_hit, n):
            self._close_all(account_id, pos, f"break-even stop (was +{(peak/entry-1)*100:.0f}%)")
            return

        # 1c) take-profit: full exit at +take_profit_pct (0 = off).
        # In HYBRID mode the broker holds the take-profit LIMIT (evaluated on real fill data), so
        # we DON'T quote-fire it here — that's what produced phantom "+72%" exits on garbage sim
        # quotes. The broker only fills the TP if the price is genuinely there.
        if mode != "hybrid" or not has_broker:
            if self._confirm(key, "takeprofit", bool(tp_pct) and pnl_pct >= tp_pct, n):
                self._close_all(account_id, pos, f"take-profit {pnl_pct:+.1%}")
                return

        # 2) partial trims (one level per tick, in order).
        # a per-signal trim_at + trim_pct override replaces the group trim ladder with one level.
        if pos.get("trim_at") is not None:   # basket/signal-controlled (authoritative; 0 = no trim)
            ta, tpc = float(pos["trim_at"]), float(pos.get("trim_pct") or 0)
            trims = [_Trim(ta, tpc)] if (ta > 0 and tpc > 0) else []
        else:
            trims = sorted(r.trims, key=lambda t: t.at_profit_pct)
        done = pos.get("trims_done") or 0
        if self._confirm(key, "trim", done < len(trims) and pnl_pct >= trims[done].at_profit_pct, n):
            orig = pos.get("original_quantity") or pos["quantity"]
            # A partial trim needs at least 1 whole contract. If the position is too small to take a
            # partial (e.g. 1 contract, trim 50% -> 0.5 rounds to 0), we CANNOT send a 0-lot order
            # (the broker rejects "invalid quantity" and nothing exits). Per the client's request,
            # treat a sub-1 trim as a FULL exit of the (small) position at the trim trigger.
            trim_qty = math.floor(orig * trims[done].close_pct)
            if trim_qty < 1:
                # Too small to take a partial (e.g. 1 contract, trim 50% -> 0.5). If a ride-the-peak
                # exit exists (fixed trail / trailing), SKIP the trim and let it ride protected —
                # full-exiting 1-lot winners at the trim trigger was killing every small trade at
                # +25% (client complaint). Only full-exit here when NO trailing mechanism exists.
                _ftv = pos.get("fix_trail_pct")
                try:
                    _ftv = float(_ftv) if _ftv is not None else 0.0
                except (TypeError, ValueError):
                    _ftv = 0.0
                _tr_on = (bool(pos.get("trail_enabled")) if pos.get("trail_enabled") is not None
                          else r.trailing.enabled)
                if pos.get("onelot_exit"):          # basket says: 1-lot takes the profit at the trim
                    self._close_all(account_id, pos,
                                    f"trim{done + 1} full exit (1-lot, basket setting) @ {pnl_pct:+.1%}")
                    return
                if _ftv > 0 or _tr_on:
                    self.store.update_open(pos["symbol"], pos["strategy_id"], trims_done=done + 1)
                    self.store.log_event(kind="info", action="exit", symbol=pos["symbol"],
                                         strategy_id=pos["strategy_id"], option_symbol=pos["option_symbol"],
                                         ok=True, detail=f"trim{done + 1} skipped (1-lot) — riding with trail protection")
                    return
                self._close_all(account_id, pos,
                                f"trim{done + 1} full exit (position too small to partial) @ {pnl_pct:+.1%}")
                return
            # Clamp to the broker's ACTUAL holding — tracked qty can drift (duplicate entries,
            # partial fills). If the broker is flat, there's nothing to trim: reconcile-close.
            try:
                held, _ = self.client.held_position(account_id, pos["option_symbol"])
            except TradeStationError:
                held = None
            if held is not None:
                if held <= 0:
                    self._close_all(account_id, pos, f"trim{done + 1}: already flat at broker")
                    return
                trim_qty = min(trim_qty, int(held))
                if int(held) < (pos.get("quantity") or 0):   # heal the tracked qty downward
                    self.store.update_open(pos["symbol"], pos["strategy_id"], quantity=int(held))
                    pos = dict(pos, quantity=int(held))
            if trim_qty >= pos["quantity"]:
                self._close_all(account_id, pos, f"trim{done + 1} closes remainder @ {pnl_pct:+.1%}")
                return
            self._exit_at[key] = time.monotonic()                       # trim cooldown (no re-fire every tick)
            self._cancel_working(account_id, pos["option_symbol"])      # never stack on a resting sell
            res = self._sell(account_id, pos, trim_qty,
                             pos.get("exit_fill") or self.s.defaults.exit_fill,
                             f"trim{done + 1} @ {pnl_pct:+.1%}")
            if not getattr(res, "ok", True):
                # trim rejected by the broker — leave the position unchanged (don't reduce the
                # tracked qty, which would drift bot vs broker) and retry on the next tick.
                self.store.log_event(kind="error", action="exit", symbol=pos["symbol"],
                                     strategy_id=pos["strategy_id"], option_symbol=pos["option_symbol"],
                                     ok=False, detail=f"trim{done + 1} rejected @ {pnl_pct:+.1%}")
                return
            # log the partial trim as its own closed-trade row at the REAL fills (§3.18 audit)
            try:
                e_real, x_real = real_pnl_prices(self.client, account_id, pos, getattr(res, "order_id", None))
                trimrec = dict(pos); trimrec["quantity"] = trim_qty
                record_exit(self.store, self.s, trimrec, e_real, x_real,
                            f"trim{done + 1} @ {pnl_pct:+.1%}", exit_method="monitor trim",
                            client=self.client, account_id=account_id, trade_status="partial")
            except Exception:  # noqa: BLE001
                log.exception("trim trade-log failed (non-fatal)")
            self.store.update_open(
                pos["symbol"], pos["strategy_id"],
                quantity=pos["quantity"] - trim_qty, trims_done=done + 1,
            )
            # Hybrid: resize the broker stop backstop down to the remaining quantity, so a
            # later backstop fire can't try to sell more contracts than we still hold.
            if mode == "hybrid" and self.bracket is not None and pos.get("bracket_ids"):
                fresh = self.store.get_open(pos["symbol"], pos["strategy_id"])
                if fresh:
                    self.bracket.resize(account_id, fresh, fresh["quantity"])
            _ot = getattr(res, "order_type", None) or "Market"
            _lp = getattr(res, "limit_price", None)
            self._notify(f"✂️ <b>TRIM</b> {pos['symbol']} · {pos['option_symbol']}\n"
                         f"sold {trim_qty} at {pnl_pct:+.1%} · {pos['quantity'] - trim_qty} left"
                         f" · {_ot}{(' @' + format(_lp, '.2f')) if _lp else ''}")
            return

        # 2b) FIXED trailing stop (per-position, stamped from the basket/signal): trails from ENTRY
        # with no activation threshold, measured in PROFIT POINTS off the peak P&L — the client's
        # intuition: trail 20%, peak +80% -> exit at +60% (peak_pnl - 20 points). At entry
        # (peak P&L = 0) it behaves as a plain -X% stop, then ratchets up point-for-point with the
        # peak. (Price-fraction semantics — peak*(1-X) — fired far below expectation: peak +80%
        # would only exit at +44%.) 0/NULL = off. Same debounce as every other quote exit.
        ft = pos.get("fix_trail_pct")
        try:
            ft = float(ft) if ft is not None else 0.0
        except (TypeError, ValueError):
            ft = 0.0
        if ft > 0:
            peak_pnl = (peak - entry) / entry
            if self._confirm(key, "fixtrail", pnl_pct <= peak_pnl - ft, n):
                self._close_all(account_id, pos,
                                f"fixed trail {ft:.0%} (peak {peak_pnl:+.0%} → exit {pnl_pct:+.1%})")
                return

        # 3) trailing stop (once activated by peak profit). Per-position trailing (stamped from the
        # symbol's basket at entry) overrides the group/global trailing config.
        if pos.get("trail_enabled") is not None:
            tr_en = bool(pos.get("trail_enabled"))
            tr_act = float(pos.get("trail_activate") or 0)
            tr_pct = float(pos.get("trail_pct") or 0)
        else:
            tr_en, tr_act, tr_pct = r.trailing.enabled, r.trailing.activate_at_pct, r.trailing.trail_pct
        if tr_en and tr_pct > 0:
            peak_pnl = (peak - entry) / entry
            trail_hit = (peak_pnl >= tr_act and current <= peak * (1 - tr_pct))
            if self._confirm(key, "trailing", trail_hit, n):
                self._close_all(account_id, pos, f"trailing stop (peak {peak:.2f})")
                return

    # ── one pass ──────────────────────────────────────────────────────────────
    def tick(self) -> None:
        try:
            account_id = self.client.resolve_account_id()
        except TradeStationError as e:
            log.error("risk tick: cannot resolve account (%s)", e)
            return

        self._ensure_session(account_id)

        if self.is_halted():
            # Halted for the day — but keep force-flattening any position that failed to close
            # during the kill-switch flatten (never abandon a stuck naked position).
            for pos in self.store.all_open():
                try:
                    self._close_all(account_id, pos, "post-halt flatten retry")
                except Exception:  # noqa: BLE001
                    log.exception("post-halt flatten retry failed for %s", pos.get("option_symbol"))
            return

        # End-of-day flatten: close every open position before 0DTE expiry (once entries are done).
        self._eod_flatten(account_id)

        # daily-loss kill-switch + daily TRAILING profit-protect (work even with no positions)
        try:
            pnl = self.day_pnl(account_id)
            limit = self.daily_loss_limit()
            if pnl <= -abs(limit):
                self.halt_and_flatten(account_id, f"day PnL ${pnl:.2f} <= -${limit:.0f}")
                return
            # Daily trailing stop: ratchet a high-water mark of the day's P&L; if the P&L falls
            # `trail` dollars from that peak, flatten + halt. Peak starts at 0, so before any profit
            # it behaves as a $trail loss floor; once in profit it locks in the gains (e.g. peak +$300,
            # trail $150 -> halt at +$150). Runs alongside the fixed daily-loss limit (either can fire).
            trail = self.daily_trailing_limit()
            if trail > 0:
                peak = max(float(self.store.get_meta("day_peak_pnl") or 0.0), pnl)
                self.store.set_meta("day_peak_pnl", f"{peak:.2f}")
                if pnl <= peak - abs(trail):
                    self.halt_and_flatten(
                        account_id,
                        f"trailing stop: day PnL ${pnl:.2f} fell ${peak - pnl:.2f} from peak "
                        f"${peak:.2f} (trail ${trail:.0f})")
                    return
        except TradeStationError as e:
            log.error("risk tick: day PnL check failed (%s)", e)

        # §3.12 max-losing-trades — SOFT halt: block NEW entries for the day once today's
        # losers reach the cap (open positions keep being managed, unlike the daily-loss flatten).
        mlt = int(getattr(self.s.risk, "max_losing_trades", 0) or 0)
        if mlt and self.store.get_meta("entries_halted") != self._today():
            since = float(self.store.get_meta("session_start_ts") or 0)
            losers = self.store.losing_trades_today(since, self.s.execution.hide_strategy_prefix or None)
            if losers >= mlt:
                self.store.set_meta("entries_halted", self._today())
                self._notify(f"🛑 <b>Max losing trades hit</b> ({losers} ≥ {mlt})\n"
                             f"Blocking NEW entries for today. Open positions are still managed. Send /resume to override.")
                try:
                    self.store.log_event(kind="halt", action="halt", ok=True,
                                         detail=f"max losing trades {losers}>={mlt} — entries blocked")
                except Exception:  # noqa: BLE001
                    pass

        open_positions = self.store.all_open()
        # prune per-position state for positions that are no longer open
        if self._breach or self._close_rejected or self._bids or self._lasts:
            live = {self.store.make_key(p["symbol"], p["strategy_id"]) for p in open_positions}
            for ck in [k for k in self._breach if k.rsplit(":", 1)[0] not in live]:
                self._breach.pop(ck, None)
            for k in [k for k in self._bids if k not in live]:
                self._bids.pop(k, None)
            for k in [k for k in self._lasts if k not in live]:
                self._lasts.pop(k, None)
            for k in [k for k in self._exit_at if k not in live]:
                self._exit_at.pop(k, None)
            self._close_rejected &= live

        for pos in open_positions:
            try:
                self._manage(account_id, pos)
            except Exception:  # noqa: BLE001 — one position's error must NOT skip managing the rest
                log.exception("manage %s failed", pos.get("option_symbol"))

    # ── background loop ───────────────────────────────────────────────────────
    async def run(self) -> None:
        self._running = True
        interval = max(1, int(self.s.execution.poll_interval_sec))
        log.info("Risk monitor started (every %ss). Daily max loss: $%.0f, stop %.0f%%.",
                 interval, self.s.risk.daily_max_loss_usd, self.s.risk.stop_loss_pct * 100)
        while self._running:
            try:
                await asyncio.to_thread(self.tick)
            except Exception:  # noqa: BLE001 — never let the loop die
                log.exception("risk loop tick error")
            await asyncio.sleep(interval)

    def stop(self) -> None:
        self._running = False
