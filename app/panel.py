"""
管理面板专用支撑（v1.16.4）。

只服务 `app/web/index.html` 与 `/api/v1/admin/*` 的读接口，不参与同步与推荐：

1. **源站屏蔽名单**：持久化在既有 `settings` 表的一个 key 里，不新增表、
   不改 `sources` 表结构、不动 syncer / scheduler。屏蔽时调用的仍是既有的
   `set_source_paused()`，取消屏蔽时把暂停位还原成屏蔽前的值 —— 用户自己
   手动暂停过的站不会被"取消屏蔽"顺手打开。
2. **只读统计快照缓存**：面板的「表清单 / 覆盖统计 / 推荐覆盖率」原本每次
   点击都要逐站 COUNT(*) 扫 12GB 大表（实测 6~50 秒）。这里只在**路由层**
   缓存它们的结果快照，底层计算函数（tablebrowse.list_browsable /
   star_tags.status_all / related.coverage_all）一行未改，`refresh=1` 时
   拿到的仍是实时值。

设计取舍
--------
- 缓存是进程内的、有 TTL 的、可强制刷新的；不落库，重启即空，不会把过期
  数字写进任何业务表，也不会影响任何对客户端开放的 API。
- 同一 key 的并发请求用一把 per-key 锁合并，避免两个人同时点「覆盖统计」
  就把 23 秒的全表扫跑两遍。
- 缓存只包「读」，包不住写；词库增删改后由前端显式重新拉取，不依赖缓存失效。
"""
import json
import logging
import threading
import time
from datetime import datetime

from sqlalchemy import select

from . import database
from .models import settings as settings_tbl

log = logging.getLogger("vodsync.panel")

BLOCK_KEY = "panel_blocked_sources"

_CACHE: dict = {}
_CACHE_LOCK = threading.Lock()
_INFLIGHT: dict = {}


# ---------------------------------------------------------------------------
# 源站屏蔽名单
#
# value 形状：{ "<slug>": {"paused_before": 0|1, "at": "ISO8601"}, ... }
# 用 dict 而不是数组，是为了记住「屏蔽前该站是否已被手动暂停」——取消屏蔽
# 时精确还原，不把用户原本的暂停状态弄丢。
# ---------------------------------------------------------------------------
def get_blocks() -> dict:
    try:
        with database.engine.connect() as conn:
            raw = conn.execute(
                select(settings_tbl.c.value).where(settings_tbl.c.key == BLOCK_KEY)
            ).scalar()
    except Exception as exc:  # noqa: BLE001 - 表还没建好时按"没有屏蔽"处理
        log.debug("读取源站屏蔽名单失败（按空处理）: %s", exc)
        return {}
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    if isinstance(data, list):          # 兼容早期数组格式
        return {str(x): {"paused_before": 0, "at": ""} for x in data if x}
    if not isinstance(data, dict):
        return {}
    return {str(k): (v if isinstance(v, dict) else {"paused_before": 0, "at": ""})
            for k, v in data.items() if k}


def set_blocks(mapping: dict) -> dict:
    clean = {str(k): {"paused_before": int((v or {}).get("paused_before") or 0),
                      "at": str((v or {}).get("at") or "")}
             for k, v in (mapping or {}).items() if k}
    now = datetime.now()
    value = json.dumps(clean, ensure_ascii=False, separators=(",", ":"))
    stmt = (
        database.dialect_insert(settings_tbl)
        .values(key=BLOCK_KEY, value=value, updated_at=now)
        .on_conflict_do_update(
            index_elements=[settings_tbl.c.key],
            set_={"value": value, "updated_at": now},
        )
    )
    with database.write_txn() as conn:
        conn.execute(stmt)
    return clean


def blocked_slugs() -> list:
    return sorted(get_blocks().keys())


# ---------------------------------------------------------------------------
# 只读快照缓存
# ---------------------------------------------------------------------------
def cached(key: str, ttl: float, producer, refresh: bool = False):
    """
    返回 `(value, age_seconds)`。

    age=0 表示刚刚现场算出来；age>0 表示命中缓存且已存在多少秒。
    `refresh=True` 强制重算（面板的「强制刷新」按钮）。
    producer 抛异常时不写缓存，下一次请求会重试。
    """
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if not refresh and hit and (time.monotonic() - hit[0]) < ttl:
            return hit[1], int(time.monotonic() - hit[0])
        lock = _INFLIGHT.setdefault(key, threading.Lock())

    with lock:
        with _CACHE_LOCK:
            hit = _CACHE.get(key)
            if not refresh and hit and (time.monotonic() - hit[0]) < ttl:
                return hit[1], int(time.monotonic() - hit[0])
        value = producer()
        with _CACHE_LOCK:
            _CACHE[key] = (time.monotonic(), value)
        return value, 0


def cache_clear(key: str = "") -> None:
    """写操作后按需让某份快照立刻失效（目前由前端显式 refresh=1 代替）。"""
    with _CACHE_LOCK:
        if key:
            _CACHE.pop(key, None)
        else:
            _CACHE.clear()
