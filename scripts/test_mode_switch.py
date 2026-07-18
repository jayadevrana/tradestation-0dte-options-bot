"""Offline unit tests for the SIM/LIVE trading-mode switch safety rails.

Covers: TS_ENVIRONMENT precedence (env var can never force live), guard_environment()
coherence (mismatches always fail toward sim), and the /api/set-mode endpoint guards
(flat required, confirm=LIVE required, unconfigured live account refused).

Run: python3 scripts/test_mode_switch.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import guard_environment, load_settings

PASS, FAIL = 0, 0


def check(name, cond):
    global PASS, FAIL
    PASS, FAIL = (PASS + 1, FAIL) if cond else (PASS, FAIL + 1)
    print(("  ✓ " if cond else "  ✗ ") + name + ("" if cond else "  <-- FAILED"))


print("TS_ENVIRONMENT precedence — env var can NEVER force live")
os.environ["TS_ENVIRONMENT"] = "live"
s = load_settings()
check("TS_ENVIRONMENT=live ignored (config says sim -> stays sim)", s.environment == "sim")
os.environ["TS_ENVIRONMENT"] = "sim"
s = load_settings()
check("TS_ENVIRONMENT=sim forces sim", s.environment == "sim")
os.environ.pop("TS_ENVIRONMENT", None)

print("guard_environment — mismatches fail toward SIM")
s = load_settings()
s.environment = "live"; s.broker.account_id = "SIM3287667M"
w = guard_environment(s, "SIM3287667M")
check("live + SIM account -> forced back to sim", s.environment == "sim" and len(w) == 1)
s = load_settings()
s.environment = "sim"; s.broker.account_id = "12058853"
w = guard_environment(s, "SIM3287667M")
check("sim + REAL account -> account restored to sim account", s.broker.account_id == "SIM3287667M")
s = load_settings()
s.environment = "live"; s.broker.account_id = "12058853"
w = guard_environment(s, "SIM3287667M")
check("live + real account -> untouched (coherent)", s.environment == "live"
      and s.broker.account_id == "12058853" and not w)
s = load_settings()
w = guard_environment(s, s.broker.account_id)
check("sim + SIM account -> untouched (coherent)", not w)

print("endpoint guards — /api/set-mode")
os.environ["DASHBOARD_TOKEN"] = "testtok"
from fastapi.testclient import TestClient
import app.main as m
c = TestClient(m.app)
T = "?token=testtok"
r = c.post(f"/api/set-mode{T}&mode=banana").json()
check("bad mode rejected", r.get("ok") is False)
r = c.post(f"/api/set-mode{T}&mode=live").json()
check("live without confirm=LIVE refused", r.get("ok") is False and "confirm" in (r.get("error") or ""))
m.store.set_meta("live_account_id", "")
r = c.post(f"/api/set-mode{T}&mode=live&confirm=LIVE").json()
check("live without a configured live account refused", r.get("ok") is False and "live account" in (r.get("error") or "").lower())
m.store.set_meta("live_account_id", "SIM9999999M")
r = c.post(f"/api/set-mode{T}&mode=live&confirm=LIVE").json()
check("SIM-prefixed live_account_id refused", r.get("ok") is False)
# flat requirement
m.store.set_meta("live_account_id", "12058853")
m.store.upsert_open("SPY", "default", "SPY 250101C600", "call", 1, 5.0, "O1")
r = c.post(f"/api/set-mode{T}&mode=live&confirm=LIVE")
check("switch refused while positions open (409)", r.status_code == 409)
m.store.mark_closed("SPY", "default")
# sim -> sim is always allowed and keeps the sim account
r = c.post(f"/api/set-mode{T}&mode=sim").json()
check("switch to sim ok", r.get("ok") is True and r.get("environment") == "sim")
check("sim keeps the yaml SIM account", (r.get("account") or "").startswith("SIM"))
# mode GET reflects state
g = c.get(f"/api/mode{T}").json()
check("GET /api/mode shows sim + live acct", g.get("mode") == "sim" and g.get("live_account") == "12058853")
# NOTE: the live-flip happy path needs a real broker (account visibility check) — verified on-server.
m.store.set_meta("live_account_id", "")
m.store.set_meta("trading_mode", "sim")

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
