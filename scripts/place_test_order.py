"""DEMO: place a BUY then a SELL on the TradeStation SIM account.

This proves the full pipeline end-to-end: auth -> account -> option chain ->
strike selection -> sizing -> place buy -> place sell.

Usage:
    python scripts/place_test_order.py                 # SPY 0DTE ATM call, buy then sell
    python scripts/place_test_order.py --symbol QQQ --side put --dollars 300
    python scripts/place_test_order.py --no-sell       # only the buy
    python scripts/place_test_order.py --dry-run       # resolve+size only, place NOTHING

Safety: refuses to run unless the resolved environment is SIM, to avoid an
accidental live order. Override only if you really mean it with --allow-live.
"""
from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import load_settings
from app.options import get_option_quote
from app.orders import OrderEngine
from app.ts_client import TradeStationClient


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--symbol", default=None, help="underlying (default from config)")
    p.add_argument("--side", default="call", choices=["call", "put"])
    p.add_argument("--expiration", default=None, help="0DTE | NEXT")
    p.add_argument("--fill", default=None, help="market | mid | ask | bid")
    p.add_argument("--dollars", type=float, default=None)
    p.add_argument("--percent", type=float, default=None)
    p.add_argument("--hold", type=float, default=3.0, help="seconds to wait before selling")
    p.add_argument("--no-sell", action="store_true", help="place only the buy")
    p.add_argument("--dry-run", action="store_true", help="resolve + size, place nothing")
    p.add_argument("--allow-live", action="store_true", help="permit running against LIVE")
    args = p.parse_args()

    settings = load_settings()
    if settings.is_live and not args.allow_live:
        sys.exit("Refusing to run against LIVE. Set environment: sim (or pass --allow-live).")

    client = TradeStationClient(settings)
    engine = OrderEngine(settings, client)
    d = settings.defaults

    symbol = (args.symbol or d.underlying).upper()
    print(f"\nEnvironment: {settings.environment.upper()}  Base: {settings.api_base}")

    account_id = client.resolve_account_id()
    equity = client.get_equity(account_id)
    print(f"Account: {account_id}   Equity: ${equity:,.2f}")

    if args.dry_run:
        from app.options import resolve_contract
        from app.orders import compute_contracts

        contract = resolve_contract(
            client, symbol, args.side,
            args.expiration or d.expiration, d.strike_selection, d.strike_offset,
        )
        budget = engine._budget(account_id, args.dollars, args.percent)
        qty = compute_contracts(budget, contract["quote"]["ask"], settings.sizing.max_contracts)
        print(f"\nDRY RUN — would buy {qty}x {contract['option_symbol']}")
        print(f"  underlying={contract['underlying_price']:.2f}  quote={contract['quote']}")
        print(f"  budget=${budget:.0f}  no order placed.")
        return

    # ── BUY ──
    print("\n--- Placing BUY ---")
    buy = engine.open_position(
        account_id=account_id,
        underlying=symbol,
        right=args.side,
        expiration_mode=args.expiration or d.expiration,
        strike_selection=d.strike_selection,
        strike_offset=d.strike_offset,
        fill=args.fill or d.entry_fill,
        dollars=args.dollars,
        percent=args.percent,
    )
    print(f"BUY result: ok={buy.ok}  {buy.message}")
    print(f"  order_id={buy.order_id}  qty={buy.quantity}  type={buy.order_type}  limit={buy.limit_price}")
    if not buy.ok or buy.quantity < 1:
        sys.exit("Buy did not place; stopping.")

    if args.no_sell:
        print("\n--no-sell set; leaving position open.")
        return

    print(f"\nHolding {args.hold}s before selling...")
    time.sleep(args.hold)

    # ── SELL ──
    print("--- Placing SELL ---")
    quote = get_option_quote(client, buy.option_symbol)
    print(f"  current option quote: {quote}")
    sell = engine.close_position(
        account_id=account_id,
        option_symbol=buy.option_symbol,
        quantity=buy.quantity,
        fill=args.fill or d.exit_fill,
    )
    print(f"SELL result: ok={sell.ok}  {sell.message}")
    print(f"  order_id={sell.order_id}  type={sell.order_type}  limit={sell.limit_price}")
    print("\nDone. Check the orders/positions in your TradeStation SIM account.")


if __name__ == "__main__":
    from app.ts_client import TradeStationError

    try:
        main()
    except TradeStationError as e:
        sys.exit(f"\nTradeStation error: {e}")
