"""SQLite record of trades, comments, logs, errors, and a little state."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


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
CREATE INDEX IF NOT EXISTS trades_created ON trades (created_at);
CREATE INDEX IF NOT EXISTS logs_created ON logs (created_at);
"""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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

    def tables(self) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name").fetchall()
        return [str(row["name"]) for row in rows]

    def count(self, table: str) -> int:
        with self._connect() as conn:
            row = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
        return int(row["n"])
