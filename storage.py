"""SQLite storage for Waguri's destination and transfer preferences."""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from contextlib import contextmanager
from typing import Optional

DATA_DIR = os.environ.get("DATA_DIR", "data")
DB_PATH = os.environ.get("DATABASE_PATH", os.path.join(DATA_DIR, "bot.sqlite3"))

DEFAULT_PREFERENCES = {
    "caption_mode": "keep",       # keep | append | replace
    "caption_text": "",
    "thumb_path": "",
    "thumb_mode": "source",    # source | custom | none
    "auto_pin": 0,
    "replace_rules": [],
}


def _connect():
    parent = os.path.dirname(os.path.abspath(DB_PATH))
    os.makedirs(parent, exist_ok=True)
    connection = sqlite3.connect(DB_PATH, timeout=15)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 15000")
    connection.execute("PRAGMA journal_mode = WAL")
    return connection


@contextmanager
def _database():
    db = _connect()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def init_storage() -> None:
    with _database() as db:
        db.execute(
            """CREATE TABLE IF NOT EXISTS owner_settings (
                owner_id INTEGER PRIMARY KEY,
                target_chat_id INTEGER,
                target_thread_id INTEGER,
                target_title TEXT,
                target_username TEXT,
                updated_at TEXT NOT NULL
            )"""
        )
        db.execute(
            """CREATE TABLE IF NOT EXISTS transfer_preferences (
                owner_id INTEGER PRIMARY KEY,
                caption_mode TEXT NOT NULL DEFAULT 'keep',
                caption_text TEXT NOT NULL DEFAULT '',
                thumb_path TEXT NOT NULL DEFAULT '',
                thumb_mode TEXT NOT NULL DEFAULT 'source',
                auto_pin INTEGER NOT NULL DEFAULT 0,
                replace_rules TEXT NOT NULL DEFAULT '[]',
                updated_at TEXT NOT NULL
            )"""
        )
        # Add columns to an existing Waguri database without deleting preferences.
        existing_columns = {
            row[1] for row in db.execute("PRAGMA table_info(transfer_preferences)").fetchall()
        }
        if "thumb_mode" not in existing_columns:
            db.execute(
                "ALTER TABLE transfer_preferences ADD COLUMN thumb_mode TEXT NOT NULL DEFAULT 'source'"
            )


def set_target(
    owner_id: int,
    chat_id: int,
    thread_id: Optional[int] = None,
    title: str = "Telegram group",
    username: Optional[str] = None,
) -> None:
    init_storage()
    with _database() as db:
        db.execute(
            """INSERT INTO owner_settings
               (owner_id, target_chat_id, target_thread_id, target_title,
                target_username, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(owner_id) DO UPDATE SET
                 target_chat_id=excluded.target_chat_id,
                 target_thread_id=excluded.target_thread_id,
                 target_title=excluded.target_title,
                 target_username=excluded.target_username,
                 updated_at=excluded.updated_at""",
            (
                int(owner_id), int(chat_id), int(thread_id) if thread_id else None,
                title or "Telegram group", username,
                datetime.now(timezone.utc).isoformat(),
            ),
        )


def get_target(owner_id: int):
    init_storage()
    with _database() as db:
        row = db.execute(
            "SELECT * FROM owner_settings WHERE owner_id = ?", (int(owner_id),)
        ).fetchone()
    return dict(row) if row and row["target_chat_id"] is not None else None


def clear_target(owner_id: int) -> bool:
    init_storage()
    with _database() as db:
        cursor = db.execute(
            "DELETE FROM owner_settings WHERE owner_id = ?", (int(owner_id),)
        )
        return cursor.rowcount > 0


def get_preferences(owner_id: int) -> dict:
    """Return persistent transfer preferences, including safe defaults."""
    init_storage()
    result = dict(DEFAULT_PREFERENCES)
    with _database() as db:
        row = db.execute(
            "SELECT * FROM transfer_preferences WHERE owner_id = ?", (int(owner_id),)
        ).fetchone()
    if not row:
        return result
    data = dict(row)
    result.update({k: data[k] for k in DEFAULT_PREFERENCES if k in data})
    try:
        result["replace_rules"] = json.loads(result.get("replace_rules") or "[]")
    except (TypeError, ValueError):
        result["replace_rules"] = []
    result["auto_pin"] = int(result.get("auto_pin", 0) or 0)
    return result


def update_preference(owner_id: int, key: str, value) -> None:
    if key not in DEFAULT_PREFERENCES:
        raise ValueError(f"Unknown transfer preference: {key}")
    init_storage()
    if key == "caption_mode" and value not in {"keep", "append", "replace"}:
        raise ValueError("caption_mode must be keep, append or replace")
    if key == "thumb_mode" and value not in {"source", "custom", "none"}:
        raise ValueError("thumb_mode must be source, custom or none")
    if key == "auto_pin":
        value = 1 if bool(value) else 0
    if key == "replace_rules":
        value = json.dumps(value or [], ensure_ascii=False)
    if value is None:
        value = ""
    with _database() as db:
        db.execute(
            """INSERT INTO transfer_preferences (owner_id, updated_at)
               VALUES (?, ?)
               ON CONFLICT(owner_id) DO UPDATE SET updated_at=excluded.updated_at""",
            (int(owner_id), datetime.now(timezone.utc).isoformat()),
        )
        db.execute(
            f"UPDATE transfer_preferences SET {key} = ?, updated_at = ? WHERE owner_id = ?",
            (value, datetime.now(timezone.utc).isoformat(), int(owner_id)),
        )
