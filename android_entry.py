"""Android 内置后端入口：首次运行生成默认数据文件，随后启动 Flask。"""

from __future__ import annotations

import json
import os
import shutil
import threading


def _seed_defaults() -> None:
    import paths

    data = os.environ.get("APANEL_DATA_DIR") or os.environ.get("HOME") or paths.APP_DIR
    os.environ["APANEL_DATA_DIR"] = data
    paths.ensure_data_dir()

    defaults = {
        "config.json": "config.example.json",
        "strategic_overlay.json": {
            "meta": {
                "valid_from": "2026-08-06",
                "valid_until": "2026-12-31",
                "regime": "neutral",
                "note": "默认中性叠加层：无战略倾向，纯技术门控。",
            },
            "avoid": [],
            "favor": [],
        },
    }
    empty = ("positions.json", "holdings.json", "lifecycle.json", "watched_boards.json")

    for name, source in defaults.items():
        target = paths.data_path(name)
        if os.path.exists(target):
            continue
        if isinstance(source, str):
            bundled = paths.bundle_path(source)
            if os.path.exists(bundled):
                shutil.copyfile(bundled, target)
                continue
        with open(target, "w", encoding="utf-8") as f:
            json.dump(source, f, ensure_ascii=False, indent=2)

    for name in empty:
        target = paths.data_path(name)
        if not os.path.exists(target):
            with open(target, "w", encoding="utf-8") as f:
                json.dump([], f)


def start(host: str = "127.0.0.1", port: int = 5050) -> None:
    _seed_defaults()

    from server import app, start_board_scheduler, _bg_refresh_sectors

    start_board_scheduler()
    _bg_refresh_sectors()

    def _run() -> None:
        app.run(host=host, port=int(port), threaded=True)

    threading.Thread(target=_run, daemon=True).start()
