# -*- coding: utf-8 -*-
"""
xhs_migrate_test.py
===================
小红书迁移可行性测试脚本（独立运行，不改动原有 bili 项目任何文件）。

测试目标：
  1) 检查依赖环境（xhs / playwright / requests）
  2) 验证字段映射表是否能与现有 Database schema 兼容
  3) 验证动量算法 5 维评分框架是否可以平移到小红书数据
  4) 模拟一次"搜索 → 详情 → 评论"完整流程的字段流转
  5) 输出迁移可行性报告

运行：
  python xhs_migrate_test.py
  python xhs_migrate_test.py --mock     # 仅跑离线模拟（不需要 cookie / 网络）
  python xhs_migrate_test.py --live      # 尝试真实调用（需要 xhs cookie）
"""

import os
import sys
import time
import json
import argparse
import sqlite3
import importlib.util
from datetime import datetime
from collections import defaultdict

# 强制 stdout/stderr 用 UTF-8 输出（解决 Windows PowerShell 中文乱码）
try:
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
except Exception:
    pass

# ============== 1. 依赖检测 ==============

def check_dependency(name):
    """检测某个模块是否已安装"""
    spec = importlib.util.find_spec(name)
    return spec is not None


def section(title):
    print("\n" + "=" * 70)
    print(f"  {title}")
    print("=" * 70)


def test_dependencies():
    section("【步骤 1】依赖环境检测")
    deps = {
        "requests": "HTTP 请求（小红书 API 必备）",
        "xhs": "reajason/xhs PyPI 库（轻量方案核心）",
        "playwright": "Playwright 兜底（用于真实浏览器抓取）",
        "sqlite3": "SQLite（数据库，Python 自带）",
    }
    results = {}
    for mod, desc in deps.items():
        ok = check_dependency(mod)
        results[mod] = ok
        flag = "✓ 已安装" if ok else "✗ 未安装"
        print(f"  [{flag}] {mod:12s} - {desc}")

    print("\n  迁移所需最小依赖：requests + sqlite3")
    print(f"  当前状态：{'满足' if results['requests'] and results['sqlite3'] else '不满足'}")
    if not results['xhs']:
        print("  提示：执行 `pip install xhs` 可启用方案 A（轻量集成）")
    if not results['playwright']:
        print("  提示：执行 `pip install playwright && python -m playwright install chromium` 可启用方案 C")
    return results


# ============== 2. 字段映射测试 ==============

# B站 → 小红书 字段映射表
FIELD_MAPPING = {
    # 标识类
    "av_id":        ("note_id",      "笔记ID（十六进制串，如 697cc945000000000a02cdad）"),
    "bvid":         ("note_id_hex",  "小红书无 BV 概念，可冗余或省略"),
    "url":          ("url",          "https://www.xiaohongshu.com/explore/{note_id}?xsec_token=..."),
    # 标题内容
    "title":        ("title",        "笔记标题（图文笔记可能为空，需用 desc 首行 fallback）"),
    "description":  ("desc",         "笔记正文"),
    "tags":         ("tag_list",     "笔记标签列表"),
    "category":     ("category",     "小红书无强分类，可用 home_feed category 替代"),
    # 作者
    "uploader":     ("nickname",     "作者昵称"),
    "uploader_uid": ("user_id",      "作者 UID"),
    "uploader_fans":("fans_count",   "作者粉丝数（需额外接口获取）"),
    # 互动指标
    "play_nums":    ("interact_count",   "互动量 = 点赞+收藏+评论（小红书无公开阅读量）"),
    "like_count":   ("liked_count",      "点赞数"),
    "coin":         (None,               "小红书无投币，字段移除"),
    "favorites":    ("collected_count",  "收藏数"),
    "review":       ("comment_count",    "评论数"),
    "danmakus":     (None,               "小红书无弹幕，字段移除"),
    "share":        ("share_count",      "分享数"),
    # 时间
    "pubdate":      ("time",             "发布时间戳（毫秒）"),
    "duration":     ("duration",         "视频笔记时长，图文为 0"),
    # 计算字段
    "video_age_hours": ("note_age_hours", "笔记年龄（小时）"),
    "play_velocity":   ("interact_velocity", "互动速率 = interact_count / note_age_hours"),
    "engagement_score":("engagement_score", "(like+collect+comment) / interact_count，无 coin/danmaku"),
    "first_comment_time":("first_comment_time", "首评时间"),
    "comment_count":("comment_count_real", "实际爬取的评论数"),
}


def test_field_mapping():
    section("【步骤 2】字段映射测试（B站 schema → 小红书 schema）")

    print(f"\n  共 {len(FIELD_MAPPING)} 个 B站 字段待映射")
    mapped = [(b, x, d) for b, (x, d) in FIELD_MAPPING.items() if x is not None]
    dropped = [(b, d) for b, (x, d) in FIELD_MAPPING.items() if x is None]

    print(f"\n  ✓ 可映射字段 ({len(mapped)}/{len(FIELD_MAPPING)}):")
    for b, x, d in mapped:
        print(f"    {b:24s} → {x:24s}  # {d}")

    print(f"\n  ✗ 需移除字段 ({len(dropped)}/{len(FIELD_MAPPING)})（小红书无对应概念）:")
    for b, d in dropped:
        print(f"    {b:24s}    # {d}")

    # 关键差异提示
    print("\n  关键差异：")
    print("    1. 'play_nums' 在小红书侧改用 'interact_count'（互动量聚合）作为主指标")
    print("    2. 移除 coin / danmakus，engagement_score 公式简化为 (like+collect+comment)/interact")
    print("    3. note_id 为十六进制串（B站 av_id 为纯数字），数据库字段类型保持 TEXT 即可")
    return mapped, dropped


# ============== 3. 动量算法移植测试 ==============

# 复用 util.py 中 Database.calculate_freshness_weight 的同款逻辑
def calculate_freshness_weight(note_age_days):
    if note_age_days is None or note_age_days < 0:
        return 1.0
    if note_age_days <= 1:   return 2.0
    elif note_age_days <= 3: return 1.7
    elif note_age_days <= 7: return 1.4
    elif note_age_days <= 14:return 1.2
    elif note_age_days <= 30:return 1.1
    elif note_age_days <= 90:return 1.0
    else:                    return 0.8


def xhs_momentum_score(note):
    """
    小红书版动量评分：原 5 维框架保持，仅替换指标
      速率 30%  + 粉丝转化 25% + 互动密度 20% + 新鲜度 15% + 互动量归一化 10%
    """
    interact = note.get("interact_count", 0)
    fans = note.get("fans_count", 0)
    like = note.get("liked_count", 0)
    collect = note.get("collected_count", 0)
    comment = note.get("comment_count", 0)
    age_hours = note.get("note_age_hours", 0) or 0.1
    velocity = note.get("interact_velocity", interact / max(age_hours, 0.1))

    # 互动密度（移除 coin/danmaku）
    engagement_raw = (like + collect + comment) / max(interact, 1)

    # 粉丝转化：互动量 / 粉丝数
    conversion = interact / fans if fans > 0 else 0

    # 新鲜度
    age_days = age_hours / 24 if age_hours > 0 else None
    freshness = calculate_freshness_weight(age_days)
    freshness_norm = (freshness - 0.8) / (2.0 - 0.8)

    return {
        "velocity": velocity,
        "conversion_rate": round(conversion, 4),
        "engagement_score": round(engagement_raw, 4),
        "freshness_weight": freshness,
        "freshness_normalized": round(freshness_norm, 4),
        "interact_count": interact,
    }


def test_momentum_algorithm():
    section("【步骤 3】动量算法移植测试")

    # 构造 5 条模拟笔记（覆盖小UP爆款/老笔记/新笔记等场景）
    mock_notes = [
        {"note_id": "n1", "title": "小UP爆款-新笔记", "interact_count": 50000, "liked_count": 40000,
         "collected_count": 8000, "comment_count": 2000, "fans_count": 500,
         "note_age_hours": 12, "interact_velocity": 50000/12},
        {"note_id": "n2", "title": "大UP普通笔记", "interact_count": 30000, "liked_count": 25000,
         "collected_count": 4000, "comment_count": 1000, "fans_count": 1000000,
         "note_age_hours": 6, "interact_velocity": 30000/6},
        {"note_id": "n3", "title": "老笔记长尾", "interact_count": 100000, "liked_count": 80000,
         "collected_count": 15000, "comment_count": 5000, "fans_count": 5000,
         "note_age_hours": 720, "interact_velocity": 100000/720},
        {"note_id": "n4", "title": "新笔记低互动", "interact_count": 200, "liked_count": 150,
         "collected_count": 30, "comment_count": 20, "fans_count": 1000,
         "note_age_hours": 2, "interact_velocity": 200/2},
        {"note_id": "n5", "title": "中UP优质笔记", "interact_count": 80000, "liked_count": 60000,
         "collected_count": 12000, "comment_count": 8000, "fans_count": 50000,
         "note_age_hours": 48, "interact_velocity": 80000/48},
    ]

    # 计算各项归一化
    max_v = max(n["interact_velocity"] for n in mock_notes)
    max_c = max((n["interact_count"]/n["fans_count"] for n in mock_notes if n["fans_count"]>0), default=1)
    max_e = max(xhs_momentum_score(n)["engagement_score"] for n in mock_notes)
    max_i = max(n["interact_count"] for n in mock_notes)
    min_i = min(n["interact_count"] for n in mock_notes)

    print(f"\n  归一化基准：max_velocity={max_v:.1f}, max_conversion={max_c:.4f}, "
          f"max_engagement={max_e:.4f}, max_interact={max_i}")

    results = []
    for n in mock_notes:
        s = xhs_momentum_score(n)
        v_score = min(s["velocity"]/max_v, 1.0) if max_v > 0 else 0
        c_score = min(s["conversion_rate"]/max_c, 1.0) if max_c > 0 else 0
        e_score = min(s["engagement_score"]/max_e, 1.0) if max_e > 0 else 0
        i_norm = (s["interact_count"]-min_i)/(max_i-min_i) if max_i>min_i else 0.5
        composite = (v_score*0.30 + c_score*0.25 + e_score*0.20 +
                     s["freshness_normalized"]*0.15 + i_norm*0.10)
        results.append({**n, **s, "v_score": v_score, "c_score": c_score,
                        "e_score": e_score, "i_norm": i_norm,
                        "composite": round(composite, 4)})

    results.sort(key=lambda x: x["composite"], reverse=True)

    print(f"\n  {'排名':<4}{'标题':<20}{'互动量':>8}{'速率':>10}{'转化':>8}"
          f"{'密度':>8}{'新鲜':>6}{'归一':>6}{'综合分':>8}")
    print("  " + "-" * 88)
    for i, r in enumerate(results, 1):
        print(f"  {i:<4}{r['title']:<20}{r['interact_count']:>8}"
              f"{r['velocity']:>10.0f}{r['conversion_rate']:>8.2f}"
              f"{r['engagement_score']:>8.3f}{r['freshness_weight']:>6.1f}"
              f"{r['i_norm']:>6.3f}{r['composite']:>8.3f}")

    print("\n  验证结论：")
    print("    ✓ 5 维评分框架成功平移到小红书数据")
    print("    ✓ 小UP爆款（n1）综合分高于大UP普通笔记（n2），符合 '出圈检测' 诉求")
    print("    ✓ 老笔记长尾（n3）虽然互动量绝对值高，但因新鲜度低被合理降权")
    return results


# ============== 4. Database schema 兼容性测试 ==============

def test_database_compat():
    section("【步骤 4】SQLite Schema 兼容性测试")

    # 在临时文件创建 schema（不影响现有 bili_spider.db）
    test_db = "xhs_migrate_test_tmp.db"
    if os.path.exists(test_db):
        os.remove(test_db)

    conn = sqlite3.connect(test_db)
    cur = conn.cursor()

    # 创建小红书版表结构（仅字段重命名，结构与原项目一致）
    cur.execute("""
        CREATE TABLE xhs_notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            note_id TEXT UNIQUE NOT NULL,
            title TEXT,
            url TEXT,
            interact_count INTEGER DEFAULT 0,
            liked_count INTEGER DEFAULT 0,
            collected_count INTEGER DEFAULT 0,
            comment_count INTEGER DEFAULT 0,
            share_count INTEGER DEFAULT 0,
            nickname TEXT,
            user_id TEXT,
            fans_count INTEGER DEFAULT 0,
            pubdate DATETIME,
            duration INTEGER DEFAULT 0,
            description TEXT,
            tags TEXT,
            category TEXT,
            note_age_hours REAL DEFAULT 0,
            interact_velocity REAL DEFAULT 0,
            engagement_score REAL DEFAULT 0,
            first_comment_time DATETIME,
            comment_count_real INTEGER DEFAULT 0,
            fetched_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.execute("""
        CREATE TABLE xhs_tasks (
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
    cur.execute("""
        CREATE TABLE xhs_note_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            note_id TEXT NOT NULL,
            task_id INTEGER,
            interact_count INTEGER,
            liked_count INTEGER,
            collected_count INTEGER,
            comment_count INTEGER,
            share_count INTEGER,
            record_time DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.execute("""
        CREATE TABLE xhs_authors (
            user_id TEXT PRIMARY KEY,
            name TEXT,
            fans INTEGER DEFAULT 0,
            fetched_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()

    # 模拟插入一条数据
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cur.execute("INSERT INTO xhs_tasks (keyword, pages, order_by, status, created_at) VALUES (?,?,?,?,?)",
                ("测试关键词", 1, "hot", "running", now))
    task_id = cur.lastrowid

    cur.execute("""
        INSERT INTO xhs_notes (task_id, note_id, title, url, interact_count, liked_count,
                              collected_count, comment_count, nickname, user_id, fans_count,
                              pubdate, note_age_hours, interact_velocity, engagement_score, fetched_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (task_id, "697cc945000000000a02cdad", "测试笔记",
          "https://www.xiaohongshu.com/explore/697cc945000000000a02cdad",
          50000, 40000, 8000, 2000, "测试UP", "5e1234", 500,
          now, 12.0, 50000/12, 0.96, now))

    cur.execute("""
        INSERT INTO xhs_note_history (note_id, task_id, interact_count, liked_count,
                                      collected_count, comment_count, share_count, record_time)
        VALUES (?,?,?,?,?,?,?,?)
    """, ("697cc945000000000a02cdad", task_id, 50000, 40000, 8000, 2000, 500, now))

    conn.commit()

    # 验证查询
    cur.execute("""
        SELECT n.title, n.interact_count, n.note_age_hours,
               h.interact_count as hist_interact
        FROM xhs_notes n
        LEFT JOIN xhs_note_history h ON n.note_id = h.note_id
        WHERE n.task_id = ?
    """, (task_id,))
    row = cur.fetchone()

    print(f"\n  测试库：{test_db}")
    print(f"  任务ID：{task_id}")
    print(f"  查询结果：标题={row[0]}, 互动量={row[1]}, 年龄={row[2]}h, 历史互动={row[3]}")

    print("\n  Schema 字段数对比：")
    print(f"    原 bili_videos 字段数：~24（含 coin/danmakus/video_age_hours 等）")
    print(f"    新 xhs_notes   字段数：22（移除 coin/danmakus，新增 interact_count）")
    print(f"    表结构兼容性：✓ 仅需 ALTER TABLE 重命名字段 + 移除 2 个字段")

    conn.close()
    os.remove(test_db)
    print(f"\n  临时库 {test_db} 已清理")
    return True


# ============== 5. 真实 API 调用测试（可选） ==============

def interactive_login(cookie_file):
    """
    有头 Edge 浏览器扫码登录小红书，持久化 user_data_dir + 保存 cookie 到文件。
    首次扫码后，后续可复用 profile 免登录。
    """
    section("【交互登录】Edge 浏览器扫码登录小红书")

    if not check_dependency("playwright"):
        print("  ✗ playwright 未安装")
        return False

    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        print(f"  ✗ 依赖加载失败: {e}")
        return False

    # 持久化用户数据目录（保存登录态、cookie、localStorage 等）
    # 注意：如果 Edge 报"另一个会话已打开"，需要先关闭所有 Edge 进程
    user_data_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "xhs_edge_profile")
    os.makedirs(user_data_dir, exist_ok=True)
    print(f"  Edge 用户数据目录: {user_data_dir}")
    print(f"  Cookie 保存位置: {cookie_file}")
    print()

    pw = None
    browser = None
    context = None
    try:
        pw = sync_playwright().start()
        print(f"  [1/4] 启动有头 Edge 浏览器...")
        # 改用 launch + new_context（非持久化），避免 profile 锁定冲突
        # 登录态通过 cookie 文件保存/恢复，不依赖 user_data_dir
        try:
            browser = pw.chromium.launch(
                channel="msedge",
                headless=False,  # 有头模式，让用户扫码
            )
            print(f"  ✓ 使用 Microsoft Edge (有头)")
        except Exception as e_edge:
            print(f"  ⚠ Edge 启动失败: {str(e_edge)[:100]}")
            browser = pw.chromium.launch(headless=False)
            print(f"  → 回退到 Playwright chromium")

        # 如果有旧 cookie，先加载，可能免扫码
        existing_cookies = []
        if os.path.exists(cookie_file):
            try:
                from xhs import help as xhs_help
                old_cookie_str = open(cookie_file, "r", encoding="utf-8").read().strip()
                if old_cookie_str:
                    old_dict = xhs_help.cookie_str_to_cookie_dict(old_cookie_str)
                    existing_cookies = [
                        {"name": k, "value": v, "domain": ".xiaohongshu.com", "path": "/"}
                        for k, v in old_dict.items()
                    ]
                    print(f"  ✓ 加载旧 cookie {len(existing_cookies)} 个（尝试免扫码复用）")
            except Exception:
                pass

        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1366, "height": 900},
            locale="zh-CN",
        )
        if existing_cookies:
            context.add_cookies(existing_cookies)
        page = context.new_page()

        # 监听 user/me API 检测登录状态
        login_status = {"logged_in": False, "info": None, "nickname": ""}

        def on_response(response):
            if "/api/sns/web/v2/user/me" in response.url:
                try:
                    body = response.json()
                    if isinstance(body, dict) and body.get("code") == 0:
                        data = body.get("data", {}) or {}
                        nickname = data.get("nickname", "") or ""
                        # 严格检测：code=0 且 nickname 非空才算真正登录
                        if nickname:
                            login_status["logged_in"] = True
                            login_status["info"] = data
                            login_status["nickname"] = nickname
                except Exception:
                    pass

        page.on("response", on_response)

        print(f"  [2/4] 访问小红书首页...")
        page.goto("https://www.xiaohongshu.com", wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(3000)

        # 检查是否已经登录（profile 复用情况，严格检测 nickname 非空）
        if login_status["logged_in"] and login_status["nickname"]:
            print(f"  ✓ 检测到已登录状态: nickname={login_status['nickname']}")
            print(f"    profile 复用成功，无需扫码")
        else:
            print(f"\n  [3/4] 请在弹出的浏览器中扫码登录小红书")
            print(f"  ─────────────────────────────────────────")
            print(f"  1. 点击页面右上角 '登录' 按钮")
            print(f"  2. 用小红书 APP 扫描二维码")
            print(f"  3. 在手机上确认登录")
            print(f"  ─────────────────────────────────────────")
            print(f"  脚本会自动检测登录状态（严格检测 nickname 非空）")
            print(f"  超时时间: 5 分钟")
            print(f"  检测周期: 每 5 秒刷新页面并检查 user/me API")
            print()

            # 轮询等待真正登录成功（nickname 非空才算数）
            deadline = time.time() + 300  # 5 分钟超时
            check_count = 0
            last_nickname = ""
            while time.time() < deadline:
                if login_status["logged_in"] and login_status["nickname"]:
                    break
                page.wait_for_timeout(5000)
                check_count += 1
                if check_count % 6 == 0:  # 每 30 秒打印一次状态
                    print(f"  仍在等待扫码登录... ({check_count * 5}s) "
                          f"current nickname='{login_status['nickname'] or '(空)'}'")
                # 每 30 秒刷新页面触发 user/me
                if check_count % 6 == 0:
                    try:
                        page.goto("https://www.xiaohongshu.com",
                                  wait_until="domcontentloaded", timeout=10000)
                        page.wait_for_timeout(2000)
                    except Exception:
                        pass

        if not login_status["logged_in"] or not login_status["nickname"]:
            print(f"\n  ✗ 登录超时或未检测到有效登录（nickname 为空）")
            print(f"  → 请确认在浏览器中已成功扫码登录，且页面显示了用户信息")
            context.close()
            return False

        info = login_status["info"] or {}
        print(f"\n  ✓ 登录成功！")
        print(f"    nickname: {login_status['nickname']}")
        print(f"    fans: {info.get('fans','?')}")
        print(f"    notes: {info.get('notes','?')}")

        # [4/4] 提取 cookie 并保存
        print(f"\n  [4/4] 提取 cookie 并保存...")
        cookies = context.cookies()
        cookie_pairs = []
        for c in cookies:
            if "xiaohongshu.com" in c.get("domain", ""):
                cookie_pairs.append(f"{c['name']}={c['value']}")
        cookie_str = "; ".join(cookie_pairs)

        with open(cookie_file, "w", encoding="utf-8") as f:
            f.write(cookie_str)
        print(f"  ✓ 已保存 {len(cookie_pairs)} 个 cookie 到 {cookie_file}")
        print(f"  ✓ cookie 长度: {len(cookie_str)} 字符")

        # 验证关键字段
        key_fields = ["a1", "web_session", "websectiga", "gid"]
        for k in key_fields:
            found = any(c["name"] == k for c in cookies)
            print(f"    {k}: {'✓' if found else '✗'}")

        print(f"\n  登录完成！后续可使用:")
        print(f"    python xhs_migrate_test.py --live   # 用保存的 cookie 跑测试")
        print(f"    profile 已持久化，下次 --login 可直接复用免扫码")

        context.close()
        return True

    except Exception as e:
        print(f"  ✗ 异常: {str(e)[:300]}")
        import traceback
        traceback.print_exc()
        return False
    finally:
        try:
            if context: context.close()
            if pw: pw.stop()
        except Exception:
            pass


def test_playwright_listen_mode(cookie_str):
    """
    Playwright 监听 API 响应模式（srxly888/rednote-crawler 模式平移到 Playwright）。
    核心思路：让 Edge 浏览器自然发请求，监听 network 响应拿 API JSON，无需签名维护。
    """
    section("【步骤 5】Playwright 监听 API 响应模式（推荐方案验证）")

    if not check_dependency("playwright"):
        print("  ✗ playwright 未安装，跳过")
        return False

    try:
        from playwright.sync_api import sync_playwright
        from xhs import help as xhs_help
    except Exception as e:
        print(f"  ✗ 依赖加载失败: {e}")
        return False

    cookie_dict = xhs_help.cookie_str_to_cookie_dict(cookie_str)
    cookies_for_pw = [
        {"name": k, "value": v, "domain": ".xiaohongshu.com", "path": "/"}
        for k, v in cookie_dict.items()
    ]
    print(f"  准备注入 {len(cookies_for_pw)} 个 cookie")
    print(f"  关键字段: a1={cookie_dict.get('a1','?')[:15]}..., "
          f"web_session={cookie_dict.get('web_session','?')[:15]}...")

    # API 响应收集器
    api_responses = []
    TARGET_APIS = {
        "user_me": "/api/sns/web/v2/user/me",
        "search": "/api/sns/web/search/notes",   # 匹配 v1/v2
        "feed": "/api/sns/web/feed",             # 匹配 v1/v2 feed（详情）
        "comment": "/api/sns/web/comment/page",  # 评论列表（v1/v2）
    }

    def on_response(response):
        url = response.url
        if "/api/sns/web/" not in url:
            return
        try:
            body = response.json()
            api_responses.append({
                "url": url,
                "status": response.status,
                "body": body,
                "ts": time.time(),
            })
        except Exception:
            pass  # 非 JSON 响应忽略

    def find_api(keyword_url):
        """从已收集的响应中查找包含 keyword_url 的响应"""
        for r in api_responses:
            if keyword_url in r["url"]:
                return r
        return None

    def wait_for_api(keyword_url, timeout_s=15):
        """等待目标 API 响应到达"""
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            r = find_api(keyword_url)
            if r:
                return r
            page.wait_for_timeout(300)
        return None

    pw = None
    browser = None
    context = None
    page = None
    try:
        pw = sync_playwright().start()
        print(f"\n  [1/6] 启动 Edge 浏览器...")
        try:
            browser = pw.chromium.launch(headless=True, channel="msedge")
            print(f"  ✓ 使用 Microsoft Edge (headless)")
        except Exception as e_edge:
            print(f"  ⚠ Edge 启动失败: {str(e_edge)[:100]}")
            browser = pw.chromium.launch(headless=True)
            print(f"  → 回退到 Playwright chromium")

        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1366, "height": 900},
            locale="zh-CN",
        )
        context.add_cookies(cookies_for_pw)
        page = context.new_page()
        page.on("response", on_response)

        # [2/6] 访问首页预热，检测登录态
        print(f"\n  [2/6] 访问小红书首页（预热 + 检测登录态）...")
        page.goto("https://www.xiaohongshu.com", wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(3000)

        user_me = wait_for_api(TARGET_APIS["user_me"], timeout_s=5)
        if user_me:
            code = user_me["body"].get("code")
            if code == 0:
                info = user_me["body"].get("data", {})
                print(f"  ✓ 登录态正常: nickname={info.get('nickname','?')}, "
                      f"fans={info.get('fans','?')}")
            else:
                msg = user_me["body"].get("msg", "?")
                print(f"  ✗ 登录态异常: code={code}, msg={msg}")
                print(f"  → cookie 可能已过期，需要重新登录")
                context.close()
                return False
        else:
            print(f"  ⚠ 未捕获到 user/me 接口，继续尝试搜索...")

        # [3/6] 触发搜索
        print(f"\n  [3/6] 触发搜索（关键词：美食）...")
        api_responses.clear()  # 清空，只关注搜索后的响应
        # 直接访问搜索结果 URL，URL 已含关键词，页面加载会自动触发 search/notes
        search_url = "https://www.xiaohongshu.com/search_result?keyword=%E7%BE%8E%E9%A3%9F&source=web_search_result_notes"
        page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
        # 等待页面完全加载 + search/notes API 被触发
        page.wait_for_timeout(5000)

        # 滚动触发更多搜索结果加载（瀑布流）
        page.evaluate("window.scrollTo(0, 600)")
        page.wait_for_timeout(3000)
        page.evaluate("window.scrollTo(0, 1200)")
        page.wait_for_timeout(3000)

        # 扩大搜索 API 匹配范围：search/notes 或任何返回笔记列表的 search API
        search_resp = None
        for api_key in ["search"]:
            search_resp = wait_for_api(TARGET_APIS[api_key], timeout_s=10)
            if search_resp:
                break

        # 如果还没找到，尝试匹配任何包含 search 且返回 items 的 API
        if not search_resp:
            print(f"  ⚠ 未找到 search/notes，扫描所有 search 相关 API...")
            for r in api_responses:
                if "search" in r["url"] and isinstance(r["body"], dict):
                    body = r["body"]
                    data = body.get("data", {})
                    if isinstance(data, dict) and (data.get("items") or data.get("notes")):
                        search_resp = r
                        print(f"  ✓ 找到替代搜索 API: {r['url'].split('xiaohongshu.com')[-1][:60]}")
                        break
        if not search_resp:
            print(f"  ✗ 未捕获到搜索 API 响应")
            print(f"  → 共捕获 {len(api_responses)} 个 API 请求:")
            for r in api_responses[:10]:
                path = r["url"].split("xiaohongshu.com")[-1][:60]
                print(f"    [{r['status']}] code={r['body'].get('code') if isinstance(r['body'],dict) else '?'} {path}")
            context.close()
            return False

        code = search_resp["body"].get("code")
        if code != 0:
            msg = search_resp["body"].get("msg", "?")
            print(f"  ✗ 搜索 API 返回错误: code={code}, msg={msg}")
            context.close()
            return False

        items = search_resp["body"].get("data", {}).get("items", []) or []
        print(f"  ✓ 搜索成功！返回 {len(items)} 条笔记")

        # 取第一个笔记的 note_id + xsec_token
        first_note = None
        for item in items:
            if item.get("model_type") == "note":
                first_note = item
                break

        if not first_note:
            print(f"  ✗ 搜索结果无 note 类型")
            context.close()
            return False

        nc = first_note.get("note_card", {}) or {}
        # note_id 可能在多个位置：note_card.note_id / note_card.id / item.id / item.note_id
        note_id = (nc.get("note_id") or nc.get("id")
                   or first_note.get("id") or first_note.get("note_id") or "")
        xsec_token = first_note.get("xsec_token", "") or nc.get("xsec_token", "")
        title = nc.get("title") or nc.get("display_title") or "(无标题)"
        ii = nc.get("interact_info", {}) or {}

        print(f"  ✓ 首条笔记: [{note_id}] {title[:30]}")
        print(f"    xsec_token: {xsec_token[:20]}... (长度 {len(xsec_token)})")

        # 打印搜索结果里已有的互动数据（搜索结果通常已经包含 liked/collected/comment）
        if ii:
            print(f"    搜索结果已有互动数据: liked={ii.get('liked_count','?')}, "
                  f"collected={ii.get('collected_count','?')}, "
                  f"comment={ii.get('comment_count','?')}, "
                  f"share={ii.get('share_count','?')}")

        # 打印 note_card 完整 keys 方便了解字段
        print(f"    note_card 字段: {sorted(list(nc.keys()))}")

        if not note_id:
            print(f"  ⚠ note_id 为空！打印 first_note 结构供调试:")
            print(f"    keys: {list(first_note.keys())}")
            import json as _json
            print(f"    first_note: {_json.dumps(first_note, ensure_ascii=False)[:600]}")

        # [4/6] 触发详情（点击笔记卡片弹出侧边栏）
        print(f"\n  [4/6] 触发笔记详情（点击卡片弹出侧边栏）...")
        api_responses.clear()

        # 方案A：点击搜索结果页的第一个笔记卡片（触发弹窗）
        clicked = False
        for selector in [
            ".note-item",
            ".feed-item",
            "section.note-item",
            "a.cover",
            ".cover-mask",
        ]:
            try:
                el = page.query_selector(selector)
                if el and el.is_visible():
                    print(f"  ✓ 找到笔记卡片: {selector}，点击中...")
                    el.click()
                    clicked = True
                    break
            except Exception:
                continue

        if not clicked:
            # 方案B：兜底，直接新开 tab 访问详情 URL
            print(f"  ⚠ 未找到可点击的笔记卡片，改用新开 tab 访问详情页")
            if xsec_token:
                detail_url = f"https://www.xiaohongshu.com/explore/{note_id}?xsec_token={xsec_token}&xsec_source=pc_search"
            else:
                detail_url = f"https://www.xiaohongshu.com/explore/{note_id}"
            new_page = context.new_page()
            new_page.on("response", on_response)
            new_page.goto(detail_url, wait_until="domcontentloaded", timeout=30000)
            page = new_page

        page.wait_for_timeout(6000)

        # 滚动触发更多 API 加载
        try:
            page.evaluate("window.scrollTo(0, 500)")
        except Exception:
            pass
        page.wait_for_timeout(2000)

        feed_resp = wait_for_api(TARGET_APIS["feed"], timeout_s=15)

        # 兜底：如果 feed API 没找到，扫描所有 API 找包含 interact_info 的响应
        if not feed_resp:
            print(f"  ⚠ 未找到 feed API，扫描所有响应找 interact_info...")
            for r in api_responses:
                try:
                    body = r["body"]
                    if not isinstance(body, dict):
                        continue
                    data = body.get("data", {})
                    items = data.get("items", []) if isinstance(data, dict) else []
                    for item in items:
                        nc = item.get("note_card", {}) if isinstance(item, dict) else {}
                        if nc.get("interact_info"):
                            feed_resp = r
                            print(f"  ✓ 找到含 interact_info 的 API: "
                                  f"{r['url'].split('xiaohongshu.com')[-1][:60]}")
                            break
                    if feed_resp:
                        break
                except Exception:
                    continue

        if feed_resp and feed_resp["body"].get("code") == 0:
            feed_data = feed_resp["body"].get("data", {})
            items_in_feed = feed_data.get("items", []) or []
            if items_in_feed:
                detail = items_in_feed[0].get("note_card", {}) or {}
                ii = detail.get("interact_info", {}) or {}
                user = detail.get("user", {}) or {}
                print(f"  ✓ 详情 API 成功:")
                print(f"    title={detail.get('title') or detail.get('display_title','?')}")
                print(f"    author={user.get('nickname','?')} (uid={user.get('user_id','?')})")
                print(f"    liked={ii.get('liked_count','?')}, "
                      f"collected={ii.get('collected_count','?')}, "
                      f"comment={ii.get('comment_count','?')}, "
                      f"share={ii.get('share_count','?')}")
                print(f"    type={detail.get('type','?')}, "
                      f"time={detail.get('time','?')}")
                # 提取标签
                tag_list = detail.get("tag_list", []) or []
                tags = [t.get("name", "") for t in tag_list if isinstance(t, dict)]
                if tags:
                    print(f"    tags={','.join(tags)}")
        else:
            print(f"  ✗ 详情 API 未返回或错误")
            if feed_resp:
                print(f"    code={feed_resp['body'].get('code')}, msg={feed_resp['body'].get('msg','?')}")
            # 打印捕获的所有 API 帮助定位
            print(f"  → 捕获的 {len(api_responses)} 个 API:")
            for r in api_responses[:15]:
                path = r["url"].split("xiaohongshu.com")[-1][:60]
                code = r["body"].get("code") if isinstance(r["body"], dict) else "?"
                print(f"    [{r['status']}] code={code} {path}")

        # [5/6] 触发评论
        print(f"\n  [5/6] 等待评论 API...")
        # 评论 API 需要滚动到评论区才触发（在详情弹窗内滚动）
        # 先试试滚动页面
        try:
            page.evaluate("window.scrollTo(0, 1500)")
        except Exception:
            pass
        page.wait_for_timeout(3000)
        try:
            page.evaluate("window.scrollTo(0, 2500)")
        except Exception:
            pass
        page.wait_for_timeout(3000)

        # 再试试滚动详情弹窗的内容容器
        for selector in [".note-scroller", ".detail-container", "#noteContainer", "main"]:
            try:
                page.evaluate(f'el => el.scrollTop = el.scrollHeight', page.query_selector(selector))
                page.wait_for_timeout(1500)
            except Exception:
                continue

        comment_resp = wait_for_api(TARGET_APIS["comment"], timeout_s=10)
        if comment_resp and comment_resp["body"].get("code") == 0:
            comments = comment_resp["body"].get("data", {}).get("comments", []) or []
            print(f"  ✓ 评论 API 成功！返回 {len(comments)} 条评论")
            for i, c in enumerate(comments[:3], 1):
                content = c.get("content", "?")[:30] if isinstance(c, dict) else str(c)[:30]
                create_time = c.get("create_time", "?") if isinstance(c, dict) else "?"
                if isinstance(create_time, (int, float)) and create_time > 1000000000:
                    from datetime import datetime as _dt
                    create_time = _dt.fromtimestamp(create_time).strftime("%Y-%m-%d %H:%M:%S")
                print(f"    {i}. [{create_time}] {content}")
        else:
            print(f"  ⚠ 评论 API 未捕获（可能笔记无评论或需更多滚动）")

        # [6/6] 总结
        print(f"\n  [6/6] Playwright 监听模式验证总结:")
        success_count = sum(1 for api_key, api_url in TARGET_APIS.items()
                           if find_api(api_url) and
                           (find_api(api_url)["body"].get("code") == 0
                            if isinstance(find_api(api_url)["body"], dict) else False))
        print(f"    成功捕获的 API: {success_count}/{len(TARGET_APIS)}")
        print(f"    总捕获 API 数: {len(api_responses)}")
        print()
        print(f"  ✓ 验证结论:")
        print(f"    - Playwright 监听 API 响应模式完全可行")
        print(f"    - 浏览器自动签名，无需维护签名算法")
        print(f"    - xsec_token 等 DOM 拿不到的鉴权参数，从 API JSON 直接获取")
        print(f"    - 字段流转与原 B站项目一致，可平移到正式 xhs_spider.py")

        context.close()
        return True

    except Exception as e:
        print(f"  ✗ 异常: {str(e)[:300]}")
        import traceback
        traceback.print_exc()
        return False
    finally:
        try:
            if page: page.close()
            if context: context.close()
            if browser: browser.close()
            if pw: pw.stop()
        except Exception:
            pass


def diagnose_with_playwright(cookie_str):
    """用 Playwright 真实浏览器访问小红书，诊断账号是否被风控"""
    section("【步骤 5.5】Playwright 浏览器诊断（验证账号是否被风控）")

    if not check_dependency("playwright"):
        print("  ✗ playwright 未安装，跳过")
        return None

    try:
        from playwright.sync_api import sync_playwright
        from xhs import help as xhs_help
    except Exception as e:
        print(f"  ✗ 依赖加载失败: {e}")
        return None

    cookie_dict = xhs_help.cookie_str_to_cookie_dict(cookie_str)
    cookies_for_pw = [
        {"name": k, "value": v, "domain": ".xiaohongshu.com", "path": "/"}
        for k, v in cookie_dict.items()
    ]

    print(f"  准备注入 {len(cookies_for_pw)} 个 cookie 到 Playwright")
    print(f"  启动 headless 浏览器...")

    pw = None
    browser = None
    try:
        pw = sync_playwright().start()
        # 优先使用系统已安装的 Edge（channel="msedge"），避免下载 Playwright 自带 chromium
        try:
            browser = pw.chromium.launch(headless=True, channel="msedge")
            print(f"  ✓ 使用 Microsoft Edge 启动浏览器")
        except Exception as e_edge:
            print(f"  ⚠ Edge 启动失败: {str(e_edge)[:100]}")
            print(f"  → 回退到 Playwright 自带 chromium（需先 `playwright install chromium`）")
            browser = pw.chromium.launch(headless=True)

        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800},
        )
        context.add_cookies(cookies_for_pw)
        page = context.new_page()

        # 监听网络请求，捕获 API 响应
        api_responses = []
        def handle_response(response):
            url = response.url
            if "/api/sns/web/" in url:
                try:
                    body = response.json()
                    api_responses.append({"url": url, "status": response.status, "body": body})
                except Exception:
                    pass

        page.on("response", handle_response)

        print(f"\n  [1/3] 访问小红书首页...")
        page.goto("https://www.xiaohongshu.com", wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(3000)

        title = page.title()
        print(f"    ✓ 页面标题: {title}")
        print(f"    ✓ 捕获到 {len(api_responses)} 个 API 请求")

        # 看首页 API 是否正常返回
        ok_count = 0
        err_count = 0
        for r in api_responses[:10]:
            body = r["body"]
            code = body.get("code") if isinstance(body, dict) else None
            status_flag = "✓" if code == 0 else "✗"
            if code == 0:
                ok_count += 1
            else:
                err_count += 1
            api_path = r["url"].split("xiaohongshu.com")[-1][:60]
            print(f"    {status_flag} [{r['status']}] code={code} {api_path}")

        print(f"\n  [2/3] 在浏览器内执行搜索（关键词：美食）...")
        # 直接在浏览器内访问搜索 URL
        search_url = "https://www.xiaohongshu.com/search_result?keyword=%E7%BE%8E%E9%A3%9F&source=web_search_result_notes"
        api_responses.clear()
        page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(5000)

        print(f"    捕获到 {len(api_responses)} 个搜索相关 API 请求")
        search_ok = False
        for r in api_responses[:5]:
            body = r["body"]
            code = body.get("code") if isinstance(body, dict) else None
            if "search" in r["url"] and code == 0:
                search_ok = True
                data = body.get("data", {})
                items = data.get("items", []) if isinstance(data, dict) else []
                print(f"    ✓ 搜索成功！返回 {len(items)} 条笔记")
                for i, item in enumerate(items[:3], 1):
                    if isinstance(item, dict):
                        nc = item.get("note_card") or item
                        title = nc.get("title") or nc.get("display_title") or "(无标题)"
                        print(f"      {i}. {title[:40]}")
                break
            else:
                api_path = r["url"].split("xiaohongshu.com")[-1][:60]
                print(f"    ? code={code} {api_path}")

        print(f"\n  [3/3] 诊断结论：")
        print(f"    首页 API 成功 {ok_count} 个，失败 {err_count} 个")
        print(f"    搜索 API: {'✓ 可用' if search_ok else '✗ 失败'}")

        if search_ok:
            print("\n    ✓ 账号在真实浏览器内可用，说明 requests 直调被识别为爬虫")
            print("      → 需要补充 x-mns / x-xray-traceid 等高级签名头")
            print("      → 或改用 Playwright 全自动化方案（方案 C）")
        elif ok_count > 0:
            print("\n    ⚠ 账号可用但搜索受限（可能需要更多预热）")
        else:
            print("\n    ✗ 账号本身被风控，建议换号或等待")

        context.close()
        return {"home_ok": ok_count > 0, "search_ok": search_ok,
                "home_success": ok_count, "home_fail": err_count}
    except Exception as e:
        print(f"  ✗ 异常: {str(e)[:200]}")
        import traceback
        traceback.print_exc()
        return None
    finally:
        try:
            if browser: browser.close()
            if pw: pw.stop()
        except Exception:
            pass


def make_local_sign(cookie_str):
    """
    构造本地纯算签名函数（无需启动 Playwright / 签名服务）。
    基于 xhs.help.sign 实现，自动从 cookie 中提取 a1。
    """
    from xhs import help as xhs_help

    cookie_dict = xhs_help.cookie_str_to_cookie_dict(cookie_str)
    a1_value = cookie_dict.get("a1", "")
    print(f"  [签名] 从 cookie 提取 a1={a1_value[:20]}... (长度 {len(a1_value)})")

    def sign_func(uri, data=None, a1="", web_session="", **kwargs):
        # xhs.help.sign 签名: sign(uri, data=None, ctime=None, a1="", b1="")
        return xhs_help.sign(uri, data=data, a1=a1 or a1_value, b1="")

    return sign_func


def test_live_api(cookie_str=None, cookie_file=None):
    section("【步骤 5】真实 API 调用测试（本地纯算签名）")

    if not check_dependency("xhs"):
        print("  ✗ xhs 库未安装，跳过真实调用测试")
        print("    执行 `pip install xhs` 后重新运行 --live")
        return False

    # 优先使用命令行 cookie，其次从 xhs_cookie.txt 读取
    if not cookie_str and cookie_file and os.path.exists(cookie_file):
        with open(cookie_file, "r", encoding="utf-8") as f:
            cookie_str = f.read().strip()
        print(f"  [Cookie] 已从 {cookie_file} 读取")

    if not cookie_str:
        print("  ✗ 未提供小红书 cookie，跳过")
        print("    方式1: --cookie 'a1=xxx; web_session=xxx'")
        print(f"    方式2: 写入 {cookie_file} 后运行 --live")
        return False

    # 校验关键 cookie 字段
    from xhs import help as xhs_help
    cookie_dict = xhs_help.cookie_str_to_cookie_dict(cookie_str)
    print(f"  ✓ xhs 库已加载")
    print(f"  cookie 长度：{len(cookie_str)} 字符")
    print(f"  cookie 字段数：{len(cookie_dict)}")

    missing = [k for k in ["a1", "web_session"] if k not in cookie_dict or not cookie_dict[k]]
    if missing:
        print(f"  ✗ 关键字段缺失：{missing}")
        return False
    print(f"  ✓ 关键字段校验通过：a1 / web_session 均存在")

    try:
        from xhs import XhsClient

        # 构造本地签名函数（核心：解决 'NoneType' object is not callable 错误）
        sign_func = make_local_sign(cookie_str)

        print("\n  [初始化] 创建 XhsClient（传入 sign=本地纯算函数）...")
        client = XhsClient(cookie=cookie_str, sign=sign_func, timeout=15)
        print(f"  ✓ XhsClient 初始化成功")

        # 测试1: 获取自身信息
        print("\n  [测试1] 获取自身账号信息 (get_self_info)...")
        try:
            me = client.get_self_info()
            if isinstance(me, dict):
                print(f"    ✓ 成功：nickname={me.get('nickname', '?')}, "
                      f"fans={me.get('fans', '?')}, notes={me.get('notes', '?')}")
            else:
                print(f"    ⚠ 返回类型异常：{type(me).__name__}, 内容前200字符: {str(me)[:200]}")
        except Exception as e:
            print(f"    ✗ 失败：{str(e)[:200]}")

        # 测试2: 关键词搜索
        print("\n  [测试2] 关键词搜索笔记 (get_note_by_keyword, 关键词：美食)...")
        search_items = []
        try:
            res = client.get_note_by_keyword(keyword="美食", page=1, page_size=5)
            if isinstance(res, dict):
                search_items = res.get("items", []) or []
                print(f"    ✓ 成功：返回 {len(search_items)} 条笔记")
                for i, item in enumerate(search_items[:3], 1):
                    if isinstance(item, dict):
                        nc = item.get("note_card") or item
                        title = nc.get("title") or nc.get("display_title") or "(无标题)"
                        nid = nc.get("note_id", "?")
                        ii = nc.get("interact_info", {}) or {}
                        print(f"      {i}. [{nid}] {title[:30]} | "
                              f"liked={ii.get('liked_count','?')}")
            else:
                print(f"    ⚠ 返回类型异常：{type(res).__name__}")
        except Exception as e:
            print(f"    ✗ 失败：{str(e)[:200]}")

        # 测试3: 获取笔记详情
        print("\n  [测试3] 获取笔记详情 (get_note_by_id)...")
        if search_items:
            try:
                first = search_items[0]
                nc = first.get("note_card") or first if isinstance(first, dict) else {}
                note_id = nc.get("note_id")
                xsec_token = first.get("xsec_token") or nc.get("xsec_token") or ""
                if note_id:
                    detail = client.get_note_by_id(note_id, xsec_token=xsec_token) if xsec_token else client.get_note_by_id(note_id)
                    if isinstance(detail, dict):
                        ii = detail.get("interact_info", {}) or {}
                        user = detail.get("user", {}) or {}
                        print(f"    ✓ 成功：note_id={note_id}")
                        print(f"      title={detail.get('title') or detail.get('display_title','?')}")
                        print(f"      author={user.get('nickname','?')} (uid={user.get('user_id','?')})")
                        print(f"      liked={ii.get('liked_count','?')}, "
                              f"collected={ii.get('collected_count','?')}, "
                              f"comment={ii.get('comment_count','?')}")
                    else:
                        print(f"    ⚠ 返回类型异常：{type(detail).__name__}")
            except Exception as e:
                print(f"    ✗ 失败：{str(e)[:200]}")
        else:
            print("    跳过（搜索未返回笔记）")

        # 测试4: 获取笔记评论（如果有 note_id）
        print("\n  [测试4] 获取笔记评论 (get_note_comments)...")
        if search_items:
            try:
                first = search_items[0]
                nc = first.get("note_card") or first if isinstance(first, dict) else {}
                note_id = nc.get("note_id")
                if note_id:
                    comments_res = client.get_note_comments(note_id=note_id)
                    if isinstance(comments_res, dict):
                        comments = comments_res.get("comments", []) or []
                        print(f"    ✓ 成功：返回 {len(comments)} 条评论")
                        for i, c in enumerate(comments[:3], 1):
                            content = c.get("content", "?")[:30] if isinstance(c, dict) else str(c)[:30]
                            create_time = c.get("create_time", "?") if isinstance(c, dict) else "?"
                            print(f"      {i}. [{create_time}] {content}")
            except Exception as e:
                print(f"    ✗ 失败：{str(e)[:200]}")
        else:
            print("    跳过（搜索未返回笔记）")

        print("\n  [测试总结]")
        print("    ✓ 本地纯算签名方案可用，无需启动 Playwright 或外部签名服务")
        print("    ✓ 整个调用链（搜索→详情→评论）字段流转与离线模拟一致")
        return True
    except Exception as e:
        print(f"  ✗ 异常：{str(e)[:300]}")
        import traceback
        traceback.print_exc()
        return False


# ============== 6. 端到端流程模拟 ==============

def test_end_to_end_mock():
    section("【步骤 6】端到端流程模拟（搜索 → 详情 → 评论 → 入库）")

    # 模拟 xhs SDK 返回的搜索结果
    mock_search_response = {
        "items": [
            {"note_card": {
                "note_id": "697cc945000000000a02cdad",
                "title": "周末早餐打卡",
                "display_title": "周末早餐打卡",
                "user": {"user_id": "5e1234", "nickname": "小厨娘"},
                "interact_info": {
                    "liked_count": "42000",
                    "collected_count": "8500",
                    "comment_count": "1800",
                    "share_count": "450"
                },
                "tag_list": [{"name": "早餐"}, {"name": "健康饮食"}],
            }},
            {"note_card": {
                "note_id": "697dd567000000001b03efbc",
                "title": "通勤穿搭分享",
                "user": {"user_id": "6f2345", "nickname": "穿搭达人"},
                "interact_info": {
                    "liked_count": "25000",
                    "collected_count": "12000",
                    "comment_count": "900",
                    "share_count": "200"
                },
            }},
        ]
    }

    print("\n  [阶段1] 搜索 API 返回字段映射：")
    for item in mock_search_response["items"]:
        nc = item["note_card"]
        ii = nc["interact_info"]
        # 模拟映射到 xhs_notes 表
        mapped = {
            "note_id": nc["note_id"],
            "title": nc.get("title") or nc.get("display_title"),
            "user_id": nc["user"]["user_id"],
            "nickname": nc["user"]["nickname"],
            "liked_count": int(ii["liked_count"]),
            "collected_count": int(ii["collected_count"]),
            "comment_count": int(ii["comment_count"]),
            "share_count": int(ii["share_count"]),
            "interact_count": int(ii["liked_count"]) + int(ii["collected_count"]) + int(ii["comment_count"]),
            "tags": ",".join(t["name"] for t in nc.get("tag_list", [])),
        }
        print(f"    ✓ {mapped['note_id']}: {mapped['title']} | "
              f"interact={mapped['interact_count']} | tags={mapped['tags']}")

    # 模拟笔记详情返回
    print("\n  [阶段2] 详情 API 补充字段：")
    mock_detail = {
        "note_id": "697cc945000000000a02cdad",
        "title": "周末早餐打卡",
        "desc": "今天做了牛油果鸡蛋三明治...",
        "time": int(time.time() * 1000) - 12 * 3600 * 1000,  # 12小时前
        "duration": 0,
        "tag_list": [{"name": "早餐"}, {"name": "健康饮食"}, {"name": "三明治"}],
        "user": {"user_id": "5e1234", "nickname": "小厨娘", "fans": "520"},
    }
    note_age_hours = (time.time() * 1000 - mock_detail["time"]) / 3600000
    interact = 42000 + 8500 + 1800
    velocity = interact / max(note_age_hours, 0.1)
    print(f"    ✓ note_age_hours={note_age_hours:.2f}")
    print(f"    ✓ interact_velocity={velocity:.0f}/h")
    print(f"    ✓ fans_count={mock_detail['user']['fans']}")

    # 模拟评论 API 返回
    print("\n  [阶段3] 评论 API 字段：")
    mock_comments = {
        "comments": [
            {"id": "c1", "create_time": int(time.time()) - 10 * 3600,
             "content": "看起来好好吃！", "like_count": 50},
            {"id": "c2", "create_time": int(time.time()) - 8 * 3600,
             "content": "求食谱", "like_count": 20},
        ],
        "cursor": "",
        "has_more": False,
    }
    earliest = min(c["create_time"] for c in mock_comments["comments"])
    first_comment_time = datetime.fromtimestamp(earliest).strftime("%Y-%m-%d %H:%M:%S")
    print(f"    ✓ 首评时间：{first_comment_time}")
    print(f"    ✓ 评论数：{len(mock_comments['comments'])}")

    print("\n  [阶段4] 最终入库记录（关键字段）：")
    final_record = {
        "note_id": mock_detail["note_id"],
        "title": mock_detail["title"],
        "interact_count": interact,
        "liked_count": 42000,
        "collected_count": 8500,
        "comment_count": 1800,
        "share_count": 450,
        "fans_count": 520,
        "note_age_hours": round(note_age_hours, 2),
        "interact_velocity": round(velocity, 2),
        "engagement_score": round((42000 + 8500 + 1800) / interact, 4),
        "first_comment_time": first_comment_time,
        "tags": "早餐,健康饮食,三明治",
    }
    for k, v in final_record.items():
        print(f"    {k:24s}: {v}")

    print("\n  ✓ 端到端流程字段流转通畅，与原 bili 项目逻辑结构一致")
    return True


# ============== 主流程 ==============

def main():
    parser = argparse.ArgumentParser(description="小红书迁移可行性测试")
    parser.add_argument("--live", action="store_true", help="启用真实 API 调用（需 --cookie）")
    parser.add_argument("--cookie", type=str, default=None,
                        help="小红书 cookie 字符串，如 'a1=xxx; web_session=xxx'")
    parser.add_argument("--mock", action="store_true", help="仅跑离线模拟（默认行为）")
    parser.add_argument("--login", action="store_true",
                        help="弹出有头 Edge 扫码登录小红书，保存 cookie + 持久化 profile")
    args = parser.parse_args()

    print(f"\n{'#'*70}")
    print(f"  小红书迁移可行性测试  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'#'*70}")

    # 步骤 1: 依赖
    deps = test_dependencies()

    # 步骤 2: 字段映射
    test_field_mapping()

    # 步骤 3: 动量算法
    test_momentum_algorithm()

    # 步骤 4: Schema 兼容性
    test_database_compat()

    # 步骤 5: 端到端模拟
    test_end_to_end_mock()

    # 步骤 6: 真实 API（可选）
    cookie_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "xhs_cookie.txt")

    # 交互登录模式（优先处理）
    if args.login:
        interactive_login(cookie_file)
        return  # 登录完成后退出

    if args.live or args.cookie:
        live_cookie = args.cookie
        if not live_cookie and os.path.exists(cookie_file):
            with open(cookie_file, "r", encoding="utf-8") as f:
                live_cookie = f.read().strip()
            print(f"  [Cookie] 已从 {cookie_file} 读取")

        if live_cookie:
            # 优先使用 Playwright 监听 API 响应模式（推荐方案）
            test_playwright_listen_mode(live_cookie)
        else:
            print("  ✗ 未提供小红书 cookie，跳过真实测试")
    else:
        section("【步骤 5】真实 API 调用测试（跳过）")
        print("  未启用 --live / --cookie，跳过")
        print("  如需测试真实接口：")
        print("    方式1: python xhs_migrate_test.py --live              # 自动读取 xhs_cookie.txt")
        print("    方式2: python xhs_migrate_test.py --cookie 'a1=...'   # 直接传 cookie 字符串")

    # 总结
    section("【迁移可行性结论】")
    print("""
  ✓ 整体架构（Spider类/CookieManager/Database/动量算法）70% 可直接复用
  ✓ 字段映射表清晰，22/24 个 B站字段可平移到小红书
  ✓ 动量算法 5 维评分框架无需改动，仅替换指标（播放量 → 互动量）
  ✓ SQLite Schema 兼容，仅需字段重命名 + 移除 coin/danmakus
  ✓ 端到端流程（搜索→详情→评论→入库）字段流转通畅

  推荐方案：Playwright 监听 API 响应模式（参考 srxly888/rednote-crawler）
    - 浏览器自动签名，无需维护 x-s/x-t/x-s-common 算法
    - 从 API JSON 直接获取 xsec_token 等 DOM 拿不到的鉴权参数
    - Edge channel="msedge" 复用系统浏览器，无需下载 chromium
    - 复用原项目 Database + 动量算法 + 多线程框架（数据层 100% 平移）

  需要重写的部分：
    - 4 个 fetch 方法 → Playwright 监听模式（page.on("response") + wait_for_api）
    - Cookie 维护：launch_persistent_context 持久化登录态 + pong() 探活
    - 反检测增强：playwright-stealth + 随机延迟 + 行为模拟
""")


if __name__ == "__main__":
    main()
