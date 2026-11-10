# -*- coding: utf-8 -*-
"""本地 HTTP 服务：把抓取、下载、台账、补采全部暴露成 JSON 接口 + 一个网页界面。

只用标准库 http.server，不引入 fastapi/uvicorn，分发时少一堆依赖。
浏览器操作全部串行到一把锁上：playwright 的 persistent context 不能并发开两个。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import traceback
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import requests

from .douyin import UA, DouyinClient, extract_sec_uid
from .downloader import Downloader
from .store import Store

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
DATA = ROOT / "data"
DB_PATH = DATA / "douyin.db"


class App:
    def __init__(self):
        DATA.mkdir(parents=True, exist_ok=True)
        self.store = Store(DB_PATH)
        self.logs: deque = deque(maxlen=300)
        self._log_lock = threading.Lock()
        self.browser_lock = threading.RLock()
        self.client: DouyinClient | None = None
        self._logged_cache: bool | None = None
        self.fetch_state = {"running": False, "target": "", "sec_uid": "", "nickname": "",
                            "count": 0, "added": 0, "error": "", "started": 0}
        self.login_state = {"running": False, "ok": False, "nickname": "", "message": "未登录"}
        root_setting = self.store.get_setting("download_root", "")
        self.download_root = Path(root_setting) if root_setting else (ROOT / "download")
        self.downloader = Downloader(self.store, self.download_root,
                                     concurrency=int(self.store.get_setting("concurrency", "3") or 3),
                                     log=self.log, on_change=lambda: None)

    # ---------- 日志 ----------
    def log(self, s: str):
        line = "%s  %s" % (time.strftime("%H:%M:%S"), s)
        with self._log_lock:
            self.logs.append(line)

    def tail_logs(self, n: int = 60) -> list[str]:
        with self._log_lock:
            return list(self.logs)[-n:]

    # ---------- 浏览器 ----------
    def ensure_client(self, headless: bool = True) -> DouyinClient:
        with self.browser_lock:
            if self.client is not None:
                return self.client
            c = DouyinClient(DATA, headless=headless, log=self.log)
            c.open()
            self.client = c
            return c

    def drop_client(self):
        with self.browser_lock:
            if self.client:
                try:
                    self.client.close()
                except Exception:
                    pass
                self.client = None

    def is_logged(self) -> bool:
        """登录态读文件即可，绝不拿 browser_lock。

        抓取时 browser_lock 会被持有好几分钟，如果这里也去抢锁，
        界面每 2.5 秒一次的状态轮询会全部阻塞，表现为整个页面卡死。
        """
        if self._logged_cache is not None:
            return self._logged_cache
        f = DATA / "login_user.json"
        ok = False
        if f.exists():
            try:
                ok = bool(json.loads(f.read_text(encoding="utf-8")).get("ok"))
            except Exception:
                ok = False
        self._logged_cache = ok
        return ok

    def start_login(self, timeout: int = 300):
        if self.login_state.get("running"):
            return False

        def _run():
            self.login_state = {"running": True, "ok": False, "nickname": "", "message": "等待扫码…"}
            self.drop_client()
            try:
                c = DouyinClient(DATA, headless=False, log=self.log)
                c.open()
                with self.browser_lock:
                    self.client = c
                r = c.interactive_login(timeout_sec=timeout)
                self._logged_cache = bool(r.get("ok"))
                self.login_state = {
                    "running": False, "ok": bool(r.get("ok")),
                    "nickname": r.get("nickname") or "",
                    "message": ("登录成功：%s" % (r.get("nickname") or "")) if r.get("ok") else "登录未完成（超时）",
                }
                self.log(self.login_state["message"])
            except Exception as e:
                self.login_state = {"running": False, "ok": False, "nickname": "",
                                    "message": "登录异常: %s" % str(e)[:120]}
                self.log("登录异常: %s" % traceback.format_exc()[:400])

        threading.Thread(target=_run, daemon=True).start()
        return True

    def start_fetch(self, target: str, max_items: int = 0):
        if self.fetch_state.get("running"):
            return False
        self.fetch_state = {"running": True, "target": target, "sec_uid": extract_sec_uid(target) or "",
                            "nickname": "", "count": 0, "added": 0, "error": "", "started": int(time.time())}

        def _run():
            try:
                with self.browser_lock:
                    c = self.ensure_client(headless=True)
                    info = c.get_self_info()
                    works = c.fetch_user_works(
                        target, max_items=max_items,
                        on_progress=lambda n: self.fetch_state.update({"count": n}))
                sec_uid = (works[0]["author_uid"] if works else "") or self.fetch_state["sec_uid"]
                nickname = (works[0]["author_name"] if works else "") or info.get("nickname", "")
                self.fetch_state.update({"sec_uid": sec_uid, "nickname": nickname, "count": len(works)})
                if sec_uid:
                    self.store.upsert_author(sec_uid, nickname=nickname,
                                             avatar=(works[0].get("cover") if works else ""))
                res = self.store.upsert_works(works, sec_uid=sec_uid)
                self.store.touch_author_fetch(sec_uid, len(works))
                self.store.log_fetch(sec_uid, nickname, len(works), res["added"], self.fetch_state["started"])
                self.fetch_state.update({"running": False, "added": res["added"]})
                self.log("抓取完成：%s 共 %d 条，新增 %d 条" % (nickname or target, len(works), res["added"]))
            except Exception as e:
                self.fetch_state.update({"running": False, "error": str(e)[:200]})
                self.log("抓取失败: %s" % traceback.format_exc()[:600])

        threading.Thread(target=_run, daemon=True).start()
        return True

    # ---------- 下载 ----------
    def start_download(self, aweme_ids: list[str] | None = None, sec_uid: str = ""):
        ids = aweme_ids or self.store.pending_ids(sec_uid=sec_uid)
        if not ids:
            self.log("没有需要下载的作品")
            return 0
        self.downloader.start()
        self.downloader.add(ids)
        return len(ids)

    def stop_download(self):
        self.downloader.stop()
        self.log("下载已停止")

    def settings_to_dict(self) -> dict:
        return {
            "download_root": str(self.download_root),
            "concurrency": self.downloader.concurrency,
            "db_path": str(DB_PATH),
        }


APP = App()


class Handler(BaseHTTPRequestHandler):
    server_version = "DouyinDown/1.0"

    # ---------- 基础 ----------
    def log_message(self, fmt, *args):
        pass

    def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _json(self, obj, code: int = 200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    # ---------- GET ----------
    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        p = u.path
        try:
            if p in ("/", "/index.html"):
                f = WEB / "index.html"
                if not f.exists():
                    return self._send(500, b"web/index.html missing", "text/plain; charset=utf-8")
                return self._send(200, f.read_bytes(), "text/html; charset=utf-8")
            if p.startswith("/static/"):
                f = WEB / p.lstrip("/")
                if not f.exists():
                    return self._send(404, b"not found", "text/plain")
                ctype = {".css": "text/css", ".js": "application/javascript",
                         ".png": "image/png", ".jpg": "image/jpeg", ".ico": "image/x-icon"}.get(
                    f.suffix.lower(), "application/octet-stream")
                return self._send(200, f.read_bytes(), ctype)

            if p == "/api/status":
                return self._json({
                    "logged": APP.is_logged(),
                    "login": APP.login_state,
                    "fetch": APP.fetch_state,
                    "download": {"queue": APP.downloader.queue_size(), "stats": APP.downloader.stats,
                                 "running": APP.downloader._running},
                    "counts": APP.store.count_works(),
                    "authors": len(APP.store.list_authors()),
                    "settings": APP.settings_to_dict(),
                    "server_time": time.strftime("%H:%M:%S"),
                })

            if p == "/api/logs":
                return self._json({"lines": APP.tail_logs(int(q.get("n", ["60"])[0]))})

            if p == "/api/authors":
                return self._json({"items": APP.store.list_authors()})

            if p == "/api/works":
                sec = q.get("sec_uid", [""])[0]
                st = q.get("status", [""])[0]
                kw = q.get("keyword", [""])[0]
                lim = int(q.get("limit", ["500"])[0])
                off = int(q.get("offset", ["0"])[0])
                items = APP.store.list_works(sec_uid=sec, status=st, keyword=kw, limit=lim, offset=off)
                return self._json({"items": items, "counts": APP.store.count_works(sec), "total": len(items)})

            if p == "/api/reclaims":
                st = q.get("status", [""])[0]
                return self._json({"items": APP.store.list_reclaims(st)})

            if p == "/api/cover":
                # parse_qs 已经解码过一次，这里不能再 unquote，
                # 否则 URL 里的签名参数会被二次解码破坏，抖音返回 404。
                url = q.get("url", [""])[0]
                if not url:
                    return self._send(404, b"no url", "text/plain")
                try:
                    r = requests.get(url, headers={"User-Agent": UA, "Referer": "https://www.douyin.com/"},
                                     timeout=20)
                    if r.status_code != 200:
                        return self._send(404, b"cover fetch failed", "text/plain")
                    ctype = r.headers.get("Content-Type", "image/jpeg")
                    return self._send(200, r.content, ctype)
                except Exception:
                    return self._send(404, b"cover error", "text/plain")

            return self._send(404, b"not found", "text/plain")
        except Exception:
            APP.log("GET %s 异常: %s" % (p, traceback.format_exc()[:500]))
            return self._json({"error": "server error"}, 500)

    # ---------- POST ----------
    def do_POST(self):
        u = urlparse(self.path)
        p = u.path
        body = self._body()
        try:
            if p == "/api/login":
                ok = APP.start_login(int(body.get("timeout", 300)))
                return self._json({"started": ok})

            if p == "/api/fetch":
                target = (body.get("target") or "").strip()
                if not target:
                    return self._json({"error": "缺少主页链接或 sec_uid"}, 400)
                ok = APP.start_fetch(target, int(body.get("max_items", 0) or 0))
                return self._json({"started": ok})

            if p == "/api/download":
                ids = body.get("aweme_ids") or []
                n = APP.start_download(ids or None, sec_uid=body.get("sec_uid", ""))
                return self._json({"queued": n})

            if p == "/api/download/stop":
                APP.stop_download()
                return self._json({"ok": True})

            if p == "/api/verify":
                r = APP.downloader.verify_and_reclaim(body.get("sec_uid", ""))
                return self._json(r)

            if p == "/api/reclaims/clear":
                ids = body.get("ids")
                APP.store.clear_reclaims(ids)
                return self._json({"ok": True})

            if p == "/api/reclaims/push":
                rows = APP.store.list_reclaims(status="waiting")
                if not rows:
                    return self._json({"queued": 0})
                ids = [r["aweme_id"] for r in rows if r["aweme_id"]]
                n = APP.start_download(ids)
                for r in rows:
                    APP.store.mark_reclaim(r["id"], "queued")
                return self._json({"queued": n})

            if p == "/api/settings":
                root = body.get("download_root")
                conc = body.get("concurrency")
                if root:
                    APP.download_root = Path(root)
                    APP.downloader.root = APP.download_root
                    APP.download_root.mkdir(parents=True, exist_ok=True)
                    APP.store.set_setting("download_root", str(root))
                if conc:
                    c = max(1, min(8, int(conc)))
                    APP.downloader.concurrency = c
                    APP.store.set_setting("concurrency", str(c))
                return self._json(APP.settings_to_dict())

            if p == "/api/works/delete":  # 清空某作者台账（不删文件）
                sec = body.get("sec_uid", "")
                if sec:
                    APP.store.execute("DELETE FROM works WHERE sec_uid=?", (sec,))
                return self._json({"ok": True})

            return self._send(404, b"not found", "text/plain")
        except Exception:
            APP.log("POST %s 异常: %s" % (p, traceback.format_exc()[:500]))
            return self._json({"error": "server error"}, 500)


def main(port: int = 8765, host: str = "127.0.0.1"):
    httpd = ThreadingHTTPServer((host, port), Handler)
    APP.log("服务已启动 http://%s:%d" % (host, port))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        APP.drop_client()


if __name__ == "__main__":
    main()
