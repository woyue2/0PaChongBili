"""
douyin_util.py - 抖音爬虫公共工具模块
基于小红书 util.py 平移改造，字段映射为抖音对应概念。
"""

import sqlite3
import time
import random
import warnings
import os
import threading
from datetime import datetime

warnings.filterwarnings("ignore")

from src.common import paths as _paths
DB_FILE = _paths.DOUYIN_DB
COOKIE_FILE = _paths.DOUYIN_COOKIE

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0",
]


class CookieManager:
    def __init__(self, cookie_file):
        self.cookies = []
        self.lock = threading.Lock()
        self.load_cookies(cookie_file)

    def load_cookies(self, cookie_file):
        if os.path.exists(cookie_file):
            with open(cookie_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        self.cookies.append(line)
        print(f"[Cookie] 已从 {cookie_file} 加载 {len(self.cookies)} 个 Cookie")

    def get_cookie(self):
        with self.lock:
            if self.cookies:
                return random.choice(self.cookies)
            return None

    def get_cookie_dict(self, cookie_str=None):
        cookie_str = cookie_str or self.get_cookie()
        if not cookie_str:
            return {}
        d = {}
        for part in cookie_str.split(";"):
            part = part.strip()
            if "=" in part:
                k, v = part.split("=", 1)
                d[k.strip()] = v.strip()
        return d

    def get_headers(self, referer="https://www.douyin.com/"):
        return {
            "User-Agent": random.choice(USER_AGENTS),
            "Referer": referer,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }


class Database:
    def __init__(self, db_file):
        self._local = threading.local()
        self._db_file = db_file
        self.create_tables()

    @property
    def conn(self):
        if not hasattr(self._local, 'conn') or self._local.conn is None:
            self._local.conn = sqlite3.connect(self._db_file)
            self._local.conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn.execute("PRAGMA busy_timeout=5000")
        return self._local.conn

    def create_tables(self):
        cursor = self.conn.cursor()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS spider_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                keyword TEXT NOT NULL,
                pages INTEGER NOT NULL,
                order_by TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                total_notes INTEGER DEFAULT 0,
                success_count INTEGER DEFAULT 0,
                fail_count INTEGER DEFAULT 0,
                csv_file TEXT,
                error_msg TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                completed_at DATETIME
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS dy_notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                aweme_id TEXT UNIQUE NOT NULL,
                title TEXT,
                description TEXT,
                video_url TEXT,
                play_count INTEGER DEFAULT 0,
                liked_count INTEGER DEFAULT 0,
                comment_count INTEGER DEFAULT 0,
                share_count INTEGER DEFAULT 0,
                collected_count INTEGER DEFAULT 0,
                nickname TEXT,
                user_id TEXT,
                sec_uid TEXT,
                fans_count INTEGER DEFAULT 0,
                pub_time INTEGER DEFAULT 0,
                note_type TEXT,
                tags TEXT,
                category TEXT,
                note_age_hours REAL DEFAULT 0,
                interact_velocity REAL DEFAULT 0,
                engagement_score REAL DEFAULT 0,
                fetched_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (task_id) REFERENCES spider_tasks(id)
            )
        """)

        cursor.execute("CREATE INDEX IF NOT EXISTS idx_dy_notes_task_id ON dy_notes(task_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_dy_notes_aweme_id ON dy_notes(aweme_id)")

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS dy_users (
                user_id TEXT PRIMARY KEY,
                sec_uid TEXT,
                nickname TEXT,
                fans INTEGER DEFAULT 0,
                follows INTEGER DEFAULT 0,
                desc TEXT,
                fetched_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS failed_notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                aweme_id TEXT NOT NULL,
                error_type TEXT,
                error_msg TEXT,
                retry_count INTEGER DEFAULT 0,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (task_id) REFERENCES spider_tasks(id)
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_failed_task_id ON failed_notes(task_id)")

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS note_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                aweme_id TEXT NOT NULL,
                task_id INTEGER,
                play_count INTEGER,
                liked_count INTEGER,
                comment_count INTEGER,
                share_count INTEGER,
                record_time DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (task_id) REFERENCES spider_tasks(id)
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_history_aweme_id ON note_history(aweme_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_history_time ON note_history(record_time)")

        self.conn.commit()

    def create_task(self, keyword, pages, order_by):
        cursor = self.conn.cursor()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        cursor.execute("""
            INSERT INTO spider_tasks (keyword, pages, order_by, status, created_at)
            VALUES (?, ?, ?, 'running', ?)
        """, (keyword, pages, order_by, now))
        self.conn.commit()
        return cursor.lastrowid

    def update_task_status(self, task_id, status, **kwargs):
        cursor = self.conn.cursor()
        fields = []
        values = []
        for k, v in kwargs.items():
            fields.append(f"{k} = ?")
            values.append(v)
        fields.append("status = ?")
        values.append(status)
        if status == "completed":
            fields.append("completed_at = ?")
            values.append(datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3])
        values.append(task_id)
        cursor.execute(f"UPDATE spider_tasks SET {', '.join(fields)} WHERE id = ?", values)
        self.conn.commit()

    def upsert_note(self, task_id, note_data):
        cursor = self.conn.cursor()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        aweme_id = note_data.get("aweme_id", "")
        if not aweme_id:
            return None

        cursor.execute("SELECT id FROM dy_notes WHERE aweme_id = ?", (aweme_id,))
        row = cursor.fetchone()

        if row:
            cursor.execute("""
                UPDATE dy_notes SET
                    task_id = ?,
                    title = ?,
                    description = ?,
                    video_url = ?,
                    play_count = ?,
                    liked_count = ?,
                    comment_count = ?,
                    share_count = ?,
                    collected_count = ?,
                    nickname = ?,
                    user_id = ?,
                    sec_uid = ?,
                    fans_count = ?,
                    pub_time = ?,
                    note_type = ?,
                    tags = ?,
                    category = ?,
                    note_age_hours = ?,
                    interact_velocity = ?,
                    engagement_score = ?,
                    fetched_at = ?
                WHERE aweme_id = ?
            """, (
                task_id,
                note_data.get("title"),
                note_data.get("description"),
                note_data.get("video_url"),
                note_data.get("play_count", 0),
                note_data.get("liked_count", 0),
                note_data.get("comment_count", 0),
                note_data.get("share_count", 0),
                note_data.get("collected_count", 0),
                note_data.get("nickname"),
                note_data.get("user_id"),
                note_data.get("sec_uid"),
                note_data.get("fans_count", 0),
                note_data.get("pub_time"),
                note_data.get("note_type"),
                note_data.get("tags"),
                note_data.get("category"),
                note_data.get("note_age_hours", 0),
                note_data.get("interact_velocity", 0),
                note_data.get("engagement_score", 0),
                now,
                aweme_id,
            ))
            note_db_id = row[0]
        else:
            cursor.execute("""
                INSERT INTO dy_notes (
                    task_id, aweme_id, title, description, video_url,
                    play_count, liked_count, comment_count, share_count, collected_count,
                    nickname, user_id, sec_uid, fans_count, pub_time, note_type,
                    tags, category, note_age_hours, interact_velocity,
                    engagement_score, fetched_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                task_id, aweme_id,
                note_data.get("title"),
                note_data.get("description"),
                note_data.get("video_url"),
                note_data.get("play_count", 0),
                note_data.get("liked_count", 0),
                note_data.get("comment_count", 0),
                note_data.get("share_count", 0),
                note_data.get("collected_count", 0),
                note_data.get("nickname"),
                note_data.get("user_id"),
                note_data.get("sec_uid"),
                note_data.get("fans_count", 0),
                note_data.get("pub_time"),
                note_data.get("note_type"),
                note_data.get("tags"),
                note_data.get("category"),
                note_data.get("note_age_hours", 0),
                note_data.get("interact_velocity", 0),
                note_data.get("engagement_score", 0),
                now,
            ))
            note_db_id = cursor.lastrowid

        cursor.execute("""
            INSERT INTO note_history (
                aweme_id, task_id, play_count, liked_count, comment_count,
                share_count, record_time
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            aweme_id, task_id,
            note_data.get("play_count", 0),
            note_data.get("liked_count", 0),
            note_data.get("comment_count", 0),
            note_data.get("share_count", 0),
            now,
        ))

        self.conn.commit()
        return note_db_id

    def add_failed_note(self, task_id, aweme_id, error_type, error_msg):
        cursor = self.conn.cursor()
        cursor.execute("""
            INSERT INTO failed_notes (task_id, aweme_id, error_type, error_msg)
            VALUES (?, ?, ?, ?)
        """, (task_id, aweme_id, error_type, error_msg))
        self.conn.commit()

    def upsert_user(self, user_id, nickname, fans=0, follows=0, sec_uid="", desc=""):
        cursor = self.conn.cursor()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        cursor.execute("SELECT user_id FROM dy_users WHERE user_id = ?", (user_id,))
        if cursor.fetchone():
            cursor.execute("""
                UPDATE dy_users SET nickname = ?, fans = ?, follows = ?, sec_uid = ?, desc = ?, fetched_at = ?
                WHERE user_id = ?
            """, (nickname, fans, follows, sec_uid, desc, now, user_id))
        else:
            cursor.execute("""
                INSERT INTO dy_users (user_id, nickname, fans, follows, sec_uid, desc, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (user_id, nickname, fans, follows, sec_uid, desc, now))
        self.conn.commit()

    def get_user_fans(self, user_id):
        cursor = self.conn.cursor()
        cursor.execute("SELECT fans FROM dy_users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        return int(row[0]) if row and row[0] else 0

    def calculate_freshness_weight(self, note_age_days):
        if note_age_days is None or note_age_days <= 0:
            return 2.0
        elif note_age_days <= 1:
            return 1.8
        elif note_age_days <= 3:
            return 1.6
        elif note_age_days <= 7:
            return 1.4
        elif note_age_days <= 14:
            return 1.2
        elif note_age_days <= 30:
            return 1.1
        elif note_age_days <= 90:
            return 1.0
        else:
            return 0.8

    def get_keyword_momentum_ranking(self, keyword, limit=20):
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT n.aweme_id, n.title, n.play_count, n.pub_time, n.nickname, n.user_id, n.fans_count,
                   n.interact_velocity, n.note_age_hours, n.engagement_score,
                   n.comment_count, n.liked_count, n.share_count, n.collected_count, n.tags, n.video_url
            FROM dy_notes n
            JOIN spider_tasks t ON n.task_id = t.id
            WHERE t.keyword = ?
            ORDER BY n.play_count DESC
        """, (keyword,))
        notes = cursor.fetchall()

        if not notes:
            return []

        velocities = [float(n[7]) for n in notes if n[7] and float(n[7]) > 0]
        max_velocity = max(velocities) if velocities else 1

        engagements = []
        for n in notes:
            play = float(n[2] or 0)
            comment = float(n[10] or 0)
            if play > 0:
                engagements.append(comment / play)
        max_engagement = max(engagements) if engagements else 1

        plays = [float(n[2]) for n in notes if n[2]]
        max_play = max(plays) if plays else 1
        min_play = min(plays) if plays else 0

        results = []
        for note in notes:
            (aweme_id, title, play_count, pub_time, nickname, user_id, fans_count,
             interact_velocity, note_age_hours, engagement_raw,
             comment_count, liked_count, share_count, collected_count, tags_str, video_url) = note

            play_count = int(play_count or 0)
            interact_velocity = float(interact_velocity or 0)
            note_age_hours = float(note_age_hours or 0)
            comment_count_val = int(comment_count or 0)

            engagement_raw = comment_count_val / max(play_count, 1) if play_count > 0 else 0

            velocity_score = min(interact_velocity / max_velocity, 1.0) if max_velocity > 0 else 0
            engagement_score = min(engagement_raw / max_engagement, 1.0) if max_engagement > 0 else 0

            note_age_days = note_age_hours / 24 if note_age_hours > 0 else None
            freshness = self.calculate_freshness_weight(note_age_days)
            freshness_normalized = (freshness - 0.8) / (2.0 - 0.8)

            normalized_value = (play_count - min_play) / (max_play - min_play) if max_play > min_play else 0.5

            composite_score = (
                velocity_score * 0.35 +
                engagement_score * 0.30 +
                freshness_normalized * 0.20 +
                normalized_value * 0.15
            )

            historical_growth = None
            data_points = 1
            try:
                h_cursor = self.conn.cursor()
                h_cursor.execute("SELECT COUNT(*) FROM note_history WHERE aweme_id = ?", (aweme_id,))
                data_points = h_cursor.fetchone()[0] or 1
                if data_points >= 2:
                    h_cursor.execute("SELECT play_count FROM note_history WHERE aweme_id = ? ORDER BY record_time ASC", (aweme_id,))
                    history = [float(r[0]) for r in h_cursor.fetchall()]
                    if history and history[0] > 0:
                        historical_growth = round((history[-1] - history[0]) / history[0] * 100, 2)
            except Exception:
                pass

            note_info = {
                "aweme_id": aweme_id,
                "title": title,
                "current_value": play_count,
                "pub_time": pub_time,
                "nickname": nickname,
                "user_id": user_id,
                "interact_velocity": round(interact_velocity, 2),
                "engagement_score": round(engagement_raw, 4),
                "note_age_hours": round(note_age_hours, 1),
                "comment_count": comment_count or 0,
                "liked_count": liked_count or 0,
                "share_count": share_count or 0,
                "collected_count": collected_count or 0,
                "velocity_score": round(velocity_score, 4),
                "engagement_norm_score": round(engagement_score, 4),
                "freshness_normalized": round(freshness_normalized, 4),
                "normalized_value": round(normalized_value, 4),
                "composite_score": round(composite_score, 4),
                "data_points": data_points,
                "historical_growth_pct": historical_growth,
                "status": "snapshot",
                "status_desc": "快照分析",
                "total_growth_pct": historical_growth,
                "tags": tags_str or "",
                "video_url": video_url or "",
            }
            results.append(note_info)

        results.sort(key=lambda x: x["composite_score"], reverse=True)
        return results[:limit]

    def get_value_ranking(self, keyword, limit=50):
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT n.aweme_id, n.title, n.play_count, n.pub_time, n.nickname, n.user_id, n.fans_count,
                   n.note_age_hours, n.engagement_score, n.comment_count,
                   n.liked_count, n.collected_count, n.share_count, n.tags, n.video_url
            FROM dy_notes n
            JOIN spider_tasks t ON n.task_id = t.id
            WHERE t.keyword = ?
              AND n.liked_count > 0
            ORDER BY n.play_count DESC
        """, (keyword,))
        notes = cursor.fetchall()

        if not notes:
            return []

        collect_rates = []
        engagements = []
        share_rates = []
        comment_rates = []

        for n in notes:
            liked = int(n[10] or 0)
            collected = int(n[11] or 0)
            share = int(n[12] or 0)
            comment = int(n[9] or 0)
            play = int(n[2] or 0)

            if liked > 0:
                collect_rates.append(collected / liked)
                share_rates.append(share / liked)
                comment_rates.append(comment / liked)
            if play > 0:
                engagements.append(collected / play)

        max_collect = max(collect_rates) if collect_rates else 1
        max_engage = max(engagements) if engagements else 1
        max_share = max(share_rates) if share_rates else 1
        max_comment = max(comment_rates) if comment_rates else 1

        results = []
        for note in notes:
            (aweme_id, title, play_count, pub_time, nickname, user_id, fans_count,
             note_age_hours, engagement_raw, comment_count,
             liked_count, collected_count, share_count, tags_str, video_url) = note

            liked = int(liked_count or 0)
            collected = int(collected_count or 0)
            share = int(share_count or 0)
            comment = int(comment_count or 0)
            play = int(play_count or 0)

            collect_rate = collected / max(liked, 1) if liked > 0 else 0
            share_rate = share / max(liked, 1) if liked > 0 else 0
            comment_rate = comment / max(liked, 1) if liked > 0 else 0
            engagement = collected / max(play, 1) if play > 0 else 0

            collect_score = min(collect_rate / max_collect, 1.0) if max_collect > 0 else 0
            engage_score = min(engagement / max_engage, 1.0) if max_engage > 0 else 0
            share_score = min(share_rate / max_share, 1.0) if max_share > 0 else 0
            comment_score = min(comment_rate / max_comment, 1.0) if max_comment > 0 else 0

            value_score = (
                collect_score * 0.40 +
                engage_score * 0.30 +
                comment_score * 0.30
            )

            results.append({
                "aweme_id": aweme_id,
                "title": title,
                "play_count": play,
                "nickname": nickname,
                "user_id": user_id,
                "liked_count": liked,
                "collected_count": collected,
                "comment_count": comment,
                "share_count": share,
                "tags": tags_str or "",
                "collect_rate": round(collect_rate, 4),
                "engagement_score": round(engagement, 4),
                "comment_rate": round(comment_rate, 4),
                "collect_score": round(collect_score, 4),
                "engage_score": round(engage_score, 4),
                "comment_score": round(comment_score, 4),
                "value_score": round(value_score, 4),
                "note_age_hours": note_age_hours or 0,
                "pub_time": pub_time,
                "video_url": video_url or "",
            })

        results.sort(key=lambda x: x["value_score"], reverse=True)
        return results[:limit]

    def export_momentum_csv(self, keyword, csv_file, results):
        import csv
        with open(csv_file, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "排名", "aweme_id", "视频链接", "标题", "播放量", "作者", "作者UID",
                "播放速率(次/小时)", "视频年龄(小时)", "互动密度", "评论数", "标签",
                "速率得分", "互动得分", "新鲜度得分", "播放量得分",
                "历史增长%", "数据点数量", "发布时间", "综合评分"
            ])
            for i, item in enumerate(results, 1):
                writer.writerow([
                    i,
                    item["aweme_id"],
                    item.get("video_url", ""),
                    item["title"],
                    item.get("current_value", 0),
                    item.get("nickname", ""),
                    item.get("user_id", ""),
                    item.get("interact_velocity", 0),
                    item.get("note_age_hours", 0),
                    item.get("engagement_score", 0),
                    item.get("comment_count", 0),
                    item.get("tags", ""),
                    item.get("velocity_score", 0),
                    item.get("engagement_norm_score", 0),
                    item.get("freshness_normalized", 0),
                    item.get("normalized_value", 0),
                    item.get("historical_growth_pct", ""),
                    item.get("data_points", 1),
                    item.get("pub_time", ""),
                    item.get("composite_score", 0),
                ])
        print(f"[导出] 动量分析 CSV 已保存到 {csv_file} ({len(results)} 条)")

    def export_value_csv(self, keyword, csv_file, results):
        import csv
        with open(csv_file, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "排名", "aweme_id", "视频链接", "标题", "播放量", "作者", "作者UID",
                "点赞数", "收藏数", "评论数", "分享数",
                "标签", "收藏率", "互动密度", "评论率",
                "收藏得分", "互动得分", "评论得分",
                "视频年龄(小时)", "发布时间", "价值综合评分"
            ])
            for i, item in enumerate(results, 1):
                writer.writerow([
                    i,
                    item["aweme_id"],
                    item.get("video_url", ""),
                    item["title"],
                    item.get("play_count", 0),
                    item.get("nickname", ""),
                    item.get("user_id", ""),
                    item.get("liked_count", 0),
                    item.get("collected_count", 0),
                    item.get("comment_count", 0),
                    item.get("share_count", 0),
                    item.get("tags", ""),
                    item.get("collect_rate", 0),
                    item.get("engagement_score", 0),
                    item.get("comment_rate", 0),
                    item.get("collect_score", 0),
                    item.get("engage_score", 0),
                    item.get("comment_score", 0),
                    item.get("note_age_hours", 0),
                    item.get("pub_time", ""),
                    item.get("value_score", 0),
                ])
        print(f"[导出] 价值分析 CSV 已保存到 {csv_file} ({len(results)} 条)")
