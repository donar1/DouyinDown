# -*- coding: utf-8 -*-
"""抖音抓取层：真实浏览器发请求，本模块只拦响应，不参与任何签名计算。

设计要点：
1. 用 launch_persistent_context 保存登录态到 data/browser，扫码一次即可长期复用。
2. 作品列表来自拦到的 `aweme/v1/web/aweme/post` 响应，滚动页面触发分页。
3. 游客态抖音只放少量作品，必须登录才能翻页拿全量。
"""
from __future__ import annotations

import json
import re
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Callable

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

POST_API = "aweme/v1/web/aweme/post"
PROFILE_API = "aweme/v1/web/user/profile/other"

SEC_UID_RE = re.compile(r"(MS4wLjAB[A-Za-z0-9_\-]+)")
USER_URL_RE = re.compile(r"douyin\.com/user/([A-Za-z0-9_\-]+)")

# 抖音主页的瀑布流不在 window 上滚动，必须把所有可滚动容器的 scrollTop 打到底，
# 单靠 mouse.wheel 增量滚动只会停在第二页（实测游客 18 条、登录 36 条就卡住）。
SCROLL_TO_BOTTOM = """() => {
    const els = [document.scrollingElement, ...document.querySelectorAll('*')];
    for (const e of els) {
        if (e && e.scrollHeight > e.clientHeight + 200) {
            e.scrollTop = e.scrollHeight - e.clientHeight;
        }
    }
    window.scrollTo(0, document.documentElement.scrollHeight);
}"""


def extract_sec_uid(text: str) -> str | None:
    """从主页链接 / 分享短链文本 / 纯 sec_uid 里提取 sec_uid。"""
    if not text:
        return None
    text = text.strip()
    m = USER_URL_RE.search(text)
    if m:
        return m.group(1)
    m = SEC_UID_RE.search(text)
    if m:
        return m.group(1)
    return None


def _first_url(obj: Any) -> str:
    if isinstance(obj, dict):
        urls = obj.get("url_list") or []
        if urls:
            return urls[0]
    return ""


def _best_play_url(video: dict) -> str:
    """优先取 h264 直链，其次 play_addr；都是无水印地址。"""
    for key in ("play_addr_h264", "play_addr"):
        u = _first_url(video.get(key) or {})
        if u:
            return u
    return ""


def _bitrates(video: dict) -> list[dict]:
    out = []
    for br in (video.get("bit_rate") or []):
        pa = br.get("play_addr") or {}
        u = _first_url(pa)
        if not u:
            continue
        out.append({
            "quality": br.get("gear_name") or "",
            "bit_rate": br.get("bit_rate") or 0,
            "width": pa.get("width") or br.get("width") or 0,
            "height": pa.get("height") or br.get("height") or 0,
            "data_size": pa.get("data_size") or 0,
            "url": u,
        })
    out.sort(key=lambda x: (x["bit_rate"], x["data_size"]), reverse=True)
    return out


def to_work(a: dict, author: dict | None = None) -> dict:
    """把 aweme 原始对象转成项目统一的作品结构。"""
    v = a.get("video") or {}
    images = a.get("images") or []
    stat = a.get("statistics") or {}
    music = (a.get("music") or {})
    author = author or (a.get("author") or {})
    cover = _first_url(v.get("cover") or {})
    if not cover and images:
        cover = _first_url(images[0])
    return {
        "aweme_id": str(a.get("aweme_id") or ""),
        "desc": (a.get("desc") or "").strip(),
        "create_time": int(a.get("create_time") or 0),
        "duration_ms": int(v.get("duration") or 0),
        "is_image": bool(images),
        "image_count": len(images),
        "cover": cover,
        "play_url": _best_play_url(v),
        "play_urls": _bitrates(v),
        "image_urls": [_first_url(img) or (img.get("url_list") or [""])[0] for img in images],
        "music_url": _first_url(music.get("play_url") or {}),
        "music_title": music.get("title") or "",
        "digg_count": stat.get("digg_count") or 0,
        "comment_count": stat.get("comment_count") or 0,
        "share_count": stat.get("share_count") or 0,
        "play_count": stat.get("play_count") or 0,
        "author_uid": (author.get("sec_uid") or ""),
        "author_name": (author.get("nickname") or ""),
        "author_unique_id": (author.get("unique_id") or ""),
        "raw": a,
    }


class DouyinClient:
    """一个抓取会话。线程安全用法：一个实例只在一个线程内使用。"""

    def __init__(self, data_dir: str | Path, headless: bool = True, log: Callable[[str], None] | None = None):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.profile_dir = self.data_dir / "browser"
        self.headless = headless
        self.log = log or (lambda s: None)
        self._pw = None
        self.ctx = None
        self.page = None
        self._works: dict[str, dict] = {}
        self._last_has_more = 1
        self._profile: dict = {}
        self._pages_cb: Callable[[dict], None] | None = None
        self._done_flag = threading.Event()

    # ---------- 生命周期 ----------
    def open(self, headless: bool | None = None):
        from playwright.sync_api import sync_playwright
        if headless is not None:
            self.headless = headless
        self._pw = sync_playwright().start()
        self.ctx = self._pw.chromium.launch_persistent_context(
            user_data_dir=str(self.profile_dir),
            channel="msedge",
            headless=self.headless,
            user_agent=UA,
            locale="zh-CN",
            viewport={"width": 1440, "height": 900},
            args=["--disable-blink-features=AutomationControlled",
                  "--no-first-run", "--no-default-browser-check"],
        )
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        self.page.on("response", self._on_response)
        self.log("浏览器已启动 (headless=%s)" % self.headless)
        return self

    def close(self):
        try:
            if self.ctx:
                self.ctx.close()
        except Exception:
            pass
        try:
            if self._pw:
                self._pw.stop()
        except Exception:
            pass
        self.ctx = None
        self.page = None
        self._pw = None

    def __enter__(self):
        return self.open()

    def __exit__(self, *a):
        self.close()

    # ---------- 拦包 ----------
    def _on_response(self, r):
        url = r.url
        if POST_API in url:
            try:
                body = r.body()
                data = json.loads(body.decode("utf-8"))
            except Exception:
                return
            for a in (data.get("aweme_list") or []):
                w = to_work(a)
                if w["aweme_id"]:
                    self._works.setdefault(w["aweme_id"], w)
            try:
                hm = data.get("has_more")
                if hm is not None:
                    self._last_has_more = int(hm)
            except Exception:
                pass
            if self._pages_cb:
                try:
                    self._pages_cb(data)
                except Exception:
                    pass
        elif PROFILE_API in url:
            try:
                self._profile = json.loads(r.body().decode("utf-8"))
            except Exception:
                pass

    # ---------- 登录 ----------
    def is_logged_in(self) -> bool:
        """用登录态 cookie 判断，避免误认游客 cookie。"""
        return self._login_signals()

    def interactive_login(self, open_page: bool = True, timeout_sec: int = 240) -> dict:
        """打开可见窗口让用户扫码，轮询检测登录状态（严格认 sessionid 系列 cookie）。"""
        if not self.ctx:
            self.open(headless=False)
        if self.headless:
            self.close()
            self.open(headless=False)
        try:
            self.page.goto("https://www.douyin.com/", wait_until="domcontentloaded", timeout=60000)
        except Exception:
            pass
        state_file = self.data_dir / "login_state.txt"
        t0 = time.time()
        result = {"ok": False, "nickname": "", "elapsed": 0}
        while time.time() - t0 < timeout_sec:
            ok = False
            try:
                ok = self._login_signals()
            except Exception as e:
                self.log("登录检查异常: %s" % e)
            try:
                state_file.write_text(
                    "elapsed=%d ok=%r at=%s" % (int(time.time() - t0), ok, time.strftime("%H:%M:%S")),
                    encoding="utf-8")
            except Exception:
                pass
            if ok:
                result["ok"] = True
                break
            time.sleep(2.5)
        result["elapsed"] = int(time.time() - t0)
        if result["ok"]:
            # 登录成功后再多等一会儿，让 cookie 落盘
            time.sleep(3)
            try:
                result["nickname"] = self._current_nickname()
            except Exception:
                pass
            try:
                (self.data_dir / "login_user.json").write_text(
                    json.dumps(result, ensure_ascii=False), encoding="utf-8")
            except Exception:
                pass
        return result

    def _login_signals(self) -> bool:
        """只看抖音的登录态 cookie，游客的 ttwid/msToken 不算。"""
        names = ("sessionid", "sessionid_ss", "sid_tt", "sid_guard")
        try:
            cookies = {c["name"]: c["value"] for c in self.ctx.cookies(["https://www.douyin.com"])}
        except Exception:
            cookies = {}
        if not any(cookies.get(n) for n in names):
            # 新建 context 时按域过滤可能读不到持久 cookie，回退到全量读取
            try:
                cookies = {c["name"]: c["value"] for c in self.ctx.cookies()}
            except Exception:
                return False
        return bool(any(cookies.get(n) for n in names))

    def _current_nickname(self) -> str:
        try:
            self.page.goto("https://www.douyin.com/user/self", wait_until="domcontentloaded", timeout=60000)
            time.sleep(4)
            return self.page.inner_text("h1", timeout=8000).strip()
        except Exception:
            return ""

    # ---------- 取作品 ----------
    def fetch_user_works(self, target: str, max_items: int = 0,
                         max_idle_rounds: int = 8, on_progress: Callable[[int], None] | None = None) -> list[dict]:
        """打开主页并滚动翻页，返回全部拦到的作品。max_items=0 表示不限。"""
        sec_uid = extract_sec_uid(target)
        if not sec_uid:
            # 可能是 v.douyin.com 短链，交给浏览器跳转解析
            try:
                self.page.goto(target.strip(), wait_until="domcontentloaded", timeout=60000)
                time.sleep(5)
                sec_uid = extract_sec_uid(self.page.url)
            except Exception as e:
                self.log("短链解析失败: %s" % e)
        if not sec_uid:
            raise ValueError("无法从输入中解析出用户 sec_uid: %s" % target)

        self._works.clear()
        self._last_has_more = 1
        url = "https://www.douyin.com/user/%s" % sec_uid
        self.log("打开主页: " + url)
        try:
            self.page.goto(url, wait_until="domcontentloaded", timeout=90000)
        except Exception as e:
            self.log("主页打开异常: %s" % e)
        time.sleep(8)

        deadline = time.time() + 1800
        idle = 0
        last_count = 0
        while time.time() < deadline:
            n = len(self._works)
            if on_progress:
                try:
                    on_progress(n)
                except Exception:
                    pass
            if max_items and n >= max_items:
                self.log("达到上限 %d 条，停止" % max_items)
                break
            if self._last_has_more == 0 and n > 0:
                self.log("接口已返回 has_more=0，确认到底（共 %d 条）" % n)
                break
            if n == last_count:
                idle += 1
            else:
                idle = 0
            last_count = n
            if idle >= max_idle_rounds and n > 0:
                self.log("连续 %d 轮无新增，停止翻页（共 %d 条）" % (idle, n))
                break
            try:
                self.page.evaluate(SCROLL_TO_BOTTOM)
            except Exception:
                pass
            try:
                self.page.mouse.wheel(0, 4000)
            except Exception:
                pass
            time.sleep(4)

        works = list(self._works.values())
        works.sort(key=lambda w: w["create_time"], reverse=True)
        return works

    def fetch_one(self, aweme_id: str) -> dict | None:
        """抓单个作品详情（含无水印地址）。"""
        self._works = {}
        try:
            self.page.goto("https://www.douyin.com/video/%s" % aweme_id,
                           wait_until="domcontentloaded", timeout=60000)
            time.sleep(6)
        except Exception:
            return None
        return self._works.get(aweme_id)

    def get_self_info(self) -> dict:
        info = {"sec_uid": "", "nickname": "", "unique_id": "", "avatar": ""}
        try:
            self.page.goto("https://www.douyin.com/user/self", wait_until="domcontentloaded", timeout=60000)
            time.sleep(5)
            info["nickname"] = self.page.inner_text("h1", timeout=8000).strip()
        except Exception:
            pass
        try:
            p = getattr(self, "_profile", None) or {}
            u = (p.get("user") or {})
            info["sec_uid"] = u.get("sec_uid") or info["sec_uid"]
            info["nickname"] = u.get("nickname") or info["nickname"]
            info["unique_id"] = u.get("unique_id") or ""
        except Exception:
            pass
        return info
