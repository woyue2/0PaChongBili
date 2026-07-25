"""
xhs_spider.py - 小红书爬虫主类
基于 Playwright 监听 API 响应模式，浏览器自动签名，无需维护 x-s 算法。
"""

import time
import random
import os
import threading
from datetime import datetime
from urllib.parse import quote

from xhs_util import DB_FILE, COOKIE_FILE, CookieManager, Database


def _parse_chinese_num(s):
    """解析中文数字，如 '9万' -> 90000, '6.6万' -> 66000, '1.4万' -> 14000"""
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


class XhsSpider:
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
            log_dir = "logs"
        os.makedirs(log_dir, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.log_file = os.path.join(log_dir, f"xhs_spider_{timestamp}.log")
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
                            "domain": ".xiaohongshu.com",
                            "path": "/",
                        })

            self._context = self._browser.new_context(
                viewport={"width": 1920, "height": 1080},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0",
            )

            if cookies_for_pw:
                self._context.add_cookies(cookies_for_pw)
                self.log(f"[浏览器] 已注入 {len(cookies_for_pw)} 个 cookie")

            self._page = self._context.new_page()
            self._page.on("response", self._on_response)
            self.log(f"[浏览器] Edge 浏览器已启动 (headless={headless})")

    def _on_response(self, response):
        url = response.url
        if "/api/sns/web/" not in url:
            return
        try:
            body = response.json()
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
            for r in self._api_responses:
                if path_keyword in r["url"]:
                    body = r["body"]
                    if isinstance(body, dict):
                        data = body.get("data", {})
                        if isinstance(data, dict) and "items" in data:
                            return r
        return None

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
        self.log("[登录检测] 访问小红书首页...")
        self._page.goto("https://www.xiaohongshu.com/", wait_until="domcontentloaded", timeout=30000)
        self._page.wait_for_timeout(4000)

        resp = self._wait_for_api("/user/me", timeout_s=8)
        if resp and resp["body"].get("code") == 0:
            data = resp["body"].get("data", {})
            nickname = data.get("nickname", "?")
            self.log(f"[登录检测] ✓ 登录正常: {nickname}")
            return True
        else:
            code = resp["body"].get("code") if resp else "?"
            msg = resp["body"].get("msg", "?") if resp else "无响应"
            self.log(f"[登录检测] ✗ 登录失效: code={code}, msg={msg}")
            return False

    def _parse_search_items(self, items):
        results = []
        for item in items:
            nc = item.get("note_card", {}) or {}
            note_id = (nc.get("note_id") or nc.get("id")
                       or item.get("id") or item.get("note_id") or "")
            if not note_id:
                continue
            ii = nc.get("interact_info", {}) or {}
            user = nc.get("user", {}) or {}
            xsec_token = item.get("xsec_token", "") or nc.get("xsec_token", "")

            liked = _parse_chinese_num(ii.get("liked_count", 0))
            collected = _parse_chinese_num(ii.get("collected_count", 0))
            comment = _parse_chinese_num(ii.get("comment_count", 0))
            share = _parse_chinese_num(ii.get("share_count", 0))
            interact = liked + collected + comment

            title = nc.get("title") or nc.get("display_title") or ""

            results.append({
                "note_id": note_id,
                "xsec_token": xsec_token,
                "title": title,
                "liked_count": liked,
                "collected_count": collected,
                "comment_count": comment,
                "share_count": share,
                "interact_count": interact,
                "nickname": user.get("nickname", ""),
                "user_id": user.get("user_id", ""),
                "note_type": nc.get("type", ""),
                "source": "search",
            })
        return results

    def search_notes(self, keyword, page=1, sort="general"):
        self._ensure_playwright()
        self._clear_api()

        sort_map = {
            "general": "",
            "popular": "popularity_descending",
            "new": "time_descending",
        }
        sort_val = sort_map.get(sort, "")

        search_url = f"https://www.xiaohongshu.com/search_result?keyword={quote(keyword)}"
        if sort_val:
            search_url += f"&sort={sort_val}"

        self.log(f"[搜索] 关键词='{keyword}' 第{page}页 排序={sort}")
        self._page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
        self._page.wait_for_timeout(5000)

        for i in range(2):
            try:
                self._page.evaluate(f"window.scrollTo(0, {500 + i * 400})")
            except Exception:
                pass
            self._page.wait_for_timeout(1500)

        resp = self._find_api_with_items("search/notes")
        if not resp:
            self.log(f"[搜索] ✗ 未捕获到搜索 API")
            return []

        body = resp["body"]
        if body.get("code") != 0:
            self.log(f"[搜索] ✗ API 错误: code={body.get('code')}, msg={body.get('msg','?')}")
            return []

        items = body.get("data", {}).get("items", []) or []
        self.log(f"[搜索] ✓ 返回 {len(items)} 条笔记")

        return self._parse_search_items(items)

    def _scroll_load_more(self, max_scrolls=5):
        self._clear_api()
        new_items = []
        last_count = 0

        for s in range(max_scrolls):
            try:
                self._page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            except Exception:
                pass
            self._page.wait_for_timeout(2500)

            resp = self._find_api_with_items("search/notes")
            if resp and resp["body"].get("code") == 0:
                items = resp["body"].get("data", {}).get("items", []) or []
                if len(items) > last_count:
                    new_items = items
                    last_count = len(items)

        return self._parse_search_items(new_items) if new_items else []

    def _get_all_cards(self):
        selectors = [
            ".note-item",
            "section.note-item",
            ".feeds-page .note-item",
            ".feed-item",
            "a[href*='/explore/']",
            ".cover-mask",
        ]
        for sel in selectors:
            cards = self._page.query_selector_all(sel)
            if cards:
                return cards
        return []

    def _extract_pubtime_tags_from_dom(self):
        try:
            return self._page.evaluate("""() => {
                const result = {};
                const mask = document.querySelector('.note-detail-mask');
                if (!mask) return null;

                // ── 发布时间 ──────────────────────────────────────────────
                // 优先：scoped class（data-v-772fa76f 是 note-content 组件的哈希）
                // 降级1：弹窗内任何带 scoped attr 的 .date 元素
                // 降级2：.note-content .date
                // 降级3：弹窗内第一个 .date
                const dateEl =
                    mask.querySelector('[class*="date"][data-v-772fa76f]') ||
                    mask.querySelector('[data-v-772fa76f].date') ||
                    (() => {
                        // 找带 data-v-* 属性且 class 含 date 的元素（应对哈希变化）
                        const all = mask.querySelectorAll('[class]');
                        for (const el of all) {
                            const hasDateClass = Array.from(el.classList).some(c => c === 'date');
                            const hasScopedAttr = Array.from(el.attributes).some(a => a.name.startsWith('data-v-'));
                            if (hasDateClass && hasScopedAttr) return el;
                        }
                        return null;
                    })() ||
                    mask.querySelector('.note-content .date') ||
                    mask.querySelector('.date');

                if (dateEl) {
                    result.pub_time_text = dateEl.textContent.trim();
                }

                // ── 标签 ──────────────────────────────────────────────────
                // 优先：scoped .tag[data-v-772fa76f]
                // 降级：弹窗内任何带 scoped attr 且 class=tag 的元素，或 href 含 /tag/
                let tagEls = mask.querySelectorAll('[class*="tag"][data-v-772fa76f]');
                if (!tagEls.length) {
                    // 应对哈希变化：找 class=tag + 任意 data-v-* 属性
                    const candidates = [];
                    mask.querySelectorAll('[class]').forEach(el => {
                        const isTag = Array.from(el.classList).some(c => c === 'tag');
                        const hasScoped = Array.from(el.attributes).some(a => a.name.startsWith('data-v-'));
                        if (isTag && hasScoped) candidates.push(el);
                    });
                    tagEls = candidates;
                }
                if (!tagEls.length) {
                    tagEls = mask.querySelectorAll('a[href*="/tag/"]');
                }
                const tags = [];
                tagEls.forEach(el => {
                    const text = el.textContent.trim().replace(/^#/, '');
                    if (text && !tags.includes(text) && text.length < 50) tags.push(text);
                });
                result.tags = tags.join(',');

                // ── 分享数 ────────────────────────────────────────────────
                // 优先：.share-wrapper 内的 .count
                // 降级：buttons 区域（data-v-2820500a）按钮顺序第4个的 .count
                const shareWrapper = mask.querySelector('.share-wrapper');
                if (shareWrapper) {
                    const countEl = shareWrapper.querySelector('.count');
                    if (countEl) {
                        result.share_count_text = countEl.textContent.trim();
                    }
                }
                if (!result.share_count_text) {
                    // engage-bar 下：找所有顶层交互按钮，分享一般在第4个
                    const counts = mask.querySelectorAll('.buttons .count, [class*="buttons"] .count');
                    if (counts.length >= 4) {
                        result.share_count_text = counts[3].textContent.trim();
                    }
                }

                // ── note_id（备用） ────────────────────────────────────────
                const noteIdAttr = mask.getAttribute('note-id') || mask.getAttribute('data-note-id');
                if (noteIdAttr) result.note_id = noteIdAttr;

                // ── 调试信息（方便排查选择器是否命中） ──────────────────
                result._debug = {
                    date_found: !!dateEl,
                    date_text: dateEl ? dateEl.textContent.trim() : null,
                    tag_count: tags.length,
                    share_text: result.share_count_text || null,
                };

                return result;
            }""")
        except Exception:
            return None

    def _parse_chinese_time(self, time_text):
        if not time_text:
            return 0
        import datetime as _dt
        # 去掉"编辑于 "前缀
        text = time_text.strip()
        for prefix in ('编辑于 ', '编辑于', '发布于 ', '发布于'):
            if text.startswith(prefix):
                text = text[len(prefix):].strip()
                break
        try:
            now = _dt.datetime.now()
            # ── 相对时间 ──────────────────────────────────────────────────
            if '分钟前' in text:
                mins = int(text.replace('分钟前', '').strip())
                return time.time() - mins * 60
            if '小时前' in text:
                hours = int(text.replace('小时前', '').strip())
                return time.time() - hours * 3600
            if '昨天' in text:
                return time.time() - 24 * 3600
            if '天前' in text:
                days = int(text.replace('天前', '').strip())
                return time.time() - days * 24 * 3600
            if '周前' in text:
                weeks = int(text.replace('周前', '').strip())
                return time.time() - weeks * 7 * 24 * 3600
            if '月前' in text:
                months = int(text.replace('月前', '').strip())
                return time.time() - months * 30 * 24 * 3600
            # ── 绝对时间（含年份） ────────────────────────────────────────
            for fmt in ['%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M',
                        '%Y-%m-%d', '%Y/%m/%d', '%Y年%m月%d日']:
                try:
                    dt = _dt.datetime.strptime(text, fmt)
                    return dt.timestamp()
                except ValueError:
                    continue
            # ── 无年份格式：MM-dd 或 MM月dd日 ────────────────────────────
            # 小红书最常见：当年内显示 "06-15"，跨年显示全年份
            for fmt in ['%m-%d', '%m/%d', '%m月%d日']:
                try:
                    dt = _dt.datetime.strptime(text, fmt).replace(year=now.year)
                    # 如果解析出来的日期在未来（跨年边界），退到上一年
                    if dt > now + _dt.timedelta(days=1):
                        dt = dt.replace(year=now.year - 1)
                    return dt.timestamp()
                except ValueError:
                    continue
        except Exception:
            pass
        return 0

    def _get_detail_by_card_index(self, card_index, search_note_data=None):
        cards = self._get_all_cards()
        if card_index >= len(cards):
            for _ in range(5):
                try:
                    self._page.evaluate("window.scrollBy(0, 800)")
                except Exception:
                    pass
                time.sleep(1)
                cards = self._get_all_cards()
                if card_index < len(cards):
                    break

        if card_index >= len(cards):
            self.log(f"[详情] ✗ 未找到第{card_index}个卡片")
            return None

        card = cards[card_index]

        try:
            card.scroll_into_view_if_needed()
            time.sleep(0.5)
            card.click()
        except Exception as e:
            try:
                self._page.evaluate("arguments[0].click()", card)
                time.sleep(0.5)
            except Exception:
                self.log(f"[详情] ✗ 点击第{card_index}个卡片失败: {e}")
                return None

        try:
            self._page.wait_for_selector(".note-detail-mask", timeout=8000)
        except Exception:
            self.log(f"[详情] ✗ 弹窗未出现")
            try:
                self._page.keyboard.press("Escape")
            except Exception:
                pass
            return None

        time.sleep(2)

        dom_data = self._extract_pubtime_tags_from_dom()
        if not dom_data:
            time.sleep(1)
            dom_data = self._extract_pubtime_tags_from_dom()

        try:
            close_btn = self._page.query_selector(".note-detail-mask .close-btn, .note-detail-mask .icon-close, .note-detail-mask [data-testid='close'], .note-detail-mask .m-close")
            if close_btn and close_btn.is_visible():
                close_btn.click()
            else:
                self._page.keyboard.press("Escape")
            time.sleep(0.6)
        except Exception:
            try:
                self._page.keyboard.press("Escape")
                time.sleep(0.5)
            except Exception:
                pass

        if not dom_data:
            self.log(f"[详情] ✗ DOM解析为空")
            return None

        # 打印调试信息，确认选择器是否命中
        dbg = dom_data.get('_debug', {})
        self.log(f"[DOM调试] date命中={dbg.get('date_found')} "
                 f"date_text='{dbg.get('date_text')}' "
                 f"tag数={dbg.get('tag_count')} "
                 f"share_text='{dbg.get('share_text')}'")

        pub_time_sec = self._parse_chinese_time(dom_data.get('pub_time_text', ''))
        pub_time_ms = int(pub_time_sec * 1000) if pub_time_sec > 0 else 0
        pub_time = ""
        if pub_time_sec > 0:
            try:
                pub_time = datetime.fromtimestamp(pub_time_sec).strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                pass

        note_age_hours = 0
        interact_velocity = 0
        interact = 0
        engagement = 0
        liked = 0
        collected = 0
        comment = 0

        if search_note_data:
            interact = search_note_data.get("interact_count", 0)
            liked = search_note_data.get("liked_count", 0)
            collected = search_note_data.get("collected_count", 0)
            comment = search_note_data.get("comment_count", 0)
            if interact > 0:
                engagement = round((liked + collected + comment) / max(interact, 1), 4)

        if pub_time_sec > 0 and interact > 0:
            age_sec = time.time() - pub_time_sec
            note_age_hours = round(age_sec / 3600, 2)
            if note_age_hours > 0:
                interact_velocity = round(interact / note_age_hours, 2)

        # 从 DOM 读分享数（搜索列表 API 不含此字段）
        share_from_dom = 0
        share_text = dom_data.get('share_count_text', '').strip()
        if share_text:
            share_from_dom = _parse_chinese_num(share_text)

        # 优先用 DOM 读到的分享数；search_note_data 里的一般为 0
        share_count_final = share_from_dom if share_from_dom > 0 else search_note_data.get("share_count", 0) if search_note_data else 0

        note_id = search_note_data.get("note_id", "") if search_note_data else dom_data.get("note_id", "")

        detail = {
            "note_id": note_id,
            "title": search_note_data.get("title", "") if search_note_data else "",
            "description": "",
            "liked_count": liked,
            "collected_count": collected,
            "comment_count": comment,
            "share_count": share_count_final,
            "interact_count": interact,
            "nickname": search_note_data.get("nickname", "") if search_note_data else "",
            "user_id": search_note_data.get("user_id", "") if search_note_data else "",
            "fans_count": 0,
            "pub_time": pub_time,
            "pub_time_ms": pub_time_ms,
            "note_type": search_note_data.get("note_type", "normal") if search_note_data else "normal",
            "tags": dom_data.get("tags", ""),
            "note_age_hours": note_age_hours,
            "interact_velocity": interact_velocity,
            "engagement_score": engagement,
            "url": f"https://www.xiaohongshu.com/explore/{note_id}" if note_id else "",
        }

        self.log(f"[详情] ✓ '{detail['title'][:30]}' "
                 f"互动={interact} 赞={detail['liked_count']} 藏={detail['collected_count']} 评={detail['comment_count']}")
        return detail

    def get_note_detail(self, note_id, xsec_token="", note_index=0):
        self._ensure_playwright()
        return self._get_detail_by_card_index(note_index)

    def crawl_keyword(self, keyword, pages=3, sort="general"):
        self.task_id = self.db.create_task(keyword, pages, sort)
        self.log(f"[任务] 创建任务#{self.task_id}: 关键词='{keyword}', 页数={pages}, 排序={sort}")

        all_notes = []
        note_ids_seen = set()
        detailed_count = 0
        card_index = 0

        notes = self.search_notes(keyword, page=1, sort=sort)
        if not notes:
            self.log(f"[任务] 搜索失败，无数据")
            return []

        for n in notes:
            if n["note_id"] not in note_ids_seen:
                note_ids_seen.add(n["note_id"])
                all_notes.append(n)

        self.log(f"[任务] 第1页搜索到 {len(notes)} 条，累计 {len(all_notes)} 条")

        for page in range(1, pages + 1):
            start_idx = card_index
            end_idx = min(start_idx + len(all_notes) - start_idx, len(all_notes))

            if start_idx >= len(all_notes):
                break

            self.log(f"[任务] 第{page}页：处理 {end_idx - start_idx} 条详情（{start_idx+1}-{end_idx}/{len(all_notes)}）...")

            for i in range(start_idx, end_idx):
                note = all_notes[i]
                try:
                    d = self._get_detail_by_card_index(card_index, search_note_data=note)
                    if d:
                        d["category"] = keyword
                        note_data = {**note, **d}
                        self.db.upsert_note(self.task_id, note_data)
                        detailed_count += 1
                        with self.stats_lock:
                            self.success_count += 1
                    else:
                        self.log(f"[详情] ✗ 第{i+1}条 '{note['title'][:20]}' 获取失败")
                        self.db.add_failed_note(self.task_id, note["note_id"], "detail_fail", "详情获取失败")
                        with self.stats_lock:
                            self.fail_count += 1
                except Exception as e:
                    self.log(f"[任务] 详情异常: {e}")
                    self.db.add_failed_note(self.task_id, note["note_id"], "detail_error", str(e))
                    with self.stats_lock:
                        self.fail_count += 1

                card_index += 1

                delay = random.uniform(3, 6)
                time.sleep(delay)

            self.log(f"[任务] 第{page}页完成，累计成功={self.success_count}, 失败={self.fail_count}")

            if page < pages:
                delay = random.uniform(3, 5)
                self.log(f"[任务] 等待 {delay:.1f}s 后滚动加载下一页...")
                time.sleep(delay)

                try:
                    self._page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    time.sleep(2)
                    self._page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                except Exception:
                    pass

                time.sleep(3)

                resp = self._find_api_with_items("search/notes")
                if resp and resp["body"].get("code") == 0:
                    items = resp["body"].get("data", {}).get("items", []) or []
                    new_notes = self._parse_search_items(items)
                    new_count = 0
                    for n in new_notes:
                        if n["note_id"] not in note_ids_seen:
                            note_ids_seen.add(n["note_id"])
                            all_notes.append(n)
                            new_count += 1
                    if new_count > 0:
                        self.log(f"[任务] 滚动后新增 {new_count} 条，累计 {len(all_notes)} 条")
                    else:
                        self.log(f"[任务] 滚动后无新数据")
                else:
                    self.log(f"[任务] 滚动后未捕获到搜索 API")

        self.log(f"[任务] 全部完成，共 {len(all_notes)} 条笔记，详情成功 {detailed_count} 条")
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

        return all_notes

    def momentum_analysis(self, keyword, limit=999999, csv_file=None):
        self.log(f"[动量分析] 关键词='{keyword}'")
        results = self.db.get_keyword_momentum_ranking(keyword, limit=limit)
        if not results:
            self.log(f"[动量分析] 无数据，请先爬取关键词: {keyword}")
            return []

        self.log(f"[动量分析] 共 {len(results)} 条笔记")
        self.log("")
        header = (
            f"{'排名':<4} {'标题':<30} {'互动量':>8} {'作者':<12} "
            f"{'速率/h':>8} {'密度':>7} {'年龄(h)':>8} {'综合分':>7}"
        )
        self.log("-" * 120)
        self.log(header)
        self.log("-" * 120)
        for i, n in enumerate(results[:20], 1):
            title = (n["title"] or "")[:28]
            uploader = (n.get("nickname") or "")[:11]
            self.log(
                f"{i:<4} {title:<30} {n['current_value']:>8,} {uploader:<12} "
                f"{n['interact_velocity']:>8.0f} {n['engagement_score']:>7.3f} "
                f"{n['note_age_hours']:>8.1f} {n['composite_score']:>7.3f}"
            )
        if len(results) > 20:
            self.log(f"  ... 还有 {len(results) - 20} 条，完整结果请查看 CSV")
        self.log("-" * 120)
        self.log("提示: 综合分 = 速率(35%) + 密度(30%) + 新鲜(20%) + 互动量(15%)")
        self.log("      动量看'速度'，价值看'质量'，两者互补")

        if csv_file:
            self.db.export_momentum_csv(keyword, csv_file, results)

        return results

    def value_analysis(self, keyword, limit=999999, csv_file=None):
        self.log(f"[价值分析] 关键词='{keyword}'")
        results = self.db.get_value_ranking(keyword, limit=limit)
        if not results:
            self.log(f"[价值分析] 无数据，请先爬取关键词（需含详情数据）: {keyword}")
            return []

        self.log(f"[价值分析] 共 {len(results)} 条笔记")
        self.log("")
        header = (
            f"{'排名':<4} {'标题':<30} {'互动量':>8} {'作者':<12} "
            f"{'收藏率':>7} {'密度':>7} {'分享率':>7} {'评论率':>7} {'价值分':>7}"
        )
        self.log("-" * 130)
        self.log(header)
        self.log("-" * 130)
        for i, n in enumerate(results[:20], 1):
            title = (n["title"] or "")[:28]
            uploader = (n.get("nickname") or "")[:11]
            self.log(
                f"{i:<4} {title:<30} {n['interact_count']:>8,} {uploader:<12} "
                f"{n['collect_rate']:>7.3f} {n['engagement_score']:>7.3f} "
                f"{n['share_rate']:>7.3f} {n['comment_rate']:>7.3f} "
                f"{n['value_score']:>7.3f}"
            )
        if len(results) > 20:
            self.log(f"  ... 还有 {len(results) - 20} 条，完整结果请查看 CSV")
        self.log("-" * 130)
        self.log("提示: 价值分 = 收藏率(40%) + 互动密度(30%) + 分享率(15%) + 评论率(15%)")
        self.log("      收藏率 = 收藏/点赞，代表内容被'保存'的深度价值")

        if csv_file:
            self.db.export_value_csv(keyword, csv_file, results)

        return results

    def close(self):
        self._close_playwright()
