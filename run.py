# -*- coding: utf-8 -*-
"""入口：起本地服务并打开界面。双击「启动.bat」等同于执行本文件。"""
import os
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def find_port(start: int = 8765, tries: int = 30) -> int:
    for p in range(start, start + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.3)
            if s.connect_ex(("127.0.0.1", p)) != 0:
                return p
    return start


def main():
    from app.server import main as serve

    port = find_port(int(os.environ.get("DOUYIN_PORT", "8765")))
    url = "http://127.0.0.1:%d/" % port

    def _open():
        time.sleep(1.6)
        try:
            webbrowser.open(url)
        except Exception:
            pass

    threading.Thread(target=_open, daemon=True).start()
    print("DouyinDown 已启动：%s" % url)
    print("关闭本窗口即停止服务。")
    serve(port=port)


if __name__ == "__main__":
    main()
