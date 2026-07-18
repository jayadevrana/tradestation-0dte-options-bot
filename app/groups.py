"""Strategy config groups (§3.13).

A symbol can belong to a named group with its own stop/take-profit/trailing/
break-even/sizing. Group settings OVERRIDE the global defaults for that symbol;
any field the group leaves null falls back to global. Symbols in no group use
the global config entirely.

effective_risk()/effective_sizing() return the resolved settings for a symbol,
used by the risk monitor (per-position management) and the entry sizing.
"""
from __future__ import annotations

from types import SimpleNamespace


def group_for(settings, symbol: str):
    groups = getattr(settings, "groups", None)
    if not groups:
        return None
    su = (symbol or "").upper()
    for g in groups:
        if su in [s.upper() for s in g.symbols]:
            return g
    return None


def effective_risk(settings, symbol: str):
    r = settings.risk
    g = group_for(settings, symbol)
    if not g:
        return r
    return SimpleNamespace(
        stop_loss_pct=g.stop_loss_pct if g.stop_loss_pct is not None else r.stop_loss_pct,
        take_profit_pct=g.take_profit_pct if g.take_profit_pct is not None else r.take_profit_pct,
        trims=g.trims if g.trims is not None else r.trims,
        trailing=g.trailing if g.trailing is not None else r.trailing,
        breakeven=g.breakeven if g.breakeven is not None else r.breakeven,
    )


def effective_sizing(settings, symbol: str):
    sz = settings.sizing
    g = group_for(settings, symbol)
    mode, dollars, percent = sz.mode, sz.dollars, sz.percent
    if g:
        if g.sizing_mode:
            mode = g.sizing_mode
        if g.dollars is not None:
            dollars = g.dollars
        if g.percent is not None:
            percent = g.percent
    return SimpleNamespace(mode=mode, dollars=dollars, percent=percent)


def group_name(settings, symbol: str) -> str:
    g = group_for(settings, symbol)
    return g.name if g else ""


def basket_for(baskets, symbol: str):
    """Dashboard-configured baskets (store.get_baskets()) take precedence over config
    groups. Return the basket dict whose symbol list contains this symbol, else None."""
    su = (symbol or "").upper()
    for b in (baskets or []):
        if su in [str(s).upper().strip() for s in (b.get("symbols") or [])]:
            return b
    return None


def basket_settings(settings, basket: dict):
    """Resolve a basket's FULL per-basket strategy config (exit mode, fills, expiration/strike,
    max exposure, trailing, retry, max losing trades). Every field the basket leaves blank falls
    back to the global config, so a partially-filled basket still trades safely. Trading window
    and daily-loss stay account-wide (not per basket). Returns a SimpleNamespace."""
    g = settings

    def val(key, default):
        v = (basket or {}).get(key)
        return v if v not in (None, "") else default

    def fnum(key, default):
        try:
            return float(val(key, default))
        except (TypeError, ValueError):
            return default

    def inum(key, default):
        try:
            return int(float(val(key, default)))
        except (TypeError, ValueError):
            return default

    exit_mode = str(val("exit_mode", g.execution.exit_mode))
    if exit_mode not in ("monitor", "bracket", "hybrid"):
        exit_mode = g.execution.exit_mode
    entry_fill = str(val("entry_fill", g.defaults.entry_fill))
    exit_fill = str(val("exit_fill", g.defaults.exit_fill))
    expiration = str(val("expiration", g.defaults.expiration))
    strike_selection = str(val("strike_selection", g.defaults.strike_selection))
    return SimpleNamespace(
        exit_mode=exit_mode,
        entry_fill=entry_fill,
        exit_fill=exit_fill,
        expiration=expiration,
        strike_selection=strike_selection,
        strike_offset=inum("strike_offset", g.defaults.strike_offset),
        max_exposure_pct=fnum("max_exposure_pct", g.sizing.max_exposure_pct),
        trailing_enabled=bool((basket or {}).get("trailing_enabled", False)),
        trail_activate_pct=fnum("trail_activate_pct", g.risk.trailing.activate_at_pct),
        trail_pct=fnum("trail_pct", g.risk.trailing.trail_pct),
        max_losing_trades=inum("max_losing_trades", 0),
        max_contracts=inum("max_contracts", g.sizing.max_contracts),
        retry_max_attempts=inum("retry_max_attempts", g.retry.max_attempts),
        retry_interval_sec=fnum("retry_interval_sec", g.retry.interval_sec),
        attach_broker_exit=(exit_mode in ("bracket", "hybrid")),
    )
