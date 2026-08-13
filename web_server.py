#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
web_server.py - 0PaChongBili Web 控制台
零第三方依赖（仅 Python 标准库）：http.server + subprocess + SSE

功能:
  - 网页里选择平台/关键词/页数，一键启动爬虫子进程
  - 爬虫的 print 输出通过 SSE 实时流式推送到浏览器（终端效果）
  - 任务列表 / 停止任务

用法:
  python web_server.py --port 8900
  浏览器打开 http://127.0.0.1:8900
"""

import argparse
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from datetime import date
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from src.common import web_preferences, web_queries

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(PROJECT_ROOT, "web")
MAX_LINES = 5000  # 每个任务内存中保留的最大日志行数

# 平台 → 展示名 / 可选模式 / 默认模式
PLATFORMS = {
    "bili": {
        "name": "B站",
        "modes": ["search", "momentum", "value"],
        "default_mode": "search",
    },
    "xhs": {"name": "小红书", "modes": ["both"], "default_mode": "both"},
    "douyin": {"name": "抖音", "modes": ["both"], "default_mode": "both"},
    "kuaishou": {"name": "快手", "modes": ["both"], "default_mode": "both"},
}

# 任务存储: task_id -> dict
TASKS = {}
TASKS_LOCK = threading.RLock()  # RLock: task_to_dict 会在持有锁的循环内被调用


def build_command(platform, keyword, pages, mode):
    """构造爬虫命令行。与各平台 main 的 CLI 参数保持一致。"""
    py = sys.executable
    if platform == "bili":
        cmd = [py, os.path.join("src", "bili", "bili_main.py"),
               "-m", mode, "-k", keyword, "-p", str(pages)]
    elif platform == "xhs":
        cmd = [py, os.path.join("src", "xhs", "xhs_main.py"),
               "search", "-k", keyword, "-p", str(pages)]
    elif platform == "douyin":
        cmd = [py, os.path.join("src", "douyin", "douyin_main.py"),
               "search", "-k", keyword, "-p", str(pages)]
    elif platform == "kuaishou":
        cmd = [py, "-m", "src.kuaishou.kuaishou_main",
               "search", "-k", keyword, "-p", str(pages)]
    else:
        raise ValueError(f"未知平台: {platform}")
    return cmd


def task_runner(task):
    """后台线程：启动子进程，逐行读取 print 输出写入任务日志。"""
    with TASKS_LOCK:
        if task["status"] == "killed":
            task["finished_at"] = time.time()
            return
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"  # 保证子进程 stdout 是 UTF-8
    env["PYTHONUNBUFFERED"] = "1"
    try:
        proc = subprocess.Popen(
            task["command"],
            cwd=PROJECT_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,  # 合并 stderr，保证所有 print/报错都进流
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,  # 行缓冲，实时读到每一行
            env=env,
        )
    except Exception as e:
        with TASKS_LOCK:
            task["status"] = "failed"
            task["error"] = str(e)
            task["lines"].append(f"[启动失败] {e}")
            task["finished_at"] = time.time()
        return

    with TASKS_LOCK:
        task["proc"] = proc
        task["pid"] = proc.pid
        if task["status"] == "killed":
            proc.terminate()
        else:
            task["status"] = "running"

    try:
        for line in proc.stdout:
            line = line.rstrip("\r\n")
            with TASKS_LOCK:
                task["lines"].append(line)
                if len(task["lines"]) > MAX_LINES:
                    # 丢弃最旧的，保留最近 MAX_LINES 行
                    for _ in range(len(task["lines"]) - MAX_LINES):
                        task["lines"].popleft()
        proc.wait()
        with TASKS_LOCK:
            # 若已被手动 kill，保留 killed 状态，不覆盖
            if task["status"] != "killed":
                task["status"] = "finished" if proc.returncode == 0 else "failed"
            task["exit_code"] = proc.returncode
            task["finished_at"] = time.time()
    except Exception as e:
        with TASKS_LOCK:
            task["status"] = "failed"
            task["error"] = str(e)
            task["finished_at"] = time.time()


def create_task(platform, keyword, pages, mode):
    """创建并启动一个爬虫任务，返回 task dict。"""
    command = build_command(platform, keyword, pages, mode)
    task_id = uuid.uuid4().hex[:12]
    task = {
        "id": task_id,
        "platform": platform,
        "platform_name": PLATFORMS[platform]["name"],
        "keyword": keyword,
        "pages": pages,
        "mode": mode,
        "command": command,
        "status": "starting",   # starting/running/finished/failed/killed
        "pid": None,
        "exit_code": None,
        "error": None,
        "created_at": time.time(),
        "finished_at": None,
        "lines": deque(maxlen=MAX_LINES),
        "proc": None,
    }
    with TASKS_LOCK:
        TASKS[task_id] = task
    threading.Thread(target=task_runner, args=(task,), daemon=True).start()
    return task


def task_to_dict(task):
    """任务的 JSON 视图（不含巨大日志）。"""
    with TASKS_LOCK:
        return {
            "id": task["id"],
            "platform": task["platform"],
            "platform_name": task["platform_name"],
            "keyword": task["keyword"],
            "pages": task["pages"],
            "mode": task["mode"],
            "status": task["status"],
            "pid": task["pid"],
            "exit_code": task["exit_code"],
            "error": task["error"],
            "created_at": task["created_at"],
            "finished_at": task["finished_at"],
            "line_count": len(task["lines"]),
            "command": " ".join(task["command"]),
        }


class Handler(BaseHTTPRequestHandler):
    server_version = "PaChongWeb/1.0"

    # ---------- 工具 ----------
    def _send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def log_message(self, fmt, *args):
        # 关闭默认的每请求访问日志，避免刷屏
        pass

    # ---------- 路由 ----------
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path in ("/", "/index.html"):
            self._serve_file(os.path.join(WEB_DIR, "index.html"), "text/html; charset=utf-8")
        elif path == "/api/health":
            self._send_json({"ok": True, "project": "0PaChongBili"})
        elif path == "/api/platforms":
            self._send_json(PLATFORMS)
        elif path == "/api/analyses":
            # 所有平台的关键词概览（供结果页/对比页下拉）
            self._handle_analyses()
        elif path == "/api/tasks":
            with TASKS_LOCK:
                tasks = [task_to_dict(t) for t in TASKS.values()]
            tasks.sort(key=lambda t: t["created_at"], reverse=True)
            self._send_json(tasks)
        elif path.startswith("/api/tasks/"):
            parts = path.strip("/").split("/")
            # /api/tasks/<id>/logs
            if len(parts) == 4 and parts[1] == "tasks" and parts[3] == "logs":
                self._stream_logs(parts[2])
            else:
                self._send_json({"error": "not found"}, 404)
        elif path.startswith("/api/results/"):
            self._handle_results(path)
        elif path.startswith("/api/result-dates/"):
            self._handle_result_dates(path)
        elif path.startswith("/api/snapshots/"):
            self._handle_snapshots(path)
        else:
            self._send_json({"error": "not found"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path

        if path == "/api/crawl":
            body = self._read_json()
            platform = body.get("platform", "bili")
            keyword = (body.get("keyword") or "").strip()
            pages = int(body.get("pages") or 3)
            mode = body.get("mode") or PLATFORMS.get(platform, {}).get("default_mode", "")

            if platform not in PLATFORMS:
                self._send_json({"error": f"未知平台: {platform}"}, 400)
                return
            if not keyword:
                self._send_json({"error": "关键词不能为空"}, 400)
                return
            if pages < 1 or pages > 50:
                self._send_json({"error": "页数需在 1-50 之间"}, 400)
                return
            try:
                task = create_task(platform, keyword, pages, mode)
            except ValueError as e:
                self._send_json({"error": str(e)}, 400)
                return
            self._send_json(task_to_dict(task), 201)

        elif path == "/api/history-visibility":
            body = self._read_json()
            platform = body.get("platform")
            video_id = str(body.get("video_id") or "").strip()
            record_date = str(body.get("record_date") or "").strip()
            hidden = body.get("hidden") is True
            if platform not in PLATFORMS or not video_id or len(record_date) != 10:
                self._send_json({"error": "platform/video_id/record_date 参数无效"}, 400)
                return
            try:
                time.strptime(record_date, "%Y-%m-%d")
            except ValueError:
                self._send_json({"error": "record_date 需为 YYYY-MM-DD"}, 400)
                return
            web_preferences.set_hidden(platform, video_id, record_date, hidden)
            self._send_json({"ok": True, "hidden": hidden})

        elif path == "/api/results/note":
            body = self._read_json()
            platform = body.get("platform")
            record_id = body.get("record_id")
            content = body.get("content")
            if platform not in PLATFORMS or record_id is None or content is None:
                self._send_json({"error": "platform/record_id/content 参数无效"}, 400)
                return
            try:
                note = web_preferences.save_record_note(platform, record_id, content)
            except ValueError as e:
                self._send_json({"error": str(e)}, 400)
            except Exception as e:
                self._send_json({"error": f"保存笔记失败: {e}"}, 500)
            else:
                self._send_json({
                    "ok": True, "platform": platform, "record_id": str(record_id), **note
                })

        elif path == "/api/results/delete":
            body = self._read_json()
            platform = body.get("platform")
            record_id = body.get("record_id")
            if platform not in PLATFORMS or record_id is None or not str(record_id).strip():
                self._send_json({"error": "platform/record_id 参数无效"}, 400)
                return
            try:
                web_queries.delete_record(platform, record_id)
                web_preferences.delete_record_note(platform, record_id)
            except ValueError as e:
                self._send_json({"error": str(e)}, 404)
            except Exception as e:
                self._send_json({"error": f"删除失败: {e}"}, 500)
            else:
                self._send_json({"ok": True, "record_id": str(record_id)})

        elif path.startswith("/api/tasks/") and path.endswith("/kill"):
            parts = path.strip("/").split("/")
            # /api/tasks/<id>/kill
            if len(parts) != 4 or parts[1] != "tasks" or parts[3] != "kill":
                self._send_json({"error": "not found"}, 404)
                return
            task_id = parts[2]
            with TASKS_LOCK:
                task = TASKS.get(task_id)
            if not task:
                self._send_json({"error": "task not found"}, 404)
                return
            proc = task.get("proc")
            if proc and proc.poll() is None:
                try:
                    proc.terminate()  # Windows 下即 TerminateProcess
                except Exception as e:
                    self._send_json({"error": str(e)}, 500)
                    return
            with TASKS_LOCK:
                task["status"] = "killed"
                task["finished_at"] = time.time()
            self._send_json(task_to_dict(task))

        else:
            self._send_json({"error": "not found"}, 404)

    # ---------- 结果/快照 API ----------
    def _handle_analyses(self):
        """GET /api/analyses → {platform: {name, keywords: [...]}}"""
        result = {}
        for platform in PLATFORMS:
            kws = web_queries.list_keywords(platform)
            result[platform] = {
                "name": PLATFORMS[platform]["name"],
                "keywords": kws,
            }
        self._send_json(result)

    def _handle_results(self, path):
        """GET /api/results/<platform>?keyword=&analysis= → 排行列表"""
        parts = path.strip("/").split("/")
        if len(parts) != 3:
            self._send_json({"error": "bad path"}, 400)
            return
        platform = parts[2]
        qs = parse_qs(urlparse(self.path).query)
        keyword = (qs.get("keyword") or [""])[0].strip()
        analysis = (qs.get("analysis") or ["momentum"])[0]
        as_of_date = (qs.get("as_of_date") or [""])[0].strip()
        try:
            page = int((qs.get("page") or ["1"])[0])
        except (TypeError, ValueError):
            self._send_json({"error": "page 必须是正整数"}, 400)
            return
        if platform not in PLATFORMS:
            self._send_json({"error": f"未知平台: {platform}"}, 400)
            return
        if not keyword:
            self._send_json({"error": "缺少 keyword 参数"}, 400)
            return
        if page < 1:
            self._send_json({"error": "page 必须是正整数"}, 400)
            return
        if as_of_date:
            try:
                date.fromisoformat(as_of_date)
            except ValueError:
                self._send_json({"error": "as_of_date 必须是 YYYY-MM-DD"}, 400)
                return
        if analysis not in ("momentum", "value"):
            self._send_json({"error": "analysis 仅支持 momentum 或 value"}, 400)
            return
        page_size = 99
        all_items = web_queries.get_ranking(
            platform, keyword, analysis, limit=None, as_of_date=as_of_date or None
        )
        total = len(all_items)
        total_pages = (total + page_size - 1) // page_size
        if total_pages and page > total_pages:
            page = total_pages
        start = (page - 1) * page_size
        items = all_items[start:start + page_size]
        hidden = web_preferences.list_hidden_days(platform, [item.get("id") for item in items])
        notes = web_preferences.list_record_notes(platform, [item.get("id") for item in items])
        for item in items:
            record_id = str(item.get("id"))
            item["hidden_history_days"] = hidden.get(record_id, [])
            item["note"] = notes.get(record_id, {}).get("content", "")
            item["note_updated_at"] = notes.get(record_id, {}).get("updated_at")
        self._send_json({
            "platform": platform,
            "platform_name": PLATFORMS[platform]["name"],
            "keyword": keyword,
            "analysis": analysis,
            "as_of_date": as_of_date or None,
            "value_supported": platform in ("bili", "xhs", "douyin", "kuaishou"),
            "count": len(items),
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": total_pages,
            "items": items,
        })

    def _handle_result_dates(self, path):
        """GET /api/result-dates/<platform>?keyword= → 有实际快照的日期。"""
        parts = path.strip("/").split("/")
        platform = parts[2] if len(parts) == 3 else ""
        keyword = (parse_qs(urlparse(self.path).query).get("keyword") or [""])[0].strip()
        if platform not in PLATFORMS:
            self._send_json({"error": f"未知平台: {platform}"}, 400)
            return
        if not keyword:
            self._send_json({"error": "缺少 keyword 参数"}, 400)
            return
        self._send_json({
            "platform": platform,
            "keyword": keyword,
            "dates": web_queries.list_ranking_dates(platform, keyword),
        })

    def _handle_snapshots(self, path):
        """GET /api/snapshots/<platform>?keyword=  → 快照列表
           GET /api/snapshots/<platform>/compare?keyword=&a=&b= → 对比结果"""
        parts = path.strip("/").split("/")
        if len(parts) < 3:
            self._send_json({"error": "bad path"}, 400)
            return
        platform = parts[2]
        qs = parse_qs(urlparse(self.path).query)
        keyword = (qs.get("keyword") or [""])[0].strip()
        if platform not in PLATFORMS:
            self._send_json({"error": f"未知平台: {platform}"}, 400)
            return
        if not keyword:
            self._send_json({"error": "缺少 keyword 参数"}, 400)
            return

        if len(parts) == 3:
            snaps = web_queries.list_snapshots(platform, keyword)
            self._send_json({
                "platform": platform,
                "platform_name": PLATFORMS[platform]["name"],
                "keyword": keyword,
                "snapshots": snaps,
            })
            return

        if len(parts) == 4 and parts[3] == "compare":
            a = qs.get("a", [""])[0]
            b = qs.get("b", [""])[0]
            try:
                task_a, task_b = int(a), int(b)
            except (TypeError, ValueError):
                self._send_json({"error": "a/b 需为任务 ID"}, 400)
                return
            result = web_queries.compare_snapshots(platform, keyword, task_a, task_b)
            if result is None:
                self._send_json({"error": "任务不存在或不属于该关键词"}, 404)
                return
            result["platform_name"] = PLATFORMS[platform]["name"]
            self._send_json(result)
            return

        self._send_json({"error": "not found"}, 404)

    # ---------- 静态文件 ----------
    def _serve_file(self, path, content_type):
        if not os.path.isfile(path):
            self._send_json({"error": "web/index.html 不存在"}, 404)
            return
        with open(path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # ---------- SSE 实时日志流 ----------
    def _stream_logs(self, task_id):
        with TASKS_LOCK:
            task = TASKS.get(task_id)
        if not task:
            self._send_json({"error": "task not found"}, 404)
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        def send(data, event=None):
            try:
                if event:
                    self.wfile.write(f"event: {event}\n".encode("utf-8"))
                self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return False
            return True

        offset = 0
        while True:
            with TASKS_LOCK:
                lines = list(task["lines"])
                status = task["status"]
            # 发送新行
            while offset < len(lines):
                if not send(lines[offset]):
                    return
                offset += 1
            # 任务结束且日志发完 → 发送 done 事件并关闭
            if status in ("finished", "failed", "killed") and offset >= len(lines):
                send(json.dumps({"status": status}, ensure_ascii=False), event="done")
                return
            time.sleep(0.3)


def main():
    parser = argparse.ArgumentParser(description="0PaChongBili Web 控制台")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址 (默认 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8900, help="监听端口 (默认 8900)")
    args = parser.parse_args()

    os.makedirs(WEB_DIR, exist_ok=True)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}"
    print(f"0PaChongBili Web 控制台已启动: {url}")
    print(f"爬虫根目录: {PROJECT_ROOT}")
    print("按 Ctrl+C 退出")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出")
        server.server_close()


if __name__ == "__main__":
    main()
