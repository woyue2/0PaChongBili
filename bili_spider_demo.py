import requests
import csv
import time
import random
import warnings
from urllib.parse import urlencode

requests.packages.urllib3.disable_warnings()
warnings.filterwarnings("ignore")

KEYWORD = "服饰"
MAX_PAGES = 3
ORDER = "click"
OUTPUT_FILE = "bili_demo.csv"

HEADERS_POOL = [
    {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://www.bilibili.com/",
    },
    {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
        "Referer": "https://search.bilibili.com/",
    },
]


def search_bilibili(keyword, page, order="click"):
    params = {
        "search_type": "video",
        "keyword": keyword,
        "page": page,
        "order": order,
        "platform": "pc",
    }
    url = f"https://api.bilibili.com/x/web-interface/search/type?{urlencode(params)}"

    headers = random.choice(HEADERS_POOL)
    try:
        resp = requests.get(url, headers=headers, timeout=10, verify=False)
        data = resp.json()

        if data["code"] != 0:
            print(f"  [错误] 搜索 API 返回错误: {data.get('message', '未知错误')}")
            return None

        return data.get("data", {})
    except requests.RequestException as e:
        print(f"  [错误] 搜索请求失败: {e}")
        return None
    except Exception as e:
        print(f"  [错误] 搜索解析异常: {e}")
        return None


def get_video_detail(av_id):
    url = f"https://api.bilibili.com/x/web-interface/view?aid={av_id}"
    headers = random.choice(HEADERS_POOL)

    try:
        resp = requests.get(url, headers=headers, timeout=10, verify=False)
        data = resp.json()

        if data["code"] != 0:
            print(f"  [警告] av{av_id} 详情获取失败: {data.get('message', '未知错误')}")
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


def main():
    print(f"=== B站爬虫 Demo ===")
    print(f"关键词: {KEYWORD}")
    print(f"最大页数: {MAX_PAGES}")
    print(f"排序方式: {ORDER} (click=播放量, pubdate=时间, dm=弹幕, stow=收藏)")
    print()

    all_av_ids = set()

    for page in range(1, MAX_PAGES + 1):
        print(f"[*] 正在爬取第 {page}/{MAX_PAGES} 页...")

        data = search_bilibili(KEYWORD, page, ORDER)
        if not data:
            print(f"  第 {page} 页获取失败，跳过")
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

        time.sleep(random.uniform(1, 3))

    print(f"\n[*] 共收集到 {len(all_av_ids)} 个视频 ID")
    print("[*] 开始获取视频详情...")

    results = []
    av_list = list(all_av_ids)

    for i, av_id in enumerate(av_list, 1):
        print(f"  [{i}/{len(av_list)}] 获取 av{av_id} 详情...")

        detail = get_video_detail(av_id)
        if detail:
            results.append(detail)

        time.sleep(random.uniform(0.5, 1.5))

    print(f"\n[*] 成功获取 {len(results)} 个视频详情")

    if results:
        results.sort(key=lambda x: x["play_nums"], reverse=True)

        with open(OUTPUT_FILE, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=["address", "danmakus", "favorites", "play_nums", "review", "title"])
            writer.writeheader()
            writer.writerows(results)

        print(f"[*] 数据已保存到 {OUTPUT_FILE}")
        print("\n前 5 条数据预览:")
        print("-" * 80)
        for i, r in enumerate(results[:5], 1):
            title = r['title'][:50]
            print(f"{i}. {title}...")
            print(f"   播放: {r['play_nums']:,} | 弹幕: {r['danmakus']:,} | 评论: {r['review']:,} | 收藏: {r['favorites']:,}")
            print()
    else:
        print("[!] 无数据可保存")


if __name__ == "__main__":
    main()