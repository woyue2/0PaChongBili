import os
import sys
import tempfile
import time
import unittest


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from src.kuaishou.kuaishou_spider import KuaishouSpider, _as_int
from src.kuaishou.kuaishou_util import Database


class KuaishouParserTests(unittest.TestCase):
    def test_extracts_mediacrawler_compatible_search_shape(self):
        now_ms = int(time.time() * 1000)
        body = {
            "data": {
                "visionSearchPhoto": {
                    "result": 1,
                    "searchSessionId": "session-1",
                    "feeds": [
                        {
                            "type": "video",
                            "author": {"id": "user-1", "name": "作者甲"},
                            "photo": {
                                "id": "video-1",
                                "caption": "深圳便宜美食",
                                "timestamp": now_ms - 3_600_000,
                                "realLikeCount": "1.2万",
                                "viewCount": "10万",
                                "commentCount": "321",
                                "coverUrl": "https://example.test/cover.jpg",
                                "photoUrl": "https://example.test/video.mp4",
                            },
                        }
                    ],
                }
            }
        }
        self.assertEqual(
            KuaishouSpider._extract_search_payload(body)["searchSessionId"],
            "session-1",
        )
        parsed = KuaishouSpider.parse_search_feeds(
            KuaishouSpider._extract_feeds(body)
        )
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["video_id"], "video-1")
        self.assertEqual(parsed[0]["play_count"], 100_000)
        self.assertEqual(parsed[0]["liked_count"], 12_000)
        self.assertEqual(parsed[0]["comment_count"], 321)
        self.assertAlmostEqual(parsed[0]["engagement_rate"], 0.12)

    def test_accepts_renamed_search_operation_with_stable_shape(self):
        body = {
            "data": {
                "newVisionSearchFeed": {
                    "result": 1,
                    "pcursor": "2",
                    "feeds": [{"photo": {"id": "video-2"}}],
                }
            }
        }
        payload = KuaishouSpider._extract_search_payload(body)
        self.assertEqual(payload["feeds"][0]["photo"]["id"], "video-2")

    def test_accepts_new_rest_search_feed_shape(self):
        body = {
            "result": 1,
            "pcursor": "no_more",
            "feeds": [{"photo": {"id": "video-rest"}}],
        }
        payload = KuaishouSpider._extract_search_payload(body)
        self.assertEqual(payload["feeds"][0]["photo"]["id"], "video-rest")

    def test_human_readable_number_parser(self):
        self.assertEqual(_as_int("2.5万"), 25_000)
        self.assertEqual(_as_int("1.2亿"), 120_000_000)
        self.assertEqual(_as_int("1,234"), 1_234)
        self.assertEqual(_as_int(None), 0)


class KuaishouDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self.temp_dir.name, "kuaishou.db"))
        self.video = {
            "video_id": "video-1",
            "title": "测试作品",
            "play_count": 10_000,
            "liked_count": 1_000,
            "comment_count": 20,
            "pub_time": int(time.time()) - 3600,
            "video_age_hours": 1,
            "play_velocity": 10_000,
            "engagement_rate": 0.1,
        }

    def tearDown(self):
        self.db.conn.close()
        self.db._local.conn = None
        self.temp_dir.cleanup()

    def test_same_video_keeps_multiple_keyword_relations_and_history(self):
        first_task = self.db.create_task("美食", 1)
        second_task = self.db.create_task("深圳便宜美食", 1)
        self.db.upsert_video(first_task, self.video, search_rank=1)
        updated = dict(self.video, play_count=11_000)
        self.db.upsert_video(second_task, updated, search_rank=3)

        relation_count = self.db.conn.execute(
            "SELECT COUNT(*) FROM task_videos WHERE video_id = 'video-1'"
        ).fetchone()[0]
        history_count = self.db.conn.execute(
            "SELECT COUNT(*) FROM video_history WHERE video_id = 'video-1'"
        ).fetchone()[0]
        current_plays = self.db.conn.execute(
            "SELECT play_count FROM ks_videos WHERE video_id = 'video-1'"
        ).fetchone()[0]

        self.assertEqual(relation_count, 2)
        self.assertEqual(history_count, 2)
        self.assertEqual(current_plays, 11_000)
        self.assertEqual(
            len(self.db.get_keyword_momentum_ranking("美食")), 1
        )
        self.assertEqual(
            len(self.db.get_keyword_momentum_ranking("深圳便宜美食")), 1
        )


if __name__ == "__main__":
    unittest.main()
