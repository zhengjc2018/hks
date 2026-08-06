"""统一读写路径：本地开发写在项目目录，Android 上写在应用内部存储。"""

from __future__ import annotations

import os

APP_DIR = os.path.dirname(os.path.abspath(__file__))


def data_dir() -> str:
    override = os.environ.get("APANEL_DATA_DIR")
    if override:
        return override
    return APP_DIR


def data_path(name: str) -> str:
    return os.path.join(data_dir(), name)


def bundle_path(*names: str) -> str:
    return os.path.join(APP_DIR, *names)


def ensure_data_dir() -> None:
    os.makedirs(data_dir(), exist_ok=True)
