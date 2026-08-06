"""板块/个股「交易权限」判定（用户只有主板权限）。

依据代码前缀区分：
  · 主板（可交易）：沪 600/601/603/605；深 000/001/002/003
  · 双创（无权限）：科创板 688；创业板 300/301
  · 北交所（无权限）：8xxxxx / 4xxxxx
其余（如 11x 转债、3 字头非 300/301 等）按非主板保守处理并标注。
"""
from __future__ import annotations


def classify_board(code: str) -> dict:
    """返回 {board_key, board_name, main_board, badge, note}。"""
    c = (code or "").strip()
    if not c or not c.isdigit():
        return {"board_key": "unknown", "board_name": "未知",
                "main_board": False, "badge": "?", "note": "代码无法识别"}

    if c.startswith("688"):
        return {"board_key": "star", "board_name": "科创板", "main_board": False,
                "badge": "科创", "note": "科创板·双创·你仅持主板权限"}
    if c.startswith("30") or c.startswith("301"):
        return {"board_key": "chi_next", "board_name": "创业板", "main_board": False,
                "badge": "创业", "note": "创业板·双创·你仅持主板权限"}
    if c.startswith("8") or c.startswith("4"):
        return {"board_key": "bse", "board_name": "北交所", "main_board": False,
                "badge": "北交", "note": "北交所·你仅持主板权限"}
    if (c.startswith("60") or c.startswith("000") or c.startswith("001")
            or c.startswith("002") or c.startswith("003")):
        return {"board_key": "main", "board_name": "主板", "main_board": True,
                "badge": "主板", "note": "主板·可交易"}
    # 其他（转债 11x、其他前缀）保守归为非主板
    return {"board_key": "other", "board_name": "其他", "main_board": False,
            "badge": "其他", "note": "非常规股票代码，谨慎"}
