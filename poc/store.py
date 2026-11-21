"""SQLite record of trades, comments, logs, errors, and a little state."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from poc.market import Bar, as_utc


_SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    run_id TEXT,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT,
    side TEXT,
    order_type TEXT,
    volume REAL,
    entry_price REAL,
    stop_loss REAL,
    take_profit REAL,
    position_id TEXT,
    order_id TEXT,
    account_id INTEGER,
    detail TEXT
);
CREATE TABLE IF NOT EXISTS comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    run_id TEXT,
    role TEXT NOT NULL,
    text TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    level TEXT NOT NULL,
    event TEXT NOT NULL,
    detail TEXT
);
CREATE TABLE IF NOT EXISTS errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    run_id TEXT,
    message TEXT NOT NULL,
    traceback TEXT
);
CREATE TABLE IF NOT EXISTS state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS telegram_subscribers (
    chat_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active'
);
CREATE TABLE IF NOT EXISTS bars (
    open_time TEXT PRIMARY KEY,
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    tick_count REAL,
    volume REAL
);
CREATE INDEX IF NOT EXISTS trades_created ON trades (created_at);
CREATE INDEX IF NOT EXISTS logs_created ON logs (created_at);
"""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _stamp(value: datetime) -> str:
    return as_utc(value).strftime("%Y-%m-%dT%H:%M:%SZ")


def _log_level(level: str | None) -> str | None:
    text = (level or "").strip().lower()
    if text in {"", "all"}:
        return None
    if text in {"warn", "warning", "warnings"}:
        return "warn"
    if text in {"info", "information"}:
        return "info"
    if text in {"error", "errors"}:
        return "error"
    return text


def _clip(value: object, limit: int) -> str | None:
    if value is None:
        return None
    text = str(value)
    if not text:
        return None
    return text[:limit]


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def save_bars(self, bars: Sequence[Bar]) -> None:
        if not bars:
            return
        rows = [
            (
                _stamp(bar.open_time),
                bar.open,
                bar.high,
                bar.low,
                bar.close,
                bar.tick_count,
                bar.volume,
            )
            for bar in bars
        ]
        with self._connect() as conn:
            conn.executemany(
                """
                INSERT INTO bars (open_time, open, high, low, close, tick_count, volume)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(open_time) DO UPDATE SET
                    open = excluded.open,
                    high = excluded.high,
                    low = excluded.low,
                    close = excluded.close,
                    tick_count = excluded.tick_count,
                    volume = excluded.volume
                """,
                rows,
            )
            conn.commit()

    def load_bars(self, since: datetime) -> list[Bar]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT open_time, open, high, low, close, tick_count, volume
                FROM bars
                WHERE open_time >= ?
                ORDER BY open_time
                """,
                (_stamp(since),),
            ).fetchall()
        found: list[Bar] = []
        for row in rows:
            found.append(
                Bar(
                    open_time=as_utc(datetime.fromisoformat(str(row["open_time"]).replace("Z", "+00:00"))),
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    tick_count=None if row["tick_count"] is None else float(row["tick_count"]),
                    volume=None if row["volume"] is None else float(row["volume"]),
                )
            )
        return found

    def prune_bars(self, before: datetime) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM bars WHERE open_time < ?", (_stamp(before),))
            conn.commit()

    def log(self, level: str, event: str, detail: object = None) -> None:
        payload = detail if isinstance(detail, str) or detail is None else json.dumps(detail, default=str)
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO logs (created_at, level, event, detail) VALUES (?, ?, ?, ?)",
                (_now(), level, event, _clip(payload, 8000)),
            )
            conn.commit()

    def error(self, message: str, traceback: str | None = None, run_id: str | None = None) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO errors (created_at, run_id, message, traceback) VALUES (?, ?, ?, ?)",
                (_now(), run_id, _clip(message, 2000) or "error", _clip(traceback, 8000)),
            )
            conn.commit()

    def comment(self, run_id: str, role: str, text: str) -> None:
        cleaned = (text or "").strip()
        if not cleaned:
            return
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO comments (created_at, run_id, role, text) VALUES (?, ?, ?, ?)",
                (_now(), run_id, role, cleaned[:8000]),
            )
            conn.commit()

    def trade(self, **fields: Any) -> None:
        columns = [
            "run_id",
            "kind",
            "status",
            "reason",
            "side",
            "order_type",
            "volume",
            "entry_price",
            "stop_loss",
            "take_profit",
            "position_id",
            "order_id",
            "account_id",
            "detail",
        ]
        detail = fields.get("detail")
        if detail is not None and not isinstance(detail, str):
            fields["detail"] = json.dumps(detail, default=str)
        values = [_clip(fields.get(name), 4000) if name in {"reason", "detail"} else fields.get(name) for name in columns]
        with self._connect() as conn:
            conn.execute(
                f"INSERT INTO trades (created_at, {', '.join(columns)}) VALUES (?, {', '.join('?' for _ in columns)})",
                [_now(), *values],
            )
            conn.commit()

    def get(self, key: str, default: str = "") -> str:
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
        if row is None:
            return default
        return str(row["value"])

    def telegram_chats(self) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT chat_id FROM telegram_subscribers WHERE status = 'active' ORDER BY created_at"
            ).fetchall()
        return [str(row["chat_id"]) for row in rows]

    def add_telegram_chat(self, chat_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO telegram_subscribers (chat_id, created_at, status) VALUES (?, ?, 'active') "
                "ON CONFLICT(chat_id) DO UPDATE SET status = 'active'",
                (str(chat_id), _now()),
            )
            conn.commit()

    def remove_telegram_chat(self, chat_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE telegram_subscribers SET status = 'stopped' WHERE chat_id = ?",
                (str(chat_id),),
            )
            conn.commit()

    def set(self, key: str, value: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO state (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            conn.commit()

    def alerts(self) -> list[dict[str, Any]]:
        raw = self.get("alerts", "[]")
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return []
        if not isinstance(parsed, list):
            return []
        out = []
        for item in parsed:
            if isinstance(item, dict) and item.get("price") is not None:
                out.append({"price": float(item["price"]), "type": str(item.get("type") or "custom")})
        return out

    def set_alerts(self, levels: list[dict[str, Any]]) -> None:
        self.set("alerts", json.dumps(levels))

    def pending(self) -> dict[str, Any] | None:
        raw = self.get("pending", "")
        if not raw:
            return None
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None

    def set_pending(self, row: dict[str, Any] | None) -> None:
        self.set("pending", "" if row is None else json.dumps(row))

    def open_ids(self) -> list[str]:
        try:
            parsed = json.loads(self.get("open_ids", "[]"))
        except json.JSONDecodeError:
            return []
        if not isinstance(parsed, list):
            return []
        return [str(item) for item in parsed]

    def set_open_ids(self, ids: list[str]) -> None:
        self.set("open_ids", json.dumps(sorted(set(ids))))

    def latest_fill(self, position_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM trades WHERE position_id = ? AND kind = 'fill' ORDER BY id DESC LIMIT 1",
                (position_id,),
            ).fetchone()
        return dict(row) if row else None

    def closed_trades(self, limit: int = 8) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM trades WHERE kind = 'close' ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def recent_events(self, level: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        """Newest log rows. Warnings are stored as warn. The errors table is included with error."""
        wanted = _log_level(level)
        capped = max(1, min(int(limit), 1000))
        with self._connect() as conn:
            if wanted is None:
                logs = conn.execute(
                    "SELECT created_at, level, event, detail FROM logs ORDER BY id DESC LIMIT ?",
                    (capped,),
                ).fetchall()
                failures = conn.execute(
                    "SELECT created_at, message, traceback FROM errors ORDER BY id DESC LIMIT ?",
                    (capped,),
                ).fetchall()
            elif wanted == "error":
                logs = conn.execute(
                    "SELECT created_at, level, event, detail FROM logs WHERE level = 'error' ORDER BY id DESC LIMIT ?",
                    (capped,),
                ).fetchall()
                failures = conn.execute(
                    "SELECT created_at, message, traceback FROM errors ORDER BY id DESC LIMIT ?",
                    (capped,),
                ).fetchall()
            else:
                logs = conn.execute(
                    "SELECT created_at, level, event, detail FROM logs WHERE level = ? ORDER BY id DESC LIMIT ?",
                    (wanted, capped),
                ).fetchall()
                failures = []
        rows = [
            {
                "created_at": str(row["created_at"]),
                "level": str(row["level"]),
                "event": str(row["event"]),
                "detail": "" if row["detail"] is None else str(row["detail"]),
            }
            for row in logs
        ]
        for row in failures:
            detail = str(row["message"] or "")
            trace = row["traceback"]
            if trace:
                detail = f"{detail}\n{trace}"
            rows.append(
                {
                    "created_at": str(row["created_at"]),
                    "level": "error",
                    "event": "error",
                    "detail": detail,
                }
            )
        rows.sort(key=lambda item: item["created_at"], reverse=True)
        return rows[:capped]

    def tables(self) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name").fetchall()
        return [str(row["name"]) for row in rows]

    def count(self, table: str) -> int:
        with self._connect() as conn:
            row = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
        return int(row["n"])
