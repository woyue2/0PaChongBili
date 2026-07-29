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

from xhs_util import DB_FILE, COOKIE_FILE, Database
from src.common import paths
from src.common.auth_state import AuthStateStore, LOGIN_MAX_AGE_DAYS


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
        self.db = Database(DB_FILE)
        self.auth_state = AuthStateStore()
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
        self._api_sequence = 0
        self._init_logger()

    def _init_logger(self):
        if self.output_dir:
            log_dir = self.output_dir
        else:
            log_dir = paths.logs("xhs")
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
            if self._context is not None:
                return
            from playwright.sync_api import sync_playwright
            self._pw = sync_playwright().start()

            headless = getattr(self.args, "headless", True)
            os.makedirs(os.path.dirname(paths.XHS_EDGE_PROFILE), exist_ok=True)
            self._context = self._pw.chromium.launch_persistent_context(
                user_data_dir=paths.XHS_EDGE_PROFILE,
                channel="msedge",
                headless=headless,
                viewport={"width": 1920, "height": 1080},
                permissions=["clipboard-read", "clipboard-write"],
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                ],
            )
            self._browser = self._context.browser
            self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
            self._page.on("response", self._on_response)

            # Hook navigator.clipboard.writeText / readText：把写入的完整分享链接存到全局变量
            # 小红书点「复制链接」后会调 clipboard.writeText(完整URL)，直接读 hook 就能拿到，
            # 不再受 headless 剪贴板权限/系统隔离影响
            self._context.add_init_script("""() => {
                try {
                    window.__xhs_last_share_link__ = '';
                    window.__xhs_share_link_log__ = [];
                    const origWrite = navigator.clipboard && navigator.clipboard.writeText
                        ? navigator.clipboard.writeText.bind(navigator.clipboard) : null;
                    if (origWrite) {
                        navigator.clipboard.writeText = function(txt) {
                            try {
                                const s = (txt == null ? '' : String(txt));
                                window.__xhs_share_link_log__.push(s);
                                if (s && (
                                    s.includes('xiaohongshu.com/') ||
                                    s.includes('xhslink.com/') ||
                                    s.includes('xsec_token=') ||
                                    s.includes('xhsshare=')
                                )) {
                                    window.__xhs_last_share_link__ = s;
                                }
                            } catch(_) {}
                            return origWrite(txt);
                        };
                    }
                    // document.execCommand('copy') 的 fallback 拦截，部分老路径会用这个
                    const origExec = document.execCommand.bind(document);
                    document.execCommand = function(cmdId, showUI, value) {
                        try {
                            if (/copy/i.test(String(cmdId || ''))) {
                                // 尝试从当前 selection / 临时隐藏 input 抓内容
                                const sel = (window.getSelection && window.getSelection()) || {toString:()=>''};
                                const candidate = (value != null ? String(value) : '') || (sel.toString ? sel.toString() : '');
                                if (candidate && (
                                    candidate.includes('xiaohongshu.com/') ||
                                    candidate.includes('xhslink.com/') ||
                                    candidate.includes('xsec_token=') ||
                                    candidate.includes('xhsshare=')
                                )) {
                                    window.__xhs_last_share_link__ = candidate;
                                    window.__xhs_share_link_log__.push(candidate);
                                }
                            }
                        } catch(_) {}
                        return origExec(cmdId, showUI, value);
                    };
                } catch(_) {}
            }""")

            self.log(
                f"[浏览器] Edge 持久化 profile 已启动 "
                f"(headless={headless}, profile={paths.XHS_EDGE_PROFILE})"
            )

    def _on_response(self, response):
        url = response.url
        if "/api/sns/web/" not in url:
            return
        try:
            body = response.json()
            with self._api_lock:
                self._api_sequence += 1
                self._api_responses.append({
                    "seq": self._api_sequence,
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

    def _api_cursor(self):
        with self._api_lock:
            return self._api_sequence

    def _wait_for_api_with_items(self, path_keyword, after_seq=0, timeout_s=15):
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            with self._api_lock:
                for response in reversed(self._api_responses):
                    if response.get("seq", 0) <= after_seq:
                        break
                    if path_keyword not in response["url"]:
                        continue
                    body = response.get("body") or {}
                    data = body.get("data", {}) if isinstance(body, dict) else {}
                    if isinstance(data, dict) and isinstance(data.get("items"), list):
                        return response
            self._page.wait_for_timeout(300)
        return None

    def _find_api_with_items(self, path_keyword, after_seq=0):
        with self._api_lock:
            for r in reversed(self._api_responses):
                if r.get("seq", 0) <= after_seq:
                    break
                if path_keyword in r["url"]:
                    body = r["body"]
                    if isinstance(body, dict):
                        data = body.get("data", {})
                        if isinstance(data, dict) and "items" in data:
                            return r
        return None

    def _wheel_scroll(self, steps=3, minimum=450, maximum=850):
        """用滚轮分段滚动，避免 JavaScript 瞬移。"""
        for _ in range(steps):
            self._page.mouse.wheel(0, random.randint(minimum, maximum))
            self._page.wait_for_timeout(random.randint(450, 950))

    def _scroll_until_new_search_response(self, timeout_s=12):
        cursor = self._api_cursor()
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            self._wheel_scroll(steps=random.randint(2, 4))
            response = self._wait_for_api_with_items(
                "search/notes",
                after_seq=cursor,
                timeout_s=2,
            )
            if response:
                return response
        return None

    def _close_playwright(self):
        with self._pw_lock:
            try:
                if self._context:
                    self._context.close()
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

    def _latest_logged_in_user(self):
        with self._api_lock:
            responses = list(reversed(self._api_responses))
        for resp in responses:
            if "/user/me" not in resp["url"]:
                continue
            body = resp.get("body") or {}
            if body.get("code") == 0:
                data = body.get("data") or {}
                return data.get("nickname") or "已登录用户"
        return ""

    def _save_current_cookies(self):
        """导出 Cookie 仅作兼容备份；运行时不再向新 context 注入。"""
        cookies = self._context.cookies("https://www.xiaohongshu.com")
        cookie_str = "; ".join(
            f"{cookie['name']}={cookie['value']}" for cookie in cookies
        )
        os.makedirs(os.path.dirname(COOKIE_FILE), exist_ok=True)
        with open(COOKIE_FILE, "w", encoding="utf-8") as cookie_file:
            cookie_file.write(cookie_str)
        self.log(f"[登录刷新] 已保存 {len(cookies)} 个 Cookie")

    def _clear_site_login_state(self):
        """清除当前平台 profile 中的站点登录状态，保留 profile 本身。"""
        self._context.clear_cookies()
        try:
            self._page.goto(
                "https://www.xiaohongshu.com/",
                wait_until="domcontentloaded",
                timeout=30000,
            )
            self._page.evaluate("""async () => {
                try { localStorage.clear(); } catch (_) {}
                try { sessionStorage.clear(); } catch (_) {}
                try {
                    if (window.indexedDB && indexedDB.databases) {
                        const dbs = await indexedDB.databases();
                        await Promise.all((dbs || []).map(db => new Promise(resolve => {
                            if (!db.name) return resolve();
                            const req = indexedDB.deleteDatabase(db.name);
                            req.onsuccess = req.onerror = req.onblocked = () => resolve();
                        })));
                    }
                } catch (_) {}
                try {
                    if (window.caches) {
                        const keys = await caches.keys();
                        await Promise.all(keys.map(key => caches.delete(key)));
                    }
                } catch (_) {}
                try {
                    if (navigator.serviceWorker) {
                        const regs = await navigator.serviceWorker.getRegistrations();
                        await Promise.all(regs.map(reg => reg.unregister()));
                    }
                } catch (_) {}
            }""")
        except Exception as exc:
            self.log(f"[登录刷新] 清理站点存储时出现非致命异常: {exc}")

    def _force_interactive_login(self, timeout_s=180):
        self.log("[登录刷新] 登录记录已超过3天或当前会话无效，强制刷新")
        self._close_playwright()
        self.args.headless = False
        self._ensure_playwright()
        self._clear_site_login_state()
        self._clear_api()

        self._page.goto(
            "https://www.xiaohongshu.com/",
            wait_until="domcontentloaded",
            timeout=30000,
        )
        self._page.wait_for_timeout(2000)
        try:
            login_button = self._page.get_by_text("登录", exact=True).first
            if login_button.is_visible():
                login_button.click()
        except Exception:
            pass

        self.log(f"[登录刷新] 请在浏览器中扫码登录，最多等待 {timeout_s} 秒...")
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            nickname = self._latest_logged_in_user()
            if nickname:
                self._save_current_cookies()
                self.auth_state.mark_login("xhs")
                self.log(f"[登录刷新] ✓ 登录成功: {nickname}")
                return True
            self._page.wait_for_timeout(1000)

        self.log("[登录刷新] ✗ 等待扫码登录超时")
        return False

    def ensure_login_ready(self, max_age_days=LOGIN_MAX_AGE_DAYS):
        """每次抓取前执行：3天内验证会话，超期或失效则强制扫码刷新。"""
        state = self.auth_state.get("xhs")
        if not self.auth_state.is_login_due("xhs", max_age_days=max_age_days):
            last_login = state.get("last_login_at") if state else "?"
            self.log(f"[登录门禁] 最近登录时间 {last_login}，未超过{max_age_days}天，验证现有会话")
            if self.check_login():
                self.auth_state.mark_verified("xhs")
                self.log("[登录门禁] ✓ 持久化会话有效，无需重复扫码或注入 Cookie")
                return True
            self.log("[登录门禁] 会话验证失败，转为强制刷新")
        else:
            last_login = state.get("last_login_at") if state else None
            reason = f"上次登录时间 {last_login}" if last_login else "没有登录时间记录"
            self.log(f"[登录门禁] {reason}，需要强制刷新")

        return self._force_interactive_login()

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
        self.log(f"[搜索] 关键词='{keyword}' 第{page}页 排序={sort}")
        self._page.goto(
            "https://www.xiaohongshu.com/",
            wait_until="domcontentloaded",
            timeout=30000,
        )
        self._page.wait_for_timeout(1800)

        search_input = None
        for selector in (
            "input[placeholder*='搜索']",
            "input[type='search']",
            ".search-input input",
            "input[type='text']",
        ):
            candidate = self._page.query_selector(selector)
            if candidate and candidate.is_visible():
                search_input = candidate
                break

        if not search_input:
            self.log("[搜索] ✗ 首页未找到可见搜索框")
            return []

        search_input.click()
        search_input.press("Control+A")
        search_input.type(keyword, delay=random.randint(80, 160))
        cursor = self._api_cursor()

        submitted = False
        for selector in (
            "button:has-text('搜索')",
            ".search-icon",
            "[class*='search-icon']",
            "[data-testid='search-button']",
        ):
            button = self._page.query_selector(selector)
            if button and button.is_visible():
                try:
                    button.click()
                    submitted = True
                    break
                except Exception:
                    pass
        if not submitted:
            search_input.press("Enter")

        resp = self._wait_for_api_with_items(
            "search/notes",
            after_seq=cursor,
            timeout_s=15,
        )
        if not resp:
            self.log(f"[搜索] ✗ 未捕获到搜索 API")
            return []

        if sort != "general":
            sort_labels = {
                "popular": ("最热", "最多点赞", "热度"),
                "new": ("最新", "最新发布", "时间"),
            }.get(sort, ())
            sort_cursor = self._api_cursor()
            sort_clicked = False
            for label in sort_labels:
                try:
                    option = self._page.get_by_text(label, exact=True).first
                    if option.is_visible():
                        option.click()
                        sort_clicked = True
                        break
                except Exception:
                    pass
            if sort_clicked:
                sorted_resp = self._wait_for_api_with_items(
                    "search/notes",
                    after_seq=sort_cursor,
                    timeout_s=10,
                )
                if sorted_resp:
                    resp = sorted_resp
                else:
                    self.log(f"[搜索] 排序={sort} 已点击，但未捕获新响应，使用当前结果")
            else:
                self.log(f"[搜索] 未找到排序={sort} 的可见选项，使用综合排序")

        body = resp["body"]
        if body.get("code") != 0:
            self.log(f"[搜索] ✗ API 错误: code={body.get('code')}, msg={body.get('msg','?')}")
            return []

        items = body.get("data", {}).get("items", []) or []
        self.log(f"[搜索] ✓ 返回 {len(items)} 条笔记")

        return self._parse_search_items(items)

    def _scroll_load_more(self, max_scrolls=5):
        new_items = []

        for _ in range(max_scrolls):
            resp = self._scroll_until_new_search_response(timeout_s=10)
            if resp and resp["body"].get("code") == 0:
                items = resp["body"].get("data", {}).get("items", []) or []
                if items:
                    new_items.extend(items)
                    break

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

    def _find_card_for_note(self, cards, note_id, fallback_index):
        """优先按 note_id 定位卡片，避免虚拟列表变化造成索引错位。"""
        if note_id:
            for card in cards:
                try:
                    href = card.get_attribute("href") or ""
                    if note_id in href:
                        return card
                    link = card.query_selector(f"a[href*='{note_id}']")
                    if link:
                        return card
                except Exception:
                    continue
        if 0 <= fallback_index < len(cards):
            return cards[fallback_index]
        return None

    def _extract_pubtime_tags_from_dom(self):
        try:
            return self._page.evaluate(r"""() => {
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

                // ── 作者主页链接（用于获取 user_id 和粉丝数）──────────────
                const authorLink = mask.querySelector('a[href*="/user/profile/"]');
                if (authorLink) {
                    const href = authorLink.getAttribute('href') || '';
                    const m = href.match(/\/user\/profile\/([a-f0-9]+)/);
                    if (m) result.author_user_id = m[1];
                }

                // ── note_id（备用） ────────────────────────────────────────
                const noteIdAttr = mask.getAttribute('note-id') || mask.getAttribute('data-note-id');
                if (noteIdAttr) result.note_id = noteIdAttr;
                // 再兜底：从 location.href 里的 /explore/<24hex> 或 /discovery/item/<24hex> 抠
                if (!result.note_id) {
                    const hm = (location.pathname || '').match(/(?:explore|discovery\/item)\/([A-Fa-f0-9]{20,})/);
                    if (hm) result.note_id = hm[1];
                }

                // ── 标题 title（和 share_link 同 DOM 来源，避免错位） ──────
                // 常见 class: .title / .note-title + 组件哈希；兜底：浏览器 tab title 切「 - 小红书」前的文字
                const titleEl =
                    mask.querySelector('[class*="title"][data-v-772fa76f]') ||
                    mask.querySelector('[data-v-772fa76f][class*="note-title"]') ||
                    mask.querySelector('.note-content .title, .note-content [class*="title"]') ||
                    mask.querySelector('.title') ||
                    null;
                if (titleEl) {
                    const t = (titleEl.innerText || titleEl.textContent || '').trim();
                    if (t) result.title = t.replace(/\s+/g, ' ').slice(0, 200);
                }
                if (!result.title) {
                    const docTitle = (document.title || '').trim();
                    const idx = docTitle.lastIndexOf(' - 小红书');
                    const sliced = (idx >= 0 ? docTitle.slice(0, idx) : docTitle).trim();
                    if (sliced) result.title = sliced.replace(/\s+/g, ' ').slice(0, 200);
                }

                // ── 昵称 nickname（和 share_link 同 DOM 来源） ────────────
                // 常见：作者头像旁边的 .username / .nickname / .name；兜底用 a[href*="/user/profile/"] 的文本
                let nickEl =
                    mask.querySelector('[class*="username"][data-v-772fa76f]') ||
                    mask.querySelector('[class*="nickname"][data-v-772fa76f]') ||
                    mask.querySelector('[class*="author"] [class*="name"]') ||
                    mask.querySelector('.username, .nickname') ||
                    null;
                if (!nickEl && authorLink) {
                    // authorLink 的父节点同级/子节点里找第一个非空短文本
                    const box = authorLink.closest('[class*="author"], [class*="user"]') || authorLink.parentElement;
                    if (box) {
                        const texts = [];
                        (function walk(r){
                            if (!r) return;
                            if (r.childElementCount === 0) {
                                const t = (r.innerText || r.textContent || '').trim();
                                if (t && t.length <= 40 && !/^\/user\//.test(t)) texts.push(t);
                            }
                            for (const c of r.children || []) walk(c);
                        })(box);
                        if (texts.length) result.nickname = texts[0].replace(/\s+/g, ' ');
                    }
                }
                if (!result.nickname && nickEl) {
                    const t = (nickEl.innerText || nickEl.textContent || '').trim();
                    if (t) result.nickname = t.replace(/\s+/g, ' ').slice(0, 40);
                }
                // 兜底：authorLink.href 若有 query 里的 nickname（极少）
                if (!result.nickname && authorLink) {
                    try {
                        const up = new URL(authorLink.href, location.origin);
                        const candidate = up.searchParams.get('nickname') || up.searchParams.get('name');
                        if (candidate) result.nickname = decodeURIComponent(candidate).replace(/\s+/g, ' ').slice(0, 40);
                    } catch(_) {}
                }

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

    def _fetch_fans_count(self, user_id):
        """后台 fetch 用户主页 HTML，解析粉丝数。不跳转页面。"""
        if not user_id:
            return 0
        # 先查数据库缓存
        cached = self.db.get_user_fans(user_id)
        if cached > 0:
            return cached
        try:
            result = self._page.evaluate(r"""async (userId) => {
                try {
                    const resp = await fetch('/user/profile/' + userId, {
                        credentials: 'include',
                        headers: {'Accept': 'text/html'}
                    });
                    if (!resp.ok) return null;
                    const html = await resp.text();
                    // 方式1: meta 标签 <meta name="description" content="...粉丝数: 1.2万...">
                    const metaDesc = html.match(/<meta[^>]*name="description"[^>]*content="([^"]*)"/i);
                    if (metaDesc) {
                        const fansMatch = metaDesc[1].match(/粉丝数[：:]\s*([\d.]+[万亿]?)/);
                        if (fansMatch) return fansMatch[1];
                    }
                    // 方式2: JSON-LD 或初始化数据
                    const initData = html.match(/window\.__INITIAL_STATE__\s*=\s*({.*?})\s*<\/script>/s);
                    if (initData) {
                        try {
                            const state = JSON.parse(initData[1].replace(/undefined/g, 'null'));
                            const userInfo = state.user && (state.user.userPageData || state.user.me);
                            if (userInfo && userInfo.fansCount) return String(userInfo.fansCount);
                            if (userInfo && userInfo.fans) return String(userInfo.fans);
                        } catch(e) {}
                    }
                    // 方式3: DOM 中的粉丝数元素
                    const fansEl = html.match(/class="[^"]*fans[^"]*"[^>]*>([^<]+)</i);
                    if (fansEl) return fansEl[1].trim();
                    return null;
                } catch(e) { return null; }
            }""", user_id)
            if result:
                fans = _parse_chinese_num(result)
                if fans > 0:
                    # 缓存到数据库
                    nickname = ""  # 暂不知昵称，仅更新粉丝数
                    self.db.upsert_user(user_id, nickname, fans=fans)
                    return fans
        except Exception as e:
            self.log(f"[粉丝数] 获取 user={user_id} 失败: {e}")
        return 0

    def _get_share_link(self, note_id=""):
        """点击分享按钮 → 点「复制链接」→ 取完整分享链接。
        两级路径：hook clipboard.writeText（主路径） → 真实剪贴板读取（次路径）
        注意：DOM 全局正则、explore/{note_id} 构造链接都是错误的半残品，不再返回。
        注意：小红书 writeText / 剪贴板写入的是整段分享文案（包含序号/标题/emoji/反引号/空格/URL），
              必须从整段文字中抠出 URL 子串，不能要求 startswith('http')。
        """
        import re as _re

        _URL_RE = _re.compile(
            r"https?://"
            r"(?:[A-Za-z0-9-]+\.)*"
            r"(?:"
            r"xhslink\.com/[^\s\"'<>)]+"
            r"|"
            r"xiaohongshu\.com/discovery/item/[A-Fa-f0-9]+[^\s\"'<>)]*"
            r")"
        )

        def _pick(s):
            """从任意字符串（可能是整段分享文案）中抠出第一条合格的分享链接。
            合格：xhslink 短链；或 xiaohongshu/discovery/item/ + (xsec_token 或 xhsshare)
            """
            if not s:
                return ""
            text = str(s)
            hits = _URL_RE.findall(text)
            if not hits:
                return ""
            scored = []
            for u in hits:
                score = 0
                if "xhslink" in u:
                    score += 3
                if "xiaohongshu.com/discovery/item/" in u:
                    score += 1
                    if "xsec_token=" in u:
                        score += 5
                    if "xhsshare=" in u:
                        score += 2
                    if "source=webshare" in u:
                        score += 1
                scored.append((score, u))
            scored.sort(key=lambda x: x[0], reverse=True)
            best_score, best_url = scored[0]
            if best_score <= 1:
                # 只有 discovery/item 但没有任何签名参数 -> 半残品，不要
                return ""
            return best_url

        try:
            # 1. 点击分享按钮
            share_btn = self._page.query_selector(
                ".note-detail-mask .buttons[data-v-2820500a] .share-icon, "
                ".note-detail-mask .share-icon-container, "
                ".note-detail-mask [class*='share-icon'], "
                ".buttons .share-icon, .share-icon-container, [class*='share-icon']"
            )
            if not share_btn:
                self.log("[分享链接] 未找到分享按钮")
                return None
            share_btn.click()
            time.sleep(1.2)

            # 2. 等分享弹窗（两种都可能）
            share_popup = None
            popup1 = None
            popup2 = None
            for _ in range(6):
                popup1 = self._page.query_selector(
                    ".share-wrapper[data-v-fe59674e], .share-wrapper"
                )
                popup2 = self._page.query_selector(
                    ".xhs-note-share-popup[data-v-0d218a15], .xhs-note-share-popup"
                )
                p1_ok = bool(popup1 and popup1.is_visible())
                p2_ok = bool(popup2 and popup2.is_visible())
                if p1_ok or p2_ok:
                    share_popup = popup1 if p1_ok else popup2
                    break
                time.sleep(0.5)

            if not share_popup:
                self.log("[分享链接] 分享弹窗未出现")
                try:
                    self._page.keyboard.press("Escape")
                except Exception:
                    pass
                return None

            popup1_found = bool(popup1 and popup1.is_visible())
            popup2_found = bool(popup2 and popup2.is_visible())

            # 调试打印（头+尾）
            dbg_target = popup2 if popup2_found else popup1
            dbg_html = ""
            try:
                if dbg_target:
                    dbg_html = dbg_target.inner_html()
            except Exception:
                dbg_html = ""
            head = dbg_html[:400] if dbg_html else ""
            tail = dbg_html[-1100:] if len(dbg_html) > 1100 else dbg_html
            self.log(f"[分享链接] 弹窗调试: popup1={popup1_found} popup2={popup2_found} html_len={len(dbg_html)} head={repr(head)} tail={repr(tail)}")

            link = ""
            clicked_ok = False

            # 3. 安装/重置 clipboard hook（必须在点复制按钮之前）
            try:
                self._page.evaluate("""() => {
                    try {
                        window.__xhs_last_share_link__ = '';
                        window.__xhs_share_link_log__ = [];
                        const isShareLike = (s) => typeof s === 'string' && s && (
                            s.includes('xhslink.com/') ||
                            (s.includes('xiaohongshu.com/discovery/item/') && (s.includes('xsec_token=') || s.includes('xhsshare=')))
                        );
                        if (navigator.clipboard && typeof navigator.clipboard.writeText === 'function') {
                            if (!navigator.clipboard.__xhs_orig_writeText) {
                                navigator.clipboard.__xhs_orig_writeText = navigator.clipboard.writeText.bind(navigator.clipboard);
                            }
                            const orig = navigator.clipboard.__xhs_orig_writeText;
                            navigator.clipboard.writeText = function(txt) {
                                try {
                                    const s = (txt == null ? '' : String(txt));
                                    window.__xhs_share_link_log__.push(s);
                                    if (isShareLike(s)) {
                                        window.__xhs_last_share_link__ = s;
                                    }
                                } catch(_) {}
                                return orig(txt);
                            };
                        }
                        if (typeof document.execCommand === 'function') {
                            if (!document.__xhs_orig_exec) {
                                document.__xhs_orig_exec = document.execCommand.bind(document);
                            }
                            const origE = document.__xhs_orig_exec;
                            document.execCommand = function(cmdId, showUI, value) {
                                try {
                                    if (cmdId && /copy/i.test(String(cmdId))) {
                                        const sel = window.getSelection && window.getSelection();
                                        const selStr = (sel && typeof sel.toString === 'function') ? sel.toString() : '';
                                        const candidate = (value != null ? String(value) : '') || selStr;
                                        if (isShareLike(candidate)) {
                                            window.__xhs_last_share_link__ = candidate;
                                            window.__xhs_share_link_log__.push(candidate);
                                        }
                                    }
                                } catch(_) {}
                                return origE(cmdId, showUI, value);
                            };
                        }
                    } catch(_) {}
                }""")
            except Exception as e:
                self.log(f"[分享链接] 安装hook异常: {e}")

            # 4. popup2「复制链接」按钮
            if popup2_found:
                copy_el = None
                try:
                    action_items = popup2.query_selector_all(
                        ".xhs-note-share-popup-action-item, [class*='share-popup-action-item']"
                    )
                    for it in action_items:
                        try:
                            label = it.query_selector(
                                ".xhs-note-share-popup-action-label, [class*='share-popup-action-label']"
                            )
                            txt = (label.inner_text() if label else "") or ""
                            if "复制链接" in txt:
                                copy_el = it
                                break
                        except Exception:
                            pass
                except Exception:
                    pass
                if not copy_el:
                    label_list = popup2.query_selector_all(
                        ".xhs-note-share-popup-action-label, [class*='share-popup-action-label']"
                    )
                    for lab in label_list:
                        try:
                            txt = (lab.inner_text() or "")
                            if "复制链接" in txt:
                                copy_el = lab
                                break
                        except Exception:
                            pass
                if copy_el:
                    try:
                        copy_el.click()
                        clicked_ok = True
                        self.log("[分享链接] ✓ 点击 popup2「复制链接」按钮")
                        time.sleep(1.6)
                    except Exception as e:
                        self.log(f"[分享链接] 点击复制链接按钮异常: {e}")
                else:
                    self.log("[分享链接] popup2 未找到「复制链接」按钮元素")

            # 5. popup1 兼容
            if not clicked_ok and popup1_found:
                try:
                    items = popup1.query_selector_all(".item[data-v-fe59674e], .item")
                    for it in items:
                        try:
                            tip = it.get_attribute("data-tooltip") or ""
                            if "复制" in tip or "链接" in tip:
                                it.click()
                                clicked_ok = True
                                self.log(f"[分享链接] ✓ 点击 popup1 item tooltip='{tip}'")
                                time.sleep(1.6)
                                break
                        except Exception:
                            pass
                    if not clicked_ok and items:
                        items[0].click()
                        clicked_ok = True
                        self.log("[分享链接] 降级点击 popup1 第一个 item")
                        time.sleep(1.6)
                except Exception as e:
                    self.log(f"[分享链接] popup1 item 异常: {e}")

            # 6. 主路径：读 hook 捕获的 writeText / execCommand 入参
            if clicked_ok:
                try:
                    hook_info = self._page.evaluate("""() => {
                        const last = (typeof window.__xhs_last_share_link__ === 'string') ? window.__xhs_last_share_link__ : '';
                        const logArr = Array.isArray(window.__xhs_share_link_log__) ? window.__xhs_share_link_log__.slice(-15) : [];
                        window.__xhs_last_share_link__ = '';
                        if (Array.isArray(window.__xhs_share_link_log__)) window.__xhs_share_link_log__.length = 0;
                        return { last, log: logArr };
                    }""")
                    last_link = ""
                    hook_log = []
                    if isinstance(hook_info, dict):
                        last_link = (hook_info.get("last") or "").strip()
                        hook_log = hook_info.get("log") or []
                    picked_hook = _pick(last_link)
                    if picked_hook:
                        link = picked_hook
                        self.log(f"[分享链接] ✓ hook剪贴板写入捕获成功: {link}")
                    else:
                        for n, entry in enumerate(reversed(list(hook_log))):
                            p = _pick(entry)
                            if p:
                                link = p
                                self.log(f"[分享链接] ✓ hook日志(倒数第{n+1}条)命中: {link}")
                                break
                        if not link:
                            self.log(f"[分享链接] hook未命中 last={repr(last_link[:160])} log={[repr(x[:120]) for x in hook_log]!r}")
                except Exception as e:
                    self.log(f"[分享链接] hook读取异常: {e}")

            # 7. 次路径：真实剪贴板读取（context 已授权 clipboard-read）
            if clicked_ok and not link:
                try:
                    cb_raw = self._page.evaluate(
                        "() => { try { return navigator.clipboard.readText(); } catch(e) { return ''; } }"
                    )
                    picked = _pick(cb_raw)
                    if picked:
                        link = picked
                        self.log(f"[分享链接] ✓ 真实剪贴板读取成功: {link}")
                    else:
                        if isinstance(cb_raw, str) and cb_raw:
                            self.log(f"[分享链接] 剪贴板非分享链接: {repr(cb_raw[:180])}")
                        else:
                            self.log("[分享链接] 剪贴板为空")
                except Exception as e:
                    self.log(f"[分享链接] 剪贴板读取异常: {e}")

            # 8. 关弹窗
            try:
                self._page.keyboard.press("Escape")
                time.sleep(0.4)
            except Exception:
                pass

            if link:
                return link
            self.log("[分享链接] ✗ hook+剪贴板均未命中有效分享链接，返回 None")
            return None

        except Exception as e:
            self.log(f"[分享链接] 最外层异常: {e}")
            try:
                self._page.keyboard.press("Escape")
            except Exception:
                pass
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

    def _ensure_modal_closed(self):
        """确保当前没有残留的笔记弹窗，避免干扰下一次点击。"""
        try:
            mask = self._page.query_selector(".note-detail-mask")
            if mask and mask.is_visible():
                self.log("[详情] 检测到残留弹窗，正在关闭...")
                close_btn = self._page.query_selector(
                    ".note-detail-mask .close-btn, .note-detail-mask .icon-close, "
                    ".note-detail-mask [data-testid='close'], .note-detail-mask .m-close"
                )
                if close_btn and close_btn.is_visible():
                    close_btn.click()
                else:
                    self._page.keyboard.press("Escape")
                time.sleep(1)
                # 再确认一次
                mask2 = self._page.query_selector(".note-detail-mask")
                if mask2 and mask2.is_visible():
                    self._page.keyboard.press("Escape")
                    time.sleep(0.5)
        except Exception:
            pass

    def _get_detail_by_card_index(self, card_index, search_note_data=None):
        search_note_id = (
            (search_note_data.get("note_id", "") if search_note_data else "") or ""
        )
        cards = self._get_all_cards()
        card = self._find_card_for_note(cards, search_note_id, card_index)
        if not card:
            for _ in range(5):
                try:
                    self._wheel_scroll(steps=1, minimum=600, maximum=900)
                except Exception:
                    pass
                cards = self._get_all_cards()
                card = self._find_card_for_note(cards, search_note_id, card_index)
                if card:
                    break

        if not card:
            self.log(
                f"[详情] ✗ 未找到卡片 index={card_index} note_id={search_note_id}"
            )
            return None

        # 点击前确保没有残留弹窗
        self._ensure_modal_closed()

        # 最多重试 2 次点击
        for attempt in range(3):
            try:
                card.scroll_into_view_if_needed()
                time.sleep(0.8 + attempt * 0.5)  # 重试时等更久
                card.click()
            except Exception as e:
                try:
                    self._page.evaluate("arguments[0].click()", card)
                    time.sleep(0.8)
                except Exception:
                    self.log(f"[详情] ✗ 点击第{card_index}个卡片失败: {e}")
                    return None

            try:
                self._page.wait_for_selector(".note-detail-mask", timeout=6000)
                break  # 弹窗出现，跳出重试循环
            except Exception:
                if attempt < 2:
                    self.log(f"[详情] 第{card_index}个卡片第{attempt+1}次点击无弹窗，重试...")
                    try:
                        self._page.keyboard.press("Escape")
                    except Exception:
                        pass
                    time.sleep(1)
                    # 重新获取卡片引用（页面可能已刷新）
                    cards = self._get_all_cards()
                    refreshed = self._find_card_for_note(
                        cards, search_note_id, card_index
                    )
                    if refreshed:
                        card = refreshed
                else:
                    self.log(f"[详情] ✗ 第{card_index}个卡片重试{attempt+1}次均无弹窗")
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

        # ── 防错位：title / nickname / note_id 一律从详情弹窗 DOM 读取（和 share_link 同一来源） ──
        dom_note_id = (dom_data.get("note_id", "") if dom_data else "") or ""
        dom_title = (dom_data.get("title", "") if dom_data else "") or ""
        dom_nickname = (dom_data.get("nickname", "") if dom_data else "") or ""

        # 若传入的 search_note_data 的 note_id 与 DOM 实际打开的 note_id 不一致，打告警（列表API和卡片点击错位发生了）
        if search_note_data and search_note_id and dom_note_id and (search_note_id != dom_note_id):
            api_title = (search_note_data.get("title", "") or "")[:25]
            self.log(
                f"[错位⚠️] 列表note_id={search_note_id} title='{api_title}' ≠ "
                f"DOM note_id={dom_note_id} title='{dom_title[:25]}'，"
                f"后续 title/nickname/note_id 以 DOM 为准（保证 share_link 一致）"
            )

        # 提前拿 note_id 传给分享链接兜底
        note_id_for_share = dom_note_id or search_note_id

        # 获取分享链接（在弹窗关闭前）
        existing_share_link = self.db.get_existing_share_link(note_id_for_share)
        if existing_share_link:
            share_link = existing_share_link
            share_link_attempted = False
            self.log(f"[分享链接] 已有有效链接，跳过重复复制: {note_id_for_share}")
        else:
            share_link = self._get_share_link(note_id=note_id_for_share)
            share_link_attempted = True

        try:
            close_btn = self._page.query_selector(".note-detail-mask .close-btn, .note-detail-mask .icon-close, .note-detail-mask [data-testid='close'], .note-detail-mask .m-close")
            if close_btn and close_btn.is_visible():
                close_btn.click()
            else:
                self._page.keyboard.press("Escape")
            time.sleep(1.0)  # 多等一下让页面恢复
        except Exception:
            try:
                self._page.keyboard.press("Escape")
                time.sleep(0.8)
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
                 f"share_text='{dbg.get('share_text')}' "
                 f"author_uid='{dom_data.get('author_user_id', '')}' "
                 f"dom_title='{dom_title[:25]}' dom_nickname='{dom_nickname[:15]}'")

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

        # note_id / title / nickname 一律以 DOM 为准；DOM 缺失才退化回 search_note_data（并打日志）
        final_note_id = dom_note_id or search_note_id
        if not dom_title and search_note_data:
            dom_title = search_note_data.get("title", "") or ""
            if dom_title:
                self.log(f"[详情] title DOM未命中，退化使用搜索列表 title: '{dom_title[:25]}'")
        if not dom_nickname and search_note_data:
            dom_nickname = search_note_data.get("nickname", "") or ""
            if dom_nickname:
                self.log(f"[详情] nickname DOM未命中，退化使用搜索列表 nickname: '{dom_nickname[:15]}'")

        # 粉丝数不参与当前动量/价值评分，主流程只读取缓存，不再后台请求作者主页。
        author_uid = dom_data.get("author_user_id", "") or (search_note_data.get("user_id", "") if search_note_data else "")
        fans_count_val = self.db.get_user_fans(author_uid) if author_uid else 0

        # note_type：优先取 DOM（暂无提取），再退化搜索列表
        note_type_final = (search_note_data.get("note_type", "normal") if search_note_data else "normal") or "normal"

        detail = {
            "note_id": final_note_id,
            "title": dom_title,
            "description": "",
            "liked_count": liked,
            "collected_count": collected,
            "comment_count": comment,
            "share_count": share_count_final,
            "interact_count": interact,
            "nickname": dom_nickname,
            "user_id": author_uid,
            "fans_count": fans_count_val,
            "pub_time": pub_time,
            "pub_time_ms": pub_time_ms,
            "note_type": note_type_final,
            "tags": dom_data.get("tags", ""),
            "note_age_hours": note_age_hours,
            "interact_velocity": interact_velocity,
            "engagement_score": engagement,
            "share_link": share_link or "",
            "url": share_link or "",  # DB 存储用
            "share_link_status": "success" if share_link else "failed",
            "share_link_attempted": share_link_attempted,
            "share_link_error": "" if share_link else "复制链接失败",
        }

        self.log(f"[详情] ✓ '{detail['title'][:30]}' "
                 f"note_id={detail['note_id']} "
                 f"互动={interact} 赞={detail['liked_count']} 藏={detail['collected_count']} 评={detail['comment_count']} 粉丝={fans_count_val}")
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
                self.db.link_task_note(
                    self.task_id,
                    n["note_id"],
                    search_rank=len(all_notes),
                    xsec_token=n.get("xsec_token", ""),
                    title=n.get("title", ""),
                )

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

                resp = self._scroll_until_new_search_response(timeout_s=15)
                if resp and resp["body"].get("code") == 0:
                    items = resp["body"].get("data", {}).get("items", []) or []
                    new_notes = self._parse_search_items(items)
                    new_count = 0
                    for n in new_notes:
                        if n["note_id"] not in note_ids_seen:
                            note_ids_seen.add(n["note_id"])
                            all_notes.append(n)
                            self.db.link_task_note(
                                self.task_id,
                                n["note_id"],
                                search_rank=len(all_notes),
                                xsec_token=n.get("xsec_token", ""),
                                title=n.get("title", ""),
                            )
                            new_count += 1
                    if new_count > 0:
                        self.log(f"[任务] 滚动后新增 {new_count} 条，累计 {len(all_notes)} 条")
                    else:
                        self.log(f"[任务] 滚动后无新数据")
                else:
                    self.log(f"[任务] 滚动后未捕获到搜索 API")

        self.log(f"[任务] 本次抓取：{len(all_notes)} 条笔记，详情成功 {detailed_count} 条")
        link_completion = self.db.get_link_completion(self.task_id)
        task_status = "completed" if link_completion["missing"] == 0 else "partial"
        self.log(
            f"[链接完整性] {link_completion['completed']}/{link_completion['total']}，"
            f"缺失 {link_completion['missing']} 条，任务状态={task_status}"
        )
        self.db.update_task_status(
            self.task_id, task_status,
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

    def retry_missing_links(self, task_id, limit=None):
        """只补指定任务中尚未成功复制的分享链接。"""
        task = self.db.get_task(task_id)
        if not task:
            raise ValueError(f"任务不存在: {task_id}")

        pending = self.db.get_pending_share_links(task_id, limit=limit)
        if not pending:
            self.log(f"[补链接] 任务#{task_id} 没有缺失链接")
            return {"total": 0, "success": 0, "failed": 0}

        self._ensure_playwright()
        success = 0
        failed = 0
        self.log(f"[补链接] 任务#{task_id} 待处理 {len(pending)} 条")

        for index, item in enumerate(pending, 1):
            note_id = item["note_id"]
            token = item.get("xsec_token") or ""
            if not token:
                self.db.record_share_link_result(
                    note_id,
                    error="缺少 xsec_token，无法打开详情补链接",
                )
                failed += 1
                self.log(f"[补链接] {index}/{len(pending)} ✗ {note_id} 缺少 xsec_token")
                continue

            detail_url = (
                f"https://www.xiaohongshu.com/explore/{note_id}"
                f"?xsec_token={quote(token, safe='')}&xsec_source=pc_search"
            )
            try:
                self._page.goto(
                    detail_url,
                    wait_until="domcontentloaded",
                    timeout=30000,
                )
                self._page.wait_for_timeout(random.randint(1800, 3000))
                link = self._get_share_link(note_id=note_id) or ""
                self.db.record_share_link_result(
                    note_id,
                    share_link=link,
                    error="" if link else "复制链接失败",
                )
                if link:
                    success += 1
                    self.log(f"[补链接] {index}/{len(pending)} ✓ {note_id}")
                else:
                    failed += 1
                    self.log(f"[补链接] {index}/{len(pending)} ✗ {note_id}")
            except Exception as exc:
                failed += 1
                self.db.record_share_link_result(note_id, error=str(exc))
                self.log(f"[补链接] {index}/{len(pending)} 异常: {exc}")

            if index < len(pending):
                time.sleep(random.uniform(4, 9))

        completion = self.db.get_link_completion(task_id)
        status = "completed" if completion["missing"] == 0 else "partial"
        self.db.update_task_status(task_id, status)
        self.log(
            f"[补链接] 完成：成功={success} 失败={failed}，"
            f"完整性={completion['completed']}/{completion['total']}，状态={status}"
        )
        return {
            "total": len(pending),
            "success": success,
            "failed": failed,
            **completion,
        }

    def momentum_analysis(self, keyword, limit=999999, csv_file=None):
        self.log(f"[动量分析] 关键词='{keyword}'")
        results = self.db.get_keyword_momentum_ranking(keyword, limit=limit)
        if not results:
            self.log(f"[动量分析] 无数据，请先爬取关键词: {keyword}")
            return []

        self.log(f"[动量分析] 范围：关键词累计库，共 {len(results)} 条笔记")
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

        self.log(f"[价值分析] 范围：关键词累计库，共 {len(results)} 条笔记")
        self.log("")
        header = (
            f"{'排名':<4} {'标题':<30} {'互动量':>8} {'作者':<12} "
            f"{'收藏率':>7} {'密度':>7} {'评论率':>7} {'价值分':>7}"
        )
        self.log("-" * 120)
        self.log(header)
        self.log("-" * 120)
        for i, n in enumerate(results[:20], 1):
            title = (n["title"] or "")[:28]
            uploader = (n.get("nickname") or "")[:11]
            self.log(
                f"{i:<4} {title:<30} {n['interact_count']:>8,} {uploader:<12} "
                f"{n['collect_rate']:>7.3f} {n['engagement_score']:>7.3f} "
                f"{n['comment_rate']:>7.3f} "
                f"{n['value_score']:>7.3f}"
            )
        if len(results) > 20:
            self.log(f"  ... 还有 {len(results) - 20} 条，完整结果请查看 CSV")
        self.log("-" * 120)
        self.log("提示: 价值分 = 收藏率(40%) + 互动密度(30%) + 评论率(30%)")
        self.log("      收藏率 = 收藏/点赞，互动密度 = 收藏/互动总量（越高=越多人深度保存）")

        if csv_file:
            self.db.export_value_csv(keyword, csv_file, results)

        return results

    def close(self):
        self._close_playwright()
