"""Windows 可执行文件入口：初始化数据目录、启动内置服务并打开浏览器。"""

from __future__ import annotations

import os
import threading
import time
import webbrowser

import android_entry
import paths


def main() -> None:
    android_entry._seed_defaults()

    host = os.environ.get("APANEL_HOST", "127.0.0.1")
    port = int(os.environ.get("APANEL_PORT") or os.environ.get("PORT") or "5000")

    from server import app, start_board_scheduler, _bg_refresh_sectors

    start_board_scheduler()
    _bg_refresh_sectors()

    def _open_browser() -> None:
        webbrowser.open(f"http://127.0.0.1:{port}/")

    threading.Timer(2.0, _open_browser).start()
    print(f"A股机会雷达 启动中 -> http://{host}:{port}")
    app.run(host=host, port=port, threaded=True)


if __name__ == "__main__":
    main()
