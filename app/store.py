# -*- coding: utf-8 -*-
"""数据层：作品台账 / 作者 / 补采清单 / 下载状态，全部落 SQLite。

台账是唯一真相来源：页面显示、断点续传、补采比对都以这里为准。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS authors (
    sec_uid      TEXT PRIMARY KEY,
    nickname     TEXT,
    unique_id    TEXT,
    avatar       TEXT,
    total_posts  INTEGER DEFAULT 0,
    last_fetch   INTEGER DEFAULT 0,
    created_at   INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS works (
    aweme_id     TEXT PRIMARY KEY,
    sec_uid      TEXT,
    author_name  TEXT,
    desc         TEXT,
    create_time  INTEGER,
    duration_ms  INTEGER,
    is_image     INTEGER DEFAULT 0,
    image_count  INTEGER DEFAULT 0,
    cover        TEXT,
    play_url     TEXT,
    status       TEXT DEFAULT 'pending',   -- pending / downloading / done / failed / missing
    saved_path   TEXT,
    file_size    INTEGER DEFAULT 0,
    error        TEXT,
    updated_at   INTEGER DEFAULT 0,
    extra        TEXT
);
CREATE INDEX IF NOT EXISTS idx_works_author ON works(sec_uid);
CREATE INDEX IF NOT EXISTS idx_works_status ON works(status);

CREATE TABLE IF NOT EXISTS reclaims (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    aweme_id     TEXT,
    sec_uid      TEXT,
    author_name  TEXT,
    desc         TEXT,
    play_url     TEXT,
    cover        TEXT,
    reason       TEXT,
    status       TEXT DEFAULT 'waiting',   -- waiting / queued / done / ignored
    created_at   INTEGER DEFAULT 0,
    updated_at   INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS settings (
    key          TEXT PRIMARY KEY,
    value        TEXT,
    updated_at   INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS fetch_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    sec_uid      TEXT,
    nickname     TEXT,
    fetched      INTEGER,
    added        INTEGER,
    started_at   INTEGER,
    ended_at     INTEGER,
    note         TEXT
);
"""


class Store:
    def __init__(self, db_path: str | Path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self):
        with self._lock:
            try:
                self.conn.close()
            except Exception:
                pass

    # ---------- 通用 ----------
    def query(self, sql: str, params: Iterable = ()) -> list[dict]:
        with self._lock:
            cur = self.conn.execute(sql, tuple(params))
            return [dict(r) for r in cur.fetchall()]

    def execute(self, sql: str, params: Iterable = ()):
        with self._lock:
            cur = self.conn.execute(sql, tuple(params))
            self.conn.commit()
            return cur

    # ---------- 作者 ----------
    def upsert_author(self, sec_uid: str, nickname: str = "", unique_id: str = "",
                      avatar: str = "", total: int | None = None):
        now = int(time.time())
        with self._lock:
            self.conn.execute(
                """INSERT INTO authors(sec_uid, nickname, unique_id, avatar, total_posts, created_at)
                   VALUES(?,?,?,?,?,?)
                   ON CONFLICT(sec_uid) DO UPDATE SET
                     nickname=COALESCE(NULLIF(excluded.nickname,''), authors.nickname),
                     unique_id=COALESCE(NULLIF(excluded.unique_id,''), authors.unique_id),
                     avatar=COALESCE(NULLIF(excluded.avatar,''), authors.avatar),
                     total_posts=COALESCE(excluded.total_posts, authors.total_posts)""",
                (sec_uid, nickname or "", unique_id or "", avatar or "", total or 0, now))
            self.conn.commit()

    def touch_author_fetch(self, sec_uid: str, total: int):
        self.execute("UPDATE authors SET last_fetch=?, total_posts=? WHERE sec_uid=?",
                     (int(time.time()), int(total), sec_uid))

    def list_authors(self) -> list[dict]:
        return self.query("SELECT * FROM authors ORDER BY last_fetch DESC")

    def delete_author(self, sec_uid: str, drop_works: bool = True) -> dict:
        """从「已入库 UP 主」里移除该作者。默认连带删掉它的作品台账行。

        注意：只动数据库，不删磁盘上的已下载文件——用户的文件不该被程序悄悄带走。
        """
        if not sec_uid:
            return {"ok": False, "works_removed": 0}
        with self._lock:
            works_removed = 0
            if drop_works:
                cur = self.conn.execute("DELETE FROM works WHERE sec_uid=?", (sec_uid,))
                works_removed = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
            self.conn.execute("DELETE FROM authors WHERE sec_uid=?", (sec_uid,))
            self.conn.commit()
        return {"ok": True, "works_removed": works_removed}

    # ---------- 作品 ----------
    def upsert_works(self, works: list[dict], sec_uid: str = "") -> dict:
        """写入作品台账，返回 {新增, 已有}。已下载的作品不会被重置状态。"""
        added = existing = 0
        now = int(time.time())
        with self._lock:
            for w in works:
                aweme_id = str(w.get("aweme_id") or "")
                if not aweme_id:
                    continue
                row = self.conn.execute("SELECT status, saved_path FROM works WHERE aweme_id=?",
                                        (aweme_id,)).fetchone()
                if row:
                    existing += 1
                    # 只补全可能过期的直链与文案，不动下载状态
                    self.conn.execute(
                        """UPDATE works SET desc=COALESCE(NULLIF(?,''), desc),
                           cover=COALESCE(NULLIF(?,''), cover),
                           play_url=COALESCE(NULLIF(?,''), play_url),
                           duration_ms=COALESCE(NULLIF(?,0), duration_ms),
                           updated_at=? WHERE aweme_id=?""",
                        (w.get("desc") or "", w.get("cover") or "", w.get("play_url") or "",
                         int(w.get("duration_ms") or 0), now, aweme_id))
                else:
                    added += 1
                    extra = {k: w.get(k) for k in ("play_urls", "image_urls", "music_url",
                                                  "music_title", "digg_count", "comment_count",
                                                  "share_count", "play_count")}
                    self.conn.execute(
                        """INSERT INTO works(aweme_id, sec_uid, author_name, desc, create_time,
                           duration_ms, is_image, image_count, cover, play_url, status, updated_at, extra)
                           VALUES(?,?,?,?,?,?,?,?,?,?, 'pending', ?, ?)""",
                        (aweme_id, w.get("author_uid") or sec_uid, w.get("author_name") or "",
                         w.get("desc") or "", int(w.get("create_time") or 0),
                         int(w.get("duration_ms") or 0), 1 if w.get("is_image") else 0,
                         int(w.get("image_count") or 0), w.get("cover") or "",
                         w.get("play_url") or "", now,
                         json.dumps(extra, ensure_ascii=False)))
            self.conn.commit()
        return {"added": added, "existing": existing}

    def delete_works(self, aweme_ids: list[str] | None = None, status: str = "",
                     sec_uid: str = "") -> int:
        """按 av 号 / 状态 / 作者删除台账行，返回删除条数。只删数据库，不动磁盘文件。"""
        with self._lock:
            if aweme_ids:
                ids = [str(x) for x in aweme_ids if x]
                if not ids:
                    return 0
                marks = ",".join("?" * len(ids))
                cur = self.conn.execute("DELETE FROM works WHERE aweme_id IN (%s)" % marks, ids)
            elif status:
                cur = self.conn.execute("DELETE FROM works WHERE status=?", (status,))
            elif sec_uid:
                cur = self.conn.execute("DELETE FROM works WHERE sec_uid=?", (sec_uid,))
            else:
                return 0
            n = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
            self.conn.commit()
            return n

    def set_status(self, aweme_id: str, status: str, saved_path: str = "",
                   size: int = 0, error: str = ""):
        self.execute(
            """UPDATE works SET status=?, saved_path=COALESCE(NULLIF(?,''), saved_path),
               file_size=?, error=?, updated_at=? WHERE aweme_id=?""",
            (status, saved_path or "", int(size or 0), error or "", int(time.time()), aweme_id))

    def get_work(self, aweme_id: str) -> dict | None:
        r = self.query("SELECT * FROM works WHERE aweme_id=?", (aweme_id,))
        return r[0] if r else None

    def list_works(self, sec_uid: str = "", status: str = "", keyword: str = "",
                   limit: int = 2000, offset: int = 0) -> list[dict]:
        sql = "SELECT * FROM works WHERE 1=1"
        p: list[Any] = []
        if sec_uid:
            sql += " AND sec_uid=?"
            p.append(sec_uid)
        if status:
            sql += " AND status=?"
            p.append(status)
        if keyword:
            sql += " AND (desc LIKE ? OR aweme_id LIKE ? OR author_name LIKE ?)"
            p.extend(["%" + keyword + "%"] * 3)
        sql += " ORDER BY create_time DESC LIMIT ? OFFSET ?"
        p.extend([int(limit), int(offset)])
        return self.query(sql, p)

    def count_works(self, sec_uid: str = "") -> dict:
        base = "SELECT status, COUNT(*) n FROM works"
        if sec_uid:
            base += " WHERE sec_uid=?"
            rows = self.query(base + " GROUP BY status", (sec_uid,))
        else:
            rows = self.query(base + " GROUP BY status")
        total = sum(r["n"] for r in rows)
        out = {"total": total, "done": 0, "failed": 0, "pending": 0, "missing": 0, "downloading": 0}
        for r in rows:
            if r["status"] in out:
                out[r["status"]] = r["n"]
        return out

    def pending_ids(self, sec_uid: str = "", statuses=("pending", "failed")) -> list[str]:
        sql = "SELECT aweme_id FROM works WHERE status IN (%s)" % ",".join("?" * len(statuses))
        p: list[Any] = list(statuses)
        if sec_uid:
            sql += " AND sec_uid=?"
            p.append(sec_uid)
        return [r["aweme_id"] for r in self.query(sql, p)]

    # ---------- 补采 ----------
    def add_reclaims(self, items: list[dict]) -> int:
        """把缺失项加入补采清单；同一 aweme_id 已在清单里则跳过。"""
        now = int(time.time())
        added = 0
        with self._lock:
            for it in items:
                exists = self.conn.execute(
                    "SELECT id FROM reclaims WHERE aweme_id=? AND status IN ('waiting','queued')",
                    (it.get("aweme_id"),)).fetchone()
                if exists:
                    continue
                self.conn.execute(
                    """INSERT INTO reclaims(aweme_id, sec_uid, author_name, desc, play_url, cover,
                       reason, status, created_at, updated_at) VALUES(?,?,?,?,?,?,?,'waiting',?,?)""",
                    (it.get("aweme_id") or "", it.get("sec_uid") or "", it.get("author_name") or "",
                     it.get("desc") or "", it.get("play_url") or "", it.get("cover") or "",
                     it.get("reason") or "", now, now))
                added += 1
            self.conn.commit()
        return added

    def list_reclaims(self, status: str = "") -> list[dict]:
        if status:
            return self.query("SELECT * FROM reclaims WHERE status=? ORDER BY id DESC", (status,))
        return self.query("SELECT * FROM reclaims ORDER BY id DESC")

    def mark_reclaim(self, rid: int, status: str):
        self.execute("UPDATE reclaims SET status=?, updated_at=? WHERE id=?",
                     (status, int(time.time()), rid))

    def clear_reclaims(self, ids: list[int] | None = None):
        with self._lock:
            if ids:
                self.conn.executemany("DELETE FROM reclaims WHERE id=?", [(i,) for i in ids])
            else:
                self.conn.execute("DELETE FROM reclaims")
            self.conn.commit()

    # ---------- 设置 ----------
    def set_setting(self, key: str, value: str):
        self.execute("INSERT INTO settings(key,value,updated_at) VALUES(?,?,?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                     (key, str(value), int(time.time())))

    def get_setting(self, key: str, default: str = "") -> str:
        r = self.query("SELECT value FROM settings WHERE key=?", (key,))
        return r[0]["value"] if r else default

    # ---------- 抓取日志 ----------
    def log_fetch(self, sec_uid: str, nickname: str, fetched: int, added: int,
                  started: int, note: str = ""):
        self.execute(
            "INSERT INTO fetch_log(sec_uid,nickname,fetched,added,started_at,ended_at,note) "
            "VALUES(?,?,?,?,?,?,?)",
            (sec_uid, nickname, int(fetched), int(added), int(started), int(time.time()), note))
