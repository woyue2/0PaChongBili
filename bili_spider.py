"""
bili_spider.py - B站视频爬虫主类
合并原 momentum_spider.py + value_spider.py 的全部逻辑。
"""

import requests
import csv
import os
import re
import sys
import time
import random
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urlencode

from bili_util import DB_FILE, COOKIE_FILE, CookieManager, Database, WbiSigner

requests.packages.urllib3.disable_warnings()


def _safe_str(s, max_len=26):
    if not s:
        return ""
    s = re.sub(r'[\U00010000-\U0010ffff]', '', s)
    return s[:max_len]


class BiliSpider:
    def __init__(self, args, output_dir=None, mode="momentum"):
        self.args = args
        self.cookie_mgr = CookieManager(COOKIE_FILE)
        self.db = Database(DB_FILE)
        self.task_id = None
        self.output_dir = output_dir
        self.mode = mode
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
        self.log_file = os.path.join(log_dir, f"bili_spider_{timestamp}.log")
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

    def get_wbi_signed_params(self, params, headers=None):
        if headers is None:
            headers = self.cookie_mgr.get_headers()
        return WbiSigner.sign(params, headers)

    # ==================== 搜索 & 热门 ====================

    def fetch_popular_page(self, page, max_retries=3):
        params = {"ps": 20, "pn": page}
        for attempt in range(max_retries):
            headers = self.cookie_mgr.get_headers("https://www.bilibili.com/")
            try:
                signed_params = self.get_wbi_signed_params(params.copy(), headers)
                url = f"https://api.bilibili.com/x/web-interface/popular?{urlencode(signed_params)}"
                resp = requests.get(url, headers=headers, timeout=15, verify=False)
                if resp.status_code == 412:
                    wait = (attempt + 1) * 5
                    self.log(f"  [限流] 热门第{page}页 等待 {wait} 秒后重试 ({attempt+1}/{max_retries})...")
                    time.sleep(wait); continue
                if resp.status_code != 200:
                    self.log(f"  [错误] 热门第{page}页 HTTP {resp.status_code}，重试 ({attempt+1}/{max_retries})...")
                    time.sleep(random.uniform(2, 4)); continue
                data = resp.json()
                if data["code"] != 0:
                    self.log(f"  [错误] 热门第{page}页: {data.get('message', '未知错误')}")
                    with self.stats_lock: self.search_fail_count += 1
                    return None
                return data.get("data", {})
            except requests.RequestException as e:
                if attempt < max_retries - 1:
                    self.log(f"  [异常] 热门第{page}页 请求异常，重试 ({attempt+1}/{max_retries})...")
                    time.sleep(random.uniform(2, 4))
                else:
                    self.log(f"  [失败] 热门第{page}页 请求失败: {e}")
                    with self.stats_lock: self.search_fail_count += 1
                    return None
        self.log(f"  [失败] 热门第{page}页 超过最大重试次数")
        with self.stats_lock: self.search_fail_count += 1
        return None

    def search_page(self, keyword, page, order="click", max_retries=3):
        params = {"search_type": "video", "keyword": keyword, "page": page, "order": order, "platform": "pc"}
        for attempt in range(max_retries):
            headers = self.cookie_mgr.get_headers("https://search.bilibili.com/")
            try:
                signed_params = self.get_wbi_signed_params(params.copy(), headers)
                url = f"https://api.bilibili.com/x/web-interface/search/type?{urlencode(signed_params)}"
                resp = requests.get(url, headers=headers, timeout=15, verify=False)
                if resp.status_code == 412:
                    wait = (attempt + 1) * 5
                    self.log(f"  [限流] 第{page}页 等待 {wait} 秒后重试 ({attempt+1}/{max_retries})...")
                    time.sleep(wait); continue
                if resp.status_code != 200:
                    self.log(f"  [错误] 第{page}页 HTTP {resp.status_code}，重试 ({attempt+1}/{max_retries})...")
                    time.sleep(random.uniform(2, 4)); continue
                data = resp.json()
                if data["code"] != 0:
                    msg = data.get("message", "未知错误")
                    if "搜索请求超时" in msg:
                        time.sleep(random.uniform(2, 4)); continue
                    self.log(f"  [错误] 第{page}页: {msg}")
                    with self.stats_lock: self.search_fail_count += 1
                    return None
                return data.get("data", {})
            except requests.RequestException as e:
                if attempt < max_retries - 1:
                    time.sleep(random.uniform(2, 4))
                else:
                    self.log(f"  [失败] 第{page}页 搜索请求失败: {e}")
                    with self.stats_lock: self.search_fail_count += 1
                    return None
        with self.stats_lock: self.search_fail_count += 1
        return None

    # ==================== 视频详情 ====================

    def get_video_detail(self, av_id, max_retries=3):
        last_error = None
        for attempt in range(max_retries):
            headers = self.cookie_mgr.get_headers()
            try:
                params = {"aid": int(av_id)}
                signed_params = self.get_wbi_signed_params(params, headers)
                url = f"https://api.bilibili.com/x/web-interface/view?{urlencode(signed_params)}"
                resp = requests.get(url, headers=headers, timeout=15, verify=False)
                if resp.status_code == 412:
                    last_error = f"412限流(第{attempt+1}次)"; time.sleep((attempt + 1) * 3); continue
                if resp.status_code != 200:
                    last_error = f"HTTP {resp.status_code}"; time.sleep(random.uniform(1, 3)); continue
                data = resp.json()
                if data["code"] == -404:
                    return None, "视频不存在(-404)"
                if data["code"] != 0:
                    msg = data.get("message", "未知错误")
                    last_error = f"API错误: {msg}"
                    if "频率" in msg or "风控" in msg:
                        time.sleep(random.uniform(3, 6)); continue
                    return None, last_error
                video_data = data["data"]; stat = video_data["stat"]; owner = video_data.get("owner", {})
                uploader_uid = str(owner.get("mid", ""))
                uploader_fans = 0
                if uploader_uid:
                    uploader_fans = self._fetch_uploader_fans(uploader_uid, headers)
                tags = []
                if video_data.get("tags"):
                    tags = [t.get("tag_name", "") for t in video_data.get("tags", [])]
                if not tags:
                    try:
                        tag_params = {"aid": int(av_id)}
                        signed_tag_params = self.get_wbi_signed_params(tag_params, headers)
                        tag_url = f"https://api.bilibili.com/x/tag/archive/tags?{urlencode(signed_tag_params)}"
                        tag_resp = requests.get(tag_url, headers=headers, timeout=10, verify=False)
                        if tag_resp.status_code == 200:
                            tag_data = tag_resp.json()
                            if tag_data.get("code") == 0 and tag_data.get("data"):
                                tags = [t.get("tag_name", "") for t in tag_data["data"] if isinstance(t, dict) and t.get("tag_name")]
                    except Exception: pass
                pubdate_ts = video_data.get("pubdate", 0)
                pubdate_str = datetime.fromtimestamp(pubdate_ts).strftime("%Y-%m-%d %H:%M:%S") if pubdate_ts else None
                video_age_hours = 0; play_velocity = 0
                if pubdate_ts > 0:
                    video_age_hours = max((time.time() - pubdate_ts) / 3600, 0.1)
                    play_velocity = round(stat.get("view", 0) / video_age_hours, 2)
                total_interactions = (stat.get("like", 0) + stat.get("coin", 0) + stat.get("favorite", 0) + stat.get("reply", 0) + stat.get("danmaku", 0))
                engagement_score = round(total_interactions / max(stat.get("view", 1), 1), 4)
                return {
                    "av_id": str(av_id), "bvid": video_data.get("bvid"),
                    "title": video_data.get("title", ""), "url": f"http://www.bilibili.com/video/av{av_id}",
                    "play_nums": stat.get("view", 0), "danmakus": stat.get("danmaku", 0),
                    "favorites": stat.get("favorite", 0), "review": stat.get("reply", 0),
                    "coin": stat.get("coin", 0), "share": stat.get("share", 0),
                    "like_count": stat.get("like", 0), "uploader": owner.get("name", ""),
                    "uploader_uid": uploader_uid, "uploader_fans": uploader_fans,
                    "uploader_level": owner.get("level", 0),
                    "uploader_verified": 1 if owner.get("official", {}).get("role") else 0,
                    "pubdate": pubdate_str, "duration": video_data.get("duration", 0),
                    "description": video_data.get("desc", ""),
                    "tags": ",".join(tags) if tags else None, "category": video_data.get("tname", ""),
                    "video_age_hours": round(video_age_hours, 2), "play_velocity": play_velocity,
                    "engagement_score": engagement_score,
                }, None
            except requests.RequestException as e:
                last_error = f"请求异常: {str(e)[:50]}"
                if attempt < max_retries - 1: time.sleep(random.uniform(1, 3))
                else: return None, last_error
        return None, last_error or "超过最大重试次数"

    # ==================== 粉丝三级回退 ====================

    def _fetch_uploader_fans(self, uid, headers, max_retries=2):
        try:
            cursor = self.db.conn.cursor()
            cursor.execute("SELECT fans FROM bili_uploaders WHERE uid = ? AND fetched_at > datetime('now', '-7 days')", (uid,))
            cached = cursor.fetchone()
            if cached and cached[0] > 0: return cached[0]
        except Exception: pass
        try:
            cursor = self.db.conn.cursor()
            cursor.execute("SELECT uploader_fans FROM bili_videos WHERE uploader_uid = ? AND uploader_fans > 0 LIMIT 1", (uid,))
            cached_video = cursor.fetchone()
            if cached_video and cached_video[0] > 0: return cached_video[0]
        except Exception: pass
        try:
            self._fetch_uploader_fans_via_api(uid, headers)
            cursor = self.db.conn.cursor()
            cursor.execute("SELECT fans FROM bili_uploaders WHERE uid = ?", (uid,))
            result = cursor.fetchone()
            if result and result[0] > 0: return result[0]
        except Exception: pass
        return 0

    def _fetch_uploader_fans_via_api(self, uid, headers):
        result = self._fans_via_card_api(uid, headers)
        if result is not None: return result
        self.log(f"  [粉丝] UID={uid} card API失败，尝试 relation API")
        time.sleep(random.uniform(0.3, 0.6))
        result = self._fans_via_relation_api(uid, headers)
        if result is not None: return result
        self.log(f"  [粉丝] UID={uid} relation API失败，尝试 Playwright")
        time.sleep(random.uniform(0.5, 1.0))
        result = self._fans_via_playwright(uid)
        if result is not None: return result
        self.log(f"  [粉丝] UID={uid} 三级回退全部失败")
        return None

    def _fans_via_card_api(self, uid, headers):
        try:
            params = {"mid": int(uid)}
            api_url = f"https://api.bilibili.com/x/web-interface/card?{urlencode(params)}"
            resp = requests.get(api_url, headers=headers, timeout=10, verify=False)
            if resp.status_code == 200:
                data = resp.json()
                if data.get("code") == 0:
                    card = data.get("data", {}).get("card", {})
                    fans = card.get("fans", 0)
                    if fans > 0:
                        self._update_uploader_fans(uid, fans, card.get("name", ""))
                        return fans
                else:
                    self.log(f"  [粉丝-card] UID={uid} code={data.get('code')} msg={data.get('message', '')[:60]}")
            else:
                self.log(f"  [粉丝-card] UID={uid} HTTP {resp.status_code}")
        except Exception as e:
            self.log(f"  [粉丝-card] UID={uid} 异常: {e}")
        return None

    def _fans_via_relation_api(self, uid, headers):
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
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.log("  [粉丝-PW] playwright 未安装，跳过")
            return None
        with self._pw_lock:
            try:
                if self._pw is None:
                    self._pw = sync_playwright().start()
                    try: self._pw_browser = self._pw.chromium.launch(headless=True)
                    except Exception: self._pw_browser = self._pw.chromium.launch(headless=True, channel="msedge")
                context = self._pw_browser.new_context(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    viewport={"width": 1280, "height": 720})
                page = context.new_page()
                page.goto(f"https://space.bilibili.com/{uid}", wait_until="domcontentloaded", timeout=15000)
                page.wait_for_timeout(3000)
                fans = 0; name = ""
                try:
                    fan_link = page.query_selector(f'a[href="/{uid}/fans/fans"]')
                    if fan_link:
                        title_attr = fan_link.get_attribute("title")
                        if title_attr: fans = int(''.join(filter(str.isdigit, title_attr)))
                    if fans == 0:
                        fan_text_el = page.query_selector(f'a[href="/{uid}/fans/fans"] .n-data-v')
                        if fan_text_el:
                            text = fan_text_el.inner_text().strip().replace(',', '').replace(' ', '')
                            if '万' in text: fans = int(float(text.replace('万', '')) * 10000)
                            else: fans = int(''.join(filter(str.isdigit, text)))
                    name_el = page.query_selector('#h-name, .h-name, .user-name')
                    if name_el: name = name_el.inner_text().strip()
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
                    if self._pw_browser: self._pw_browser.close()
                    if self._pw: self._pw.stop()
                except Exception: pass
                self._pw = None; self._pw_browser = None
        return None

    def _close_playwright(self):
        with self._pw_lock:
            try:
                if self._pw_browser: self._pw_browser.close()
                if self._pw: self._pw.stop()
            except Exception: pass
            self._pw = None; self._pw_browser = None

    def _update_uploader_fans(self, uid, fans, name=None):
        try:
            cursor = self.db.conn.cursor()
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            if name:
                cursor.execute("""INSERT INTO bili_uploaders (uid, name, fans, fetched_at) VALUES (?, ?, ?, ?)
                    ON CONFLICT(uid) DO UPDATE SET name=excluded.name, fans=excluded.fans, fetched_at=excluded.fetched_at""", (uid, name, fans, now))
            else:
                cursor.execute("UPDATE bili_uploaders SET fans = ?, fetched_at = ? WHERE uid = ?", (fans, now, uid))
            self.db.conn.commit()
        except Exception: pass

    # ==================== 评论 & 数据补全 ====================

    def fetch_video_detail(self, av_id):
        time.sleep(random.uniform(0.5, self.args.delay))
        detail, error = self.get_video_detail(av_id)
        if detail:
            comment_data = self.fetch_video_comments(av_id, detail.get("play_nums", 0), detail.get("pubdate"))
            if comment_data: detail.update(comment_data)
        return av_id, detail, error

    def fetch_video_comments(self, av_id, play_nums, pubdate=None, max_retries=3):
        for attempt in range(max_retries):
            headers = self.cookie_mgr.get_headers(); headers['Referer'] = 'https://www.bilibili.com/'
            try:
                params = {"type": 1, "oid": int(av_id), "mode": 3, "next": 0}
                signed_params = self.get_wbi_signed_params(params, headers)
                comment_url = f"https://api.bilibili.com/x/v2/reply/main?{urlencode(signed_params)}"
                resp = requests.get(comment_url, headers=headers, timeout=15, verify=False)
                if resp.status_code == 412:
                    wait = (attempt + 1) * 3
                    if attempt < max_retries - 1:
                        self.log(f"  [评论限流] av{av_id} 等待 {wait} 秒后重试 ({attempt+1}/{max_retries})...")
                        time.sleep(wait); continue
                    else: return None
                if resp.status_code != 200:
                    if attempt < max_retries - 1: time.sleep(random.uniform(1, 3)); continue
                    else: return None
                data = resp.json()
                if data.get("code") != 0:
                    if attempt < max_retries - 1: time.sleep(random.uniform(1, 3)); continue
                    else: return None
                reply_data = data.get('data', {}); replies = reply_data.get('replies', []) or []
                if not replies: return None
                earliest_ctime = replies[-1].get('ctime', 0)
                if not earliest_ctime: return None
                if pubdate:
                    try:
                        pub_dt = datetime.strptime(pubdate.split('.')[0], '%Y-%m-%d %H:%M:%S')
                        pub_ts = int(pub_dt.timestamp()); actual_start = min(earliest_ctime, pub_ts)
                    except Exception: actual_start = earliest_ctime
                else: actual_start = earliest_ctime
                now = int(time.time()); time_span_hours = (now - actual_start) / 3600
                play_velocity = play_nums / time_span_hours if time_span_hours > 0 and play_nums > 0 else 0
                first_comment_time = datetime.fromtimestamp(actual_start).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
                return {"first_comment_time": first_comment_time, "play_velocity": round(play_velocity, 2), "time_span_hours": round(time_span_hours, 2)}
            except Exception as e:
                if attempt < max_retries - 1:
                    self.log(f"  [评论异常] av{av_id} 重试 ({attempt+1}/{max_retries}): {e}")
                    time.sleep(random.uniform(1, 3))
                else:
                    self.log(f"  [评论获取失败] {av_id}: {e}"); return None
        return None

    def fix_comment_count_from_review(self):
        cursor = self.db.conn.cursor()
        cursor.execute("UPDATE bili_videos SET comment_count = review WHERE review > 0 AND (comment_count = 0 OR comment_count <= 20)")
        fixed = cursor.rowcount; self.db.conn.commit()
        if fixed > 0: self.log(f"[修复] 已修正 {fixed} 条视频的评论数 (comment_count <- review)")

    def enrich_videos_with_fans(self, keyword=None):
        self.log(f"\n{'='*60}\n  自动检测并补充粉丝数\n{'='*60}")
        uploaders = self.db.get_uploaders_with_zero_fans(keyword)
        self.log(f"待补充UP主数: {len(uploaders)}")
        if not uploaders:
            self.log("[完成] 所有UP主已有粉丝数据"); return
        success = 0; fail = 0
        for i, (uid, name) in enumerate(uploaders, 1):
            if i % 10 == 0: self.log(f"  进度: {i}/{len(uploaders)}")
            headers = self.cookie_mgr.get_headers()
            fans = self._fetch_uploader_fans_via_api(uid, headers)
            if fans and fans > 0:
                updated = self.db.batch_update_uploader_fans(uid, fans, name)
                success += 1; self.log(f"  [{i}/{len(uploaders)}] UID={uid} ({name}) -> {fans:,} (更新{updated}条视频)")
            else:
                fail += 1; self.log(f"  [{i}/{len(uploaders)}] UID={uid} ({name}) -> 获取失败")
            time.sleep(random.uniform(0.3, 0.8))
        self.log(f"\n[完成] 粉丝数补充: 成功 {success}, 失败 {fail}")

    def enrich_videos_with_tags(self, keyword=None):
        self.log(f"\n{'='*60}\n  自动检测并补充视频标签\n{'='*60}")
        cursor = self.db.conn.cursor()
        cursor.execute("""SELECT v.av_id FROM bili_videos v JOIN spider_tasks t ON v.task_id = t.id
            WHERE t.keyword = ? AND (v.tags IS NULL OR v.tags = '')""", (keyword,))
        missing = [row[0] for row in cursor.fetchall()]
        self.log(f"待补充标签视频数: {len(missing)}")
        if not missing:
            self.log("[完成] 所有视频已有标签数据"); return
        success = 0; fail = 0
        for i, av_id in enumerate(missing, 1):
            if i % 10 == 0: self.log(f"  进度: {i}/{len(missing)}")
            headers = self.cookie_mgr.get_headers()
            try:
                params = {"aid": int(av_id)}
                signed_params = self.get_wbi_signed_params(params, headers)
                url = f"https://api.bilibili.com/x/tag/archive/tags?{urlencode(signed_params)}"
                resp = requests.get(url, headers=headers, timeout=15, verify=False)
                if resp.status_code == 412:
                    self.log(f"  [{i}/{len(missing)}] av{av_id} 412限流，等待5秒")
                    time.sleep(5); fail += 1; continue
                if resp.status_code != 200: fail += 1; continue
                data = resp.json()
                if data.get("code") != 0: fail += 1; continue
                raw_tags = data.get("data") or []
                tags = [t.get("tag_name", "") for t in raw_tags if isinstance(t, dict) and t.get("tag_name")]
                tags_str = ",".join(tags) if tags else ""
                if tags_str:
                    cursor.execute("UPDATE bili_videos SET tags = ? WHERE av_id = ?", (tags_str, av_id))
                    self.db.conn.commit(); success += 1
                    self.log(f"  [{i}/{len(missing)}] av{av_id} -> {len(tags)}个标签")
                else: fail += 1; self.log(f"  [{i}/{len(missing)}] av{av_id} -> 无标签")
            except Exception as e:
                fail += 1; self.log(f"  [{i}/{len(missing)}] av{av_id} -> 异常: {str(e)[:50]}")
            time.sleep(random.uniform(0.3, 0.8))
        self.log(f"\n[完成] 标签补充: 成功 {success}, 失败 {fail}")

    # ==================== 爬取主流程 ====================

    def _fetch_and_insert_details(self, new_ids, log_prefix="搜索"):
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
                        with self.stats_lock: self.success_count += 1
                        self.log(f"  [{i}/{len(new_ids)}] av{av_id} ✓")
                    else:
                        with self.stats_lock: self.fail_count += 1
                        self.db.insert_failed_video(self.task_id, av_id, "detail_fetch_failed", error or "未知错误")
                        self.log(f"  [{i}/{len(new_ids)}] av{av_id} ✗ ({error or '未知错误'})")
                except Exception as e:
                    with self.stats_lock: self.fail_count += 1
                    self.db.insert_failed_video(self.task_id, av_id, "exception", str(e)[:100])
                    self.log(f"  [{i}/{len(new_ids)}] av{av_id} ✗ (异常: {e})")
        self.db.update_task(self.task_id, status="completed", success_count=self.success_count,
            fail_count=self.fail_count, completed_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        self.log(f"\n[{log_prefix}完成] 成功: {self.success_count}, 失败: {self.fail_count}")

    def run_popular(self):
        self.log("=" * 60 + "\n  B站全站热门视频爬取\n" + "=" * 60)
        self.log(f"页数: {self.args.pages}\n线程数: {self.args.threads}\n延迟: {self.args.delay}s\n")
        keyword = "全站热门"
        self.task_id = self.db.create_task(keyword, self.args.pages, "popular")
        self.log(f"[任务] 任务ID: {self.task_id}")
        all_av_ids = set(); failed_pages = []
        for page in range(1, self.args.pages + 1):
            self.log(f"[热门] 第 {page}/{self.args.pages} 页...")
            data = self.fetch_popular_page(page)
            if not data:
                failed_pages.append(page); self.log(f"  失败，跳过")
                time.sleep(random.uniform(2, 4)); continue
            results = data.get("list", [])
            if not results:
                self.log(f"  无结果，可能已到末页"); break
            new_count = 0
            for item in results:
                av_id = str(item.get("aid", ""))
                if av_id and av_id not in all_av_ids: all_av_ids.add(av_id); new_count += 1
            total = data.get("no_more", False)
            self.log(f"  找到 {len(results)} 个，新增 {new_count} 个" + (" (已到底)" if total else ""))
            time.sleep(random.uniform(self.args.delay, self.args.delay + 1))
            if total:
                self.log(f"  热门列表已到底，停止翻页"); break
        existing_ids = self.db.get_existing_av_ids()
        new_ids = list(all_av_ids - existing_ids)
        self.log(f"\n[搜索完成] 共 {len(all_av_ids)} 个视频，已有 {len(all_av_ids) - len(new_ids)} 个，新增 {len(new_ids)} 个")
        self.log(f"[搜索统计] 失败页数: {len(failed_pages)}")
        if not new_ids:
            self.log("[完成] 没有新视频需要爬取")
            self.db.update_task(self.task_id, status="completed", total_videos=len(all_av_ids)); return
        self._fetch_and_insert_details(new_ids, "热门")

    def run(self):
        self.log("=" * 60 + "\n  B站视频爬虫 (增强版)\n" + "=" * 60)
        self.log(f"关键词: {self.args.keyword}\n页数: {self.args.pages}\n排序: {self.args.order}\n线程数: {self.args.threads}\n延迟: {self.args.delay}s\n")
        self.task_id = self.db.create_task(self.args.keyword, self.args.pages, self.args.order)
        self.log(f"[任务] 任务ID: {self.task_id}")
        all_av_ids = set(); failed_pages = []
        for page in range(1, self.args.pages + 1):
            self.log(f"[搜索] 第 {page}/{self.args.pages} 页...")
            data = self.search_page(self.args.keyword, page, self.args.order)
            if not data:
                failed_pages.append(page); self.log(f"  失败，跳过")
                time.sleep(random.uniform(2, 4)); continue
            results = data.get("result", [])
            if not results:
                self.log(f"  无结果，可能已到末页"); break
            new_count = 0
            for item in results:
                av_id = str(item.get("aid", ""))
                if av_id and av_id not in all_av_ids: all_av_ids.add(av_id); new_count += 1
            self.log(f"  找到 {len(results)} 个，新增 {new_count} 个")
            time.sleep(random.uniform(self.args.delay, self.args.delay + 1))
        existing_ids = self.db.get_existing_av_ids()
        new_ids = list(all_av_ids - existing_ids)
        self.log(f"\n[搜索完成] 共 {len(all_av_ids)} 个视频，今日已爬 {len(all_av_ids) - len(new_ids)} 个，新增 {len(new_ids)} 个")
        self.log(f"[搜索统计] 搜索失败页数: {len(failed_pages)}")
        if not new_ids:
            self.log("[完成] 没有新视频需要爬取")
            self.db.update_task(self.task_id, status="completed", total_videos=len(all_av_ids)); return
        self._fetch_and_insert_details(new_ids, "搜索")

    def close(self):
        self._close_playwright()
        self.db.close()

    # ==================== 动量分析 ====================

    def analyze_momentum(self):
        keyword = self.args.keyword
        metric = "play_nums"
        limit = 999999
        self.log(f"\n{'=' * 90}\n  动量分析 (单次快照模式)\n{'=' * 90}")
        self.log(f"关键词: {keyword}\n显示数量: {limit}")
        self.log(f"评分维度: 播放速率(30%) + 粉丝转化(25%) + 互动密度(20%) + 新鲜度(15%) + 播放量(10%)\n")
        ranking = self.db.get_keyword_momentum_ranking(keyword, metric, limit)
        if not ranking:
            self.log("[动量分析] 无数据可分析"); return
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
            age_str = f"{age_hours/24:.0f}天" if age_hours >= 24 else f"{age_hours:.0f}h"
            growth = f"{item['historical_growth_pct']:.1f}%" if item.get('historical_growth_pct') is not None else "N/A"
            score = f"{item['composite_score']:.3f}"
            self.log(f"{i:<4} {title:<28} {current:>10} {uploader:<10} {fans:>8} {conv:>7} {velocity:>9} {engagement:>8} {age_str:>8} {growth:>8} {score:>7}")
        self.log("-" * 180)
        self.log("\n提示: 综合分 = 播放速率(30%) + 粉丝转化(25%) + 互动密度(20%) + 新鲜度(15%) + 播放量(10%)")
        self.log("      历史增长列显示多次爬取后的真实增长率，首次爬取为 N/A")
        self._export_momentum_csv(ranking, keyword, metric)
        self._print_tag_ranking_momentum(ranking, keyword)

    def _export_momentum_csv(self, ranking, keyword, metric):
        output_dir = os.path.join("output", keyword)
        os.makedirs(output_dir, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_file = os.path.join(output_dir, f"momentum_{timestamp}.csv")
        with open(output_file, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow([
                "排名", "av_id", "标题", "播放量", "UP主", "UP主UID", "粉丝数", "粉丝转化率",
                "播放速率(次/小时)", "视频年龄(小时)", "互动密度", "评论数", "标签",
                "速率得分", "转化得分", "互动得分", "新鲜度得分", "播放量得分",
                "历史增长%", "数据点数量", "发布时间", "综合评分"])
            for i, item in enumerate(ranking, 1):
                writer.writerow([
                    i, item["av_id"], item["title"], item.get("current_value", 0),
                    item.get("uploader", ""), item.get("uploader_uid", ""),
                    item.get("uploader_fans", 0), item.get("conversion_rate", 0),
                    item.get("play_velocity", 0), item.get("video_age_hours", 0),
                    item.get("engagement_score", 0), item.get("comment_count", 0),
                    item.get("tags", ""), item.get("velocity_score", 0),
                    item.get("conversion_score", 0), item.get("engagement_norm_score", 0),
                    item.get("freshness_normalized", 0), item.get("normalized_value", 0),
                    item.get("historical_growth_pct", "N/A"), item.get("data_points", 1),
                    item.get("pubdate", ""), round(item.get("composite_score", 0), 4)])
        self.log(f"\n[导出] 动量分析结果已保存到 {output_file}")

    def _print_tag_ranking_momentum(self, ranking, keyword):
        total_videos = len(ranking)
        tag_stats = defaultdict(lambda: {"count": 0, "score_sum": 0.0})
        for v in ranking:
            for tag in v.get("tag_list", []):
                tag_stats[tag]["count"] += 1
                tag_stats[tag]["score_sum"] += v.get("composite_score", 0)
        if not tag_stats:
            self.log("\n[标签分析] 无标签数据"); return
        tag_weights = []
        for tag, stats in tag_stats.items():
            avg_score = stats["score_sum"] / stats["count"]
            weight = stats["count"] * avg_score
            tag_weights.append({"tag": tag, "count": stats["count"], "avg_score": avg_score,
                                "weight": weight, "pct": stats["count"] / total_videos * 100})
        tag_weights.sort(key=lambda x: x["weight"], reverse=True)
        top10 = tag_weights[:10]
        lines = [f"\n{'=' * 80}", f"  标签权重 Top 10 (关键词: {keyword})", f"{'=' * 80}",
                 f"总视频数: {total_videos} | 不同标签数: {len(tag_stats)}",
                 f"算法: 标签权重 = 出现视频数 × 平均动量分", f"{'-' * 80}",
                 f"{'排名':<4} {'标签':<20} {'出现次数':>8} {'覆盖率':>8} {'平均动量分':>10} {'标签权重':>10}",
                 f"{'-' * 80}"]
        for i, tw in enumerate(top10, 1):
            lines.append(f"{i:<4} {tw['tag'][:18]:<20} {tw['count']:>8} {tw['pct']:>7.1f}% {tw['avg_score']:>10.3f} {tw['weight']:>10.3f}")
        lines.extend([f"{'-' * 80}", "说明: 标签权重越高 = 该标签在高动量视频中出现越频繁，越值得关注"])
        for line in lines: self.log(line)
        output_dir = os.path.join("output", keyword); os.makedirs(output_dir, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        tag_file = os.path.join(output_dir, f"tags_{timestamp}.txt")
        with open(tag_file, "w", encoding="utf-8") as f: f.write("\n".join(lines))
        self.log(f"\n[导出] 标签分析已保存到 {tag_file}")

    # ==================== 价值分析 ====================

    def analyze_value(self):
        keyword = self.args.keyword
        rows = self.db.get_keyword_videos_for_value(keyword)
        if not rows:
            self.log("[价值分析] 无数据可分析"); return
        videos = []
        for row in rows:
            av_id, title, play_nums, uploader, uploader_uid, uploader_fans, \
                like_count, coin, favorites, share_count, danmakus, review, \
                video_age_hours, pubdate, tags_str = row
            play_nums = float(play_nums or 0); uploader_fans = float(uploader_fans or 0)
            like_count = float(like_count or 0); coin = float(coin or 0)
            favorites = float(favorites or 0); share_count = float(share_count or 0)
            danmakus = float(danmakus or 0); review = float(review or 0)
            video_age_hours = float(video_age_hours or 0)
            deep_ratio = (coin + favorites) / max(like_count, 1)
            total_interaction = like_count + coin + favorites + review + danmakus
            engagement_density = total_interaction / max(play_nums, 1)
            fav_rate = favorites / max(play_nums, 1)
            conv_rate = play_nums / max(uploader_fans, 1) if uploader_fans > 0 else 0
            share_rate = share_count / max(play_nums, 1)
            videos.append({
                "av_id": av_id, "title": title, "play_nums": play_nums,
                "uploader": uploader, "uploader_uid": uploader_uid, "uploader_fans": uploader_fans,
                "like_count": like_count, "coin": coin, "favorites": favorites,
                "share": share_count, "danmakus": danmakus, "review": review,
                "video_age_hours": video_age_hours, "pubdate": pubdate,
                "tags": tags_str or "",
                "tag_list": [t.strip() for t in (tags_str or "").split(",") if t.strip()],
                "deep_ratio": deep_ratio, "engagement_density": engagement_density,
                "fav_rate": fav_rate, "conv_rate": conv_rate, "share_rate": share_rate})
        def normalize(values):
            min_v = min(values); max_v = max(values)
            if max_v == min_v: return [0.5] * len(values)
            return [(v - min_v) / (max_v - min_v) for v in values]
        deep_norm = normalize([v["deep_ratio"] for v in videos])
        eng_norm = normalize([v["engagement_density"] for v in videos])
        fav_norm = normalize([v["fav_rate"] for v in videos])
        conv_norm = normalize([v["conv_rate"] for v in videos])
        share_norm = normalize([v["share_rate"] for v in videos])
        for i, v in enumerate(videos):
            v["deep_score"] = deep_norm[i]; v["eng_score"] = eng_norm[i]
            v["fav_score"] = fav_norm[i]; v["conv_score"] = conv_norm[i]
            v["share_score"] = share_norm[i]
            v["value_score"] = (deep_norm[i] * 0.35 + eng_norm[i] * 0.25 +
                                fav_norm[i] * 0.20 + conv_norm[i] * 0.10 + share_norm[i] * 0.10)
        videos.sort(key=lambda x: x["value_score"], reverse=True)
        self._print_value_ranking(videos, keyword)
        self._print_tag_ranking_value(videos, keyword)
        self._export_value_csv(videos, keyword)

    def _print_value_ranking(self, videos, keyword):
        self.log(f"\n{'=' * 100}\n  价值分析 (独立评分)\n{'=' * 100}")
        self.log(f"关键词: {keyword}")
        self.log(f"评分维度: 深度互动比(35%) + 互动密度(25%) + 收藏率(20%) + 粉丝转化(10%) + 分享率(10%)")
        self.log(f"说明: 价值评分衡量视频的长期内容质量，不受视频年龄/新鲜度影响\n")
        limit = min(self.args.limit if hasattr(self.args, 'limit') else 999999, len(videos))
        self.log(f"\n价值排行 Top {limit}（按价值综合评分排序）")
        self.log("-" * 190)
        header = (f"{'排名':<4} {'标题':<28} {'播放量':>10} {'UP主':<10} {'粉丝':>8} "
                  f"{'深/浅比':>7} {'互动密度':>8} {'收藏率':>7} {'转化':>7} {'分享率':>7} "
                  f"{'深互动分':>7} {'互动分':>6} {'收藏分':>6} {'转化分':>6} {'分享分':>6} {'价值分':>7}")
        self.log(header)
        self.log("-" * 190)
        for i, item in enumerate(videos[:limit], 1):
            title = _safe_str(item["title"] or "未知", 26)
            current = f"{int(item['play_nums']):,}" if item['play_nums'] else "-"
            uploader = _safe_str(item.get('uploader') or item.get('uploader_uid') or '未知', 9)
            fans = f"{int(item['uploader_fans']):,}" if item.get('uploader_fans') else "-"
            like = item.get('like_count', 0)
            deep = item.get('coin', 0) + item.get('favorites', 0)
            deep_light = f"{deep / max(like, 1):.2f}" if like > 0 else "-"
            engagement = f"{item['engagement_density']:.3f}" if item.get('engagement_density', 0) > 0 else "-"
            fav_rate = f"{item['fav_rate']:.3f}" if item.get('fav_rate', 0) > 0 else "-"
            conv = f"{item['conv_rate']:.1f}x" if item.get('conv_rate', 0) > 0 else "-"
            share_rate = f"{item['share_rate']:.3f}" if item.get('share_rate', 0) > 0 else "-"
            self.log(f"{i:<4} {title:<28} {current:>10} {uploader:<10} {fans:>8} "
                     f"{deep_light:>7} {engagement:>8} {fav_rate:>7} {conv:>7} {share_rate:>7} "
                     f"{item['deep_score']:>7.3f} {item['eng_score']:>6.3f} {item['fav_score']:>6.3f} "
                     f"{item['conv_score']:>6.3f} {item['share_score']:>6.3f} {item['value_score']:>7.3f}")
        self.log("-" * 190)
        self.log("\n提示: 价值分 = 深度互动比(35%) + 互动密度(25%) + 收藏率(20%) + 粉丝转化(10%) + 分享率(10%)")
        self.log("      深度互动比 = (投币+收藏)/点赞，>1.0 表示用户愿意付出成本的认可超过浅层点赞")
        self.log("      与动量分析互补：动量看'速度'，价值看'质量'")

    def _print_tag_ranking_value(self, videos, keyword):
        total_videos = len(videos)
        tag_stats = defaultdict(lambda: {"count": 0, "score_sum": 0.0})
        for v in videos:
            for tag in v["tag_list"]:
                tag_stats[tag]["count"] += 1
                tag_stats[tag]["score_sum"] += v["value_score"]
        if not tag_stats:
            self.log("\n[标签分析] 无标签数据"); return
        tag_weights = []
        for tag, stats in tag_stats.items():
            avg_score = stats["score_sum"] / stats["count"]
            weight = stats["count"] * avg_score
            tag_weights.append({"tag": tag, "count": stats["count"], "avg_score": avg_score,
                                "weight": weight, "pct": stats["count"] / total_videos * 100})
        tag_weights.sort(key=lambda x: x["weight"], reverse=True)
        top10 = tag_weights[:10]
        lines = [f"\n{'=' * 80}", f"  标签权重 Top 10 (关键词: {keyword})", f"{'=' * 80}",
                 f"总视频数: {total_videos} | 不同标签数: {len(tag_stats)}",
                 f"算法: 标签权重 = 出现视频数 × 平均价值分", f"{'-' * 80}",
                 f"{'排名':<4} {'标签':<20} {'出现次数':>8} {'覆盖率':>8} {'平均价值分':>10} {'标签权重':>10}",
                 f"{'-' * 80}"]
        for i, tw in enumerate(top10, 1):
            lines.append(f"{i:<4} {_safe_str(tw['tag'], 18):<20} {tw['count']:>8} {tw['pct']:>7.1f}% {tw['avg_score']:>10.3f} {tw['weight']:>10.3f}")
        lines.extend([f"{'-' * 80}", "说明: 标签权重越高 = 该标签在高质量视频中出现越频繁，越值得重点关注"])
        for line in lines: self.log(line)
        output_dir = os.path.join("output", keyword); os.makedirs(output_dir, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        tag_file = os.path.join(output_dir, f"tags_{timestamp}.txt")
        with open(tag_file, "w", encoding="utf-8") as f: f.write("\n".join(lines))
        self.log(f"\n[导出] 标签分析已保存到 {tag_file}")

    def _export_value_csv(self, videos, keyword):
        output_dir = os.path.join("output", keyword); os.makedirs(output_dir, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_file = os.path.join(output_dir, f"value_{timestamp}.csv")
        with open(output_file, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow([
                "排名", "av_id", "标题", "播放量", "UP主", "UP主UID", "粉丝数",
                "点赞数", "投币数", "收藏数", "分享数", "弹幕数", "评论数",
                "标签", "深度互动比(币+藏/赞)", "互动密度", "收藏率", "粉丝转化率", "分享率",
                "深度互动得分", "互动密度得分", "收藏率得分", "转化率得分", "分享率得分",
                "视频年龄(小时)", "发布时间", "价值综合评分"])
            for i, item in enumerate(videos, 1):
                writer.writerow([
                    i, item["av_id"], item["title"], int(item.get("play_nums", 0)),
                    item.get("uploader", ""), item.get("uploader_uid", ""),
                    int(item.get("uploader_fans", 0)), int(item.get("like_count", 0)),
                    int(item.get("coin", 0)), int(item.get("favorites", 0)),
                    int(item.get("share", 0)), int(item.get("danmakus", 0)),
                    int(item.get("review", 0)), item.get("tags", ""),
                    round(item.get("deep_ratio", 0), 4), round(item.get("engagement_density", 0), 4),
                    round(item.get("fav_rate", 0), 4), round(item.get("conv_rate", 0), 4),
                    round(item.get("share_rate", 0), 4), round(item.get("deep_score", 0), 4),
                    round(item.get("eng_score", 0), 4), round(item.get("fav_score", 0), 4),
                    round(item.get("conv_score", 0), 4), round(item.get("share_score", 0), 4),
                    round(item.get("video_age_hours", 0), 2), item.get("pubdate", ""),
                    round(item.get("value_score", 0), 4)])
        self.log(f"\n[导出] 价值分析结果已保存到 {output_file}")
