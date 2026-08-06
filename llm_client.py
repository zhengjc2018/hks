"""可选大模型客户端（OpenAI 兼容 chat/completions）。

用途：在用户「配置了自己的模型 API」后，由工作台后端调用该模型，
生成「板块表现原因 / 个股买点·卖点一句话总结」以及「减持/利空风险研判」。
不配置则不调用、不产生任何 token 费用（全程走规则模板兜底）。

配置存于 apanel/config.json（含 api_key），已加入 .gitignore，不外传。
"""
from __future__ import annotations
import json
import os
import threading
import time

import requests
import paths

CONFIG_PATH = paths.data_path("config.json")
_lock = threading.RLock()

_DEFAULT = {"llm": {"enabled": False, "endpoint": "", "api_key": "", "model": ""}, "issue_form_url": ""}

# 熔断：单次 LLM 调用失败（超时/网络不通）后，短时间内其余调用直接跳过，
# 降级到规则模板，避免「死端点逐个 25s 超时」把整个请求拖死（如代理挂掉时）。
LLM_TIMEOUT = 60          # 单次调用超时（秒）；minimax-m3 等模型逐板块生成较慢，放宽避免被掐断
LLM_DEAD_WINDOW = 60      # 熔断窗口（秒）：失败后多少秒内不再试 LLM
_LLM_DEAD_UNTIL = 0.0


def load_config() -> dict:
    with _lock:
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            cfg.setdefault("llm", _DEFAULT["llm"])
            return cfg
        except Exception:
            return json.loads(json.dumps(_DEFAULT))


def save_config(cfg: dict) -> None:
    with _lock:
        cur = load_config()
        cur["llm"] = cfg.get("llm", cur["llm"])
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cur, f, ensure_ascii=False, indent=2)


def public_config() -> dict:
    """对外暴露配置（隐藏 api_key）。"""
    cfg = load_config()
    llm = cfg["llm"]
    return {"enabled": bool(llm.get("enabled")),
            "endpoint": llm.get("endpoint", ""),
            "model": llm.get("model", ""),
            "has_key": bool(llm.get("api_key")),
            "issue_form_url": str(cfg.get("issue_form_url", "")).strip()}


def call_llm(system: str, user: str, max_tokens: int = 400, timeout: int = None) -> str | None:
    """调用 OpenAI 兼容接口。失败/未配置返回 None（调用方须降级到规则模板）。"""
    global _LLM_DEAD_UNTIL
    # 离线验证钩子：APANEL_LLM_MOCK=1 时返回与板块数量匹配的模拟简评，
    # 用于没有可用 LLM 端点时验证「每板块 LLM 简评」渲染管线（不消耗 token）。
    if os.environ.get("APANEL_LLM_MOCK") == "1":
        import re as _re
        _lines = [l for l in user.split("\n") if _re.match(r"^\d+\.\s", l.strip())]
        _out = [f"{i}. 模拟简评{i}：量价配合，关注延续性" for i in range(1, len(_lines) + 1)]
        _out.append("总览：模拟总览——主线轮动，控制仓位")
        return "\n".join(_out)
    llm = load_config()["llm"]
    if not llm.get("enabled") or not llm.get("endpoint") or not llm.get("api_key"):
        return None
    # 熔断：窗口内曾失败则直接跳过，避免死端点拖垮整批请求
    if time.time() < _LLM_DEAD_UNTIL:
        return None
    url = llm["endpoint"].rstrip("/") + "/chat/completions"
    body = {
        "model": llm.get("model") or "gpt-3.5-turbo",
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "temperature": 0.3,
        "max_tokens": max_tokens,
    }
    # 显式走代理（默认读 HTTPS_PROXY/HTTP_PROXY，缺失时回退 127.0.0.1:7890），
    # 避免依赖 requests 的 trust_env 默认行为，保证 LLM 走代理出网。
    _px = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY") or "http://127.0.0.1:7890"
    _proxies = {"http": _px, "https": _px}
    try:
        r = requests.post(url, json=body,
                          headers={"Authorization": "Bearer " + llm["api_key"],
                                   "Content-Type": "application/json"},
                          timeout=(timeout if isinstance(timeout, int) and timeout > 0 else LLM_TIMEOUT), proxies=_proxies)
        if r.status_code != 200:
            return None
        data = r.json()
        msg = data["choices"][0]["message"]
        # 推理模型（如 GLM）把正文放 content、思考放 reasoning_content；
        # 若 content 为空（token 被思考吃光）则兜底取 reasoning_content，避免整段摘要失败
        text = (msg.get("content") or msg.get("reasoning_content") or "").strip()
        # 防御：部分模型会把思考过程用 <think>...</think> 包在 content 里泄漏，
        # 统一在源头剥离，避免脏文本进到下游清洗/前端
        if text and ("<think>" in text.lower() or "<think:6124c78e>" in text):
            import re as _re
            text = _re.sub(r'<think>.*?</think>', '', text,
                           flags=_re.DOTALL | _re.IGNORECASE).strip()
            text = _re.sub(r'<think:6124c78e>.*?</think:6124c78e>', '', text,
                           flags=_re.DOTALL | _re.IGNORECASE).strip()
        _LLM_DEAD_UNTIL = 0.0  # 成功则解除熔断
        return text or None
    except Exception:
        _LLM_DEAD_UNTIL = time.time() + LLM_DEAD_WINDOW  # 失败则进入熔断窗口
        return None


def llm_test_details() -> dict:
    """连通性诊断（供前端『测试连接』按钮）。返回真实失败原因，不再吞错。"""
    llm = load_config()["llm"]
    if not llm.get("enabled"):
        return {"ok": False, "stage": "config",
                "error": "未勾选『启用自定义模型』（请先在面板打勾再测）"}
    if not llm.get("endpoint"):
        return {"ok": False, "stage": "config", "error": "endpoint 为空"}
    if not llm.get("api_key"):
        return {"ok": False, "stage": "config",
                "error": "api_key 为空（保存可能未生效，请重新点保存）"}
    url = llm["endpoint"].rstrip("/") + "/chat/completions"
    model = llm.get("model") or "gpt-3.5-turbo"
    body = {
        "model": model,
        "messages": [{"role": "system", "content": "你是连接测试助手。"},
                     {"role": "user", "content": "只回复两个字：正常"}],
        "temperature": 0.3, "max_tokens": 200,
    }
    _px = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY") or "http://127.0.0.1:7890"
    _proxies = {"http": _px, "https": _px}
    try:
        r = requests.post(url, json=body,
                          headers={"Authorization": "Bearer " + llm["api_key"],
                                   "Content-Type": "application/json"},
                          timeout=25, proxies=_proxies)
        if r.status_code != 200:
            return {"ok": False, "stage": "http",
                    "error": f"HTTP {r.status_code}：{r.text[:400]}"}
        data = r.json()
        msg = data["choices"][0]["message"]
        sample = (msg.get("content") or msg.get("reasoning_content") or "").strip()
        return {"ok": True, "sample": sample}
    except Exception as e:
        return {"ok": False, "stage": "net",
                "error": f"{type(e).__name__}：{e}"}
