# -*- coding: utf-8 -*-
"""下载层：并发下载、断点续传、重试、按 UP 主分文件夹落盘。

设计要点：
1. 落盘目录 = <保存根>/<作者昵称>/，一个 UP 主一个文件夹（用户明确要求）。
2. 用 .part 临时文件 + Range 续传，避免大文件下到一半断掉后重下。
3. 状态写回 SQLite 台账，页面与补采清单都读同一份真相。
"""
from __future__ import annotations

import os
import queue
import re
import threading
import time
from pathlib import Path
from typing import Callable

import requests

from .douyin import UA
from .store import Store

INVALID = re.compile(r'[\\/:*?"<>|\r\n\t]')
CTRL = re.compile(r"[\x00-\x1f\x7f]")
MULTI_SPACE = re.compile(r"\s+")
MAX_NAME = 60


def clean_name(s: str, fallback: str = "未命名") -> str:
    s = CTRL.sub("", s or "")
    s = INVALID.sub("_", s)
    s = MULTI_SPACE.sub(" ", s).strip()
    s = s.strip("._ ")
    if not s:
        s = fallback
    if len(s) > MAX_NAME:
        s = s[:MAX_NAME].rstrip()
    return s


def date_str(ts: int) -> str:
    if not ts:
        return "00000000"
    return time.strftime("%Y%m%d", time.localtime(ts))


def build_filename(work: dict, ext: str = ".mp4") -> str:
    desc = clean_name(work.get("desc") or "", fallback="无文案")
    d = date_str(int(work.get("create_time") or 0))
    return "%s_%s_%s%s" % (d, desc, work.get("aweme_id"), ext)


class Downloader:
    """一个下载器实例管理一个队列 + 若干 worker 线程。"""

    def __init__(self, store: Store, root_dir: str | Path, concurrency: int = 3,
                 log: Callable[[str], None] | None = None,
                 on_change: Callable[[], None] | None = None):
        self.store = store
        self.root = Path(root_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        self.concurrency = max(1, int(concurrency))
        self.log = log or (lambda s: None)
        self.on_change = on_change or (lambda: None)
        self.q: queue.Queue[str | None] = queue.Queue()
        self._workers: list[threading.Thread] = []
        self._running = False
        self._stop_flag = threading.Event()
        self._inflight: set[str] = set()
        self._lock = threading.Lock()
        self.stats = {"queued": 0, "done": 0, "failed": 0}

    # ---------- 队列 ----------
    def add(self, aweme_ids: list[str]):
        for aid in aweme_ids:
            self.q.put((aid, 0))
        with self._lock:
            self.stats["queued"] += len(aweme_ids)
        self.log("已入队 %d 条" % len(aweme_ids))

    def queue_size(self) -> int:
        return self.q.qsize()

    def start(self):
        if self._running:
            return
        self._running = True
        self._stop_flag.clear()
        for i in range(self.concurrency):
            t = threading.Thread(target=self._worker, name="dl-%d" % i, daemon=True)
            t.start()
            self._workers.append(t)
        self.log("下载器启动，并发 %d" % self.concurrency)

    def stop(self):
        self._stop_flag.set()
        for _ in self._workers:
            self.q.put(None)
        self._running = False
        self._workers.clear()

    # ---------- worker ----------
    def _worker(self):
        while not self._stop_flag.is_set():
            item = self.q.get()
            if item is None:
                break
            aid, attempt = item
            with self._lock:
                if aid in self._inflight:
                    continue
                self._inflight.add(aid)
            try:
                self._handle(aid, attempt)
            except Exception as e:
                self.log("下载异常 %s: %s" % (aid, e))
            finally:
                with self._lock:
                    self._inflight.discard(aid)
                self.q.task_done()

    def _handle(self, aweme_id: str, attempt: int):
        w = self.store.get_work(aweme_id)
        if not w:
            return
        if w.get("status") == "done" and w.get("saved_path") and Path(w["saved_path"]).exists():
            return
        self.store.set_status(aweme_id, "downloading")
        self.on_change()
        try:
            extra = w.get("extra")
            import json as _json
            try:
                ex = _json.loads(extra) if extra else {}
            except Exception:
                ex = {}
            author = clean_name(w.get("author_name") or "", fallback=(w.get("sec_uid") or "")[-10:])
            folder = self.root / author
            folder.mkdir(parents=True, exist_ok=True)

            if w.get("is_image"):
                urls = ex.get("image_urls") or []
                if not urls:
                    raise RuntimeError("图集缺少图片地址")
                sub = folder / clean_name(build_filename(w, ""), fallback=aweme_id)
                sub.mkdir(parents=True, exist_ok=True)
                total = 0
                for i, u in enumerate(urls, 1):
                    ext = os.path.splitext(u.split("?")[0])[1] or ".jpg"
                    p = sub / ("%03d%s" % (i, ext))
                    total += self._fetch(u, p, referer="https://www.douyin.com/")
                self.store.set_status(aweme_id, "done", saved_path=str(sub), size=total)
            else:
                url = w.get("play_url") or ""
                if not url:
                    raise RuntimeError("缺少无水印播放地址")
                p = folder / build_filename(w, ".mp4")
                size = self._fetch(url, p, referer="https://www.douyin.com/")
                if size <= 0:
                    raise RuntimeError("下载得到 0 字节")
                self.store.set_status(aweme_id, "done", saved_path=str(p), size=size)
            with self._lock:
                self.stats["done"] += 1
            self.log("完成 %s" % aweme_id)
        except Exception as e:
            err = str(e)[:200]
            if attempt < 2:
                self.log("重试 %s (%d/3): %s" % (aweme_id, attempt + 1, err))
                time.sleep(2 + attempt * 2)
                self.q.put((aweme_id, attempt + 1))
                self.store.set_status(aweme_id, "pending", error=err)
            else:
                self.store.set_status(aweme_id, "failed", error=err)
                with self._lock:
                    self.stats["failed"] += 1
                self.log("失败 %s: %s" % (aweme_id, err))
        self.on_change()

    def _fetch(self, url: str, target: Path, referer: str, tries: int = 3) -> int:
        """带 Range 断点续传的单文件下载，返回最终字节数。"""
        part = target.with_suffix(target.suffix + ".part")
        existing = part.stat().st_size if part.exists() else 0
        headers = {
            "User-Agent": UA,
            "Referer": referer,
            "Accept": "*/*",
        }
        last_err = None
        for t in range(tries):
            try:
                if existing:
                    headers["Range"] = "bytes=%d-" % existing
                with requests.get(url, headers=headers, stream=True, timeout=30, allow_redirects=True) as r:
                    if r.status_code == 416:  # 已经下完
                        part.replace(target)
                        return target.stat().st_size
                    if r.status_code >= 400:
                        raise RuntimeError("HTTP %d" % r.status_code)
                    mode = "ab" if (existing and r.status_code == 206) else "wb"
                    if mode == "wb":
                        existing = 0
                    with open(part, mode) as f:
                        for chunk in r.iter_content(chunk_size=1024 * 256):
                            if self._stop_flag.is_set():
                                raise RuntimeError("已停止")
                            if chunk:
                                f.write(chunk)
                                existing += len(chunk)
                    if target.exists():
                        target.unlink()
                    part.replace(target)
                    return target.stat().st_size
            except Exception as e:
                last_err = e
                time.sleep(1.5 + t * 1.5)
        raise RuntimeError("下载失败: %s" % last_err)

    # ---------- 补采比对 ----------
    def verify_and_reclaim(self, sec_uid: str = "") -> dict:
        """比对台账里应为 done 的作品与磁盘实际文件，缺失的加入补采清单。"""
        rows = self.store.list_works(sec_uid=sec_uid, limit=100000)
        missing = []
        for w in rows:
            if w.get("status") != "done":
                continue
            p = w.get("saved_path") or ""
            if not p or not Path(p).exists():
                missing.append(w)
            elif Path(p).is_file() and Path(p).stat().st_size == 0:
                missing.append(w)
        if missing:
            self.store.add_reclaims([{
                "aweme_id": w["aweme_id"], "sec_uid": w.get("sec_uid") or "",
                "author_name": w.get("author_name") or "", "desc": w.get("desc") or "",
                "play_url": w.get("play_url") or "", "cover": w.get("cover") or "",
                "reason": "磁盘上找不到文件",
            } for w in missing])
        self.log("比对完成：应完成 %d 条，缺失 %d 条" % (
            sum(1 for r in rows if r.get("status") == "done"), len(missing)))
        return {"checked": len(rows), "missing": len(missing)}
