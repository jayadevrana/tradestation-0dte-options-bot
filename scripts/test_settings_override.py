"""Offline unit tests for the dashboard-editable settings overrides.

Covers coerce_override() validation/normalization, apply_overrides() in-place mutation of a
live Settings object, whitelist enforcement, and store round-trip persistence.

Run: python3 scripts/test_settings_override.py
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import OVERRIDE_SPEC, apply_overrides, coerce_override, load_settings
from app.state import Store

PASS, FAIL = 0, 0


def check(name, cond):
    global PASS, FAIL
    PASS, FAIL = (PASS + 1, FAIL) if cond else (PASS, FAIL + 1)
    print(("  ✓ " if cond else "  ✗ ") + name + ("" if cond else "  <-- FAILED"))


def fresh_store():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return Store(path), path


# ── coercion + validation ─────────────────────────────────────────────────────
print("coerce_override — choices / ranges / normalization")
for path, raw, exp_ok in [
    ("execution.exit_mode", "hybrid", True),
    ("execution.exit_mode", "bracket", True),
    ("execution.exit_mode", "banana", False),
    ("defaults.entry_fill", "mid", True),
    ("defaults.entry_fill", "midpoint", False),
    ("defaults.expiration", "0DTE", True),
    ("defaults.strike_selection", "OTM", True),
    ("defaults.strike_offset", 3, True),
    ("defaults.strike_offset", 99, False),          # out of [0,20]
    ("sizing.max_exposure_pct", 0.0, True),         # 0 = off, allowed
    ("sizing.max_exposure_pct", 0.5, True),
    ("sizing.max_exposure_pct", 1.5, False),        # > 1.0
    ("risk.trailing.enabled", "true", True),
    ("risk.trailing.enabled", "off", True),         # coerces to False
    ("risk.trailing.trail_pct", 0.2, True),
    ("risk.trailing.trail_pct", 0.0, False),        # below 0.01 min
    ("risk.breakeven.trigger_pct", 0.2, True),
    ("risk.max_losing_trades", 0, True),            # 0 = off
    ("risk.max_losing_trades", 5, True),
    ("risk.max_losing_trades", 999, False),
    ("retry.max_attempts", 20, True),
    ("retry.max_attempts", 0, False),               # below min 1
    ("retry.interval_sec", 2.0, True),
    ("schedule.enabled", False, True),
    ("schedule.entry_start", "9:5", True),          # normalizes to 09:05
    ("schedule.entry_start", "25:00", False),
    ("schedule.days", "mon,tue,fri", True),
    ("schedule.days", [], False),                   # empty -> invalid
    ("bogus.path", 1, False),                       # not whitelisted
]:
    ok, _val, _err = coerce_override(path, raw)
    check(f"{path}={raw!r} -> {'ok' if exp_ok else 'reject'}", ok == exp_ok)

print("normalization details")
_, v, _ = coerce_override("schedule.entry_start", "9:5")
check("time zero-pads -> 09:05", v == "09:05")
_, v, _ = coerce_override("schedule.days", "fri, mon , mon")
check("days title-case, dedup, week-order -> [Mon, Fri]", v == ["Mon", "Fri"])
_, v, _ = coerce_override("risk.trailing.enabled", "off")
check("'off' -> False", v is False)
_, v, _ = coerce_override("defaults.strike_offset", "2")
check("numeric string '2' -> int 2", v == 2 and isinstance(v, int))

# ── apply_overrides mutates a live Settings object in place ────────────────────
print("apply_overrides — in-place mutation + whitelist enforcement")
s = load_settings()
applied = apply_overrides(s, {
    "execution.exit_mode": "bracket",
    "defaults.entry_fill": "mid",
    "sizing.max_exposure_pct": 0.33,
    "risk.trailing.enabled": True,
    "risk.breakeven.trigger_pct": 0.25,
    "schedule.entry_start": "09:35",
    "schedule.days": ["Mon", "Wed", "Fri"],
    "bogus.path": 1,                 # ignored
    "execution.exit_mode": "bracket",
})
check("exit_mode applied", s.execution.exit_mode == "bracket")
check("nested defaults applied", s.defaults.entry_fill == "mid")
check("nested sizing applied", s.sizing.max_exposure_pct == 0.33)
check("nested risk.trailing applied", s.risk.trailing.enabled is True)
check("nested risk.breakeven applied", s.risk.breakeven.trigger_pct == 0.25)
check("schedule time applied", s.schedule.entry_start == "09:35")
check("schedule days applied", s.schedule.days == ["Mon", "Wed", "Fri"])
check("bogus path NOT applied", all(p != "bogus.path" for p, _ in applied))

print("bad values are skipped, good ones still apply")
s2 = load_settings()
orig_mode = s2.execution.exit_mode
apply_overrides(s2, {"execution.exit_mode": "banana", "defaults.exit_fill": "bid"})
check("invalid exit_mode left untouched", s2.execution.exit_mode == orig_mode)
check("valid exit_fill still applied", s2.defaults.exit_fill == "bid")

# ── store persistence round-trip + survives a reload ──────────────────────────
print("store round-trip + reload persistence")
store, path = fresh_store()
try:
    check("empty overrides default {}", store.get_settings_overrides() == {})
    ov = {"execution.exit_mode": "monitor", "schedule.entry_end": "15:45"}
    store.set_settings_overrides(ov)
    check("overrides persisted + read back", store.get_settings_overrides() == ov)
    # simulate a /reload: fresh config, then re-apply stored overrides
    fresh = load_settings()
    apply_overrides(fresh, store.get_settings_overrides())
    check("reload re-applies exit_mode", fresh.execution.exit_mode == "monitor")
    check("reload re-applies entry_end", fresh.schedule.entry_end == "15:45")
finally:
    os.remove(path)

print("coverage")
check("all 10 dashboard rows represented (>=18 fields)", len(OVERRIDE_SPEC) >= 18)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
