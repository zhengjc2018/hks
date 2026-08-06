"""通达信行情源封装（easy_tdx 直连公共行情服务器）。

替代原 东财/新浪/腾讯 行情获取。
注意：easy_tdx 的 MacClient 不是线程安全的，Flask(dev server, threaded=True)
会在不同工作线程处理请求，因此使用**有上限的进程级连接池**：每次调用独占一条
连接，避免跨线程共享同一 socket；池满或连接失败时由各调用方回退旧源。
沙箱/断网时 available()=False，由各调用方回退旧源（东财/新浪/腾讯）。
"""
from __future__ import annotations
import threading
import socket
import time
import contextlib
import concurrent.futures

from easy_tdx import MacClient, Market, Period, Adjust, BoardType

_lock = threading.Lock()

# ★2026-08-04 连接泄漏修复：原实现是「线程局部客户端」。但上层几乎所有 TDX 调用都
# 走 server._call_timeout —— 它每次都新开一个工作线程，于是每一次 kline/board_members
# 都在新线程里重新 from_best_host() 多主机探测 + 建连（数秒），并留下一个永不回收的
# easy_tdx 心跳线程。实测运行 4 分钟即累积 91 个心跳线程/连接，最终把通达信连接打满
# → 建连失败 → 熔断 → 成分股取不到 → 扫描 0 只票（run21 现象的真正上游）。
# 改为**定长连接池**：连接复用（单次调用从「探测+建连+查询」降为「纯查询」，实测
# 0.46s/票 vs 原 6s/票），且总连接数有上限，不再泄漏。池内连接由调用方独占使用，
# 天然满足 MacClient 的非线程安全约束。
_POOL_SIZE = 4                  # 池上限：够前端 enrich 与后台扫描并行，又不打爆对端
_POOL_WAIT = 20                 # 等空闲连接上限；等不到即抛错，上层回退新浪
_pool_idle = []                 # 空闲连接
_pool_live = 0                  # 已创建（含在用）连接数
_pool_cv = threading.Condition()

# 通达信死连接（半开 socket）在 Windows 上 recv 会无限阻塞，导致整轮行情请求
# 挂起直到前端 30s 超时 abort。这里给 TDX 调用施加一个作用域内的 socket 超时，
# 超时即抛出 → 由 _call 捕获并重置连接重试 / 由上层兜底切换数据源。
_TDX_SOCKET_TIMEOUT = 10
# 连接建立硬超时：Windows 上连 Mac 版通达信客户端的 connect 可能被防火墙静默丢弃，
# setdefaulttimeout 对 connect 约束力弱（TCP 重传可达数分钟）。用线程级硬超时兜底，
# 超时即熔断 tdx（永久禁用），后续所有调用走 EM 兜底，避免整轮扫描挂死。
_TDX_CONNECT_TIMEOUT = 12
# 熔断标志：首次连接/行情获取持续失败后置 True，available() 直接返回 False。
# ★2026-08-04 修正：原实现是「永久熔断」，导致服务一旦在启动瞬间连不上通达信
# （对端客户端还没起来 / 瞬时网络抖动），就整天走新浪兜底，成分股 board_members
# 无兜底 → 扫描永远 0 只票（run21 现象）。改为**冷却式熔断**：熔断 N 秒后自动
# 允许再试一次，连上即恢复；仍连不上则重新计时，不会退化成疯狂重连。
_TDX_BROKEN = False
_TDX_BROKEN_AT = 0.0
_TDX_COOLDOWN = 300          # 熔断冷却秒数，到点自动半开重试
_TDX_BROKEN_LOCK = threading.Lock()


def _broken():
    """是否处于熔断中。超过冷却期自动半开（清标志，允许下一次真实建连尝试）。"""
    global _TDX_BROKEN
    if not _TDX_BROKEN:
        return False
    if time.time() - _TDX_BROKEN_AT >= _TDX_COOLDOWN:
        with _TDX_BROKEN_LOCK:
            if _TDX_BROKEN and time.time() - _TDX_BROKEN_AT >= _TDX_COOLDOWN:
                _TDX_BROKEN = False
                print(f"[tdx] 熔断已冷却 {_TDX_COOLDOWN}s，半开重试")
        return False
    return True


# ★2026-08-06 Errno22 根因修复：原实现每个线程各自 old=getdefaulttimeout()、退出时
# setdefaulttimeout(old)。socket 默认超时是**进程全局**状态，Flask threaded=True +
# 连接池 4 并发下多线程交错保存/恢复，会把脏值（甚至 0）永久留在全局：
#   A进入(old=None,set10) → B进入(old=10,set10) → A退出(set None) → B退出(set 10)
# 一旦全局被留成 0，后续所有新建 socket 变非阻塞 → Windows connect 抛
# OSError [Errno 22] Invalid argument（WSAEINVAL）→ /api/stock 持续 500，
# 且连接池「换一条重试」也必然失败（新连接同样建不起来）。
# 改为「进程原值 + 引用计数」：并发期间维持 sec，最后一个退出者恢复到模块加载时的
# 原始值，任何交错都不会残留脏超时。
_ORIG_SOCK_TIMEOUT = socket.getdefaulttimeout()
_TO_LOCK = threading.Lock()
_TO_DEPTH = 0


@contextlib.contextmanager
def _tdx_timeout(sec=_TDX_SOCKET_TIMEOUT):
    global _TO_DEPTH
    with _TO_LOCK:
        _TO_DEPTH += 1
        socket.setdefaulttimeout(sec)
    try:
        yield
    finally:
        with _TO_LOCK:
            _TO_DEPTH -= 1
            if _TO_DEPTH <= 0:
                _TO_DEPTH = 0
                socket.setdefaulttimeout(_ORIG_SOCK_TIMEOUT)


def _mk(market_str):
    """'SH'/'SZ'/'BJ' 或 '1'/'0'/'2' -> int 市场代码。"""
    s = str(market_str).upper()
    return int(getattr(Market, s)) if s.isalpha() else int(s)


def _new_client():
    """新建一条 TDX 连接。连接建立（含 from_best_host 多 host 探测）用线程级硬超时
    兜底：Windows 上连 Mac 版通达信客户端的 connect 可能静默卡死（setdefaulttimeout
    对 connect 约束力弱），用 fut.result(timeout) 强制在 _TDX_CONNECT_TIMEOUT 内中断，
    避免整轮行情无限挂起。超时/异常即熔断 tdx（冷却期内走新浪兜底）。"""
    global _TDX_BROKEN, _TDX_BROKEN_AT
    def _connect():
        with _tdx_timeout():
            cc = MacClient.from_best_host()
            cc.ensure_connected()
            return cc
    # 注意：绝不能用 `with ThreadPoolExecutor() as ex:` —— 一旦 fut.result(timeout)
    # 因 from_best_host() 卡在 TCP 重传而超时抛错，退出 with 块时 __exit__ 会
    # shutdown(wait=True)，阻塞到那个孤儿 _connect 线程跑完（可能数分钟）→ 整轮扫描
    # 卡死（run14 根因）。这里显式建 executor，超时/异常时 shutdown(wait=False) 丢弃。
    ex = concurrent.futures.ThreadPoolExecutor(
        max_workers=1, thread_name_prefix="tdx-connect")
    try:
        fut = ex.submit(_connect)
        c = fut.result(timeout=_TDX_CONNECT_TIMEOUT)
    except Exception:
        with _TDX_BROKEN_LOCK:
            _TDX_BROKEN = True
            _TDX_BROKEN_AT = time.time()
        print(f"[tdx] 连接失败，熔断 {_TDX_COOLDOWN}s（到点自动半开重试），期间走新浪兜底",
              flush=True)
        raise
    finally:
        ex.shutdown(wait=False)
    return c


def _close_client(c):
    if c is None:
        return
    try:
        c.close()
    except Exception:
        try:
            c.disconnect()
        except Exception:
            pass


def _acquire(timeout=_POOL_WAIT):
    """从连接池取一条空闲连接；池未满则新建；池满则等待。超时抛错（上层回退新浪）。"""
    global _pool_live
    if _broken():
        raise RuntimeError("tdx 源熔断冷却中")
    deadline = time.time() + timeout
    with _pool_cv:
        while True:
            if _pool_idle:
                return _pool_idle.pop()
            if _pool_live < _POOL_SIZE:
                _pool_live += 1
                break                      # 出锁后再建连（建连最长 12s，不能占着条件变量）
            rest = deadline - time.time()
            if rest <= 0:
                raise RuntimeError(f"tdx 连接池等待超过 {timeout}s（{_POOL_SIZE} 条连接全忙）")
            _pool_cv.wait(rest)
    try:
        c = _new_client()
    except Exception:
        with _pool_cv:
            _pool_live -= 1
            _pool_cv.notify()
        raise
    with _pool_cv:
        n = _pool_live
    print(f"[tdx] 新建连接（池内 {n}/{_POOL_SIZE}）", flush=True)
    return c


def _release(c, broken=False):
    """归还连接。broken=True 表示这条连接已脏（调用抛错），关闭并从池中注销。"""
    global _pool_live
    with _pool_cv:
        if broken or c is None:
            _pool_live -= 1
        else:
            _pool_idle.append(c)
        _pool_cv.notify()
    if broken:
        _close_client(c)


def _call(fn):
    """带连接池 + 自动重连的调用包装。fn 接收一个 client 参数。

    easy_tdx 的 MacClient 非线程安全且连接被中断时会抛 'signal is aborted' 类错误，
    或（半开死 socket）无限阻塞 recv。这里：
      1) 每次调用独占一条池内连接（不共享 → 线程安全）；
      2) 作用域内 socket 超时把「挂起」变成「快速超时异常」；
      3) 首次失败即丢弃该连接、换一条重试一次；
      4) 池上限 _POOL_SIZE，杜绝「每次调用新建连接」的线程/连接泄漏。
    """
    c = _acquire()
    try:
        with _tdx_timeout():
            r = fn(c)
    except Exception:
        _release(c, broken=True)
        c2 = _acquire()                    # 换一条干净连接重试一次
        try:
            with _tdx_timeout():
                r2 = fn(c2)
        except Exception:
            _release(c2, broken=True)
            raise
        _release(c2)
        return r2
    _release(c)
    return r


def available():
    """通达信源是否可用。熔断冷却期内直接返回 False；池满(繁忙)视为可用。"""
    if _broken():
        return False
    try:
        c = _acquire(timeout=3)
    except Exception:
        # 池满 → 说明连接是通的，只是繁忙；建连失败 → _broken 已置位
        return not _broken()
    _release(c)
    return True


def quotes(stocks):
    """实时报价。stocks: list[(market_str, code)] -> DataFrame。"""
    return _call(lambda c: c.get_stock_quotes(
        [(_mk(m), code) for m, code in stocks]))


def kline(market_str, code, period=Period.DAILY, count=800, adjust=Adjust.NONE):
    """K线。period: Period.DAILY/WEEKLY/MIN_60/MIN_15。返回 DataFrame。"""
    return _call(lambda c: c.get_stock_kline(
        _mk(market_str), code, period=period, count=count, adjust=adjust))


def board_ranking(board_type=BoardType.HY, top_n=50):
    return _call(lambda c: c.get_board_ranking(
        board_type=board_type, top_n=top_n))


def board_members(board_symbol, count=100000):
    return _call(lambda c: c.get_board_members(board_symbol, count=count))


def board_summary(board_symbol):
    return _call(lambda c: c.get_board_summary(board_symbol))


def capital_flow(market_str, code):
    return _call(lambda c: c.get_capital_flow(_mk(market_str), code))


def tick(market_str, code, date=None):
    return _call(lambda c: c.get_tick_chart(_mk(market_str), code, date=date))
