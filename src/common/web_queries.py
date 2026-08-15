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
import threading
from datetime import date, timedelta

from src.common import paths
from src.common import web_preferences

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

# 各平台都支持基于已抓取互动指标的价值分析。
_VALUE_SUPPORTED = ("bili", "xhs", "douyin", "kuaishou")

_HISTORY_METRICS = {
    "bili": [("play_nums", "播放"), ("like_count", "点赞"), ("favorites", "收藏"),
             ("coin", "投币"), ("review", "评论"), ("danmakus", "弹幕"), ("share", "分享")],
    "xhs": [("interact_count", "互动"), ("liked_count", "点赞"),
            ("collected_count", "收藏"), ("comment_count", "评论"), ("share_count", "分享")],
    "douyin": [("play_count", "播放"), ("liked_count", "点赞"),
               ("comment_count", "评论"), ("share_count", "分享")],
    "kuaishou": [("play_count", "播放"), ("liked_count", "点赞"),
                 ("comment_count", "评论"), ("share_count", "分享")],
}

_ALGORITHM_FACTORS = {
    ("bili", "momentum"): [("velocity_score", "播放速率", .30), ("conversion_score", "粉丝转化", .25),
                           ("engagement_norm_score", "互动表现", .20), ("freshness_normalized", "新鲜度", .15),
                           ("normalized_value", "播放规模", .10)],
    ("bili", "value"): [("deep_ratio", "深度互动比", .35), ("engagement_density", "互动密度", .25),
                        ("fav_rate", "收藏率", .20), ("conv_rate", "粉丝转化率", .10),
                        ("share_rate", "分享率", .10)],
    ("xhs", "momentum"): [("velocity_score", "互动速率", .35), ("engagement_norm_score", "互动密度", .30),
                          ("freshness_normalized", "新鲜度", .20), ("normalized_value", "互动规模", .15)],
    ("xhs", "value"): [("collect_score", "收藏率", .40), ("engage_score", "收藏互动密度", .30),
                       ("comment_score", "评论率", .30)],
    ("douyin", "momentum"): [("velocity_score", "互动速率", .35), ("density_score", "互动规模", .30),
                             ("freshness_score", "新鲜度", .20), ("comment_activity_score", "评论活跃度", .15)],
    ("douyin", "value"): [("collect_score", "收藏率", .35), ("share_score", "分享率", .25),
                          ("comment_score", "评论率", .20), ("interact_score", "综合互动率", .20)],
    ("kuaishou", "momentum"): [("velocity_score", "播放速率", .40), ("play_score", "播放规模", .25),
                               ("engagement_score", "互动率", .20), ("freshness_score", "新鲜度", .15)],
    ("kuaishou", "value"): [("like_score", "点赞率", .35), ("comment_score", "评论率", .25),
                              ("share_score", "分享率", .20), ("interaction_score", "互动率", .20)],
}


def _connect(platform):
    db_file = _PLATFORM_DB[platform]
    if not os.path.exists(db_file):
        return None
    uri = "file:" + os.path.abspath(db_file).replace("\\", "/") + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
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
                          "dates": [], "task_ids": [], "last_task_at": None}
        g = groups[kw]
        g["task_count"] += 1
        if r["status"] == "completed":
            g["done_count"] += 1
        date = str(r["created_at"])[:10]
        if date and date not in g["dates"]:
            g["dates"].append(date)
        g["task_ids"].append(r["id"])
        created_at = r["created_at"]
        if created_at is not None and (g["last_task_at"] is None or str(created_at) > str(g["last_task_at"])):
            g["last_task_at"] = created_at
    for g in groups.values():
        g["dates"].sort()
    return sorted(groups.values(), key=lambda g: str(g["last_task_at"] or ""), reverse=True)


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


def list_ranking_dates(platform, keyword):
    """Return dates that actually contain history rows for this keyword."""
    if platform not in _PLATFORM_DB or not keyword:
        return []
    conn = _connect(platform)
    if conn is None or not _task_table_exists(conn):
        return []
    schema = _SCHEMA[platform]
    try:
        rows = conn.execute(f"""
            SELECT DISTINCT date(h.record_time) AS snapshot_date
            FROM {schema['history_table']} h
            JOIN spider_tasks t ON t.id = h.task_id
            WHERE t.keyword = ? AND h.record_time IS NOT NULL
            ORDER BY snapshot_date DESC
        """, (keyword,)).fetchall()
        return [row[0] for row in rows if row[0]]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def get_ranking(platform, keyword, analysis="momentum", limit=100, as_of_date=None):
    """
    最新排行。复用各平台 util 的评分方法，统一输出字段：
      {id, title, author, metric_value, score, growth_pct, data_points, url, raw}
    返回空列表表示无数据/平台不支持。
    """
    if platform not in _PLATFORM_DB:
        return []
    if analysis == "value" and platform not in _VALUE_SUPPORTED:
        return []
    conn = _connect(platform)
    if conn is None:
        return []
    if as_of_date:
        try:
            conn = _as_of_snapshot(conn, platform, as_of_date)
        except (TypeError, ValueError, sqlite3.Error):
            conn.close()
            return []
    db = None
    try:
        if platform == "bili":
            from src.bili import bili_util
            db = _readonly_database(bili_util.Database, conn)
            if analysis == "value":
                rows = db.get_keyword_videos_for_value(keyword)
                items = _bili_value_rank(rows)
            else:
                items = db.get_keyword_momentum_ranking(keyword, "play_nums", limit)
        elif platform == "xhs":
            from src.xhs import xhs_util
            db = _readonly_database(xhs_util.Database, conn)
            if analysis == "value":
                items = db.get_value_ranking(keyword, limit)
            else:
                items = db.get_keyword_momentum_ranking(keyword, limit)
        elif platform == "douyin":
            from src.douyin import douyin_util
            db = _readonly_database(douyin_util.Database, conn)
            if analysis == "value":
                items = db.get_value_ranking(keyword, limit)
            else:
                items = db.get_keyword_momentum_ranking(keyword, limit)
        elif platform == "kuaishou":
            from src.kuaishou import kuaishou_util
            conn.row_factory = sqlite3.Row
            db = _readonly_database(kuaishou_util.Database, conn)
            items = (db.get_value_ranking(keyword, limit)
                     if analysis == "value" else
                     db.get_keyword_momentum_ranking(keyword, limit))
        else:
            return []
        _attach_first_seen(conn, platform, items)
        _attach_urls(conn, platform, items)
        _attach_history(conn, platform, items)
        _attach_algorithm(platform, analysis, items)
    except sqlite3.OperationalError:
        # Legacy schemas may miss columns expected by current scoring helpers.
        # Upgrade an in-memory copy only; the source database remains read-only.
        try:
            memory = sqlite3.connect(":memory:")
            conn.backup(memory)
            conn.close()
            conn = memory
            conn.row_factory = sqlite3.Row
            db = _readonly_database(type(db), conn)
            db.create_tables()
            if platform == "bili":
                items = (_bili_value_rank(db.get_keyword_videos_for_value(keyword))
                         if analysis == "value" else
                         db.get_keyword_momentum_ranking(keyword, "play_nums", limit))
            elif platform == "xhs":
                items = (db.get_value_ranking(keyword, limit) if analysis == "value"
                         else db.get_keyword_momentum_ranking(keyword, limit))
            elif platform == "douyin":
                items = (db.get_value_ranking(keyword, limit) if analysis == "value"
                         else db.get_keyword_momentum_ranking(keyword, limit))
            else:
                items = (db.get_value_ranking(keyword, limit)
                         if analysis == "value" else
                         db.get_keyword_momentum_ranking(keyword, limit))
            _attach_first_seen(conn, platform, items)
            _attach_urls(conn, platform, items)
            _attach_history(conn, platform, items)
            _attach_algorithm(platform, analysis, items)
        except Exception:
            return []
    except Exception:
        return []
    finally:
        if db is not None:
            try:
                conn.close()
            except Exception:
                pass

    # 永久隐藏（记录级黑名单）：隐藏的记录不进入任何结果视图，
    # 但数据库原数据保留，重新采集也不会再显示。
    hidden_set = web_preferences.list_hidden_records(platform)
    normalized = [_normalize_item(platform, i) for i in items]
    if hidden_set:
        normalized = [n for n in normalized if str(n.get("id") or "") not in hidden_set]
    return normalized


def _as_of_snapshot(source, platform, as_of_date):
    """Return an in-memory DB whose counters stop at the end of ``as_of_date``."""
    day = date.fromisoformat(as_of_date)
    cutoff = (day + timedelta(days=1)).isoformat()
    memory = sqlite3.connect(":memory:")
    memory.row_factory = sqlite3.Row
    source.backup(memory)
    source.close()

    schema = _SCHEMA[platform]
    id_col = schema["id_col"]
    main_table = schema["main_table"]
    history_table = schema["history_table"]
    history_columns = {row[1] for row in memory.execute(f"PRAGMA table_info({history_table})")}
    main_columns = {row[1] for row in memory.execute(f"PRAGMA table_info({main_table})")}
    memory.execute(f"DELETE FROM {history_table} WHERE record_time >= ?", (cutoff,))
    memory.execute(
        f"DELETE FROM {main_table} WHERE NOT EXISTS ("
        f"SELECT 1 FROM {history_table} h WHERE h.{id_col} = {main_table}.{id_col})"
    )

    # task relation tables are used by XHS/Kuaishou ranking queries. Remove videos
    # that have no snapshot at or before the selected day, so future discoveries
    # cannot leak into a historical ranking.
    relation = {"xhs": ("task_notes", "note_id"), "kuaishou": ("task_videos", "video_id")}.get(platform)
    if relation:
        relation_table, relation_id = relation
        memory.execute(
            f"DELETE FROM {relation_table} WHERE task_id IN ("
            "SELECT id FROM spider_tasks WHERE created_at >= ?)", (cutoff,)
        )
        memory.execute(
            f"DELETE FROM {relation_table} WHERE NOT EXISTS ("
            f"SELECT 1 FROM {history_table} h WHERE h.{id_col} = {relation_table}.{relation_id})"
        )

    metric_map = {
        "bili": ["play_nums", "danmakus", "favorites", "review", "coin", "share", "like_count"],
        "xhs": ["interact_count", "liked_count", "collected_count", "comment_count", "share_count"],
        "douyin": ["play_count", "liked_count", "comment_count", "share_count"],
        "kuaishou": ["play_count", "liked_count", "comment_count", "share_count"],
    }[platform]
    for column in metric_map:
        if column not in history_columns or column not in main_columns:
            continue
        memory.execute(f"""
            UPDATE {main_table} AS m SET {column} = COALESCE((
                SELECT h.{column} FROM {history_table} h
                WHERE h.{id_col} = m.{id_col}
                ORDER BY h.record_time DESC, h.id DESC LIMIT 1
            ), 0)
        """)

    # Rebuild rate fields solely from retained snapshots. This prevents a rate
    # calculated during a later crawl from influencing the historical score.
    primary = schema["metric_col"]
    velocity_col = "play_velocity" if platform in ("bili", "kuaishou") else "interact_velocity"
    if velocity_col in main_columns:
        memory.execute(f"""
            UPDATE {main_table} AS m SET {velocity_col} = COALESCE((
                SELECT CASE WHEN julianday(MAX(h.record_time)) > julianday(MIN(h.record_time))
                    THEN (MAX(h.{primary}) - MIN(h.{primary})) /
                         ((julianday(MAX(h.record_time)) - julianday(MIN(h.record_time))) * 24.0)
                    ELSE 0 END
                FROM {history_table} h WHERE h.{id_col} = m.{id_col}
            ), 0)
        """)
    if platform == "bili" and "engagement_score" in main_columns:
        memory.execute(f"""
            UPDATE {main_table} SET engagement_score =
                (COALESCE(like_count, 0) + COALESCE(coin, 0) + COALESCE(favorites, 0) +
                 COALESCE(review, 0) + COALESCE(danmakus, 0)) / MAX(COALESCE(play_nums, 0), 1.0)
        """)
    if platform == "kuaishou" and "engagement_rate" in main_columns:
        memory.execute(f"""
            UPDATE {main_table} SET engagement_rate =
                (COALESCE(liked_count, 0) + COALESCE(comment_count, 0) + COALESCE(share_count, 0)) /
                MAX(COALESCE(play_count, 0), 1.0)
        """)
    memory.commit()
    return memory


def delete_record(platform, record_id):
    """删除一个平台记录及其历史采集数据，不删除爬取任务本身。"""
    if platform not in _PLATFORM_DB or record_id is None or str(record_id).strip() == "":
        raise ValueError("platform 或 record_id 无效")
    db_file = _PLATFORM_DB[platform]
    if not os.path.exists(db_file):
        raise ValueError("数据文件不存在")
    schema = _SCHEMA[platform]
    record_id = str(record_id).strip()
    conn = sqlite3.connect(db_file)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        exists = conn.execute(
            f"SELECT 1 FROM {schema['main_table']} WHERE {schema['id_col']} = ? LIMIT 1",
            (record_id,),
        ).fetchone()
        if not exists:
            raise ValueError("记录不存在")
        cleanup_tables = [(schema["history_table"], schema["id_col"])]
        if platform == "xhs":
            cleanup_tables += [("task_notes", "note_id"), ("failed_notes", "note_id")]
        elif platform == "kuaishou":
            cleanup_tables += [("task_videos", "video_id"), ("failed_videos", "video_id")]
        elif platform == "bili":
            cleanup_tables += [("failed_videos", "av_id")]
        else:
            cleanup_tables += [("failed_notes", "aweme_id")]
        for table, column in cleanup_tables:
            table_exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?", (table,)
            ).fetchone()
            if table_exists:
                conn.execute(f"DELETE FROM {table} WHERE {column} = ?", (record_id,))
        conn.execute(f"DELETE FROM {schema['main_table']} WHERE {schema['id_col']} = ?", (record_id,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return True


def _readonly_database(database_class, conn):
    """Create a scoring helper without running its schema-mutating constructor."""
    db = database_class.__new__(database_class)
    db._local = threading.local()
    db._local.conn = conn
    db._db_file = _PLATFORM_DB.get(database_class.__module__.split(".")[-2], "")
    return db


def _attach_first_seen(conn, platform, items):
    schema = _SCHEMA[platform]
    id_col = schema["id_col"]
    main_table = schema["main_table"]
    history_table = schema["history_table"]
    columns = {r[1] for r in conn.execute(f"PRAGMA table_info({main_table})")}
    first_expr = "m.first_seen_at" if "first_seen_at" in columns else "NULL"
    rows = conn.execute(f"""
        SELECT m.{id_col}, COALESCE({first_expr}, MIN(h.record_time), m.fetched_at)
        FROM {main_table} m
        LEFT JOIN {history_table} h ON h.{id_col} = m.{id_col}
        GROUP BY m.{id_col}
    """).fetchall()
    first_seen = {r[0]: r[1] for r in rows}
    for item in items:
        item["first_seen_at"] = first_seen.get(item.get(id_col))


def _attach_urls(conn, platform, items):
    """Fill each platform's canonical work URL from its main table."""
    if not items:
        return
    schema = _SCHEMA[platform]
    id_col = schema["id_col"]
    main_table = schema["main_table"]
    columns = {r[1] for r in conn.execute(f"PRAGMA table_info({main_table})")}
    url_col = {"bili": "url", "xhs": "url", "douyin": "video_url", "kuaishou": "page_url"}[platform]
    if url_col not in columns:
        return
    ids = [item.get(id_col) for item in items if item.get(id_col) is not None]
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT {id_col}, {url_col} FROM {main_table} WHERE {id_col} IN ({placeholders})", ids
    ).fetchall()
    urls = {str(row[0]): row[1] or "" for row in rows}
    for item in items:
        vid = str(item.get(id_col) or "")
        url = urls.get(vid, "")
        if platform == "bili" and not url and vid:
            url = f"https://www.bilibili.com/video/av{vid}"
        if platform == "xhs":
            item["share_link"] = item.get("share_link") or url
        elif platform == "douyin":
            item["page_url"] = item.get("page_url") or (f"https://www.douyin.com/video/{vid}" if vid else "")
            item["video_url"] = item.get("video_url") or url
        else:
            item["url" if platform == "bili" else "page_url"] = item.get("url" if platform == "bili" else "page_url") or url


def _attach_history(conn, platform, items):
    if not items:
        return
    schema = _SCHEMA[platform]
    id_col = schema["id_col"]
    history_table = schema["history_table"]
    available = {r[1] for r in conn.execute(f"PRAGMA table_info({history_table})")}
    metrics = [(key, label) for key, label in _HISTORY_METRICS[platform] if key in available]
    ids = [item.get(id_col) for item in items if item.get(id_col) is not None]
    placeholders = ",".join("?" for _ in ids)
    selected = ", ".join(key for key, _ in metrics)
    rows = conn.execute(
        f"SELECT {id_col}, record_time{', ' + selected if selected else ''} "
        f"FROM {history_table} WHERE {id_col} IN ({placeholders}) ORDER BY record_time",
        ids,
    ).fetchall()
    grouped = {vid: [] for vid in ids}
    for row in rows:
        grouped.setdefault(row[0], []).append({
            "record_time": row[1],
            "metrics": [{"key": key, "label": label, "value": row[key]} for key, label in metrics],
        })
    for item in items:
        item["history"] = grouped.get(item.get(id_col), [])


def _attach_algorithm(platform, analysis, items):
    factors = _ALGORITHM_FACTORS.get((platform, analysis), [])
    for item in items:
        item["algorithm"] = [{
            "key": key, "label": label, "value": item.get(key), "weight": weight,
        } for key, label, weight in factors]


def _first_seen(item):
    return item.get("first_seen_at") or item.get("fetched_at")


def _normalize_item(platform, item):
    """把各平台返回的 dict 规范成统一结构"""
    if platform == "bili":
        return {
            "id": item.get("av_id", ""),
            "title": item.get("title", ""),
            "author": item.get("uploader", "") or item.get("uploader_uid", ""),
            "author_uid": item.get("uploader_uid", ""),
            "metric_value": item.get("current_value", 0),
            "score": item.get("composite_score", item.get("value_score", 0)),
            "growth_pct": item.get("historical_growth_pct"),
            "data_points": item.get("data_points", 1),
            "url": item.get("url", ""),
            "pubdate": item.get("pubdate", ""),
            "tags": item.get("tags", ""),
            "first_seen_at": _first_seen(item),
            "history": item.get("history", []),
            "algorithm": item.get("algorithm", []),
            "raw": item,
        }
    elif platform == "xhs":
        return {
            "id": item.get("note_id", ""),
            "title": item.get("title", ""),
            "author": item.get("nickname", ""),
            "author_uid": item.get("user_id", ""),
            "metric_value": item.get("current_value", item.get("interact_count", 0)),
            "score": item.get("composite_score", item.get("value_score", 0)),
            "growth_pct": item.get("historical_growth_pct"),
            "data_points": item.get("data_points", 1),
            "url": item.get("share_link", "") or item.get("author_url", ""),
            "pubdate": item.get("pub_time", ""),
            "tags": item.get("tags", ""),
            "first_seen_at": _first_seen(item),
            "history": item.get("history", []),
            "algorithm": item.get("algorithm", []),
            "raw": item,
        }
    elif platform == "douyin":
        return {
            "id": item.get("aweme_id", ""),
            "title": item.get("title", ""),
            "author": item.get("nickname", ""),
            "author_uid": item.get("user_id", ""),
            "metric_value": item.get("total_interact", 0) or item.get("current_value", 0),
            "score": item.get("composite_score", item.get("value_score", 0)),
            "growth_pct": item.get("historical_growth_pct"),
            "data_points": item.get("data_points", 1),
            "url": item.get("page_url", "") or item.get("video_url", ""),
            "pubdate": item.get("pub_time", ""),
            "tags": item.get("tags", ""),
            "first_seen_at": _first_seen(item),
            "history": item.get("history", []),
            "algorithm": item.get("algorithm", []),
            "raw": item,
        }
    elif platform == "kuaishou":
        return {
            "id": item.get("video_id", ""),
            "title": item.get("title", ""),
            "author": item.get("nickname", ""),
            "author_uid": item.get("author_id", ""),
            "metric_value": item.get("current_value", item.get("play_count", 0)),
            "score": item.get("composite_score", item.get("momentum_score", 0)),
            "growth_pct": None,
            "data_points": 1,
            "url": item.get("page_url", ""),
            "pubdate": "",
            "tags": "",
            "first_seen_at": _first_seen(item),
            "history": item.get("history", []),
            "algorithm": item.get("algorithm", []),
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

        # 永久隐藏（记录级黑名单）：对比视图同样排除已隐藏记录
        hidden_set = web_preferences.list_hidden_records(platform)
        if hidden_set:
            added = [x for x in added if str(x["id"]) not in hidden_set]
            removed = [x for x in removed if str(x["id"]) not in hidden_set]
            changed = [x for x in changed if str(x["id"]) not in hidden_set]

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


# 各平台主表作者列名（用于黑名单视图展示）
_AUTHOR_COL = {
    "bili": "uploader",
    "xhs": "nickname",
    "douyin": "nickname",
    "kuaishou": "nickname",
}


def list_blacklist(platform):
    """黑名单管理视图数据：返回 [{record_id, hidden_at, note, title, author, url}, ...]

    明细（hidden_at/note）来自 web_preferences.hidden_records，
    标题/作者/链接来自各平台主表（记录若已被永久删除则缺省为空）。
    """
    if platform not in _PLATFORM_DB:
        return []
    entries = web_preferences.list_hidden_records_detail(platform)
    if not entries:
        return []
    conn = _connect(platform)
    if conn is None:
        return entries
    schema = _SCHEMA[platform]
    id_col = schema["id_col"]
    main_table = schema["main_table"]
    author_col = _AUTHOR_COL.get(platform, "nickname")
    ids = [e["record_id"] for e in entries]
    placeholders = ",".join("?" for _ in ids)
    try:
        rows = conn.execute(
            f"SELECT {id_col}, title, COALESCE({author_col}, '') AS author, url "
            f"FROM {main_table} WHERE {id_col} IN ({placeholders})",
            ids,
        ).fetchall()
        meta = {r[0]: {"title": r[1] or "", "author": r[2] or "", "url": r[3] or ""}
                for r in rows}
    except sqlite3.Error:
        meta = {}
    finally:
        conn.close()
    for e in entries:
        m = meta.get(e["record_id"], {})
        e["title"] = m.get("title", "")
        e["author"] = m.get("author", "")
        e["url"] = m.get("url", "")
    return entries
