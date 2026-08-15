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
    conn.execute("""
        CREATE TABLE IF NOT EXISTS hidden_records (
            platform TEXT NOT NULL,
            record_id TEXT NOT NULL,
            hidden_at TEXT NOT NULL,
            note TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (platform, record_id)
        )
    """)
    # 兼容早期版本（无 note 列）的库
    try:
        cols = conn.execute("PRAGMA table_info(hidden_records)").fetchall()
        if not any(row[1] == "note" for row in cols):
            conn.execute("ALTER TABLE hidden_records ADD COLUMN note TEXT NOT NULL DEFAULT ''")
    except sqlite3.Error:
        pass
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


def set_record_hidden(platform, record_id, hidden):
    """永久隐藏/恢复一条记录（按 platform+record_id）。

    隐藏后该记录在 Web 结果视图中不再出现，但数据库原数据保留，
    重新采集时走 ON CONFLICT UPDATE，不会清除隐藏标记。
    """
    platform = str(platform or "").strip()
    record_id = str(record_id or "").strip()
    if not platform or not record_id:
        raise ValueError("platform/record_id 参数无效")
    if platform not in ("bili", "xhs", "douyin", "kuaishou"):
        raise ValueError("未知平台")
    conn = _connect()
    try:
        if hidden:
            # ON CONFLICT 仅刷新 hidden_at，保留已存在的 note
            conn.execute(
                "INSERT INTO hidden_records (platform, record_id, hidden_at, note) "
                "VALUES (?, ?, ?, '') "
                "ON CONFLICT(platform, record_id) DO UPDATE SET hidden_at = excluded.hidden_at",
                (platform, record_id, datetime.now().isoformat(timespec="seconds")),
            )
        else:
            conn.execute(
                "DELETE FROM hidden_records WHERE platform = ? AND record_id = ?",
                (platform, record_id),
            )
        conn.commit()
    finally:
        conn.close()


def list_hidden_records(platform):
    """返回某平台全部被隐藏的 record_id 集合（str）。"""
    platform = str(platform or "").strip()
    if not platform:
        return set()
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT record_id FROM hidden_records WHERE platform = ?",
            (platform,),
        ).fetchall()
        return {str(r[0]) for r in rows}
    finally:
        conn.close()


def is_record_hidden(platform, record_id):
    platform = str(platform or "").strip()
    record_id = str(record_id or "").strip()
    if not platform or not record_id:
        return False
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT 1 FROM hidden_records WHERE platform = ? AND record_id = ? LIMIT 1",
            (platform, record_id),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def set_hidden_record_note(platform, record_id, note):
    """更新黑名单条目的备注（仅当该记录已在黑名单中时生效）。"""
    platform = str(platform or "").strip()
    record_id = str(record_id or "").strip()
    if not platform or not record_id:
        raise ValueError("platform/record_id 参数无效")
    note = (note or "").strip()
    if len(note) > MAX_NOTE_LENGTH:
        raise ValueError(f"备注不能超过 {MAX_NOTE_LENGTH} 个字符")
    conn = _connect()
    try:
        conn.execute(
            "UPDATE hidden_records SET note = ? WHERE platform = ? AND record_id = ?",
            (note, platform, record_id),
        )
        conn.commit()
    finally:
        conn.close()


def list_hidden_records_detail(platform):
    """返回某平台黑名单条目明细，按拉黑时间倒序：
    [{record_id, hidden_at, note}, ...]
    """
    platform = str(platform or "").strip()
    if not platform:
        return []
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT record_id, hidden_at, note FROM hidden_records "
            "WHERE platform = ? ORDER BY hidden_at DESC",
            (platform,),
        ).fetchall()
        return [
            {"record_id": str(r[0]), "hidden_at": r[1], "note": r[2] or ""}
            for r in rows
        ]
    finally:
        conn.close()
