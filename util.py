"""
util.py - B站爬虫公共工具模块
提取自 bili_spider.py，供 bili_spider.py 和 value_spider.py 共享使用。
"""

import requests
import sqlite3
import time
import random
import warnings
import os
import threading
import hashlib
import functools
from datetime import datetime
from urllib.parse import urlencode

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
                return random.choice(self.full_cookies)
            if self.cookies:
                return random.choice(self.cookies)
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


class WbiSigner:
    """WBI 签名工具类"""

    @staticmethod
    def _get_mixin_key(orig):
        return functools.reduce(lambda s, i: s + orig[i], MIXIN_KEY_ENC_TAB, '')[:32]

    @staticmethod
    def _get_wbi_keys(headers):
        url = "https://api.bilibili.com/x/web-interface/nav"
        try:
            resp = requests.get(url, headers=headers, timeout=10, verify=False)
            data = resp.json()
            if data.get("code") == 0:
                img_key = data["data"]["wbi_img"]["img_url"].rsplit("/", 1)[1].split(".")[0]
                sub_key = data["data"]["wbi_img"]["sub_url"].rsplit("/", 1)[1].split(".")[0]
                return img_key, sub_key
        except Exception:
            pass
        return None, None

    @classmethod
    def sign(cls, params, headers=None):
        if headers is None:
            headers = {"User-Agent": random.choice(USER_AGENTS)}

        img_key, sub_key = cls._get_wbi_keys(headers)
        if not img_key or not sub_key:
            return params

        mixin_key = cls._get_mixin_key(img_key + sub_key)
        curr_time = round(time.time())
        params['wts'] = curr_time
        params = dict(sorted(params.items()))
        params = {k: ''.join(filter(lambda c: c not in "!'()*", str(v))) for k, v in params.items()}
        query = urlencode(params)
        wbi_sign = hashlib.md5((query + mixin_key).encode()).hexdigest()
        params['w_rid'] = wbi_sign
        return params


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
            """)
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
                    changes = [(values[i] - values[i - 1]) / max(values[i - 1], 1) * 100
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
        """
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT v.av_id, v.title, v.play_nums, v.pubdate, v.uploader, v.uploader_uid, v.uploader_fans,
                   v.play_velocity, v.video_age_hours, v.engagement_score,
                   v.comment_count, v.like_count, v.coin, v.share, v.danmakus, v.review, v.tags
            FROM bili_videos v
            JOIN spider_tasks t ON v.task_id = t.id
            WHERE t.keyword = ?
            ORDER BY v.play_nums DESC
        """, (keyword,))
        videos = cursor.fetchall()

        if not videos:
            return []

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
             comment_count, like_count, coin, share, danmakus, review, tags_str) = video

            current_plays = int(current_plays or 0)
            uploader_fans = int(uploader_fans or 0)
            play_velocity = float(play_velocity or 0)
            video_age_hours = float(video_age_hours or 0)
            engagement_raw = float(engagement_raw or 0)

            velocity_score = min(play_velocity / max_velocity, 1.0) if max_velocity > 0 else 0

            conversion_rate = 0
            if uploader_fans > 0:
                conversion_rate = current_plays / uploader_fans
            conversion_score = min(conversion_rate / max_conversion, 1.0) if max_conversion > 0 and conversion_rate > 0 else 0

            engagement_score = min(engagement_raw / max_engagement, 1.0) if max_engagement > 0 else 0

            video_age_days = video_age_hours / 24 if video_age_hours > 0 else None
            freshness = self.calculate_freshness_weight(video_age_days)
            freshness_normalized = (freshness - 0.8) / (2.0 - 0.8)

            normalized_value = (current_plays - min_plays) / (max_plays - min_plays) if max_plays > min_plays else 0.5

            composite_score = (
                velocity_score * 0.30 +
                conversion_score * 0.25 +
                engagement_score * 0.20 +
                freshness_normalized * 0.15 +
                normalized_value * 0.10
            )

            historical_growth = None
            data_points = 1
            try:
                h_cursor = self.conn.cursor()
                h_cursor.execute("SELECT COUNT(*) FROM video_history WHERE av_id = ?", (av_id,))
                data_points = h_cursor.fetchone()[0] or 1
                if data_points >= 2:
                    h_cursor.execute("SELECT play_nums FROM video_history WHERE av_id = ? ORDER BY record_time ASC", (av_id,))
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
                "status": "snapshot",
                "status_desc": "快照分析",
                "total_growth_pct": historical_growth,
                "cagr_daily_pct": None,
                "avg_incremental_pct": None,
                "freshness_weight": freshness,
                "tags": tags_str or "",
                "tag_list": [t.strip() for t in (tags_str or "").split(",") if t.strip()],
            }
            results.append(video_info)

        results.sort(key=lambda x: x["composite_score"], reverse=True)
        return results[:limit]

    # ========== 价值评分排名 ==========

    def get_keyword_value_ranking(self, keyword, limit=20):
        """
        价值评分排名：衡量视频的长期内容价值。

        评分维度（全部归一化到 0~1）：
          - 深度互动比 (35%): (coin+favorite) / like
          - 互动密度   (25%): (like+coin+fav+reply+danmaku) / play
          - 收藏率     (20%): favorite / play
          - 粉丝转化率 (10%): play / fans
          - 分享率     (10%): share / play
        """
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT v.av_id, v.title, v.play_nums, v.pubdate, v.uploader, v.uploader_uid, v.uploader_fans,
                   v.like_count, v.coin, v.favorites, v.share, v.danmakus, v.review,
                   v.video_age_hours, v.engagement_score
            FROM bili_videos v
            JOIN spider_tasks t ON v.task_id = t.id
            WHERE t.keyword = ?
            ORDER BY v.play_nums DESC
        """, (keyword,))
        videos = cursor.fetchall()

        if not videos:
            return []

        # 第一遍：计算所有原始值，确定极值用于归一化
        raw_list = []
        for v in videos:
            (av_id, title, play_nums, pubdate, uploader, uploader_uid, uploader_fans,
             like_count, coin, favorites, share, danmakus, review,
             video_age_hours, engagement_score) = v

            play_nums = int(play_nums or 0)
            like_count = int(like_count or 0)
            coin = int(coin or 0)
            favorites = int(favorites or 0)
            share_count = int(share or 0)
            danmakus = int(danmakus or 0)
            review_count = int(review or 0)
            uploader_fans = int(uploader_fans or 0)
            video_age_hours = float(video_age_hours or 0)

            # 深度互动比: (投币+收藏) / 点赞
            deep_ratio = (coin + favorites) / max(like_count, 1)

            # 互动密度
            total_interaction = like_count + coin + favorites + review_count + danmakus
            engagement_density = total_interaction / max(play_nums, 1)

            # 收藏率
            fav_rate = favorites / max(play_nums, 1)

            # 粉丝转化率
            conv_rate = play_nums / max(uploader_fans, 1) if uploader_fans > 0 else 0

            # 分享率
            share_rate = share_count / max(play_nums, 1)

            raw_list.append({
                "av_id": av_id,
                "title": title,
                "play_nums": play_nums,
                "pubdate": pubdate,
                "uploader": uploader,
                "uploader_uid": uploader_uid,
                "uploader_fans": uploader_fans,
                "like_count": like_count,
                "coin": coin,
                "favorites": favorites,
                "share": share_count,
                "danmakus": danmakus,
                "review": review_count,
                "video_age_hours": video_age_hours,
                "deep_ratio": deep_ratio,
                "engagement_density": engagement_density,
                "fav_rate": fav_rate,
                "conv_rate": conv_rate,
                "share_rate": share_rate,
            })

        # 计算各维度极值
        max_deep = max(r["deep_ratio"] for r in raw_list) or 1
        max_eng = max(r["engagement_density"] for r in raw_list) or 1
        max_fav = max(r["fav_rate"] for r in raw_list) or 1
        max_conv = max(r["conv_rate"] for r in raw_list) or 1
        max_share = max(r["share_rate"] for r in raw_list) or 1

        # 第二遍：归一化 + 计算综合分
        results = []
        for r in raw_list:
            deep_score = min(r["deep_ratio"] / max_deep, 1.0) if max_deep > 0 else 0
            eng_score = min(r["engagement_density"] / max_eng, 1.0) if max_eng > 0 else 0
            fav_score = min(r["fav_rate"] / max_fav, 1.0) if max_fav > 0 else 0
            conv_score = min(r["conv_rate"] / max_conv, 1.0) if max_conv > 0 else 0
            share_score = min(r["share_rate"] / max_share, 1.0) if max_share > 0 else 0

            value_score = (
                deep_score * 0.35 +
                eng_score * 0.25 +
                fav_score * 0.20 +
                conv_score * 0.10 +
                share_score * 0.10
            )

            results.append({
                "av_id": r["av_id"],
                "title": r["title"],
                "current_value": r["play_nums"],
                "pubdate": r["pubdate"],
                "uploader": r["uploader"],
                "uploader_uid": r["uploader_uid"],
                "uploader_fans": r["uploader_fans"],
                "like_count": r["like_count"],
                "coin": r["coin"],
                "favorites": r["favorites"],
                "share": r["share"],
                "danmakus": r["danmakus"],
                "review": r["review"],
                "video_age_hours": r["video_age_hours"],
                # 原始值
                "deep_ratio": round(r["deep_ratio"], 4),
                "engagement_density": round(r["engagement_density"], 4),
                "fav_rate": round(r["fav_rate"], 4),
                "conv_rate": round(r["conv_rate"], 2),
                "share_rate": round(r["share_rate"], 4),
                # 归一化得分
                "deep_score": round(deep_score, 4),
                "eng_score": round(eng_score, 4),
                "fav_score": round(fav_score, 4),
                "conv_score": round(conv_score, 4),
                "share_score": round(share_score, 4),
                # 综合分
                "value_score": round(value_score, 4),
            })

        results.sort(key=lambda x: x["value_score"], reverse=True)
        return results[:limit]

    def close(self):
        if hasattr(self._local, 'conn') and self._local.conn:
            self._local.conn.close()
            self._local.conn = None
