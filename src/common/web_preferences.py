"""Persistent Web-console display preferences, separate from crawler databases."""

import os
import sqlite3
from datetime import datetime

from src.common import paths


DB_FILE = paths.data("web_preferences.db")
MAX_NOTE_LENGTH = 10000


def _connect():
    os.makedirs(os.path.dirname(DB_FILE), exist_ok=True)
    conn = sqlite3.connect(DB_FILE)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS hidden_history_days (
            platform TEXT NOT NULL,
            video_id TEXT NOT NULL,
            record_date TEXT NOT NULL,
            hidden_at TEXT NOT NULL,
            PRIMARY KEY (platform, video_id, record_date)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS record_notes (
            platform TEXT NOT NULL,
            record_id TEXT NOT NULL,
            content TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (platform, record_id)
        )
    """)
    return conn


def list_hidden_days(platform, video_ids):
    ids = [str(value) for value in video_ids if value is not None]
    if not ids:
        return {}
    conn = _connect()
    try:
        placeholders = ",".join("?" for _ in ids)
        rows = conn.execute(
            f"SELECT video_id, record_date FROM hidden_history_days "
            f"WHERE platform = ? AND video_id IN ({placeholders})",
            [platform, *ids],
        ).fetchall()
        result = {video_id: [] for video_id in ids}
        for video_id, record_date in rows:
            result.setdefault(video_id, []).append(record_date)
        return result
    finally:
        conn.close()


def set_hidden(platform, video_id, record_date, hidden):
    conn = _connect()
    try:
        if hidden:
            conn.execute(
                "INSERT OR REPLACE INTO hidden_history_days "
                "(platform, video_id, record_date, hidden_at) VALUES (?, ?, ?, ?)",
                (platform, str(video_id), record_date, datetime.now().isoformat(timespec="seconds")),
            )
        else:
            conn.execute(
                "DELETE FROM hidden_history_days WHERE platform = ? AND video_id = ? AND record_date = ?",
                (platform, str(video_id), record_date),
            )
        conn.commit()
    finally:
        conn.close()


def list_record_notes(platform, record_ids):
    ids = [str(value) for value in record_ids if value is not None]
    if not ids:
        return {}
    conn = _connect()
    try:
        placeholders = ",".join("?" for _ in ids)
        rows = conn.execute(
            f"SELECT record_id, content, updated_at FROM record_notes "
            f"WHERE platform = ? AND record_id IN ({placeholders})",
            [platform, *ids],
        ).fetchall()
        return {
            str(record_id): {"content": content, "updated_at": updated_at}
            for record_id, content, updated_at in rows
        }
    finally:
        conn.close()


def save_record_note(platform, record_id, content):
    platform = str(platform or "").strip()
    record_id = str(record_id or "").strip()
    if not platform or not record_id:
        raise ValueError("platform/record_id 参数无效")
    if not isinstance(content, str):
        raise ValueError("content 必须是字符串")
    content = content.strip()
    if len(content) > MAX_NOTE_LENGTH:
        raise ValueError(f"笔记不能超过 {MAX_NOTE_LENGTH} 个字符")
    conn = _connect()
    try:
        now = datetime.now().isoformat(timespec="seconds")
        if not content:
            conn.execute(
                "DELETE FROM record_notes WHERE platform = ? AND record_id = ?",
                (platform, record_id),
            )
        else:
            conn.execute(
                """INSERT INTO record_notes
                   (platform, record_id, content, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(platform, record_id) DO UPDATE SET
                     content = excluded.content, updated_at = excluded.updated_at""",
                (platform, record_id, content, now, now),
            )
        conn.commit()
        return {"content": content, "updated_at": now}
    finally:
        conn.close()


def delete_record_note(platform, record_id):
    conn = _connect()
    try:
        conn.execute(
            "DELETE FROM record_notes WHERE platform = ? AND record_id = ?",
            (str(platform), str(record_id)),
        )
        conn.commit()
    finally:
        conn.close()
