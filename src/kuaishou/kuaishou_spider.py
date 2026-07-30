"""快手关键词搜索小样本试验。

借鉴 MediaCrawler 对 visionSearchPhoto 返回结构的理解，但不复制其 GraphQL
查询：由真实浏览器完成请求和站点参数处理，本模块只监听并解析浏览器收到的响应。
"""

import json
import os
import random
import sys
import threading
import time
from datetime import datetime
from urllib.parse import quote

from src.common import paths
from src.common.auth_state import AuthStateStore, LOGIN_MAX_AGE_DAYS

try:
    from .kuaishou_util import (
        COOKIE_FILE,
        DB_FILE,
        Database,
        build_kuaishou_page_url,
    )
except ImportError:
    from kuaishou_util import (
        COOKIE_FILE,
        DB_FILE,
        Database,
        build_kuaishou_page_url,
    )


def _as_int(value):
    if isinstance(value, bool) or value is None:
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip().replace(",", "")
    if not text:
        return 0
    multipliers = {"万": 10_000, "亿": 100_000_000, "w": 10_000, "W": 10_000}
    suffix = text[-1]
    try:
        if suffix in multipliers:
            return int(float(text[:-1]) * multipliers[suffix])
        return int(float(text))
    except (TypeError, ValueError):
        return 0


class KuaishouSpider:
    SEARCH_OPERATION = "visionSearchPhoto"

    def __init__(self, args, output_dir=None):
        self.args = args
        self.output_dir = output_dir
        self.db = Database(DB_FILE)
        self.auth_state = AuthStateStore()
        self.task_id = None
        self._pw = None
        self._context = None
        self._browser = None
        self._page = None
        self._api_lock = threading.Lock()
        self._search_responses = []
        self._graphql_requests = []
        self._graphql_responses = []
        self._init_logger()

    def _init_logger(self):
        log_dir = self.output_dir or paths.KUAISHOU_LOGS
        os.makedirs(log_dir, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.log_file = os.path.join(log_dir, f"kuaishou_spider_{timestamp}.log")

    def log(self, message):
        line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}"
        try:
            print(line)
        except UnicodeEncodeError:
            encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
            print(line.encode(encoding, errors="replace").decode(encoding))
        with open(self.log_file, "a", encoding="utf-8") as stream:
            stream.write(line + "\n")

    def _ensure_playwright(self):
        if self._context is not None:
            return
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        os.makedirs(os.path.dirname(paths.KUAISHOU_EDGE_PROFILE), exist_ok=True)
        headless = getattr(self.args, "headless", False)
        self._context = self._pw.chromium.launch_persistent_context(
            user_data_dir=paths.KUAISHOU_EDGE_PROFILE,
            channel="msedge",
            headless=headless,
            viewport={"width": 1920, "height": 1080},
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        self._browser = self._context.browser
        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
        self._page.on("request", self._on_request)
        self._page.on("response", self._on_response)
        self.log(
            f"[浏览器] Edge 持久化 profile 已启动 "
            f"(headless={headless}, profile={paths.KUAISHOU_EDGE_PROFILE})"
        )

    @classmethod
    def _extract_search_payload(cls, body):
        if not isinstance(body, dict):
            return {}
        data = body.get("data", body)
        if not isinstance(data, dict):
            return {}
        payload = data.get(cls.SEARCH_OPERATION)
        if isinstance(payload, dict):
            return payload

        # 快手可能修改 GraphQL operation 名，但搜索结果仍具有 feeds、
        # searchSessionId/pcursor 等稳定特征。只在 data 的直接子项中识别，
        # 避免误把其他推荐流当作关键词搜索。
        for key, candidate in data.items():
            if not isinstance(candidate, dict):
                continue
            key_lower = str(key).lower()
            looks_like_search = "search" in key_lower and (
                "photo" in key_lower or "feed" in key_lower
            )
            has_search_shape = isinstance(candidate.get("feeds"), list) and (
                "searchSessionId" in candidate
                or "pcursor" in candidate
                or "result" in candidate
            )
            if looks_like_search and has_search_shape:
                return candidate
        return {}

    @classmethod
    def _extract_feeds(cls, body):
        payload = cls._extract_search_payload(body)
        feeds = payload.get("feeds", [])
        return feeds if isinstance(feeds, list) else []

    @staticmethod
    def _feed_video_id(feed):
        if not isinstance(feed, dict):
            return ""
        photo = feed.get("photo") or {}
        return str(photo.get("id") or "")

    def _on_response(self, response):
        if "kuaishou.com" not in response.url or response.status != 200:
            return
        try:
            body = response.json()
        except Exception:
            return
        if "/graphql" in response.url:
            with self._api_lock:
                self._graphql_responses.append(
                    {
                        "url": response.url,
                        "body": body,
                        "captured_at": time.time(),
                    }
                )
        if not self._extract_search_payload(body):
            return
        with self._api_lock:
            self._search_responses.append(
                {"url": response.url, "body": body, "captured_at": time.time()}
            )

    def _on_request(self, request):
        if "kuaishou.com/graphql" not in request.url:
            return
        try:
            post_data = request.post_data or ""
            payload = json.loads(post_data) if post_data else {}
        except (TypeError, ValueError):
            payload = {}
        with self._api_lock:
            self._graphql_requests.append(
                {
                    "operation_name": payload.get("operationName", ""),
                    "variables": payload.get("variables", {}),
                    "captured_at": time.time(),
                }
            )

    def _clear_search_responses(self):
        with self._api_lock:
            self._search_responses.clear()
            self._graphql_requests.clear()
            self._graphql_responses.clear()

    def _log_graphql_debug(self):
        with self._api_lock:
            requests = list(self._graphql_requests)
            responses = list(self._graphql_responses)
        operations = []
        for request in requests:
            operation = request.get("operation_name") or "<unknown>"
            if operation not in operations:
                operations.append(operation)
        response_keys = []
        errors = []
        for response in responses:
            body = response.get("body") or {}
            data = body.get("data")
            if isinstance(data, dict):
                for key in data:
                    if key not in response_keys:
                        response_keys.append(key)
            if body.get("errors"):
                errors.append(str(body["errors"])[:300])
        self.log(
            f"[调试] GraphQL 请求={len(requests)}，响应={len(responses)}，"
            f"operations={operations[:12]}，data_keys={response_keys[:12]}"
        )
        if errors:
            self.log(f"[调试] GraphQL errors={errors[:3]}")

    @staticmethod
    def _photo_timestamp(photo):
        timestamp = _as_int(photo.get("timestamp"))
        if timestamp > 10_000_000_000:
            timestamp //= 1000
        return timestamp

    @classmethod
    def parse_search_feeds(cls, feeds):
        now = int(time.time())
        results = []
        for feed in feeds:
            if not isinstance(feed, dict):
                continue
            photo = feed.get("photo") or {}
            author = feed.get("author") or photo.get("author") or {}
            video_id = str(photo.get("id") or "")
            if not video_id:
                continue
            play_count = _as_int(
                photo.get("viewCount")
                or photo.get("view_count")
                or photo.get("playCount")
            )
            liked_count = _as_int(
                photo.get("realLikeCount")
                or photo.get("likeCount")
                or photo.get("likedCount")
            )
            comment_count = _as_int(
                photo.get("commentCount") or photo.get("comment_count")
            )
            share_count = _as_int(
                photo.get("shareCount") or photo.get("share_count")
            )
            pub_time = cls._photo_timestamp(photo)
            age_hours = max((now - pub_time) / 3600, 0) if pub_time else 0
            play_velocity = play_count / max(age_hours, 0.1) if play_count else 0
            engagement_rate = liked_count / play_count if play_count else 0
            caption = str(photo.get("caption") or photo.get("title") or "")
            results.append(
                {
                    "video_id": video_id,
                    "title": caption[:500],
                    "description": caption[:500],
                    "page_url": build_kuaishou_page_url(video_id),
                    "cover_url": photo.get("coverUrl")
                    or photo.get("cover_url")
                    or "",
                    "video_url": photo.get("photoUrl")
                    or photo.get("photo_url")
                    or "",
                    "video_type": feed.get("type", ""),
                    "play_count": play_count,
                    "liked_count": liked_count,
                    "comment_count": comment_count,
                    "share_count": share_count,
                    "author_id": str(author.get("id") or author.get("userId") or ""),
                    "nickname": author.get("name") or author.get("nickname") or "",
                    "pub_time": pub_time,
                    "video_age_hours": age_hours,
                    "play_velocity": play_velocity,
                    "engagement_rate": engagement_rate,
                }
            )
        return results

    def _latest_payload(self):
        with self._api_lock:
            if not self._search_responses:
                return {}
            return self._extract_search_payload(self._search_responses[-1]["body"])

    def _wait_for_new_items(self, seen_ids, start_index=0, timeout_s=15):
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            with self._api_lock:
                responses = list(self._search_responses[start_index:])
            feeds = []
            for response in responses:
                feeds.extend(self._extract_feeds(response["body"]))
            parsed = self.parse_search_feeds(feeds)
            new_items = [
                item for item in parsed if item["video_id"] not in seen_ids
            ]
            if new_items:
                unique = {}
                for item in new_items:
                    unique[item["video_id"]] = item
                return list(unique.values())
            self._page.wait_for_timeout(500)
        return []

    def check_login(self):
        self._ensure_playwright()
        self._page.goto(
            "https://www.kuaishou.com/",
            wait_until="domcontentloaded",
            timeout=30000,
        )
        self._page.wait_for_timeout(2500)
        cookies = {
            cookie["name"]: cookie["value"]
            for cookie in self._context.cookies("https://www.kuaishou.com")
        }
        # web_ph/did 在匿名访问时也会存在，不能据此判断登录。
        login_button = self._page.locator(".sidebar-login-button")
        login_prompt_visible = (
            login_button.count() == 1 and login_button.is_visible()
        )
        logged_in = bool(
            cookies.get("kuaishou.server.web_st") and not login_prompt_visible
        )
        self.log(
            f"[登录检测] "
            f"{'✓ 持久化会话有效' if logged_in else '✗ 登录会话无效'}"
        )
        return logged_in

    def _save_current_cookies(self):
        cookies = self._context.cookies("https://www.kuaishou.com")
        os.makedirs(os.path.dirname(COOKIE_FILE), exist_ok=True)
        with open(COOKIE_FILE, "w", encoding="utf-8") as stream:
            stream.write(
                "; ".join(f"{item['name']}={item['value']}" for item in cookies)
            )

    def _force_interactive_login(self, timeout_s=180):
        # 有头模式下直接复用当前持久化 context。关闭后立即重启同一个
        # Playwright 实例没有必要，还可能让用户误以为启动了临时 profile。
        if getattr(self.args, "headless", False):
            self.close()
            self.args.headless = False
            self._ensure_playwright()
        elif self._context is None:
            self.args.headless = False
            self._ensure_playwright()
        self._page.goto(
            "https://www.kuaishou.com/",
            wait_until="domcontentloaded",
            timeout=30000,
        )
        self.log(f"[登录刷新] 请在浏览器中登录快手，最多等待 {timeout_s} 秒")
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            cookies = {
                item["name"]: item["value"]
                for item in self._context.cookies("https://www.kuaishou.com")
            }
            if cookies.get("kuaishou.server.web_st"):
                self._save_current_cookies()
                self.auth_state.mark_login("kuaishou")
                self.log("[登录刷新] ✓ 快手登录成功")
                return True
            self._page.wait_for_timeout(1000)
        self.log("[登录刷新] 等待登录超时")
        return False

    def ensure_login_ready(self, max_age_days=LOGIN_MAX_AGE_DAYS):
        """与小红书、抖音一致：3天内复用并验证，超期才刷新登录。"""
        state = self.auth_state.get("kuaishou")
        if not self.auth_state.is_login_due(
            "kuaishou", max_age_days=max_age_days
        ):
            last_login = state.get("last_login_at") if state else "?"
            self.log(
                f"[登录门禁] 最近登录时间 {last_login}，"
                f"未超过{max_age_days}天，验证现有持久化会话"
            )
            if self.check_login():
                self.auth_state.mark_verified("kuaishou")
                self.log("[登录门禁] ✓ 无需重复登录或注入 Cookie")
                return True
            self.log("[登录门禁] 会话验证失败，转为人工刷新")
        else:
            last_login = state.get("last_login_at") if state else None
            reason = (
                f"上次登录时间 {last_login}"
                if last_login
                else "没有快手登录时间记录"
            )
            self.log(f"[登录门禁] {reason}，需要完成首次登录或刷新")
        return self._force_interactive_login()

    def _trigger_search_from_home(self, keyword):
        self.log("[搜索] 打开快手首页并通过搜索框触发搜索")
        self._page.goto(
            "https://www.kuaishou.com/",
            wait_until="domcontentloaded",
            timeout=30000,
        )
        self._page.wait_for_timeout(1500)

        selectors = (
            "input.input[placeholder='搜索你感兴趣的内容']",
            "input[placeholder='搜索你感兴趣的内容']",
            "input.input-mini[placeholder='搜索']",
        )
        search_input = None
        for selector in selectors:
            locator = self._page.locator(selector)
            if locator.count() == 1 and locator.is_visible():
                search_input = locator
                break
        if search_input is None:
            raise RuntimeError("没有找到唯一且可见的快手搜索输入框")

        search_input.click()
        search_input.fill(keyword)
        self.log(f"[搜索] 已输入关键词='{keyword}'")

        search_root = self._page.locator("div.search")
        search_button = None
        if search_root.count() == 1:
            for selector in (".icon", ".search-text"):
                candidate = search_root.locator(selector)
                if candidate.count() == 1 and candidate.is_visible():
                    search_button = candidate
                    break
        if search_button is None:
            raise RuntimeError("没有找到唯一且可见的快手搜索按钮")

        search_button.click()
        self.log("[搜索] 已点击搜索按钮")

    def _parse_visible_cards(self):
        """网络结构变化时，从已渲染作品卡片保底提取链接和文本。"""
        try:
            raw_cards = self._page.evaluate(
                """
                () => {
                    const links = Array.from(
                        document.querySelectorAll('a[href*="/short-video/"]')
                    );
                    return links.slice(0, 100).map(link => {
                        const href = link.href || '';
                        const match = href.match(/\\/short-video\\/([^/?#]+)/);
                        const card = link.closest(
                            '.video-card, .card, .photo-card, li'
                        ) || link.parentElement || link;
                        const image = card.querySelector('img');
                        return {
                            video_id: match ? match[1] : '',
                            page_url: href,
                            text: (card.innerText || link.innerText || '').trim(),
                            cover_url: image ? (image.currentSrc || image.src || '') : ''
                        };
                    }).filter(item => item.video_id);
                }
                """
            )
        except Exception as exc:
            self.log(f"[HTML降级] 读取作品卡片失败: {exc}")
            return []

        unique = {}
        for card in raw_cards or []:
            video_id = str(card.get("video_id") or "")
            if not video_id:
                continue
            lines = [
                line.strip()
                for line in str(card.get("text") or "").splitlines()
                if line.strip()
            ]
            title = lines[0] if lines else ""
            unique[video_id] = {
                "video_id": video_id,
                "title": title[:500],
                "description": title[:500],
                "page_url": card.get("page_url")
                or build_kuaishou_page_url(video_id),
                "cover_url": card.get("cover_url", ""),
                "video_url": "",
                "video_type": "dom_fallback",
                "play_count": 0,
                "liked_count": 0,
                "comment_count": 0,
                "share_count": 0,
                "author_id": "",
                "nickname": "",
                "pub_time": 0,
                "video_age_hours": 0,
                "play_velocity": 0,
                "engagement_rate": 0,
            }
        if unique:
            self.log(f"[HTML降级] 从已渲染页面提取 {len(unique)} 条作品")
        return list(unique.values())

    def _save_debug_html(self, keyword):
        target_dir = self.output_dir or paths.output(keyword)
        os.makedirs(target_dir, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        html_file = os.path.join(
            target_dir, f"kuaishou_search_debug_{timestamp}.html"
        )
        with open(html_file, "w", encoding="utf-8") as stream:
            stream.write(self._page.content())
        self.log(f"[调试] 已保存当前搜索页 HTML: {html_file}")
        return html_file

    def search_videos(self, keyword, pages=1):
        self._ensure_playwright()
        self._clear_search_responses()
        self._trigger_search_from_home(keyword)
        first_items = self._wait_for_new_items(set(), timeout_s=20)

        if not first_items:
            self._log_graphql_debug()
            first_items = self._parse_visible_cards()
        if not first_items:
            self._save_debug_html(keyword)
            self.log(
                f"[搜索] 未捕获 {self.SEARCH_OPERATION} 响应；"
                "页面也没有可解析的作品卡片"
            )
            return [], ""

        payload = self._latest_payload()
        session_id = str(payload.get("searchSessionId") or "")
        all_items = {item["video_id"]: item for item in first_items}
        self.log(f"[搜索] 首批捕获 {len(first_items)} 条作品")

        for page_number in range(2, max(int(pages), 1) + 1):
            with self._api_lock:
                start_index = len(self._search_responses)
            try:
                self._page.evaluate(
                    """
                    () => {
                        const target = document.scrollingElement
                            || document.documentElement;
                        target.scrollTop = target.scrollHeight;
                        window.dispatchEvent(new Event('scroll'));
                    }
                    """
                )
                self._page.mouse.wheel(0, 1600)
            except Exception as exc:
                self.log(f"[滚动] 第{page_number}页触发异常: {exc}")
            self._page.wait_for_timeout(int(random.uniform(1800, 2800)))
            new_items = self._wait_for_new_items(
                set(all_items), start_index=start_index, timeout_s=10
            )
            for item in new_items:
                all_items[item["video_id"]] = item
            self.log(
                f"[搜索] 第{page_number}批新增 {len(new_items)} 条，"
                f"累计 {len(all_items)} 条"
            )
            if not new_items:
                break

        return list(all_items.values()), session_id

    def crawl_keyword(self, keyword, pages=1):
        self.task_id = self.db.create_task(keyword, pages, "general")
        try:
            videos, session_id = self.search_videos(keyword, pages=pages)
            if not videos:
                self.db.update_task_status(
                    self.task_id,
                    "failed",
                    total_videos=0,
                    error_msg=f"未捕获 {self.SEARCH_OPERATION} 搜索响应",
                )
                return []
            failures = 0
            for rank, video in enumerate(videos, 1):
                try:
                    self.db.upsert_video(
                        self.task_id,
                        video,
                        search_rank=rank,
                        search_session_id=session_id,
                    )
                except Exception as exc:
                    failures += 1
                    self.db.add_failed_video(
                        self.task_id,
                        video.get("video_id", ""),
                        "store_error",
                        str(exc),
                    )
            successes = len(videos) - failures
            self.db.update_task_status(
                self.task_id,
                "completed",
                total_videos=len(videos),
                success_count=successes,
                fail_count=failures,
            )
            if self.output_dir:
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                csv_file = os.path.join(
                    self.output_dir, f"kuaishou_momentum_{timestamp}.csv"
                )
                ranking = self.db.get_keyword_momentum_ranking(keyword, limit=9999)
                self.db.export_momentum_csv(keyword, csv_file, ranking)
                self.log(f"[完成] 已保存 {successes} 条，分析结果: {csv_file}")
            return videos
        except Exception as exc:
            self.db.update_task_status(
                self.task_id, "failed", error_msg=str(exc)
            )
            raise

    def close(self):
        try:
            if self._context:
                self._context.close()
            if self._pw:
                self._pw.stop()
        except Exception:
            pass
        self._context = None
        self._browser = None
        self._page = None
        self._pw = None

    def wait_for_manual_close(self):
        if not self._context:
            return
        self.log("[浏览器] 将保持打开，请手动关闭窗口以结束程序")
        try:
            while self._browser and self._browser.is_connected() and self._context.pages:
                time.sleep(0.5)
        finally:
            self.close()
