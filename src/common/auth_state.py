"""跨平台登录状态记录。

这里只记录“最近一次人工登录/刷新”和“最近一次在线验证”的时间。
完整会话由各平台独立的 Playwright 持久化 profile 保存。
"""

import sqlite3
import os
from contextlib import closing
from datetime import datetime, timedelta

from src.common import paths


AUTH_STATE_DB = paths.data("auth_state.db")
LOGIN_MAX_AGE_DAYS = 3


class AuthStateStore:
    def __init__(self, db_file=AUTH_STATE_DB):
        self.db_file = db_file
        self._create_table()

    def _connect(self):
        conn = sqlite3.connect(self.db_file)
        conn.row_factory = sqlite3.Row
        return conn

    def _create_table(self):
        os.makedirs(os.path.dirname(self.db_file), exist_ok=True)
        with closing(self._connect()) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS platform_login_state (
                    platform TEXT PRIMARY KEY,
                    last_login_at TEXT,
                    last_verified_at TEXT,
                    updated_at TEXT NOT NULL
                )
            """)
            conn.commit()

    def get(self, platform):
        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT platform, last_login_at, last_verified_at, updated_at
                FROM platform_login_state
                WHERE platform = ?
                """,
                (platform,),
            ).fetchone()
        return dict(row) if row else None

    def is_login_due(self, platform, max_age_days=LOGIN_MAX_AGE_DAYS, now=None):
        state = self.get(platform)
        if not state or not state.get("last_login_at"):
            return True
        try:
            last_login = datetime.fromisoformat(state["last_login_at"])
        except (TypeError, ValueError):
            return True
        current = now or datetime.now()
        return current - last_login >= timedelta(days=max_age_days)

    def mark_login(self, platform, when=None):
        timestamp = (when or datetime.now()).isoformat(timespec="seconds")
        with closing(self._connect()) as conn:
            conn.execute(
                """
                INSERT INTO platform_login_state (
                    platform, last_login_at, last_verified_at, updated_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(platform) DO UPDATE SET
                    last_login_at = excluded.last_login_at,
                    last_verified_at = excluded.last_verified_at,
                    updated_at = excluded.updated_at
                """,
                (platform, timestamp, timestamp, timestamp),
            )
            conn.commit()

    def mark_verified(self, platform, when=None):
        timestamp = (when or datetime.now()).isoformat(timespec="seconds")
        with closing(self._connect()) as conn:
            conn.execute(
                """
                INSERT INTO platform_login_state (
                    platform, last_login_at, last_verified_at, updated_at
                ) VALUES (?, NULL, ?, ?)
                ON CONFLICT(platform) DO UPDATE SET
                    last_verified_at = excluded.last_verified_at,
                    updated_at = excluded.updated_at
                """,
                (platform, timestamp, timestamp),
            )
            conn.commit()
