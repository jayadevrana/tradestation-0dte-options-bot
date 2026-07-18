"""Trading-schedule gate.

entries_allowed() returns whether NEW entries may be placed right now, based on the
configured timezone, daily window, allowed weekdays, and holiday skip-list. Exits,
stops, trims, trailing, and the daily-loss kill-switch are NEVER gated by this — they
must always be able to run for risk management.
"""
from __future__ import annotations

import datetime as dt

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

_DAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def now_in_tz(tzname: str) -> dt.datetime:
    if ZoneInfo is not None:
        try:
            return dt.datetime.now(ZoneInfo(tzname))
        except Exception:  # noqa: BLE001 — bad tz / missing db -> local fallback
            pass
    return dt.datetime.now()


def _hhmm(s: str) -> tuple[int, int]:
    h, m = s.split(":")
    return int(h), int(m)


def entries_allowed(settings, now: dt.datetime | None = None) -> tuple[bool, str]:
    """Return (allowed, reason_if_blocked). `now` override is for testing."""
    sc = settings.schedule
    if not sc.enabled:
        return True, ""
    n = now or now_in_tz(sc.timezone)

    allowed_days = {_DAYS.get(d.strip().lower()[:3], -1) for d in sc.days}
    if n.weekday() not in allowed_days:
        return False, f"not a trading day ({n.strftime('%a')})"

    if n.strftime("%Y-%m-%d") in (sc.holidays or []):
        return False, f"holiday {n.strftime('%Y-%m-%d')}"

    sh, sm = _hhmm(sc.entry_start)
    eh, em = _hhmm(sc.entry_end)
    start = n.replace(hour=sh, minute=sm, second=0, microsecond=0)
    end = n.replace(hour=eh, minute=em, second=0, microsecond=0)
    if start <= n <= end:
        return True, ""
    return False, f"outside entry window {sc.entry_start}-{sc.entry_end} {sc.timezone}"


def flatten_due(settings, now: dt.datetime | None = None) -> bool:
    """True when it's at/after the configured end-of-day flatten time on a trading day, so the
    risk monitor should force-close every open position before 0DTE expiry."""
    sc = settings.schedule
    if not getattr(sc, "flatten_enabled", True):
        return False
    n = now or now_in_tz(sc.timezone)
    allowed_days = {_DAYS.get(d.strip().lower()[:3], -1) for d in sc.days}
    if n.weekday() not in allowed_days:
        return False
    if n.strftime("%Y-%m-%d") in (sc.holidays or []):
        return False
    fh, fm = _hhmm(getattr(sc, "flatten_at", "15:50"))
    flat = n.replace(hour=fh, minute=fm, second=0, microsecond=0)
    return n >= flat
