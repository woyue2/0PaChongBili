"""快手搜索试验的数据存储与分析工具。"""

import csv
import os
import sqlite3
import threading
from datetime import datetime

from src.common import paths


DB_FILE = paths.KUAISHOU_DB
COOKIE_FILE = paths.KUAISHOU_COOKIE


def build_kuaishou_page_url(video_id):
    return (
        f"https://www.kuaishou.com/short-video/{video_id}"
        if video_id
        else ""
    )


class Database:
    """保存作品实体、关键词关系和指标历史。

    task_videos 是刻意保留的多对多关系。同一作品出现在不同关键词下时，
    不会覆盖之前的关键词归属。
    """

    def __init__(self, db_file=DB_FILE):
        self._db_file = db_file
        self._local = threading.local()
        parent = os.path.dirname(os.path.abspath(db_file))
        os.makedirs(parent, exist_ok=True)
        self.create_tables()

    @property
    def conn(self):
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._local.conn = sqlite3.connect(self._db_file)
            self._local.conn.row_factory = sqlite3.Row
            self._local.conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn.execute("PRAGMA busy_timeout=5000")
            self._local.conn.execute("PRAGMA foreign_keys=ON")
        return self._local.conn

    def create_tables(self):
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS spider_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                keyword TEXT NOT NULL,
                pages INTEGER NOT NULL,
                order_by TEXT NOT NULL DEFAULT 'general',
                status TEXT NOT NULL DEFAULT 'pending',
                total_videos INTEGER NOT NULL DEFAULT 0,
                success_count INTEGER NOT NULL DEFAULT 0,
                fail_count INTEGER NOT NULL DEFAULT 0,
                error_msg TEXT,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                completed_at DATETIME
            );

            CREATE TABLE IF NOT EXISTS ks_videos (
                video_id TEXT PRIMARY KEY,
                title TEXT,
                description TEXT,
                page_url TEXT,
                cover_url TEXT,
                video_url TEXT,
                video_type TEXT,
                play_count INTEGER NOT NULL DEFAULT 0,
                liked_count INTEGER NOT NULL DEFAULT 0,
                comment_count INTEGER NOT NULL DEFAULT 0,
                share_count INTEGER NOT NULL DEFAULT 0,
                author_id TEXT,
                nickname TEXT,
                pub_time INTEGER NOT NULL DEFAULT 0,
                video_age_hours REAL NOT NULL DEFAULT 0,
                play_velocity REAL NOT NULL DEFAULT 0,
                engagement_rate REAL NOT NULL DEFAULT 0,
                fetched_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                first_seen_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS task_videos (
                task_id INTEGER NOT NULL,
                video_id TEXT NOT NULL,
                search_rank INTEGER,
                search_session_id TEXT,
                discovered_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (task_id, video_id),
                FOREIGN KEY (task_id) REFERENCES spider_tasks(id),
                FOREIGN KEY (video_id) REFERENCES ks_videos(video_id)
            );

            CREATE TABLE IF NOT EXISTS video_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                video_id TEXT NOT NULL,
                task_id INTEGER,
                play_count INTEGER NOT NULL DEFAULT 0,
                liked_count INTEGER NOT NULL DEFAULT 0,
                comment_count INTEGER NOT NULL DEFAULT 0,
                share_count INTEGER NOT NULL DEFAULT 0,
                record_time DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (video_id) REFERENCES ks_videos(video_id),
                FOREIGN KEY (task_id) REFERENCES spider_tasks(id)
            );

            CREATE TABLE IF NOT EXISTS failed_videos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                video_id TEXT,
                error_type TEXT,
                error_msg TEXT,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (task_id) REFERENCES spider_tasks(id)
            );

            CREATE INDEX IF NOT EXISTS idx_task_videos_video
                ON task_videos(video_id);
            CREATE INDEX IF NOT EXISTS idx_history_video_time
                ON video_history(video_id, record_time);
            """
        )
        video_columns = {row[1] for row in self.conn.execute("PRAGMA table_info(ks_videos)")}
        if "first_seen_at" not in video_columns:
            self.conn.execute("ALTER TABLE ks_videos ADD COLUMN first_seen_at DATETIME")
        self.conn.execute("""
            UPDATE ks_videos SET first_seen_at = COALESCE(
                (SELECT MIN(record_time) FROM video_history h WHERE h.video_id = ks_videos.video_id), fetched_at
            ) WHERE first_seen_at IS NULL
        """)
        self.conn.commit()

    def create_task(self, keyword, pages, order_by="general"):
        cursor = self.conn.execute(
            """
            INSERT INTO spider_tasks (keyword, pages, order_by, status)
            VALUES (?, ?, ?, 'running')
            """,
            (keyword, pages, order_by),
        )
        self.conn.commit()
        return cursor.lastrowid

    def update_task_status(self, task_id, status, **values):
        allowed = {
            "total_videos",
            "success_count",
            "fail_count",
            "error_msg",
        }
        updates = [(key, value) for key, value in values.items() if key in allowed]
        fields = [f"{key} = ?" for key, _ in updates]
        params = [value for _, value in updates]
        fields.append("status = ?")
        params.append(status)
        if status in {"completed", "failed"}:
            fields.append("completed_at = ?")
            params.append(datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3])
        params.append(task_id)
        self.conn.execute(
            f"UPDATE spider_tasks SET {', '.join(fields)} WHERE id = ?",
            params,
        )
        self.conn.commit()

    def upsert_video(self, task_id, video, search_rank=None, search_session_id=""):
        video_id = str(video.get("video_id") or "")
        if not video_id:
            return None
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        values = (
            video_id,
            video.get("title", ""),
            video.get("description", ""),
            video.get("page_url") or build_kuaishou_page_url(video_id),
            video.get("cover_url", ""),
            video.get("video_url", ""),
            str(video.get("video_type", "")),
            int(video.get("play_count", 0) or 0),
            int(video.get("liked_count", 0) or 0),
            int(video.get("comment_count", 0) or 0),
            int(video.get("share_count", 0) or 0),
            str(video.get("author_id", "") or ""),
            video.get("nickname", ""),
            int(video.get("pub_time", 0) or 0),
            float(video.get("video_age_hours", 0) or 0),
            float(video.get("play_velocity", 0) or 0),
            float(video.get("engagement_rate", 0) or 0),
            now,
        )
        self.conn.execute(
            """
            INSERT INTO ks_videos (
                video_id, title, description, page_url, cover_url, video_url,
                video_type, play_count, liked_count, comment_count, share_count,
                author_id, nickname, pub_time, video_age_hours, play_velocity,
                engagement_rate, fetched_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(video_id) DO UPDATE SET
                title = excluded.title,
                description = excluded.description,
                page_url = excluded.page_url,
                cover_url = excluded.cover_url,
                video_url = excluded.video_url,
                video_type = excluded.video_type,
                play_count = excluded.play_count,
                liked_count = excluded.liked_count,
                comment_count = excluded.comment_count,
                share_count = excluded.share_count,
                author_id = excluded.author_id,
                nickname = excluded.nickname,
                pub_time = excluded.pub_time,
                video_age_hours = excluded.video_age_hours,
                play_velocity = excluded.play_velocity,
                engagement_rate = excluded.engagement_rate,
                fetched_at = excluded.fetched_at
            """,
            values,
        )
        self.conn.execute(
            """
            INSERT INTO task_videos (
                task_id, video_id, search_rank, search_session_id, discovered_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(task_id, video_id) DO UPDATE SET
                search_rank = COALESCE(excluded.search_rank, task_videos.search_rank),
                search_session_id = COALESCE(
                    NULLIF(excluded.search_session_id, ''),
                    task_videos.search_session_id
                )
            """,
            (task_id, video_id, search_rank, search_session_id, now),
        )
        self.conn.execute(
            """
            INSERT INTO video_history (
                video_id, task_id, play_count, liked_count,
                comment_count, share_count, record_time
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                video_id,
                task_id,
                int(video.get("play_count", 0) or 0),
                int(video.get("liked_count", 0) or 0),
                int(video.get("comment_count", 0) or 0),
                int(video.get("share_count", 0) or 0),
                now,
            ),
        )
        self.conn.commit()
        return video_id

    def add_failed_video(self, task_id, video_id, error_type, error_msg):
        self.conn.execute(
            """
            INSERT INTO failed_videos (
                task_id, video_id, error_type, error_msg
            ) VALUES (?, ?, ?, ?)
            """,
            (task_id, video_id, error_type, error_msg),
        )
        self.conn.commit()

    def get_keyword_momentum_ranking(self, keyword, limit=50):
        rows = self.conn.execute(
            """
            SELECT v.*
            FROM ks_videos v
            WHERE EXISTS (
                SELECT 1 FROM task_videos tv JOIN spider_tasks t ON t.id = tv.task_id
                WHERE tv.video_id = v.video_id AND t.keyword = ?
            )
            ORDER BY v.play_velocity DESC, v.play_count DESC
            """,
            (keyword,),
        ).fetchall()
        if not rows:
            return []

        max_velocity = max(float(row["play_velocity"] or 0) for row in rows) or 1
        max_plays = max(int(row["play_count"] or 0) for row in rows) or 1
        max_engagement = max(float(row["engagement_rate"] or 0) for row in rows) or 1
        results = []
        for row in rows:
            item = dict(row)
            age_hours = float(item["video_age_hours"] or 0)
            if age_hours <= 24:
                freshness = 1.0
            elif age_hours <= 72:
                freshness = 0.8
            elif age_hours <= 168:
                freshness = 0.6
            elif age_hours <= 720:
                freshness = 0.4
            else:
                freshness = 0.2
            score = (
                min(float(item["play_velocity"] or 0) / max_velocity, 1) * 0.40
                + min(int(item["play_count"] or 0) / max_plays, 1) * 0.25
                + min(float(item["engagement_rate"] or 0) / max_engagement, 1) * 0.20
                + freshness * 0.15
            )
            item["velocity_score"] = round(min(float(item["play_velocity"] or 0) / max_velocity, 1), 4)
            item["play_score"] = round(min(int(item["play_count"] or 0) / max_plays, 1), 4)
            item["engagement_score"] = round(min(float(item["engagement_rate"] or 0) / max_engagement, 1), 4)
            item["freshness_score"] = freshness
            item["momentum_score"] = round(score, 4)
            results.append(item)
        results.sort(key=lambda item: item["momentum_score"], reverse=True)
        return results[:limit]

    def export_momentum_csv(self, keyword, csv_file, results=None):
        results = results or self.get_keyword_momentum_ranking(keyword)
        parent = os.path.dirname(os.path.abspath(csv_file))
        os.makedirs(parent, exist_ok=True)
        with open(csv_file, "w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(
                [
                    "排名",
                    "作品ID",
                    "作品链接",
                    "标题",
                    "作者",
                    "播放数",
                    "点赞数",
                    "评论数",
                    "发布时长(小时)",
                    "播放速度/小时",
                    "点赞播放比",
                    "动量评分",
                ]
            )
            for rank, item in enumerate(results, 1):
                writer.writerow(
                    [
                        rank,
                        item["video_id"],
                        item["page_url"],
                        item["title"],
                        item["nickname"],
                        item["play_count"],
                        item["liked_count"],
                        item["comment_count"],
                        round(float(item["video_age_hours"] or 0), 1),
                        round(float(item["play_velocity"] or 0), 2),
                        round(float(item["engagement_rate"] or 0), 6),
                        item["momentum_score"],
                    ]
                )
        return csv_file
