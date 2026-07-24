import requests
import sqlite3
import argparse
import time
import random
import warnings
import os
import threading
import hashlib
import functools
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urlencode, parse_qs, unquote

requests.packages.urllib3.disable_warnings()
warnings.filterwarnings("ignore")

DB_FILE = "bili_spider.db"
COOKIE_FILE = "bili_cookie.txt"

MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]

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
        self.full_cookies = []
        self.current_index = 0
        self.lock = threading.Lock()
        self.load_cookies(cookie_file)

    def load_cookies(self, cookie_file):
        if os.path.exists(cookie_file):
            with open(cookie_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        if "=" in line and "SESSDATA" in line:
                            self.full_cookies.append(line)
                        else:
                            self.cookies.append(line)
        total = len(self.cookies) + len(self.full_cookies)
        print(f"[Cookie] 已加载 {total} 个 Cookie (SESSDATA: {len(self.full_cookies)}, 完整Cookie: {len(self.cookies)})")

    def get_cookie(self):
        with self.lock:
            if self.full_cookies:
                cookie = self.full_cookies[self.current_index % len(self.full_cookies)]
                self.current_index += 1
                return cookie
            if self.cookies:
                cookie = self.cookies[self.current_index]
                self.current_index = (self.current_index + 1) % len(self.cookies)
                return cookie
            return None

    def get_headers(self, referer="https://www.bilibili.com/"):
        headers = {
            "User-Agent": random.choice(USER_AGENTS),
            "Referer": referer,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        cookie = self.get_cookie()
        if cookie:
            if "=" in cookie:
                headers["Cookie"] = cookie
            else:
                headers["Cookie"] = f"SESSDATA={cookie}"
        return headers


class Database:
    def __init__(self, db_file):
        self._local = threading.local()
        self._db_file = db_file
        self._init_conn()
        self.create_tables()

    @property
    def conn(self):
        if not hasattr(self._local, 'conn') or self._local.conn is None:
            self._local.conn = sqlite3.connect(self._db_file)
            self._local.conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn.execute("PRAGMA busy_timeout=5000")
        return self._local.conn

    def _init_conn(self):
        self._local.conn = sqlite3.connect(self._db_file)
        self._local.conn.execute("PRAGMA journal_mode=WAL")
        self._local.conn.execute("PRAGMA busy_timeout=5000")

    def create_tables(self):
        cursor = self.conn.cursor()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS spider_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                keyword TEXT NOT NULL,
                pages INTEGER NOT NULL,
                order_by TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                total_videos INTEGER DEFAULT 0,
                success_count INTEGER DEFAULT 0,
                fail_count INTEGER DEFAULT 0,
                csv_file TEXT,
                error_msg TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                completed_at DATETIME
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS bili_videos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                av_id TEXT UNIQUE NOT NULL,
                bvid TEXT,
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
                duration INTEGER DEFAULT 0,
                description TEXT,
                tags TEXT,
                category TEXT,
                fetched_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (task_id) REFERENCES spider_tasks(id)
            )
        """)

        cursor.execute("CREATE INDEX IF NOT EXISTS idx_videos_task_id ON bili_videos(task_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_videos_av_id ON bili_videos(av_id)")

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS bili_uploaders (
                uid TEXT PRIMARY KEY,
                name TEXT,
                fans INTEGER DEFAULT 0,
                level INTEGER DEFAULT 0,
                verified INTEGER DEFAULT 0,
                fetched_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS failed_videos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                av_id TEXT NOT NULL,
                error_type TEXT,
                error_msg TEXT,
                retry_count INTEGER DEFAULT 0,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (task_id) REFERENCES spider_tasks(id)
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_failed_task_id ON failed_videos(task_id)")

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS video_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                av_id TEXT NOT NULL,
                task_id INTEGER,
                play_nums INTEGER,
                danmakus INTEGER,
                favorites INTEGER,
                review INTEGER,
                coin INTEGER,
                share INTEGER,
                like_count INTEGER,
                record_time DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (task_id) REFERENCES spider_tasks(id)
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_history_av_id ON video_history(av_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_history_time ON video_history(record_time)")

        self._migrate_columns()
        self.conn.commit()

    def _migrate_columns(self):
        cursor = self.conn.cursor()
        cursor.execute("PRAGMA table_info(spider_tasks)")
        columns = [row[1] for row in cursor.fetchall()]
        
        if "csv_file" not in columns:
            cursor.execute("ALTER TABLE spider_tasks ADD COLUMN csv_file TEXT")
            print("[数据库] 已添加 csv_file 字段")

        cursor.execute("PRAGMA table_info(bili_videos)")
        video_columns = [row[1] for row in cursor.fetchall()]
        
        if "uploader_fans" not in video_columns:
            cursor.execute("ALTER TABLE bili_videos ADD COLUMN uploader_fans INTEGER DEFAULT 0")
            print("[数据库] 已添加 uploader_fans 字段")
        
        if "first_comment_time" not in video_columns:
            cursor.execute("ALTER TABLE bili_videos ADD COLUMN first_comment_time DATETIME")
            print("[数据库] 已添加 first_comment_time 字段")
        
        if "comment_count" not in video_columns:
            cursor.execute("ALTER TABLE bili_videos ADD COLUMN comment_count INTEGER DEFAULT 0")
            print("[数据库] 已添加 comment_count 字段")
        
        if "play_velocity" not in video_columns:
            cursor.execute("ALTER TABLE bili_videos ADD COLUMN play_velocity REAL DEFAULT 0")
            print("[数据库] 已添加 play_velocity 字段 (hourly_view_rate)")
        
        if "time_span_hours" not in video_columns:
            cursor.execute("ALTER TABLE bili_videos ADD COLUMN time_span_hours REAL DEFAULT 0")
            print("[数据库] 已添加 time_span_hours 字段")

        if "video_age_hours" not in video_columns:
            cursor.execute("ALTER TABLE bili_videos ADD COLUMN video_age_hours REAL DEFAULT 0")
            print("[数据库] 已添加 video_age_hours 字段")

        if "engagement_score" not in video_columns:
            cursor.execute("ALTER TABLE bili_videos ADD COLUMN engagement_score REAL DEFAULT 0")
            print("[数据库] 已添加 engagement_score 字段")

        # 回填旧数据的 video_age_hours 和 play_velocity
        if "video_age_hours" in video_columns:
            try:
                cursor.execute("""
                    UPDATE bili_videos 
                    SET video_age_hours = ROUND((julianday('now') - julianday(pubdate)) * 24, 2)
                    WHERE pubdate IS NOT NULL 
                      AND pubdate != '' 
                      AND (video_age_hours IS NULL OR video_age_hours = 0)
                """)
                backfilled = cursor.rowcount
                if backfilled > 0:
                    cursor.execute("""
                        UPDATE bili_videos 
                        SET play_velocity = ROUND(play_nums / video_age_hours, 2)
                        WHERE video_age_hours > 0 AND (play_velocity IS NULL OR play_velocity = 0)
                    """)
                    print(f"[数据库] 已回填 {backfilled} 条视频的 video_age_hours 和 play_velocity")
            except:
                pass

        # 回填旧数据的 engagement_score
        if "engagement_score" in video_columns:
            try:
                cursor.execute("""
                    UPDATE bili_videos 
                    SET engagement_score = ROUND(
                        CAST(COALESCE(like_count, 0) + COALESCE(coin, 0) + COALESCE(favorites, 0) 
                             + COALESCE(review, 0) + COALESCE(danmakus, 0) AS FLOAT) 
                        / MAX(play_nums, 1), 4)
                    WHERE (engagement_score IS NULL OR engagement_score = 0)
                      AND play_nums > 0
                """)
                if cursor.rowcount > 0:
                    print(f"[数据库] 已回填 {cursor.rowcount} 条视频的 engagement_score")
            except:
                pass

    def create_task(self, keyword, pages, order_by):
        cursor = self.conn.cursor()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        cursor.execute("""
            INSERT INTO spider_tasks (keyword, pages, order_by, status, created_at)
            VALUES (?, ?, ?, 'running', ?)
        """, (keyword, pages, order_by, now))
        self.conn.commit()
        return cursor.lastrowid

    def update_task(self, task_id, **kwargs):
        cursor = self.conn.cursor()
        updates = []
        params = []
        has_completed_at = False
        for key, value in kwargs.items():
            if key == "completed_at":
                has_completed_at = True
            updates.append(f"{key} = ?")
            params.append(value)
        if not has_completed_at and ("completed_at" in kwargs or "status" in kwargs):
            updates.append("completed_at = ?")
            params.append(datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3])
        params.append(task_id)
        cursor.execute(f"UPDATE spider_tasks SET {', '.join(updates)} WHERE id = ?", params)
        self.conn.commit()

    def insert_video(self, task_id, video_data):
        cursor = self.conn.cursor()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        cursor.execute("""
            INSERT INTO bili_videos 
            (task_id, av_id, bvid, title, url, play_nums, danmakus, favorites, 
             review, coin, share, like_count, uploader, uploader_uid, uploader_fans,
             pubdate, duration, description, tags, category, fetched_at,
             first_comment_time, comment_count, play_velocity, time_span_hours,
             video_age_hours, engagement_score)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(av_id) DO UPDATE SET
                task_id = excluded.task_id,
                bvid = excluded.bvid,
                title = excluded.title,
                url = excluded.url,
                play_nums = excluded.play_nums,
                danmakus = excluded.danmakus,
                favorites = excluded.favorites,
                review = excluded.review,
                coin = excluded.coin,
                share = excluded.share,
                like_count = excluded.like_count,
                uploader = excluded.uploader,
                uploader_uid = excluded.uploader_uid,
                uploader_fans = excluded.uploader_fans,
                pubdate = excluded.pubdate,
                duration = excluded.duration,
                description = excluded.description,
                tags = excluded.tags,
                category = excluded.category,
                fetched_at = excluded.fetched_at,
                first_comment_time = excluded.first_comment_time,
                comment_count = excluded.comment_count,
                play_velocity = excluded.play_velocity,
                time_span_hours = excluded.time_span_hours,
                video_age_hours = excluded.video_age_hours,
                engagement_score = excluded.engagement_score
        """, (
            task_id,
            video_data.get("av_id"),
            video_data.get("bvid"),
            video_data.get("title"),
            video_data.get("url"),
            video_data.get("play_nums", 0),
            video_data.get("danmakus", 0),
            video_data.get("favorites", 0),
            video_data.get("review", 0),
            video_data.get("coin", 0),
            video_data.get("share", 0),
            video_data.get("like_count", 0),
            video_data.get("uploader"),
            video_data.get("uploader_uid"),
            video_data.get("uploader_fans", 0),
            video_data.get("pubdate"),
            video_data.get("duration", 0),
            video_data.get("description"),
            video_data.get("tags"),
            video_data.get("category"),
            now,
            video_data.get("first_comment_time"),
            video_data.get("comment_count", 0),
            video_data.get("play_velocity", 0),
            video_data.get("time_span_hours", 0),
            video_data.get("video_age_hours", 0),
            video_data.get("engagement_score", 0),
        ))

        cursor.execute("""
            INSERT INTO video_history 
            (av_id, task_id, play_nums, danmakus, favorites, review, coin, share, like_count, record_time)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            video_data.get("av_id"),
            task_id,
            video_data.get("play_nums", 0),
            video_data.get("danmakus", 0),
            video_data.get("favorites", 0),
            video_data.get("review", 0),
            video_data.get("coin", 0),
            video_data.get("share", 0),
            video_data.get("like_count", 0),
            now,
        ))
        
        self._save_uploader(video_data)
        self.conn.commit()

    def _save_uploader(self, video_data):
        uid = video_data.get("uploader_uid")
        if not uid:
            return
        
        cursor = self.conn.cursor()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        cursor.execute("""
            INSERT INTO bili_uploaders 
            (uid, name, fans, level, verified, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(uid) DO UPDATE SET
                name = excluded.name,
                fans = excluded.fans,
                level = excluded.level,
                verified = excluded.verified,
                fetched_at = excluded.fetched_at
        """, (
            uid,
            video_data.get("uploader"),
            video_data.get("uploader_fans", 0),
            video_data.get("uploader_level", 0),
            video_data.get("uploader_verified", 0),
            now,
        ))

    def update_video_comments(self, av_id, first_comment_time, play_velocity, time_span_hours):
        cursor = self.conn.cursor()
        cursor.execute("""
            UPDATE bili_videos 
            SET first_comment_time = ?,
                play_velocity = ?,
                time_span_hours = ?
            WHERE av_id = ?
        """, (first_comment_time, play_velocity, time_span_hours, av_id))
        self.conn.commit()

    def get_videos_needing_comments(self, limit=50):
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT av_id, play_nums, pubdate 
            FROM bili_videos 
            WHERE (first_comment_time IS NULL OR comment_count = 0 OR comment_count <= 20)
              AND play_nums > 0
            ORDER BY play_nums DESC
            LIMIT ?
        """, (limit,))
        return cursor.fetchall()

    def insert_failed_video(self, task_id, av_id, error_type, error_msg):
        cursor = self.conn.cursor()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        cursor.execute("""
            INSERT INTO failed_videos (task_id, av_id, error_type, error_msg, created_at)
            VALUES (?, ?, ?, ?, ?)
        """, (task_id, av_id, error_type, error_msg, now))
        self.conn.commit()

    def get_existing_av_ids(self):
        cursor = self.conn.cursor()
        cursor.execute("SELECT av_id FROM bili_videos")
        return set(row[0] for row in cursor.fetchall())

    def get_today_av_ids(self):
        today = datetime.now().strftime("%Y-%m-%d")
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT av_id FROM bili_videos 
            WHERE DATE(fetched_at) = ?
        """, (today,))
        return set(row[0] for row in cursor.fetchall())

    def get_av_ids_by_keyword(self, keyword):
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT DISTINCT v.av_id 
            FROM bili_videos v
            JOIN spider_tasks t ON v.task_id = t.id
            WHERE t.keyword = ?
        """, (keyword,))
        return set(row[0] for row in cursor.fetchall())

    def get_uploaders_with_zero_fans(self, keyword=None):
        """获取粉丝数为0的UP主UID列表（去重）"""
        cursor = self.conn.cursor()
        if keyword:
            cursor.execute("""
                SELECT DISTINCT v.uploader_uid, v.uploader
                FROM bili_videos v
                JOIN spider_tasks t ON v.task_id = t.id
                WHERE t.keyword = ? AND v.uploader_uid IS NOT NULL AND v.uploader_uid != '' AND v.uploader_fans = 0
            """, (keyword,))
        else:
            cursor.execute("""
                SELECT DISTINCT uploader_uid, uploader
                FROM bili_videos
                WHERE uploader_uid IS NOT NULL AND uploader_uid != '' AND uploader_fans = 0
            """
            )
        return cursor.fetchall()

    def batch_update_uploader_fans(self, uid, fans, name=None):
        """批量更新某UP主所有视频的粉丝数"""
        cursor = self.conn.cursor()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        cursor.execute("""
            UPDATE bili_videos SET uploader_fans = ?, fetched_at = ?
            WHERE uploader_uid = ? AND uploader_fans = 0
        """, (fans, now, uid))
        updated = cursor.rowcount
        # 同时更新 uploaders 表
        if name:
            cursor.execute("""
                INSERT INTO bili_uploaders (uid, name, fans, fetched_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(uid) DO UPDATE SET name=excluded.name, fans=excluded.fans, fetched_at=excluded.fetched_at
            """, (uid, name, fans, now))
        else:
            cursor.execute("UPDATE bili_uploaders SET fans = ?, fetched_at = ? WHERE uid = ?", (fans, now, uid))
        self.conn.commit()
        return updated

    def get_task_stats(self, task_id):
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT 
                COUNT(*) as total,
                SUM(CASE WHEN play_nums > 0 THEN 1 ELSE 0 END) as success_count
            FROM bili_videos WHERE task_id = ?
        """, (task_id,))
        row = cursor.fetchone()
        return {"total": row[0], "success": row[1] or 0}

    MIN_TIME_SPAN_HOURS = 48

    def _parse_timestamp(self, timestamp_str):
        if not timestamp_str:
            return None
        try:
            return datetime.strptime(timestamp_str, "%Y-%m-%d %H:%M:%S.%f")
        except ValueError:
            try:
                return datetime.strptime(timestamp_str, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                try:
                    return datetime.fromisoformat(timestamp_str)
                except:
                    return None

    def calculate_momentum(self, av_id, metrics=None, video_pubdate=None):
        if metrics is None:
            metrics = ["play_nums", "danmakus", "favorites", "review", "like_count"]
        
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT * FROM video_history 
            WHERE av_id = ? 
            ORDER BY record_time ASC
        """, (av_id,))
        records = cursor.fetchall()
        col_names = [desc[0] for desc in cursor.description]
        records_dict = [dict(zip(col_names, record)) for record in records]
        
        result = {
            "av_id": av_id,
            "data_points": len(records_dict),
            "time_span_hours": 0,
            "status": "no_data",
            "status_desc": "无历史数据",
            "metrics": {},
            "composite_score": None
        }

        if len(records_dict) == 0:
            return result

        if len(records_dict) == 1:
            result["status"] = "first_record"
            result["status_desc"] = "首次采集，仅显示当前值"
            for metric in metrics:
                result["metrics"][metric] = {
                    "current": records_dict[0].get(metric, 0),
                    "total_growth_pct": None,
                    "cagr_daily_pct": None,
                    "avg_incremental_pct": None,
                    "is_momentum_valid": False
                }
            return result

        first_time = self._parse_timestamp(records_dict[0]["record_time"])
        last_time = self._parse_timestamp(records_dict[-1]["record_time"])
        if first_time and last_time:
            time_span_hours = (last_time - first_time).total_seconds() / 3600
        else:
            time_span_hours = 0
        result["time_span_hours"] = round(time_span_hours, 2)

        is_time_valid = time_span_hours >= self.MIN_TIME_SPAN_HOURS

        if not is_time_valid:
            result["status"] = "data_insufficient"
            result["status_desc"] = f"数据积累中（需≥{self.MIN_TIME_SPAN_HOURS}小时，当前{time_span_hours:.1f}小时）"
        else:
            result["status"] = "momentum_ready"
            result["status_desc"] = "动量可计算"

        for metric in metrics:
            values = [r.get(metric, 0) for r in records_dict]
            current = values[-1]
            previous = values[0]

            metric_result = {
                "current": current,
                "total_growth_pct": None,
                "cagr_daily_pct": None,
                "avg_incremental_pct": None,
                "is_momentum_valid": is_time_valid
            }

            if is_time_valid and previous > 0:
                total_growth = (current - previous) / previous * 100
                metric_result["total_growth_pct"] = round(total_growth, 2)

                time_span_days = max(time_span_hours / 24, 0.5)
                cagr = ((current / previous) ** (1 / time_span_days) - 1) * 100
                metric_result["cagr_daily_pct"] = round(cagr, 2)

                if len(values) >= 3:
                    changes = [(values[i] - values[i-1]) / max(values[i-1], 1) * 100 
                              for i in range(1, len(values))]
                    metric_result["avg_incremental_pct"] = round(sum(changes) / len(changes), 2)

            result["metrics"][metric] = metric_result

        if video_pubdate and result["status"] == "momentum_ready":
            try:
                pubdate_dt = datetime.strptime(video_pubdate, "%Y-%m-%d %H:%M:%S")
                age_days = (datetime.now() - pubdate_dt).days
                result["video_age_days"] = age_days
            except:
                result["video_age_days"] = None

        return result

    def calculate_freshness_weight(self, video_age_days):
        if video_age_days is None or video_age_days < 0:
            return 1.0
        if video_age_days <= 1:
            return 2.0
        elif video_age_days <= 3:
            return 1.7
        elif video_age_days <= 7:
            return 1.4
        elif video_age_days <= 14:
            return 1.2
        elif video_age_days <= 30:
            return 1.1
        elif video_age_days <= 90:
            return 1.0
        else:
            return 0.8

    def get_keyword_momentum_ranking(self, keyword, metric="play_nums", limit=20):
        """
        单次快照动量排名：无需历史数据，一次爬取即可分析。
        
        评分维度（全部归一化到 0~1）：
          - velocity_score  (30%): 播放速率 = 播放量 / 视频年龄(小时)
          - conversion_score (25%): 粉丝转化率 = 播放量 / UP主粉丝数
          - engagement_score (20%): 互动密度 = (点赞+投币+收藏+评论+弹幕) / 播放量
          - freshness_weight (15%): 新鲜度权重（基于视频年龄的分段函数）
          - normalized_value (10%): 当前播放量归一化
        
        当有历史数据时，额外显示真实增长率作为参考。
        """
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT v.av_id, v.title, v.play_nums, v.pubdate, v.uploader, v.uploader_uid, v.uploader_fans,
                   v.play_velocity, v.video_age_hours, v.engagement_score,
                   v.comment_count, v.like_count, v.coin, v.share, v.danmakus, v.review
            FROM bili_videos v
            JOIN spider_tasks t ON v.task_id = t.id
            WHERE t.keyword = ?
            ORDER BY v.play_nums DESC
        """, (keyword,))
        videos = cursor.fetchall()

        if not videos:
            return []

        # 计算归一化所需的极值（全部强制转数值，兼容数据库脏数据）
        velocities = [float(v[7]) for v in videos if v[7] and float(v[7]) > 0]
        max_velocity = max(velocities) if velocities else 1

        conversions = []
        for v in videos:
            fans = float(v[5] or 0)
            if fans > 0:
                conversions.append(float(v[2] or 0) / fans)
        max_conversion = max(conversions) if conversions else 1

        engagements = [float(v[9]) for v in videos if v[9] and float(v[9]) > 0]
        max_engagement = max(engagements) if engagements else 1

        plays = [float(v[2]) for v in videos if v[2]]
        max_plays = max(plays) if plays else 1
        min_plays = min(plays) if plays else 0

        results = []
        for video in videos:
            (av_id, title, current_plays, pubdate, uploader, uploader_uid, uploader_fans,
             play_velocity, video_age_hours, engagement_raw,
             comment_count, like_count, coin, share, danmakus, review) = video

            current_plays = int(current_plays or 0)
            uploader_fans = int(uploader_fans or 0)
            play_velocity = float(play_velocity or 0)
            video_age_hours = float(video_age_hours or 0)
            engagement_raw = float(engagement_raw or 0)

            # 1. 播放速率得分
            velocity_score = min(play_velocity / max_velocity, 1.0) if max_velocity > 0 else 0

            # 2. 粉丝转化率得分
            conversion_rate = 0
            if uploader_fans > 0:
                conversion_rate = current_plays / uploader_fans
            conversion_score = min(conversion_rate / max_conversion, 1.0) if max_conversion > 0 and conversion_rate > 0 else 0

            # 3. 互动密度得分
            engagement_score = min(engagement_raw / max_engagement, 1.0) if max_engagement > 0 else 0

            # 4. 新鲜度权重（基于 video_age_hours）
            video_age_days = video_age_hours / 24 if video_age_hours > 0 else None
            freshness = self.calculate_freshness_weight(video_age_days)
            # 归一化到 0~1 范围（原始范围 0.8~2.0）
            freshness_normalized = (freshness - 0.8) / (2.0 - 0.8)

            # 5. 当前值归一化
            normalized_value = (current_plays - min_plays) / (max_plays - min_plays) if max_plays > min_plays else 0.5

            # 综合评分
            composite_score = (
                velocity_score * 0.30 +
                conversion_score * 0.25 +
                engagement_score * 0.20 +
                freshness_normalized * 0.15 +
                normalized_value * 0.10
            )

            # 尝试获取历史增长数据（可选增强）
            historical_growth = None
            data_points = 1
            try:
                h_cursor = self.conn.cursor()
                h_cursor.execute("""
                    SELECT COUNT(*) FROM video_history WHERE av_id = ?
                """, (av_id,))
                data_points = h_cursor.fetchone()[0] or 1
                if data_points >= 2:
                    h_cursor.execute("""
                        SELECT play_nums FROM video_history WHERE av_id = ? ORDER BY record_time ASC
                    """, (av_id,))
                    history = [float(r[0]) for r in h_cursor.fetchall()]
                    if history and history[0] > 0:
                        historical_growth = round((history[-1] - history[0]) / history[0] * 100, 2)
            except:
                pass

            video_info = {
                "av_id": av_id,
                "title": title,
                "current_value": current_plays,
                "pubdate": pubdate,
                "uploader": uploader,
                "uploader_uid": uploader_uid,
                "uploader_fans": uploader_fans,
                "conversion_rate": round(conversion_rate, 2),
                "play_velocity": round(play_velocity, 2),
                "engagement_score": round(engagement_raw, 4),
                "video_age_hours": round(video_age_hours, 1),
                "comment_count": review or 0,
                "velocity_score": round(velocity_score, 4),
                "conversion_score": round(conversion_score, 4),
                "engagement_norm_score": round(engagement_score, 4),
                "freshness_normalized": round(freshness_normalized, 4),
                "normalized_value": round(normalized_value, 4),
                "composite_score": round(composite_score, 4),
                "data_points": data_points,
                "historical_growth_pct": historical_growth,
                # 兼容旧字段
                "status": "snapshot",
                "status_desc": "快照分析",
                "total_growth_pct": historical_growth,
                "cagr_daily_pct": None,
                "avg_incremental_pct": None,
                "freshness_weight": freshness,
            }

            results.append(video_info)

        results.sort(key=lambda x: x["composite_score"], reverse=True)
        return results[:limit]

    def close(self):
        if hasattr(self._local, 'conn') and self._local.conn:
            self._local.conn.close()
            self._local.conn = None


class BiliSpider:
    def __init__(self, args):
        self.args = args
        self.cookie_mgr = CookieManager(COOKIE_FILE)
        self.db = Database(DB_FILE)
        self.task_id = None
        self.stats_lock = threading.Lock()
        self.success_count = 0
        self.fail_count = 0
        self.search_fail_count = 0
        self._pw = None
        self._pw_browser = None
        self._pw_lock = threading.Lock()
        self._init_logger()

    def _init_logger(self):
        log_dir = "logs"
        os.makedirs(log_dir, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.log_file = os.path.join(log_dir, f"spider_{timestamp}.log")
        self.log(f"日志文件: {self.log_file}")

    def log(self, message):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        log_line = f"[{timestamp}] {message}"
        print(log_line)
        try:
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(log_line + "\n")
        except Exception:
            pass

    def _get_mixin_key(self, orig):
        return functools.reduce(lambda s, i: s + orig[i], MIXIN_KEY_ENC_TAB, '')[:32]

    def _get_wbi_keys(self, headers):
        url = "https://api.bilibili.com/x/web-interface/nav"
        try:
            resp = requests.get(url, headers=headers, timeout=10, verify=False)
            data = resp.json()
            if data.get("code") == 0:
                img_key = data["data"]["wbi_img"]["img_url"].rsplit("/", 1)[1].split(".")[0]
                sub_key = data["data"]["wbi_img"]["sub_url"].rsplit("/", 1)[1].split(".")[0]
                return img_key, sub_key
        except Exception as e:
            self.log(f"  [WBI] 获取密钥失败: {e}")
        return None, None

    def get_wbi_signed_params(self, params, headers=None):
        if headers is None:
            headers = self.cookie_mgr.get_headers()
        
        img_key, sub_key = self._get_wbi_keys(headers)
        if not img_key or not sub_key:
            return params
        
        mixin_key = self._get_mixin_key(img_key + sub_key)
        curr_time = round(time.time())
        params['wts'] = curr_time
        params = dict(sorted(params.items()))
        params = {k: ''.join(filter(lambda c: c not in "!'()*", str(v))) for k, v in params.items()}
        query = urlencode(params)
        wbi_sign = hashlib.md5((query + mixin_key).encode()).hexdigest()
        params['w_rid'] = wbi_sign
        return params

    def search_page(self, keyword, page, order="click", max_retries=3):
        params = {
            "search_type": "video",
            "keyword": keyword,
            "page": page,
            "order": order,
            "platform": "pc",
        }

        for attempt in range(max_retries):
            headers = self.cookie_mgr.get_headers("https://search.bilibili.com/")
            try:
                signed_params = self.get_wbi_signed_params(params.copy(), headers)
                url = f"https://api.bilibili.com/x/web-interface/search/type?{urlencode(signed_params)}"
                resp = requests.get(url, headers=headers, timeout=15, verify=False)

                if resp.status_code == 412:
                    wait = (attempt + 1) * 5
                    self.log(f"  [限流] 第{page}页 等待 {wait} 秒后重试 ({attempt+1}/{max_retries})...")
                    time.sleep(wait)
                    continue

                if resp.status_code != 200:
                    self.log(f"  [错误] 第{page}页 HTTP {resp.status_code}，重试 ({attempt+1}/{max_retries})...")
                    time.sleep(random.uniform(2, 4))
                    continue

                data = resp.json()

                if data["code"] != 0:
                    msg = data.get("message", "未知错误")
                    if "搜索请求超时" in msg:
                        self.log(f"  [超时] 第{page}页 重试 ({attempt+1}/{max_retries})...")
                        time.sleep(random.uniform(2, 4))
                        continue
                    else:
                        self.log(f"  [错误] 第{page}页: {msg}")
                        with self.stats_lock:
                            self.search_fail_count += 1
                        return None

                return data.get("data", {})

            except requests.RequestException as e:
                if attempt < max_retries - 1:
                    self.log(f"  [异常] 第{page}页 请求异常，重试 ({attempt+1}/{max_retries})...")
                    time.sleep(random.uniform(2, 4))
                else:
                    self.log(f"  [失败] 第{page}页 搜索请求失败: {e}")
                    with self.stats_lock:
                        self.search_fail_count += 1
                    return None

        self.log(f"  [失败] 第{page}页 超过最大重试次数")
        with self.stats_lock:
            self.search_fail_count += 1
        return None

    def get_video_detail(self, av_id, max_retries=3):
        """获取视频详情，返回 (detail_dict, error_str)，detail_dict 为 None 表示失败"""
        last_error = None

        for attempt in range(max_retries):
            headers = self.cookie_mgr.get_headers()
            try:
                params = {"aid": int(av_id)}
                signed_params = self.get_wbi_signed_params(params, headers)
                url = f"https://api.bilibili.com/x/web-interface/view?{urlencode(signed_params)}"
                resp = requests.get(url, headers=headers, timeout=15, verify=False)

                if resp.status_code == 412:
                    wait = (attempt + 1) * 3
                    last_error = f"412限流(第{attempt+1}次)"
                    time.sleep(wait)
                    continue

                if resp.status_code != 200:
                    last_error = f"HTTP {resp.status_code}"
                    time.sleep(random.uniform(1, 3))
                    continue

                data = resp.json()

                if data["code"] == -404:
                    last_error = "视频不存在(-404)"
                    return None, last_error

                if data["code"] != 0:
                    msg = data.get("message", "未知错误")
                    last_error = f"API错误: {msg}"
                    if "频率" in msg or "风控" in msg:
                        time.sleep(random.uniform(3, 6))
                        continue
                    return None, last_error

                video_data = data["data"]
                stat = video_data["stat"]
                owner = video_data.get("owner", {})
                uploader_uid = str(owner.get("mid", ""))

                uploader_fans = 0
                uploader_level = owner.get("level", 0)
                uploader_verified = 1 if owner.get("official", {}).get("role") else 0
                
                if uploader_uid:
                    uploader_fans = self._fetch_uploader_fans(uploader_uid, headers)

                tags = []
                if video_data.get("tags"):
                    tags = [t.get("tag_name", "") for t in video_data.get("tags", [])]

                # 用发布时间计算视频年龄和播放速率（单次爬取即可用）
                pubdate_ts = video_data.get("pubdate", 0)
                pubdate_str = datetime.fromtimestamp(pubdate_ts).strftime("%Y-%m-%d %H:%M:%S") if pubdate_ts else None
                video_age_hours = 0
                play_velocity = 0
                if pubdate_ts > 0:
                    video_age_hours = max((time.time() - pubdate_ts) / 3600, 0.1)
                    play_nums = stat.get("view", 0)
                    play_velocity = round(play_nums / video_age_hours, 2)

                # 互动密度：(点赞+投币+收藏+评论+弹幕) / 播放量
                total_interactions = (stat.get("like", 0) + stat.get("coin", 0) + 
                                     stat.get("favorite", 0) + stat.get("reply", 0) + 
                                     stat.get("danmaku", 0))
                play_nums_for_engagement = stat.get("view", 1)
                engagement_score = round(total_interactions / max(play_nums_for_engagement, 1), 4)

                return {
                    "av_id": str(av_id),
                    "bvid": video_data.get("bvid"),
                    "title": video_data.get("title", ""),
                    "url": f"http://www.bilibili.com/video/av{av_id}",
                    "play_nums": stat.get("view", 0),
                    "danmakus": stat.get("danmaku", 0),
                    "favorites": stat.get("favorite", 0),
                    "review": stat.get("reply", 0),
                    "coin": stat.get("coin", 0),
                    "share": stat.get("share", 0),
                    "like_count": stat.get("like", 0),
                    "uploader": owner.get("name", ""),
                    "uploader_uid": uploader_uid,
                    "uploader_fans": uploader_fans,
                    "uploader_level": uploader_level,
                    "uploader_verified": uploader_verified,
                    "pubdate": pubdate_str,
                    "duration": video_data.get("duration", 0),
                    "description": video_data.get("desc", ""),
                    "tags": ",".join(tags) if tags else None,
                    "category": video_data.get("tname", ""),
                    "video_age_hours": round(video_age_hours, 2),
                    "play_velocity": play_velocity,
                    "engagement_score": engagement_score,
                }, None

            except requests.RequestException as e:
                last_error = f"请求异常: {str(e)[:50]}"
                if attempt < max_retries - 1:
                    time.sleep(random.uniform(1, 3))
                else:
                    return None, last_error

        return None, last_error or "超过最大重试次数"

    def _fetch_uploader_fans(self, uid, headers, max_retries=2):
        try:
            cursor = self.db.conn.cursor()
            cursor.execute("SELECT fans FROM bili_uploaders WHERE uid = ? AND fetched_at > datetime('now', '-7 days')", (uid,))
            cached = cursor.fetchone()
            if cached and cached[0] > 0:
                return cached[0]
        except:
            pass

        try:
            cursor = self.db.conn.cursor()
            cursor.execute("SELECT uploader_fans FROM bili_videos WHERE uploader_uid = ? AND uploader_fans > 0 LIMIT 1", (uid,))
            cached_video = cursor.fetchone()
            if cached_video and cached_video[0] > 0:
                return cached_video[0]
        except:
            pass

        try:
            self._fetch_uploader_fans_via_api(uid, headers)
            cursor = self.db.conn.cursor()
            cursor.execute("SELECT fans FROM bili_uploaders WHERE uid = ?", (uid,))
            result = cursor.fetchone()
            if result and result[0] > 0:
                return result[0]
        except:
            pass

        return 0

    def _fetch_uploader_fans_via_api(self, uid, headers):
        """三级级联获取粉丝: card API → relation API → Playwright"""
        # 第1级: card API
        result = self._fans_via_card_api(uid, headers)
        if result is not None:
            return result
        self.log(f"  [粉丝] UID={uid} card API失败，尝试 relation API")
        time.sleep(random.uniform(0.3, 0.6))

        # 第2级: relation API
        result = self._fans_via_relation_api(uid, headers)
        if result is not None:
            return result
        self.log(f"  [粉丝] UID={uid} relation API失败，尝试 Playwright")
        time.sleep(random.uniform(0.5, 1.0))

        # 第3级: Playwright 兜底
        result = self._fans_via_playwright(uid)
        if result is not None:
            return result

        self.log(f"  [粉丝] UID={uid} 三级回退全部失败")
        return None

    def _fans_via_card_api(self, uid, headers):
        """第1级: card API 获取粉丝数"""
        try:
            params = {"mid": int(uid)}
            api_url = f"https://api.bilibili.com/x/web-interface/card?{urlencode(params)}"
            resp = requests.get(api_url, headers=headers, timeout=10, verify=False)
            if resp.status_code == 200:
                data = resp.json()
                if data.get("code") == 0:
                    card = data.get("data", {}).get("card", {})
                    fans = card.get("fans", 0)
                    name = card.get("name", "")
                    if fans > 0:
                        self._update_uploader_fans(uid, fans, name)
                        return fans
                else:
                    self.log(f"  [粉丝-card] UID={uid} code={data.get('code')} msg={data.get('message', '')[:60]}")
            else:
                self.log(f"  [粉丝-card] UID={uid} HTTP {resp.status_code}")
        except Exception as e:
            self.log(f"  [粉丝-card] UID={uid} 异常: {e}")
        return None

    def _fans_via_relation_api(self, uid, headers):
        """第2级: relation stat API 获取粉丝数"""
        try:
            api_url = f"https://api.bilibili.com/x/relation/stat?vmid={uid}"
            resp = requests.get(api_url, headers=headers, timeout=10, verify=False)
            if resp.status_code == 200:
                data = resp.json()
                if data.get("code") == 0:
                    followers = data.get("data", {}).get("follower", 0)
                    if followers > 0:
                        self._update_uploader_fans(uid, followers)
                        return followers
                else:
                    self.log(f"  [粉丝-relation] UID={uid} code={data.get('code')} msg={data.get('message', '')[:60]}")
            else:
                self.log(f"  [粉丝-relation] UID={uid} HTTP {resp.status_code}")
        except Exception as e:
            self.log(f"  [粉丝-relation] UID={uid} 异常: {e}")
        return None

    def _fans_via_playwright(self, uid):
        """第3级: Playwright 浏览器兜底获取粉丝数"""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.log("  [粉丝-PW] playwright 未安装，跳过 (pip install playwright && python -m playwright install chromium)")
            return None

        with self._pw_lock:
            try:
                if self._pw is None:
                    self._pw = sync_playwright().start()
                    try:
                        self._pw_browser = self._pw.chromium.launch(headless=True)
                    except Exception:
                        self._pw_browser = self._pw.chromium.launch(headless=True, channel="msedge")

                context = self._pw_browser.new_context(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    viewport={"width": 1280, "height": 720},
                )
                page = context.new_page()
                page.goto(f"https://space.bilibili.com/{uid}", wait_until="domcontentloaded", timeout=15000)
                page.wait_for_timeout(3000)

                fans = 0
                name = ""
                try:
                    # 方法1: 通过 title 属性获取精确粉丝数
                    fan_link = page.query_selector(f'a[href="/{uid}/fans/fans"]')
                    if fan_link:
                        title_attr = fan_link.get_attribute("title")
                        if title_attr:
                            fans = int(''.join(filter(str.isdigit, title_attr)))
                    # 方法2: 回退到文本解析
                    if fans == 0:
                        fan_text_el = page.query_selector(f'a[href="/{uid}/fans/fans"] .n-data-v')
                        if fan_text_el:
                            text = fan_text_el.inner_text().strip().replace(',', '').replace(' ', '')
                            if '万' in text:
                                fans = int(float(text.replace('万', '')) * 10000)
                            else:
                                fans = int(''.join(filter(str.isdigit, text)))
                    # 获取UP主名称
                    name_el = page.query_selector('#h-name, .h-name, .user-name')
                    if name_el:
                        name = name_el.inner_text().strip()
                except Exception as e:
                    self.log(f"  [粉丝-PW] UID={uid} 页面解析异常: {e}")

                context.close()

                if fans > 0:
                    self.log(f"  [粉丝-PW] UID={uid} Playwright兜底成功: {fans:,}")
                    self._update_uploader_fans(uid, fans, name or None)
                    return fans
                else:
                    self.log(f"  [粉丝-PW] UID={uid} 页面未找到粉丝数")
            except Exception as e:
                self.log(f"  [粉丝-PW] UID={uid} 浏览器异常: {e}")
                try:
                    if self._pw_browser:
                        self._pw_browser.close()
                    if self._pw:
                        self._pw.stop()
                except:
                    pass
                self._pw = None
                self._pw_browser = None
        return None

    def _close_playwright(self):
        """关闭 Playwright 浏览器实例"""
        with self._pw_lock:
            try:
                if self._pw_browser:
                    self._pw_browser.close()
                if self._pw:
                    self._pw.stop()
            except:
                pass
            self._pw = None
            self._pw_browser = None

    def _update_uploader_fans(self, uid, fans, name=None):
        try:
            cursor = self.db.conn.cursor()
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            if name:
                cursor.execute("""
                    INSERT INTO bili_uploaders (uid, name, fans, fetched_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(uid) DO UPDATE SET 
                        name = excluded.name,
                        fans = excluded.fans,
                        fetched_at = excluded.fetched_at
                """, (uid, name, fans, now))
            else:
                cursor.execute("UPDATE bili_uploaders SET fans = ?, fetched_at = ? WHERE uid = ?", (fans, now, uid))
            self.db.conn.commit()
        except:
            pass

    def fetch_video_detail(self, av_id):
        time.sleep(random.uniform(0.5, self.args.delay))
        detail, error = self.get_video_detail(av_id)
        
        # 同时获取评论数据
        if detail:
            comment_data = self.fetch_video_comments(
                av_id, 
                detail.get("play_nums", 0), 
                detail.get("pubdate")
            )
            if comment_data:
                detail.update(comment_data)
        
        return av_id, detail, error

    def fetch_video_comments(self, av_id, play_nums, pubdate=None, max_retries=3):
        for attempt in range(max_retries):
            headers = self.cookie_mgr.get_headers()
            headers['Referer'] = 'https://www.bilibili.com/'
            
            try:
                params = {
                    "type": 1,
                    "oid": int(av_id),
                    "mode": 3,
                    "next": 0,
                }
                signed_params = self.get_wbi_signed_params(params, headers)
                comment_url = f"https://api.bilibili.com/x/v2/reply/main?{urlencode(signed_params)}"
                resp = requests.get(comment_url, headers=headers, timeout=15, verify=False)
                
                if resp.status_code == 412:
                    wait = (attempt + 1) * 3
                    if attempt < max_retries - 1:
                        self.log(f"  [评论限流] av{av_id} 等待 {wait} 秒后重试 ({attempt+1}/{max_retries})...")
                        time.sleep(wait)
                        continue
                    else:
                        return None
                
                if resp.status_code != 200:
                    if attempt < max_retries - 1:
                        time.sleep(random.uniform(1, 3))
                        continue
                    return None
                
                data = resp.json()
                if data.get("code") != 0:
                    if attempt < max_retries - 1:
                        time.sleep(random.uniform(1, 3))
                        continue
                    return None
                
                reply_data = data.get('data', {})
                replies = reply_data.get('replies', []) or []
                
                if not replies:
                    return None
                
                earliest_ctime = replies[-1].get('ctime', 0)
                
                if not earliest_ctime:
                    return None
                
                from datetime import datetime
                
                if pubdate:
                    try:
                        pub_dt = datetime.strptime(pubdate.split('.')[0], '%Y-%m-%d %H:%M:%S')
                        pub_ts = int(pub_dt.timestamp())
                        actual_start = min(earliest_ctime, pub_ts)
                    except:
                        actual_start = earliest_ctime
                else:
                    actual_start = earliest_ctime
                
                now = int(time.time())
                time_span_hours = (now - actual_start) / 3600
                
                if time_span_hours > 0 and play_nums > 0:
                    play_velocity = play_nums / time_span_hours
                else:
                    play_velocity = 0
                
                first_comment_time = datetime.fromtimestamp(actual_start).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
                
                return {
                    "first_comment_time": first_comment_time,
                    "play_velocity": round(play_velocity, 2),
                    "time_span_hours": round(time_span_hours, 2),
                }
                
            except Exception as e:
                if attempt < max_retries - 1:
                    self.log(f"  [评论异常] av{av_id} 重试 ({attempt+1}/{max_retries}): {e}")
                    time.sleep(random.uniform(1, 3))
                else:
                    self.log(f"  [评论获取失败] {av_id}: {e}")
                    return None
        
        return None

    def enrich_videos_with_comments(self, limit=50):
        self.log(f"\n{'='*60}")
        self.log(f"  补充评论数据和播放速率")
        self.log(f"{'='*60}")
        
        videos = self.db.get_videos_needing_comments(limit)
        self.log(f"待处理视频数: {len(videos)}")
        
        if not videos:
            self.log("[完成] 所有视频都已有评论数据")
            return
        
        success = 0
        fail = 0
        
        for i, (av_id, play_nums, pubdate) in enumerate(videos, 1):
            if i % 10 == 0:
                self.log(f"  进度: {i}/{len(videos)}")
            
            result = self.fetch_video_comments(av_id, play_nums or 0, pubdate)
            
            if result:
                self.db.update_video_comments(
                    av_id,
                    result['first_comment_time'],
                    result['play_velocity'],
                    result['time_span_hours']
                )
                success += 1
            else:
                fail += 1
            
            time.sleep(random.uniform(0.3, 0.8))
        
        self.log(f"\n[完成] 评论数据补充: 成功 {success}, 失败 {fail}")

    def fix_comment_count_from_review(self):
        """用视频详情API的review字段修复错误的comment_count"""
        cursor = self.db.conn.cursor()
        cursor.execute("""
            UPDATE bili_videos 
            SET comment_count = review 
            WHERE review > 0 AND (comment_count = 0 OR comment_count <= 20)
        """)
        fixed = cursor.rowcount
        self.db.conn.commit()
        if fixed > 0:
            self.log(f"[修复] 已修正 {fixed} 条视频的评论数 (comment_count <- review)")

    def enrich_videos_with_fans(self, keyword=None):
        """自动检测并补充粉丝数为0的UP主数据（三级回退）"""
        self.log(f"\n{'='*60}")
        self.log(f"  自动检测并补充粉丝数")
        self.log(f"{'='*60}")

        uploaders = self.db.get_uploaders_with_zero_fans(keyword)
        self.log(f"待补充UP主数: {len(uploaders)}")

        if not uploaders:
            self.log("[完成] 所有UP主已有粉丝数据")
            return

        success = 0
        fail = 0

        for i, (uid, name) in enumerate(uploaders, 1):
            if i % 10 == 0:
                self.log(f"  进度: {i}/{len(uploaders)}")

            headers = self.cookie_mgr.get_headers()
            fans = self._fetch_uploader_fans_via_api(uid, headers)

            if fans and fans > 0:
                updated = self.db.batch_update_uploader_fans(uid, fans, name)
                success += 1
                self.log(f"  [{i}/{len(uploaders)}] UID={uid} ({name}) -> {fans:,} (更新{updated}条视频)")
            else:
                fail += 1
                self.log(f"  [{i}/{len(uploaders)}] UID={uid} ({name}) -> 获取失败")

            time.sleep(random.uniform(0.3, 0.8))

        self.log(f"\n[完成] 粉丝数补充: 成功 {success}, 失败 {fail}")

    def run(self):
        self.log("=" * 60)
        self.log("  B站视频爬虫 (增强版)")
        self.log("=" * 60)
        self.log(f"关键词: {self.args.keyword}")
        self.log(f"页数: {self.args.pages}")
        self.log(f"排序: {self.args.order}")
        self.log(f"线程数: {self.args.threads}")
        self.log(f"延迟: {self.args.delay}s")
        self.log("")

        self.task_id = self.db.create_task(
            self.args.keyword, self.args.pages, self.args.order
        )
        self.log(f"[任务] 任务ID: {self.task_id}")

        all_av_ids = set()
        failed_pages = []

        for page in range(1, self.args.pages + 1):
            self.log(f"[搜索] 第 {page}/{self.args.pages} 页...")
            data = self.search_page(self.args.keyword, page, self.args.order)

            if not data:
                failed_pages.append(page)
                self.log(f"  失败，跳过")
                time.sleep(random.uniform(2, 4))
                continue

            results = data.get("result", [])
            if not results:
                self.log(f"  无结果，可能已到末页")
                break

            new_count = 0
            for item in results:
                av_id = str(item.get("aid", ""))
                if av_id and av_id not in all_av_ids:
                    all_av_ids.add(av_id)
                    new_count += 1

            self.log(f"  找到 {len(results)} 个，新增 {new_count} 个")
            time.sleep(random.uniform(self.args.delay, self.args.delay + 1))

        existing_ids = self.db.get_existing_av_ids()
        new_ids = list(all_av_ids - existing_ids)

        self.log(f"\n[搜索完成] 共 {len(all_av_ids)} 个视频，今日已爬 {len(all_av_ids) - len(new_ids)} 个，新增 {len(new_ids)} 个")
        self.log(f"[搜索统计] 搜索失败页数: {len(failed_pages)}")

        if not new_ids:
            self.log("[完成] 没有新视频需要爬取")
            self.db.update_task(self.task_id, status="completed", total_videos=len(all_av_ids))
            return

        self.log(f"\n[详情获取] 使用 {self.args.threads} 个线程获取视频详情...")

        self.db.update_task(self.task_id, total_videos=len(new_ids))

        with ThreadPoolExecutor(max_workers=self.args.threads) as executor:
            futures = {executor.submit(self.fetch_video_detail, av_id): av_id for av_id in new_ids}

            for i, future in enumerate(as_completed(futures), 1):
                av_id = futures[future]
                try:
                    result_av_id, detail, error = future.result()
                    if detail:
                        self.db.insert_video(self.task_id, detail)
                        with self.stats_lock:
                            self.success_count += 1
                        self.log(f"  [{i}/{len(new_ids)}] av{av_id} ✓")
                    else:
                        with self.stats_lock:
                            self.fail_count += 1
                        error_type = "detail_fetch_failed"
                        error_msg = error or "未知错误"
                        self.db.insert_failed_video(self.task_id, av_id, error_type, error_msg)
                        self.log(f"  [{i}/{len(new_ids)}] av{av_id} ✗ ({error_msg})")
                except Exception as e:
                    with self.stats_lock:
                        self.fail_count += 1
                    self.db.insert_failed_video(self.task_id, av_id, "exception", str(e)[:100])
                    self.log(f"  [{i}/{len(new_ids)}] av{av_id} ✗ (异常: {e})")

        self.db.update_task(
            self.task_id,
            status="completed",
            success_count=self.success_count,
            fail_count=self.fail_count,
            completed_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        )

        self.log(f"\n[完成] 成功: {self.success_count}, 失败: {self.fail_count}")

        self._print_top_videos()

    def _print_top_videos(self, limit=10):
        cursor = self.db.conn.cursor()
        cursor.execute("""
            SELECT title, url, play_nums, danmakus, favorites, review
            FROM bili_videos
            WHERE task_id = ?
            ORDER BY play_nums DESC
            LIMIT ?
        """, (self.task_id, limit))
        rows = cursor.fetchall()

        if rows:
            self.log(f"\n{'=' * 60}")
            self.log(f"  Top {limit} 热门视频")
            self.log(f"{'=' * 60}")
            for i, row in enumerate(rows, 1):
                title, url, plays, danmaku, favs, reviews = row
                self.log(f"\n{i}. {title[:50]}")
                self.log(f"   播放: {plays:,} | 弹幕: {danmaku:,} | 收藏: {favs:,} | 评论: {reviews:,}")

    def export_csv(self, keyword=None):
        if keyword is None:
            keyword = self.args.keyword

        output_dir = os.path.join("output", keyword)
        os.makedirs(output_dir, exist_ok=True)
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_file = os.path.join(output_dir, f"{timestamp}.csv")

        cursor = self.db.conn.cursor()
        cursor.execute("""
            SELECT v.url, v.danmakus, v.favorites, v.play_nums, v.review, v.title
            FROM bili_videos v
            JOIN spider_tasks t ON v.task_id = t.id
            WHERE t.keyword = ?
            ORDER BY v.play_nums DESC
        """, (keyword,))
        rows = cursor.fetchall()

        if rows:
            import csv
            with open(output_file, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.writer(f)
                writer.writerow(["address", "danmakus", "favorites", "play_nums", "review", "title"])
                writer.writerows(rows)
            
            self.log(f"\n[导出] CSV 已保存到 {output_file}")
            self.log(f"[记录] 共 {len(rows)} 条数据")
            return output_file
        else:
            self.log(f"\n[导出] 无数据可导出 (关键词: {keyword})")
            return None

    def analyze_momentum(self):
        keyword = self.args.keyword
        metric = "play_nums"
        limit = 999999  # 爬多少算多少
        
        self.log(f"\n{'=' * 90}")
        self.log(f"  动量分析 (单次快照模式)")
        self.log(f"{'=' * 90}")
        self.log(f"关键词: {keyword}")
        self.log(f"显示数量: {limit}")
        self.log(f"评分维度: 播放速率(30%) + 粉丝转化(25%) + 互动密度(20%) + 新鲜度(15%) + 播放量(10%)")
        self.log("")

        ranking = self.db.get_keyword_momentum_ranking(keyword, metric, limit)

        if not ranking:
            self.log("[动量分析] 无数据可分析")
            return

        self.log(f"\n动量排行 Top {limit}（按综合评分排序）")
        self.log("-" * 180)
        header = (f"{'排名':<4} {'标题':<28} {'播放量':>10} {'UP主':<10} {'粉丝':>8} "
                  f"{'转化':>7} {'速率/h':>9} {'互动密度':>8} {'视频年龄':>8} {'历史增长':>8} {'综合分':>7}")
        self.log(header)
        self.log("-" * 180)

        for i, item in enumerate(ranking, 1):
            title = (item["title"] or "未知")[:26]
            current = f"{item['current_value']:,}" if item['current_value'] else "-"
            uploader = (item.get('uploader_uid') or '未知')[:9]
            fans = f"{item['uploader_fans']:,}" if item.get('uploader_fans') else "-"
            conv = f"{item['conversion_rate']:.1f}x" if item.get('conversion_rate', 0) > 0 else "-"
            velocity = f"{item['play_velocity']:.0f}" if item.get('play_velocity', 0) > 0 else "-"
            engagement = f"{item['engagement_score']:.3f}" if item.get('engagement_score', 0) > 0 else "-"
            age_hours = item.get('video_age_hours', 0)
            if age_hours >= 24:
                age_str = f"{age_hours/24:.0f}天"
            else:
                age_str = f"{age_hours:.0f}h"
            growth = f"{item['historical_growth_pct']:.1f}%" if item.get('historical_growth_pct') is not None else "N/A"
            score = f"{item['composite_score']:.3f}"
            
            self.log(f"{i:<4} {title:<28} {current:>10} {uploader:<10} {fans:>8} {conv:>7} {velocity:>9} {engagement:>8} {age_str:>8} {growth:>8} {score:>7}")

        self.log("-" * 180)
        self.log("\n提示: 综合分 = 播放速率(30%) + 粉丝转化(25%) + 互动密度(20%) + 新鲜度(15%) + 播放量(10%)")
        self.log("      历史增长列显示多次爬取后的真实增长率，首次爬取为 N/A")

        # 动量分析后自动导出
        self._export_momentum_csv(ranking, keyword, metric)

    def _export_momentum_csv(self, ranking, keyword, metric):
        output_dir = os.path.join("output", keyword)
        os.makedirs(output_dir, exist_ok=True)
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_file = os.path.join(output_dir, f"momentum_{timestamp}.csv")

        import csv
        with open(output_file, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow([
                "排名", "av_id", "标题", "播放量", "UP主", "UP主UID", "粉丝数", "粉丝转化率",
                "播放速率(次/小时)", "视频年龄(小时)", "互动密度", "评论数",
                "速率得分", "转化得分", "互动得分", "新鲜度得分", "播放量得分",
                "历史增长%", "数据点数量", "发布时间", "综合评分"
            ])
            for i, item in enumerate(ranking, 1):
                writer.writerow([
                    i,
                    item["av_id"],
                    item["title"],
                    item.get("current_value", 0),
                    item.get("uploader", ""),
                    item.get("uploader_uid", ""),
                    item.get("uploader_fans", 0),
                    item.get("conversion_rate", 0),
                    item.get("play_velocity", 0),
                    item.get("video_age_hours", 0),
                    item.get("engagement_score", 0),
                    item.get("comment_count", 0),
                    item.get("velocity_score", 0),
                    item.get("conversion_score", 0),
                    item.get("engagement_norm_score", 0),
                    item.get("freshness_normalized", 0),
                    item.get("normalized_value", 0),
                    item.get("historical_growth_pct", "N/A"),
                    item.get("data_points", 1),
                    item.get("pubdate", ""),
                    round(item.get("composite_score", 0), 4),
                ])

        self.log(f"\n[导出] 动量分析结果已保存到 {output_file}")


def main():
    parser = argparse.ArgumentParser(
        description="B站视频爬虫 - 动量分析版",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python bili_spider.py -k 穷人 -p 5              # 爬取+补粉丝+动量分析+导出
  python bili_spider.py -k 穷人 -p 10 -t 5        # 10页5线程
  python bili_spider.py -k 穷人 --export-only      # 仅导出原始CSV
        """
    )

    parser.add_argument("--keyword", "-k", type=str, default="服饰",
                        help="搜索关键词 (默认: 服饰)")
    parser.add_argument("--pages", "-p", type=int, default=5,
                        help="爬取页数 (默认: 5)")
    parser.add_argument("--order", "-o", type=str, default="click",
                        choices=["click", "pubdate", "dm", "stow"],
                        help="排序方式 (默认: click)")
    parser.add_argument("--threads", "-t", type=int, default=3,
                        help="线程数 (默认: 3)")
    parser.add_argument("--delay", "-d", type=float, default=1.0,
                        help="请求间隔秒数 (默认: 1.0)")
    parser.add_argument("--export-only", action="store_true",
                        help="仅导出原始CSV，不爬取")

    args = parser.parse_args()

    spider = BiliSpider(args)

    try:
        if args.export_only:
            print(f"[导出模式] 仅导出 {args.keyword} 的数据")
            spider.export_csv(args.keyword)
        else:
            spider.run()
            spider.fix_comment_count_from_review()
            spider.enrich_videos_with_fans(args.keyword)
            spider.analyze_momentum()
    except KeyboardInterrupt:
        print("\n[中断] 用户手动停止")
        spider.db.update_task(spider.task_id, status="failed", error_msg="用户中断")
    except Exception as e:
        print(f"\n[错误] {e}")
        if spider.task_id:
            spider.db.update_task(spider.task_id, status="failed", error_msg=str(e))
    finally:
        spider._close_playwright()
        spider.db.close()


if __name__ == "__main__":
    main()