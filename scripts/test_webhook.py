"""Webhook payload tests — esp. the TradingView STRATEGY one-message-box mapping via
{{strategy.market_position}}. Run: python3 scripts/test_webhook.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.models import WebhookPayload

PASS, FAIL = 0, 0


def check(name, cond):
    global PASS, FAIL
    PASS, FAIL = (PASS + 1, FAIL) if cond else (PASS, FAIL + 1)
    print(("  ✓ " if cond else "  ✗ ") + name + ("" if cond else "  <-- FAILED"))


def mk(**kw):
    return WebhookPayload(secret="x", **kw)


def test_market_position_drives_action():
    print("test_market_position_drives_action (strategy one-box)")
    # entries
    check("long entry  (buy,  mp=long)  -> buy",  mk(action="buy",  market_position="long").normalized_action() == "buy")
    check("short entry (sell, mp=short) -> sell", mk(action="sell", market_position="short").normalized_action() == "sell")
    # exits — THE disambiguation: a close emits the opposite action, but mp=flat means EXIT
    check("close long  (sell, mp=flat)  -> exit", mk(action="sell", market_position="flat").normalized_action() == "exit")
    check("close short (buy,  mp=flat)  -> exit", mk(action="buy",  market_position="flat").normalized_action() == "exit")
    # right (call/put) follows the derived action
    p = mk(action="buy", market_position="long")
    check("long -> CALL", p.right_for(p.normalized_action()) == "call")
    p = mk(action="sell", market_position="short")
    check("short -> PUT", p.right_for(p.normalized_action()) == "put")


def test_fallback_to_action_word():
    print("test_fallback_to_action_word (indicator style / no market_position)")
    check("buy",  mk(action="buy").normalized_action() == "buy")
    check("sell", mk(action="sell").normalized_action() == "sell")
    check("exit", mk(action="exit").normalized_action() == "exit")
    check("synonym long -> buy", mk(action="long").normalized_action() == "buy")
    check("synonym close -> exit", mk(action="close").normalized_action() == "exit")
    # unfilled placeholder (alert pasted without a strategy) must NOT be misread -> falls back to action
    check("unfilled mp placeholder falls back to action",
          mk(action="buy", market_position="{{strategy.market_position}}").normalized_action() == "buy")
    check("nothing usable -> ''", mk().normalized_action() == "")


if __name__ == "__main__":
    test_market_position_drives_action()
    test_fallback_to_action_word()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
