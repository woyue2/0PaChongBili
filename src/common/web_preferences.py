"""Persistent Web-console display preferences, separate from crawler databases."""

import os
import sqlite3
from datetime import datetime

from src.common import paths


DB_FILE = paths.data("web_preferences.db")


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
