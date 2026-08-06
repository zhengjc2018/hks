# -*- coding: utf-8 -*-
"""
lifecycle.py —— 个股全生命周期状态机（judge_state，配套 选股买卖点SOP.md v1 · §4/§5）

状态机漏斗（SOP §4.2 + §5）：
  双通道进自选 ──▶ 观察池(4-Tier) + seq8等待(④→最多8交易日→⑤) ──▶ 当日待上车 ──▶ 持仓 ──▶ 卖出三层

阶段（stage）：
  watch   自选观察   双通道并集进自选；记录 hooked_date（进池日）
  ready   待上车     ⑤启动触发当日即可就绪，用户手动确认后进入持仓（取消挂钩≥1天硬性门槛）
  holding 持仓       用户手动「确认上车」后；同步写入 positions.json → 卖点信号开始跟踪
  exited  已离场     用户手动「清仓」后；从 positions.json 移除（卖点信号停止跟踪）

双通道（SOP §6）：
  通道A（机械）：picks.get_cache()['full_match']（五条件全命中）→ 自动进自选
  通道B（主观）：用户在个股详情点「加入观察池」→ 手动进自选

约束（设计_盘中预警带价挂单工作流.md §二）：程序绝不自动下单/撤单，只算阶段、由用户在
  券商 APP 手动执行；本模块仅做阶段推进 + 记录手动确认，不碰交易。

性能：持仓/候选数量少，仍走后台预计算缓存（与 picks/sell 一致），避免与 TDX 锁抢资源。
免责：所有产出仅供研究参考，不构成投资建议（SOP §10.10）。
"""
from __future__ import annotations
import os
import time
import json
import threading
import datetime

import paths

SEQ8_WINDOW = 8

import picks
import sell

# ===== 阶段常量 =====
STAGE_WATCH = "watch"      # 自选观察
STAGE_READY = "ready"      # 待上车（尾盘命中即就绪，无 +1 天等待）
STAGE_HOLD = "holding"     # 持仓
STAGE_EXIT = "exited"      # 已离场
STAGES = [STAGE_WATCH, STAGE_READY, STAGE_HOLD, STAGE_EXIT]

# 卖出三层（manual 确认层）：1=减仓(部分) / 2=减仓(大半) / 3=清仓
EXIT_LAYER_REDUCE = 1
EXIT_LAYER_REDUCE_MORE = 2
EXIT_LAYER_CLEAR = 3

LIFECYCLE_PATH = paths.data_path("lifecycle.json")

# ===== 锁 =====
# _LC_LOCK    ：后台预计算「computing」标志（避免重复后台计算）
# _LC_FILE_LOCK：文件读写串行化（防止后台 compute 与手动动作并发写文件互相覆盖）
_LC_CACHE = {"ts": 0, "data": None, "computing": False, "last_err": None}
_LC_LOCK = threading.Lock()
_LC_FILE_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# 基础读写（全部经 _LC_FILE_LOCK 串行化）
# ---------------------------------------------------------------------------
def _now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _today():
    return datetime.date.today().isoformat()


def load_lc():
    """读 lifecycle.json；缺失/格式错 → 返回空列表。"""
    try:
        with open(LIFECYCLE_PATH, "r", encoding="utf-8") as f:
            arr = json.load(f)
        return arr if isinstance(arr, list) else []
    except Exception:
        return []


def save_lc(arr):
    try:
        with open(LIFECYCLE_PATH, "w", encoding="utf-8") as f:
            json.dump(arr if isinstance(arr, list) else [], f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        print("[lifecycle] save error:", e)
        return False


def _days_between(d0, d1):
    """两个 ISO 日期字符串相差天数；任一非法 → 0。"""
    try:
        return (datetime.date.fromisoformat(d1) - datetime.date.fromisoformat(d0)).days
    except Exception:
        return 0


def _add_trade_days(date_str, days):
    """交易日近似计算：跳过周六周日；行情层实际节假日以数据日期为准。"""
    try:
        d = datetime.date.fromisoformat(date_str)
    except Exception:
        d = datetime.date.today()
    step = 1 if days >= 0 else -1
    left = abs(int(days))
    while left:
        d += datetime.timedelta(days=step)
        if d.weekday() < 5:
            left -= 1
    return d.isoformat()


def _new_entry(secid, name, channel, tier=4, sector=None, bk=None):
    today = _today()
    return {
        "secid": secid,
        "name": name,
        "channel": channel,          # "A" 机械自动 / "B" 手动
        "tier": tier,                # 4-Tier 注意力分级（仅展示，不参与上车判定）
        "sector": sector,
        "bk": bk,
        "stage": STAGE_WATCH,
        "hooked_date": today,        # 进观察池日
        "seq8_state": "idle",        # idle / waiting / triggered / expired
        "seq8_start_date": None,       # ④确认日
        "seq8_deadline": None,         # ④后第8个交易日
        "seq8_c3b_ok": False,         # ③b 是否已在底座期确认
        "seq8_c4_time": None,
        "seq8_c5_time": None,
        "seq8_trigger_date": None,     # ⑤确认日；当日即可手动确认上车
        "seq8_buyable_date": None,       # ⑤触发当日即可手动确认上车
        "seq8_base_ok": False,
        "hidden": False,                     # ★C3 ①②暂跌隐藏标记（others 不展示）
        "tail_pass": False,            # 兼容旧字段；不再作为唯一上车依据
        "tail_date": today if channel == "A" else None,
        # 五条件命中快照（前端展示用）
        "c1": False, "c2": False, "c3a": False, "c3b": False, "c4": False, "c5": False,
        "trend_type": None,            # ★2026-08-05 双路径：main/early/none（观察池徽标用）
        "hit_bits": "",                # 形如 "①②③④" 的命中编号字符串
        "entry_ok": None,               # 入场过滤：信号日收盘不得高于 MA5
        "entry_ma5_ratio": None,        # 信号日收盘相对 MA5 偏离百分比
        "entry_filtered": False,        # ⑤ 已响但因远离 MA5 被过滤
        "board_confirmed": False,
        "board_date": None,
        "entry_price": None,
        "entry_date": None,
        "shares": None,
        "exit_layer": None,          # 手动确认层 1/2/3
        "exit_suggest": None,        # 卖点信号建议：清仓/减仓/持有
        "exit_confirmed": False,
        "exit_date": None,
        "created_at": _now(),
        "updated_at": _now(),
    }


# ---------------------------------------------------------------------------
# 双通道进自选（ingest）
# ---------------------------------------------------------------------------
def ingest():
    """双通道进自选：A=五条件全命中(picks.full_match 自动)；B=已手动加入(本文件 channel B)。
    返回本次新自动进池的条数。"""
    with _LC_FILE_LOCK:
        arr = load_lc()
        by_sec = {e["secid"]: e for e in arr}

        # 通道A：picks 全命中自动进自选（SOP §6 通道1）
        pc = picks.get_cache()
        fm = (pc or {}).get("full_match") or []
        pool = {r.get("secid"): r for r in (pc or {}).get("pool") or []}
        trig = {r.get("secid"): r for r in (pc or {}).get("seq8_triggers") or []}
        cand = {r.get("secid"): r for r in (pc or {}).get("seq8_candidates") or []}
        # 全量 picks 记录 map，方便刷新命中字段
        all_picks = {}
        for grp in (pc or {}).values():
            if isinstance(grp, list):
                for r in grp:
                    if r.get("secid"):
                        all_picks[r["secid"]] = r

        def _refresh_hit(e, r):
            e["c1"] = bool(r.get("c1"))
            e["c2"] = bool(r.get("c2"))
            e["c3a"] = bool(r.get("c3a"))
            e["c3b"] = bool(r.get("c3b"))
            e["c4"] = bool(r.get("c4"))
            e["c5"] = bool(r.get("c5"))
            e["trend_type"] = r.get("trend_type")   # ★2026-08-05 双路径标签
            e["entry_ok"] = r.get("entry_ok")
            e["entry_ma5_ratio"] = r.get("entry_ma5_ratio")
            e["entry_filtered"] = bool(e["c5"] and r.get("entry_ok") is False)
            bits = []
            if e["c1"]: bits.append("①")
            if e["c2"]: bits.append("②")
            if e["c3a"]: bits.append("③")
            if e["c3b"]: bits.append("④")
            if e["c4"]: bits.append("⑤")
            # 命中条数字符串：V5 把 ③b 显示为 ④，c4 显示为 ⑤；这里按用户截图还原
            disp = []
            if e["c1"]: disp.append("①")
            if e["c2"]: disp.append("②")
            if e["c3a"]: disp.append("③")
            if e["c4"]: disp.append("④")
            if e["c5"]: disp.append("⑤")
            e["hit_bits"] = "".join(disp)

        new_cnt = 0
        for r in fm:
            secid = r.get("secid")
            if not secid or secid in by_sec:
                continue
            entry = _new_entry(secid, r.get("name"), "A",
                               tier=r.get("tier", 4),
                               sector=r.get("sector"), bk=r.get("bk"))
            _refresh_hit(entry, r)
            arr.append(entry)
            by_sec[secid] = entry
            new_cnt += 1

        # 通道A 已在池的：刷新 tier/板块/命中字段
        for r in fm:
            secid = r.get("secid")
            if secid and secid in by_sec:
                e = by_sec[secid]
                e["tier"] = r.get("tier", e.get("tier"))
                e["sector"] = r.get("sector")
                e["bk"] = r.get("bk")
                e["name"] = r.get("name") or e.get("name")
                e["channel"] = "A"
                _refresh_hit(e, r)
                e["updated_at"] = _now()

        save_lc(arr)
    return new_cnt


# ---------------------------------------------------------------------------
# 阶段推进（evaluate）
# ---------------------------------------------------------------------------
def evaluate():
    """按 SOP 规则推进每只候选的阶段（文件级串行）。"""
    with _LC_FILE_LOCK:
        arr = load_lc()
        if not arr:
            return
        pc = picks.get_cache()
        fm_secids = {r.get("secid") for r in (pc or {}).get("full_match") or []}
        seq_map = {r.get("secid"): r for r in (pc or {}).get("seq8_candidates") or []}
        trig_map = {r.get("secid"): r for r in (pc or {}).get("seq8_triggers") or []}
        # 全量 picks 记录 map（用于刷新命中字段）
        all_picks = {}
        for grp in (pc or {}).values():
            if isinstance(grp, list):
                for r in grp:
                    if r.get("secid"):
                        all_picks[r["secid"]] = r
        sc = sell.get_cache()
        sig_map = {s.get("secid"): s for s in (sc or {}).get("signals") or []}
        today = _today()

        def _refresh_hit_bits(e, r):
            e["c1"] = bool(r.get("c1"))
            e["c2"] = bool(r.get("c2"))
            e["c3a"] = bool(r.get("c3a"))
            e["c3b"] = bool(r.get("c3b"))
            e["c4"] = bool(r.get("c4"))
            e["c5"] = bool(r.get("c5"))
            e["trend_type"] = r.get("trend_type")   # ★2026-08-05 双路径标签
            e["entry_ok"] = r.get("entry_ok")
            e["entry_ma5_ratio"] = r.get("entry_ma5_ratio")
            e["entry_filtered"] = bool(e["c5"] and r.get("entry_ok") is False)
            stage = r.get("stage") or e.get("seq8_state") or "idle"
            bits = []
            if e["c1"]: bits.append("①")
            if e["c2"]: bits.append("②")
            if e["c3a"]: bits.append("③")
            # V5 展示约定：triggered(⑤已响) 不显示④，显示⑤；waiting(④武装中) 显示④
            if e["c5"]:
                bits.append("⑤")
            if e["c4"]:
                bits.append("④")
            e["hit_bits"] = "".join(bits)
            e["_hit_stage"] = stage

        for e in arr:
            secid = e.get("secid")
            tier = int(e.get("tier") or 4)
            # 旧版落盘条目可能没有命中快照字段；先补齐字段，再按当前 picks 刷新。
            # 若该票不在当日 picks 缓存（例如手动观察票），不臆造命中结果，保留已有值，
            # 但保证 API 结构稳定，前端不会因缺字段而退化成另一种数据形态。
            for key in ("c1", "c2", "c3a", "c3b", "c4", "c5"):
                e.setdefault(key, False)
            e.setdefault("hit_bits", "")
            # 每日刷新五条件命中快照（前端观察池显示用）
            r = all_picks.get(secid)
            if r:
                _refresh_hit_bits(e, r)
            else:
                # 不在当日 picks 中时，仅根据已保存快照重建展示字符串；不把 seq8 状态
                # 反推成新的五条件命中，避免把历史/手动观察状态伪装成当日命中。
                bits = []
                if e["c1"]: bits.append("①")
                if e["c2"]: bits.append("②")
                if e["c3a"]: bits.append("③")
                if e["c5"]: bits.append("⑤")
                if e["c4"]: bits.append("④")
                e["hit_bits"] = "".join(bits)
                e["_hit_stage"] = e.get("seq8_state") or e.get("stage") or "idle"
            if e["stage"] in (STAGE_WATCH, STAGE_READY):
                # ★入场过滤落地：旧缓存/旧生命周期可能仍是 triggered/ready；
                # 当前 picks 若明确判定⑤但 entry_ok=False，必须降回观察，不能继续显示待上车。
                # 保留 last_c4/seq8_c4_time 供复盘，但清除当前可上车状态。
                current_entry_filtered = bool(r and r.get("c5") and r.get("entry_ok") is False)
                if current_entry_filtered:
                    e["entry_filtered"] = True
                    e["stage"] = STAGE_WATCH
                    e["seq8_state"] = "idle"
                    e["seq8_trigger_date"] = None
                    e["seq8_buyable_date"] = None
                # 先写基础底座：①②③ 是否成立
                base_ok = secid in fm_secids or bool((seq_map.get(secid) or {}).get("seq8_base"))
                e["seq8_base_ok"] = base_ok

                # seq8 状态推进：
                # 1) 见到④ → waiting，记起始日与 deadline，同时锁定 ③b 底座历史状态
                # 2) waiting 中若出现⑤ → triggered，记录触发日与当日可上车日
                # 3) 超过 deadline 仍未⑤ → expired
                seqr = seq_map.get(secid) or {}
                trr = trig_map.get(secid) or {}
                if seqr.get("seq8_base"):
                    e["seq8_base_ok"] = True
                if seqr.get("c3b"):
                    e["seq8_c3b_ok"] = True   # ③b 事件型：④日确认一次即锁定，不再每日重验
                c4_seen = bool(seqr.get("c4") or seqr.get("seq8_qualify"))
                # ★2026-08-04 破位修复：waiting 期必须同时守住结构底座（①②③a）。
                # 破位（base_ok=False）→ 立即作废，不等 deadline 到期（锚点规则：破位立即作废）。
                # 原实现 waiting_valid 只看 state+③b 锁定，结构破了仍挂 waiting 到超时，与
                # picks.py 扫描侧（if not base_ok: wash_armed=False）不一致。
                waiting_valid = (e.get("seq8_state") == "waiting" and e.get("seq8_c3b_ok") and base_ok)
                c4_qualified = bool(seqr.get("c3b") and c4_seen)
                if seqr.get("seq8_qualify") or c4_qualified or waiting_valid:
                    if e.get("seq8_state") != "triggered":
                        prev_state = e.get("seq8_state")
                        e["seq8_state"] = "waiting"
                        # 窗口起算点只在【首次武装】固定；已 waiting 不重置，杜绝每日 armed 把 deadline 往后滚
                        if e.get("seq8_start_date") is None:
                            e["seq8_start_date"] = today
                            e["seq8_deadline"] = _add_trade_days(today, SEQ8_WINDOW)
                            e["seq8_c4_time"] = seqr.get("seq8_c4_time")
                            e["seq8_buyable_date"] = None
                        elif prev_state == "expired":
                            # 旧窗口逾期后重新武装 = 开新的一轮，重置起算点
                            e["seq8_start_date"] = today
                            e["seq8_deadline"] = _add_trade_days(today, SEQ8_WINDOW)
                            e["seq8_c4_time"] = seqr.get("seq8_c4_time")
                            e["seq8_buyable_date"] = None
                        # 已是 waiting 且未逾期：deadline 维持不变
                    if c4_seen and seqr.get("seq8_c4_time"):
                        e["seq8_c4_time"] = seqr.get("seq8_c4_time")
                    if trr.get("seq8_trigger") and not current_entry_filtered and e.get("seq8_state") == "waiting":
                        e["seq8_state"] = "triggered"
                        e["seq8_trigger_date"] = today
                        e["seq8_c5_time"] = trr.get("seq8_c5_time")
                        e["seq8_buyable_date"] = today
                        e["stage"] = STAGE_READY
                    elif e.get("seq8_state") == "waiting" and e.get("seq8_deadline") and _days_between(e["seq8_deadline"], today) > 0:
                        e["seq8_state"] = "expired"
                        e["seq8_buyable_date"] = None
                elif e.get("seq8_state") == "waiting" and e.get("seq8_deadline") and _days_between(e["seq8_deadline"], today) > 0:
                    e["seq8_state"] = "expired"
                    e["seq8_buyable_date"] = None
                elif e.get("seq8_state") == "waiting":
                    # ★2026-08-05 C1/C2：仅 ③a 结构性破位才 void（清 c4_armed）；①② 暂跌不破位。
                    seqr2 = seq_map.get(secid) or {}
                    c3a_ok = bool(seqr2.get("c3a")) or (secid in fm_secids)
                    if not c3a_ok:
                        # ③a=false → 直接破位(void)，清 c4_armed（C1）
                        e["seq8_state"] = "voided"
                        e["seq8_start_date"] = None
                        e["seq8_deadline"] = None
                        e["seq8_c3b_ok"] = False
                        e["seq8_c4_time"] = None
                        e["seq8_buyable_date"] = None
                    else:
                        # ★C3 ①②暂跌（③a 仍 true）→ 不破位，降权重至不可见，保留 waiting+c4_armed
                        e["seq8_base_ok"] = False
                        e["hidden"] = True

                # 尾盘闸：当日是否仍处 ④⑤ 全命中（即 picks 全命中）
                tail = secid in fm_secids
                e["tail_pass"] = tail
                if tail:
                    e["tail_date"] = today
                # 兼容旧路径：若当日 full_match 直接命中，则直接待上车
                if tail and e.get("seq8_state") != "triggered":
                    e["stage"] = STAGE_READY
                elif e.get("seq8_state") == "triggered":
                    e["stage"] = STAGE_READY
                elif e.get("seq8_state") == "waiting" and e.get("seq8_c3b_ok"):
                    e["stage"] = STAGE_WATCH
                else:
                    e["stage"] = STAGE_WATCH
                e["updated_at"] = _now()
            elif e["stage"] == STAGE_HOLD:
                # 持仓期：拉卖点信号建议（sell.py 已算好）
                s = sig_map.get(secid) or {}
                act = s.get("action")  # 清仓/减仓/持有
                e["exit_suggest"] = act
                if act == "清仓":
                    e["exit_layer_suggest"] = EXIT_LAYER_CLEAR
                elif act == "减仓":
                    e["exit_layer_suggest"] = EXIT_LAYER_REDUCE
                else:
                    e["exit_layer_suggest"] = None
                e["updated_at"] = _now()
        save_lc(arr)


# ---------------------------------------------------------------------------
# 手动动作（用户确认，程序绝不自动下单）
# ---------------------------------------------------------------------------
def add_manual(secid, name=None):
    """通道B：手动加入观察池。已存在则返回 False。"""
    with _LC_FILE_LOCK:
        arr = load_lc()
        if any(e["secid"] == secid for e in arr):
            return False
        pc = picks.get_cache()
        # ★2026-08-06：合并池票在 pool（T1/T2）而非 full_match，两处都查，落盘不丢 tier/命中
        fm = {r.get("secid"): r for r in (pc or {}).get("full_match") or []}
        pl = {r.get("secid"): r for r in (pc or {}).get("pool") or []}
        r = fm.get(secid) or pl.get(secid)
        entry = _new_entry(secid, name or (r.get("name") if r else None), "B",
                           tier=(r.get("tier", 4) if r else 4),
                           sector=(r.get("sector") if r else None),
                           bk=(r.get("bk") if r else None))
        if r:
            # 若 picks 里已有该票，同步命中字段
            entry["c1"] = bool(r.get("c1"))
            entry["c2"] = bool(r.get("c2"))
            entry["c3a"] = bool(r.get("c3a"))
            entry["c3b"] = bool(r.get("c3b"))
            entry["c4"] = bool(r.get("c4"))
            entry["c5"] = bool(r.get("c5"))
            entry["trend_type"] = r.get("trend_type")   # ★2026-08-05 双路径标签
            bits = []
            if entry["c1"]: bits.append("①")
            if entry["c2"]: bits.append("②")
            if entry["c3a"]: bits.append("③")
            if entry["c4"]: bits.append("④")
            if entry["c5"]: bits.append("⑤")
            entry["hit_bits"] = "".join(bits)
        arr.append(entry)
        save_lc(arr)
    return True


def confirm_board(secid, entry_price=None, entry_date=None, shares=None):
    """手动「确认上车」：阶段→持仓，并写入 positions.json（卖点信号开始跟踪）。"""
    with _LC_FILE_LOCK:
        arr = load_lc()
        e = next((x for x in arr if x["secid"] == secid), None)
        if not e:
            return None
        e["stage"] = STAGE_HOLD
        e["board_confirmed"] = True
        e["board_date"] = _today()
        e["updated_at"] = _now()
        # 同步 positions.json（sell.py 数据源）
        positions = sell.load_positions()
        idx = next((i for i, p in enumerate(positions) if str(p.get("secid")) == secid), None)
        rec = {
            "secid": secid,
            "name": e.get("name"),
            "entry_price": (float(entry_price) if entry_price not in (None, "")
                            else (e.get("entry_price") or (positions[idx]["entry_price"] if idx is not None else None))),
            "entry_date": (str(entry_date) if entry_date not in (None, "") else _today()),
            "shares": (int(shares) if shares not in (None, "") else None),
            "batch": 1,   # 仓位管理 334：首笔=1（3成），加仓推进到 2（6成）/3（满仓）。仅对标记持仓生效。
            "seq8_state": e.get("seq8_state"),
            "seq8_start_date": e.get("seq8_start_date"),
            "seq8_deadline": e.get("seq8_deadline"),
            "seq8_c3b_ok": e.get("seq8_c3b_ok"),
            "seq8_trigger_date": e.get("seq8_trigger_date"),
            "seq8_buyable_date": e.get("seq8_buyable_date"),
        }
        if idx is not None:
            rec["stop_mode"] = positions[idx].get("stop_mode")  # 保留止损模式状态
            rec["batch"] = positions[idx].get("batch") or 1     # 保留仓位批次状态
            positions[idx] = rec
        else:
            positions.append(rec)
        sell.save_positions(positions)
        save_lc(arr)
    sell.trigger_refresh()   # 新持仓立即重算卖点信号
    return True


def confirm_exit(secid, layer):
    """手动卖出三层确认：layer=1 减仓(部分) / 2 减仓(大半) / 3 清仓。
    layer==3 清仓 → 阶段置已离场 + 从 positions.json 移除（卖点信号停止跟踪）。"""
    if layer not in (EXIT_LAYER_REDUCE, EXIT_LAYER_REDUCE_MORE, EXIT_LAYER_CLEAR):
        return None
    with _LC_FILE_LOCK:
        arr = load_lc()
        e = next((x for x in arr if x["secid"] == secid), None)
        if not e:
            return None
        e["exit_layer"] = layer
        e["exit_confirmed"] = True
        e["exit_date"] = _today()
        e["updated_at"] = _now()
        if layer == EXIT_LAYER_CLEAR:
            e["stage"] = STAGE_EXIT
            positions = sell.load_positions()
            positions = [p for p in positions if str(p.get("secid")) != secid]
            sell.save_positions(positions)
        save_lc(arr)
    if layer == EXIT_LAYER_CLEAR:
        sell.trigger_refresh()
    return True


def remove(secid):
    """从漏斗移除（无论处于何阶段）。若在持仓，同步移出 positions.json。"""
    with _LC_FILE_LOCK:
        arr = load_lc()
        arr = [e for e in arr if e["secid"] != secid]
        save_lc(arr)
        positions = sell.load_positions()
        if any(str(p.get("secid")) == secid for p in positions):
            positions = [p for p in positions if str(p.get("secid")) != secid]
            sell.save_positions(positions)
            sell.trigger_refresh()


# ---------------------------------------------------------------------------
# 快照 / 预计算缓存
# ---------------------------------------------------------------------------
def _group(arr):
    groups = {s: [] for s in STAGES}
    for e in arr:
        groups.setdefault(e["stage"], []).append(e)
    for s in groups:
        groups[s].sort(key=lambda x: (x.get("tier") or 9, x.get("secid") or ""))
    return groups


def _pool_watch_entry(r):
    """把 picks.pool（T1/T2）合成一条观察池展示条目（只读，不落盘）。"""
    secid = r.get("secid")
    c1 = bool(r.get("c1")); c2 = bool(r.get("c2")); c3a = bool(r.get("c3a"))
    c3b = bool(r.get("c3b")); c4 = bool(r.get("c4")); c5 = bool(r.get("c5"))
    bits = ""
    if c1: bits += "①"
    if c2: bits += "②"
    if c3a: bits += "③"
    if c4: bits += "④"
    if c5: bits += "⑤"
    return {
        "secid": secid, "name": r.get("name"),
        "tier": r.get("tier", 2), "stage": r.get("stage") or "watch",
        "channel": "A", "merged_pool": True,
        "c1": c1, "c2": c2, "c3a": c3a, "c3b": c3b, "c4": c4, "c5": c5,
        "trend_type": r.get("trend_type"),   # ★2026-08-05 双路径标签
        "hit_bits": bits,
        "seq8_state": r.get("stage") or "watch",
        "seq8_base_ok": (c1 and c2 and c3a),
        "armed": r.get("armed"), "last_c4_date": r.get("last_c4_date"),
        "c4_date": r.get("c4_date"), "days_left": r.get("days_left"),
        "fired_date": r.get("fired_date"),
        "entry_ok": r.get("entry_ok"),
        "entry_ma5_ratio": r.get("entry_ma5_ratio"),
        "entry_filtered": bool(r.get("c5") and r.get("entry_ok") is False),
        "hidden": bool(r.get("hidden")),
    }


def get_snapshot():
    """从文件即时分组快照（无 TDX 调用，毫秒级），供接口秒回。
    ★F（2026-08-05）：只读合并 picks.pool（T1/T2）进观察池 watch，不落盘，避免陈旧票累积。
    ★2026-08-06：full_match（⑤触发·前置全满足）也并入观察池 T1——SOP 4.1「Tier1=1235」，
    ⑤触发票本就在观察池（命中任一），前置满足即置顶；未就绪的⑤触发已由 _tier 归 T3 隐藏。"""
    with _LC_FILE_LOCK:
        arr = load_lc()
    groups = _group(arr)
    # 合并今日 pool（T1/T2）+ full_match 到观察池；去重（已在文件中的不重复）
    try:
        pc = picks.get_cache()
        pool_list = (pc or {}).get("pool") or []
        full_list = (pc or {}).get("full_match") or []
        exist = {e.get("secid") for e in arr}
        merged = [_pool_watch_entry(r) for r in pool_list + full_list
                   if r.get("secid") and r.get("secid") not in exist]
        if merged:
            groups["watch"] = groups["watch"] + merged
            groups["watch"].sort(key=lambda x: (x.get("tier") or 9, x.get("secid") or ""))
    except Exception as e:
        print("[lifecycle] merge pool err:", e)
    return {"ts": int(time.time()), "groups": groups,
            "stats": {s: len(groups[s]) for s in STAGES}}


def compute_lifecycle(force=False):
    """后台线程执行（或强制实时）：ingest → evaluate → 写缓存。返回分组快照。"""
    with _LC_LOCK:
        if _LC_CACHE["computing"] and not force:
            return None
        _LC_CACHE["computing"] = True
    t0 = time.time()
    try:
        # picks 缓存为空时惰性触发一次（不阻塞等待，仅保证有数据可比对尾盘闸）
        if picks.get_cache() is None and not picks.is_computing():
            picks.trigger_refresh()
        new_cnt = ingest()
        evaluate()
        snap = get_snapshot()
        snap["new_ingest"] = new_cnt
        with _LC_LOCK:
            _LC_CACHE["data"] = snap
            _LC_CACHE["ts"] = snap["ts"]
            _LC_CACHE["last_err"] = None
        print(f"[lifecycle] 完成：观察 {snap['stats']['watch']} / 待上车 {snap['stats']['ready']} "
              f"/ 持仓 {snap['stats']['holding']} / 已离场 {snap['stats']['exited']}，新进池 {new_cnt}，"
              f"用时 {round(time.time()-t0,1)}s")
        return snap
    except Exception as e:
        print("[lifecycle] compute err:", e)
        with _LC_LOCK:
            _LC_CACHE["last_err"] = str(e)
        return None
    finally:
        with _LC_LOCK:
            _LC_CACHE["computing"] = False


def _bg_run():
    compute_lifecycle()


def trigger_refresh():
    with _LC_LOCK:
        if _LC_CACHE["computing"]:
            return False
    threading.Thread(target=_bg_run, daemon=True).start()
    return True


def get_cache():
    with _LC_LOCK:
        return _LC_CACHE["data"]


def cache_ts():
    with _LC_LOCK:
        return _LC_CACHE["ts"]


def is_computing():
    with _LC_LOCK:
        return _LC_CACHE["computing"]


def last_err():
    with _LC_LOCK:
        return _LC_CACHE["last_err"]


if __name__ == "__main__":
    r = compute_lifecycle(force=True)
    print(json.dumps(r["stats"] if r else {"err": "none"}, ensure_ascii=False, indent=2))
