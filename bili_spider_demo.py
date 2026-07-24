import requests
import csv
import time
import random
import warnings
import os
from datetime import datetime
from urllib.parse import urlencode

requests.packages.urllib3.disable_warnings()
warnings.filterwarnings("ignore")

KEYWORD = "服饰"
MAX_PAGES = 5
ORDER = "click"
OUTPUT_FILE = "bili_demo.csv"

COOKIE_FILE = "bili_cookie.txt"
BILI_SESSDATA = ""

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
]


def load_cookie():
    global BILI_SESSDATA
    if BILI_SESSDATA:
        return BILI_SESSDATA

    if os.path.exists(COOKIE_FILE):
        with open(COOKIE_FILE, "r", encoding="utf-8") as f:
            cookie = f.read().strip()
            if cookie:
                BILI_SESSDATA = cookie
                return BILI_SESSDATA

    return ""


def get_headers(referer="https://www.bilibili.com/"):
    headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "Referer": referer,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }

    sessdata = load_cookie()
    if sessdata:
        headers["Cookie"] = f"SESSDATA={sessdata}"

    return headers


def search_bilibili(keyword, page, order="click", max_retries=3):
    params = {
        "search_type": "video",
        "keyword": keyword,
        "page": page,
        "order": order,
        "platform": "pc",
    }
    url = f"https://api.bilibili.com/x/web-interface/search/type?{urlencode(params)}"

    for attempt in range(max_retries):
        headers = get_headers("https://search.bilibili.com/")
        try:
            resp = requests.get(url, headers=headers, timeout=15, verify=False)
            
            if resp.status_code == 412:
                wait = (attempt + 1) * 5
                print(f"  [限流] 请求被限制，等待 {wait} 秒后重试 ({attempt+1}/{max_retries})...")
                time.sleep(wait)
                continue
            
            if resp.status_code != 200:
                print(f"  [错误] HTTP {resp.status_code}，重试 ({attempt+1}/{max_retries})...")
                time.sleep(random.uniform(3, 6))
                continue

            data = resp.json()

            if data["code"] != 0:
                msg = data.get("message", "未知错误")
                if "请输入" in msg or "登录" in msg:
                    print(f"  [错误] 搜索需要登录，请在 {COOKIE_FILE} 中配置 SESSDATA")
                    return None
                elif msg == "搜索请求超时":
                    print(f"  [超时] 搜索请求超时，重试 ({attempt+1}/{max_retries})...")
                    time.sleep(random.uniform(3, 6))
                    continue
                else:
                    print(f"  [错误] 搜索 API 返回错误: {msg}")
                    return None

            return data.get("data", {})
            
        except requests.RequestException as e:
            if attempt < max_retries - 1:
                print(f"  [错误] 请求失败，重试 ({attempt+1}/{max_retries})...")
                time.sleep(random.uniform(3, 6))
            else:
                print(f"  [错误] 搜索请求失败: {e}")
                return None
        except Exception as e:
            print(f"  [错误] 搜索解析异常: {e}")
            return None

    print(f"  [错误] 已达最大重试次数")
    return None


def get_video_detail(av_id):
    url = f"https://api.bilibili.com/x/web-interface/view?aid={av_id}"
    headers = get_headers()

    try:
        resp = requests.get(url, headers=headers, timeout=10, verify=False)
        data = resp.json()

        if data["code"] != 0:
            msg = data.get("message", "未知错误")
            if data["code"] == -404:
                print(f"  [跳过] av{av_id} 视频不存在")
            elif "频率" in msg or "风控" in msg:
                print(f"  [警告] av{av_id} 触发风控，增加延迟后重试")
                time.sleep(random.uniform(5, 10))
                return None
            else:
                print(f"  [警告] av{av_id} 详情获取失败: {msg}")
            return None

        video_data = data["data"]
        stat = video_data["stat"]

        return {
            "title": video_data.get("title", ""),
            "address": f"http://www.bilibili.com/video/av{av_id}",
            "play_nums": stat.get("view", 0),
            "danmakus": stat.get("danmaku", 0),
            "review": stat.get("reply", 0),
            "favorites": stat.get("favorite", 0),
        }
    except requests.RequestException as e:
        print(f"  [错误] av{av_id} 请求异常: {e}")
        return None
    except Exception as e:
        print(f"  [错误] av{av_id} 解析异常: {e}")
        return None


def check_cookie():
    sessdata = load_cookie()
    if not sessdata:
        print("[!] 未检测到 Cookie，未登录状态可能导致搜索 API 不稳定")
        print(f"    如需配置，请将 SESSDATA 保存到 {COOKIE_FILE}")
        print("    获取方法：登录 bilibili.com → F12 → Application → Cookies → 复制 SESSDATA 值")
        return False

    url = "https://api.bilibili.com/x/web-interface/nav"
    headers = get_headers()
    try:
        resp = requests.get(url, headers=headers, timeout=10, verify=False)
        data = resp.json()
        if data["code"] == 0:
            uname = data.get("data", {}).get("uname", "未知")
            print(f"[✓] Cookie 有效，当前账号: {uname}")
            return True
        else:
            print(f"[✗] Cookie 已失效，请重新获取")
            return False
    except Exception:
        print("[!] Cookie 验证失败，继续尝试爬取...")
        return False


def main():
    print("=" * 50)
    print("  B站视频爬虫 Demo (增强版)")
    print("=" * 50)
    print(f"关键词: {KEYWORD}")
    print(f"最大页数: {MAX_PAGES}")
    print(f"排序方式: {ORDER}")
    print(f"输出文件: {OUTPUT_FILE}")
    print()

    check_cookie()
    print()

    all_av_ids = set()
    failed_pages = []

    for page in range(1, MAX_PAGES + 1):
        print(f"[*] 正在爬取第 {page}/{MAX_PAGES} 页...")

        data = search_bilibili(KEYWORD, page, ORDER)
        if not data:
            failed_pages.append(page)
            print(f"  第 {page} 页获取失败，跳过")
            time.sleep(random.uniform(2, 4))
            continue

        results = data.get("result", [])
        if not results:
            print(f"  第 {page} 页无结果，可能已到达末页")
            break

        new_count = 0
        for item in results:
            av_id = item.get("aid", "")
            if av_id and av_id not in all_av_ids:
                all_av_ids.add(av_id)
                new_count += 1

        print(f"  找到 {len(results)} 个视频，新增 {new_count} 个")

        time.sleep(random.uniform(2, 4))

    if failed_pages:
        print(f"\n[!] 以下页面爬取失败: {failed_pages}")
        print(f"    建议配置 Cookie 或增加延迟")

    print(f"\n[*] 共收集到 {len(all_av_ids)} 个视频 ID")
    print("[*] 开始获取视频详情...")

    results = []
    av_list = list(all_av_ids)
    failed_details = []

    for i, av_id in enumerate(av_list, 1):
        print(f"  [{i}/{len(av_list)}] 获取 av{av_id} 详情...", end="")

        detail = get_video_detail(av_id)
        if detail:
            results.append(detail)
            print(" ✓")
        else:
            failed_details.append(av_id)
            print(" ✗")

        time.sleep(random.uniform(1, 2))

    print(f"\n[*] 成功获取 {len(results)}/{len(av_list)} 个视频详情")
    if failed_details:
        print(f"[!] 失败的视频 ID: {failed_details[:10]}{'...' if len(failed_details) > 10 else ''}")

    if results:
        results.sort(key=lambda x: x["play_nums"], reverse=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"bili_{KEYWORD}_{timestamp}.csv"

        try:
            with open(filename, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.DictWriter(f, fieldnames=["address", "danmakus", "favorites", "play_nums", "review", "title"])
                writer.writeheader()
                writer.writerows(results)
            print(f"[*] 数据已保存到 {filename}")
        except PermissionError:
            alt_filename = f"bili_{KEYWORD}_backup.csv"
            with open(alt_filename, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.DictWriter(f, fieldnames=["address", "danmakus", "favorites", "play_nums", "review", "title"])
                writer.writeheader()
                writer.writerows(results)
            print(f"[!] 原文件被占用，数据已保存到 {alt_filename}")
        print("\n" + "=" * 50)
        print("  前 5 条数据预览 (按播放量排序)")
        print("=" * 50)
        for i, r in enumerate(results[:5], 1):
            title = r['title'][:50]
            print(f"\n{i}. {title}")
            print(f"   链接: {r['address']}")
            print(f"   播放: {r['play_nums']:,} | 弹幕: {r['danmakus']:,} | 评论: {r['review']:,} | 收藏: {r['favorites']:,}")
    else:
        print("[!] 无数据可保存")


if __name__ == "__main__":
    main()