import requests
import sqlite3
import argparse
import time
import random
import warnings
import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urlencode

requests.packages.urllib3.disable_warnings()
warnings.filterwarnings("ignore")

DB_FILE = "bili_spider.db"
COOKIE_FILE = "bili_cookie.txt"

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
        self.current_index = 0
        self.lock = threading.Lock()
        self.load_cookies(cookie_file)

    def load_cookies(self, cookie_file):
        if os.path.exists(cookie_file):
            with open(cookie_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        self.cookies.append(line)
        print(f"[Cookie] 已加载 {len(self.cookies)} 个 Cookie")

    def get_cookie(self):
        with self.lock:
            if not self.cookies:
                return None
            cookie = self.cookies[self.current_index]
            self.current_index = (self.current_index + 1) % len(self.cookies)
            return cookie

    def get_headers(self, referer="https://www.bilibili.com/"):
        headers = {
            "User-Agent": random.choice(USER_AGENTS),
            "Referer": referer,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        cookie = self.get_cookie()
        if cookie:
            headers["Cookie"] = f"SESSDATA={cookie}"
        return headers


class Database:
    def __init__(self, db_file):
        self.conn = sqlite3.connect(db_file)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.create_tables()

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
        for key, value in kwargs.items():
            updates.append(f"{key} = ?")
            params.append(value)
        if "completed_at" in kwargs or "status" in kwargs:
            updates.append("completed_at = ?")
            params.append(datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3])
        params.append(task_id)
        cursor.execute(f"UPDATE spider_tasks SET {', '.join(updates)} WHERE id = ?", params)
        self.conn.commit()

    def insert_video(self, task_id, video_data):
        cursor = self.conn.cursor()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        cursor.execute("""
            INSERT OR REPLACE INTO bili_videos 
            (task_id, av_id, bvid, title, url, play_nums, danmakus, favorites, 
             review, coin, share, like_count, uploader, uploader_uid, uploader_fans,
             pubdate, duration, description, tags, category, fetched_at,
             first_comment_time, comment_count, play_velocity, time_span_hours)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            INSERT OR REPLACE INTO bili_uploaders 
            (uid, name, fans, level, verified, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            uid,
            video_data.get("uploader"),
            video_data.get("uploader_fans", 0),
            video_data.get("uploader_level", 0),
            video_data.get("uploader_verified", 0),
            now,
        ))

    def update_video_comments(self, av_id, first_comment_time, comment_count, play_velocity, time_span_hours):
        cursor = self.conn.cursor()
        cursor.execute("""
            UPDATE bili_videos 
            SET first_comment_time = ?,
                comment_count = ?,
                play_velocity = ?,
                time_span_hours = ?
            WHERE av_id = ?
        """, (first_comment_time, comment_count, play_velocity, time_span_hours, av_id))
        self.conn.commit()

    def get_videos_needing_comments(self, limit=50):
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT av_id, play_nums, pubdate 
            FROM bili_videos 
            WHERE (first_comment_time IS NULL OR comment_count = 0)
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
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT v.av_id, v.title, v.play_nums, v.pubdate, v.uploader_uid, v.uploader_fans,
                   v.play_velocity, v.time_span_hours, v.comment_count, v.first_comment_time
            FROM bili_videos v
            JOIN spider_tasks t ON v.task_id = t.id
            WHERE t.keyword = ?
            ORDER BY v.play_nums DESC
        """, (keyword,))
        videos = cursor.fetchall()

        cursor2 = self.conn.cursor()
        cursor2.execute("""
            SELECT MAX(play_nums) as max_plays, MIN(play_nums) as min_plays,
                   MAX(CAST(play_nums AS FLOAT) / CASE WHEN uploader_fans > 0 THEN uploader_fans ELSE 1 END) as max_conversion,
                   MIN(CAST(play_nums AS FLOAT) / CASE WHEN uploader_fans > 0 THEN uploader_fans ELSE 1 END) as min_conversion,
                   MAX(play_velocity) as max_velocity, MIN(play_velocity) as min_velocity
            FROM bili_videos v
            JOIN spider_tasks t ON v.task_id = t.id
            WHERE t.keyword = ? AND uploader_fans > 0
        """, (keyword,))
        max_min = cursor2.fetchone()
        max_plays = max_min[0] or 1
        min_plays = max_min[1] or 0
        max_conversion = max_min[2] or 1
        min_conversion = max_min[3] or 0
        max_velocity = max_min[4] or 1
        min_velocity = max_min[5] or 0

        cursor3 = self.conn.cursor()
        cursor3.execute("""
            SELECT MAX(play_velocity) as max_velocity_all
            FROM bili_videos v
            JOIN spider_tasks t ON v.task_id = t.id
            WHERE t.keyword = ? AND play_velocity > 0
        """, (keyword,))
        max_velocity_row = cursor3.fetchone()
        max_velocity_all = max_velocity_row[0] or 1

        results = []
        for video in videos:
            av_id, title, current_plays, pubdate, uploader_uid, uploader_fans, play_velocity, video_time_span, comment_count, first_comment_time = video
            momentum = self.calculate_momentum(av_id, [metric], pubdate)

            conversion_rate = 0
            if uploader_fans and uploader_fans > 0:
                conversion_rate = (current_plays or 0) / uploader_fans

            video_info = {
                "av_id": av_id,
                "title": title,
                "current_value": current_plays or 0,
                "pubdate": pubdate,
                "uploader_uid": uploader_uid,
                "uploader_fans": uploader_fans or 0,
                "conversion_rate": round(conversion_rate, 2),
                "play_velocity": play_velocity or 0,
                "comment_count": comment_count or 0,
                "time_span_hours": video_time_span or 0,
                "first_comment_time": first_comment_time,
                "status": momentum["status"],
                "status_desc": momentum["status_desc"],
                "data_points": momentum["data_points"],
                "momentum_time_span": momentum["time_span_hours"],
                "total_growth_pct": None,
                "cagr_daily_pct": None,
                "avg_incremental_pct": None,
                "freshness_weight": 1.0,
                "normalized_value": 0,
                "conversion_score": 0,
                "velocity_score": 0,
                "momentum_score": 0,
                "composite_score": 0
            }

            if metric in momentum["metrics"]:
                m = momentum["metrics"][metric]
                video_info["total_growth_pct"] = m["total_growth_pct"]
                video_info["cagr_daily_pct"] = m["cagr_daily_pct"]
                video_info["avg_incremental_pct"] = m["avg_incremental_pct"]

            video_age = momentum.get("video_age_days")
            video_info["freshness_weight"] = self.calculate_freshness_weight(video_age)

            if max_plays > min_plays:
                video_info["normalized_value"] = (current_plays - min_plays) / (max_plays - min_plays)
            else:
                video_info["normalized_value"] = 0.5

            if max_conversion > min_conversion and conversion_rate > 0:
                video_info["conversion_score"] = min(conversion_rate / max(max_conversion, 1), 1.0)
            else:
                video_info["conversion_score"] = 0

            if play_velocity and max_velocity_all > 0:
                video_info["velocity_score"] = min(play_velocity / max_velocity_all, 1.0)
            else:
                video_info["velocity_score"] = 0

            if momentum["status"] == "momentum_ready":
                cagr = video_info["cagr_daily_pct"] or 0
                momentum_score = min(max(cagr + 100, 0), 500) / 500
                video_info["momentum_score"] = momentum_score
                video_info["composite_score"] = (
                    video_info["normalized_value"] * 0.25 +
                    momentum_score * 0.20 +
                    video_info["conversion_score"] * 0.20 +
                    video_info["velocity_score"] * 0.20 +
                    video_info["freshness_weight"] * 0.15
                )
            else:
                video_info["momentum_score"] = 0
                video_info["composite_score"] = (
                    video_info["normalized_value"] * 0.35 +
                    video_info["conversion_score"] * 0.25 +
                    video_info["velocity_score"] * 0.25 +
                    video_info["freshness_weight"] * 0.15
                )

            results.append(video_info)

        results.sort(key=lambda x: x["composite_score"], reverse=True)
        return results[:limit]

    def close(self):
        self.conn.close()


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

    def search_page(self, keyword, page, order="click", max_retries=3):
        params = {
            "search_type": "video",
            "keyword": keyword,
            "page": page,
            "order": order,
            "platform": "pc",
        }
        url = f"https://api.bilibili.com/x/web-interface/search/type?{urlencode(params)}"

        for attempt in range(max_retries):
            headers = self.cookie_mgr.get_headers("https://search.bilibili.com/")
            try:
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
        url = f"https://api.bilibili.com/x/web-interface/view?aid={av_id}"
        last_error = None

        for attempt in range(max_retries):
            headers = self.cookie_mgr.get_headers()
            try:
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
                    self._last_error = last_error
                    return None

                if data["code"] != 0:
                    msg = data.get("message", "未知错误")
                    last_error = f"API错误: {msg}"
                    if "频率" in msg or "风控" in msg:
                        time.sleep(random.uniform(3, 6))
                        continue
                    self._last_error = last_error
                    return None

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

                self._last_error = None
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
                    "pubdate": datetime.fromtimestamp(video_data.get("pubdate", 0)).strftime("%Y-%m-%d %H:%M:%S") if video_data.get("pubdate") else None,
                    "duration": video_data.get("duration", 0),
                    "description": video_data.get("desc", ""),
                    "tags": ",".join(tags) if tags else None,
                    "category": video_data.get("tname", ""),
                }

            except requests.RequestException as e:
                last_error = f"请求异常: {str(e)[:50]}"
                if attempt < max_retries - 1:
                    time.sleep(random.uniform(1, 3))
                else:
                    self._last_error = last_error
                    return None

        self._last_error = last_error or "超过最大重试次数"
        return None

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
        import hashlib
        import functools
        from urllib.parse import urlencode

        MIXIN_KEY_ENC_TAB = [
            46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
            27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
            37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
            22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
        ]

        def get_mixin_key(orig):
            return functools.reduce(lambda s, i: s + orig[i], MIXIN_KEY_ENC_TAB, '')[:32]

        try:
            nav_resp = requests.get(
                "https://api.bilibili.com/x/web-interface/wbi/index/nav",
                headers=headers, timeout=10, verify=False
            )
            if nav_resp.status_code == 200:
                nav_data = nav_resp.json()
                if nav_data.get("code") == 0:
                    img_url = nav_data["data"]["wbi_img"]["img_url"]
                    sub_url = nav_data["data"]["wbi_img"]["sub_url"]
                    img_key = img_url.rsplit("/", 1)[1].split(".")[0]
                    sub_key = sub_url.rsplit("/", 1)[1].split(".")[0]
                    mixin_key = get_mixin_key(img_key + sub_key)

                    params = {
                        "mid": int(uid),
                        "photo": "false",
                        "platform": "web",
                    }
                    params["wts"] = int(time.time())
                    params = dict(sorted(params.items()))
                    params = {k: ''.join(filter(lambda c: c not in "!'()*", str(v))) for k, v in params.items()}
                    query = urlencode(params)
                    wbi_sign = hashlib.md5((query + mixin_key).encode()).hexdigest()
                    params["w_rid"] = wbi_sign

                    api_url = f"https://api.bilibili.com/x/wbi/space/acc/info?{urlencode(params)}"
                    resp = requests.get(api_url, headers=headers, timeout=10, verify=False)
                    if resp.status_code == 200:
                        data = resp.json()
                        if data.get("code") == 0:
                            info = data.get("data", {})
                            fans = info.get("fans", 0)
                            name = info.get("name", "")
                            if fans > 0:
                                self._update_uploader_fans(uid, fans, name)
                                return fans
        except:
            pass

        return None

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
            self.conn.commit()
        except:
            pass

    def fetch_video_detail(self, av_id):
        time.sleep(random.uniform(0.5, self.args.delay))
        self._last_error = None
        detail = self.get_video_detail(av_id)
        error = self._last_error
        return av_id, detail, error

    def fetch_video_comments(self, av_id, play_nums, pubdate=None):
        headers = self.cookie_mgr.get_headers()
        headers['Referer'] = 'https://www.bilibili.com/'
        
        try:
            comment_url = f"https://api.bilibili.com/x/v2/reply/main?type=1&oid={av_id}&mode=3&next=0"
            resp = requests.get(comment_url, headers=headers, timeout=15, verify=False)
            
            if resp.status_code == 412:
                time.sleep(2)
                resp = requests.get(comment_url, headers=headers, timeout=15, verify=False)
            
            if resp.status_code != 200:
                return None
            
            data = resp.json()
            if data.get("code") != 0:
                return None
            
            reply_data = data.get('data', {})
            replies = reply_data.get('replies', []) or []
            pagination = reply_data.get('cursor', {}).get('pagination', {})
            
            comment_count = pagination.get('count', 0)
            
            if not replies:
                return None
            
            earliest_ctime = replies[-1].get('ctime', 0)
            latest_ctime = replies[0].get('ctime', 0)
            
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
                "comment_count": comment_count or len(replies),
                "play_velocity": round(play_velocity, 2),
                "time_span_hours": round(time_span_hours, 2),
            }
            
        except Exception as e:
            self.log(f"  [评论获取失败] {av_id}: {e}")
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
                    result['comment_count'],
                    result['play_velocity'],
                    result['time_span_hours']
                )
                success += 1
            else:
                fail += 1
            
            time.sleep(random.uniform(0.3, 0.8))
        
        self.log(f"\n[完成] 评论数据补充: 成功 {success}, 失败 {fail}")

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

        today_ids = self.db.get_today_av_ids()
        new_ids = list(all_av_ids - today_ids)

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
        metric = self.args.momentum_metric
        limit = self.args.momentum_limit
        
        self.log(f"\n{'=' * 80}")
        self.log(f"  动量分析 (增强版)")
        self.log(f"{'=' * 80}")
        self.log(f"关键词: {keyword}")
        self.log(f"指标: {metric}")
        self.log(f"显示数量: {limit}")
        self.log("")

        ranking = self.db.get_keyword_momentum_ranking(keyword, metric, limit)

        if not ranking:
            self.log("[动量分析] 无数据可分析")
            return

        metric_names = {
            "play_nums": "播放量",
            "danmakus": "弹幕数",
            "favorites": "收藏数",
            "review": "评论数",
            "like_count": "点赞数"
        }
        metric_name = metric_names.get(metric, metric)

        self.log(f"\n动量排行 Top {limit}（按综合评分排序）")
        self.log("-" * 170)
        header = f"{'排名':<4} {'标题':<30} {'当前' + metric_name:>10} {'UP主':<12} {'粉丝数':>8} {'转化率':>8} {'播放速率':>10} {'评论数':>8} {'总增长%':>8} {'日均%':>8} {'状态':<10}"
        self.log(header)
        self.log("-" * 170)

        for i, item in enumerate(ranking, 1):
            title = item["title"][:28] if item["title"] else "未知"
            current = f"{item['current_value']:,}" if item['current_value'] else "-"
            uploader = item.get('uploader_uid', '未知')[:11]
            fans = f"{item['uploader_fans']:,}" if item.get('uploader_fans') else "-"
            conv_rate = f"{item['conversion_rate']:.1f}x" if item.get('conversion_rate', 0) > 0 else "-"
            velocity = f"{item['play_velocity']:.1f}/h" if item.get('play_velocity', 0) > 0 else "-"
            comments = f"{item['comment_count']:,}" if item.get('comment_count', 0) > 0 else "-"
            total_growth = f"{item['total_growth_pct']:.1f}%" if item.get('total_growth_pct') is not None else "N/A"
            cagr = f"{item['cagr_daily_pct']:.2f}%" if item.get('cagr_daily_pct') is not None else "N/A"
            status = item.get('status_desc', '')[:8]
            
            self.log(f"{i:<4} {title:<30} {current:>10} {uploader:<12} {fans:>8} {conv_rate:>8} {velocity:>10} {comments:>8} {total_growth:>8} {cagr:>8} {status:<10}")

        self.log("-" * 170)

        if self.args.export_momentum:
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
                "排名", "av_id", "标题", "当前值", "UP主UID", "粉丝数", "粉丝转化率",
                "播放速率(次/小时)", "评论数", "时间跨度(小时)", "最早评论时间",
                "总增长%", "日均增长%", "增量均值%", "数据点数量",
                "发布时间", "状态", "综合评分"
            ])
            for i, item in enumerate(ranking, 1):
                writer.writerow([
                    i,
                    item["av_id"],
                    item["title"],
                    item.get("current_value", 0),
                    item.get("uploader_uid", ""),
                    item.get("uploader_fans", 0),
                    item.get("conversion_rate", 0),
                    item.get("play_velocity", 0),
                    item.get("comment_count", 0),
                    item.get("time_span_hours", 0),
                    item.get("first_comment_time", ""),
                    item.get("total_growth_pct", "N/A"),
                    item.get("cagr_daily_pct", "N/A"),
                    item.get("avg_incremental_pct", "N/A"),
                    item.get("data_points", 1),
                    item.get("pubdate", ""),
                    item.get("status_desc", ""),
                    round(item.get("composite_score", 0), 4),
                ])

        self.log(f"\n[导出] 动量分析结果已保存到 {output_file}")


def main():
    parser = argparse.ArgumentParser(
        description="B站视频爬虫 - 支持关键词搜索、多线程获取、SQLite存储",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python bili_spider.py --keyword 服饰 --pages 10
  python bili_spider.py --keyword 穿搭 --pages 5 --order pubdate -e
  python bili_spider.py --keyword 美食 --pages 3 --threads 5
  python bili_spider.py --keyword 服饰 --export-only  # 仅导出CSV
  python bili_spider.py --keyword 服饰 --enrich-comments  # 补充评论和播放速率
  python bili_spider.py --keyword 服饰 --enrich-comments --comments-limit 100  # 补充100条
  python bili_spider.py --keyword 服饰 --momentum  # 动量分析
  python bili_spider.py -k 服饰 -m --momentum-metric favorites  # 收藏动量
  python bili_spider.py -k 服饰 -m --export-momentum  # 导出动量结果
        """
    )

    parser.add_argument("--keyword", "-k", type=str, default="服饰",
                        help="搜索关键词 (默认: 服饰)")
    parser.add_argument("--pages", "-p", type=int, default=5,
                        help="爬取页数 (默认: 5)")
    parser.add_argument("--order", "-o", type=str, default="click",
                        choices=["click", "pubdate", "dm", "stow"],
                        help="排序方式: click=播放量, pubdate=时间, dm=弹幕, stow=收藏 (默认: click)")
    parser.add_argument("--threads", "-t", type=int, default=3,
                        help="线程数 (默认: 3, 建议 1-5)")
    parser.add_argument("--delay", "-d", type=float, default=1.0,
                        help="请求间隔秒数 (默认: 1.0)")
    parser.add_argument("--export", "-e", action="store_true",
                        help="完成后导出CSV文件")
    parser.add_argument("--export-only", action="store_true",
                        help="仅从数据库导出CSV，不执行爬取")
    parser.add_argument("--enrich-comments", action="store_true",
                        help="补充评论数据和播放速率 (hourly_view_rate)")
    parser.add_argument("--comments-limit", type=int, default=50,
                        help="补充评论数据的视频数量上限 (默认: 50)")
    parser.add_argument("--momentum", "-m", action="store_true",
                        help="进行动量分析，评估视频增长趋势")
    parser.add_argument("--momentum-metric", type=str, default="play_nums",
                        choices=["play_nums", "danmakus", "favorites", "review", "like_count"],
                        help="动量分析指标 (默认: play_nums)")
    parser.add_argument("--momentum-limit", type=int, default=20,
                        help="动量分析显示数量 (默认: 20)")
    parser.add_argument("--export-momentum", action="store_true",
                        help="导出动量分析结果为CSV")

    args = parser.parse_args()

    spider = BiliSpider(args)

    try:
        if args.export_only:
            print(f"[导出模式] 仅导出 {args.keyword} 的数据，不爬取")
            spider.export_csv(args.keyword)
        elif args.enrich_comments and not args.momentum:
            print(f"[评论补充模式] 补充评论数据和播放速率")
            spider.enrich_videos_with_comments(limit=args.comments_limit)
        else:
            spider.run()
            
            if args.enrich_comments:
                print(f"\n[评论补充] 补充评论数据和播放速率")
                spider.enrich_videos_with_comments(limit=args.comments_limit)
            
            if args.export:
                spider.export_csv()
            
            if args.momentum:
                print(f"\n[动量分析] 开始动量分析")
                spider.analyze_momentum()
            
            if args.export_momentum:
                print(f"\n[导出动量] 导出动量分析结果")
                ranking = spider.db.get_keyword_momentum_ranking(args.keyword, args.momentum_metric, args.momentum_limit)
                spider._export_momentum_csv(ranking, args.keyword, args.momentum_metric)
    except KeyboardInterrupt:
        print("\n[中断] 用户手动停止")
        spider.db.update_task(spider.task_id, status="failed", error_msg="用户中断")
    except Exception as e:
        print(f"\n[错误] {e}")
        if spider.task_id:
            spider.db.update_task(spider.task_id, status="failed", error_msg=str(e))
    finally:
        spider.db.close()


if __name__ == "__main__":
    main()