# -*- coding: utf-8 -*-
"""Windows 打包版自动更新（EXE only）。

流程：前端「检查更新」→ /api/update/check 查 GitHub release →
下载 exe 到本地数据目录 → 用户确认后启动 PowerShell 更新器，等当前
进程退出后替换 hks.exe 并重新打开。Android / 源码运行不做自动安装。
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import time

import requests

import paths
from llm_client import load_config

APP_VERSION = "1.0.0"
GITHUB_REPO = "zhengjc2018/hks"
UPDATE_TAG = "v1.0.0-windows"
_NO_PROXY = {"http": None, "https": None}

_STATE_LOCK = threading.Lock()
_UPDATE_STATE = {
    "status": "idle",
    "progress": 0.0,
    "bytes": 0,
    "total": 0,
    "message": "",
    "asset_name": None,
    "done": False,
    "error": None,
}


def _settings() -> dict:
    cfg = load_config()
    return {
        "repo": str(cfg.get("update_repo") or GITHUB_REPO).strip(),
        "tag": str(cfg.get("update_tag") or UPDATE_TAG).strip(),
        "version": str(cfg.get("app_version") or APP_VERSION).strip(),
    }


def _parse_version(v):
    m = re.search(r"(\d+(?:\.\d+){0,2})", str(v or ""))
    if not m:
        return None
    parts = [int(x) for x in m.group(1).split(".")]
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])


def _github_release():
    s = _settings()
    url = f"https://api.github.com/repos/{s['repo']}/releases/tags/{s['tag']}"
    r = requests.get(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "Mozilla/5.0",
        },
        timeout=15,
        proxies=_NO_PROXY,
    )
    r.raise_for_status()
    data = r.json()
    assets = data.get("assets") or []
    asset = next((a for a in assets if str(a.get("name", "")).lower().endswith(".exe")), None)
    return data, asset


def check_update():
    s = _settings()
    try:
        data, asset = _github_release()
    except Exception as e:
        return {"ok": False, "error": f"检查更新失败：{e}"}
    if not asset:
        return {"ok": False, "error": "GitHub release 中没有找到 exe 安装包"}
    latest_raw = str(s["tag"]).replace("v", "", 1).split("-", 1)[0]
    cur_v = _parse_version(s["version"])
    latest_v = _parse_version(latest_raw)
    has_update = bool(cur_v and latest_v and latest_v > cur_v)
    return {
        "ok": True,
        "current_version": s["version"],
        "latest_version": latest_raw,
        "has_update": has_update,
        "asset": {
            "name": asset.get("name"),
            "size": asset.get("size"),
            "download_url": asset.get("browser_download_url"),
            "created_at": asset.get("created_at"),
        },
        "published_at": data.get("published_at"),
        "release_name": data.get("name"),
        "notes": (data.get("body") or "")[:1200],
    }


def update_status():
    with _STATE_LOCK:
        return dict(_UPDATE_STATE)


def download_update(force=False):
    with _STATE_LOCK:
        if _UPDATE_STATE["status"] == "downloading":
            return False, "正在下载中，请稍候"
        _UPDATE_STATE.update(
            status="downloading",
            progress=0.0,
            bytes=0,
            total=0,
            message="",
            asset_name=None,
            done=False,
            error=None,
        )

    def _run():
        try:
            info = check_update()
            if not info.get("ok"):
                raise RuntimeError(info.get("error", "检查更新失败"))
            if not info.get("has_update") and not force:
                raise RuntimeError("当前已是最新版本")
            asset = info["asset"]
            url = asset.get("download_url")
            if not url:
                raise RuntimeError("下载地址为空")
            update_dir = paths.data_path("update")
            os.makedirs(update_dir, exist_ok=True)
            target = os.path.join(update_dir, str(asset["name"]))
            tmp = target + ".part"
            total = int(asset.get("size") or 0)
            done_bytes = 0
            with requests.get(url, stream=True, timeout=(20, 120), proxies=_NO_PROXY) as r:
                r.raise_for_status()
                if total <= 0:
                    total = int(r.headers.get("Content-Length") or 0)
                with open(tmp, "wb") as f:
                    for chunk in r.iter_content(chunk_size=1024 * 256):
                        if not chunk:
                            continue
                        f.write(chunk)
                        done_bytes += len(chunk)
                        with _STATE_LOCK:
                            _UPDATE_STATE.update(
                                bytes=done_bytes,
                                total=total,
                                progress=round(done_bytes / total * 100, 1) if total else 0.0,
                            )
            os.replace(tmp, target)
            with _STATE_LOCK:
                _UPDATE_STATE.update(
                    status="done",
                    done=True,
                    asset_name=str(asset["name"]),
                    message=f"下载完成：{asset['name']}",
                    progress=100.0,
                )
        except Exception as e:
            with _STATE_LOCK:
                _UPDATE_STATE.update(status="error", error=str(e), message=f"下载失败：{e}")

    threading.Thread(target=_run, daemon=True).start()
    return True, "开始下载"


_PS_UPDATER = r"""
param(
  [Parameter(Mandatory=$true)][string]$TargetExe,
  [Parameter(Mandatory=$true)][string]$NewExe,
  [Parameter(Mandatory=$false)][string]$LogFile = ""
)
function Log($msg) {
  if ($LogFile) {
    Add-Content -LiteralPath $LogFile -Value ("[{0}] {1}" -f (Get-Date -Format "HH:mm:ss"), $msg)
  }
}
Log "update started"
$deadline = (Get-Date).AddMinutes(5)
while (((Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq $TargetExe })) -and ((Get-Date) -lt $deadline)) {
  Start-Sleep -Milliseconds 300
}
Start-Sleep -Milliseconds 800
if (-not (Test-Path -LiteralPath $NewExe)) {
  Log "new exe missing"
  exit 1
}
try {
  Copy-Item -LiteralPath $NewExe -Destination $TargetExe -Force -ErrorAction Stop
  Log "copied"
} catch {
  Log ("copy failed: " + $_.Exception.Message)
  exit 2
}
Start-Process -FilePath $TargetExe -WorkingDirectory (Split-Path -Parent $TargetExe)
Log "restarted"
exit 0
"""


def apply_update():
    if sys.platform != "win32":
        return {"ok": False, "error": "自动安装仅支持 Windows 打包版"}
    if not getattr(sys, "frozen", False):
        return {"ok": False, "error": "当前是源码运行，请使用打包后的 EXE"}
    with _STATE_LOCK:
        if _UPDATE_STATE.get("done") and _UPDATE_STATE.get("asset_name"):
            new_exe = os.path.join(paths.data_path("update"), str(_UPDATE_STATE["asset_name"]))
        else:
            update_dir = paths.data_path("update")
            candidates = []
            if os.path.isdir(update_dir):
                candidates = [
                    os.path.join(update_dir, n)
                    for n in os.listdir(update_dir)
                    if n.lower().endswith(".exe")
                ]
            if not candidates:
                return {"ok": False, "error": "未找到已下载的安装包，请先下载"}
            new_exe = max(candidates, key=os.path.getmtime)
    if not os.path.isfile(new_exe):
        return {"ok": False, "error": "下载文件不存在，请重新下载"}
    target_exe = sys.executable
    if not str(target_exe).lower().endswith(".exe"):
        return {"ok": False, "error": "未识别到当前 EXE 路径"}
    update_dir = paths.data_path("update")
    os.makedirs(update_dir, exist_ok=True)
    script = os.path.join(update_dir, "apply_update.ps1")
    log = os.path.join(update_dir, "apply_update.log")
    with open(script, "w", encoding="utf-8-sig") as f:
        f.write(_PS_UPDATER)
    cmd = [
        "powershell",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        script,
        target_exe,
        new_exe,
        log,
    ]
    try:
        flags = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
        subprocess.Popen(cmd, creationflags=flags, close_fds=True)
    except Exception as e:
        return {"ok": False, "error": f"启动更新器失败：{e}"}
    return {
        "ok": True,
        "message": "更新已启动：程序退出后会自动替换 hks.exe 并重新打开",
    }
