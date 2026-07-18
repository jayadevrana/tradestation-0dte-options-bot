"""Offline unit tests for the bot-side exit robustness fix (app/risk.py):
  - quote sanity: an implausibly WIDE bid/ask spread is ignored (the false "-46% stop"
    where a 1.6 fill showed a garbage 0.86 bid but really traded at 1.59).
  - debounce: a bid-threshold trigger must hold N consecutive ticks before it exits, so a
    single transient bad tick can't fire an irreversible market exit.

Run: python3 scripts/test_risk_debounce.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import load_settings
from app.risk import RiskMonitor
from app.state import Store

PASS, FAIL = 0, 0


def check(name, cond):
    global PASS, FAIL
    PASS, FAIL = (PASS + 1, FAIL) if cond else (PASS, FAIL + 1)
    print(("  ✓ " if cond else "  ✗ ") + name + ("" if cond else "  <-- FAILED"))


class FakeClient:
    def __init__(self):
        self.q = {"Bid": 0, "Ask": 0, "Last": 0}
        self.held = 0                    # broker-held qty reported by held_position()
    def set_quote(self, bid, ask, last=None):
        self.q = {"Bid": bid, "Ask": ask, "Last": last if last is not None else ((bid + ask) / 2 if ask else bid)}
    def get_quote(self, symbol):
        return dict(self.q)
    def held_position(self, account_id, symbol):
        return self.held, 1.30
    def order_fill_price(self, account_id, order_id):
        return 1.30                      # any concrete fill so real_pnl_prices returns fast
    def get_orders(self, account_id):
        return []                        # no resting orders to cancel during a flatten
    def resolve_account_id(self):
        return "ACC"
    def get_equity(self, account_id):
        return getattr(self, "equity", 100000.0)


class FakeEngine:
    def __init__(self):
        self.closes = 0
        self.reject = False              # when True, the broker rejects the sell (ok=False)
    def close_position(self, account_id, option_symbol, quantity, fill):
        self.closes += 1
        return SimpleNamespace(ok=not self.reject, order_id=f"EX{self.closes}", option_symbol=option_symbol)


def make(symbol="XYZ", entry=1.6, qty=5, trim_at=None, trim_pct=None, be_at=None):
    """RiskMonitor in monitor mode + a real store holding one open position (no group => global risk)."""
    s = load_settings()
    s.execution.exit_mode = "monitor"      # bypass bracket reconcile in _manage
    s.execution.exit_confirm_ticks = 2
    s.execution.max_spread_ratio = 0.5
    fd, path = tempfile.mkstemp(suffix=".db"); os.close(fd)
    store = Store(path)
    store.upsert_open(symbol, "default", f"{symbol} 250101C100", "call", qty, entry, "ORD1",
                      trim_at=trim_at, trim_pct=trim_pct, be_at=be_at)
    client = FakeClient(); engine = FakeEngine()
    client.held = qty                      # broker holds what the bot tracks (tests can override)
    mon = RiskMonitor(s, client, engine, store, notifier=None, bracket=None)
    return mon, client, engine, store, path, symbol


def test_garbage_bid_uses_last():
    print("test_garbage_low_bid_skipped (the false -46% stop)")
    mon, client, engine, store, path, sym = make(entry=1.6)
    # garbage low bid 0.86 vs last 1.59 -> disagree 46% -> skip the tick, no phantom stop
    client.set_quote(bid=0.86, ask=1.6, last=1.59)
    for _ in range(4):
        mon._manage("ACC", store.get_open(sym, "default"))
    check("no exit — bid 0.86 vs last 1.59 disagree -> skipped", engine.closes == 0)
    check("position still open", store.get_open(sym, "default") is not None)
    os.unlink(path)


def test_garbage_high_last_skipped():
    print("test_garbage_high_last_skipped (the false +72% take-profit)")
    mon, client, engine, store, path, sym = make(symbol="SPY", entry=1.0)
    # last-trade prints garbage high 1.53 but the bid (realizable) is ~0.88 -> disagree -> skip, no TP
    for _ in range(4):
        client.set_quote(bid=0.88, ask=0.92, last=1.53)
        mon._manage("ACC", store.get_open(sym, "default"))
    check("no phantom take-profit — bid 0.88 vs last 1.53 disagree -> skipped", engine.closes == 0)
    check("position still open", store.get_open(sym, "default") is not None)
    os.unlink(path)


def test_debounce_requires_two_ticks():
    print("test_debounce_requires_two_ticks (tight-spread bad tick)")
    mon, client, engine, store, path, sym = make(entry=1.6)
    client.set_quote(bid=1.30, ask=1.32)   # -18.75% (past 15% stop), tight spread passes the gate
    mon._manage("ACC", store.get_open(sym, "default"))
    check("1st breach tick does NOT exit", engine.closes == 0)
    mon._manage("ACC", store.get_open(sym, "default"))
    check("2nd consecutive breach tick DOES exit", engine.closes == 1)
    check("position marked closed", store.get_open(sym, "default") is None)
    os.unlink(path)


def test_transient_tick_resets():
    print("test_transient_tick_resets")
    mon, client, engine, store, path, sym = make(entry=1.6)
    client.set_quote(bid=1.30, ask=1.32); mon._manage("ACC", store.get_open(sym, "default"))  # breach (1)
    client.set_quote(bid=1.55, ask=1.57); mon._manage("ACC", store.get_open(sym, "default"))  # recovers -> reset
    client.set_quote(bid=1.30, ask=1.32); mon._manage("ACC", store.get_open(sym, "default"))  # breach (1 again)
    check("a single tick after recovery does NOT exit", engine.closes == 0)
    check("position still open", store.get_open(sym, "default") is not None)
    os.unlink(path)


def test_sustained_stop_fires():
    print("test_sustained_stop_fires (genuine drop)")
    mon, client, engine, store, path, sym = make(entry=1.6)
    client.set_quote(bid=1.20, ask=1.22)   # -25%, tight spread, real
    mon._manage("ACC", store.get_open(sym, "default"))
    mon._manage("ACC", store.get_open(sym, "default"))
    check("sustained stop exits after 2 ticks", engine.closes == 1)
    os.unlink(path)


def test_no_bid_skips():
    print("test_no_bid_skips")
    mon, client, engine, store, path, sym = make(entry=1.6)
    client.set_quote(bid=0, ask=1.6, last=0)        # no bid, no last -> no usable price
    for _ in range(3):
        mon._manage("ACC", store.get_open(sym, "default"))
    check("no exit when no usable price", engine.closes == 0)
    os.unlink(path)


def test_webhook_sl_override_tighter_stop():
    print("test_webhook_sl_override_tighter_stop (sl_pct=0.10 beats global 0.15)")
    s = load_settings(); s.execution.exit_mode = "monitor"; s.execution.exit_confirm_ticks = 2
    fd, path = tempfile.mkstemp(suffix=".db"); os.close(fd)
    store = Store(path)
    store.upsert_open("XYZ", "default", "XYZ 250101C100", "call", 5, 2.0, "O", sl_pct=0.10)  # override -10%
    client = FakeClient(); engine = FakeEngine()
    client.held = 5                        # broker holds what the bot tracks
    mon = RiskMonitor(s, client, engine, store, notifier=None, bracket=None)
    # -12.5%: PAST the 10% override stop, but NOT past the global 15% -> fires only because of override
    client.set_quote(bid=1.75, ask=1.77, last=1.75)
    for _ in range(6):
        pos = store.get_open("XYZ", "default")
        if not pos:
            break
        mon._manage("ACC", pos)
    check("override -10% stop fires at -12.5% (global -15% would not)", engine.closes == 1)
    os.unlink(path)


def test_webhook_trim_override():
    print("test_webhook_trim_override (per-signal trim 50% @ +30%)")
    mon, client, engine, store, path, sym = make(symbol="XYZ", entry=1.0, qty=4, trim_at=0.30, trim_pct=0.50)
    client.set_quote(bid=1.30, ask=1.31, last=1.30)   # +30% sustained
    for _ in range(3):
        pos = store.get_open(sym, "default")
        if not pos:
            break
        mon._manage("ACC", pos)
    p = store.get_open(sym, "default")
    check("trim sold 50% (4 -> 2 remain, still open)", p is not None and p["quantity"] == 2)
    check("exactly one trim sell fired", engine.closes == 1)
    os.unlink(path)


def test_webhook_be_override_raises_bar():
    print("test_webhook_be_override_raises_bar (be_at=0.25 replaces global 0.20)")
    # global breakeven arms at +20%; a per-signal be_at=0.25 must REPLACE it, so a peak of +22%
    # does NOT arm break-even and a return to entry does NOT exit.
    mon, client, engine, store, path, sym = make(symbol="XYZ", entry=1.0, qty=4, be_at=0.25)
    client.set_quote(bid=1.22, ask=1.23, last=1.22)   # peak +22% (would arm global 20%, not override 25%)
    for _ in range(3):
        mon._manage("ACC", store.get_open(sym, "default"))
    client.set_quote(bid=1.00, ask=1.01, last=1.00)   # back to entry
    for _ in range(3):
        pos = store.get_open(sym, "default")
        if not pos:
            break
        mon._manage("ACC", pos)
    check("override be_at 25% not armed at +22% -> position stays open", store.get_open(sym, "default") is not None)
    os.unlink(path)


def test_basket_trim_off_authoritative():
    print("test_basket_trim_off_authoritative (basket trim_at=0 => no trim despite global +50% trims)")
    # global config has a +50% trim; a basketed position stamps trim_at=0 (OFF) -> must NOT trim.
    mon, client, engine, store, path, sym = make(symbol="XYZ", entry=1.0, qty=4, trim_at=0.0, trim_pct=0.0)
    client.set_quote(bid=1.60, ask=1.61, last=1.60)   # +60% (would hit the global +50% trim)
    for _ in range(3):
        pos = store.get_open(sym, "default")
        if not pos:
            break
        mon._manage("ACC", pos)
    p = store.get_open(sym, "default")
    check("no trim (basket 'off' is authoritative)", engine.closes == 0 and p is not None and p["quantity"] == 4)
    os.unlink(path)


def test_median_ignores_takeprofit_spike():
    print("test_median_ignores_takeprofit_spike (phantom +42% TP)")
    # SPY Group A: take-profit +20%. entry 1.0 → TP would fire if current >= 1.20.
    mon, client, engine, store, path, sym = make(symbol="SPY", entry=1.0)
    # option really trades ~0.98 (last), with a 2-tick high garbage print 1.42/1.40 (even crossing
    # above the ask we paid). buffer last5 = [0.98,0.98,0.98,1.42,1.40] -> median 0.98 -> no TP.
    for _ in range(3):
        client.set_quote(bid=0.97, ask=1.01, last=0.98); mon._manage("ACC", store.get_open(sym, "default"))
    client.set_quote(bid=1.42, ask=1.45, last=1.42); mon._manage("ACC", store.get_open(sym, "default"))
    client.set_quote(bid=1.40, ask=1.43, last=1.40); mon._manage("ACC", store.get_open(sym, "default"))
    check("median ignores the 2-tick spike — NO phantom take-profit", engine.closes == 0)
    check("position still open", store.get_open(sym, "default") is not None)
    os.unlink(path)


def test_median_still_fires_on_sustained_move():
    print("test_median_still_fires_on_sustained_move (real +25%)")
    mon, client, engine, store, path, sym = make(symbol="SPY", entry=1.0)
    # genuine move up that holds: median crosses +20% and stays
    for b in (1.24, 1.25, 1.26, 1.27, 1.28, 1.29):
        pos = store.get_open(sym, "default")
        if not pos:
            break                              # already exited — stop feeding ticks
        client.set_quote(bid=b, ask=b + 0.02); mon._manage("ACC", pos)
    check("sustained +25% DOES fire take-profit", engine.closes == 1)
    check("position closed", store.get_open(sym, "default") is None)
    os.unlink(path)


# ── orphan-on-rejected-close fix ─────────────────────────────────────────────
def test_rejected_close_still_held_keeps_open():
    print("test_rejected_close_still_held_keeps_open (no orphan)")
    mon, client, engine, store, path, sym = make(entry=1.6)
    engine.reject = True                 # broker rejects the sell
    client.held = 5                      # ...and the broker STILL holds the position
    mon._close_all("ACC", store.get_open(sym, "default"), "stop")
    check("position NOT marked closed (stays protected)", store.get_open(sym, "default") is not None)
    check("flagged as close-rejected", mon.store.make_key(sym, "default") in mon._close_rejected)
    check("no phantom trade recorded", len(store.recent_trades(5)) == 0)
    os.unlink(path)


def test_rejected_close_broker_flat_marks_closed():
    print("test_rejected_close_broker_flat_marks_closed (backstop already filled)")
    mon, client, engine, store, path, sym = make(entry=1.6)
    engine.reject = True                 # our sell rejected...
    client.held = 0                      # ...because the broker is already flat (backstop filled)
    mon._close_all("ACC", store.get_open(sym, "default"), "stop")
    check("position marked closed (reconciled)", store.get_open(sym, "default") is None)
    check("trade recorded", len(store.recent_trades(5)) == 1)
    os.unlink(path)


def test_successful_close_marks_closed():
    print("test_successful_close_marks_closed (regression)")
    mon, client, engine, store, path, sym = make(entry=1.6)
    engine.reject = False
    mon._close_all("ACC", store.get_open(sym, "default"), "stop")
    check("position marked closed", store.get_open(sym, "default") is None)
    check("trade recorded", len(store.recent_trades(5)) == 1)
    os.unlink(path)


def test_hybrid_no_bracket_bot_stop_fires():
    print("test_hybrid_without_broker_bracket_bot_stop_fires (the client -43% unprotected incident)")
    mon, client, engine, store, path, sym = make(entry=1.60, qty=5)
    # hybrid mode but the broker exit NEVER attached (bracket_ids empty) — must NOT be left naked
    store.upsert_open(sym, "default", f"{sym} 250101C100", "call", 5, 1.60, "ORD1", exit_mode="hybrid")
    for _ in range(4):                              # sustained -25% (past the 15% stop)
        pos = store.get_open(sym, "default")
        if pos is None:
            break
        client.set_quote(bid=1.20, ask=1.22, last=1.20)
        mon._manage("ACC", pos)
    check("hybrid w/ NO bracket -> bot-side stop FIRED", store.get_open(sym, "default") is None)
    check("a market close was placed", engine.closes >= 1)
    os.remove(path)


def test_hybrid_with_bracket_defers_to_broker():
    print("test_hybrid_with_broker_bracket_skips_bot_stop (broker owns the stop)")
    mon, client, engine, store, path, sym = make(entry=1.60, qty=5)
    store.upsert_open(sym, "default", f"{sym} 250101C100", "call", 5, 1.60, "ORD1", exit_mode="hybrid")
    store.update_open(sym, "default", bracket_ids="9999")   # broker DOES hold a stop
    for _ in range(4):
        client.set_quote(bid=1.20, ask=1.22, last=1.20)
        mon._manage("ACC", store.get_open(sym, "default"))
    check("hybrid w/ bracket -> bot stop SKIPPED (defers to broker)", store.get_open(sym, "default") is not None)
    check("no bot close placed", engine.closes == 0)
    os.remove(path)


def test_exit_cooldown_prevents_flood():
    print("test_exit_cooldown_prevents_rejected_exit_flood (the trim/order stacking flood)")
    mon, client, engine, store, path, sym = make(entry=1.0, qty=4, trim_at=0.30, trim_pct=0.50)
    engine.reject = True                       # every exit order is rejected (rests unfilled / stacks)
    for _ in range(6):                         # 6 ticks all at +40% (trigger the +30% trim)
        p = store.get_open(sym, "default")
        if p is None:
            break
        client.set_quote(bid=1.40, ask=1.42, last=1.40)
        mon._manage("ACC", p)
    # without the cooldown this would re-fire the trim EVERY tick (>=4 stacked sell attempts);
    # with the cooldown it attempts at most once until the window elapses.
    check("rejected exit attempted at most ONCE within cooldown (no flood)", engine.closes <= 1)
    os.remove(path)


def test_trim_below_one_contract_full_exits():
    print("test_trim_below_1_contract_full_exits (client 1-lot trim -> full close, no 0-qty)")
    mon, client, engine, store, path, sym = make(entry=1.0, qty=1, trim_at=0.30, trim_pct=0.50)
    client.held = 1
    for _ in range(3):                                 # sustained +40% -> trim triggers
        p = store.get_open(sym, "default")
        if not p:
            break
        client.set_quote(bid=1.40, ask=1.42, last=1.40)
        mon._manage("ACC", p)
    check("1-lot position fully exits at the trim (no 0-lot order)", store.get_open(sym, "default") is None)
    check("a close was actually placed", engine.closes >= 1)
    os.remove(path)


def test_fixed_trailing_sl():
    print("test_fixed_trailing_sl (trails from ENTRY, ratchets with peak)")
    # ride up: peak 1.50 -> trail line 1.20 (20%); dip to 1.19 must exit
    mon, client, engine, store, path, sym = make(entry=1.0, qty=2)
    store.upsert_open(sym, "default", f"{sym} 250101C100", "call", 2, 1.0, "O1",
                      fix_trail_pct=0.20, trim_at=0.0, trim_pct=0.0, be_at=0.0)  # isolate: only fixed trail
    client.held = 2
    for px in (1.20, 1.35, 1.50, 1.50):                 # rising — no exit
        client.set_quote(bid=px, ask=px+0.02, last=px)
        p = store.get_open(sym, "default")
        mon._manage("ACC", p)
    check("no exit while rising (peak 1.50)", store.get_open(sym, "default") is not None)
    # smoothed (median) peak is ~1.425 -> trail line ~1.14; a sustained dip to 1.10 must exit
    for _ in range(7):
        p = store.get_open(sym, "default")
        if not p:
            break
        client.set_quote(bid=1.10, ask=1.12, last=1.10)
        mon._manage("ACC", p)
    check("exits 20% off the (smoothed) peak", store.get_open(sym, "default") is None)
    check("close placed", engine.closes >= 1)
    os.remove(path)
    # from entry: no rise, straight drop through entry*(1-20%)=0.80 must exit too
    mon, client, engine, store, path, sym = make(entry=1.0, qty=2)
    store.upsert_open(sym, "default", f"{sym} 250101C100", "call", 2, 1.0, "O2", fix_trail_pct=0.20)
    client.held = 2
    for _ in range(3):
        p = store.get_open(sym, "default")
        if not p:
            break
        client.set_quote(bid=0.79, ask=0.81, last=0.79)
        mon._manage("ACC", p)
    check("acts as a plain 20% stop from entry (0.79 <= 0.80)", store.get_open(sym, "default") is None)
    os.remove(path)


def test_onelot_with_trail_rides_not_trimmed():
    print("test_1lot_with_fixed_trail_RIDES (trim skipped, trail manages the winner)")
    mon, client, engine, store, path, sym = make(entry=1.0, qty=1)
    store.upsert_open(sym, "default", f"{sym} 250101C100", "call", 1, 1.0, "O1",
                      fix_trail_pct=0.20, trim_at=0.25, trim_pct=0.50, be_at=0.0)
    client.held = 1
    for px in (1.30, 1.40, 1.60, 1.80):                 # blasts past the +25% trim trigger
        p = store.get_open(sym, "default")
        if not p:
            break
        client.set_quote(bid=px, ask=px+0.02, last=px)
        mon._manage("ACC", p)
    check("1-lot NOT closed at the trim trigger (rides with trail)", store.get_open(sym, "default") is not None)
    for _ in range(8):                                  # now collapse -> trail must catch it
        p = store.get_open(sym, "default")
        if not p:
            break
        client.set_quote(bid=1.15, ask=1.17, last=1.15)
        mon._manage("ACC", p)
    check("fixed trail exits the ride on the pullback", store.get_open(sym, "default") is None)
    os.remove(path)


def test_live_uses_raw_tick_peak():
    print("test_live_mode_uses_RAW_tick (peak reads the real spike, decisions do not lag)")
    mon, client, engine, store, path, sym = make(entry=1.0, qty=2)
    mon.s.environment = "live"                          # live -> raw corroborated tick, no median lag
    store.upsert_open(sym, "default", f"{sym} 250101C100", "call", 2, 1.0, "O1",
                      fix_trail_pct=0.20, trim_at=0.0, trim_pct=0.0, be_at=0.0)
    client.held = 2
    # one real spike tick to 1.50 (bid/last agree -> corroborated), then fall to 1.15
    for px in (1.05, 1.50, 1.18, 1.15):
        p = store.get_open(sym, "default")
        if not p:
            break
        client.set_quote(bid=px, ask=px+0.02, last=px)
        mon._manage("ACC", p)
    pos = store.get_open(sym, "default")
    peak_seen = (pos or {}).get("peak_price") or 0
    check("peak captured the RAW 1.50 spike (median would read ~1.18)",
          pos is None or peak_seen >= 1.49)
    check("trail fired off that real peak (1.15 <= 1.50*0.8)", store.get_open(sym, "default") is None)
    os.remove(path)


def test_onelot_toggle_full_exit():
    print("test_1lot_toggle: basket 'full exit @trim' ON -> 1-lot takes profit at the trigger")
    mon, client, engine, store, path, sym = make(entry=1.0, qty=1)
    store.upsert_open(sym, "default", f"{sym} 250101C100", "call", 1, 1.0, "O1",
                      fix_trail_pct=0.20, trim_at=0.25, trim_pct=0.50, be_at=0.0, onelot_exit=True)
    client.held = 1
    for _ in range(4):
        p = store.get_open(sym, "default")
        if not p:
            break
        client.set_quote(bid=1.30, ask=1.32, last=1.30)
        mon._manage("ACC", p)
    check("1-lot FULLY exits at the +25% trim trigger (toggle ON)", store.get_open(sym, "default") is None)
    os.remove(path)


def test_fixed_trail_profit_points():
    print("test_fixed_trail_PROFIT_POINTS (client spec: peak +80% -> sell at +60%, NOT +44%)")
    mon, client, engine, store, path, sym = make(entry=1.0, qty=2)
    mon.s.environment = "live"                          # raw ticks: peak reads the real high
    store.upsert_open(sym, "default", f"{sym} 250101C100", "call", 2, 1.0, "O1",
                      fix_trail_pct=0.20, trim_at=0.0, trim_pct=0.0, be_at=0.0)
    client.held = 2
    for px in (1.30, 1.60, 1.80):                       # ride to +80%
        client.set_quote(bid=px, ask=px+0.02, last=px)
        mon._manage("ACC", store.get_open(sym, "default"))
    for _ in range(3):                                  # +62%: only 18 points off peak -> HOLD
        p = store.get_open(sym, "default")
        client.set_quote(bid=1.62, ask=1.64, last=1.62)
        mon._manage("ACC", p)
    check("holds at +62% (18 pts off the +80% peak)", store.get_open(sym, "default") is not None)
    for _ in range(3):                                  # +58%: 22 points off peak -> SELL
        p = store.get_open(sym, "default")
        if not p:
            break
        client.set_quote(bid=1.58, ask=1.60, last=1.58)
        mon._manage("ACC", p)
    check("sells at +58% (>=20 pts off the +80% peak)", store.get_open(sym, "default") is None)
    os.remove(path)


def test_daily_trailing_stop():
    print("test_daily_trailing_profit_protect (peak +300, trail 150 -> halt at +150)")
    mon, client, engine, store, path, sym = make(entry=1.6)
    mon.s.schedule.flatten_enabled = False              # keep EOD flatten out of this test
    store.set_meta("daily_max_loss_override", "100000")  # fixed loss floor far away
    store.set_meta("daily_trailing_override", "150")     # trail $150 off the day's peak
    client.held = 0
    client.equity = 100000.0
    mon.tick()                                          # anchors session, bod=100000, peak 0
    check("no halt at day start", not mon.is_halted())
    client.equity = 100300.0                            # day P&L +300
    mon.tick()
    peak = float(store.get_meta("day_peak_pnl") or 0)
    check("peak ratchets to +300", abs(peak - 300) < 1)
    check("no halt at the peak", not mon.is_halted())
    client.equity = 100200.0                            # +200: only -100 from peak
    mon.tick()
    check("no halt at -100 from peak (< 150 trail)", not mon.is_halted())
    client.equity = 100140.0                            # +140: -160 from peak >= 150
    mon.tick()
    check("HALT fires at -150 from peak (+140 <= 300-150)", mon.is_halted())
    os.remove(path)


def test_killswitch_rejected_flatten_no_orphan():
    print("test_killswitch_rejected_flatten_does_not_orphan (kill-switch orphan fix)")
    mon, client, engine, store, path, sym = make(entry=1.60, qty=5)
    engine.reject = True                # broker REJECTS the flatten sell
    client.held = 5                     # ...and still holds the position
    mon.halt_and_flatten("ACC", "test kill-switch")
    check("rejected flatten -> position KEPT tracked (not orphaned naked)",
          store.get_open(sym, "default") is not None)
    os.remove(path)


def test_maxdd_tracks_last_not_bid():
    print("test_max_drawdown_uses_LTP_not_garbage_bid")
    mon, client, engine, store, path, sym = make(entry=1.60, qty=5)
    try:
        # three clean ticks: option last-trades at 1.40 (a real -12.5% dip), bid≈last so the
        # gate passes and no stop fires (stop is 15% -> 1.36, and 1.39 > 1.36).
        for _ in range(3):
            client.set_quote(bid=1.39, ask=1.41, last=1.40)
            mon._manage("ACC", store.get_open(sym, "default"))
        # GARBAGE: bid spikes toward 0 while the option still last-trades ~1.41. bid vs last
        # disagree -> the tick is skipped, so it must NOT drag the drawdown trough down.
        client.set_quote(bid=0.05, ask=1.42, last=1.41)
        mon._manage("ACC", store.get_open(sym, "default"))
        pos = store.get_open(sym, "default")
        trough = pos.get("trough_price")
        check("trough tracks LAST (~1.40), not garbage bid 0.05", abs(trough - 1.40) < 0.05)
        maxdd = (trough - 1.60) / 1.60
        check("max_dd ~ -12.5% (from LTP), not -97% (garbage bid)", -0.14 < maxdd < -0.11)

        # record the trade and confirm the stored max_drawdown_pct reflects LTP, not the bid
        store.record_trade(symbol=sym, strategy_id="default", option_symbol="X", side="call",
                           quantity=5, entry_price=1.60, exit_price=1.41, exit_reason="signal-exit",
                           opened_at=None, peak_price=pos.get("peak_price"), trough_price=trough)
        rec = store.recent_trades(1)[0]
        check("recorded max_drawdown_pct ~ -12.5%", -0.14 < (rec.get("max_drawdown_pct") or 0) < -0.11)
    finally:
        os.remove(path)


if __name__ == "__main__":
    test_garbage_bid_uses_last()
    test_garbage_high_last_skipped()
    test_debounce_requires_two_ticks()
    test_transient_tick_resets()
    test_sustained_stop_fires()
    test_no_bid_skips()
    test_rejected_close_still_held_keeps_open()
    test_rejected_close_broker_flat_marks_closed()
    test_successful_close_marks_closed()
    test_webhook_sl_override_tighter_stop()
    test_webhook_trim_override()
    test_webhook_be_override_raises_bar()
    test_basket_trim_off_authoritative()
    test_median_ignores_takeprofit_spike()
    test_median_still_fires_on_sustained_move()
    test_maxdd_tracks_last_not_bid()
    test_hybrid_no_bracket_bot_stop_fires()
    test_hybrid_with_bracket_defers_to_broker()
    test_killswitch_rejected_flatten_no_orphan()
    test_exit_cooldown_prevents_flood()
    test_trim_below_one_contract_full_exits()
    test_fixed_trailing_sl()
    test_onelot_with_trail_rides_not_trimmed()
    test_live_uses_raw_tick_peak()
    test_fixed_trail_profit_points()
    test_onelot_toggle_full_exit()
    test_daily_trailing_stop()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
