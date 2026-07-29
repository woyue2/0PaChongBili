import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "src", "xhs"))

from src.common.auth_state import AuthStateStore
from xhs_util import Database


class AuthStateStoreTests(unittest.TestCase):
    def test_login_becomes_due_at_three_days(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = AuthStateStore(os.path.join(temp_dir, "auth.db"))
            now = datetime(2026, 7, 29, 12, 0, 0)

            self.assertTrue(store.is_login_due("xhs", now=now))
            store.mark_login("xhs", now - timedelta(days=2, hours=23))
            self.assertFalse(store.is_login_due("xhs", now=now))
            self.assertTrue(
                store.is_login_due("xhs", now=now + timedelta(hours=1))
            )


class XhsDatabaseReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self.temp_dir.name, "xhs.db"))

    def tearDown(self):
        self.db.conn.close()
        self.db._local.conn = None
        self.temp_dir.cleanup()

    def test_empty_link_does_not_overwrite_successful_link(self):
        first_task = self.db.create_task("美食", 1, "general")
        self.db.link_task_note(
            first_task, "note-1", 1, "token-1", "标题"
        )
        self.db.upsert_note(
            first_task,
            {
                "note_id": "note-1",
                "title": "标题",
                "url": "https://xhslink.com/valid",
                "author_url": (
                    "https://www.xiaohongshu.com/user/profile/user-1"
                    "?xsec_token=token"
                ),
                "share_link_attempted": True,
                "liked_count": 10,
            },
        )

        second_task = self.db.create_task("旅行", 1, "general")
        self.db.link_task_note(
            second_task, "note-1", 1, "token-2", "标题"
        )
        self.db.upsert_note(
            second_task,
            {
                "note_id": "note-1",
                "title": "标题",
                "url": "",
                "share_link_attempted": True,
                "liked_count": 20,
            },
        )

        self.assertEqual(
            self.db.get_existing_share_link("note-1"),
            "https://xhslink.com/valid",
        )
        self.assertEqual(
            self.db.get_link_completion(first_task)["missing"], 0
        )
        self.assertEqual(
            self.db.get_link_completion(second_task)["missing"], 0
        )
        food_results = self.db.get_keyword_momentum_ranking("美食")
        self.assertEqual(len(food_results), 1)
        self.assertEqual(
            food_results[0]["author_url"],
            "https://www.xiaohongshu.com/user/profile/user-1",
        )
        self.assertEqual(
            len(self.db.get_keyword_momentum_ranking("旅行")), 1
        )

    def test_legacy_schema_is_migrated(self):
        legacy_path = os.path.join(self.temp_dir.name, "legacy.db")
        conn = sqlite3.connect(legacy_path)
        conn.executescript(
            """
            CREATE TABLE spider_tasks (
                id INTEGER PRIMARY KEY,
                keyword TEXT NOT NULL,
                pages INTEGER NOT NULL,
                order_by TEXT NOT NULL,
                status TEXT,
                created_at DATETIME,
                completed_at DATETIME
            );
            CREATE TABLE xhs_notes (
                id INTEGER PRIMARY KEY,
                task_id INTEGER NOT NULL,
                note_id TEXT UNIQUE NOT NULL,
                title TEXT,
                url TEXT,
                xsec_token TEXT,
                fetched_at DATETIME
            );
            INSERT INTO spider_tasks VALUES (
                1, '旧关键词', 1, 'general', 'completed',
                CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
            );
            INSERT INTO xhs_notes VALUES (
                1, 1, 'old-note', '旧标题',
                'https://xhslink.com/old', 'token', CURRENT_TIMESTAMP
            );
            """
        )
        conn.commit()
        conn.close()

        migrated = Database(legacy_path)
        self.assertEqual(
            migrated.get_existing_share_link("old-note"),
            "https://xhslink.com/old",
        )
        self.assertEqual(
            migrated.get_link_completion(1),
            {"total": 1, "completed": 1, "missing": 0},
        )
        migrated.conn.close()
        migrated._local.conn = None


if __name__ == "__main__":
    unittest.main()
