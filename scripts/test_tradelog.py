"""Offline unit tests for §3.18 (full trade-log fields) and §3.12 (max losing trades).

Run: python3 scripts/test_tradelog.py
"""
from __future__ import annotations

import datetime as dt
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import load_settings
from app.risk import RiskMonitor
from app.state import Store
from app.tradelog import record_exit

PASS, FAIL = 0, 0


def check(name, cond):
    global PASS, FAIL
    PASS, FAIL = (PASS + 1, FAIL) if cond else (PASS, FAIL + 1)
    print(("  ✓ " if cond else "  ✗ ") + name + ("" if cond else "  <-- FAILED"))


def fresh():
    fd, path = tempfile.mkstemp(suffix=".db"); os.close(fd)
    return Store(path), path


def test_full_field_set_populated():
    print("test_full_field_set_populated (§3.18)")
    s = load_settings(); store, path = fresh()
    # SPY is in Group A (stop 0.15, take-profit 0.20)
    store.upsert_open("SPY", "default", "SPY 260629C737", "call", 2, 2.00, "ORD1",
                      signal_score=0.83, buy_reason="BUY signal → CALL", alert_time=111.0)
    store.update_open("SPY", "default", peak_price=2.40, trough_price=1.50)
    pos = store.get_open("SPY", "default")
    record_exit(store, s, pos, 2.00, 1.50, "stop -25%", exit_method="monitor (market)")
    t = store.recent_trades(1)[0]
    checks = {
        "entry_price=2.0": t["entry_price"] == 2.0,
        "exit_price=1.5": t["exit_price"] == 1.5,
        "pnl=-100": t["pnl"] == -100.0,
        "pnl_pct=-0.25": t["pnl_pct"] == -0.25,
        "net_pnl=-100": t["net_pnl"] == -100.0,
        "position_size_usd=400": t["position_size_usd"] == 400.0,
        "stop_loss_pct=0.15 (Group A)": t["stop_loss_pct"] == 0.15,
        "take_profit_pct=0.20 (Group A)": t["take_profit_pct"] == 0.20,
        "max_profit_pct=0.20": t["max_profit_pct"] == 0.20,
        "max_drawdown_pct=-0.25": t["max_drawdown_pct"] == -0.25,
        "put_call=CALL": t["put_call"] == "CALL",
        "exit_method set": t["exit_method"] == "monitor (market)",
        "trade_status=closed": t["trade_status"] == "closed",
        "signal_score=0.83": t["signal_score"] == 0.83,
        "buy_reason carried": (t["buy_reason"] or "").startswith("BUY"),
        "sell/exit reason set": t["exit_reason"] == "stop -25%",
        "alert_time carried": t["alert_time"] == 111.0,
        "minutes present": t["minutes"] is not None,
    }
    for n, c in checks.items():
        check(n, c)
    os.unlink(path)


def test_commission_net_pnl():
    print("test_commission_net_pnl")
    s = load_settings(); store, path = fresh()
    store.upsert_open("SPY", "t2", "SPY 260629C737", "call", 1, 2.00, "O")
    pos = store.get_open("SPY", "t2")
    record_exit(store, s, pos, 2.00, 2.50, "take-profit", exit_method="broker bracket", commission=1.30)
    t = store.recent_trades(1)[0]
    check("pnl=+50", t["pnl"] == 50.0)
    check("commission=1.30", t["commission"] == 1.30)
    check("net_pnl = 50 - 1.30 = 48.70", t["net_pnl"] == 48.70)
    os.unlink(path)


def test_losing_trades_count_and_halt():
    print("test_losing_trades_count_and_halt (§3.12)")
    s = load_settings(); store, path = fresh()
    # two losers + one winner
    for px, strat in ((1.5, "a"), (1.4, "b")):           # losers (entry 2.0)
        store.upsert_open("SPY", strat, "SPY C", "call", 1, 2.0, "o")
        record_exit(store, s, store.get_open("SPY", strat), 2.0, px, "stop", exit_method="m")
    store.upsert_open("QQQ", "c", "QQQ C", "call", 1, 1.0, "o")
    record_exit(store, s, store.get_open("QQQ", "c"), 1.0, 1.5, "tp", exit_method="m")   # winner
    check("losing_trades_today counts 2 losers", store.losing_trades_today(0) == 2)

    mon = RiskMonitor(s, None, None, store, None, None)
    check("entries_halted False before flag", mon.entries_halted() is False)
    store.set_meta("entries_halted", dt.date.today().isoformat())
    check("entries_halted True after soft-halt flag", mon.entries_halted() is True)
    store.set_meta("entries_halted", "")
    check("entries_halted False after /resume clears it", mon.entries_halted() is False)
    os.unlink(path)


def test_day_pnl_excludes_test_strategies():
    print("test_day_pnl_excludes_test_strategies (dashboard P&L pollution fix)")
    s = load_settings(); store, path = fresh()
    # a real client LOSER (-$100) and an internal self-test LOSER (-$50)
    store.upsert_open("SPY", "default", "SPY C", "call", 1, 2.0, "o")
    record_exit(store, s, store.get_open("SPY", "default"), 2.0, 1.0, "stop", exit_method="m")   # -100
    store.upsert_open("SPY", "loop_selftest", "SPY C", "call", 1, 1.0, "o")
    record_exit(store, s, store.get_open("SPY", "loop_selftest"), 1.0, 0.5, "stop", exit_method="m")  # -50
    check("unfiltered P&L = -100 + -50 = -150", abs(store.trades_today_pnl() - (-150.0)) < 1e-6)
    check("client-only P&L (exclude loop_) = -100", abs(store.trades_today_pnl(exclude_prefix="loop_") - (-100.0)) < 1e-6)
    # §3.12 kill-switch must not count the loop_ test loser
    check("losing_trades_today all = 2", store.losing_trades_today(0) == 2)
    check("losing_trades_today exclude loop_ = 1 (test loser ignored)", store.losing_trades_today(0, "loop_") == 1)
    os.unlink(path)


def test_daily_loss_override():
    print("test_daily_loss_override (dashboard-editable limit)")
    s = load_settings(); store, path = fresh()
    mon = RiskMonitor(s, None, None, store, None, None)
    check("default = config", mon.daily_loss_limit() == float(s.risk.daily_max_loss_usd))
    store.set_meta("daily_max_loss_override", "20000")
    check("override applies (20000)", mon.daily_loss_limit() == 20000.0)
    store.set_meta("daily_max_loss_override", "")
    check("cleared -> back to config", mon.daily_loss_limit() == float(s.risk.daily_max_loss_usd))
    os.unlink(path)


if __name__ == "__main__":
    test_full_field_set_populated()
    test_commission_net_pnl()
    test_losing_trades_count_and_halt()
    test_day_pnl_excludes_test_strategies()
    test_daily_loss_override()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
