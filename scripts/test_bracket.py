"""Offline unit tests for broker-side exits (app/bracket.py) — bracket + hybrid modes.

No network — a FakeClient simulates TradeStation order/position/fill responses, and a
real in-memory Store (temp SQLite) records state. Run:  python3 scripts/test_bracket.py

AAPL is in Group B (stop 10%, take-profit 30%); backstop_extra_pct = 5%.
Covers:
  bracket mode  : attach OCO [TP @ fill*1.30 + SL @ fill*0.90] off the REAL fill; two leg ids.
  hybrid  mode  : attach SL-only backstop @ fill*(1-(0.10+0.05))=fill*0.85; one leg id.
  attach guard  : entry never fills -> NO SELLTOCLOSE placed (no "long 0 contracts").
  attach partial: protects the quantity actually held (not the requested qty).
  reconcile     : a leg fills -> record trade at real prices, mark closed, cancel sibling(s).
  cancel        : cancels every stored leg id (TradingView exit / orphan cleanup).
  resize (hybrid): cancel + re-place the stop at the remaining qty after a trim.
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.bracket import BracketManager
from app.config import load_settings
from app.state import Store

PASS, FAIL = 0, 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}  <-- FAILED")


class FakeClient:
    """Simulates only what BracketManager touches."""
    def __init__(self):
        self.orders = {}            # order_id -> order dict
        self.positions = {}         # symbol -> {"Quantity","AveragePrice"}
        self.placed_groups = []     # list of (tp_price, sl_price, stop_type, qty)
        self.cancelled = []         # order ids cancelled
        self._next = 1000

    def order_fill_price(self, account_id, order_id):
        o = self.orders.get(str(order_id))
        return float(o["FilledPrice"]) if o and o.get("FilledPrice") else None

    def order_exec_qty(self, account_id, order_id):
        o = self.orders.get(str(order_id))
        if not o:
            return 0
        return sum(int(l.get("ExecQuantity", 0) or 0) for l in (o.get("Legs") or []))

    def held_position(self, account_id, symbol):
        p = self.positions.get(symbol)
        if not p:
            return 0, 0.0
        return abs(int(p["Quantity"])), float(p.get("AveragePrice") or 0.0)

    def place_exit_bracket(self, account_id, option_symbol, quantity, tp_price, sl_price,
                           stop_type="StopMarket", time_in_force="GTC"):
        ids = []
        if tp_price:
            tp_id = str(self._next); self._next += 1
            self.orders[tp_id] = {"OrderID": tp_id, "OrderType": "Limit",
                                  "StatusDescription": "Received", "LimitPrice": tp_price}
            ids.append(tp_id)
        sl_id = str(self._next); self._next += 1
        self.orders[sl_id] = {"OrderID": sl_id, "OrderType": stop_type,
                              "StatusDescription": "Received", "StopPrice": sl_price}
        ids.append(sl_id)
        self.placed_groups.append((tp_price, sl_price, stop_type, quantity))
        return True, ids, "ok"

    def get_order(self, account_id, order_id):
        return self.orders.get(str(order_id))

    def cancel_order(self, order_id):
        self.cancelled.append(str(order_id))
        o = self.orders.get(str(order_id))
        if o:
            o["StatusDescription"] = "Cancelled"
        return {}


def settings_with(mode):
    s = load_settings()
    s.execution.exit_mode = mode
    return s


def fresh_store():
    fd, path = tempfile.mkstemp(suffix=".db"); os.close(fd)
    return Store(path), path


def seed_filled(store, client, *, symbol="AAPL", strat="default", opt="AAPL 250101C150",
                side="call", req_qty=2, held_qty=2, exec_qty=None, real_fill=2.00, order_id="500",
                tp_pct=None, sl_pct=None):
    exec_qty = held_qty if exec_qty is None else exec_qty
    store.upsert_open(symbol, strat, opt, side, req_qty, 1.90, order_id, tp_pct=tp_pct, sl_pct=sl_pct)
    client.orders[order_id] = {"OrderID": order_id, "StatusDescription": "Received",
                               "FilledPrice": real_fill, "Legs": [{"ExecQuantity": exec_qty}]}
    client.positions[opt] = {"Quantity": held_qty, "AveragePrice": real_fill}
    return symbol, strat, opt


# ── bracket mode ───────────────────────────────────────────────────────────────
def test_bracket_mode_places_oco():
    print("test_bracket_mode_places_oco (exit_mode=bracket)")
    s = settings_with("bracket"); store, path = fresh_store(); c = FakeClient()
    sym, strat, opt = seed_filled(store, c, req_qty=2, held_qty=2, real_fill=2.00, order_id="500")
    BracketManager(s, c, store, None)._attach("ACC", sym, strat, opt, 2)
    tp, sl, _, qty = c.placed_groups[-1]
    check("OCO placed", len(c.placed_groups) == 1)
    check("TP = 2.00*1.30 = 2.60", abs(tp - 2.60) < 1e-6)
    check("SL = 2.00*0.90 = 1.80", abs(sl - 1.80) < 1e-6)
    check("qty = 2", qty == 2)
    pos = store.get_open(sym, strat)
    check("two leg ids stored", len((pos.get("bracket_ids") or "").split(",")) == 2)
    check("entry_price set to real fill 2.00", abs((pos.get("entry_price") or 0) - 2.00) < 1e-6)
    os.unlink(path)


# ── hybrid mode ────────────────────────────────────────────────────────────────
def test_hybrid_places_tp_and_backstop():
    print("test_hybrid_places_tp_and_backstop (hybrid, grouped → broker TP + SL backstop)")
    s = settings_with("hybrid"); store, path = fresh_store(); c = FakeClient()
    # AAPL Group B: stop 10%, take-profit 30%, backstop_extra 5%
    sym, strat, opt = seed_filled(store, c, req_qty=2, held_qty=2, real_fill=2.00, order_id="510")
    BracketManager(s, c, store, None)._attach("ACC", sym, strat, opt, 2)
    tp, sl, _, qty = c.placed_groups[-1]
    check("OCO placed", len(c.placed_groups) == 1)
    check("broker TP = 2.00*1.30 = 2.60 (real-data take-profit)", abs(tp - 2.60) < 1e-6)
    check("SL = 2.00*(1-0.10) = 1.80 (broker stop at the stop level)", abs(sl - 1.80) < 1e-6)
    pos = store.get_open(sym, strat)
    check("two leg ids stored (TP+SL)", len((pos.get("bracket_ids") or "").split(",")) == 2)
    os.unlink(path)


def test_hybrid_webhook_tp_sl_override():
    print("test_hybrid_webhook_tp_sl_override (per-signal tp_pct/sl_pct beats group config)")
    s = settings_with("hybrid"); store, path = fresh_store(); c = FakeClient()
    # AAPL Group B config = tp30/stop10, but the SIGNAL overrides to tp50 / sl25
    sym, strat, opt = seed_filled(store, c, real_fill=2.00, order_id="560", tp_pct=0.50, sl_pct=0.25)
    BracketManager(s, c, store, None)._attach("ACC", sym, strat, opt, 2)
    tp, sl, _, _ = c.placed_groups[-1]
    check("TP uses override = 2.00*1.50 = 3.00 (not group 30%)", abs(tp - 3.00) < 1e-6)
    check("SL uses override = 2.00*(1-0.25) = 1.50", abs(sl - 1.50) < 1e-6)
    os.unlink(path)


def test_hybrid_no_tp_backstop_only():
    print("test_hybrid_no_tp_backstop_only (ungrouped, take-profit=0 → SL backstop only)")
    s = settings_with("hybrid"); store, path = fresh_store(); c = FakeClient()
    sym, strat, opt = seed_filled(store, c, symbol="ZZZ", req_qty=2, held_qty=2, real_fill=2.00, order_id="515")
    BracketManager(s, c, store, None)._attach("ACC", sym, strat, opt, 2)
    tp, sl, _, qty = c.placed_groups[-1]
    check("NO TP leg (global take-profit=0)", tp is None)
    check("SL = 2.00*(1-0.15) = 1.70", abs(sl - 1.70) < 1e-6)
    os.unlink(path)


def test_attach_skips_when_not_filled():
    print("test_attach_skips_when_not_filled")
    s = settings_with("hybrid"); store, path = fresh_store(); c = FakeClient()
    store.upsert_open("AAPL", "default", "AAPL 250101C150", "call", 2, 1.90, "511")
    c.orders["511"] = {"OrderID": "511", "StatusDescription": "Received"}   # no fill, no position
    import app.bracket as bm
    orig = bm.time.sleep; bm.time.sleep = lambda *_: None   # don't actually wait 30s
    try:
        BracketManager(s, c, store, None)._attach("ACC", "AAPL", "default", "AAPL 250101C150", 2)
    finally:
        bm.time.sleep = orig
    check("NO SELLTOCLOSE placed on 0 contracts", len(c.placed_groups) == 0)
    check("no bracket_ids stored", not (store.get_open("AAPL", "default").get("bracket_ids")))
    os.unlink(path)


def test_attach_partial_protects_held_qty():
    print("test_attach_partial_protects_held_qty (entry order FINAL with a partial fill)")
    s = settings_with("hybrid"); store, path = fresh_store(); c = FakeClient()
    sym, strat, opt = seed_filled(store, c, req_qty=5, held_qty=3, real_fill=2.00, order_id="512")
    c.orders["512"]["StatusDescription"] = "Filled"   # terminal: 3 is all we will ever get
    BracketManager(s, c, store, None)._attach("ACC", sym, strat, opt, 5)
    _, _, _, qty = c.placed_groups[-1]
    check("protects held 3 (not requested 5)", qty == 3)
    os.unlink(path)


def test_attach_waits_for_full_fill():
    print("test_attach_waits_for_full_fill (live partial mid-fill — the AAPL 6/3 incident)")
    # A live 6-lot market buy reports 3 held mid-fill (order still working). Attaching at that
    # instant protected only 3 and left 3 naked. The attach must WAIT for the full fill.
    s = settings_with("hybrid"); store, path = fresh_store(); c = FakeClient()
    sym, strat, opt = seed_filled(store, c, req_qty=6, held_qty=6, exec_qty=3, real_fill=0.47, order_id="600")
    c.orders["600"]["StatusDescription"] = "Received"   # entry order NOT terminal yet
    calls = {"n": 0}
    orig = c.order_exec_qty
    def eq(acct, oid):
        calls["n"] += 1
        if calls["n"] >= 3:                             # rest of the fill arrives on the 3rd poll
            c.orders["600"]["Legs"][0]["ExecQuantity"] = 6
        return orig(acct, oid)
    c.order_exec_qty = eq
    BracketManager(s, c, store, None)._attach("ACC", sym, strat, opt, 6)
    _, _, _, qty = c.placed_groups[-1]
    check("stop protects the FULL 6 (waited out the partial)", qty == 6)
    os.unlink(path)


def test_attach_commingled_protects_strategy_share():
    print("test_attach_commingled_protects_strategy_share (§3.14 same contract, two strategies)")
    s = settings_with("hybrid"); store, path = fresh_store(); c = FakeClient()
    # this strategy ordered 4, but the broker shows 8 held (another strategy holds the SAME contract)
    sym, strat, opt = seed_filled(store, c, req_qty=4, held_qty=8, exec_qty=4, real_fill=2.00, order_id="540")
    BracketManager(s, c, store, None)._attach("ACC", sym, strat, opt, 4)
    _, _, _, qty = c.placed_groups[-1]
    check("protects THIS strategy's 4 (not the commingled 8)", qty == 4)
    os.unlink(path)


def test_reconcile_tp_fills_cancels_sibling():
    print("test_reconcile_tp_fills_cancels_sibling (bracket)")
    s = settings_with("bracket"); store, path = fresh_store(); c = FakeClient()
    sym, strat, opt = seed_filled(store, c, req_qty=2, held_qty=2, real_fill=2.00, order_id="520")
    b = BracketManager(s, c, store, None); b._attach("ACC", sym, strat, opt, 2)
    tp_id, sl_id = store.get_open(sym, strat)["bracket_ids"].split(",")
    c.orders[tp_id]["StatusDescription"] = "Filled"; c.orders[tp_id]["FilledPrice"] = 2.60
    did = b.reconcile("ACC", store.get_open(sym, strat))
    check("reconcile True", did is True)
    check("sibling SL cancelled", sl_id in c.cancelled)
    check("position closed", store.get_open(sym, strat) is None)
    tr = store.recent_trades(5)[0]
    check("entry 2.00 / exit 2.60", abs(tr["entry_price"]-2.00) < 1e-6 and abs(tr["exit_price"]-2.60) < 1e-6)
    check("P&L = (2.60-2.00)*2*100 = +120", abs((tr["pnl"] or 0) - 120.0) < 1e-6)
    check("reason = take-profit", "take-profit" in (tr["exit_reason"] or ""))
    os.unlink(path)


def test_hybrid_reconcile_backstop_fired():
    print("test_hybrid_reconcile_backstop_fired")
    s = settings_with("hybrid"); store, path = fresh_store(); c = FakeClient()
    sym, strat, opt = seed_filled(store, c, req_qty=2, held_qty=2, real_fill=2.00, order_id="521")
    b = BracketManager(s, c, store, None); b._attach("ACC", sym, strat, opt, 2)
    ids = store.get_open(sym, strat)["bracket_ids"].split(",")
    sl_id = [i for i in ids if c.orders[i]["OrderType"] != "Limit"][0]   # the stop leg (not the TP limit)
    c.orders[sl_id]["StatusDescription"] = "Filled"; c.orders[sl_id]["FilledPrice"] = 1.70
    did = b.reconcile("ACC", store.get_open(sym, strat))
    check("reconcile True (backstop fired)", did is True)
    check("position closed", store.get_open(sym, strat) is None)
    tr = store.recent_trades(5)[0]
    check("reason = stop-loss (broker backstop)", "backstop" in (tr["exit_reason"] or ""))
    check("exit = 1.70", abs((tr["exit_price"] or 0) - 1.70) < 1e-6)
    os.unlink(path)


def test_reconcile_noop_when_resting():
    print("test_reconcile_noop_when_resting")
    s = settings_with("hybrid"); store, path = fresh_store(); c = FakeClient()
    sym, strat, opt = seed_filled(store, c, order_id="522")
    b = BracketManager(s, c, store, None); b._attach("ACC", sym, strat, opt, 2)
    check("reconcile False (still resting)", b.reconcile("ACC", store.get_open(sym, strat)) is False)
    check("position still open", store.get_open(sym, strat) is not None)
    os.unlink(path)


def test_cancel_all_legs():
    print("test_cancel_all_legs (bracket)")
    s = settings_with("bracket"); store, path = fresh_store(); c = FakeClient()
    sym, strat, opt = seed_filled(store, c, order_id="523")
    b = BracketManager(s, c, store, None); b._attach("ACC", sym, strat, opt, 2)
    pos = store.get_open(sym, strat); ids = pos["bracket_ids"].split(",")
    b.cancel("ACC", pos)
    check("both legs cancelled on TradingView exit", all(i in c.cancelled for i in ids))
    os.unlink(path)


def test_hybrid_resize_after_trim():
    print("test_hybrid_resize_after_trim")
    s = settings_with("hybrid"); store, path = fresh_store(); c = FakeClient()
    sym, strat, opt = seed_filled(store, c, req_qty=4, held_qty=4, real_fill=2.00, order_id="524")
    b = BracketManager(s, c, store, None); b._attach("ACC", sym, strat, opt, 4)
    old_ids = store.get_open(sym, strat)["bracket_ids"].split(",")
    # bot trimmed 2 -> 2 remain; risk.py would update qty then call resize
    store.update_open(sym, strat, quantity=2)
    b.resize("ACC", store.get_open(sym, strat), 2)
    check("old leg(s) cancelled", all(i in c.cancelled for i in old_ids))
    _, sl, _, qty = c.placed_groups[-1]
    check("new stop qty = 2 (remaining)", qty == 2)
    check("stop level unchanged 1.80", abs(sl - 1.80) < 1e-6)
    check("bracket_ids re-pointed to new leg", store.get_open(sym, strat)["bracket_ids"] != ",".join(old_ids))
    os.unlink(path)


if __name__ == "__main__":
    test_bracket_mode_places_oco()
    test_hybrid_places_tp_and_backstop()
    test_hybrid_webhook_tp_sl_override()
    test_hybrid_no_tp_backstop_only()
    test_attach_skips_when_not_filled()
    test_attach_partial_protects_held_qty()
    test_attach_waits_for_full_fill()
    test_attach_commingled_protects_strategy_share()
    test_reconcile_tp_fills_cancels_sibling()
    test_hybrid_reconcile_backstop_fired()
    test_reconcile_noop_when_resting()
    test_cancel_all_legs()
    test_hybrid_resize_after_trim()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
