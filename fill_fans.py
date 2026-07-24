"""
B站粉丝数补充脚本 - 实用版本
支持通过以下方式获取粉丝数:
1. bili-api-python 库 (如果安装了 aiohttp/httpx)
2. 手动导入 CSV 文件
3. 使用 B 站公开接口配合 WBI 签名

使用方式:
  python fill_fans.py                           # 补充所有缺失的粉丝数
  python fill_fans.py --keyword 服饰             # 仅补充特定关键词
  python fill_fans.py --uid 12345               # 单个 UID
  python fill_fans.py --csv fans.csv             # 从 CSV 批量导入
"""

import sqlite3
import argparse
import sys
import os
import time
import warnings
import json
import hashlib
import urllib.parse
import functools
import requests

warnings.filterwarnings("ignore")

DB_FILE = "bili_spider.db"

MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]


def get_mixin_key(orig: str) -> str:
    return functools.reduce(lambda s, i: s + orig[i], MIXIN_KEY_ENC_TAB, '')[:32]


def enc_wbi(params: dict, img_key: str, wbi_key: str) -> dict:
    mixin_key = get_mixin_key(img_key + wbi_key)
    curr_time = round(time.time())
    params['wts'] = curr_time
    params = dict(sorted(params.items()))
    params = {k: ''.join(filter(lambda c: c not in "!'()*", str(v))) for k, v in params.items()}
    query = urllib.parse.urlencode(params)
    wbi_sign = hashlib.md5((query + mixin_key).encode()).hexdigest()
    params['w_rid'] = wbi_sign
    return params


def get_wbi_keys(session: requests.Session, headers: dict):
    url = "https://api.bilibili.com/x/web-interface/wbi/index/nav"
    try:
        resp = session.get(url, headers=headers, timeout=10, verify=False)
        data = resp.json()
        if data.get("code") == 0:
            img_key = data["data"]["wbi_img"]["img_url"].rsplit("/", 1)[1].split(".")[0]
            sub_key = data["data"]["wbi_img"]["sub_url"].rsplit("/", 1)[1].split(".")[0]
            return img_key, sub_key
    except Exception as e:
        print(f"  [WBI] 获取密钥失败: {e}")
    return None, None


def get_user_info_wbi(uid: str, sessdata: str = None):
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Referer': 'https://space.bilibili.com/',
    }
    if sessdata:
        headers['Cookie'] = f'SESSDATA={sessdata}'
    
    session = requests.Session()
    img_key, sub_key = get_wbi_keys(session, headers)
    if not img_key or not sub_key:
        return None, None
    
    params = {
        "mid": int(uid),
        "photo": "false",
        "platform": "web",
        "web_location": "1550101",
    }
    
    try:
        signed_params = enc_wbi(params, img_key, sub_key)
        url = "https://api.bilibili.com/x/wbi/space/acc/info?" + urllib.parse.urlencode(signed_params)
        resp = session.get(url, headers=headers, timeout=10, verify=False)
        data = resp.json()
        if data.get("code") == 0:
            info = data.get("data", {})
            fans = info.get("fans", 0)
            name = info.get("name", "")
            return fans, name
        else:
            print(f"  [WBI] API错误: code={data.get('code')}, msg={data.get('message')}")
    except Exception as e:
        print(f"  [WBI] 请求失败: {e}")
    
    return None, None


def get_user_info_simple(uid: str, sessdata: str = None):
    """尝试使用不带 WBI 的简单接口"""
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Referer': 'https://www.bilibili.com/',
    }
    if sessdata:
        headers['Cookie'] = f'SESSDATA={sessdata}'
    
    urls = [
        f"https://api.bilibili.com/x/space/acc/info?mid={uid}",
        f"https://api.bilibili.com/x/relation/stat?up_mid={uid}",
    ]
    
    for url in urls:
        try:
            resp = requests.get(url, headers=headers, timeout=10, verify=False)
            data = resp.json()
            if data.get("code") == 0:
                if "data" in data:
                    d = data["data"]
                    if "fans" in d:
                        return d["fans"], d.get("name", "")
                    if "follower" in d:
                        return d["follower"], None
                return 0, None
        except:
            pass
        time.sleep(0.3)
    
    return None, None


def get_missing_fans_uids(conn, keyword=None):
    cursor = conn.cursor()
    
    if keyword:
        cursor.execute("""
            SELECT DISTINCT v.uploader_uid 
            FROM bili_videos v
            JOIN spider_tasks t ON v.task_id = t.id
            WHERE t.keyword = ? 
              AND v.uploader_uid IS NOT NULL 
              AND v.uploader_uid != ''
              AND (v.uploader_fans IS NULL OR v.uploader_fans = 0)
            ORDER BY v.uploader_uid
        """, (keyword,))
    else:
        cursor.execute("""
            SELECT DISTINCT uploader_uid 
            FROM bili_videos
            WHERE uploader_uid IS NOT NULL 
              AND uploader_uid != ''
              AND (uploader_fans IS NULL OR uploader_fans = 0)
            ORDER BY uploader_uid
        """)
    
    rows = cursor.fetchall()
    return [row[0] for row in rows]


def get_uploader_name(conn, uid):
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM bili_uploaders WHERE uid = ?", (uid,))
    row = cursor.fetchone()
    if row and row[0]:
        return row[0]
    
    cursor.execute("""
        SELECT uploader FROM bili_videos 
        WHERE uploader_uid = ? 
        LIMIT 1
    """, (uid,))
    row = cursor.fetchone()
    return row[0] if row else None


def update_fans(conn, uid, fans, name=None):
    cursor = conn.cursor()
    now = __import__("datetime").datetime.now()
    timestamp = now.strftime("%Y-%m-%d %H:%M:%S.") + f"{now.microsecond:06d}"[:3]
    
    if name:
        cursor.execute("""
            INSERT INTO bili_uploaders (uid, name, fans, fetched_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(uid) DO UPDATE SET 
                name = excluded.name,
                fans = excluded.fans,
                fetched_at = excluded.fetched_at
        """, (uid, name, fans, timestamp))
    else:
        cursor.execute("""
            INSERT INTO bili_uploaders (uid, fans, fetched_at)
            VALUES (?, ?, ?)
            ON CONFLICT(uid) DO UPDATE SET 
                fans = excluded.fans,
                fetched_at = excluded.fetched_at
        """, (uid, fans, timestamp))
    
    cursor.execute("""
        UPDATE bili_videos 
        SET uploader_fans = ?,
            uploader = COALESCE(?, uploader)
        WHERE uploader_uid = ?
    """, (fans, name, uid))
    
    conn.commit()


def load_cookie_from_file():
    if os.path.exists("bili_cookie.txt"):
        with open("bili_cookie.txt", "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    return line
    return None


def import_from_csv(conn, csv_file):
    import csv
    
    if not os.path.exists(csv_file):
        print(f"[错误] CSV 文件不存在: {csv_file}")
        return
    
    count = 0
    with open(csv_file, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            uid = row.get("uid", "").strip()
            fans = int(row.get("fans", 0))
            name = row.get("name", "").strip() or None
            
            if uid and fans > 0:
                update_fans(conn, uid, fans, name)
                count += 1
                print(f"  导入: UID={uid}, 粉丝={fans}, 名称={name or '-'}")
    
    print(f"\n[完成] 从 CSV 导入了 {count} 条粉丝数据")


def main():
    parser = argparse.ArgumentParser(
        description="补充B站作者粉丝数",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python fill_fans.py                           # 补充所有缺失的粉丝数
  python fill_fans.py --keyword 服饰             # 仅补充"服饰"相关作者
  python fill_fans.py --uid 123456789            # 单个 UID
  python fill_fans.py --csv fans.csv             # 从 CSV 导入
  python fill_fans.py --method wbi              # 使用 WBI 签名方式
  python fill_fans.py --limit 10                # 限制数量
        """
    )
    parser.add_argument("--keyword", "-k", type=str, default=None,
                        help="仅处理特定关键词的视频作者")
    parser.add_argument("--uid", "-u", type=str, default=None,
                        help="单独处理某个 UID")
    parser.add_argument("--csv", type=str, default=None,
                        help="从 CSV 文件批量导入")
    parser.add_argument("--method", "-m", type=str, default="auto",
                        choices=["auto", "wbi", "simple", "bilibili-api"],
                        help="获取方式 (默认: auto)")
    parser.add_argument("--limit", "-l", type=int, default=None,
                        help="限制处理数量")
    parser.add_argument("--cookie", type=str, default=None,
                        help="Cookie 字符串")
    
    args = parser.parse_args()
    
    if not os.path.exists(DB_FILE):
        print(f"[错误] 数据库文件不存在: {DB_FILE}")
        return
    
    conn = sqlite3.connect(DB_FILE)
    cookie = args.cookie or load_cookie_from_file()
    
    if args.csv:
        print(f"[导入] 从 {args.csv} 导入粉丝数据...")
        import_from_csv(conn, args.csv)
        conn.close()
        return
    
    if args.uid:
        uids = [args.uid]
    else:
        print(f"[查询] 获取需要补充粉丝数的 UID 列表...")
        uids = get_missing_fans_uids(conn, args.keyword)
        print(f"[查询] 共 {len(uids)} 个 UID 需要补充粉丝数")
    
    if not uids:
        print("[完成] 所有 UID 都已有粉丝数据")
        conn.close()
        return
    
    if args.limit:
        uids = uids[:args.limit]
        print(f"[限制] 仅处理前 {args.limit} 个 UID")
    
    success_count = 0
    fail_count = 0
    
    for i, uid in enumerate(uids, 1):
        print(f"\n[{i}/{len(uids)}] 处理 UID: {uid}")
        
        existing_name = get_uploader_name(conn, uid)
        
        fans = None
        name = existing_name
        
        if args.method in ("auto", "bilibili-api"):
            try:
                from bilibili_api import user, sync
                print(f"  尝试 bilibili-api-python...")
                
                class CustomSession:
                    def __init__(self, sessdata):
                        self.sessdata = sessdata
                    def get_cookies(self):
                        return [{"name": "SESSDATA", "value": self.sessdata, "domain": ".bilibili.com"}]
                
                if cookie:
                    sess = CustomSession(cookie)
                    import bilibili_api
                    bilibili_api.request.sessions.DEFAULT = sess
                
                u = user.User(uid=int(uid))
                info = sync(u.get_user_info())
                fans = info.get('fans', 0)
                name = info.get('name', '')
                print(f"  成功! 粉丝: {fans}, 名称: {name}")
            except ImportError:
                print(f"  bilibili-api-python 未安装, 跳过")
            except Exception as e:
                print(f"  [bilibili-api] 错误: {e}")
        
        if fans is None and args.method in ("auto", "wbi"):
            print(f"  尝试 WBI 签名方式...")
            fans, fetched_name = get_user_info_wbi(uid, cookie)
            if fans is not None:
                if fetched_name:
                    name = fetched_name
                print(f"  成功! 粉丝: {fans}, 名称: {name}")
        
        if fans is None and args.method in ("auto", "simple"):
            print(f"  尝试简单接口...")
            fans, fetched_name = get_user_info_simple(uid, cookie)
            if fans is not None:
                if fetched_name:
                    name = fetched_name
                print(f"  成功! 粉丝: {fans}, 名称: {name}")
        
        if fans is not None and fans > 0:
            update_fans(conn, uid, fans, name)
            success_count += 1
        else:
            print(f"  失败, 无法获取粉丝数")
            fail_count += 1
        
        time.sleep(0.5)
    
    print(f"\n{'='*60}")
    print(f"[完成] 处理 {len(uids)} 个 UID")
    print(f"  成功: {success_count}")
    print(f"  失败: {fail_count}")
    print(f"\n提示: 如果大部分失败，请使用 --csv 参数手动导入")
    
    conn.close()


if __name__ == "__main__":
    main()