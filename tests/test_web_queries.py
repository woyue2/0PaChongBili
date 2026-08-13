import os
import sqlite3
import sys
import tempfile
import unittest


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import src.common.web_queries as wq


def _make_db(path, schema_key):
    """构造一个最小的测试库，返回 connection"""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE spider_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            keyword TEXT NOT NULL,
            pages INTEGER NOT NULL,
            order_by TEXT NOT NULL,
            status TEXT,
            total_videos INTEGER DEFAULT 0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            completed_at DATETIME
        );
    """)
    if schema_key == "bili":
        conn.executescript("""
            CREATE TABLE bili_videos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                av_id TEXT UNIQUE NOT NULL,
                title TEXT,
                url TEXT,
                play_nums INTEGER DEFAULT 0,
                danmakus INTEGER DEFAULT 0,
                favorites INTEGER DEFAULT 0,
                review INTEGER DEFAULT 0,
                coin INTEGER DEFAULT 0,
                share INTEGER DEFAULT 0,
                like_count INTEGER DEFAULT 0,
                uploader TEXT,
                uploader_uid TEXT,
                uploader_fans INTEGER DEFAULT 0,
                pubdate DATETIME,
                tags TEXT,
                video_age_hours REAL DEFAULT 0,
                play_velocity REAL DEFAULT 0,
                engagement_score REAL DEFAULT 0,
                fetched_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE video_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                av_id TEXT NOT NULL,
                task_id INTEGER,
                play_nums INTEGER,
                danmakus INTEGER DEFAULT 0,
                favorites INTEGER DEFAULT 0,
                review INTEGER DEFAULT 0,
                coin INTEGER DEFAULT 0,
                share INTEGER DEFAULT 0,
                like_count INTEGER DEFAULT 0,
                record_time DATETIME DEFAULT CURRENT_TIMESTAMP
            );
        """)
    elif schema_key == "xhs":
        conn.executescript("""
            CREATE TABLE xhs_notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                note_id TEXT UNIQUE NOT NULL,
                title TEXT,
                url TEXT,
                interact_count INTEGER DEFAULT 0,
                liked_count INTEGER DEFAULT 0,
                collected_count INTEGER DEFAULT 0,
                comment_count INTEGER DEFAULT 0,
                share_count INTEGER DEFAULT 0,
                nickname TEXT,
                user_id TEXT,
                fans_count INTEGER DEFAULT 0,
                pub_time DATETIME,
                tags TEXT,
                note_age_hours REAL DEFAULT 0,
                interact_velocity REAL DEFAULT 0,
                engagement_score REAL DEFAULT 0,
                fetched_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE task_notes (
                task_id INTEGER NOT NULL,
                note_id TEXT NOT NULL,
                search_rank INTEGER,
                discovered_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (task_id, note_id)
            );
            CREATE TABLE note_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                note_id TEXT NOT NULL,
                task_id INTEGER,
                interact_count INTEGER,
                liked_count INTEGER DEFAULT 0,
                collected_count INTEGER DEFAULT 0,
                comment_count INTEGER DEFAULT 0,
                share_count INTEGER DEFAULT 0,
                record_time DATETIME DEFAULT CURRENT_TIMESTAMP
            );
        """)
    else:  # douyin 结构（kuaishou 简化同构）
        conn.executescript("""
            CREATE TABLE dy_notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                aweme_id TEXT UNIQUE NOT NULL,
                title TEXT,
                video_url TEXT,
                play_count INTEGER DEFAULT 0,
                liked_count INTEGER DEFAULT 0,
                comment_count INTEGER DEFAULT 0,
                share_count INTEGER DEFAULT 0,
                collected_count INTEGER DEFAULT 0,
                nickname TEXT,
                user_id TEXT,
                fans_count INTEGER DEFAULT 0,
                pub_time INTEGER DEFAULT 0,
                tags TEXT,
                note_age_hours REAL DEFAULT 0,
                interact_velocity REAL DEFAULT 0,
                engagement_score REAL DEFAULT 0,
                fetched_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE note_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                aweme_id TEXT NOT NULL,
                task_id INTEGER,
                play_count INTEGER,
                liked_count INTEGER DEFAULT 0,
                comment_count INTEGER DEFAULT 0,
                share_count INTEGER DEFAULT 0,
                record_time DATETIME DEFAULT CURRENT_TIMESTAMP
            );
        """)
    return conn


class WebQueriesCompareTests(unittest.TestCase):
    """compare_snapshots 核心逻辑：新增/消失/增长"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.db")
        self.conn = _make_db(self.db_path, "bili")
        # 任务 A: 视频 v1(v2), v2(v2), v3(v3) — v3 之后消失
        self.conn.execute(
            "INSERT INTO spider_tasks (id, keyword, pages, order_by, status, created_at) "
            "VALUES (1, '服饰', 1, 'click', 'completed', '2026-07-24 10:00:00')"
        )
        self.conn.execute(
            "INSERT INTO spider_tasks (id, keyword, pages, order_by, status, created_at) "
            "VALUES (2, '服饰', 1, 'click', 'completed', '2026-07-25 10:00:00')"
        )
        self.conn.execute(
            "INSERT INTO spider_tasks (id, keyword, pages, order_by, status, created_at) "
            "VALUES (3, '穷人', 1, 'click', 'completed', '2026-07-25 11:00:00')"
        )  # 不同关键词，不应出现在对比里
        # 主表只保留"最新"数据（av_id UNIQUE，与真实 B站库行为一致）
        for vid, play in [("v1", 1500), ("v2", 2600), ("v4", 500)]:
            self.conn.execute(
                "INSERT INTO bili_videos (task_id, av_id, title, play_nums, uploader, url) "
                "VALUES (2, ?, ?, ?, 'UP主甲', 'http://b.com/')",
                (vid, "标题" + vid, play),
            )
        # 历史快照
        for vid, play in [("v1", 1000), ("v2", 2000), ("v3", 3000)]:
            self.conn.execute(
                "INSERT INTO video_history (av_id, task_id, play_nums, record_time) "
                "VALUES (?, 1, ?, '2026-07-24 10:30:00')",
                (vid, play),
            )
        for vid, play in [("v1", 1500), ("v2", 2600), ("v4", 500)]:
            self.conn.execute(
                "INSERT INTO video_history (av_id, task_id, play_nums, record_time) "
                "VALUES (?, 2, ?, '2026-07-25 10:30:00')",
                (vid, play),
            )
        self.conn.commit()

        # 备份原平台 DB 路径，指向测试库
        self._orig = wq._PLATFORM_DB["bili"]
        wq._PLATFORM_DB["bili"] = self.db_path

    def tearDown(self):
        wq._PLATFORM_DB["bili"] = self._orig
        self.conn.close()
        self.tmp.cleanup()

    def test_compare_detects_added_removed_changed(self):
        result = wq.compare_snapshots("bili", "服饰", 1, 2)
        self.assertIsNotNone(result)

        added_ids = {x["id"] for x in result["added"]}
        removed_ids = {x["id"] for x in result["removed"]}
        changed_ids = {x["id"] for x in result["changed"]}

        self.assertEqual(added_ids, {"v4"})
        self.assertEqual(removed_ids, {"v3"})
        self.assertEqual(changed_ids, {"v1", "v2"})

        # 增长计算: v1 1000→1500 = +50%
        v1 = next(x for x in result["changed"] if x["id"] == "v1")
        self.assertEqual(v1["metric_a"], 1000)
        self.assertEqual(v1["metric_b"], 1500)
        self.assertEqual(v1["delta"], 500)
        self.assertEqual(v1["growth_pct"], 50.0)

        v2 = next(x for x in result["changed"] if x["id"] == "v2")
        self.assertEqual(v2["growth_pct"], 30.0)

    def test_compare_rejects_wrong_keyword(self):
        result = wq.compare_snapshots("bili", "穷人", 1, 2)
        self.assertIsNone(result)

    def test_compare_missing_task_returns_none(self):
        result = wq.compare_snapshots("bili", "服饰", 1, 999)
        self.assertIsNone(result)

    def test_list_snapshots_filters_empty_tasks(self):
        # 任务 3 没有视频 → 不出现在服饰列表；任务 2 有 3 条
        snaps = wq.list_snapshots("bili", "服饰")
        ids = [s["task_id"] for s in snaps]
        self.assertEqual(ids, [2, 1])
        self.assertEqual(snaps[0]["video_count"], 3)

    def test_list_keywords_groups_and_counts(self):
        kws = wq.list_keywords("bili")
        by_kw = {k["keyword"]: k for k in kws}
        self.assertEqual(set(by_kw.keys()), {"服饰", "穷人"})
        self.assertEqual(by_kw["服饰"]["task_count"], 2)
        self.assertEqual(by_kw["服饰"]["done_count"], 2)
        self.assertEqual(by_kw["服饰"]["dates"], ["2026-07-24", "2026-07-25"])


class WebQueriesValueRankTests(unittest.TestCase):
    """B站价值评分复用逻辑"""

    def test_bili_value_rank_scores_and_sorts(self):
        rows = [
            ("a1", "视频A", 10000, "UP甲", "u1", 1000, 100, 50, 40, 5, 10, 20, 24, "2026-07-01", "tag1"),
            ("a2", "视频B", 5000, "UP乙", "u2", 500, 50, 10, 5, 1, 5, 10, 24, "2026-07-01", "tag2"),
        ]
        items = wq._bili_value_rank(rows)
        self.assertEqual(len(items), 2)
        # 深度互动比高的视频A 应排前
        self.assertEqual(items[0]["av_id"], "a1")
        self.assertGreater(items[0]["value_score"], items[1]["value_score"])


class WebQueriesRankingTests(unittest.TestCase):
    """get_ranking 规范化输出"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.db")
        self.conn = _make_db(self.db_path, "xhs")
        self.conn.execute(
            "INSERT INTO spider_tasks (id, keyword, pages, order_by, status, created_at) "
            "VALUES (1, '美食', 1, 'general', 'completed', '2026-07-25 10:00:00')"
        )
        self.conn.execute(
            "INSERT INTO xhs_notes (task_id, note_id, title, interact_count, nickname, user_id, url, tags) "
            "VALUES (1, 'n1', '笔记一', 888, '作者甲', 'u1', 'http://xhs.com/', '美食,探店')"
        )
        self.conn.execute(
            "INSERT INTO task_notes (task_id, note_id, search_rank) VALUES (1, 'n1', 1)"
        )
        self.conn.execute(
            "INSERT INTO note_history (note_id, task_id, interact_count, record_time) "
            "VALUES ('n1', 1, 888, '2026-07-25 10:30:00')"
        )
        self.conn.commit()
        self._orig = wq._PLATFORM_DB["xhs"]
        wq._PLATFORM_DB["xhs"] = self.db_path

    def tearDown(self):
        wq._PLATFORM_DB["xhs"] = self._orig
        self.conn.close()
        self.tmp.cleanup()

    def test_get_ranking_returns_normalized_items(self):
        items = wq.get_ranking("xhs", "美食", "momentum")
        self.assertEqual(len(items), 1)
        it = items[0]
        self.assertEqual(it["id"], "n1")
        self.assertEqual(it["title"], "笔记一")
        self.assertEqual(it["author"], "作者甲")
        self.assertEqual(it["metric_value"], 888)
        self.assertGreaterEqual(it["score"], 0)

    def test_kuaishou_value_unsupported(self):
        self.assertEqual(wq.get_ranking("kuaishou", "x", "value"), [])


if __name__ == "__main__":
    unittest.main()
