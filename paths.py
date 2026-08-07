"""统一读写路径：本地开发写在项目目录，Android 上写在应用内部存储。"""

from __future__ import annotations

import os
import sys

APP_DIR = os.path.dirname(os.path.abspath(__file__))


def _default_data_dir() -> str:
    if getattr(sys, "frozen", False) and sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        return os.path.join(base, "HKS")
    return APP_DIR


def data_dir() -> str:
    override = os.environ.get("APANEL_DATA_DIR")
    if override:
        return override
    return _default_data_dir()


def data_path(name: str) -> str:
    return os.path.join(data_dir(), name)


def bundle_path(*names: str) -> str:
    return os.path.join(APP_DIR, *names)


def ensure_data_dir() -> None:
    os.makedirs(data_dir(), exist_ok=True)
