"""Offline unit tests for per-basket strategy config.

Covers basket_settings() resolution (per-basket value vs global fallback), the new position
columns round-trip (exit_mode / exit_fill / trailing stamped and read back), and the
per-basket losing-trades filter.

Run: python3 scripts/test_baskets_config.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import load_settings
from app.groups import basket_for, basket_settings
from app.state import Store

PASS, FAIL = 0, 0


def check(name, cond):
    global PASS, FAIL
    PASS, FAIL = (PASS + 1, FAIL) if cond else (PASS, FAIL + 1)
    print(("  ✓ " if cond else "  ✗ ") + name + ("" if cond else "  <-- FAILED"))


def fresh():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return Store(path), path


s = load_settings()

# ── basket_settings: two baskets resolve to different, independent configs ─────
print("basket_settings — independent per-basket resolution")
b1 = {"name": "B1", "symbols": ["SPY"], "stop_loss_pct": 0.20,
      "exit_mode": "bracket", "entry_fill": "mid", "exit_fill": "ask",
      "expiration": "NEXT", "strike_selection": "OTM", "strike_offset": 2,
      "max_exposure_pct": 0.40, "trailing_enabled": True, "trail_activate_pct": 0.5, "trail_pct": 0.10,
      "max_losing_trades": 2, "retry_max_attempts": 8, "retry_interval_sec": 1.0}
b2 = {"name": "B2", "symbols": ["QQQ"], "stop_loss_pct": 0.15, "trailing_enabled": False}
r1 = basket_settings(s, b1)
r2 = basket_settings(s, b2)
check("b1 exit_mode=bracket", r1.exit_mode == "bracket")
check("b1 attach_broker_exit True (bracket)", r1.attach_broker_exit is True)
check("b1 entry/exit fill", r1.entry_fill == "mid" and r1.exit_fill == "ask")
check("b1 expiration/strike/offset", r1.expiration == "NEXT" and r1.strike_selection == "OTM" and r1.strike_offset == 2)
check("b1 max_exposure 0.40", r1.max_exposure_pct == 0.40)
check("b1 trailing on 0.5/0.10", r1.trailing_enabled and r1.trail_activate_pct == 0.5 and r1.trail_pct == 0.10)
check("b1 max_losing 2, retry 8/1.0", r1.max_losing_trades == 2 and r1.retry_max_attempts == 8 and r1.retry_interval_sec == 1.0)
check("b2 differs: falls back to global exit_mode", r2.exit_mode == s.execution.exit_mode)
check("b2 entry_fill falls back to global default", r2.entry_fill == s.defaults.entry_fill)
check("b2 trailing off", r2.trailing_enabled is False)
check("b2 max_losing default 0", r2.max_losing_trades == 0)

print("monitor mode -> no broker exit attached")
rm = basket_settings(s, {"name": "M", "symbols": ["IWM"], "exit_mode": "monitor"})
check("monitor attach_broker_exit False", rm.attach_broker_exit is False)

print("bad values fall back safely")
rb = basket_settings(s, {"name": "X", "symbols": ["X"], "exit_mode": "banana", "strike_offset": "abc"})
check("invalid exit_mode -> global", rb.exit_mode == s.execution.exit_mode)
check("invalid strike_offset -> global default", rb.strike_offset == s.defaults.strike_offset)

# ── position columns round-trip ───────────────────────────────────────────────
print("position columns — exit_mode / exit_fill / trailing stamped + read back")
store, path = fresh()
try:
    store.upsert_open("QQQ", "default", "QQQ 250101C500", "call", 3, 4.00, "ord1",
                      tp_pct=0.0, sl_pct=0.20, trim_pct=0.5, trim_at=0.3, be_at=0.3,
                      exit_mode="bracket", exit_fill="mid",
                      trail_enabled=True, trail_activate=0.4, trail_pct=0.15)
    pos = store.get_open("QQQ", "default")
    check("exit_mode stored", pos.get("exit_mode") == "bracket")
    check("exit_fill stored", pos.get("exit_fill") == "mid")
    check("trail_enabled stored as 1", pos.get("trail_enabled") == 1)
    check("trail_activate stored", pos.get("trail_activate") == 0.4)
    check("trail_pct stored", pos.get("trail_pct") == 0.15)
    check("sl_pct still stored", pos.get("sl_pct") == 0.20)

    # a legacy-style upsert (no new fields) leaves them NULL -> risk falls back to global
    store.upsert_open("SPY", "default", "SPY 250101C600", "call", 1, 5.00, "ord2", sl_pct=0.15)
    p2 = store.get_open("SPY", "default")
    check("legacy exit_mode NULL", p2.get("exit_mode") is None)
    check("legacy trail_enabled NULL", p2.get("trail_enabled") is None)

    # a fresh open must NOT inherit a prior position's broker bracket_ids (stale-bracket bug)
    store.update_open("QQQ", "default", bracket_ids="960870429")
    check("bracket_ids set", store.get_open("QQQ", "default").get("bracket_ids") == "960870429")
    store.upsert_open("QQQ", "default", "QQQ 250101C510", "call", 2, 4.50, "ord3", sl_pct=0.20,
                      exit_mode="monitor")
    check("re-open clears stale bracket_ids", not store.get_open("QQQ", "default").get("bracket_ids"))

    # ── per-basket losing-trades filter ──
    print("losing_trades_today_for — scoped to basket symbols")
    now = time.time()
    for sym, pnl in [("SPY", -10), ("SPY", -5), ("QQQ", -8), ("IWM", -3), ("SPY", 12)]:
        store.record_trade(symbol=sym, strategy_id="s", option_symbol=f"{sym}X", side="call",
                           quantity=1, entry_price=1.0, exit_price=1.0 + pnl / 100.0,
                           exit_reason="test", opened_at=now)
    n_spy = store.losing_trades_today_for(now - 3600, ["SPY"])
    n_multi = store.losing_trades_today_for(now - 3600, ["SPY", "QQQ"])
    n_none = store.losing_trades_today_for(now - 3600, [])
    check("SPY basket losers = 2", n_spy == 2)
    check("SPY+QQQ basket losers = 3", n_multi == 3)
    check("empty symbol list = 0", n_none == 0)
    check("excludes a loop_ loser", store.losing_trades_today_for(
        now - 3600, ["SPY"], exclude_prefix="loop_") == 2)
finally:
    os.remove(path)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
