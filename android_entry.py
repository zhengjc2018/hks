"""Android 内置后端入口：首次运行生成默认数据文件，随后启动 Flask。"""

from __future__ import annotations

import json
import os
import shutil
import threading


def _seed_defaults() -> None:
    import paths

    data = os.environ.get("APANEL_DATA_DIR") or paths.data_dir()
    os.environ["APANEL_DATA_DIR"] = data
    paths.ensure_data_dir()

    cfg_target = paths.data_path("config.json")
    if not os.path.exists(cfg_target):
        cfg_src = paths.bundle_path("config.example.json")
        if os.path.exists(cfg_src):
            shutil.copyfile(cfg_src, cfg_target)
        else:
            with open(cfg_target, "w", encoding="utf-8") as f:
                json.dump({}, f)

    overlay_default = {
            "meta": {
                "valid_from": "2026-08-06",
                "valid_until": "2026-12-31",
                "regime": "neutral",
                "note": "默认中性叠加层：无战略倾向，纯技术门控。",
            },
            "avoid": [],
            "favor": [],
    }
    fallbacks = {
        "lifecycle.json": [],
        "positions.json": [],
        "holdings.json": [],
        "watched_boards.json": [],
        "strategic_overlay.json": overlay_default,
    }

    for name, fallback in fallbacks.items():
        target = paths.data_path(name)
        if os.path.exists(target):
            continue
        bundled = paths.bundle_path("seed", name)
        if os.path.exists(bundled):
            shutil.copyfile(bundled, target)
            continue
        with open(target, "w", encoding="utf-8") as f:
            json.dump(fallback, f, ensure_ascii=False, indent=2)


def start(host: str = "127.0.0.1", port: int = 5050) -> None:
    _seed_defaults()

    from server import app, start_board_scheduler, _bg_refresh_sectors

    start_board_scheduler()
    _bg_refresh_sectors()

    def _run() -> None:
        app.run(host=host, port=int(port), threaded=True)

    threading.Thread(target=_run, daemon=True).start()
