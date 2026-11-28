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
    "media_filter": "",
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
        if "media_filter" not in existing_columns:
            db.execute(
                "ALTER TABLE transfer_preferences ADD COLUMN media_filter TEXT NOT NULL DEFAULT ''"
            )
        db.execute(
            """CREATE TABLE IF NOT EXISTS transfer_stats (
                owner_id INTEGER PRIMARY KEY,
                total_files INTEGER NOT NULL DEFAULT 0,
                total_bytes INTEGER NOT NULL DEFAULT 0,
                failed_items INTEGER NOT NULL DEFAULT 0,
                total_batches INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            )"""
        )
        db.execute(
            """CREATE TABLE IF NOT EXISTS batch_checkpoints (
                owner_id INTEGER PRIMARY KEY,
                status TEXT NOT NULL DEFAULT 'active',
                specs_json TEXT NOT NULL DEFAULT '[]',
                last_range_index INTEGER NOT NULL DEFAULT 0,
                last_message_id INTEGER NOT NULL DEFAULT 0,
                success_count INTEGER NOT NULL DEFAULT 0,
                total_count INTEGER NOT NULL DEFAULT 0,
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
    if key == "media_filter":
        tokens = [t.strip().lower() for t in str(value or "").split(",") if t.strip()]
        value = ",".join(dict.fromkeys(tokens))
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


def record_transfer_stats(
    owner_id: int,
    files: int = 0,
    bytes_transferred: int = 0,
    failed: int = 0,
    batches: int = 0,
) -> None:
    init_storage()
    now = datetime.now(timezone.utc).isoformat()
    with _database() as db:
        db.execute(
            """INSERT INTO transfer_stats
               (owner_id, total_files, total_bytes, failed_items, total_batches, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(owner_id) DO UPDATE SET
                 total_files = total_files + excluded.total_files,
                 total_bytes = total_bytes + excluded.total_bytes,
                 failed_items = failed_items + excluded.failed_items,
                 total_batches = total_batches + excluded.total_batches,
                 updated_at = excluded.updated_at""",
            (int(owner_id), int(files), int(bytes_transferred), int(failed), int(batches), now),
        )


def get_transfer_stats(owner_id: int) -> dict:
    init_storage()
    with _database() as db:
        row = db.execute(
            "SELECT * FROM transfer_stats WHERE owner_id = ?", (int(owner_id),)
        ).fetchone()
    if not row:
        return {
            "total_files": 0,
            "total_bytes": 0,
            "failed_items": 0,
            "total_batches": 0,
            "updated_at": None,
        }
    return dict(row)


def save_batch_checkpoint(
    owner_id: int,
    *,
    status: str,
    specs: list[dict],
    last_range_index: int,
    last_message_id: int,
    success_count: int,
    total_count: int,
) -> None:
    init_storage()
    now = datetime.now(timezone.utc).isoformat()
    with _database() as db:
        db.execute(
            """INSERT INTO batch_checkpoints
               (owner_id, status, specs_json, last_range_index, last_message_id,
                success_count, total_count, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(owner_id) DO UPDATE SET
                 status = excluded.status,
                 specs_json = excluded.specs_json,
                 last_range_index = excluded.last_range_index,
                 last_message_id = excluded.last_message_id,
                 success_count = excluded.success_count,
                 total_count = excluded.total_count,
                 updated_at = excluded.updated_at""",
            (
                int(owner_id),
                str(status),
                json.dumps(specs, ensure_ascii=False),
                int(last_range_index),
                int(last_message_id),
                int(success_count),
                int(total_count),
                now,
            ),
        )


def get_batch_checkpoint(owner_id: int) -> Optional[dict]:
    init_storage()
    with _database() as db:
        row = db.execute(
            "SELECT * FROM batch_checkpoints WHERE owner_id = ?", (int(owner_id),)
        ).fetchone()
    if not row:
        return None
    data = dict(row)
    try:
        data["specs"] = json.loads(data.get("specs_json") or "[]")
    except (TypeError, ValueError):
        data["specs"] = []
    return data


def clear_batch_checkpoint(owner_id: int) -> bool:
    init_storage()
    with _database() as db:
        cur = db.execute("DELETE FROM batch_checkpoints WHERE owner_id = ?", (int(owner_id),))
        return cur.rowcount > 0
