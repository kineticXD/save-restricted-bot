"""Tiny SQLite settings store for a single-owner Telegram bot.

On Railway, mount a volume at /data and set DATA_DIR=/data to persist the
selected destination across deploys/restarts. Without a volume, the bot still
works; settings are just stored in the container filesystem.
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from contextlib import contextmanager
from typing import Optional

DATA_DIR = os.environ.get("DATA_DIR", "data")
DB_PATH = os.environ.get("DATABASE_PATH", os.path.join(DATA_DIR, "bot.sqlite3"))


def _connect():
    parent = os.path.dirname(os.path.abspath(DB_PATH))
    os.makedirs(parent, exist_ok=True)
    connection = sqlite3.connect(DB_PATH, timeout=15)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 15000")
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
