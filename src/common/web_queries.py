"""
web_queries.py - Web 控制台只读查询层（零第三方依赖）

统一封装 4 个平台的 SQLite 只读查询，供 web_server.py 调用：
  - list_keywords(platform)        平台下所有有数据的关键词（含任务数/日期/完成数）
  - list_snapshots(platform, kw)   某关键词的全部历史快照任务
  - get_ranking(platform, kw, ...) 最新动量/价值排行（复用各平台 util 的评分逻辑）
  - compare_snapshots(...)         两个快照任务的对比（新增/消失/增长）

注意：本模块只读数据库，绝不修改爬虫数据。
"""

import os
import sqlite3

from src.common import paths

# 各平台数据库路径
_PLATFORM_DB = {
    "bili": paths.BILI_DB,
    "xhs": paths.XHS_DB,
    "douyin": paths.DOUYIN_DB,
    "kuaishou": paths.KUAISHOU_DB,
}

# 平台展示名
PLATFORM_NAMES = {
    "bili": "B站",
    "xhs": "小红书",
    "douyin": "抖音",
    "kuaishou": "快手",
}

# 每平台: 主键列 / 主表 / 历史表 / 历史指标列
_SCHEMA = {
    "bili": {
        "id_col": "av_id",
        "main_table": "bili_videos",
        "history_table": "video_history",
        "metric_col": "play_nums",
    },
    "xhs": {
        "id_col": "note_id",
        "main_table": "xhs_notes",
        "history_table": "note_history",
        "metric_col": "interact_count",
    },
    "douyin": {
        "id_col": "aweme_id",
        "main_table": "dy_notes",
        "history_table": "note_history",
        "metric_col": "play_count",
    },
    "kuaishou": {
        "id_col": "video_id",
        "main_table": "ks_videos",
        "history_table": "video_history",
        "metric_col": "play_count",
    },
}

# 哪些平台支持价值分析（kuaishou 无 value 评分）
_VALUE_SUPPORTED = ("bili", "xhs", "douyin")


def _connect(platform):
    db_file = _PLATFORM_DB[platform]
    if not os.path.exists(db_file):
        return None
    conn = sqlite3.connect(db_file)
    conn.row_factory = sqlite3.Row
    return conn


def _task_table_exists(conn):
    """探测 spider_tasks 是否存在（避免旧库报错）"""
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='spider_tasks'"
    ).fetchone()
    return row is not None


def list_keywords(platform):
    """返回该平台所有有数据的关键词: [{keyword, task_count, done_count, dates, video_count}]"""
    if platform not in _PLATFORM_DB:
        return []
    conn = _connect(platform)
    if conn is None or not _task_table_exists(conn):
        return []
    try:
        rows = conn.execute(
            "SELECT keyword, status, created_at, id FROM spider_tasks ORDER BY created_at"
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()

    from collections import OrderedDict
    groups = OrderedDict()
    for r in rows:
        kw = r["keyword"] or ""
        if kw not in groups:
            groups[kw] = {"keyword": kw, "task_count": 0, "done_count": 0,
                          "dates": [], "task_ids": []}
        g = groups[kw]
        g["task_count"] += 1
        if r["status"] == "completed":
            g["done_count"] += 1
        date = str(r["created_at"])[:10]
        if date and date not in g["dates"]:
            g["dates"].append(date)
        g["task_ids"].append(r["id"])
    for g in groups.values():
        g["dates"].sort()
    return list(groups.values())


def list_snapshots(platform, keyword, min_videos=1):
    """某关键词全部历史快照: [{task_id, created_at, status, video_count}]（按时间倒序）"""
    if platform not in _PLATFORM_DB:
        return []
    conn = _connect(platform)
    if conn is None:
        return []
    schema = _SCHEMA[platform]
    id_col = schema["id_col"]
    history_table = schema["history_table"]
    try:
        # 通过 history 表统计每个任务实际记录的视频数（快照语义：历史表为准）
        rows = conn.execute(f"""
            SELECT t.id AS task_id, t.created_at, t.status,
                   COUNT(DISTINCT h.{id_col}) AS video_count
            FROM spider_tasks t
            LEFT JOIN {history_table} h ON h.task_id = t.id
            WHERE t.keyword = ?
            GROUP BY t.id
            HAVING video_count >= ?
            ORDER BY t.created_at DESC
        """, (keyword, min_videos)).fetchall()
    except sqlite3.Error as e:
        return []
    finally:
        conn.close()

    result = []
    for r in rows:
        result.append({
            "task_id": r["task_id"],
            "created_at": r["created_at"],
            "status": r["status"],
            "video_count": r["video_count"],
        })
    return result


def get_ranking(platform, keyword, analysis="momentum", limit=100):
    """
    最新排行。复用各平台 util 的评分方法，统一输出字段：
      {id, title, author, metric_value, score, growth_pct, data_points, url, raw}
    返回空列表表示无数据/平台不支持。
    """
    if platform not in _PLATFORM_DB:
        return []
    if analysis == "value" and platform not in _VALUE_SUPPORTED:
        return []
    db_file = _PLATFORM_DB[platform]

    db = None
    try:
        if platform == "bili":
            from src.bili import bili_util
            db = bili_util.Database(db_file)
            if analysis == "value":
                rows = db.get_keyword_videos_for_value(keyword)
                items = _bili_value_rank(rows)
            else:
                items = db.get_keyword_momentum_ranking(keyword, "play_nums", limit)
        elif platform == "xhs":
            from src.xhs import xhs_util
            db = xhs_util.Database(db_file)
            if analysis == "value":
                items = db.get_value_ranking(keyword, limit)
            else:
                items = db.get_keyword_momentum_ranking(keyword, limit)
        elif platform == "douyin":
            from src.douyin import douyin_util
            db = douyin_util.Database(db_file)
            if analysis == "value":
                items = db.get_value_ranking(keyword, limit)
            else:
                items = db.get_keyword_momentum_ranking(keyword, limit)
        elif platform == "kuaishou":
            from src.kuaishou import kuaishou_util
            db = kuaishou_util.Database(db_file)
            items = db.get_keyword_momentum_ranking(keyword, limit)
        else:
            return []
    except Exception:
        return []
    finally:
        if db is not None:
            try:
                conn = getattr(db, "conn", None)
                if conn is not None:
                    conn.close()
            except Exception:
                pass

    return [_normalize_item(platform, i) for i in items]


def _normalize_item(platform, item):
    """把各平台返回的 dict 规范成统一结构"""
    if platform == "bili":
        return {
            "id": item.get("av_id", ""),
            "title": item.get("title", ""),
            "author": item.get("uploader", "") or item.get("uploader_uid", ""),
            "author_uid": item.get("uploader_uid", ""),
            "metric_value": item.get("current_value", 0),
            "score": item.get("composite_score", 0),
            "growth_pct": item.get("historical_growth_pct"),
            "data_points": item.get("data_points", 1),
            "url": item.get("url", ""),
            "pubdate": item.get("pubdate", ""),
            "tags": item.get("tags", ""),
            "raw": item,
        }
    elif platform == "xhs":
        return {
            "id": item.get("note_id", ""),
            "title": item.get("title", ""),
            "author": item.get("nickname", ""),
            "author_uid": item.get("user_id", ""),
            "metric_value": item.get("current_value", 0),
            "score": item.get("composite_score", 0),
            "growth_pct": item.get("historical_growth_pct"),
            "data_points": item.get("data_points", 1),
            "url": item.get("share_link", "") or item.get("author_url", ""),
            "pubdate": item.get("pub_time", ""),
            "tags": item.get("tags", ""),
            "raw": item,
        }
    elif platform == "douyin":
        return {
            "id": item.get("aweme_id", ""),
            "title": item.get("title", ""),
            "author": item.get("nickname", ""),
            "author_uid": item.get("user_id", ""),
            "metric_value": item.get("total_interact", 0) or item.get("current_value", 0),
            "score": item.get("composite_score", 0),
            "growth_pct": item.get("historical_growth_pct"),
            "data_points": item.get("data_points", 1),
            "url": item.get("page_url", "") or item.get("video_url", ""),
            "pubdate": item.get("pub_time", ""),
            "tags": item.get("tags", ""),
            "raw": item,
        }
    elif platform == "kuaishou":
        return {
            "id": item.get("video_id", ""),
            "title": item.get("title", ""),
            "author": item.get("nickname", ""),
            "author_uid": item.get("author_id", ""),
            "metric_value": item.get("play_count", 0),
            "score": item.get("momentum_score", 0),
            "growth_pct": None,
            "data_points": 1,
            "url": item.get("page_url", ""),
            "pubdate": "",
            "tags": "",
            "raw": item,
        }
    return item


def _bili_value_rank(rows):
    """B站价值分析（与 bili_spider.analyze_value 相同算法，纯计算）"""
    videos = []
    for row in rows:
        (av_id, title, play_nums, uploader, uploader_uid, uploader_fans,
         like_count, coin, favorites, share_count, danmakus, review,
         video_age_hours, pubdate, tags_str) = row
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
            "uploader": uploader, "uploader_uid": uploader_uid,
            "uploader_fans": uploader_fans, "pubdate": pubdate,
            "tags": tags_str or "",
            "deep_ratio": deep_ratio, "engagement_density": engagement_density,
            "fav_rate": fav_rate, "conv_rate": conv_rate, "share_rate": share_rate,
        })

    def normalize(values):
        min_v = min(values); max_v = max(values)
        if max_v == min_v:
            return [0.5] * len(values)
        return [(v - min_v) / (max_v - min_v) for v in values]

    if not videos:
        return []
    deep_norm = normalize([v["deep_ratio"] for v in videos])
    eng_norm = normalize([v["engagement_density"] for v in videos])
    fav_norm = normalize([v["fav_rate"] for v in videos])
    conv_norm = normalize([v["conv_rate"] for v in videos])
    share_norm = normalize([v["share_rate"] for v in videos])
    for i, v in enumerate(videos):
        v["value_score"] = (deep_norm[i] * 0.35 + eng_norm[i] * 0.25 +
                            fav_norm[i] * 0.20 + conv_norm[i] * 0.10 + share_norm[i] * 0.10)
        v["composite_score"] = v["value_score"]  # 统一 score 字段
        v["current_value"] = v["play_nums"]      # 统一 metric 字段
    videos.sort(key=lambda x: x["value_score"], reverse=True)
    return videos


def compare_snapshots(platform, keyword, task_a, task_b):
    """
    对比两个快照任务（均须属于该关键词）：
      - 从历史表取各视频当时指标
      - added:    B 有 A 无（B 新出现）
      - removed:  A 有 B 无（A 有但 B 没再出现）
      - changed:  两者都有 → 附 metric_a/metric_b/delta/growth_pct
    返回 {snapshot_a, snapshot_b, added, removed, changed}
    """
    if platform not in _PLATFORM_DB:
        return None
    conn = _connect(platform)
    if conn is None:
        return None
    schema = _SCHEMA[platform]
    id_col = schema["id_col"]
    main_table = schema["main_table"]
    history_table = schema["history_table"]
    metric_col = schema["metric_col"]

    try:
        # 确认两个任务都属于该关键词
        rows = conn.execute(
            "SELECT id, keyword, created_at, status FROM spider_tasks WHERE id IN (?, ?)",
            (task_a, task_b),
        ).fetchall()
        tasks = {r["id"]: r for r in rows}
        if task_a not in tasks or task_b not in tasks:
            return None
        if tasks[task_a]["keyword"] != keyword or tasks[task_b]["keyword"] != keyword:
            return None

        # 取两个任务的快照数据: id -> {metric, record_time}
        def load(task_id):
            snap = {}
            hrows = conn.execute(
                f"SELECT {id_col} AS vid, {metric_col} AS m, record_time "
                f"FROM {history_table} WHERE task_id = ?",
                (task_id,),
            ).fetchall()
            for r in hrows:
                snap[r["vid"]] = {"metric": r["m"] or 0, "record_time": r["record_time"]}
            return snap

        snap_a = load(task_a)
        snap_b = load(task_b)

        # 主表元数据（标题/作者/链接）
        meta = {}
        all_ids = set(snap_a) | set(snap_b)
        if all_ids:
            placeholders = ",".join("?" * len(all_ids))
            mrows = conn.execute(
                f"SELECT * FROM {main_table} WHERE {id_col} IN ({placeholders})",
                tuple(all_ids),
            ).fetchall()
            for r in mrows:
                meta[r[id_col]] = dict(r)

        def build(vid, metric, rec_time):
            m = meta.get(vid, {})
            return {
                "id": vid,
                "title": m.get("title", "") or vid,
                "author": m.get("uploader") or m.get("nickname") or "",
                "url": m.get("url") or m.get("page_url") or m.get("video_url") or "",
                "metric": metric,
                "record_time": rec_time,
            }

        added = []
        removed = []
        changed = []
        for vid, vb in snap_b.items():
            if vid not in snap_a:
                added.append(build(vid, vb["metric"], vb["record_time"]))
            else:
                va = snap_a[vid]
                delta = (vb["metric"] or 0) - (va["metric"] or 0)
                growth = None
                if va["metric"]:
                    growth = round(delta / va["metric"] * 100, 2)
                item = build(vid, vb["metric"], vb["record_time"])
                item.update({
                    "metric_a": va["metric"], "metric_b": vb["metric"],
                    "delta": delta, "growth_pct": growth,
                })
                changed.append(item)
        for vid, va in snap_a.items():
            if vid not in snap_b:
                removed.append(build(vid, va["metric"], va["record_time"]))

        changed.sort(key=lambda x: x.get("growth_pct") or -1e9, reverse=True)
        added.sort(key=lambda x: x["metric"], reverse=True)
        removed.sort(key=lambda x: x["metric"], reverse=True)

        return {
            "snapshot_a": {"task_id": task_a, "created_at": tasks[task_a]["created_at"],
                           "status": tasks[task_a]["status"], "count": len(snap_a)},
            "snapshot_b": {"task_id": task_b, "created_at": tasks[task_b]["created_at"],
                           "status": tasks[task_b]["status"], "count": len(snap_b)},
            "added": added,
            "removed": removed,
            "changed": changed,
        }
    finally:
        conn.close()
