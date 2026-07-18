"""parse_order_response — rejections must NEVER register as accepted (no phantom positions).
Run: python3 scripts/test_orders.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.orders import parse_order_response

PASS, FAIL = 0, 0


def check(name, cond):
    global PASS, FAIL
    PASS, FAIL = (PASS + 1, FAIL) if cond else (PASS, FAIL + 1)
    print(("  ✓ " if cond else "  ✗ ") + name + ("" if cond else "  <-- FAILED"))


def test_accept_vs_reject():
    print("test_parse_order_response")
    ok, oid, _ = parse_order_response({"Orders": [{"OrderID": "1", "StatusDescription": "Received"}]})
    check("clean ack -> accepted", ok and oid == "1")
    ok, _, _ = parse_order_response({"Orders": [{"OrderID": "2", "Error": "X", "Message": "bad"}]})
    check("Error field -> rejected", not ok)
    # the fix: a free-text rejection Message with NO Error field must be caught
    ok, _, _ = parse_order_response({"Orders": [{"OrderID": "3", "Message": "Insufficient buying power"}]})
    check("'Insufficient buying power' -> rejected (no phantom)", not ok)
    ok, _, _ = parse_order_response({"Orders": [{"OrderID": "4", "Message": "Order rejected by exchange"}]})
    check("'rejected' message -> rejected", not ok)
    ok, _, _ = parse_order_response({"Orders": [{"OrderID": "5", "StatusDescription": "Rejected"}]})
    check("Rejected status -> rejected", not ok)
    ok, _, _ = parse_order_response({"Orders": [{"OrderID": "6", "Message": "Margin requirement not met"}]})
    check("'margin' message -> rejected", not ok)
    ok, oid, _ = parse_order_response({})
    check("empty envelope -> not accepted", not ok)
    ok, _, _ = parse_order_response({"Orders": [{"OrderID": "7", "Message": "Order received and queued"}]})
    check("benign 'received' message -> accepted", ok)


if __name__ == "__main__":
    test_accept_vs_reject()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
