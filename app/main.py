"""FastAPI webhook server — directional 0DTE options bot, low-latency hot path.

TradingView posts a JSON alert to /webhook:
    BUY  -> open a CALL   |   SELL -> open a PUT   |   EXIT -> close the position

The MarketCache keeps each symbol's ATM call/put symbols + quotes + equity hot in
memory, so the order path is: validate -> dedupe -> dict read -> size -> POST.
A background RiskMonitor (if risk.enabled) manages stop/trim/trailing/daily-loss.

Run:  uvicorn app.main:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import asyncio
import csv
import hashlib
import hmac
import html
import io
import logging
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from .bracket import BracketManager
from .chaser import Chaser
from .config import (LIVE_BASE, OVERRIDE_SPEC, apply_overrides, coerce_override,
                     guard_environment, load_settings)
from .dashboard import DASHBOARD_HTML, build_state
from .groups import basket_for, basket_settings, effective_sizing, group_name
from .market_cache import MarketCache
from .models import OrderResult, WebhookPayload
from .notify import Notifier
from .options import normalize_underlying
from .orders import OrderEngine, real_pnl_prices
from .schedule import entries_allowed
from .risk import RiskMonitor
from .state import Store
from .tradelog import record_exit
from .telegram_commands import TelegramPoller
from .ts_client import TradeStationClient, TradeStationError
from .watcher import Watcher

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("main")

settings = load_settings()
client = TradeStationClient(settings)
engine = OrderEngine(settings, client)
store = Store()
cache = MarketCache(settings, client)
notifier = Notifier(settings.telegram_token, settings.telegram_chat_id, settings.telegram.enabled)
bracket = BracketManager(settings, client, store, notifier)
monitor = RiskMonitor(settings, client, engine, store, notifier, bracket)
chaser = Chaser(settings, client, store, notifier)

# Re-apply any dashboard-set runtime overrides on top of config.yaml (in place, so every
# manager above — which holds the same settings object — sees them). Survives restarts.
apply_overrides(settings, store.get_settings_overrides())

# ── trading mode (sim/live) — dashboard-switchable, persisted in the store ────
# config.yaml always carries the SIM account; the live account id is set per server in meta
# ("live_account_id"). meta "trading_mode"=="live" flips environment+account at startup and via
# POST /api/set-mode. guard_environment() makes a mismatched state fail SAFE (toward sim).
SIM_ACCOUNT_ID = settings.broker.account_id      # the yaml (paper) account — restore target


def _apply_trading_mode() -> None:
    """Apply meta trading_mode onto the live settings object + client base. Fail toward sim."""
    mode = (store.get_meta("trading_mode") or "sim").lower()
    live_acct = (store.get_meta("live_account_id") or "").strip()
    if mode == "live" and live_acct and not live_acct.upper().startswith("SIM"):
        settings.environment = "live"
        settings.broker.account_id = live_acct
    else:
        if mode == "live":
            log.error("trading_mode=live but live_account_id is missing/invalid (%r) — staying SIM.", live_acct)
        settings.environment = "sim"
        settings.broker.account_id = SIM_ACCOUNT_ID
    for w in guard_environment(settings, SIM_ACCOUNT_ID):
        log.error("ENV GUARD: %s", w)
    client.base = settings.api_base
    if settings.is_live:
        log.warning("⚠️ TRADING MODE: LIVE — real money. account=%s", settings.broker.account_id)


_apply_trading_mode()

ACCOUNT_ID = ""
STARTED_AT = time.time()
tg_poller = None
watcher = Watcher(settings, client, store, notifier, lambda: ACCOUNT_ID)
_recent: dict[str, float] = {}        # idempotency: key -> monotonic ts
_recent_lock = threading.Lock()
# per-(symbol|strategy_id) lock so concurrent webhooks (e.g. buy + exit arriving together,
# or alert resends) can't race the get->decide->place->record critical section.
_pos_locks: dict[str, threading.Lock] = {}
_pos_locks_guard = threading.Lock()


def _pos_lock(symbol: str, strategy_id: str) -> threading.Lock:
    key = f"{(symbol or '').upper()}|{strategy_id}"
    with _pos_locks_guard:
        lk = _pos_locks.get(key)
        if lk is None:
            lk = threading.Lock()
            _pos_locks[key] = lk
        return lk


def _dash_authorized(token: str) -> bool:
    t = settings.dashboard_token
    return bool(t) and hmac.compare_digest(token or "", t)


# ── dashboard login (username/password -> signed session cookie) ──────────────
SESSION_COOKIE = "dash_session"
SESSION_TTL = 12 * 3600           # re-login required after 12h


def _session_key() -> str:
    # sign sessions with the dashboard token (already a per-server secret in .env)
    return settings.dashboard_token or "no-token-set"


def _make_session() -> str:
    exp = str(int(time.time()) + SESSION_TTL)
    sig = hmac.new(_session_key().encode(), exp.encode(), hashlib.sha256).hexdigest()
    return f"{exp}.{sig}"


def _valid_session(cookie: str) -> bool:
    if not cookie or "." not in cookie:
        return False
    exp_s, sig = cookie.rsplit(".", 1)
    try:
        if int(exp_s) < time.time():
            return False
    except ValueError:
        return False
    good = hmac.new(_session_key().encode(), exp_s.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, good)


def _login_ok(user: str, pw: str) -> bool:
    du, dp = settings.dash_user, settings.dash_pass
    if not du or not dp:                       # creds not configured -> login disabled
        return False
    return hmac.compare_digest(user or "", du) and hmac.compare_digest(pw or "", dp)


# Auth is enforced centrally here. A valid login cookie makes the request authorized WITHOUT any
# token in the URL (so the shareable link carries no secret). Programmatic/admin access can still
# pass ?token=<DASHBOARD_TOKEN>. Anything else is bounced to /login (pages) or 401 (API).
_OPEN_PATHS = {"/login", "/logout", "/health", "/webhook", "/favicon.ico", "/visualize"}


async def _token_keepalive():
    """Refresh the OAuth token in the background so the order hot path never does.
    Runs every 30s; keepalive() refreshes ~2 min before expiry, so the token is
    always warm well before an order would need it."""
    while True:
        await asyncio.sleep(30)
        try:
            await asyncio.to_thread(client.keepalive)
        except Exception:  # noqa: BLE001
            log.exception("token keepalive failed")


def _reconcile_positions():
    """On startup, drop local positions the broker doesn't actually hold, so stale
    state from a previous run can't block new entries or mis-fire an exit."""
    try:
        broker = {p.get("Symbol") for p in client.get_positions(ACCOUNT_ID)}
        for pos in store.all_open():
            if pos["option_symbol"] not in broker:
                # If a bracket OCO closed this while the bot was down, the sibling leg may
                # still be resting at the broker — cancel it so it can't fire on nothing.
                if pos.get("bracket_ids"):
                    bracket.cancel(ACCOUNT_ID, pos)
                store.mark_closed(pos["symbol"], pos["strategy_id"])
                log.warning("Reconcile: local %s not held at broker -> marked closed.",
                            pos["option_symbol"])
    except TradeStationError as e:
        log.error("Startup reconciliation skipped: %s", e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global ACCOUNT_ID
    tasks = []
    try:
        client.warm()
        ACCOUNT_ID = client.resolve_account_id()
        _reconcile_positions()
        cache.prime()
        log.info("Account %s ready. environment=%s live=%s",
                 ACCOUNT_ID, settings.environment, settings.is_live)
        if settings.telegram.notify_lifecycle:
            notifier.send(f"🟢 <b>Bot online</b>\nenv={settings.environment} · account={ACCOUNT_ID} · "
                          f"risk={'on' if settings.risk.enabled else 'off'} · symbols={settings.cache.symbols}")
    except TradeStationError as e:
        log.error("Startup warm/prime failed (will retry in background): %s", e)
        if settings.telegram.notify_errors:
            notifier.send(f"⚠️ <b>Bot startup error</b>\n{e}")
    global tg_poller
    tasks.append(asyncio.create_task(_token_keepalive()))
    tasks.append(asyncio.create_task(cache.run()))
    if settings.watcher.enabled:
        tasks.append(asyncio.create_task(watcher.run()))
        log.info("Watcher ENABLED.")
    if settings.telegram.enabled and settings.telegram_token and settings.telegram_chat_id:
        tg_poller = TelegramPoller(settings.telegram_token, settings.telegram_chat_id, handle_telegram_command)
        tasks.append(asyncio.create_task(tg_poller.run()))
        log.info("Telegram command listener ENABLED.")
    if settings.risk.enabled:
        tasks.append(asyncio.create_task(monitor.run()))
        log.info("Risk monitor ENABLED.")
    else:
        log.info("Risk monitor DISABLED (risk.enabled=false) — order placement only.")
    try:
        yield
    finally:
        if settings.telegram.notify_lifecycle:
            notifier.send("🔴 <b>Bot stopped</b>")
        cache.stop()
        monitor.stop()
        watcher.stop()
        if tg_poller:
            tg_poller.stop()
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except asyncio.CancelledError:
                pass


app = FastAPI(title="TradeStation 0DTE Options Bot", lifespan=lifespan)


@app.middleware("http")
async def _auth_gate(request: Request, call_next):
    path = request.url.path
    if path in _OPEN_PATHS:
        return await call_next(request)
    cookie = request.cookies.get(SESSION_COOKIE, "")
    if settings.dash_user and _valid_session(cookie):
        # logged in: inject the real token so downstream endpoints authorize with no URL secret
        from urllib.parse import parse_qs, urlencode
        q = parse_qs(request.scope.get("query_string", b"").decode())
        q["token"] = [settings.dashboard_token]
        request.scope["query_string"] = urlencode(q, doseq=True).encode()
        return await call_next(request)
    # not logged in: allow a valid ?token= through (admin/programmatic), else bounce
    if _dash_authorized(request.query_params.get("token", "")):
        return await call_next(request)
    if path.startswith("/api/") or path in ("/positions", "/cache", "/halt", "/reload", "/export/trades"):
        return JSONResponse(status_code=401, content={"error": "unauthorized — log in"})
    return RedirectResponse(url="/login")


@app.get("/login")
def login_page(bad: int = 0):
    if not settings.dash_user:
        return HTMLResponse("Login not configured — set DASH_USER / DASH_PASS in .env.", status_code=503)
    err = ('<p style="color:#f85149;margin:0 0 12px">Wrong username or password.</p>' if bad else "")
    return HTMLResponse(f"""<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sign in — TradeStation Bot</title>
<body style="margin:0;background:#0d1117;color:#e6edf3;font:15px -apple-system,Segoe UI,Roboto,sans-serif;
display:flex;min-height:100vh;align-items:center;justify-content:center">
<form method="post" action="/login" style="background:#161b22;border:1px solid #30363d;border-radius:12px;
padding:28px;width:300px">
<h2 style="margin:0 0 4px">TradeStation Bot</h2>
<div style="color:#8b949e;font-size:13px;margin-bottom:18px">Sign in to your dashboard</div>
{err}
<input name="user" placeholder="Username" autofocus autocomplete="username"
style="width:100%;box-sizing:border-box;margin-bottom:10px;background:#0d1117;color:#e6edf3;
border:1px solid #30363d;border-radius:8px;padding:11px">
<input name="pw" type="password" placeholder="Password" autocomplete="current-password"
style="width:100%;box-sizing:border-box;margin-bottom:16px;background:#0d1117;color:#e6edf3;
border:1px solid #30363d;border-radius:8px;padding:11px">
<button type="submit" style="width:100%;background:#238636;color:#fff;border:0;border-radius:8px;
padding:11px;font-weight:700;font-size:15px;cursor:pointer">Sign in</button>
</form></body>""")


@app.post("/login")
async def login_submit(request: Request):
    # parse the urlencoded form body manually (avoids a python-multipart dependency)
    from urllib.parse import parse_qs
    body = parse_qs((await request.body()).decode())
    user = (body.get("user") or [""])[0]
    pw = (body.get("pw") or [""])[0]
    if not _login_ok(user, pw):
        return RedirectResponse(url="/login?bad=1", status_code=303)
    resp = RedirectResponse(url="/dashboard", status_code=303)
    resp.set_cookie(SESSION_COOKIE, _make_session(), max_age=SESSION_TTL,
                    httponly=True, samesite="lax")
    log.info("Dashboard login OK from %s", request.client.host if request.client else "?")
    return resp


@app.get("/logout")
def logout():
    resp = RedirectResponse(url="/login", status_code=303)
    resp.delete_cookie(SESSION_COOKIE)
    return resp


@app.get("/visualize")
def visualize():
    """PUBLIC educational page (no login): animated demo of every protection feature.
    Contains no account data and no controls — safe to share as a plain link."""
    from .visualize import VISUALIZE_HTML
    return HTMLResponse(VISUALIZE_HTML)



def _is_duplicate(key: str) -> bool:
    window = settings.execution.dedupe_window_sec
    now = time.monotonic()
    with _recent_lock:
        # opportunistic prune
        for k in [k for k, t in _recent.items() if now - t > 60]:
            _recent.pop(k, None)
        last = _recent.get(key)
        _recent[key] = now
    return last is not None and (now - last) < window


@app.get("/")
def root():
    return RedirectResponse(url="/dashboard")


@app.get("/dashboard")
def dashboard(token: str = ""):
    if not settings.dashboard_token:
        return HTMLResponse("Dashboard disabled — set DASHBOARD_TOKEN in .env.", status_code=503)
    if not _dash_authorized(token):
        return HTMLResponse("<h3>Unauthorized</h3><p>Open this page with <code>?token=YOUR_TOKEN</code>.</p>",
                            status_code=401)
    return HTMLResponse(DASHBOARD_HTML)


@app.get("/api/dashboard")
def api_dashboard(token: str = ""):
    if not _dash_authorized(token):
        return JSONResponse(status_code=401, content={"error": "unauthorized"})
    return build_state(settings, store, cache, ACCOUNT_ID, monitor, STARTED_AT, watcher)


@app.post("/api/set-daily-loss")
def set_daily_loss(token: str = "", value: float = 0.0):
    """Dashboard control: change the daily-loss kill-switch limit at runtime (persists in the
    store, survives restart, overrides config). Token-gated."""
    if not _dash_authorized(token):
        return JSONResponse(status_code=401, content={"ok": False, "error": "unauthorized"})
    try:
        v = float(value)
    except (TypeError, ValueError):
        return JSONResponse(status_code=422, content={"ok": False, "error": "value must be a number"})
    if not (0 < v <= 1_000_000):
        return JSONResponse(status_code=422, content={"ok": False, "error": "out of range (0 < value <= 1,000,000)"})
    store.set_meta("daily_max_loss_override", str(v))
    log.warning("Daily-loss limit changed via dashboard -> $%.0f", v)
    return {"ok": True, "daily_max_loss_usd": v}


@app.post("/api/set-daily-trailing")
def set_daily_trailing(token: str = "", value: float = 0.0):
    """Dashboard control: daily TRAILING profit-protect stop. Flatten + halt when the day's P&L
    falls this many $ from its peak. 0 = off. Token-gated; persists; survives restart."""
    if not _dash_authorized(token):
        return JSONResponse(status_code=401, content={"ok": False, "error": "unauthorized"})
    try:
        v = float(value)
    except (TypeError, ValueError):
        return JSONResponse(status_code=422, content={"ok": False, "error": "value must be a number"})
    if not (0 <= v <= 1_000_000):
        return JSONResponse(status_code=422, content={"ok": False, "error": "out of range (0..1,000,000; 0 = off)"})
    store.set_meta("daily_trailing_override", str(v))
    store.set_meta("day_peak_pnl", "0")   # re-anchor the high-water mark when the setting changes
    log.warning("Daily TRAILING stop changed via dashboard -> $%.0f (0=off)", v)
    return {"ok": True, "daily_trailing_stop_usd": v}


@app.get("/api/baskets")
def api_get_baskets(token: str = ""):
    if not _dash_authorized(token):
        return JSONResponse(status_code=401, content={"error": "unauthorized"})
    return {"baskets": store.get_baskets()}


@app.post("/api/baskets")
async def api_set_baskets(request: Request, token: str = ""):
    """Dashboard control: configure the trading baskets (symbol groups + their risk/sizing).
    The bot applies a basket's settings to every symbol in it, at entry. Token-gated."""
    if not _dash_authorized(token):
        return JSONResponse(status_code=401, content={"ok": False, "error": "unauthorized"})
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse(status_code=422, content={"ok": False, "error": "bad json"})
    baskets = body.get("baskets") if isinstance(body, dict) else body
    if not isinstance(baskets, list):
        return JSONResponse(status_code=422, content={"ok": False, "error": "baskets must be a list"})
    clean = []
    for b in baskets[:10]:
        if not isinstance(b, dict):
            continue

        def num(key, lo, hi, default):
            try:
                v = float(b.get(key))
                return v if lo <= v <= hi else default
            except (TypeError, ValueError):
                return default

        def inum(key, lo, hi, default):
            try:
                v = int(float(b.get(key)))
                return v if lo <= v <= hi else default
            except (TypeError, ValueError):
                return default

        def choice(key, opts, default):
            v = str(b.get(key) or "")
            return v if v in opts else default
        raw = b.get("symbols")
        if isinstance(raw, str):
            raw = raw.replace(",", " ").split()
        syms = [str(s).strip().upper() for s in (raw or []) if str(s).strip()]
        clean.append({
            "name": str(b.get("name") or "Basket")[:40],
            "symbols": syms[:50],
            "stop_loss_pct": num("stop_loss_pct", 0.001, 0.95, 0.15),
            "take_profit_pct": num("take_profit_pct", 0.0, 5.0, 0.0),
            "trim_pct": num("trim_pct", 0.0, 0.99, 0.0),
            "trim_at": num("trim_at", 0.0, 5.0, 0.0),
            "be_at": num("be_at", 0.0, 5.0, 0.0),
            "fix_trail_pct": num("fix_trail_pct", 0.0, 0.95, 0.0),
            "onelot_trim_exit": bool(b.get("onelot_trim_exit")),
            "sizing_mode": "percent" if b.get("sizing_mode") == "percent" else "dollars",
            "dollars": num("dollars", 1, 1_000_000, 500),
            "percent": num("percent", 0.1, 100, 5),
            # per-basket strategy config (each basket is an independent strategy)
            "exit_mode": choice("exit_mode", ("monitor", "bracket", "hybrid"), "hybrid"),
            "entry_fill": choice("entry_fill", ("market", "mid", "ask", "bid"), "market"),
            "exit_fill": choice("exit_fill", ("market", "mid", "ask", "bid"), "market"),
            "expiration": choice("expiration", ("0DTE", "NEXT"), "0DTE"),
            "strike_selection": choice("strike_selection", ("ATM", "OTM", "ITM"), "ATM"),
            "strike_offset": inum("strike_offset", 0, 20, 0),
            "max_exposure_pct": num("max_exposure_pct", 0.0, 1.0, 0.0),
            "trailing_enabled": bool(b.get("trailing_enabled")),
            "trail_activate_pct": num("trail_activate_pct", 0.0, 5.0, 0.30),
            "trail_pct": num("trail_pct", 0.0, 0.99, 0.20),
            "max_losing_trades": inum("max_losing_trades", 0, 100, 0),
            "max_contracts": inum("max_contracts", 1, 1000, 10),
            "retry_max_attempts": inum("retry_max_attempts", 1, 100, 20),
            "retry_interval_sec": num("retry_interval_sec", 0.2, 30.0, 2.0),
        })
    store.set_baskets(clean)
    log.info("Baskets updated via dashboard: %d baskets, symbols=%s",
             len(clean), [s for b in clean for s in b["symbols"]])
    return {"ok": True, "baskets": clean}


def _editable_settings() -> dict:
    """Current values of the dashboard-editable settings (flat keys the UI renders)."""
    s = settings
    return {
        "execution.exit_mode": s.execution.exit_mode,
        "defaults.entry_fill": s.defaults.entry_fill,
        "defaults.exit_fill": s.defaults.exit_fill,
        "defaults.expiration": s.defaults.expiration,
        "defaults.strike_selection": s.defaults.strike_selection,
        "defaults.strike_offset": s.defaults.strike_offset,
        "sizing.max_exposure_pct": s.sizing.max_exposure_pct,
        "risk.trailing.enabled": s.risk.trailing.enabled,
        "risk.trailing.activate_at_pct": s.risk.trailing.activate_at_pct,
        "risk.trailing.trail_pct": s.risk.trailing.trail_pct,
        "risk.breakeven.enabled": s.risk.breakeven.enabled,
        "risk.breakeven.trigger_pct": s.risk.breakeven.trigger_pct,
        "risk.max_losing_trades": s.risk.max_losing_trades,
        "retry.max_attempts": s.retry.max_attempts,
        "retry.interval_sec": s.retry.interval_sec,
        "schedule.enabled": s.schedule.enabled,
        "schedule.entry_start": s.schedule.entry_start,
        "schedule.entry_end": s.schedule.entry_end,
        "schedule.days": s.schedule.days,
        "schedule.flatten_enabled": s.schedule.flatten_enabled,
        "schedule.flatten_at": s.schedule.flatten_at,
    }


@app.get("/api/settings")
def api_get_settings(token: str = ""):
    if not _dash_authorized(token):
        return JSONResponse(status_code=401, content={"error": "unauthorized"})
    return {"settings": _editable_settings()}


@app.post("/api/settings")
async def api_set_settings(request: Request, token: str = ""):
    """Dashboard control: change strategy settings live (exit mode, fills, trailing, break-even,
    max exposure, retry/chase, max losing trades, trading window, …). Each change is validated
    against a whitelist, persisted (survives restart/reload), and applied in place. Token-gated."""
    if not _dash_authorized(token):
        return JSONResponse(status_code=401, content={"ok": False, "error": "unauthorized"})
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse(status_code=422, content={"ok": False, "error": "bad json"})
    if not isinstance(body, dict):
        return JSONResponse(status_code=422, content={"ok": False, "error": "expected an object of field->value"})
    overrides = store.get_settings_overrides()
    applied, errors = {}, {}
    for path, raw in body.items():
        ok, val, err = coerce_override(path, raw)
        if ok:
            overrides[path] = val
            applied[path] = val
        else:
            errors[path] = err
    if applied:
        store.set_settings_overrides(overrides)
        apply_overrides(settings, overrides)   # mutate the live settings object in place
        log.warning("Settings changed via dashboard: %s", applied)
    return {"ok": not errors, "applied": applied, "errors": errors, "settings": _editable_settings()}


_ET_TZ = ZoneInfo("America/New_York")


def _et_str(ts) -> str:
    """Epoch seconds -> human-readable US Eastern (market) time, e.g. '2026-07-09 09:31:04 ET'."""
    try:
        return datetime.fromtimestamp(float(ts), _ET_TZ).strftime("%Y-%m-%d %H:%M:%S ET")
    except (TypeError, ValueError, OSError):
        return ""


@app.get("/api/mode")
def api_get_mode(token: str = ""):
    if not _dash_authorized(token):
        return JSONResponse(status_code=401, content={"error": "unauthorized"})
    return {"mode": "live" if settings.is_live else "sim",
            "environment": settings.environment,
            "account": settings.broker.account_id,
            "sim_account": SIM_ACCOUNT_ID,
            "live_account": store.get_meta("live_account_id") or "",
            "open_positions": len(store.all_open())}


@app.post("/api/set-mode")
def api_set_mode(token: str = "", mode: str = "", confirm: str = ""):
    """Dashboard control: switch between SIM (paper) and LIVE (real money) trading.
    Guards: must be flat; live requires a configured, verified live account AND confirm=LIVE."""
    if not _dash_authorized(token):
        return JSONResponse(status_code=401, content={"ok": False, "error": "unauthorized"})
    global ACCOUNT_ID
    mode = (mode or "").lower()
    if mode not in ("sim", "live"):
        return JSONResponse(status_code=422, content={"ok": False, "error": "mode must be sim or live"})
    if store.all_open():
        return JSONResponse(status_code=409, content={
            "ok": False, "error": "Close all open positions before switching mode."})
    if mode == "live":
        if confirm != "LIVE":
            return JSONResponse(status_code=428, content={
                "ok": False, "error": "confirm=LIVE required to switch to real-money trading"})
        live_acct = (store.get_meta("live_account_id") or "").strip()
        if not live_acct or live_acct.upper().startswith("SIM"):
            return JSONResponse(status_code=422, content={
                "ok": False, "error": "No live account configured for this server (live_account_id)."})
        # verify the live account is actually reachable under this login BEFORE committing
        old_base = client.base
        try:
            client.base = LIVE_BASE
            accs = [a.get("AccountID") for a in client.get_accounts()]
            if live_acct not in accs:
                client.base = old_base
                return JSONResponse(status_code=422, content={
                    "ok": False, "error": f"Live account {live_acct} not visible under this login ({accs})."})
        except TradeStationError as e:
            client.base = old_base
            return JSONResponse(status_code=502, content={"ok": False, "error": f"live account check failed: {e}"})
    store.set_meta("trading_mode", mode)
    _apply_trading_mode()
    # re-anchor the daily session to the NEW account's equity (else day-PnL compares live equity
    # against the sim baseline and instantly trips the kill-switch), and clear any halts.
    store.set_meta("session_date", "")
    store.set_meta("halted", "")
    store.set_meta("entries_halted", "")
    try:
        ACCOUNT_ID = client.resolve_account_id()
        cache.prime()
    except Exception:  # noqa: BLE001
        log.exception("post-switch prime failed (cache will recover on its own)")
    msg = (f"🔴 <b>LIVE TRADING ON</b> — real money!\naccount {settings.broker.account_id}"
           if settings.is_live else
           f"🟢 <b>Back on SIM (paper)</b>\naccount {settings.broker.account_id}")
    notifier.send(msg)
    log.warning("MODE SWITCH via dashboard -> %s (account %s)", mode.upper(), settings.broker.account_id)
    try:
        store.log_event(kind="mode", action=mode, ok=True,
                        detail=f"trading mode -> {mode.upper()} account {settings.broker.account_id}")
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True, "mode": mode, "environment": settings.environment,
            "account": settings.broker.account_id}


@app.get("/export/trades")
def export_trades(token: str = ""):
    if not _dash_authorized(token):
        return JSONResponse(status_code=401, content={"error": "unauthorized"})
    # Full §3.18 audit-trail field set (populated where data is available). entry_time_et / exit_time_et
    # are readable US-Eastern (market) times the client can read directly; the raw epoch columns
    # (opened_at/fill_time/closed_at) are kept alongside for audit.
    fields = ["id", "entry_time_et", "exit_time_et", "closed_at", "opened_at", "alert_time",
              "submitted_time", "fill_time", "minutes",
              "symbol", "option_symbol", "put_call", "side", "quantity", "strategy_id",
              "position_size_usd", "account_value", "entry_price", "exit_price",
              "pnl", "pnl_pct", "net_pnl", "commission", "max_profit_pct", "max_drawdown_pct",
              "stop_loss_pct", "take_profit_pct", "signal_score", "retry_count",
              "buy_reason", "exit_reason", "exit_method", "trade_status", "notes"]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(fields)
    hide = settings.execution.hide_strategy_prefix
    for r in store.all_trades():
        if hide and (r.get("strategy_id") or "").startswith(hide):
            continue   # omit internal/test (loop_) trades from the client export
        row = []
        for f in fields:
            if f == "entry_time_et":
                row.append(_et_str(r.get("fill_time") or r.get("opened_at")))
            elif f == "exit_time_et":
                row.append(_et_str(r.get("closed_at")))
            else:
                row.append(r.get(f, ""))
        w.writerow(row)
    return Response(content=buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=trades.csv"})


@app.get("/health")
def health():
    return {
        "status": "ok",
        "environment": settings.environment,
        "live_trading": settings.is_live,
        "account": ACCOUNT_ID,
        "risk_enabled": settings.risk.enabled,
        "halted_today": monitor.is_halted() if settings.risk.enabled else False,
        "open_positions": len(store.all_open()),
        "cached_symbols": settings.cache.symbols,
    }


@app.get("/positions")
def positions(token: str = ""):
    if not _dash_authorized(token):
        return JSONResponse(status_code=401, content={"error": "unauthorized"})
    return {"open": store.all_open(),
            "equity_cached": cache.equity(),
            "halted_today": monitor.is_halted() if settings.risk.enabled else False}


@app.get("/cache")
def cache_state(token: str = ""):
    """Debug: what the hot cache currently holds for each symbol."""
    if not _dash_authorized(token):
        return JSONResponse(status_code=401, content={"error": "unauthorized"})
    out = {}
    for sym in settings.cache.symbols:
        hot = cache.get(sym.upper())
        if hot:
            out[sym] = {"expiration": hot.expiration, "underlying": hot.underlying_price,
                        "call": hot.call_symbol, "call_quote": hot.call_quote,
                        "put": hot.put_symbol, "put_quote": hot.put_quote,
                        "age_sec": round(time.monotonic() - hot.updated, 2)}
        else:
            out[sym] = "cold/stale"
    return {"equity": cache.equity(), "symbols": out}


@app.post("/halt")
def manual_halt(token: str = ""):
    if not _dash_authorized(token):
        return JSONResponse(status_code=401, content={"ok": False, "error": "unauthorized"})
    try:
        monitor.halt_and_flatten(client.resolve_account_id(), "manual /halt")
        return {"ok": True, "halted": True}
    except TradeStationError as e:
        return JSONResponse(status_code=502, content={"ok": False, "error": str(e)})


@app.post("/webhook")
async def webhook(request: Request):
    t0 = time.perf_counter()
    body = await request.json()
    try:
        payload = WebhookPayload(**body)
    except Exception as e:  # noqa: BLE001
        return JSONResponse(status_code=422, content={"ok": False, "error": f"bad payload: {e}"})

    configured = settings.webhook.shared_secret
    if not configured:
        log.error("Webhook secret not configured (set WEBHOOK_SECRET in .env). Rejecting all.")
        return JSONResponse(status_code=500, content={"ok": False, "error": "server misconfigured: no webhook secret"})
    if not hmac.compare_digest(payload.secret or "", configured):
        log.warning("Rejected webhook: bad secret.")
        return JSONResponse(status_code=401, content={"ok": False, "error": "unauthorized"})

    action = payload.normalized_action()
    symbol = normalize_underlying(payload.symbol or settings.defaults.underlying)
    strategy_id = payload.strategy_id if settings.execution.multi_strategy_positions else "default"
    if not action:
        return JSONResponse(status_code=422,
                            content={"ok": False, "error": f"unknown action '{payload.action}'"})

    if _is_duplicate(payload.dedupe_key(symbol, strategy_id, action)):
        log.info("Duplicate %s %s/%s within dedupe window — ignored.", action, symbol, strategy_id)
        return {"ok": True, "message": "duplicate ignored", "duplicate": True}

    # async placement: ack instantly, fire order in the background (lowest TV-perceived latency)
    if settings.execution.async_placement:
        task = asyncio.create_task(asyncio.to_thread(_run_signal, payload, action, symbol, strategy_id, t0))

        def _log_async_result(t: asyncio.Task):
            try:
                t.result()
            except Exception:  # noqa: BLE001
                log.exception("ASYNC_FAILURE: background order task raised")

        task.add_done_callback(_log_async_result)
        return {"ok": True, "accepted": True, "action": action, "symbol": symbol}

    try:
        result = await asyncio.to_thread(_run_signal, payload, action, symbol, strategy_id, t0)
        latency_ms = round((time.perf_counter() - t0) * 1000, 1)
        return {"ok": result.ok, "message": result.message,
                "order_id": result.order_id, "latency_ms": latency_ms}
    except TradeStationError as e:
        log.exception("Broker error")
        return JSONResponse(status_code=502, content={"ok": False, "error": str(e)})
    except Exception as e:  # noqa: BLE001
        log.exception("Unhandled error")
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


def _run_signal(payload: WebhookPayload, action: str, symbol: str,
                strategy_id: str, t0: float) -> OrderResult:
    try:
        result = handle_signal(payload, action, symbol, strategy_id)
    except Exception as e:  # noqa: BLE001 — never let a signal raise into the async task
        log.exception("Signal handling failed")
        result = OrderResult(ok=False, message=f"error: {e}")
    latency_ms = round((time.perf_counter() - t0) * 1000, 1)
    log.info("SIGNAL %s %s/%s -> ok=%s order_id=%s (%.1fms) | %s",
             action, symbol, strategy_id, result.ok, result.order_id, latency_ms, result.message)
    try:
        store.log_event(kind="signal", action=action, symbol=symbol, strategy_id=strategy_id,
                        option_symbol=result.option_symbol or "", ok=result.ok,
                        order_id=result.order_id, latency_ms=latency_ms, detail=result.message)
    except Exception:  # noqa: BLE001
        log.exception("log_event failed (non-fatal)")
    _notify_trade(action, symbol, strategy_id, result, latency_ms)
    return result


def _notify_trade(action: str, symbol: str, strategy_id: str,
                  result: OrderResult, latency_ms: float) -> None:
    """Build and enqueue a Telegram alert for a signal. Never raises."""
    try:
        hide = settings.execution.hide_strategy_prefix
        if hide and (strategy_id or "").startswith(hide):
            return   # don't alert the client for internal self-test (loop_) trades
        tg = settings.telegram
        ignored = "ignored" in result.message.lower()
        is_exit = action == "exit"
        if not result.ok:
            if not tg.notify_errors:
                return
            head = f"⚠️ <b>{action.upper()} FAILED</b>"
        elif ignored:
            head = "ℹ️ <b>signal ignored</b>"
        elif is_exit:
            if not tg.notify_exits:
                return
            head = "⚪ <b>EXIT</b> ✅"
        else:
            if not tg.notify_entries:
                return
            arrow = "🟢 <b>BUY → CALL</b> ✅" if action == "buy" else "🔴 <b>SELL → PUT</b> ✅"
            head = arrow
        esc = html.escape  # prevent HTML-injection breaking the Telegram message
        strat = "" if strategy_id == "default" else f" [{esc(strategy_id)}]"
        lines = [head, f"{esc(symbol)}{strat}"]
        if result.option_symbol:
            qty = f"{result.quantity}x " if result.quantity else ""
            typ = f" · {esc(result.order_type)}" if result.order_type else ""
            lines.append(f"{qty}{esc(result.option_symbol)}{typ}")
        if result.order_id and result.ok:
            lines.append(f"order {esc(str(result.order_id))} · ⚡{latency_ms:.0f}ms")
        if not result.ok:
            lines.append(esc(result.message[:160]))
        notifier.send("\n".join(lines))
    except Exception:  # noqa: BLE001
        log.exception("notify_trade failed (non-fatal)")


def _entry_budget(symbol: str, payload: WebhookPayload) -> tuple:
    """Resolve sizing as $/% with precedence: per-signal payload > dashboard basket > group > global."""
    if payload.dollars is not None:
        return payload.dollars, None
    if payload.percent is not None:
        return None, payload.percent
    b = basket_for(store.get_baskets(), symbol)   # dashboard basket sizing
    if b:
        if (b.get("sizing_mode") or "dollars") == "percent":
            return None, float(b.get("percent") or 5)
        return float(b.get("dollars") or 500), None
    es = effective_sizing(settings, symbol)
    if es.mode == "percent":
        return None, es.percent
    return es.dollars, None


def _open_entry(symbol: str, right: str, payload: WebhookPayload, bs) -> OrderResult:
    # entry fill + contract selection come from the symbol's basket (a per-signal webhook
    # value still overrides). The pre-cached hot path only holds the ATM 0DTE contract, so
    # use it ONLY when the basket asks for ATM/0DTE/offset-0; anything else takes the slow
    # resolve path so the basket's expiration/strike is actually honored.
    fill = payload.fill or bs.entry_fill
    dollars, percent = _entry_budget(symbol, payload)   # basket/group-aware sizing
    exp = payload.expiration or bs.expiration
    ssel = payload.strike_selection or bs.strike_selection
    soff = payload.strike_offset if payload.strike_offset is not None else bs.strike_offset
    can_fast = (str(exp).upper() == "0DTE" and str(ssel).upper() == "ATM" and int(soff or 0) == 0)
    if can_fast:
        hot = cache.get(symbol)
        if hot:
            quote = hot.quote_for(right)
            if quote.get("ask"):
                equity = cache.equity() or client.get_equity(ACCOUNT_ID)
                return engine.place_entry_fast(ACCOUNT_ID, hot.symbol_for(right), quote, equity,
                                               fill, dollars, percent,
                                               max_contracts=bs.max_contracts)
    # slow resolve path (cache cold/stale, symbol not pre-cached, or non-ATM/0DTE basket contract)
    log.info("Resolving %s %s %s+%s via slow path (non-cached contract or cold cache).",
             symbol, exp, ssel, soff)
    return engine.open_position(ACCOUNT_ID, symbol, right, exp, ssel, int(soff or 0),
                                fill, dollars, percent, max_contracts=bs.max_contracts)


def _close_entry(pos: dict, payload: WebhookPayload) -> OrderResult:
    # exit fill comes from the position's basket (stamped at entry); per-signal webhook overrides.
    fill = payload.fill or pos.get("exit_fill") or settings.defaults.exit_fill
    quote = None if fill.lower() == "market" else cache.quote_for_symbol(pos["option_symbol"])
    return engine.place_exit_fast(ACCOUNT_ID, pos["option_symbol"], pos["quantity"], fill, quote)


def _safe_mark_closed(symbol: str, strategy_id: str) -> None:
    try:
        pos = store.get_open(symbol, strategy_id)
        if pos and pos.get("bracket_ids"):
            bracket.cancel(ACCOUNT_ID, pos)   # cancel any resting TP/SL so none orphan
        store.mark_closed(symbol, strategy_id)
    except Exception:  # noqa: BLE001
        log.exception("DB mark_closed failed for %s/%s", symbol, strategy_id)


def _record_close(pos: dict, reason: str, exit_order_id=None, exit_method: str = "signal (market)") -> None:
    """Record a closed trade using the broker's ACTUAL fill prices (entry + exit), so
    dashboard P&L matches TradeStation exactly, with the full §3.18 field set."""
    try:
        entry_real, exit_real = real_pnl_prices(client, ACCOUNT_ID, pos, exit_order_id)
        record_exit(store, settings, pos, entry_real, exit_real, reason,
                    exit_method=exit_method, account_value=cache.equity() or None)
    except Exception:  # noqa: BLE001
        log.exception("record_trade failed (non-fatal)")


def _exposure_ok(symbol: str, payload: WebhookPayload, basket: dict, bs) -> tuple[bool, str]:
    """Block a new entry if this BASKET's open capital + this order would exceed the basket's
    max_exposure_pct of equity. Exposure is scoped to the basket's own symbols (0 = off)."""
    mp = bs.max_exposure_pct
    if not mp:
        return True, ""
    eq = cache.equity() or 0.0
    if eq <= 0:
        return True, ""
    basket_syms = {str(s).upper() for s in (basket.get("symbols") or [])}
    exposure = sum((p.get("entry_price") or 0) * (p.get("quantity") or 0) * 100
                   for p in store.all_open() if (p.get("symbol") or "").upper() in basket_syms)
    d, pc = _entry_budget(symbol, payload)
    budget = float(d) if d is not None else eq * float(pc or 0) / 100.0
    cap = eq * mp
    if exposure + budget > cap:
        return False, (f"Entry blocked — basket '{basket.get('name')}' max exposure {int(mp*100)}% "
                       f"(${cap:,.0f}) would be exceeded: open ${exposure:,.0f} + ~${budget:,.0f}.")
    return True, ""


def handle_signal(payload: WebhookPayload, action: str, symbol: str, strategy_id: str) -> OrderResult:
    # Serialize everything for this symbol|strategy_id so two concurrent webhooks can't both
    # read a stale position and double-act (e.g. buy + exit, or an alert resend).
    with _pos_lock(symbol, strategy_id):
        return _handle_signal(payload, action, symbol, strategy_id)


def _handle_signal(payload: WebhookPayload, action: str, symbol: str, strategy_id: str) -> OrderResult:
    if action == "exit":
        pos = store.get_open(symbol, strategy_id)
        if not pos:
            return OrderResult(ok=False, message=f"No open position for {symbol}/{strategy_id}")
        # Cancel any resting broker OCO/backstop FIRST (gated on the position actually having
        # one — exit mode is now per-basket) so it can't fire while we close on the TradingView
        # exit, and so no leg is left orphaned at the broker.
        if pos.get("bracket_ids"):
            bracket.cancel(ACCOUNT_ID, pos)
        result = _close_entry(pos, payload)
        if result.ok:
            if result.order_type == "Limit" and chaser.applies("exit"):
                # chase the limit close to a fill; it records the trade + marks closed when filled.
                # retry cadence/attempts come from the position's basket (if any).
                _xb = basket_for(store.get_baskets(), symbol)
                _xbs = basket_settings(settings, _xb) if _xb else None
                chaser.spawn(mode="exit", account_id=ACCOUNT_ID, symbol=symbol, strategy_id=strategy_id,
                             option_symbol=pos["option_symbol"], trade_action="SELLTOCLOSE",
                             quantity=pos["quantity"], order_id=result.order_id, limit_price=result.limit_price,
                             max_attempts=(_xbs.retry_max_attempts if _xbs else None),
                             interval_sec=(_xbs.retry_interval_sec if _xbs else None))
            else:
                _record_close(pos, "signal-exit", result.order_id)
                _safe_mark_closed(symbol, strategy_id)
        return result

    # buy / sell -> directional entry
    if settings.risk.enabled and monitor.entries_halted():
        return OrderResult(ok=False, message="Trading halted for the day (daily loss / max losing trades).")

    allowed, why = entries_allowed(settings)
    if not allowed:
        return OrderResult(ok=False, message=f"Entry blocked — {why} (exits still allowed).")

    # Baskets are the sole allow-list: a symbol not in ANY basket is rejected (the client
    # configures every tradable symbol into a basket on the dashboard).
    basket = basket_for(store.get_baskets(), symbol)
    if not basket:
        return OrderResult(ok=False,
                           message=f"{symbol} is not in any basket — entry rejected. "
                                   f"Add it to a basket on the dashboard.")
    bs = basket_settings(settings, basket)

    # per-basket max losing trades: pause NEW entries for this basket after N losers today
    if bs.max_losing_trades > 0:
        since = float(store.get_meta("session_start_ts") or 0)
        losers = store.losing_trades_today_for(
            since, basket.get("symbols") or [],
            exclude_prefix=settings.execution.hide_strategy_prefix or None)
        if losers >= bs.max_losing_trades:
            return OrderResult(ok=False, message=(
                f"Basket '{basket.get('name')}' hit its max losing trades "
                f"({losers}/{bs.max_losing_trades}) — entries paused for today."))

    ok_exp, why_exp = _exposure_ok(symbol, payload, basket, bs)
    if not ok_exp:
        return OrderResult(ok=False, message=why_exp)

    right = payload.right_for(action)              # buy->call, sell->put
    existing = store.get_open(symbol, strategy_id)
    if existing:
        if existing["side"] == right:
            return OrderResult(ok=True, message=f"Already {right} on {symbol}/{strategy_id}; duplicate entry ignored.")
        if settings.execution.reverse_on_opposite:
            if existing.get("bracket_ids"):
                bracket.cancel(ACCOUNT_ID, existing)   # drop the old broker exit before reversing
            close = _close_entry(existing, payload)
            if not close.ok:
                # close failed -> DO NOT open the opposite, or we'd hold both legs.
                log.error("Reverse aborted: close of %s failed (%s). Not opening %s.",
                          existing["side"], close.message, right)
                return OrderResult(ok=False, option_symbol=existing["option_symbol"],
                                   message=f"Reverse aborted — could not close existing {existing['side']}: {close.message}")
            _record_close(existing, "reverse", close.order_id)
            _safe_mark_closed(symbol, strategy_id)
            log.info("Reversed: closed %s before opening %s.", existing["side"], right)
        else:
            return OrderResult(ok=False, message=f"Opposite {existing['side']} open; reverse disabled.")

    result = _open_entry(symbol, right, payload, bs)
    if result.ok and result.quantity > 0:
        # capture §3.18 entry-time audit fields from the alert (signal score / reason / arrival time)
        extra = getattr(payload, "model_extra", None) or {}
        try:
            score = float(extra.get("score", extra.get("signal_score")))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            score = None
        buy_reason = extra.get("reason") or extra.get("comment") or f"{action.upper()} signal → {right.upper()}"

        def _pct(v, lo, hi):   # accept a sane fraction, else ignore -> falls back to config group
            try:
                v = float(v)
                return v if lo <= v <= hi else None
            except (TypeError, ValueError):
                return None
        tp_o = _pct(payload.tp_pct, 0.0001, 5.0)   # >0 .. +500%; 0/blank -> fall back below
        sl_o = _pct(payload.sl_pct, 0.001, 0.95)   # 0.1% .. 95% stop-loss
        trim_pct_o = _pct(payload.trim_pct, 0.01, 0.99)   # sell 1%..99% of the position at the trim
        trim_at_o = _pct(payload.trim_at, 0.01, 5.0)      # trigger at +1%..+500% profit
        be_at_o = _pct(payload.be_at, 0.0001, 5.0)        # arm break-even at +X% (move stop to entry)
        ftr_o = _pct(payload.fix_trail_pct, 0.005, 0.95)  # fixed trail: exit at peak*(1-X), from entry
        # DASHBOARD BASKET drives the risk (the client only sends secret/action/symbol). An explicit
        # per-signal webhook value still overrides the basket. Basket values are AUTHORITATIVE
        # (a 0 means "off", not "fall back to config"). `basket`/`bs` were resolved at the entry gate.
        def _num(v):
            try:
                return float(v)
            except (TypeError, ValueError):
                return None
        if sl_o is None:      sl_o = _num(basket.get("stop_loss_pct")) or sl_o
        if tp_o is None:      tp_o = _num(basket.get("take_profit_pct"))    # 0 -> no take-profit
        if trim_pct_o is None: trim_pct_o = _num(basket.get("trim_pct"))
        if trim_at_o is None:  trim_at_o = _num(basket.get("trim_at"))      # not-None -> basket-controlled trims
        if be_at_o is None:    be_at_o = _num(basket.get("be_at"))          # 0 -> no break-even
        if ftr_o is None:      ftr_o = _num(basket.get("fix_trail_pct"))    # 0 -> no fixed trail
        try:
            store.upsert_open(symbol, strategy_id, result.option_symbol, right,
                              result.quantity, result.entry_ref_price or 0.0, result.order_id,
                              signal_score=score, buy_reason=str(buy_reason)[:120], alert_time=time.time(),
                              tp_pct=tp_o, sl_pct=sl_o, trim_pct=trim_pct_o, trim_at=trim_at_o, be_at=be_at_o,
                              fix_trail_pct=ftr_o,
                              onelot_exit=bool(basket.get("onelot_trim_exit")),
                              exit_mode=bs.exit_mode, exit_fill=bs.exit_fill,
                              trail_enabled=bs.trailing_enabled, trail_activate=bs.trail_activate_pct,
                              trail_pct=bs.trail_pct)
        except Exception:  # noqa: BLE001
            # The order WAS placed. Returning an error here would make TradingView retry
            # and double-place. Keep ok=True; reconciliation will recover the tracking.
            log.exception("CRITICAL: order placed (%s %s) but DB record failed — position untracked.",
                          result.option_symbol, result.order_id)
            result.message += " [WARNING: order placed but local tracking failed]"
        if result.order_type == "Limit" and chaser.applies("entry"):
            # chase the limit entry to a fill in the background (never blocks the webhook);
            # retry cadence/attempts come from the basket
            chaser.spawn(mode="entry", account_id=ACCOUNT_ID, symbol=symbol, strategy_id=strategy_id,
                         option_symbol=result.option_symbol, trade_action="BUYTOOPEN",
                         quantity=result.quantity, order_id=result.order_id, limit_price=result.limit_price,
                         max_attempts=bs.retry_max_attempts, interval_sec=bs.retry_interval_sec)
        if bs.attach_broker_exit:
            # Attach the broker-side exit (pure OCO TP/SL, or hybrid stop backstop) off the REAL
            # entry fill, in the background, once the broker confirms the position. Per-basket exit mode.
            bracket.spawn_attach(account_id=ACCOUNT_ID, symbol=symbol, strategy_id=strategy_id,
                                 option_symbol=result.option_symbol, quantity=result.quantity)
    return result


def handle_telegram_command(text: str) -> str:
    """Dispatch a Telegram command. Only called for the authorized chat (poller checks)."""
    cmd = text.strip().split()[0].lower().lstrip("/").split("@")[0]
    if cmd in ("status", "s", "health"):
        d = build_state(settings, store, cache, ACCOUNT_ID, monitor, STARTED_AT, watcher)
        st = d["status"]
        return ("📊 Status\n"
                f"env: {st['environment']} ({'LIVE $$' if st['live_trading'] else 'paper'})\n"
                f"risk: {'ON' if st['risk_enabled'] else 'OFF'}  |  halted: {'YES' if st['halted_today'] else 'no'}\n"
                f"open positions: {st['open_count']}  |  equity: ${st['equity']:,.0f}\n"
                f"today P&L: ${d['day_pnl']:.2f}")
    if cmd in ("positions", "pos", "p", "position"):
        rows = build_state(settings, store, cache, ACCOUNT_ID, monitor, STARTED_AT, watcher)["open_positions"]
        if not rows:
            return "No open positions."
        out = ["📈 Open positions:"]
        for r in rows:
            up = r.get("unrealized_pct")
            upt = "" if up is None else f"  ({up*100:+.1f}%)"
            out.append(f"• {r['symbol']} {(r.get('side') or '').upper()} {r['option_symbol']} x{r['quantity']} "
                       f"@ {r.get('entry_price')}→{r.get('current_price')}{upt}")
        return "\n".join(out)
    if cmd in ("pnl",):
        p = cache.pnl()
        r, u = p.get("realized"), p.get("unrealized")
        if r is not None or u is not None:
            return (f"💰 P&L (TradeStation): ${(r or 0) + (u or 0):.2f}\n"
                    f"realized ${r or 0:.2f} · open ${u or 0:.2f}")
        return f"Realized P&L (calc): ${store.trades_today_pnl(exclude_prefix=settings.execution.hide_strategy_prefix or None):.2f}"
    if cmd in ("closeall", "close", "stop", "flatten", "panic", "stopall"):
        try:
            monitor.halt_and_flatten(ACCOUNT_ID, "manual /closeall via Telegram")
            return "🚨 Closing ALL positions and halting new trades for today.\nSend /resume to allow trading again."
        except Exception as e:  # noqa: BLE001
            return f"⚠️ closeall failed: {e}"
    if cmd in ("resume", "go", "unhalt"):
        store.set_meta("halted", "")
        store.set_meta("entries_halted", "")
        return "✅ Halt cleared (daily-loss + max-losing-trades) — new entries are allowed again."
    if cmd in ("help", "start", "commands", "menu"):
        return ("🤖 Bot commands:\n"
                "/status — bot status & today's P&L\n"
                "/positions — current open positions\n"
                "/pnl — today's realized P&L\n"
                "/closeall — close ALL positions + stop for today\n"
                "/resume — allow trading again after a halt\n"
                "/help — this list")
    return "Unknown command. Send /help for the list."


@app.post("/reload")
def reload_config(token: str = ""):
    if not _dash_authorized(token):
        return JSONResponse(status_code=401, content={"ok": False, "error": "unauthorized"})
    global settings, engine, notifier, ACCOUNT_ID, SIM_ACCOUNT_ID
    prev_env = settings.environment
    settings = load_settings()
    SIM_ACCOUNT_ID = settings.broker.account_id                 # yaml always carries the sim account
    apply_overrides(settings, store.get_settings_overrides())   # keep dashboard overrides across reloads
    _apply_trading_mode()                                       # keep the sim/live mode across reloads
    if settings.environment != prev_env:   # loudly flag any sim<->live transition
        log.warning("RELOAD: environment changed %s -> %s", prev_env, settings.environment)
        if settings.is_live:
            log.warning("⚠️ BOT IS NOW LIVE (real money). account=%s", settings.broker.account_id)
    client.s = settings
    client.base = settings.api_base
    engine = OrderEngine(settings, client)
    notifier = Notifier(settings.telegram_token, settings.telegram_chat_id, settings.telegram.enabled)
    monitor.s = settings
    monitor.engine = engine
    monitor.notifier = notifier
    bracket.s = settings
    bracket.notifier = notifier
    cache.s = settings
    chaser.s = settings
    chaser.notifier = notifier
    watcher.s = settings
    watcher.notifier = notifier
    try:
        ACCOUNT_ID = client.resolve_account_id()
    except TradeStationError as e:
        log.error("reload: could not re-resolve account: %s", e)
    return {"reloaded": True, "environment": settings.environment, "account": ACCOUNT_ID}
