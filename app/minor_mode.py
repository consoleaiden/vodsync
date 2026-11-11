# -*- coding: utf-8 -*-
"""
未成年模式（v1.15.0）：按源站的 18+ 标记屏蔽内容。

设计要点
--------
1. **默认全关，零侵入**。开关是「请求级」的：不带 `X-Minor-Mode` /
   `?minor_mode=1` 的请求一律走原路径，连一次额外 SQL 都不会发。现有的
   App / 采集器 / 面板调用一个字都不用改，行为与升级前完全一致。
   关闭未成年模式 = 不再带这个头，天然恢复全部展示与访问，不需要任何
   「恢复数据」动作（本功能从不删数据，只改读路径的可见集合）。

2. **标记是持久化的，屏蔽不是**。`sources.is_adult` 落库；「当前是否屏蔽」
   只由本次请求决定，没有任何全局运行状态会被写脏，重启也不需要恢复。

3. **后端硬拦截，不只是前端隐藏**。光靠前端不渲染入口，直接调 API 就绕过
   了。所以所有内容读接口（列表/详情/搜索/分类/MacCMS/推荐）都在这里统一
   过滤 slug；单源站入口用 `guard()` 挡在查表之前。管理写接口（同步、标记编辑、
   词库、Token、统计）**完全不经过本模块**；源站清单 / 单站详情属于「展示入口」，
   会被过滤 —— 面板不带开关，因此默认配置下管理操作不受影响。

4. **不做缓存**。`sources` 只有几十行，`SELECT slug WHERE is_adult=1` 在 PG
   上 <1ms；缓存反而会引入「面板刚保存、读路径还看到旧值」的一致性窗口。

5. **数据兼容**。`is_adult` 为 NULL / 0 / 任意非 1 值都按「否」处理，存量库
   靠 `database._migrate()` 补列 DEFAULT 0。`refresh_sources()` 的 INSERT 不带
   这列（走 server_default）、UPDATE 只改名/状态/描述，标记不会被刷新洗掉。

契约（前端对接）
--------------
  开启：所有请求带 `X-Minor-Mode: 1`（推荐），或 `?minor_mode=1` / `?minor=1`
        用于播放器内核、<img> 等无法自定义请求头的场景。
  关闭：不带上述参数即可；也可显式传 0/false/off 覆盖。
  能力探测：`/healthz.capabilities` 含 `minor_mode` 表示服务端已支持。
  标记下发：`/source` 与 `/api/v1/sources` 每项新增 `is_adult` 布尔值。

已知限制
--------
  请求级开关由客户端自报，技术上可被去掉头绕过。需要服务端强制管控时把部署
  环境变量 `MINOR_MODE_FORCE=true`，此时所有请求都进入未成年模式，且管理面板
  的源站清单也会隐藏 18+ 行（它走同一个 GET 接口）；FORCE 期间改标记需临时
  关闭 FORCE 或直接调 `POST /api/v1/sources/{slug}/adult`（该端点不受拦截）。
"""
import logging

from fastapi import HTTPException, Request
from sqlalchemy import func, select

from . import config, database
from .models import sources

log = logging.getLogger("vodsync.minor_mode")

_TRUE = frozenset({"1", "true", "on", "yes"})
_FALSE = frozenset({"0", "false", "off", "no", ""})
_HEADER = "x-minor-mode"
# 参数别名按优先级排列：minor_mode 为主，minor / teen_mode 兼容无法加头的场景
_QUERY_KEYS = ("minor_mode", "minor", "teen_mode")


def is_adult(value) -> bool:
    """列值归一化：只有整数 1 算「是」，NULL / 0 / 脏值一律按「否」。"""
    try:
        return int(value or 0) == 1
    except (TypeError, ValueError):
        return False


def _flag(raw) -> bool:
    return str(raw if raw is not None else "").strip().lower() in _TRUE


def requested(request: Request) -> bool:
    """本次请求是否自报「未成年模式」。头优先于查询参数；都没带 = False。"""
    if request is None:
        return False
    raw = request.headers.get(_HEADER)
    if raw is not None and str(raw).strip().lower() in (_TRUE | _FALSE):
        return _flag(raw)
    params = request.query_params
    for key in _QUERY_KEYS:
        raw = params.get(key)
        if raw is not None and str(raw).strip().lower() in (_TRUE | _FALSE):
            return _flag(raw)
    return False


def active(request: Request) -> bool:
    """最终是否生效：部署级强制开关 OR 请求级开关。"""
    return bool(config.MINOR_MODE_FORCE) or requested(request)


def blocked_slugs() -> frozenset:
    """当前被标记为 18+ 的源站 slug 集合；查询失败时返回空集并记日志。

    失败按「不屏蔽」降级而不是抛 500：库抖动时不能让整站内容接口全挂，
    标记本身是人工录入的静态数据，短暂读不到不改变已入库内容。
    """
    try:
        with database.engine.connect() as conn:
            rows = conn.execute(
                select(sources.c.slug).where(func.coalesce(sources.c.is_adult, 0) == 1)
            ).all()
        return frozenset(r[0] for r in rows)
    except Exception as e:  # noqa: BLE001
        log.warning("[未成年模式] 读取 18+ 源站标记失败，本次按不屏蔽处理: %s", e)
        return frozenset()


def source_clause(request: Request):
    """给 `select(sources...)` 追加的 WHERE 条件；未开启时返回 None。

    调用方：`cond = [sources.c.enabled == 1]; c = minor_mode.source_clause(request);
             if c is not None: cond.append(c)`
    """
    if not active(request):
        return None
    return func.coalesce(sources.c.is_adult, 0) == 0


def filter_slugs(request: Request, slugs) -> list:
    """从待查询的 slug 列表里剔除 18+ 源站；未开启时原样返回（转成 list）。"""
    out = list(slugs or [])
    if not out or not active(request):
        return out
    blocked = blocked_slugs()
    if not blocked:
        return out
    return [s for s in out if s not in blocked]


def guard(request: Request, slug: str) -> None:
    """单源站内容入口守卫：被屏蔽时抛 404。

    用「不存在」而不是 403：403 等于告诉调用方这个源站确实存在、只是被拦了，
    反而给探测提供了信息。文案与 `_require_table` 的源站不存在保持一致。
    """
    if not active(request):
        return
    if slug in blocked_slugs():
        raise HTTPException(status_code=404, detail=f"源站 {slug} 不存在或当前不可访问")


def status(request: Request) -> dict:
    """诊断用：当前请求是否生效、强制开关状态、被标记的源站数量。"""
    blocked = blocked_slugs()
    return {
        "forced": bool(config.MINOR_MODE_FORCE),
        "requested": requested(request),
        "active": active(request),
        "adult_sources": sorted(blocked),
        "adult_count": len(blocked),
    }
