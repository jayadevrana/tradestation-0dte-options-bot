"""Options selection logic: pick expiration (0DTE/NEXT), pick strike (ATM +/- offset),
build the TradeStation option symbol, and read its bid/ask/mid quote.

TradeStation option symbol format:  "<ROOT> <YYMMDD><C|P><STRIKE>"
    e.g.  SPY 250620C580   ->  SPY, 20-Jun-2025, 580 Call
          SPY 250620P578.5 ->  SPY, 20-Jun-2025, 578.5 Put

NOTE: SPX 0DTE must use the SPXW root (PM-settled weeklies/dailies), not SPX.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Optional

from .ts_client import TradeStationClient, TradeStationError

log = logging.getLogger("options")


def normalize_underlying(symbol: str) -> str:
    """SPX 0DTE/dailies live on the SPXW root (PM-settled). Plain SPX is AM-settled
    monthlies only, so an automated daily/0DTE bot must use SPXW. Auto-correct it."""
    s = symbol.upper().strip()
    if s == "SPX":
        log.warning("Symbol SPX auto-mapped to SPXW (SPX has no daily/0DTE; SPXW does).")
        return "SPXW"
    return s


def _parse_exp_date(raw: str) -> dt.date:
    # API returns e.g. "2025-06-20T00:00:00Z"
    return dt.datetime.fromisoformat(raw.replace("Z", "+00:00")).date()


def pick_expiration(client: TradeStationClient, underlying: str, mode: str) -> dt.date:
    """mode: '0DTE' (nearest, == today if it exists) or 'NEXT' (the one after the nearest)."""
    today = dt.date.today()
    exps = client.get_option_expirations(underlying)
    dates = sorted({_parse_exp_date(e["Date"]) for e in exps if "Date" in e})
    future = [d for d in dates if d >= today]
    if not future:
        raise TradeStationError(f"No current/future expirations for {underlying}.")

    if mode.upper() == "NEXT":
        # the expiration AFTER the nearest one
        return future[1] if len(future) > 1 else future[0]
    # 0DTE: nearest expiration (today if listed). Warn if it isn't actually today.
    if future[0] != today:
        log.warning("No 0DTE expiration for %s today; using nearest %s.", underlying, future[0])
    return future[0]


def _fmt_strike(strike: float) -> str:
    s = ("%f" % float(strike)).rstrip("0").rstrip(".")
    return s if s else "0"


def build_option_symbol(root: str, expiration: dt.date, right: str, strike: float) -> str:
    cp = "C" if right.lower().startswith("c") else "P"
    yymmdd = expiration.strftime("%y%m%d")
    return f"{root} {yymmdd}{cp}{_fmt_strike(strike)}"


def get_underlying_price(client: TradeStationClient, underlying: str) -> float:
    q = client.get_quote(underlying)
    for key in ("Last", "Close"):
        if q.get(key):
            return float(q[key])
    bid, ask = float(q.get("Bid", 0) or 0), float(q.get("Ask", 0) or 0)
    if bid and ask:
        return (bid + ask) / 2
    raise TradeStationError(f"Could not determine price for {underlying}.")


def select_strike(
    strikes: list[float],
    underlying_price: float,
    right: str,
    selection: str,
    offset: int,
) -> float:
    """Find ATM (nearest listed strike) then move `offset` strikes ITM/OTM/none."""
    if not strikes:
        raise TradeStationError("No strikes available for this expiration.")
    atm_idx = min(range(len(strikes)), key=lambda i: abs(strikes[i] - underlying_price))

    sel = selection.upper()
    is_call = right.lower().startswith("c")
    step = 0
    if sel == "OTM":
        # calls: OTM = higher strikes; puts: OTM = lower strikes
        step = offset if is_call else -offset
    elif sel == "ITM":
        step = -offset if is_call else offset
    else:  # ATM
        step = 0

    idx = max(0, min(len(strikes) - 1, atm_idx + step))
    return strikes[idx]


def get_option_quote(client: TradeStationClient, option_symbol: str) -> dict:
    """Return {'bid','ask','mid','last'} for an option contract."""
    q = client.get_quote(option_symbol)
    bid = float(q.get("Bid", 0) or 0)
    ask = float(q.get("Ask", 0) or 0)
    last = float(q.get("Last", 0) or 0)
    mid = round((bid + ask) / 2, 2) if (bid and ask) else (last or ask or bid)
    return {"bid": bid, "ask": ask, "mid": mid, "last": last}


def resolve_contract(
    client: TradeStationClient,
    underlying: str,
    right: str,
    expiration_mode: str,
    strike_selection: str,
    strike_offset: int,
) -> dict:
    """Full pipeline: pick expiry -> price -> strikes -> strike -> symbol -> quote."""
    expiration = pick_expiration(client, underlying, expiration_mode)
    price = get_underlying_price(client, underlying)
    strikes = client.get_option_strikes(underlying, expiration.strftime("%m-%d-%Y"))
    strike = select_strike(strikes, price, right, strike_selection, strike_offset)
    symbol = build_option_symbol(underlying, expiration, right, strike)
    quote = get_option_quote(client, symbol)
    log.info(
        "Resolved %s %s @ %s exp %s -> %s | u=%.2f q=%s",
        underlying, right, strike, expiration, symbol, price, quote,
    )
    return {
        "option_symbol": symbol,
        "underlying": underlying,
        "right": right,
        "strike": strike,
        "expiration": expiration.isoformat(),
        "underlying_price": price,
        "quote": quote,
    }
