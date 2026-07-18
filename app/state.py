"""Tiny SQLite-backed position store.

Positions are keyed by (symbol + strategy_id) so the SAME underlying can hold
independent positions from two timeframes (e.g. SPY_5m and SPY_15m). Also holds
the day-start equity baseline and the 'halted' flag for the daily-loss kill-switch
so they survive a restart.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

DB_PATH = Path(__file__).resolve().parent.parent / "bot_state.db"

# columns the risk engine is allowed to update in place
_UPDATABLE = {"quantity", "entry_price", "peak_price", "trough_price", "trims_done",
              "order_id", "status", "bracket_ids"}


class Store:
    def __init__(self, path: Optional[str] = None):
        self.path = str(path or DB_PATH)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init()

    def _init(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS positions (
                    key                TEXT PRIMARY KEY,   -- symbol|strategy_id
                    symbol             TEXT,
                    strategy_id        TEXT,
                    option_symbol      TEXT,
                    side               TEXT,
                    quantity           INTEGER,            -- REMAINING contracts
                    original_quantity  INTEGER,            -- size at open (for trim math)
                    entry_price        REAL,               -- reference premium at entry
                    peak_price         REAL,               -- running peak (for trailing)
                    trims_done         INTEGER DEFAULT 0,
                    order_id           TEXT,
                    status             TEXT,               -- open | closed
                    opened_at          REAL
                );
                CREATE TABLE IF NOT EXISTS meta (
                    k TEXT PRIMARY KEY,
                    v TEXT
                );
                CREATE TABLE IF NOT EXISTS events (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts          REAL,
                    kind        TEXT,            -- signal | exit | error | halt | info
                    action      TEXT,            -- buy | sell | exit
                    symbol      TEXT,
                    strategy_id TEXT,
                    option_symbol TEXT,
                    ok          INTEGER,
                    order_id    TEXT,
                    latency_ms  REAL,
                    detail      TEXT
                );
                CREATE TABLE IF NOT EXISTS trades (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol        TEXT,
                    strategy_id   TEXT,
                    option_symbol TEXT,
                    side          TEXT,
                    quantity      INTEGER,
                    entry_price   REAL,
                    exit_price    REAL,
                    pnl           REAL,
                    pnl_pct       REAL,
                    exit_reason   TEXT,
                    opened_at     REAL,
                    closed_at     REAL
                );
                """
            )
            # migrate: add newer trade-log columns to an existing DB (ignore if present).
            # Full §3.18 audit-trail field set (populated where the data is available).
            for col, typ in (("put_call", "TEXT"), ("position_size_usd", "REAL"),
                             ("minutes", "REAL"), ("max_profit_pct", "REAL"),
                             ("buy_reason", "TEXT"), ("notes", "TEXT"),
                             ("alert_time", "REAL"), ("submitted_time", "REAL"), ("fill_time", "REAL"),
                             ("signal_score", "REAL"), ("account_value", "REAL"),
                             ("stop_loss_pct", "REAL"), ("take_profit_pct", "REAL"),
                             ("max_drawdown_pct", "REAL"), ("retry_count", "INTEGER"),
                             ("exit_method", "TEXT"), ("trade_status", "TEXT"),
                             ("commission", "REAL"), ("net_pnl", "REAL")):
                try:
                    self._conn.execute(f"ALTER TABLE trades ADD COLUMN {col} {typ}")
                except sqlite3.OperationalError:
                    pass
            # migrate: extra per-position columns
            for col, typ in (("bracket_ids", "TEXT"), ("trough_price", "REAL"),
                             ("signal_score", "REAL"), ("buy_reason", "TEXT"),
                             ("alert_time", "REAL"), ("tp_pct", "REAL"), ("sl_pct", "REAL"),
                             ("trim_pct", "REAL"), ("trim_at", "REAL"), ("be_at", "REAL"),
                             ("exit_mode", "TEXT"), ("exit_fill", "TEXT"),
                             ("trail_enabled", "INTEGER"), ("trail_activate", "REAL"),
                             ("trail_pct", "REAL"), ("fix_trail_pct", "REAL"), ("onelot_exit", "INTEGER")):
                try:
                    self._conn.execute(f"ALTER TABLE positions ADD COLUMN {col} {typ}")
                except sqlite3.OperationalError:
                    pass
            self._conn.commit()

    @staticmethod
    def make_key(symbol: str, strategy_id: str) -> str:
        return f"{symbol.upper()}|{strategy_id}"

    def upsert_open(
        self,
        symbol: str,
        strategy_id: str,
        option_symbol: str,
        side: str,
        quantity: int,
        entry_price: float,
        order_id: Optional[str],
        signal_score: Optional[float] = None,
        buy_reason: Optional[str] = None,
        alert_time: Optional[float] = None,
        tp_pct: Optional[float] = None,
        sl_pct: Optional[float] = None,
        trim_pct: Optional[float] = None,
        trim_at: Optional[float] = None,
        be_at: Optional[float] = None,
        fix_trail_pct: Optional[float] = None,
        onelot_exit: Optional[bool] = None,
        exit_mode: Optional[str] = None,
        exit_fill: Optional[str] = None,
        trail_enabled: Optional[bool] = None,
        trail_activate: Optional[float] = None,
        trail_pct: Optional[float] = None,
    ) -> None:
        key = self.make_key(symbol, strategy_id)
        trail_en = None if trail_enabled is None else (1 if trail_enabled else 0)
        with self._lock:
            self._conn.execute(
                """INSERT INTO positions
                   (key,symbol,strategy_id,option_symbol,side,quantity,original_quantity,
                    entry_price,peak_price,trough_price,trims_done,order_id,status,opened_at,
                    signal_score,buy_reason,alert_time,tp_pct,sl_pct,trim_pct,trim_at,be_at,fix_trail_pct,onelot_exit,
                    exit_mode,exit_fill,trail_enabled,trail_activate,trail_pct)
                   VALUES (?,?,?,?,?,?,?,?,?,?,0,?, 'open', ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(key) DO UPDATE SET
                     option_symbol=excluded.option_symbol, side=excluded.side,
                     quantity=excluded.quantity, original_quantity=excluded.original_quantity,
                     entry_price=excluded.entry_price, peak_price=excluded.peak_price,
                     trough_price=excluded.trough_price, trims_done=0, order_id=excluded.order_id,
                     status='open', opened_at=excluded.opened_at, signal_score=excluded.signal_score,
                     buy_reason=excluded.buy_reason, alert_time=excluded.alert_time,
                     tp_pct=excluded.tp_pct, sl_pct=excluded.sl_pct, trim_pct=excluded.trim_pct,
                     trim_at=excluded.trim_at, be_at=excluded.be_at, fix_trail_pct=excluded.fix_trail_pct, onelot_exit=excluded.onelot_exit,
                     exit_mode=excluded.exit_mode, exit_fill=excluded.exit_fill,
                     trail_enabled=excluded.trail_enabled, trail_activate=excluded.trail_activate,
                     trail_pct=excluded.trail_pct, bracket_ids=''""",
                (key, symbol.upper(), strategy_id, option_symbol, side, quantity, quantity,
                 entry_price, entry_price, entry_price, order_id, time.time(),
                 signal_score, buy_reason, alert_time, tp_pct, sl_pct, trim_pct, trim_at, be_at, fix_trail_pct, (None if onelot_exit is None else (1 if onelot_exit else 0)),
                 exit_mode, exit_fill, trail_en, trail_activate, trail_pct),
            )
            self._conn.commit()

    def update_open(self, symbol: str, strategy_id: str, **fields) -> None:
        cols = {k: v for k, v in fields.items() if k in _UPDATABLE}
        if not cols:
            return
        key = self.make_key(symbol, strategy_id)
        assignments = ", ".join(f"{c}=?" for c in cols)
        with self._lock:
            self._conn.execute(
                f"UPDATE positions SET {assignments} WHERE key=?",
                (*cols.values(), key),
            )
            self._conn.commit()

    def get_open(self, symbol: str, strategy_id: str) -> Optional[dict]:
        key = self.make_key(symbol, strategy_id)
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM positions WHERE key=? AND status='open'", (key,)
            ).fetchone()
        return dict(row) if row else None

    def all_open(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM positions WHERE status='open'"
            ).fetchall()
        return [dict(r) for r in rows]

    def mark_closed(self, symbol: str, strategy_id: str) -> None:
        key = self.make_key(symbol, strategy_id)
        with self._lock:
            self._conn.execute(
                "UPDATE positions SET status='closed', quantity=0 WHERE key=?", (key,)
            )
            self._conn.commit()

    # ── meta (day baseline + halt flag) ───────────────────────────────────────
    def set_meta(self, k: str, v: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO meta (k,v) VALUES (?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                (k, v),
            )
            self._conn.commit()

    def get_meta(self, k: str) -> Optional[str]:
        with self._lock:
            row = self._conn.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return row["v"] if row else None

    # ── dashboard-configured baskets (symbol groups + their risk/sizing) ──────
    def get_baskets(self) -> list:
        v = self.get_meta("baskets")
        try:
            return json.loads(v) if v else []
        except (TypeError, ValueError):
            return []

    def set_baskets(self, baskets: list) -> None:
        self.set_meta("baskets", json.dumps(baskets))

    # ── dashboard-configured settings overrides (dotted-path -> value) ─────────
    def get_settings_overrides(self) -> dict:
        v = self.get_meta("settings_overrides")
        try:
            return json.loads(v) if v else {}
        except (TypeError, ValueError):
            return {}

    def set_settings_overrides(self, overrides: dict) -> None:
        self.set_meta("settings_overrides", json.dumps(overrides))

    # ── event log (signals + actions, for the dashboard) ──────────────────────
    def log_event(self, kind: str, action: str = "", symbol: str = "", strategy_id: str = "",
                  option_symbol: str = "", ok: bool = True, order_id: Optional[str] = None,
                  latency_ms: Optional[float] = None, detail: str = "") -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO events (ts,kind,action,symbol,strategy_id,option_symbol,ok,order_id,latency_ms,detail)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (time.time(), kind, action, symbol, strategy_id, option_symbol,
                 1 if ok else 0, order_id, latency_ms, detail[:500]),
            )
            # keep the table bounded
            self._conn.execute(
                "DELETE FROM events WHERE id < (SELECT MAX(id)-2000 FROM events)"
            )
            self._conn.commit()

    def recent_events(self, limit: int = 60) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    # ── closed-trade log ──────────────────────────────────────────────────────
    def record_trade(self, symbol: str, strategy_id: str, option_symbol: str, side: str,
                     quantity: int, entry_price: float, exit_price: float,
                     exit_reason: str, opened_at: Optional[float],
                     peak_price: Optional[float] = None, trough_price: Optional[float] = None,
                     *, account_value: Optional[float] = None,
                     stop_loss_pct: Optional[float] = None, take_profit_pct: Optional[float] = None,
                     exit_method: Optional[str] = None, trade_status: str = "closed",
                     retry_count: Optional[int] = None, commission: Optional[float] = None,
                     signal_score: Optional[float] = None, buy_reason: Optional[str] = None,
                     notes: Optional[str] = None, alert_time: Optional[float] = None,
                     submitted_time: Optional[float] = None, fill_time: Optional[float] = None) -> None:
        now = time.time()
        pnl = round((exit_price - entry_price) * quantity * 100, 2) if (entry_price and exit_price) else None
        pnl_pct = round((exit_price - entry_price) / entry_price, 4) if entry_price else None
        minutes = round((now - opened_at) / 60, 1) if opened_at else None
        pos_usd = round(entry_price * quantity * 100, 2) if entry_price else None
        max_profit_pct = round((peak_price - entry_price) / entry_price, 4) if (peak_price and entry_price) else None
        max_dd_pct = round((trough_price - entry_price) / entry_price, 4) if (trough_price and entry_price) else None
        net_pnl = round(pnl - (commission or 0), 2) if pnl is not None else None
        put_call = "CALL" if (side or "").lower().startswith("c") else "PUT"
        if not buy_reason:
            buy_reason = "BUY signal → CALL" if put_call == "CALL" else "SELL signal → PUT"
        with self._lock:
            self._conn.execute(
                """INSERT INTO trades (symbol,strategy_id,option_symbol,side,put_call,quantity,
                       entry_price,exit_price,pnl,pnl_pct,position_size_usd,minutes,max_profit_pct,
                       max_drawdown_pct,exit_reason,buy_reason,notes,opened_at,closed_at,alert_time,
                       submitted_time,fill_time,signal_score,account_value,stop_loss_pct,take_profit_pct,
                       retry_count,exit_method,trade_status,commission,net_pnl)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (symbol, strategy_id, option_symbol, side, put_call, quantity, entry_price, exit_price,
                 pnl, pnl_pct, pos_usd, minutes, max_profit_pct, max_dd_pct, exit_reason, buy_reason, notes,
                 opened_at, now, alert_time, submitted_time, fill_time, signal_score, account_value,
                 stop_loss_pct, take_profit_pct, retry_count, exit_method, trade_status, commission, net_pnl),
            )
            self._conn.commit()

    def losing_trades_today(self, since_ts: float, exclude_prefix: Optional[str] = None) -> int:
        q = "SELECT COUNT(*) AS c FROM trades WHERE closed_at >= ? AND pnl < 0"
        params: list = [since_ts]
        if exclude_prefix:   # don't let internal/test (loop_) losers trip the real kill-switch
            q += " AND (strategy_id IS NULL OR strategy_id NOT LIKE ?)"
            params.append(exclude_prefix + "%")
        with self._lock:
            row = self._conn.execute(q, params).fetchone()
        return int(row["c"] or 0)

    def losing_trades_today_for(self, since_ts: float, symbols: list,
                                exclude_prefix: Optional[str] = None) -> int:
        """Losing trades since since_ts whose symbol is in `symbols` — for a per-basket
        max-losing-trades gate. Empty `symbols` -> 0."""
        syms = [str(s).upper() for s in (symbols or []) if str(s).strip()]
        if not syms:
            return 0
        ph = ",".join("?" for _ in syms)
        q = f"SELECT COUNT(*) AS c FROM trades WHERE closed_at >= ? AND pnl < 0 AND UPPER(symbol) IN ({ph})"
        params: list = [since_ts, *syms]
        if exclude_prefix:
            q += " AND (strategy_id IS NULL OR strategy_id NOT LIKE ?)"
            params.append(exclude_prefix + "%")
        with self._lock:
            row = self._conn.execute(q, params).fetchone()
        return int(row["c"] or 0)

    def recent_trades(self, limit: int = 60) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def all_trades(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM trades ORDER BY id ASC").fetchall()
        return [dict(r) for r in rows]

    def trades_today_pnl(self, exclude_prefix: Optional[str] = None) -> float:
        cutoff = time.time() - 86400
        q = "SELECT COALESCE(SUM(pnl),0) AS s FROM trades WHERE closed_at >= ?"
        params: list = [cutoff]
        if exclude_prefix:
            q += " AND (strategy_id IS NULL OR strategy_id NOT LIKE ?)"
            params.append(exclude_prefix + "%")
        with self._lock:
            row = self._conn.execute(q, params).fetchone()
        return float(row["s"] or 0)
