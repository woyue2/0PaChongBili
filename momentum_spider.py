import requests
import argparse
import time
import random
import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urlencode

from util import (
    DB_FILE, COOKIE_FILE,
    CookieManager, Database, WbiSigner,
)

requests.packages.urllib3.disable_warnings()


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

    def get_wbi_signed_params(self, params, headers=None):
        """WBI 签名（委托给 WbiSigner）"""
        if headers is None:
            headers = self.cookie_mgr.get_headers()
        return WbiSigner.sign(params, headers)

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
                # 视频详情API的tags可能为None，用标签专用API补充
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
                    except Exception:
                        pass

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

    def enrich_videos_with_tags(self, keyword=None):
        """自动检测并补充缺失标签的视频数据"""
        self.log(f"\n{'='*60}")
        self.log(f"  自动检测并补充视频标签")
        self.log(f"{'='*60}")

        # 查询该关键词下标签为空的视频
        cursor = self.db.conn.cursor()
        cursor.execute("""
            SELECT v.av_id FROM bili_videos v
            JOIN spider_tasks t ON v.task_id = t.id
            WHERE t.keyword = ? AND (v.tags IS NULL OR v.tags = '')
        """, (keyword,))
        missing = [row[0] for row in cursor.fetchall()]
        self.log(f"待补充标签视频数: {len(missing)}")

        if not missing:
            self.log("[完成] 所有视频已有标签数据")
            return

        success = 0
        fail = 0

        for i, av_id in enumerate(missing, 1):
            if i % 10 == 0:
                self.log(f"  进度: {i}/{len(missing)}")

            headers = self.cookie_mgr.get_headers()
            try:
                params = {"aid": int(av_id)}
                signed_params = self.get_wbi_signed_params(params, headers)
                url = f"https://api.bilibili.com/x/tag/archive/tags?{urlencode(signed_params)}"
                resp = requests.get(url, headers=headers, timeout=15, verify=False)

                if resp.status_code == 412:
                    self.log(f"  [{i}/{len(missing)}] av{av_id} 412限流，等待5秒")
                    time.sleep(5)
                    fail += 1
                    continue

                if resp.status_code != 200:
                    fail += 1
                    continue

                data = resp.json()
                if data.get("code") != 0:
                    fail += 1
                    continue

                raw_tags = data.get("data") or []
                tags = [t.get("tag_name", "") for t in raw_tags if isinstance(t, dict) and t.get("tag_name")]

                tags_str = ",".join(tags) if tags else ""
                if tags_str:
                    cursor.execute("UPDATE bili_videos SET tags = ? WHERE av_id = ?", (tags_str, av_id))
                    self.db.conn.commit()
                    success += 1
                    self.log(f"  [{i}/{len(missing)}] av{av_id} -> {len(tags)}个标签")
                else:
                    fail += 1
                    self.log(f"  [{i}/{len(missing)}] av{av_id} -> 无标签")

            except Exception as e:
                fail += 1
                self.log(f"  [{i}/{len(missing)}] av{av_id} -> 异常: {str(e)[:50]}")

            time.sleep(random.uniform(0.3, 0.8))

        self.log(f"\n[完成] 标签补充: 成功 {success}, 失败 {fail}")

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

        # 标签权重分析
        self._print_tag_ranking(ranking, keyword)

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
                "播放速率(次/小时)", "视频年龄(小时)", "互动密度", "评论数", "标签",
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
                    item.get("tags", ""),
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

    def _print_tag_ranking(self, ranking, keyword):
        """
        标签权重算法:
        对每个标签，统计含有该标签的视频数量（出现频次）和这些视频的平均动量分。
        标签权重 = 出现频次 × 平均动量分
        """
        from collections import defaultdict

        total_videos = len(ranking)
        tag_stats = defaultdict(lambda: {"count": 0, "score_sum": 0.0})

        for v in ranking:
            for tag in v.get("tag_list", []):
                tag_stats[tag]["count"] += 1
                tag_stats[tag]["score_sum"] += v.get("composite_score", 0)

        if not tag_stats:
            self.log("\n[标签分析] 无标签数据")
            return

        tag_weights = []
        for tag, stats in tag_stats.items():
            avg_score = stats["score_sum"] / stats["count"]
            weight = stats["count"] * avg_score
            tag_weights.append({
                "tag": tag,
                "count": stats["count"],
                "avg_score": avg_score,
                "weight": weight,
                "pct": stats["count"] / total_videos * 100,
            })

        tag_weights.sort(key=lambda x: x["weight"], reverse=True)
        top10 = tag_weights[:10]

        lines = []
        lines.append(f"\n{'=' * 80}")
        lines.append(f"  标签权重 Top 10 (关键词: {keyword})")
        lines.append(f"{'=' * 80}")
        lines.append(f"总视频数: {total_videos} | 不同标签数: {len(tag_stats)}")
        lines.append(f"算法: 标签权重 = 出现视频数 × 平均动量分")
        lines.append(f"{'-' * 80}")
        lines.append(f"{'排名':<4} {'标签':<20} {'出现次数':>8} {'覆盖率':>8} {'平均动量分':>10} {'标签权重':>10}")
        lines.append(f"{'-' * 80}")
        for i, tw in enumerate(top10, 1):
            tag_display = tw["tag"][:18]
            lines.append(
                f"{i:<4} {tag_display:<20} {tw['count']:>8} {tw['pct']:>7.1f}% {tw['avg_score']:>10.3f} {tw['weight']:>10.3f}"
            )
        lines.append(f"{'-' * 80}")
        lines.append("说明: 标签权重越高 = 该标签在高动量视频中出现越频繁，越值得关注")

        for line in lines:
            self.log(line)

        # 保存到文件
        output_dir = os.path.join("output", keyword)
        os.makedirs(output_dir, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        tag_file = os.path.join(output_dir, f"tags_{timestamp}.txt")
        with open(tag_file, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        self.log(f"\n[导出] 标签分析已保存到 {tag_file}")


def main():
    parser = argparse.ArgumentParser(
        description="B站视频爬虫 - 动量分析版",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python momentum_spider.py -k 穷人 -p 5              # 爬取+补粉丝+动量分析+导出
  python momentum_spider.py -k 穷人 -p 10 -t 5        # 10页5线程
  python momentum_spider.py -k 穷人 --export-only      # 仅导出原始CSV
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