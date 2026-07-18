"""Loads and validates configuration from config.yaml + .env.

Everything tunable lives in config.yaml so the strategy can be changed without
touching code. Secrets (API keys) live in .env / environment variables.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent

LIVE_BASE = "https://api.tradestation.com/v3"
SIM_BASE = "https://sim-api.tradestation.com/v3"
SIGNIN_BASE = "https://signin.tradestation.com"


# ── config.yaml schema (validated on load) ────────────────────────────────────
class BrokerCfg(BaseModel):
    account_id: str = ""


class WebhookCfg(BaseModel):
    shared_secret: str = ""   # loaded from .env (WEBHOOK_SECRET); see load_settings


class DefaultsCfg(BaseModel):
    underlying: str = "SPY"
    expiration: str = "0DTE"        # 0DTE | NEXT
    strike_selection: str = "ATM"   # ATM | OTM | ITM
    strike_offset: int = 0
    entry_fill: str = "mid"         # market | mid | ask | bid
    exit_fill: str = "market"
    time_in_force: str = "DAY"


class SizingCfg(BaseModel):
    mode: str = "dollars"           # dollars | percent
    dollars: float = 500
    percent: float = 5
    max_contracts: int = 10
    buying_power_safety: float = 0.95
    max_exposure_pct: float = 0.0   # 0 = off; else block new entries past this % of equity in open positions


class TrimCfg(BaseModel):
    at_profit_pct: float
    close_pct: float


class TrailingCfg(BaseModel):
    enabled: bool = True
    activate_at_pct: float = 0.30
    trail_pct: float = 0.20


class BreakevenCfg(BaseModel):
    enabled: bool = True
    trigger_pct: float = 0.20       # once up this much, move the stop to entry (break-even)


class RiskCfg(BaseModel):
    enabled: bool = True            # master switch for the stop/trim/trailing/daily-loss monitor
    stop_loss_pct: float = 0.15
    take_profit_pct: float = 0.0    # 0 = off; else full exit at +X% (e.g. 0.20 = +20%)
    trims: list[TrimCfg] = Field(default_factory=list)
    trailing: TrailingCfg = Field(default_factory=TrailingCfg)
    breakeven: BreakevenCfg = Field(default_factory=BreakevenCfg)
    daily_max_loss_usd: float = 1000
    daily_trailing_stop_usd: float = 0.0   # 0 = off; else trail the DAY's P&L: flatten + halt when the
                                           # day P&L falls this many $ from its peak (locks in daily profit)
    max_losing_trades: int = 0      # 0 = off; else block NEW entries for the day after N losing trades


class GroupCfg(BaseModel):
    """A ticker group with its own risk/sizing; overrides global defaults for its
    symbols. Any field left null falls back to the global setting."""
    name: str
    symbols: list[str] = Field(default_factory=list)
    stop_loss_pct: Optional[float] = None
    take_profit_pct: Optional[float] = None
    trailing: Optional[TrailingCfg] = None
    breakeven: Optional[BreakevenCfg] = None
    trims: Optional[list[TrimCfg]] = None
    sizing_mode: Optional[str] = None       # dollars | percent
    dollars: Optional[float] = None
    percent: Optional[float] = None


class ExecutionCfg(BaseModel):
    multi_strategy_positions: bool = True
    slippage_ticks: int = 3
    poll_interval_sec: int = 3
    async_placement: bool = False   # True = ack webhook instantly, place order in background
    reverse_on_opposite: bool = True  # opposite signal closes the existing position then opens
    dedupe_window_sec: float = 1.0  # drop identical repeated signals within this window
    # Exit management mode:
    #   monitor = bot-side only (stop/trim/trail/break-even/TP in app/risk.py)
    #   bracket = broker holds a static OCO [TP limit + SL stop]; bot only reconciles (no trail/trim)
    #   hybrid  = broker holds a death-proof SL backstop (set below the bot stop) AND the bot keeps
    #             managing trail/trim/break-even/TP. Best protection; survives a bot/server outage.
    exit_mode: str = "hybrid"
    bracket_stop_type: str = "StopMarket"   # StopMarket | StopLimit (the broker stop leg)
    backstop_extra_pct: float = 0.05        # hybrid: broker stop sits this far BELOW the bot stop
    # Bot-side exit robustness vs bad/stale quote ticks:
    exit_confirm_ticks: int = 2             # require N consecutive ticks past a bid threshold before a bot-side exit
    max_spread_ratio: float = 0.5           # skip exit decisions when (ask-bid)/mid exceeds this (unreliable/wide quote)
    exit_smoothing_ticks: int = 5           # value the option at the MEDIAN of the last N prices (kills 1-2 tick spikes)
    max_quote_disagreement: float = 0.20    # skip the tick if bid vs last-traded disagree by more than this (garbage quote)
    hide_strategy_prefix: str = ""          # hide strategy_ids with this prefix from the dashboard + day P&L (e.g. "loop_" test trades)

    @property
    def attach_broker_exit(self) -> bool:
        return self.exit_mode in ("bracket", "hybrid")


class CacheCfg(BaseModel):
    """Pre-cache settings for the low-latency hot path."""
    symbols: list[str] = Field(default_factory=lambda: ["SPY"])
    hot_refresh_sec: float = 2.0    # underlying + option quotes
    chain_refresh_sec: float = 60.0  # expirations + strikes
    equity_refresh_sec: float = 5.0
    stale_after_sec: float = 8.0    # if hot data older than this, fall back to slow path


class ScheduleCfg(BaseModel):
    """Entries are only allowed inside this window; exits/risk are always allowed."""
    enabled: bool = False
    timezone: str = "America/New_York"
    entry_start: str = "09:30"        # HH:MM in `timezone`
    entry_end: str = "15:55"
    days: list[str] = Field(default_factory=lambda: ["Mon", "Tue", "Wed", "Thu", "Fri"])
    holidays: list[str] = Field(default_factory=list)   # e.g. ["2026-07-03"]
    # End-of-day flatten: force-close EVERY open position at/after this time (market) so a 0DTE
    # never rides into expiry unmanaged. Runs on the configured trading `days`.
    flatten_enabled: bool = True
    flatten_at: str = "15:50"         # HH:MM in `timezone` — set before the close (0DTE expiry)


class RetryCfg(BaseModel):
    enabled: bool = True
    applies_to: str = "both"        # entries | exits | both
    max_attempts: int = 3
    interval_sec: float = 2.0       # wait this long for a fill before re-pricing
    price_step_ticks: int = 1       # chase this many ticks toward marketable per attempt
    market_fallback: bool = True    # after max attempts, send a market order


class WatcherCfg(BaseModel):
    enabled: bool = True
    interval_sec: float = 60.0       # how often to reconcile bot state vs broker
    alert: bool = True               # send Telegram alerts on discrepancies
    auto_flatten_unmanaged: bool = True   # market-close a broker option position the bot isn't managing
                                          # (confirmed 2 cycles) so an orphan can't ride unprotected to expiry


class TelegramCfg(BaseModel):
    enabled: bool = False           # token + chat id come from env (TELEGRAM_BOT_TOKEN/CHAT_ID)
    notify_entries: bool = True
    notify_exits: bool = True
    notify_errors: bool = True
    notify_lifecycle: bool = True   # bot online/offline


class Settings(BaseModel):
    environment: str = "sim"        # sim | live
    broker: BrokerCfg
    webhook: WebhookCfg
    defaults: DefaultsCfg
    sizing: SizingCfg
    risk: RiskCfg
    groups: list[GroupCfg] = Field(default_factory=list)
    execution: ExecutionCfg
    cache: CacheCfg = Field(default_factory=CacheCfg)
    schedule: ScheduleCfg = Field(default_factory=ScheduleCfg)
    retry: RetryCfg = Field(default_factory=RetryCfg)
    watcher: WatcherCfg = Field(default_factory=WatcherCfg)
    telegram: TelegramCfg = Field(default_factory=TelegramCfg)

    # ── secrets (from env, not yaml) ──
    client_id: str = ""
    client_secret: str = ""
    refresh_token: str = ""
    redirect_uri: str = "http://localhost:8182/callback"
    telegram_token: str = ""
    telegram_chat_id: str = ""
    dashboard_token: str = ""
    dash_user: str = ""            # dashboard login username (.env DASH_USER)
    dash_pass: str = ""            # dashboard login password (.env DASH_PASS)

    @property
    def api_base(self) -> str:
        return SIM_BASE if self.environment.lower() == "sim" else LIVE_BASE

    @property
    def is_live(self) -> bool:
        return self.environment.lower() == "live"


# ── dashboard-editable runtime overrides ─────────────────────────────────────
# A whitelist of settings the client may change live from the dashboard. Each maps
# a dotted attribute path on the Settings object to a (kind, arg) validation spec.
# Overrides are persisted in the store (meta "settings_overrides") and re-applied on
# top of config.yaml at startup and on every /reload, so they survive a restart.
_VALID_DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

OVERRIDE_SPEC = {
    "execution.exit_mode":           ("choice", ["monitor", "bracket", "hybrid"]),
    "defaults.entry_fill":           ("choice", ["market", "mid", "ask", "bid"]),
    "defaults.exit_fill":            ("choice", ["market", "mid", "ask", "bid"]),
    "defaults.expiration":           ("choice", ["0DTE", "NEXT"]),
    "defaults.strike_selection":     ("choice", ["ATM", "OTM", "ITM"]),
    "defaults.strike_offset":        ("int", (0, 20)),
    "sizing.max_exposure_pct":       ("float", (0.0, 1.0)),
    "risk.trailing.enabled":         ("bool", None),
    "risk.trailing.activate_at_pct": ("float", (0.0, 5.0)),
    "risk.trailing.trail_pct":       ("float", (0.01, 0.99)),
    "risk.breakeven.enabled":        ("bool", None),
    "risk.breakeven.trigger_pct":    ("float", (0.0, 5.0)),
    "risk.max_losing_trades":        ("int", (0, 100)),
    "retry.max_attempts":            ("int", (1, 100)),
    "retry.interval_sec":            ("float", (0.2, 30.0)),
    "schedule.enabled":              ("bool", None),
    "schedule.entry_start":          ("time", None),
    "schedule.entry_end":            ("time", None),
    "schedule.days":                 ("days", None),
    "schedule.flatten_enabled":      ("bool", None),
    "schedule.flatten_at":           ("time", None),
}


def coerce_override(path: str, raw):
    """Validate + coerce one override value. Returns (ok, value, error)."""
    spec = OVERRIDE_SPEC.get(path)
    if not spec:
        return False, None, "unknown field"
    kind, arg = spec
    try:
        if kind == "choice":
            v = str(raw)
            return (True, v, None) if v in arg else (False, None, f"must be one of {arg}")
        if kind == "bool":
            if isinstance(raw, bool):
                v = raw
            elif isinstance(raw, str):
                v = raw.strip().lower() in ("1", "true", "yes", "on")
            else:
                v = bool(raw)
            return True, v, None
        if kind == "int":
            v, (lo, hi) = int(float(raw)), arg
            return (True, v, None) if lo <= v <= hi else (False, None, f"out of range [{lo},{hi}]")
        if kind == "float":
            v, (lo, hi) = float(raw), arg
            return (True, v, None) if lo <= v <= hi else (False, None, f"out of range [{lo},{hi}]")
        if kind == "time":
            hh, mm = str(raw).strip().split(":")
            h, m = int(hh), int(mm)
            return (True, f"{h:02d}:{m:02d}", None) if (0 <= h < 24 and 0 <= m < 60) else (False, None, "bad time")
        if kind == "days":
            items = raw
            if isinstance(items, str):
                items = items.replace(",", " ").split()
            norm = []
            for d in items or []:
                d3 = str(d).strip().title()[:3]
                if d3 in _VALID_DAYS and d3 not in norm:
                    norm.append(d3)
            if not norm:
                return False, None, "no valid days"
            norm.sort(key=_VALID_DAYS.index)
            return True, norm, None
    except (TypeError, ValueError, AttributeError):
        return False, None, "invalid value"
    return False, None, "unhandled"


def apply_overrides(settings: "Settings", overrides: dict) -> list:
    """Mutate the live Settings object in place from a stored overrides dict. Runs after
    load_settings() so the dashboard's changes win over config.yaml. Skips bad values."""
    applied = []
    for path, raw in (overrides or {}).items():
        ok, val, _err = coerce_override(path, raw)
        if not ok:
            continue
        try:
            obj = settings
            parts = path.split(".")
            for p in parts[:-1]:
                obj = getattr(obj, p)
            setattr(obj, parts[-1], val)
            applied.append((path, val))
        except (AttributeError, TypeError):
            pass
    return applied


def load_settings(config_path: Optional[str] = None) -> Settings:
    load_dotenv(ROOT / ".env")
    path = Path(config_path) if config_path else ROOT / "config.yaml"
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    settings = Settings(**raw)

    # secrets from environment
    settings.client_id = os.getenv("TS_CLIENT_ID", "")
    settings.client_secret = os.getenv("TS_CLIENT_SECRET", "")
    settings.refresh_token = os.getenv("TS_REFRESH_TOKEN", "")
    settings.redirect_uri = os.getenv("TS_REDIRECT_URI", settings.redirect_uri)
    settings.telegram_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    settings.telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    settings.dashboard_token = os.getenv("DASHBOARD_TOKEN", "")
    settings.dash_user = os.getenv("DASH_USER", "")
    settings.dash_pass = os.getenv("DASH_PASS", "")

    # Webhook secret: prefer .env (WEBHOOK_SECRET) over any value in config.yaml.
    settings.webhook.shared_secret = os.getenv("WEBHOOK_SECRET", settings.webhook.shared_secret)

    # TS_ENVIRONMENT in .env may only force the SAFER direction (sim). It can NEVER force live —
    # a stray env var must not be able to flip a client onto real money. Going live is an explicit
    # action: config.yaml environment: live, or the dashboard mode switch (meta trading_mode).
    env_override = (os.getenv("TS_ENVIRONMENT") or "").lower()
    if env_override == "sim":
        settings.environment = "sim"
    elif env_override == "live" and settings.environment.lower() != "live":
        import logging
        logging.getLogger("config").error(
            "TS_ENVIRONMENT=live in .env IGNORED — going live requires config.yaml environment: live "
            "or the dashboard mode switch. Staying on %s.", settings.environment)

    return settings


def guard_environment(settings: "Settings", sim_account: str) -> list:
    """Enforce environment/account coherence — mutates to a safe state and returns warnings.
    live must not point at a SIM account; sim must not point at a real account. On any
    mismatch we fall back to SIM + the configured sim account (never fail toward live)."""
    warnings = []
    acct = (settings.broker.account_id or "").upper()
    if settings.environment.lower() == "live" and acct.startswith("SIM"):
        warnings.append(f"environment=live but account {acct} is a SIM account — forcing SIM.")
        settings.environment = "sim"
    if settings.environment.lower() != "live" and acct and not acct.startswith("SIM"):
        warnings.append(f"environment=sim but account {acct} is a REAL account — restoring sim account {sim_account}.")
        settings.broker.account_id = sim_account
    return warnings
