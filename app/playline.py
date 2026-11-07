"""
播放线路策略的「环境相关」部分（v1.11.0）。

app/utils.py 里的 classify_play_line / parse_play_lines 是纯字符串函数 —— 好测、
好复用。但还有三件事必须碰外部世界：

1. **源站级策略覆盖**：`sources.play_line_policy` 默认 `inherit`（跟随全局），
   只有非凡资源站这类「第 1 条线路恒为 /share/ 分享页」的站才单独设 `filter`，
   避免全局策略误伤别的站。读它要查库，所以带 TTL 缓存（面板改下拉后立即失效）。
2. **HTML 探测**（可选，默认关）：PLAY_LINE_PROBE_HTML=true 时对 unknown 地址做一次
   HEAD。这里负责限并发、超时、进程内缓存；任何一个环节失败都只回落到 unknown，
   绝不把详情接口拖挂。
3. **读侧统计**：过滤发生在读路径，同步阶段无从统计。这里累计进程内计数
   （累计值，重启清零），供 /api/v1/stats 与面板展示「线路：2→1（过滤 1 条 page）」。
"""
import logging
import threading
import time

import httpx
from sqlalchemy import select

from . import config, database
from .models import sources

log = logging.getLogger("vodsync.playline")

POLICIES = ("inherit", "off", "filter", "whitelist")

# ---------------------------------------------------------------------------
# 源站级策略
# ---------------------------------------------------------------------------
_policy_cache: dict = {}
_policy_lock = threading.Lock()


def normalize(policy) -> str:
    p = (policy or "").strip().lower()
    return p if p in POLICIES else "inherit"


def source_policy(slug: str) -> str:
    """
    读某源站配置的策略（磁盘值，`inherit` 表示跟随全局）。带 TTL 缓存。

    任何异常（列不存在的老库、连接抖动）都按 inherit 处理 —— 线路过滤是增强功能，
    不能因为它让详情接口 500。
    """
    if not slug:
        return "inherit"
    now = time.time()
    with _policy_lock:
        hit = _policy_cache.get(slug)
    if hit and hit[0] > now:
        return hit[1]
    value = "inherit"
    try:
        with database.engine.connect() as conn:
            row = conn.execute(
                select(sources.c.play_line_policy).where(sources.c.slug == slug)
            ).first()
        if row:
            value = normalize(row[0])
    except Exception as e:  # noqa: BLE001
        log.debug("[%s] 读取线路策略失败（按 inherit 处理）: %s", slug, e)
    with _policy_lock:
        if len(_policy_cache) > 512:
            _policy_cache.clear()
        _policy_cache[slug] = (now + max(1, config.PLAY_LINE_POLICY_CACHE_SECONDS), value)
    return value


def global_policy() -> str:
    """全局策略（非法值回落到 filter，与 utils._norm_policy 同口径）。"""
    p = (config.PLAY_LINE_POLICY or "").strip().lower()
    return p if p in ("off", "filter", "whitelist") else "filter"


def effective_policy(slug: str) -> str:
    """最终生效策略：源站显式设置优先，否则用全局 PLAY_LINE_POLICY。"""
    per = source_policy(slug)
    return per if per != "inherit" else global_policy()


def reset_policy_cache(slug: str = None) -> None:
    """面板改完策略后立刻失效（不等 TTL 过期）。"""
    with _policy_lock:
        if slug:
            _policy_cache.pop(slug, None)
        else:
            _policy_cache.clear()


# ---------------------------------------------------------------------------
# HTML 探测（可选）
# ---------------------------------------------------------------------------
_probe_cache: dict = {}
_probe_lock = threading.Lock()
_probe_sem = threading.BoundedSemaphore(2)
_PROBE_CACHE_MAX = 512


def probe_is_page(url: str) -> bool:
    """
    对地址做一次 HEAD，`Content-Type: text/html` 视为页面（不可直接播放）。

    只在 PLAY_LINE_PROBE_HTML=true 且该线路类型为 unknown 时被调用：
    - 限并发 2，避免一次详情请求里几十条线路同时打出去；
    - 超时 PLAY_LINE_PROBE_TIMEOUT（默认 1.5s），失败一律按「不是页面」；
    - 结果只进进程内缓存（PLAY_LINE_PROBE_CACHE_SECONDS），不落库。
    """
    if not config.PLAY_LINE_PROBE_HTML or not url:
        return False
    now = time.time()
    with _probe_lock:
        hit = _probe_cache.get(url)
    if hit and hit[0] > now:
        return hit[1]
    is_html = False
    if _probe_sem.acquire(timeout=config.PLAY_LINE_PROBE_TIMEOUT):
        try:
            with httpx.Client(
                timeout=config.PLAY_LINE_PROBE_TIMEOUT,
                follow_redirects=True,
                headers={"User-Agent": config.USER_AGENT},
            ) as c:
                resp = c.head(url)
                ctype = (resp.headers.get("content-type") or "").lower()
                is_html = "text/html" in ctype
        except Exception:  # noqa: BLE001 - 探测失败只影响分类
            is_html = False
        finally:
            _probe_sem.release()
    with _probe_lock:
        if len(_probe_cache) > _PROBE_CACHE_MAX:
            _probe_cache.clear()
        _probe_cache[url] = (now + max(0, config.PLAY_LINE_PROBE_CACHE_SECONDS), is_html)
    return is_html


# ---------------------------------------------------------------------------
# 读侧统计（进程内累计，重启清零）
#
# 为什么不在同步阶段统计：过滤发生在读路径（同步只负责把 play_from 存下来），
# 同步时算一遍等于把 600 万条 play_url 全解析一次，纯属浪费。这里的数字是
# 「自本次进程启动以来，客户端实际触发了多少次过滤」，用于判断策略是否真的在生效。
# ---------------------------------------------------------------------------
_stats = {"lines": 0, "dropped": 0, "fallback": 0, "page_lines": 0}
_by_source: dict = {}
_STATS_MAX_SOURCES = 200
_stats_lock = threading.Lock()
_started_at = time.time()


def note(slug: str, meta: dict) -> None:
    """记一次解析结果（由 API 层调用，参数就是 parse_play_lines 的 meta）。"""
    if not meta:
        return
    lines = int(meta.get("raw_lines") or 0)
    dropped = int(meta.get("dropped") or 0)
    page_lines = int(meta.get("page_lines") or 0)
    with _stats_lock:
        _stats["lines"] += lines
        _stats["dropped"] += dropped
        _stats["page_lines"] += page_lines
        if meta.get("fallback"):
            _stats["fallback"] += 1
        if slug:
            rec = _by_source.get(slug)
            if rec is None:
                if len(_by_source) >= _STATS_MAX_SOURCES:
                    return
                rec = _by_source[slug] = {"lines": 0, "dropped": 0, "fallback": 0}
            rec["lines"] += lines
            rec["dropped"] += dropped
            if meta.get("fallback"):
                rec["fallback"] += 1


def stats_snapshot(slug: str = None) -> dict:
    """
    统计快照。slug 给定时附带该源站的累计数字（面板每行展示用）。

    口径说明：这是**本次进程启动以来**的累计值（重启清零），不是历史总量 ——
    它反映「策略是否真的在拦截线路」，不用于计费或审计。
    """
    with _stats_lock:
        data = {
            "policy": (config.PLAY_LINE_POLICY or "").strip().lower() or "filter",
            "filtered_total": _stats["dropped"],
            "fallback_total": _stats["fallback"],
            "page_lines_total": _stats["page_lines"],
            "lines_total": _stats["lines"],
            "uptime_seconds": int(time.time() - _started_at),
            "by_source": {k: dict(v) for k, v in _by_source.items()},
        }
    if slug:
        data["source"] = data["by_source"].get(slug, {"lines": 0, "dropped": 0, "fallback": 0})
    return data


def reset_stats() -> None:
    """测试用：清空计数。"""
    global _started_at
    with _stats_lock:
        for k in _stats:
            _stats[k] = 0
        _by_source.clear()
        _started_at = time.time()


def set_source_policy(slug: str, policy: str) -> str:
    """
    写源站级策略。非法值抛 ValueError（由 API 转 400）。
    返回写入后的值。
    """
    p = normalize(policy)
    if (policy or "").strip().lower() not in POLICIES:
        raise ValueError(f"未知的线路策略: {policy}（可选 {'/'.join(POLICIES)}）")
    with database.write_txn() as conn:
        conn.execute(
            sources.update().where(sources.c.slug == slug).values(play_line_policy=p)
        )
    reset_policy_cache(slug)
    log.info("[%s] 播放线路策略 = %s", slug, p)
    return p
