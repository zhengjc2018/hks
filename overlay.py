"""§6.8 战略叠加层（Strategic Overlay）加载与施加。

数据源：strategic_overlay.json（由《战略预见_YYYY年W周备忘录》§二/§三提炼，大模型每周跑一次产出冻结的结构化JSON）。
板块解析：easy_tdx.UnifiedTdxClient.get_board_list —— 返回 TDX 代码(881xxx)，与 server.py 的 bk 同体系，
          故 §6.8 直接按代码匹配（无需按板块名对齐，规避编码错位）。
生效规则（§6.8 软打分：叠加层是顾问不是门卫）：
  - meta.valid_from ~ valid_until 区间内才施加；过期/未到 → 本层完全退化（纯技术门控），不影响原判定。
  - avoid 命中 → overlay_score −2（软扣分，绝不强制改标签）。
  - favor 命中 → overlay_score +2（软加分，绝不强制改标签）。
  - 标签(state/label) 始终由确定性指标(_quality_label / classify_sector)拍板，本层只微调分数。
  - regime → 透传前端作背景语境。
回测：board_ok_for_date(bk, dt) 按日期区间门控（仅标注 overlay_hit 语义），avoid 不再硬踢候选。
总开关：config.strategic_overlay_enabled=False 时整层关闭，纯技术、零外部依赖。
"""
from __future__ import annotations

import os
import time
import json
import threading
from datetime import datetime, date

import paths
from easy_tdx import UnifiedTdxClient, BoardType

_OVERLAY_PATH = paths.data_path("strategic_overlay.json")
_OVERLAY_CONFIG = paths.data_path("config.json")
_OVERLAY_CFG_CACHE = {"mtime": 0, "val": True}
_lock = threading.Lock()          # 保护本模块自身的 TDX 客户端
_cache = {"mtime": 0, "data": None}

_client = None


def _get_client():
    global _client
    if _client is None:
        _client = UnifiedTdxClient()
    return _client


def _build_name_map():
    """拉全部板块，建 name→code / code→name 双向映射。"""
    c = _get_client()
    name2code, code2name = {}, {}
    for bt in (BoardType.HY, BoardType.HY2, BoardType.GN, BoardType.FG):
        try:
            df = c.get_board_list(bt)
            if df is None or getattr(df, "empty", True):
                continue
            for _, r in df.iterrows():
                nm, cd = r.get("name"), r.get("code")
                if nm and cd is not None:
                    nm, cd = str(nm), str(cd)
                    name2code.setdefault(nm, cd)
                    code2name.setdefault(cd, nm)
        except Exception as e:  # 单类失败不影响其它类
            print("[overlay] get_board_list", bt.name, "err:", e)
    return name2code, code2name


def _resolve_codes(names, name2code):
    """板块名 → 代码集合（精确优先，次子串兜底）。返回 set。"""
    out = set()
    for n in names:
        n = str(n)
        if n in name2code:
            out.add(name2code[n])
            continue
        hit = [cd for nm, cd in name2code.items() if n in nm]   # 如 yaml 写「银行」命中「全国性银行」
        if hit:
            out.update(hit)
        else:
            print("[overlay] 未解析板块名（TDX 无此板块）:", n)
    return out


def load_overlay(force=False):
    """加载 json + 预解析 avoid/favor 为代码集合。带 mtime 缓存。返回 dict 或 None。"""
    try:
        mt = os.path.getmtime(_OVERLAY_PATH)
    except OSError:
        return None
    if not force and _cache["mtime"] == mt and _cache["data"] is not None:
        return _cache["data"]
    with _lock:
        try:
            with open(_OVERLAY_PATH, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception as e:
            print("[overlay] 读取失败:", e)
            return None
        name2code, _ = _build_name_map()
        for grp in ("avoid", "favor"):
            resolved = []
            for item in (raw.get(grp) or []):
                codes = _resolve_codes(item.get("boards", []), name2code)
                if codes:
                    resolved.append({"codes": codes, "reason": item.get("reason", "")})
            raw["_" + grp + "_resolved"] = resolved
        raw["_name2code"] = name2code
        _cache["mtime"] = mt
        _cache["data"] = raw
    return raw


def _date_in_range(ov, dt):
    """dt: str('YYYY-MM-DD') 或 date。在区间内返回 True。"""
    vf = (ov.get("meta") or {}).get("valid_from")
    vu = (ov.get("meta") or {}).get("valid_until")
    try:
        d = datetime.strptime(dt, "%Y-%m-%d").date() if isinstance(dt, str) else dt
        if vf and d < datetime.strptime(vf, "%Y-%m-%d").date():
            return False
        if vu and d > datetime.strptime(vu, "%Y-%m-%d").date():
            return False
    except Exception:
        return True
    return True


def _overlay_enabled():
    """总开关：config.strategic_overlay_enabled，默认 True。
    False 时叠加层整层关闭，apply_overlay 直接返回纯技术结果（零外部依赖）。"""
    try:
        mt = os.path.getmtime(_OVERLAY_CONFIG)
        if _OVERLAY_CFG_CACHE["mtime"] != mt:
            with open(_OVERLAY_CONFIG, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            _OVERLAY_CFG_CACHE["mtime"] = mt
            _OVERLAY_CFG_CACHE["val"] = bool(cfg.get("strategic_overlay_enabled", True))
    except Exception:
        return True
    return _OVERLAY_CFG_CACHE["val"]


def apply_overlay(quality, bk, name=None):
    """§6.8 软打分修正：叠加层只调整 overlay_score 并写备注/透传 regime，
    绝不覆盖确定性指标算出的 state/label。文件缺失/区间外/总开关关闭 → 纯技术退化。"""
    # 总开关关闭 → 直接纯技术，零外部依赖
    if not _overlay_enabled():
        quality.setdefault("overlay_score", 0)
        quality["regime"] = None
        quality["overlay_note"] = None
        return quality, None, None
    ov = load_overlay()
    regime = (ov.get("meta") or {}).get("regime") if ov else None
    in_range = ov and _date_in_range(ov, date.today().isoformat())
    quality.setdefault("overlay_score", 0)
    quality["regime"] = regime if in_range else None
    quality["overlay_note"] = None
    if not in_range:
        return quality, (regime if in_range else None), None
    bk = str(bk)
    note = None
    for a in ov.get("_avoid_resolved", []):
        if bk in a["codes"]:
            quality["overlay_score"] -= 2      # 软扣分，不强制跳类目
            note = "战略回避：" + a["reason"]
            break
    if note is None:
        for f in ov.get("_favor_resolved", []):
            if bk in f["codes"]:
                quality["overlay_score"] += 2  # 软加分
                note = "战略推荐：" + f["reason"]
                break
    quality["overlay_note"] = note
    return quality, regime, note


def board_ok_for_date(bk, dt):
    """回测用：叠加层只做软打分提示，不剥夺技术门控的否决/放行权。
    本函数保留日期区间门控语义（用于标注 overlay_hit），但 avoid 不再 return False。"""
    ov = load_overlay()
    if not ov or not _date_in_range(ov, dt):
        return True
    bk = str(bk)
    for a in ov.get("_avoid_resolved", []):
        if bk in a["codes"]:
            return True   # 曾经此处 return False（硬踢）；现改为不剥夺技术否决权
    return True
