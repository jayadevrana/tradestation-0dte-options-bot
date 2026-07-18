"""Centralised closed-trade recording (§3.18 audit trail).

Every exit path (risk monitor, broker bracket, limit-chaser, signal exit) routes through
record_exit() so each closed trade logs a CONSISTENT, full field set: stop/take-profit %,
account value, exit method, max drawdown, retry count, commission, net P&L, signal score,
buy/sell reason, and the entry-time fields carried on the position. Fields with no data
available (per contract §3.18) are left null.
"""
from __future__ import annotations

import logging
from typing import Optional

from .groups import effective_risk

log = logging.getLogger("tradelog")


def record_exit(store, settings, pos: dict, entry_price: float, exit_price: float,
                exit_reason: str, exit_method: str, *, client=None, account_id: Optional[str] = None,
                account_value: Optional[float] = None, retry_count: Optional[int] = None,
                commission: Optional[float] = None, trade_status: str = "closed",
                notes: Optional[str] = None) -> None:
    try:
        r = effective_risk(settings, pos.get("symbol"))
        if account_value is None and client is not None and account_id:
            try:
                account_value = client.get_equity(account_id)
            except Exception:  # noqa: BLE001 — account value is best-effort
                account_value = None
        # record the TP/SL actually in force — the per-signal override if present, else the group
        sl = pos.get("sl_pct") if pos.get("sl_pct") is not None else getattr(r, "stop_loss_pct", None)
        tp = pos.get("tp_pct") if pos.get("tp_pct") is not None else getattr(r, "take_profit_pct", None)
        store.record_trade(
            pos.get("symbol"), pos.get("strategy_id"), pos.get("option_symbol"),
            pos.get("side"), pos.get("quantity") or 0, entry_price, exit_price,
            exit_reason, pos.get("opened_at"), pos.get("peak_price"), pos.get("trough_price"),
            account_value=account_value,
            stop_loss_pct=sl,
            take_profit_pct=(tp or None),
            exit_method=exit_method, trade_status=trade_status, retry_count=retry_count,
            commission=commission, signal_score=pos.get("signal_score"),
            buy_reason=pos.get("buy_reason"), notes=notes, alert_time=pos.get("alert_time"),
        )
    except Exception:  # noqa: BLE001 — logging a trade must never break trading
        log.exception("record_exit failed (non-fatal)")
