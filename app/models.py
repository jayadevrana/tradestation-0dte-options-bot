"""Pydantic models for the incoming TradingView webhook payload.

DIRECTIONAL model (what TradingView sends):

    BUY signal  -> open a CALL  (bullish)
    SELL signal -> open a PUT   (bearish)
    EXIT signal -> close the open position for that symbol/strategy

Minimal JSON to paste in the TradingView alert message box:

    { "secret": "<shared_secret>", "action": "buy",  "symbol": "SPY" }   # -> buy CALL
    { "secret": "<shared_secret>", "action": "sell", "symbol": "SPY" }   # -> buy PUT
    { "secret": "<shared_secret>", "action": "exit", "symbol": "SPY" }   # -> close

Optional fields: strategy_id (separate multi-timeframe positions), id (idempotency
key to dedupe TradingView resends), and per-signal overrides of the config defaults
(fill / expiration / strike_selection / strike_offset / dollars / percent / side).
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

# action synonyms -> canonical action
_BUY = {"buy", "long", "buytoopen", "call", "bull"}
_SELL = {"sell", "short", "put", "bear"}
_EXIT = {"exit", "close", "flat", "flatten", "closeall", "close_all"}


class WebhookPayload(BaseModel):
    secret: str
    action: Optional[str] = None                 # buy | sell | exit (+ synonyms). Optional if market_position is sent.
    symbol: Optional[str] = None                 # underlying; default from config
    strategy_id: str = "default"                 # separates multi-timeframe positions
    id: Optional[str] = None                     # optional idempotency key (dedupe resends)

    # TradingView STRATEGY placeholders (one alert box for everything). market_position is the
    # position AFTER the order, which disambiguates entries from exits (a close-long also emits
    # action="sell"). When present it DRIVES the action: flat->exit, long->buy(call), short->sell(put).
    market_position: Optional[str] = None        # {{strategy.market_position}}: long | short | flat
    prev_market_position: Optional[str] = None   # {{strategy.prev_market_position}}

    # optional explicit override of the call/put direction (normally derived from action)
    side: Optional[str] = None                   # call | put

    # optional per-signal overrides of config defaults
    expiration: Optional[str] = None             # 0DTE | NEXT
    strike_selection: Optional[str] = None       # ATM | OTM | ITM
    strike_offset: Optional[int] = None
    fill: Optional[str] = None                   # market | mid | ask | bid

    # optional explicit sizing override
    dollars: Optional[float] = None
    percent: Optional[float] = None

    # optional per-signal TP/SL override (fraction of option premium, e.g. 0.20 = +20% TP,
    # 0.15 = -15% SL). Omit to use the symbol's config-group values.
    tp_pct: Optional[float] = None
    sl_pct: Optional[float] = None

    # optional per-signal partial-trim + move-to-break-even (overrides the group config)
    trim_pct: Optional[float] = None    # fraction of the position to sell at the trim (0.50 = 50%)
    trim_at: Optional[float] = None     # profit fraction that triggers the trim (0.30 = +30%)
    be_at: Optional[float] = None       # profit fraction that arms break-even = move stop to entry
    fix_trail_pct: Optional[float] = None  # FIXED trailing stop: exit when price falls this fraction from the peak (trails from ENTRY)

    model_config = {"extra": "allow"}            # tolerate extra TradingView fields

    def normalized_action(self) -> str:
        """Map to canonical: 'buy' | 'sell' | 'exit' | '' (unknown).

        For a TradingView STRATEGY (one alert box), market_position is authoritative — the
        position AFTER the order tells us intent regardless of the raw buy/sell action:
            flat  -> exit (close)   |   long -> buy (open CALL)   |   short -> sell (open PUT)
        Only when market_position is absent/unrecognized do we fall back to the action word."""
        mp = (self.market_position or "").strip().lower()
        if mp in ("flat", "none", "0"):
            return "exit"
        if mp == "long":
            return "buy"
        if mp == "short":
            return "sell"
        a = (self.action or "").strip().lower()
        if a in _EXIT:
            return "exit"
        if a in _BUY:
            return "buy"
        if a in _SELL:
            return "sell"
        return ""

    def right_for(self, action: str) -> str:
        """Option right for a directional action; explicit `side` overrides."""
        if self.side:
            return "call" if self.side.strip().lower().startswith("c") else "put"
        return "call" if action == "buy" else "put"

    def dedupe_key(self, symbol: str, strategy_id: str, action: str) -> str:
        return self.id or f"{symbol}|{strategy_id}|{action}"


class OrderResult(BaseModel):
    ok: bool
    message: str = ""
    order_id: Optional[str] = None
    option_symbol: Optional[str] = None
    quantity: int = 0
    limit_price: Optional[float] = None
    entry_ref_price: Optional[float] = None   # reference premium for risk tracking
    order_type: str = ""
    raw: dict = Field(default_factory=dict)
