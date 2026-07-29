"""
douyin_spider.py - 抖音爬虫主类
基于 Playwright 监听 API 响应模式，浏览器自动签名，无需维护 X-Bogus 算法。
搜索接口: /aweme/v1/web/general/search/single/
搜索URL格式: https://www.douyin.com/search/{keyword}?aid={uuid}&type=general
"""

import time
import random
import os
import sys
import uuid
import threading
from datetime import datetime
from urllib.parse import quote

try:
    from .douyin_util import (
        DB_FILE, COOKIE_FILE, CookieManager, Database, build_douyin_page_url,
    )
except ImportError:
    from douyin_util import (
        DB_FILE, COOKIE_FILE, CookieManager, Database, build_douyin_page_url,
    )
from src.common import paths


def _parse_chinese_num(s):
    if s is None:
        return 0
    if isinstance(s, (int, float)):
        return int(s)
    s = str(s).strip()
    if not s:
        return 0
    try:
        if "万" in s:
            num = float(s.replace("万", ""))
            return int(num * 10000)
        if "亿" in s:
            num = float(s.replace("亿", ""))
            return int(num * 100000000)
        return int(float(s))
    except (ValueError, TypeError):
        return 0


class DouyinSpider:
    def __init__(self, args, output_dir=None, mode="both"):
        self.args = args
        self.cookie_mgr = CookieManager(COOKIE_FILE)
        self.db = Database(DB_FILE)
        self.task_id = None
        self.output_dir = output_dir
        self.mode = mode
        self.stats_lock = threading.Lock()
        self.success_count = 0
        self.fail_count = 0
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None
        self._pw_lock = threading.Lock()
        self._api_responses = []
        self._api_lock = threading.Lock()
        self._init_logger()

    def _init_logger(self):
        if self.output_dir:
            log_dir = self.output_dir
        else:
            log_dir = paths.DOUYIN_LOGS  # 使用 logs/dy/ 目录
        os.makedirs(log_dir, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.log_file = os.path.join(log_dir, f"douyin_spider_{timestamp}.log")
        self.log(f"日志文件: {self.log_file}")

    def log(self, message):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        log_line = f"[{timestamp}] {message}"
        try:
            print(log_line)
        except UnicodeEncodeError:
            console_encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
            safe_line = log_line.encode(console_encoding, errors="replace").decode(console_encoding)
            print(safe_line)
        try:
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(log_line + "\n")
        except Exception:
            pass

    def _ensure_playwright(self):
        with self._pw_lock:
            if self._browser is not None:
                return
            from playwright.sync_api import sync_playwright
            self._pw = sync_playwright().start()

            headless = getattr(self.args, "headless", True)

            self._browser = self._pw.chromium.launch(
                channel="msedge",
                headless=headless,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                ],
            )

            cookie_str = self.cookie_mgr.get_cookie()
            cookies_for_pw = []
            if cookie_str:
                for part in cookie_str.split(";"):
                    part = part.strip()
                    if "=" in part:
                        k, v = part.split("=", 1)
                        cookies_for_pw.append({
                            "name": k.strip(),
                            "value": v.strip(),
                            "domain": ".douyin.com",
                            "path": "/",
                        })

            self._context = self._browser.new_context(
                viewport={"width": 1920, "height": 1080},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0",
                permissions=["clipboard-read", "clipboard-write"],
            )

            if cookies_for_pw:
                self._context.add_cookies(cookies_for_pw)
                self.log(f"[浏览器] 已注入 {len(cookies_for_pw)} 个 cookie")

            self._page = self._context.new_page()
            self._page.on("response", self._on_response)

            self.log(f"[浏览器] Edge 浏览器已启动 (headless={headless})")

    def _on_response(self, response):
        url = response.url
        # 放宽过滤条件，捕获所有 douyin.com 的 API 响应
        if "douyin.com" not in url or response.status != 200:
            return
        try:
            body = response.json()
            # 只记录有意义的 API 响应（包含 aweme 或 search 关键字）
            if "aweme" in url or "search" in url or "item" in url:
                with self._api_lock:
                    self._api_responses.append({
                        "url": url,
                        "status": response.status,
                        "body": body,
                    })
        except Exception:
            pass

    def _clear_api(self):
        with self._api_lock:
            self._api_responses.clear()

    @staticmethod
    def _extract_aweme_list(body):
        """从不同版本的搜索响应中提取视频列表。"""
        if not isinstance(body, dict):
            return []

        data = body.get("data", {})
        if isinstance(data, list):
            return data

        if isinstance(data, dict):
            for key in ("aweme_list", "items", "list", "aweme_detail"):
                items = data.get(key)
                if isinstance(items, list) and items:
                    return items

        items = body.get("aweme_list")
        return items if isinstance(items, list) else []

    @classmethod
    def _is_search_items_response(cls, response_data):
        url = response_data.get("url", "")
        is_search_url = (
            "/general/search/" in url
            or "/search/item/" in url
            or "/search/single/" in url
        )
        return is_search_url and bool(cls._extract_aweme_list(response_data.get("body")))

    @staticmethod
    def _item_aweme_id(item):
        if not isinstance(item, dict):
            return ""
        aweme_info = item.get("aweme_info", item)
        if not isinstance(aweme_info, dict):
            return ""
        return str(aweme_info.get("aweme_id", "") or "")

    def _wait_for_api(self, path_contains, timeout_s=15):
        start = time.time()
        while time.time() - start < timeout_s:
            with self._api_lock:
                for r in self._api_responses:
                    if path_contains in r["url"]:
                        return r
            time.sleep(0.5)
        return None

    def _find_api_with_items(self, path_keyword):
        with self._api_lock:
            # 从最新响应向前查，避免滚动后仍反复读取首屏响应。
            for r in reversed(self._api_responses):
                if path_keyword in r["url"]:
                    if self._extract_aweme_list(r["body"]):
                        return r
        return None

    def _debug_api_urls(self):
        """调试用：打印所有捕获的 API URL"""
        with self._api_lock:
            urls = [r["url"].split("?")[0] for r in self._api_responses]
            if urls:
                self.log(f"[调试] 已捕获 {len(urls)} 个 API 响应:")
                for u in urls[:10]:
                    self.log(f"  - {u}")
            else:
                self.log("[调试] 未捕获任何 API 响应")

    def _close_playwright(self):
        with self._pw_lock:
            try:
                if self._context:
                    self._context.close()
                if self._browser:
                    self._browser.close()
                if self._pw:
                    self._pw.stop()
            except Exception:
                pass
            self._context = None
            self._browser = None
            self._pw = None
            self._page = None
            self.log("[浏览器] Edge 已关闭")

    def check_login(self):
        self._ensure_playwright()
        self._clear_api()
        self.log("[登录检测] 访问抖音首页...")
        self._page.goto("https://www.douyin.com/", wait_until="domcontentloaded", timeout=30000)
        self._page.wait_for_timeout(5000)

        resp = self._wait_for_api("/user/info", timeout_s=10)
        if resp and resp["body"].get("status_code") == 0:
            data = resp["body"].get("user", {})
            nickname = data.get("nickname", "?")
            self.log(f"[登录检测] ✓ 登录正常: {nickname}")
            return True
        else:
            status = resp["body"].get("status_code") if resp else "无响应"
            self.log(f"[登录检测] ✗ 登录失效: status_code={status}")
            return False

    def _parse_search_items(self, aweme_list):
        results = []
        skipped = 0
        for idx, item in enumerate(aweme_list):
            # 抖音搜索 API 返回的是嵌套结构: {aweme_info: {...}}
            aweme_info = item.get("aweme_info", item)
            
            aweme_id = aweme_info.get("aweme_id", "")
            if not aweme_id:
                skipped += 1
                continue

            author = aweme_info.get("author", {}) or {}
            video = aweme_info.get("video", {}) or {}

            # statistics 可能在 item 外层 或 aweme_info 内层
            statistics = item.get("statistics") or aweme_info.get("statistics") or {}
            
            # 调试：打印第一个记录的结构
            if idx == 0 and len(results) == 0:
                self.log(f"[调试] item keys: {list(item.keys())}")
                self.log(f"[调试] aweme_info keys: {list(aweme_info.keys())}")
                self.log(f"[调试] statistics: {str(statistics)[:300]}")

            digg_count = int(statistics.get("digg_count", 0))
            comment_count = int(statistics.get("comment_count", 0))
            share_count = int(statistics.get("share_count", 0))
            collect_count = int(statistics.get("collect_count", 0))
            play_count = int(statistics.get("play_count", 0))

            # 如果所有统计都是 0，尝试从其他字段获取
            if digg_count == 0 and comment_count == 0 and share_count == 0 and play_count == 0:
                # 检查是否在其他位置
                if "digg_count" in item:
                    digg_count = int(item.get("digg_count", 0))
                    comment_count = int(item.get("comment_count", 0))
                    share_count = int(item.get("share_count", 0))
                    play_count = int(item.get("play_count", 0))

            interact_count = digg_count + comment_count + share_count + collect_count

            title = aweme_info.get("desc", "")
            create_time = int(aweme_info.get("create_time", 0))
            
            # 计算视频年龄（小时）
            now_ts = int(time.time())
            note_age_hours = (now_ts - create_time) / 3600 if create_time > 0 else 0
            
            # 计算互动速率（互动数/小时）
            interact_velocity = interact_count / max(note_age_hours, 0.1) if interact_count > 0 else 0

            play_addr = ""
            video_urls = video.get("play_addr", {}).get("url_list", [])
            if video_urls:
                play_addr = video_urls[0]

            results.append({
                "aweme_id": aweme_id,
                "title": title,
                "description": aweme_info.get("desc", ""),
                "play_count": play_count,
                "liked_count": digg_count,
                "comment_count": comment_count,
                "share_count": share_count,
                "collected_count": collect_count,
                "interact_count": interact_count,
                "nickname": author.get("nickname", ""),
                "user_id": author.get("unique_id", "") or str(author.get("user_id", "")),
                "sec_uid": author.get("sec_uid", ""),
                "pub_time": create_time,  # 数据库字段名是 pub_time
                "note_age_hours": note_age_hours,
                "interact_velocity": interact_velocity,
                "engagement_score": interact_count,  # 用互动总数做初始评分
                "page_url": build_douyin_page_url(aweme_id),
                "video_url": play_addr,
                "note_type": aweme_info.get("aweme_type", 0),
                "source": "search",
            })
        
        if skipped > 0:
            self.log(f"[调试] 跳过 {skipped} 条无 aweme_id 的记录")
        return results

    def search_videos(self, keyword, page=1, sort="general"):
        self._ensure_playwright()
        self._clear_api()

        self.log(f"[搜索] 打开抖音首页...")
        self._page.goto("https://www.douyin.com/", wait_until="domcontentloaded", timeout=30000)
        self._page.wait_for_timeout(3000)

        # 检查是否弹出登录框等
        try:
            # 关闭可能的弹窗
            close_btn = self._page.query_selector("button[class*='close'], div[class*='modal'] button")
            if close_btn:
                close_btn.click()
                self._page.wait_for_timeout(500)
        except Exception:
            pass

        self.log(f"[搜索] 关键词='{keyword}' 第{page}页")

        # 模拟用户：在搜索框输入关键词并搜索
        try:
            # 找到搜索输入框
            search_input = self._page.query_selector("input[type='text']")
            if not search_input:
                search_input = self._page.query_selector("input[placeholder*='搜索']")
            if not search_input:
                search_input = self._page.query_selector("[data-e2e='search-input']")
            
            if search_input:
                # 清空并输入关键词
                search_input.click()
                self._page.wait_for_timeout(300)
                search_input.fill("")
                search_input.fill(keyword)
                self._page.wait_for_timeout(500)
                
                # 点击搜索按钮或按Enter
                try:
                    search_btn = self._page.query_selector("button[class*='search']")
                    if search_btn:
                        search_btn.click()
                    else:
                        self._page.keyboard.press("Enter")
                except Exception:
                    self._page.keyboard.press("Enter")
                
                self.log(f"[搜索] 已输入关键词并点击搜索")
            else:
                # 如果找不到搜索框，尝试直接跳转
                self.log(f"[搜索] 未找到搜索框，尝试直接跳转...")
                search_url = f"https://www.douyin.com/search/{quote(keyword)}?type=general"
                self._page.goto(search_url, wait_until="domcontentloaded", timeout=30000)

        except Exception as e:
            self.log(f"[搜索] 搜索操作异常: {e}")
            # 降级方案：直接跳转
            search_url = f"https://www.douyin.com/search/{quote(keyword)}?type=general"
            self._page.goto(search_url, wait_until="domcontentloaded", timeout=30000)

        # 等待页面跳转和加载
        self._page.wait_for_timeout(5000)

        # 滚动触发加载
        for i in range(3):
            try:
                self._page.evaluate(f"window.scrollTo(0, {300 + i * 500})")
            except Exception:
                pass
            self._page.wait_for_timeout(2000)

        # 调试：查看捕获的 API
        self._debug_api_urls()

        # 优先匹配当前实际使用的搜索接口。
        resp = self._find_api_with_items("general/search")
        if not resp:
            resp = self._find_api_with_items("search/item")
        if not resp:
            self.log("[搜索] 尝试等待更多 API 响应...")
            self._page.wait_for_timeout(3000)
            self._debug_api_urls()
            with self._api_lock:
                resp = next(
                    (r for r in reversed(self._api_responses) if self._is_search_items_response(r)),
                    None,
                )

        if not resp:
            self.log(f"[搜索] ✗ 未捕获到搜索 API")
            self.log(f"[调试] 当前页面 URL: {self._page.url}")
            self.log(f"[调试] 当前页面标题: {self._page.title()}")
            return []

        body = resp["body"]
        self.log(f"[搜索] 捕获到 API: {resp['url'].split('?')[0]}")

        aweme_list = self._extract_aweme_list(body)

        if not aweme_list:
            self.log(f"[搜索] ✗ API 返回数据为空")
            self.log(f"[调试] API 响应结构: {list(body.keys())}")
            data = body.get("data", {})
            self.log(f"[调试] data 类型: {type(data).__name__}, data keys: {list(data.keys()) if isinstance(data, dict) else 'N/A'}")
            # 打印 body 的部分内容
            self.log(f"[调试] body 预览: {str(body)[:500]}")
            return []

        self.log(f"[搜索] ✓ 返回 {len(aweme_list)} 条视频")
        
        # 调试：解析前后的对比
        parsed = self._parse_search_items(aweme_list)
        self.log(f"[调试] 解析后: {len(parsed)} 条有效数据")
        if parsed:
            self.log(f"[调试] 第一条数据: aweme_id={parsed[0]['aweme_id']}, title={parsed[0]['title'][:30]}")
        
        return parsed

    def _scroll_load_more(self, seen_ids=None, max_scrolls=5):
        """
        将最后一个搜索结果滚入视口，等待并解析滚动后产生的新搜索响应。

        抖音当前使用 /general/search/single/，每个响应通常只是一个批次，
        因此不能用响应列表长度判断翻页，必须按 aweme_id 与已见集合去重。
        """
        seen_ids = set(seen_ids or ())
        self._clear_api()
        new_items = {}

        for attempt in range(1, max_scrolls + 1):
            try:
                scroll_state = self._page.evaluate(r"""
                    (seenIds) => {
                        const links = Array.from(
                            document.querySelectorAll('a[href*="/video/"], a[href*="/note/"]')
                        ).filter((element, index, all) => {
                            const href = element.href || '';
                            return href && all.findIndex(item => item.href === href) === index;
                        });
                        const knownLinks = links.filter(element => {
                            const href = element.href || '';
                            return seenIds.some(id =>
                                href.includes('/video/' + id) || href.includes('/note/' + id)
                            );
                        });
                        const relevantLinks = knownLinks.length ? knownLinks : links;
                        const last = relevantLinks.length
                            ? relevantLinks[relevantLinks.length - 1]
                            : null;

                        const isScrollable = element => {
                            if (!element) return false;
                            const style = getComputedStyle(element);
                            const overflowY = style.overflowY;
                            return element.scrollHeight > element.clientHeight + 80
                                && element.clientHeight > 200
                                && (overflowY === 'auto'
                                    || overflowY === 'scroll'
                                    || overflowY === 'overlay');
                        };

                        let target = last ? last.parentElement : null;
                        while (target && target !== document.body && !isScrollable(target)) {
                            target = target.parentElement;
                        }

                        if (!target || !isScrollable(target)) {
                            const candidates = Array.from(document.querySelectorAll('*'))
                                .filter(isScrollable)
                                .map(element => {
                                    const videoLinks = element.querySelectorAll(
                                        'a[href*="/video/"], a[href*="/note/"]'
                                    ).length;
                                    const rect = element.getBoundingClientRect();
                                    return {
                                        element,
                                        videoLinks,
                                        area: Math.max(rect.width, 0) * Math.max(rect.height, 0)
                                    };
                                })
                                .sort((a, b) =>
                                    (b.videoLinks - a.videoLinks) || (b.area - a.area)
                                );
                            target = candidates.length ? candidates[0].element : null;
                        }

                        if (!target) {
                            target = document.scrollingElement || document.documentElement;
                        }
                        window.__douyinSearchScrollTarget = target;

                        if (last) {
                            last.scrollIntoView({behavior: 'auto', block: 'end'});
                        }

                        const before = target.scrollTop;
                        const distance = Math.max(target.clientHeight * 0.85, 700);
                        target.scrollTop = Math.min(
                            target.scrollTop + distance,
                            target.scrollHeight - target.clientHeight
                        );
                        target.dispatchEvent(new Event('scroll', {bubbles: true}));

                        const rect = target.getBoundingClientRect();
                        const identity = target.id
                            ? '#' + target.id
                            : target.tagName.toLowerCase() + (
                                target.className && typeof target.className === 'string'
                                    ? '.' + target.className.trim().split(/\s+/).slice(0, 2).join('.')
                                    : ''
                            );
                        return {
                            cards: relevantLinks.length,
                            knownCards: knownLinks.length,
                            container: identity.slice(0, 100),
                            before: Math.round(before),
                            top: Math.round(target.scrollTop),
                            height: target.scrollHeight,
                            viewport: target.clientHeight,
                            mouseX: Math.max(1, Math.round(rect.left + rect.width / 2)),
                            mouseY: Math.max(1, Math.round(rect.top + rect.height / 2))
                        };
                    }
                """, list(seen_ids))
                self._page.mouse.move(
                    scroll_state.get("mouseX", 1),
                    scroll_state.get("mouseY", 1),
                )
                self._page.mouse.wheel(0, 1200)
            except Exception as e:
                self.log(f"[滚动] 第{attempt}次滚动执行异常: {e}")
                scroll_state = {}

            # 网络回调是异步的；等待本轮滚动产生搜索响应。
            deadline = time.time() + 8
            processed_responses = 0
            while time.time() < deadline:
                with self._api_lock:
                    responses = list(self._api_responses)

                for response_data in responses[processed_responses:]:
                    if not self._is_search_items_response(response_data):
                        continue
                    for item in self._extract_aweme_list(response_data["body"]):
                        aweme_id = self._item_aweme_id(item)
                        if aweme_id and aweme_id not in seen_ids:
                            new_items[aweme_id] = item

                processed_responses = len(responses)
                if new_items:
                    self.log(
                        f"[滚动] 第{attempt}次捕获到 {len(new_items)} 条新视频 "
                        f"(已知卡片={scroll_state.get('knownCards', '?')}/"
                        f"{scroll_state.get('cards', '?')}, "
                        f"容器={scroll_state.get('container', '?')}, "
                        f"位置={scroll_state.get('top', '?')}/{scroll_state.get('height', '?')})"
                    )
                    return self._parse_search_items(list(new_items.values()))

                self._page.wait_for_timeout(500)

            self.log(
                f"[滚动] 第{attempt}次未捕获新搜索响应 "
                f"(已知卡片={scroll_state.get('knownCards', '?')}/"
                f"{scroll_state.get('cards', '?')}, "
                f"容器={scroll_state.get('container', '?')}, "
                f"位置={scroll_state.get('top', '?')}/{scroll_state.get('height', '?')})"
            )
            # 已在底部但观察器未触发时，上移后再次让末卡进入视口。
            try:
                self._page.evaluate("""
                    () => {
                        const target = window.__douyinSearchScrollTarget
                            || document.scrollingElement
                            || document.documentElement;
                        target.scrollTop = Math.max(0, target.scrollTop - 500);
                        target.dispatchEvent(new Event('scroll', {bubbles: true}));
                    }
                """)
                self._page.wait_for_timeout(400)
            except Exception:
                pass

        return []

    def crawl_keyword(self, keyword, pages=3, sort="general"):
        self.task_id = self.db.create_task(keyword, pages, sort)
        self.log(f"[任务] 创建任务#{self.task_id}: 关键词='{keyword}', 页数={pages}")

        all_videos = []
        aweme_ids_seen = set()

        videos = self.search_videos(keyword, page=1, sort=sort)
        if not videos:
            self.log(f"[任务] 搜索失败，无数据")
            return []

        for v in videos:
            if v["aweme_id"] not in aweme_ids_seen:
                aweme_ids_seen.add(v["aweme_id"])
                all_videos.append(v)

        self.log(f"[任务] 第1页搜索到 {len(videos)} 条，累计 {len(all_videos)} 条")

        for page in range(2, pages + 1):
            delay = random.uniform(3, 5)
            self.log(f"[任务] 等待 {delay:.1f}s 后滚动加载第{page}页...")
            time.sleep(delay)

            new_videos = self._scroll_load_more(
                seen_ids=aweme_ids_seen,
                max_scrolls=3,
            )
            new_count = 0
            for v in new_videos:
                if v["aweme_id"] not in aweme_ids_seen:
                    aweme_ids_seen.add(v["aweme_id"])
                    all_videos.append(v)
                    new_count += 1

            self.log(f"[任务] 第{page}页: 新增 {new_count} 条，累计 {len(all_videos)} 条")

        self.log(f"[任务] 本次抓取：{len(all_videos)} 条视频")

        for v in all_videos:
            v["category"] = keyword
            try:
                self.db.upsert_note(self.task_id, v)
                with self.stats_lock:
                    self.success_count += 1
            except Exception as e:
                self.log(f"[任务] 存储异常: {e}")
                self.db.add_failed_note(self.task_id, v.get("aweme_id", ""), "store_error", str(e))
                with self.stats_lock:
                    self.fail_count += 1

        self.db.update_task_status(
            self.task_id, "completed",
            success_count=self.success_count,
            fail_count=self.fail_count,
        )

        if self.output_dir:
            self.log("")
            self.log("=" * 70)
            self.log(f"  自动分析与导出  模式: {self.mode}")
            self.log("=" * 70)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            outputs = []

            if self.mode in ("momentum", "both"):
                self.log("")
                mom_csv = os.path.join(self.output_dir, f"momentum_{ts}.csv")
                mom_results = self.momentum_analysis(keyword, csv_file=mom_csv)
                outputs.append(f"动量分析: {os.path.basename(mom_csv)} ({len(mom_results)} 条)")

            if self.mode in ("value", "both"):
                self.log("")
                val_csv = os.path.join(self.output_dir, f"value_{ts}.csv")
                val_results = self.value_analysis(keyword, csv_file=val_csv)
                outputs.append(f"价值分析: {os.path.basename(val_csv)} ({len(val_results)} 条)")

            self.log("")
            self.log(f"[完成] 全部结果已输出到: {self.output_dir}")
            self.log(f"  - 日志: {os.path.basename(self.log_file)}")
            for out in outputs:
                self.log(f"  - {out}")

        return all_videos

    def momentum_analysis(self, keyword, limit=999999, csv_file=None):
        self.log(f"[动量分析] 关键词='{keyword}'")
        results = self.db.get_keyword_momentum_ranking(keyword, limit=limit)
        if not results:
            self.log(f"[动量分析] 无数据，请先爬取关键词: {keyword}")
            return []

        self.log(f"[动量分析] 范围：关键词累计库，共 {len(results)} 条视频")
        self.log("")
        header = (
            f"{'排名':<4} {'标题':<30} {'互动总数':>10} {'作者':<12} "
            f"{'速率/h':>8} {'密度分':>7} {'年龄(h)':>8} {'综合分':>7}"
        )
        self.log("-" * 120)
        self.log(header)
        self.log("-" * 120)
        for i, n in enumerate(results[:20], 1):
            title = (n["title"] or "")[:28]
            uploader = (n.get("nickname") or "")[:11]
            self.log(
                f"{i:<4} {title:<30} {n['total_interact']:>10,} {uploader:<12} "
                f"{n['interact_velocity']:>8.0f} {n['density_score']:>7.3f} "
                f"{n['note_age_hours']:>8.1f} {n['composite_score']:>7.3f}"
            )
        if len(results) > 20:
            self.log(f"  ... 还有 {len(results) - 20} 条，完整结果请查看 CSV")
        self.log("-" * 120)
        self.log("提示: 综合分 = 速率(35%) + 密度(30%) + 新鲜度(20%) + 评论活跃度(15%)")

        if csv_file:
            self.db.export_momentum_csv(keyword, csv_file, results)

        return results

    def value_analysis(self, keyword, limit=999999, csv_file=None):
        self.log(f"[价值分析] 关键词='{keyword}'")
        results = self.db.get_value_ranking(keyword, limit=limit)
        if not results:
            self.log(f"[价值分析] 无数据，请先爬取关键词: {keyword}")
            return []

        self.log(f"[价值分析] 范围：关键词累计库，共 {len(results)} 条视频")
        self.log("")
        header = (
            f"{'排名':<4} {'标题':<30} {'互动总数':>10} {'作者':<12} "
            f"{'收藏率':>7} {'分享率':>7} {'评论率':>7} {'价值分':>7}"
        )
        self.log("-" * 120)
        self.log(header)
        self.log("-" * 120)
        for i, n in enumerate(results[:20], 1):
            title = (n["title"] or "")[:28]
            uploader = (n.get("nickname") or "")[:11]
            self.log(
                f"{i:<4} {title:<30} {n['total_interact']:>10,} {uploader:<12} "
                f"{n['collect_rate']:>7.3f} {n['share_rate']:>7.3f} "
                f"{n['comment_rate']:>7.3f} "
                f"{n['value_score']:>7.3f}"
            )
        if len(results) > 20:
            self.log(f"  ... 还有 {len(results) - 20} 条，完整结果请查看 CSV")
        self.log("-" * 120)
        self.log("提示: 价值分 = 收藏率(35%) + 分享率(25%) + 评论率(20%) + 互动率(20%)")

        if csv_file:
            self.db.export_value_csv(keyword, csv_file, results)

        return results

    def close(self):
        self._close_playwright()
