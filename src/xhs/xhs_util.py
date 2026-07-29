"""
xhs_util.py - 小红书爬虫公共工具模块
基于 B站 util.py 平移改造，字段映射为小红书对应概念。
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
DB_FILE = _paths.XHS_DB
COOKIE_FILE = _paths.XHS_COOKIE

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

    def get_headers(self, referer="https://www.xiaohongshu.com/"):
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
            CREATE TABLE IF NOT EXISTS xhs_notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                note_id TEXT UNIQUE NOT NULL,
                title TEXT,
                url TEXT,
                xsec_token TEXT,
                interact_count INTEGER DEFAULT 0,
                liked_count INTEGER DEFAULT 0,
                collected_count INTEGER DEFAULT 0,
                comment_count INTEGER DEFAULT 0,
                share_count INTEGER DEFAULT 0,
                nickname TEXT,
                user_id TEXT,
                fans_count INTEGER DEFAULT 0,
                pub_time DATETIME,
                note_type TEXT,
                description TEXT,
                tags TEXT,
                category TEXT,
                note_age_hours REAL DEFAULT 0,
                interact_velocity REAL DEFAULT 0,
                engagement_score REAL DEFAULT 0,
                fetched_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (task_id) REFERENCES spider_tasks(id)
            )
        """)

        cursor.execute("CREATE INDEX IF NOT EXISTS idx_notes_task_id ON xhs_notes(task_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_notes_note_id ON xhs_notes(note_id)")

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS xhs_users (
                user_id TEXT PRIMARY KEY,
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
                note_id TEXT NOT NULL,
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
                note_id TEXT NOT NULL,
                task_id INTEGER,
                interact_count INTEGER,
                liked_count INTEGER,
                collected_count INTEGER,
                comment_count INTEGER,
                share_count INTEGER,
                record_time DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (task_id) REFERENCES spider_tasks(id)
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_history_note_id ON note_history(note_id)")
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
        note_id = note_data.get("note_id", "")
        if not note_id:
            return None

        cursor.execute("SELECT id FROM xhs_notes WHERE note_id = ?", (note_id,))
        row = cursor.fetchone()

        if row:
            cursor.execute("""
                UPDATE xhs_notes SET
                    task_id = ?,
                    title = ?,
                    url = ?,
                    xsec_token = ?,
                    interact_count = ?,
                    liked_count = ?,
                    collected_count = ?,
                    comment_count = ?,
                    share_count = ?,
                    nickname = ?,
                    user_id = ?,
                    fans_count = ?,
                    pub_time = ?,
                    note_type = ?,
                    description = ?,
                    tags = ?,
                    category = ?,
                    note_age_hours = ?,
                    interact_velocity = ?,
                    engagement_score = ?,
                    fetched_at = ?
                WHERE note_id = ?
            """, (
                task_id,
                note_data.get("title"),
                note_data.get("url"),
                note_data.get("xsec_token"),
                note_data.get("interact_count", 0),
                note_data.get("liked_count", 0),
                note_data.get("collected_count", 0),
                note_data.get("comment_count", 0),
                note_data.get("share_count", 0),
                note_data.get("nickname"),
                note_data.get("user_id"),
                note_data.get("fans_count", 0),
                note_data.get("pub_time"),
                note_data.get("note_type"),
                note_data.get("description"),
                note_data.get("tags"),
                note_data.get("category"),
                note_data.get("note_age_hours", 0),
                note_data.get("interact_velocity", 0),
                note_data.get("engagement_score", 0),
                now,
                note_id,
            ))
            note_db_id = row[0]
        else:
            cursor.execute("""
                INSERT INTO xhs_notes (
                    task_id, note_id, title, url, xsec_token,
                    interact_count, liked_count, collected_count, comment_count, share_count,
                    nickname, user_id, fans_count, pub_time, note_type,
                    description, tags, category, note_age_hours, interact_velocity,
                    engagement_score, fetched_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                task_id, note_id,
                note_data.get("title"),
                note_data.get("url"),
                note_data.get("xsec_token"),
                note_data.get("interact_count", 0),
                note_data.get("liked_count", 0),
                note_data.get("collected_count", 0),
                note_data.get("comment_count", 0),
                note_data.get("share_count", 0),
                note_data.get("nickname"),
                note_data.get("user_id"),
                note_data.get("fans_count", 0),
                note_data.get("pub_time"),
                note_data.get("note_type"),
                note_data.get("description"),
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
                note_id, task_id, interact_count, liked_count, collected_count,
                comment_count, share_count, record_time
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            note_id, task_id,
            note_data.get("interact_count", 0),
            note_data.get("liked_count", 0),
            note_data.get("collected_count", 0),
            note_data.get("comment_count", 0),
            note_data.get("share_count", 0),
            now,
        ))

        self.conn.commit()
        return note_db_id

    def add_failed_note(self, task_id, note_id, error_type, error_msg):
        cursor = self.conn.cursor()
        cursor.execute("""
            INSERT INTO failed_notes (task_id, note_id, error_type, error_msg)
            VALUES (?, ?, ?, ?)
        """, (task_id, note_id, error_type, error_msg))
        self.conn.commit()

    def upsert_user(self, user_id, nickname, fans=0, follows=0, desc=""):
        cursor = self.conn.cursor()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        cursor.execute("SELECT user_id FROM xhs_users WHERE user_id = ?", (user_id,))
        if cursor.fetchone():
            cursor.execute("""
                UPDATE xhs_users SET nickname = ?, fans = ?, follows = ?, desc = ?, fetched_at = ?
                WHERE user_id = ?
            """, (nickname, fans, follows, desc, now, user_id))
        else:
            cursor.execute("""
                INSERT INTO xhs_users (user_id, nickname, fans, follows, desc, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (user_id, nickname, fans, follows, desc, now))
        self.conn.commit()

    def get_user_fans(self, user_id):
        cursor = self.conn.cursor()
        cursor.execute("SELECT fans FROM xhs_users WHERE user_id = ?", (user_id,))
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
            SELECT n.note_id, n.title, n.interact_count, n.pub_time, n.nickname, n.user_id, n.fans_count,
                   n.interact_velocity, n.note_age_hours, n.engagement_score,
                   n.comment_count, n.liked_count, n.collected_count, n.share_count, n.tags, n.url
            FROM xhs_notes n
            JOIN spider_tasks t ON n.task_id = t.id
            WHERE t.keyword = ?
            ORDER BY n.interact_count DESC
        """, (keyword,))
        notes = cursor.fetchall()

        if not notes:
            return []

        velocities = [float(n[7]) for n in notes if n[7] and float(n[7]) > 0]
        max_velocity = max(velocities) if velocities else 1

        conversions = []
        for n in notes:
            fans = float(n[6] or 0)
            if fans > 0:
                conversions.append(float(n[2] or 0) / fans)
        max_conversion = max(conversions) if conversions else 1

        # 互动密度 = comment / interact，值域 0~1，有真实区分度
        engagements = []
        for n in notes:
            interact = float(n[2] or 0)
            comment = float(n[10] or 0)   # comment_count 在 index 10
            if interact > 0:
                engagements.append(comment / interact)
        max_engagement = max(engagements) if engagements else 1

        interacts = [float(n[2]) for n in notes if n[2]]
        max_interact = max(interacts) if interacts else 1
        min_interact = min(interacts) if interacts else 0

        results = []
        for note in notes:
            (note_id, title, interact_count, pub_time, nickname, user_id, fans_count,
             interact_velocity, note_age_hours, engagement_raw,
             comment_count, liked_count, collected_count, share_count, tags_str, url_str) = note

            interact_count = int(interact_count or 0)
            interact_velocity = float(interact_velocity or 0)
            note_age_hours = float(note_age_hours or 0)
            comment_count_val = int(comment_count or 0)

            # 互动密度 = 评论数 / 互动总量（0~1，有区分度）
            engagement_raw = comment_count_val / max(interact_count, 1) if interact_count > 0 else 0

            velocity_score = min(interact_velocity / max_velocity, 1.0) if max_velocity > 0 else 0

            engagement_score = min(engagement_raw / max_engagement, 1.0) if max_engagement > 0 else 0

            note_age_days = note_age_hours / 24 if note_age_hours > 0 else None
            freshness = self.calculate_freshness_weight(note_age_days)
            freshness_normalized = (freshness - 0.8) / (2.0 - 0.8)

            normalized_value = (interact_count - min_interact) / (max_interact - min_interact) if max_interact > min_interact else 0.5

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
                h_cursor.execute("SELECT COUNT(*) FROM note_history WHERE note_id = ?", (note_id,))
                data_points = h_cursor.fetchone()[0] or 1
                if data_points >= 2:
                    h_cursor.execute("SELECT interact_count FROM note_history WHERE note_id = ? ORDER BY record_time ASC", (note_id,))
                    history = [float(r[0]) for r in h_cursor.fetchall()]
                    if history and history[0] > 0:
                        historical_growth = round((history[-1] - history[0]) / history[0] * 100, 2)
            except Exception:
                pass

            note_info = {
                "note_id": note_id,
                "title": title,
                "current_value": interact_count,
                "pub_time": pub_time,
                "nickname": nickname,
                "user_id": user_id,
                "interact_velocity": round(interact_velocity, 2),
                "engagement_score": round(engagement_raw, 4),
                "note_age_hours": round(note_age_hours, 1),
                "comment_count": comment_count or 0,
                "liked_count": liked_count or 0,
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
                "share_link": url_str or "",
            }
            results.append(note_info)

        results.sort(key=lambda x: x["composite_score"], reverse=True)
        return results[:limit]

    def get_value_ranking(self, keyword, limit=50):
        """
        价值分析：与动量分析互补，动量看“速度”，价值看“质量”。
        3 维评分：
        - 收藏率(40%): 收藏/点赞  →  内容被“保存”的意愿，代表深度价值
        - 互动密度(30%): 收藏/互动总量  →  深度保存占所有互动的比例
        - 评论率(30%): 评论/点赞  →  讨论热度
        注：分享率已废弃（小红书前端不展示分享数，恒为 0）
        """
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT n.note_id, n.title, n.interact_count, n.pub_time, n.nickname, n.user_id, n.fans_count,
                   n.note_age_hours, n.engagement_score, n.comment_count,
                   n.liked_count, n.collected_count, n.share_count, n.tags, n.url
            FROM xhs_notes n
            JOIN spider_tasks t ON n.task_id = t.id
            WHERE t.keyword = ?
              AND n.liked_count > 0
            ORDER BY n.interact_count DESC
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
            interact = int(n[2] or 0)

            if liked > 0:
                collect_rates.append(collected / liked)
                share_rates.append(share / liked)
                comment_rates.append(comment / liked)
            if interact > 0:
                # 互动密度 = 收藏 / 互动总量，代表"深度保存"占所有互动的比例
                engagements.append(collected / interact)

        max_collect = max(collect_rates) if collect_rates else 1
        max_engage = max(engagements) if engagements else 1
        max_share = max(share_rates) if share_rates else 1
        max_comment = max(comment_rates) if comment_rates else 1

        results = []
        for note in notes:
            (note_id, title, interact_count, pub_time, nickname, user_id, fans_count,
             note_age_hours, engagement_raw, comment_count,
             liked_count, collected_count, share_count, tags_str, url_str) = note

            liked = int(liked_count or 0)
            collected = int(collected_count or 0)
            share = int(share_count or 0)
            comment = int(comment_count or 0)
            interact = int(interact_count or 0)

            collect_rate = collected / max(liked, 1) if liked > 0 else 0
            share_rate = share / max(liked, 1) if liked > 0 else 0
            comment_rate = comment / max(liked, 1) if liked > 0 else 0
            # 互动密度：收藏占互动总量的比例（区间 0~1，有真实区分度）
            engagement = collected / max(interact, 1) if interact > 0 else 0

            collect_score = min(collect_rate / max_collect, 1.0) if max_collect > 0 else 0
            engage_score = min(engagement / max_engage, 1.0) if max_engage > 0 else 0
            share_score = min(share_rate / max_share, 1.0) if max_share > 0 else 0
            comment_score = min(comment_rate / max_comment, 1.0) if max_comment > 0 else 0

            value_score = (
                collect_score * 0.40 +
                engage_score * 0.30 +
                # share_score 恒为 0（小红书前端不展示分享数），权重合并至评论率
                comment_score * 0.30
            )

            results.append({
                "note_id": note_id,
                "title": title,
                "interact_count": interact,
                "nickname": nickname,
                "user_id": user_id,
                "liked_count": liked,
                "collected_count": collected,
                "comment_count": comment,
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
                "share_link": url_str or "",
            })

        results.sort(key=lambda x: x["value_score"], reverse=True)
        return results[:limit]

    def export_momentum_csv(self, keyword, csv_file, results):
        import csv
        with open(csv_file, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "排名", "note_id", "分享链接", "标题", "互动量", "作者", "作者UID",
                "互动速率(次/小时)", "笔记年龄(小时)", "互动密度", "评论数", "标签",
                "速率得分", "互动得分", "新鲜度得分", "互动量得分",
                "历史增长%", "数据点数量", "发布时间", "综合评分"
            ])
            for i, item in enumerate(results, 1):
                writer.writerow([
                    i,
                    item["note_id"],
                    item.get("share_link", ""),
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
                "排名", "note_id", "分享链接", "标题", "互动量", "作者", "作者UID",
                "点赞数", "收藏数", "评论数",
                "标签", "收藏率", "互动密度", "评论率",
                "收藏得分", "互动得分", "评论得分",
                "笔记年龄(小时)", "发布时间", "价值综合评分"
            ])
            for i, item in enumerate(results, 1):
                writer.writerow([
                    i,
                    item["note_id"],
                    item.get("share_link", ""),
                    item["title"],
                    item.get("interact_count", 0),
                    item.get("nickname", ""),
                    item.get("user_id", ""),
                    item.get("liked_count", 0),
                    item.get("collected_count", 0),
                    item.get("comment_count", 0),
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

    def export_to_csv(self, keyword, csv_file, momentum_results):
        self.export_momentum_csv(keyword, csv_file, momentum_results)
