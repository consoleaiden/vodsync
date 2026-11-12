"""
对外 API：
1. REST 风格：/api/v1/videos?source={slug} 列表、/api/v1/videos/{slug}/{vod_id} 详情
   —— 通过 source 参数区分源站，支持逗号分隔多源合并
2. 苹果 CMS 兼容风格：/api/v1/{slug}?ac=list|detail&pg=1&ids=1,2
   —— 输出结构与 MacCMS 采集接口一致，方便现有播放器/采集器直接对接
3. 管理接口：源站列表、手动触发同步、查看同步日志
"""
import json
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from sqlalchemy import bindparam, func, select, text
from sqlalchemy.exc import IntegrityError

from .. import __version__, adfilter, apiresp, auth, config, database, minor_mode
from ..models import build_vod_table, sources, sync_logs
from ..syncer import (
    delete_custom_source,
    ensure_table,
    probe_source,
    refresh_sources,
    register_custom_source,
    sync_all,
    sync_source,
    update_custom_source,
)
from ..utils import DETAIL_EXTRA_COLS, page_params, parse_play_lines, validate_slug

log = logging.getLogger("vodsync.api")
# 鉴权统一由 main.py 的 _auth_guard 中间件处理（Token / 会话 cookie / 开关），
# 这里不再挂路由级依赖；/api/v1/auth/* 中的 login 与 state 由中间件按白名单放行。
router = APIRouter()

# 成功响应的 code（v1.14.0 / B-11）：默认 0，可用 API_SUCCESS_CODE=1 回滚成
# v1.13 的 MacCMS 风格。全文件统一引用这个常量，避免几十处字面量各自漂移。
SUCCESS_CODE = apiresp.SUCCESS_CODE
# MacCMS 兼容接口（/api/v1/{slug}）**恒为 1**：那是苹果 CMS 采集端的约定，
# 改它会让第三方采集器/播放器直接失效，与上面的 REST 契约互不影响。
MACCMS_CODE = 1


# ---------------------------------------------------------------------------
# 访问控制：登录 / 登出 / 改密码 / 开关 / Token
# ---------------------------------------------------------------------------
def _client_ip(request: Request) -> str:
    """
    登录限流用的客户端标识。

    默认只看 TCP 对端地址：X-Forwarded-For 是客户端可以随便伪造的请求头，
    无条件采信它等于给爆破脚本发了一张「每次换个假 IP」的免死金牌（六位默认密码尤其危险）。
    确实部署在反代之后时，把 TRUST_PROXY=true，并取 XFF 的**最后一跳**——
    那是我们信任的反代追加的；取第一段仍然可被伪造。
    """
    peer = request.client.host if request.client else "unknown"
    if not config.TRUST_PROXY:
        return peer
    fwd = request.headers.get("x-forwarded-for", "")
    if not fwd:
        return peer
    last = fwd.split(",")[-1].strip()
    return last or peer


def _set_session_cookie(response: Response) -> None:
    response.set_cookie(
        auth.COOKIE,
        auth.make_session(),
        max_age=config.SESSION_TTL_DAYS * 86400,
        httponly=True,       # JS 读不到，降低 XSS 窃取风险
        samesite="lax",
        path="/",
    )


@router.post("/api/v1/auth/login", summary="管理面板登录")
def auth_login(payload: dict, request: Request, response: Response):
    ip = _client_ip(request)
    wait = auth.login_lock_seconds(ip)
    if wait:
        raise HTTPException(
            status_code=429,
            detail=f"尝试次数过多，请 {wait} 秒后再试",
            headers={"Retry-After": str(wait)},
        )

    if not auth.verify_password((payload or {}).get("password", "")):
        left = auth.note_login_fail(ip)
        time.sleep(0.25)   # 拖慢脚本爆破
        detail = "密码错误" if left else f"密码错误次数过多，请 {config.LOGIN_LOCK_MINUTES} 分钟后再试"
        raise HTTPException(status_code=401, detail=detail)

    auth.reset_login_fail(ip)
    _set_session_cookie(response)
    return {"ok": True, "enabled": auth.is_enabled()}


@router.post("/api/v1/auth/logout", summary="退出登录")
def auth_logout(response: Response):
    response.delete_cookie(auth.COOKIE, path="/")
    return {"ok": True}


@router.get("/api/v1/auth/state", summary="鉴权状态（登录页/面板启动时用）")
def auth_state(request: Request, response: Response):
    """
    这个接口在中间件白名单里（登录页要靠它决定是否渲染表单），因此**未登录也能调**。
    所以按登录态分两级返回：

    - 未登录：只给 `enabled` / `logged_in`，够登录页判断跳转即可；
    - 已登录：再补 Token 是否存在、Token 与密码的变更时间。

    返回值里不含 Token 本身，也不含 Token 的任何片段 —— 状态接口不是凭据回显口，
    真正要拿 Token 请用 `GET /api/v1/auth/token`（需登录）。
    """
    enabled = auth.is_enabled()
    logged_in = auth.has_session(request) or not enabled
    state = {"enabled": enabled, "logged_in": logged_in}
    if logged_in:
        state.update(
            {
                "has_token": auth.token_present(),
                "password_changed_at": auth.password_changed_at(),
                "token_created_at": auth.token_created_at(),
            }
        )
    # 状态随登录态而变，别让任何代理/浏览器缓存它
    response.headers["Cache-Control"] = "no-store"
    return state


@router.post("/api/v1/auth/password", summary="修改管理面板密码")
def auth_change_password(payload: dict, response: Response):
    payload = payload or {}
    old, new = payload.get("old", ""), payload.get("new", "")
    if not auth.verify_password(old):
        raise HTTPException(status_code=400, detail="当前密码不正确")
    if len(new or "") < 6:
        raise HTTPException(status_code=400, detail="新密码至少 6 位")
    if new == old:
        raise HTTPException(status_code=400, detail="新密码不能与当前密码相同")
    auth.set_password(new)
    _set_session_cookie(response)   # 密码指纹变了，旧会话失效，这里补发新会话
    log.info("管理面板密码已修改")
    return {"ok": True}


@router.post("/api/v1/auth/settings", summary="启用/关闭授权访问")
def auth_settings(payload: dict):
    enabled = bool((payload or {}).get("enabled"))
    was = auth.is_enabled()
    auth.set_enabled(enabled)
    token = None
    if enabled and not was:
        # 关闭期间任何人都能拿到旧 Token，启用时换一枚新的，避免"开着的门配着旧钥匙"
        token = auth.rotate_token()
    log.info("访问控制已%s", "启用" if enabled else "关闭")
    # v1.14.1：补 B-11 包络的 code 字段。这曾是唯一一个「成功响应既没 code 也没
    # list/detail」的写接口，第三方客户端容易漏判（审计 M3）。ok 保留兼容旧面板。
    return {"code": SUCCESS_CODE, "ok": True, "enabled": enabled, "token": token}


@router.get("/api/v1/auth/token", summary="查看 API Token（需登录）")
def auth_get_token():
    return {
        "code": SUCCESS_CODE,
        "token": auth.get_token(),
        "scope": auth.token_scope(),
        "created_at": auth.token_created_at(),
    }


@router.post("/api/v1/auth/token", summary="重置 API Token（旧 Token 立即失效）")
def auth_rotate_token(
    scope: str = Query("", description="read / admin；留空=admin（与 v1.13 行为一致）"),
    payload: dict = None,
):
    """
    重置存量单 Token（v1.14.0 起可指定权限）。

    `scope` 既是查询参数也接受 body `{"scope": "read"}` —— 面板用前者，
    App 控制台习惯用后者，两个都收不会造成歧义（body 优先）。
    注意：**重置会立刻让旧 Token 失效**，正在用旧 Token 的客户端会 401。
    想「新增一枚只读 Token 而不动现有的」，用 POST /api/v1/auth/tokens。
    """
    want = ((payload or {}).get("scope") or scope or "").strip().lower()
    if want and want not in auth.SCOPES:
        raise HTTPException(status_code=400, detail="scope 只能是 read 或 admin")
    token = auth.rotate_token(want or "admin")
    return {
        "code": SUCCESS_CODE,
        "token": token,
        "scope": auth.token_scope(),
        "created_at": auth.token_created_at(),
    }


@router.get("/api/v1/auth/tokens", summary="已签发 Token 清单（不返回明文）")
def auth_list_tokens():
    """
    列出所有可用 Token 的权限与签发时间。

    第 0 项是存量单 Token（id=0，kind=legacy），其余是 v1.14.0 起的具名 Token。
    任何一项都**不含明文**：清单接口不是凭据回显口。
    """
    return {"code": SUCCESS_CODE, "list": auth.list_tokens()}


@router.post("/api/v1/auth/tokens", summary="新增具名 Token（可指定权限）")
def auth_create_token(payload: dict = None):
    """
    新增一枚 Token，返回**一次性明文**。

    与「重置」的区别：不影响现有 Token，适合给 App 发一枚 read Token，
    同时把 admin Token 留给同步控制台（B-08 的目标模型）。
    """
    payload = payload or {}
    scope = (payload.get("scope") or "read").strip().lower()
    if scope not in auth.SCOPES:
        raise HTTPException(status_code=400, detail="scope 只能是 read 或 admin")
    info = auth.create_scoped_token(scope, payload.get("note") or "")
    return {"code": SUCCESS_CODE, **info}


@router.delete("/api/v1/auth/tokens/{token_id}", summary="吊销具名 Token")
def auth_revoke_token(token_id: int):
    if not auth.revoke_scoped_token(token_id):
        raise HTTPException(
            status_code=404,
            detail="Token 不存在、已吊销，或是不可单独吊销的存量单 Token（id=0）",
        )
    return {"code": SUCCESS_CODE, "revoked": token_id}


# ---------------------------------------------------------------------------
# 内部查询工具
# ---------------------------------------------------------------------------
def _require_table(slug: str):
    slug = validate_slug(slug)
    with database.engine.connect() as conn:
        src = conn.execute(select(sources).where(sources.c.slug == slug)).mappings().first()
    if not src:
        raise HTTPException(status_code=404, detail=f"源站 {slug} 不存在")
    table = build_vod_table(slug)
    if not database.table_exists(table.name):
        raise HTTPException(status_code=404, detail=f"源站 {slug} 尚未同步任何数据")
    return src, table


def _maccms_class_map(conn, table):
    """从数据表推导分类列表（type_id, type_name 去重）。"""
    rows = conn.execute(
        select(table.c.type_id, table.c.type_name)
        .distinct()
        .order_by(table.c.type_id)
    ).all()
    return [{"type_id": r[0], "type_name": r[1]} for r in rows if r[1]]


def _esc_like(kw: str) -> str:
    """转义 LIKE 通配符（\\ % _），避免用户输入变成全表匹配。"""
    return kw.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _query_vods(
    conn,
    table,
    slug: str,
    page: int,
    page_size: int,
    type_id: int = None,
    wd: str = None,
    detail: bool = False,
    play_raw: bool = False,
    play_meta: dict = None,
):
    """
    通用分页查询：按更新时间倒序。返回 (total, pagecount, items)。

    详情模式下条目统一由 `_detail_item()` 构造（v1.11.0）—— 之前这里是第二份
    手写的字段列表，加了新字段忘了同步就会出现「同一份详情结构，走这条路少几个键」。
    """
    where = []
    if type_id:
        where.append(table.c.type_id == type_id)
    if wd:
        # v1.10.0 / C3：原来没转义通配符，搜 `%` 或 `_` 会变成全表匹配；
        # 另外 params 里存了值却从没绑定（死代码），一并去掉。
        where.append(table.c.name.like(f"%{_esc_like(wd)}%", escape="\\"))

    base = select(table).where(*where) if where else select(table)
    total = conn.execute(
        select(func.count()).select_from(base.subquery())
    ).scalar() or 0
    pagecount = (total + page_size - 1) // page_size

    cols = [
        table.c.vod_id,
        table.c.type_id,
        table.c.type_name,
        table.c.name,
        table.c.pic,
        table.c.remarks,
        table.c.vod_time,
    ]
    if detail:
        cols += [table.c.content, table.c.play_url]
        cols += [table.c[c] for c in DETAIL_EXTRA_COLS]
    q = (
        select(*cols)
        .where(*where)
        .order_by(table.c.vod_time.desc(), table.c.vod_id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    rows = conn.execute(q).mappings().all()

    items = []
    for r in rows:
        if detail:
            items.append(_detail_item(slug, r, play_raw=play_raw, play_meta=play_meta))
            continue
        items.append(_list_item(r))
    return total, pagecount, items


# ---------------------------------------------------------------------------
# 参数与输出辅助
# ---------------------------------------------------------------------------
def _qp(request: Request, name: str, default: str = "") -> str:
    """
    读取查询参数，兼容"多打一个问号"的写法。

    背景：URL 写成 `?source=jkun&?ac=detail` 时，第二个参数的键实际是 `?ac`，
    框架会把 `ac` 当成未传而静默忽略，用户容易误以为接口不返回详情。
    这里同时匹配 `ac` 与 `?ac` 两种键名，避免参数丢失。
    """
    qp = request.query_params
    for key in (name, "?" + name):
        val = qp.get(key)
        if val not in (None, ""):
            return val
    return default


def _pick(row, key):
    """
    容错取列值（v1.10.1）。

    `_detail_item` 有 3 个调用点，其中「/videos 多源归并列表」的行是 multiquery
    自己组装的 dict（THIN_COLS + DETAIL_COLS），不是完整表行 —— 如果以后有人往
    这里加字段却忘了同步 multiquery，直接 `row["xxx"]` 就是 KeyError，
    会把整个列表接口打挂（500）。

    这里同时兼容 SQLAlchemy 的 Row / RowMapping（走 `_mapping.get`）与普通 dict，
    取不到或为 None 时统一返回空串，客户端不必再判 null。
    """
    m = getattr(row, "_mapping", row)
    try:
        return m.get(key) or ""
    except Exception:
        return ""


def _list_item(row, source: str = "", source_name: str = "") -> dict:
    """
    列表卡片（瘦字段）的**唯一**构造函数（v1.14.0）。

    为什么集中：改版前有 5 处各手写一份同样的 dict，加字段时必然漏一两处，
    于是「同一个 /api/v1/videos，走多源归并和走标签检索字段不一样」，客户端
    某些路径下卡片缺字段。v1.12.2 的渲染 bug 就是这类分叉引起的。
    集中后新增字段只改这里。

    字段名同时给契约名与历史名（`remarks` / `vod_remarks`，见
    BACKEND_UPGRADE_REQUIREMENTS_2026-11-09.md §2.1），客户端用哪个都行；
    两个都给是为了不打断任何一版客户端。

    source / source_name 按调用方需要加：MacCMS 兼容接口
    （/api/v1/{slug}）的 list 里没有 source，保持原样。
    """
    vod_remarks = _pick(row, "remarks")
    item = {
        "vod_id": _pick(row, "vod_id"),
        "type_id": _pick(row, "type_id"),
        "type_name": _pick(row, "type_name"),
        "vod_name": _pick(row, "name"),
        "vod_pic": _pick(row, "pic"),
        "vod_remarks": vod_remarks,
        "remarks": vod_remarks,
        # 客户端列表卡片要「地区·年份」（契约 §2.1）
        "vod_area": _pick(row, "vod_area"),
        "vod_year": _pick(row, "vod_year"),
        "vod_time": _pick(row, "vod_time"),
    }
    if source:
        item["source"] = source
    if source_name:
        item["source_name"] = source_name
    return item


def _play_lines(slug: str, row, play_raw: bool = False) -> tuple:
    """
    结构化播放线路（v1.11.0）：识别类型 + 按策略过滤/排序，返回 `(lines, meta)`。

    play_raw=True 走 off 策略：不改顺序、不过滤，用于排障（`?play_raw=1`）。
    过滤/排序只影响 vod_play_list；`vod_play_url` 原始串始终原样下发。
    """
    from .. import playline

    policy = "off" if play_raw else playline.effective_policy(slug)
    lines, meta = parse_play_lines(
        _pick(row, "play_url"), _pick(row, "play_from"), policy=policy
    )
    if not play_raw:
        playline.note(slug, meta)   # 读侧计数（/api/v1/stats 的 play_line 段）
    return lines, meta


def _detail_item(source: str, row, *, play_raw: bool = False,
                 play_meta: dict = None) -> dict:
    """
    详情条目：原始播放地址 + 结构化播放列表（列表接口与详情接口共用）。

    play_meta 传一个 dict 进来时，会把「本次是否真的过滤掉了线路」写进去
    （`play_meta["filtered"] = True`），供调用方在响应顶层打 `play_line_filtered`。
    不传则只影响本条目的 vod_play_list。
    """
    lines, meta = _play_lines(source, row, play_raw)
    if play_meta is not None and meta.get("filtered"):
        play_meta["filtered"] = True
    return {
        "source": source,
        "vod_id": row["vod_id"],
        "type_id": row["type_id"],
        "type_name": row["type_name"],
        "vod_name": row["name"],
        "vod_pic": row["pic"],
        "vod_remarks": row["remarks"],
        "vod_time": row["vod_time"],
        # v1.10.1：演职员 / 地区 / 年代 / 明星标签（纯新增字段）。
        # 这些列 v1.9.0 起就在库里有，只是一直没透出 —— NAS 模式下客户端拿不到，
        # 演员只能靠 star/related 的 stars 兜底，标签则没有任何数据源、整行隐藏。
        # 取值必须走 _pick（见其 docstring）：3 个调用点里有一路的行没有这些列。
        "vod_actor": _pick(row, "vod_actor"),
        "vod_director": _pick(row, "vod_director"),
        "vod_area": _pick(row, "vod_area"),
        "vod_year": _pick(row, "vod_year"),
        "vod_tag": _pick(row, "vod_tag"),
        "vod_content": _pick(row, "content"),
        "vod_play_url": _pick(row, "play_url"),
        # MacCMS 语义的「播放源名称」。存量行为一直是常量 "vodsync"，这里保持不变
        # （改成上游 provider 串会打断依赖该常量的老客户端）；真实 provider 见
        # 下面 play_from 以及每条线路自带的 from。
        "vod_play_from": "vodsync",
        # v1.11.0：上游 vod_play_from 原始串（"feifan$$$ffm3u8"），与 vod_play_url
        # 的线路顺序一一对应。存量行为空串（未回填）。
        "play_from": _pick(row, "play_from"),
        "vod_play_list": lines,
        # v1.14.0 / B-03：客户端契约里这个字段叫 `play_lines`（见
        # BACKEND_UPGRADE_REQUIREMENTS_2026-11-09.md §2.2）。历史实现一直叫
        # `vod_play_list`（MacCMS 风格，文档与面板都在用），两边都不能删：
        #   - 改名 -> 现网 v1.5.x 客户端拿不到线路，详情页整块空白；
        #   - 只留旧名 -> 按新契约写的客户端同样空白。
        # 所以同值双发：**同一个 list 对象**，Python 侧不多占内存，JSON 里重复
        # 的部分 gzip 会压掉（两份内容完全一样，压缩率接近 0 成本）。
        "play_lines": lines,
        # 同上：契约里详情标签字段叫 `tag`，库里与老接口叫 `vod_tag`
        "tag": _pick(row, "vod_tag"),
    }


# ---------------------------------------------------------------------------
# 分类接口：供客户端做「分类浏览」入口
# ---------------------------------------------------------------------------
# 分类清单缓存（v1.10.0 / P10）：不带参数时要做 52 次 GROUP BY（几百 ms），
# 而分类几乎不变 —— 缓存 CATEGORIES_CACHE_SECONDS 秒，面板/App 反复进入分类页不再打库。
_categories_cache: dict = {}
_categories_lock = threading.Lock()


def reset_categories_cache() -> None:
    """清空分类缓存（同步/删源站后由调用方按需触发）。"""
    with _categories_lock:
        _categories_cache.clear()


@router.get("/api/v1/categories")
def categories(
    request: Request,
    source: str = Query("", description="源站 slug，逗号分隔多源；留空=全部源站合并"),
):
    """
    分类清单（type_id / type_name 去重 + 各分类条数）。

    用法：
      /api/v1/categories?source=jkun          单源站分类
      /api/v1/categories?source=jkun,t1       多源合并（同名分类会合并计数）
      /api/v1/categories                      全部源站合并
    拿到 type_id 后即可用 GET /api/v1/videos?source=..&type_id=.. 按分类翻页。

    v1.10.0（纯新增）：响应带 `cached`（true = 命中本地缓存，分类几乎不变，
    默认缓存 CATEGORIES_CACHE_SECONDS=300 秒）。客户端无需处理，仅供排查。
    """
    from collections import defaultdict

    raw = [s.strip() for s in source.split(",") if s.strip()] if source else None
    if raw:
        for s in raw:
            validate_slug(s)
        # v1.15.0：未成年模式下显式指定的 18+ 源站也要剔除
        slugs = minor_mode.filter_slugs(request, raw)
    else:
        clause = minor_mode.source_clause(request)
        cond = [sources.c.enabled == 1] + ([clause] if clause is not None else [])
        with database.engine.connect() as conn:
            slugs = [r[0] for r in conn.execute(
                select(sources.c.slug).where(*cond)
            ).all()]

    # 缓存 key 必须带未成年模式状态：否则开启/关闭两种模式会串用同一份
    # 分类结果，关掉后仍看到被屏蔽站的分类数（或反之）。
    cache_key = (tuple(slugs), source, minor_mode.active(request))
    ttl = config.CATEGORIES_CACHE_SECONDS
    if ttl > 0:
        with _categories_lock:
            hit = _categories_cache.get(cache_key)
        if hit and hit[0] > time.time():
            return {"code": SUCCESS_CODE, "total": len(hit[1]), "list": hit[1], "cached": True}

    merged: dict = defaultdict(lambda: {"type_id": None, "type_name": "", "count": 0, "sources": []})
    existing = database.list_tables("vod_")   # v1.10.0 / P6：一次取全，别逐表往返
    with database.engine.connect() as conn:
        for slug in slugs:
            table = build_vod_table(slug)
            if table.name not in existing:
                continue
            rows = conn.execute(
                select(table.c.type_id, table.c.type_name, func.count())
                .group_by(table.c.type_id, table.c.type_name)
            ).all()
            for type_id, type_name, cnt in rows:
                if not type_name:
                    continue
                key = f"{type_name}"
                item = merged[key]
                item["type_name"] = type_name
                if item["type_id"] is None or (type_id or 0) > 0:
                    item["type_id"] = type_id
                item["count"] += cnt
                item["sources"].append(slug)

    items = sorted(
        (
            {"type_id": v["type_id"], "type_name": v["type_name"],
             "count": v["count"], "sources": v["sources"]}
            for v in merged.values()
        ),
        key=lambda x: (x["type_id"] or 0),
    )
    if ttl > 0:
        with _categories_lock:
            if len(_categories_cache) > 32:
                _categories_cache.clear()
            _categories_cache[cache_key] = (time.time() + ttl, items)
    return {"code": SUCCESS_CODE, "total": len(items), "list": items, "cached": False}


# ---------------------------------------------------------------------------
# 管理接口
# ---------------------------------------------------------------------------
@router.get("/healthz")
def healthz(detail: int = Query(0, ge=0, le=1,
                                description="v1.14.0：1=capabilities 以对象形态返回（运维排障用）")):
    """
    健康检查 / 存活探针 / **能力清单**（v1.14.0 / B-10）。

    关键点（三条都不能破）：
    1. 数据库繁忙时**不返回 5xx**。容器健康检查只看 HTTP 状态码，若这里因为
       连接池排队而 500/超时，Docker 会把一个其实还在正常干活的容器判定为不健康
       并反复重启（v1.4.0 在 NAS 上就表现为"容器崩溃"）。所以恒 200，
       用 status=degraded + db=false 表达数据库不可用。
    2. 免鉴权（在 auth.PUBLIC_PATHS 里）：客户端连接测试用它测延迟，
       不能因为没 Token 就打不进来。
    3. `capabilities` 只声明**真正可用**的能力，且数据库不可用时收缩。
       客户端据此决定「能不能用 ?tag= / mode=backfill_play_from」，
       声明了但没实现等于骗客户端切过去。

    返回：status / db / version / build / capabilities / time
      version 取自 app/__init__.py（与镜像 tag 同源），能力变更时升 minor。
    """
    from .. import caps

    now = datetime.now().isoformat()
    build = os.environ.get("VODSYNC_BUILD_SHA", "") or "dev"
    try:
        with database.engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return {
            "status": "ok",
            "db": True,
            "version": __version__,
            "build": build,
            "capabilities": caps.snapshot(db_ok=True) if not detail else caps.detailed(True),
            "time": now,
        }
    except Exception as e:
        log.warning("/healthz 数据库暂不可用: %s", e)
        return {
            "status": "degraded",
            "db": False,
            "version": __version__,
            "build": build,
            # 库挂了：依赖数据的能力一律不声明（tag_search 需要查 video_tags），
            # 只剩「代码上线即成立」的静态项
            "capabilities": caps.snapshot(db_ok=False) if not detail else caps.detailed(False),
            "detail": str(e)[:200],
            "time": now,
        }


@router.get("/healthz.capabilities", summary="能力清单独立端点（免鉴权，客户端能力门控用）")
def healthz_capabilities():
    """
    `/healthz.capabilities` 作为**真实 URL**存在（v1.14.1 修）。

    背景：v1.14.0 文档里 `/healthz.capabilities` 本意是「/healthz 响应中的
    capabilities 字段」的字段路径写法，但客户端（和文档自己的行文）会把它当
    独立 URL 调 —— 而这个路径不在路由表里，鉴权中间件对非 /api/ 路径又只认
    会话 cookie，于是 App 拿着合法 Token 也吃 401。

    修复：把端点做出来（与 /healthz 同级的免鉴权路径，已在 auth.PUBLIC_PATHS），
    响应与 /healthz 的能力部分完全一致。已用 /healthz.capabilities 字段的老
    客户端不受影响；两个入口读的是同一份 caps.snapshot()。
    """
    from .. import caps

    try:
        with database.engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        db_ok = True
    except Exception:  # noqa: BLE001
        db_ok = False
    return {
        "status": "ok" if db_ok else "degraded",
        "db": db_ok,
        "version": __version__,
        "capabilities": caps.snapshot(db_ok=db_ok),
        "time": datetime.now().isoformat(),
    }


def _is_synced(row) -> bool:
    """
    该源站是否"同步过"：有水位线 / 已入库 / 有断点，任一成立即视为同步过。
    三者皆无 = 从未开始同步（进度应为 0，不能显示"已完成"）。
    """
    return bool(row["watermark"]) or (row["total_videos"] or 0) > 0 or (row["last_page"] or 0) > 0


def compute_progress(row, live: dict = None) -> float:
    """
    全量同步进度百分比（0-100）。

    口径（按顺序判定）：
      1. 从未同步过（无水位线、无数据、无断点）→ 0
      2. 有断点且有总页数 → 断点占比
      3. 有断点但暂无总页数 → 0（刚拿到断点，尚未读到总页数）
      4. 正在跑全量且还没到第一个检查点 → 0
      5. 其余（已同步过且无断点）→ 100（已完成）
    """
    if not _is_synced(row):
        return 0.0
    if row["last_page"] and row["total_pages"]:
        return round(row["last_page"] * 100 / row["total_pages"], 1)
    if row["last_page"]:
        return 0.0
    if live and live.get("mode") == "full":
        return 0.0
    return 100.0


@router.get("/api/v1/sources")
def list_sources(request: Request):
    """
    所有源站及本地同步状态。

    字段口径（易混淆，特意分开）：
      running       此刻是否有同步任务在跑（只有 true 才应该提供「暂停」操作）
      running_mode  正在跑的类型：full / incremental
      running_seconds 本次已运行秒数
      syncing       全量同步是否「未跑完」（有断点），不代表正在跑，仅用于展示进度条
      synced        是否同步过（无水位线、无数据、无断点 = false，进度恒为 0）
      paused        该源站是否被单独暂停（持久化）

    v1.15.0：每项新增 `is_adult`（该源站是否含 18+ 内容，布尔）。请求带
    `X-Minor-Mode: 1` 时本清单会直接剔除 18+ 源站，并带顶层 `minor_mode: true`。
    管理面板不带该头，因此始终能看到并编辑全部源站 —— 管理操作不受影响。
    """
    from .. import playline, syncer

    running = syncer.running_snapshot()
    queued = syncer.queued_slugs()
    line_stats = playline.stats_snapshot()["by_source"]

    clause = minor_mode.source_clause(request)
    cond = [clause] if clause is not None else []
    with database.engine.connect() as conn:
        rows = conn.execute(
            select(
                sources.c.slug, sources.c.name, sources.c.api_url, sources.c.status,
                sources.c.enabled, sources.c.total_videos, sources.c.watermark,
                sources.c.last_page, sources.c.total_pages, sources.c.last_sync_at,
                sources.c.paused, sources.c.paused_at, sources.c.auto_sync,
                sources.c.origin, sources.c.fail_streak, sources.c.skip_until,
                sources.c.play_line_policy, sources.c.is_adult,
            ).where(*cond).order_by(sources.c.id)
        ).mappings().all()
    return {
        "code": SUCCESS_CODE,
        # 纯新增字段：老客户端忽略即可；前端用它确认「当前清单是否已按未成年模式过滤」
        "minor_mode": minor_mode.active(request),
        "list": [
            {
                "slug": r["slug"],
                "name": r["name"],
                "is_adult": minor_mode.is_adult(r["is_adult"]),
                "api_url": r["api_url"],
                "origin": r["origin"] or "latest",
                "status": r["status"],
                "enabled": bool(r["enabled"]),
                "total_videos": r["total_videos"],
                "watermark": r["watermark"],
                "syncing": r["last_page"] > 0,   # 有断点说明全量未完成（不代表正在跑）
                # 真实运行态：此刻是否有任务在跑（决定是否显示「暂停」按钮）
                "running": r["slug"] in running,
                "running_mode": running.get(r["slug"], {}).get("mode"),
                "running_seconds": running.get(r["slug"], {}).get("elapsed_seconds"),
                # 已排队等待并发名额（还没开始跑）：前端可显示「排队中」
                "queued": r["slug"] in queued,
                # 是否同步过：false 表示从未开始，前端应显示「未同步」而非「已完成」
                "synced": _is_synced(r),
                # 全量同步进度（0-100）；从未同步=0，已完成=100
                "progress": compute_progress(r, running.get(r["slug"])),
                "last_page": r["last_page"],
                "total_pages": r["total_pages"],
                "last_sync_at": r["last_sync_at"].isoformat() if r["last_sync_at"] else None,
                # 单源站暂停（该源站独立于全局暂停，两者任一命中即不参与同步）
                "paused": bool(r["paused"]),
                "paused_at": r["paused_at"].isoformat() if r["paused_at"] else None,
                # 自动同步开关：false 时不参与定时任务与全站同步（手动单站同步不受影响）
                "auto_sync": bool(r["auto_sync"]),
                # 健康度退避（v1.8.1）：连续大面积失败次数 + 退避截止时间
                "fail_streak": r["fail_streak"] or 0,
                "skip_until": r["skip_until"].isoformat() if r["skip_until"] else None,
                # 播放线路策略（v1.11.0）：inherit 跟随全局；面板用这个值渲染下拉框
                "play_line_policy": r["play_line_policy"] or "inherit",
                "play_line_effective": (
                    r["play_line_policy"] if (r["play_line_policy"] or "inherit") != "inherit"
                    else playline.global_policy()
                ),
                # 读侧累计（本次进程启动以来）：面板显示「线路 2→1（过滤 1 条 page）」
                "play_line": line_stats.get(r["slug"], {"lines": 0, "dropped": 0, "fallback": 0}),
            }
            for r in rows
        ],
    }


@router.get("/source")
def source_catalog(
    request: Request,
    status: str = Query("", description="按状态过滤：ok / down，留空=全部"),
    only_ready: bool = Query(False, description="仅返回本地已有数据的源站"),
):
    """
    资源站 API 清单。

    对外提供「当前服务掌握的所有上游资源站」及其采集接口地址，方便下游客户端
    自行直连上游，或改用本服务的统一接口（local_api / local_list_api）。

    返回字段说明：
      api             上游苹果 CMS 采集接口地址（可直接 ?ac=list / ?ac=detail 调用）
      local_api       本服务的 MacCMS 兼容接口（同样支持 ?ac=list|detail）
      local_list_api  本服务的 REST 列表接口
      video_count     本服务已同步入库的条数（未同步为 0）
      synced          是否已同步过（有水位线或已入库）
      is_adult        v1.15.0：该源站是否包含 18+ 内容（布尔）

    v1.15.0：请求带 `X-Minor-Mode: 1`（或 `?minor_mode=1`）时，本清单直接从
    服务端剔除 18+ 源站 —— App 初始化时拿到的就是「可见源站全集」，不会漏出
    展示入口。不带该头时行为与升级前完全一致（含全部源站）。
    """
    clause = minor_mode.source_clause(request)
    cond = [sources.c.enabled == 1] + ([clause] if clause is not None else [])
    with database.engine.connect() as conn:
        rows = conn.execute(
            select(
                sources.c.name, sources.c.slug, sources.c.api_url, sources.c.homepage,
                sources.c.description, sources.c.status, sources.c.enabled,
                sources.c.total_videos, sources.c.watermark, sources.c.last_sync_at,
                sources.c.is_adult,
            )
            .where(*cond)
            .order_by(sources.c.total_videos.desc(), sources.c.id)
        ).mappings().all()

    items = []
    for r in rows:
        if status and (r["status"] or "") != status:
            continue
        if only_ready and not r["total_videos"]:
            continue
        items.append({
            "name": r["name"],
            "slug": r["slug"],
            "api": r["api_url"],
            "homepage": r["homepage"] or "",
            "status": r["status"] or "ok",
            "description": r["description"] or "",
            "video_count": r["total_videos"] or 0,
            "synced": bool(r["watermark"]) or bool(r["total_videos"]),
            "last_sync_at": r["last_sync_at"].isoformat() if r["last_sync_at"] else None,
            "local_api": f"/api/v1/{r['slug']}",
            "local_list_api": f"/api/v1/videos?source={r['slug']}",
            # v1.15.0：纯新增字段，老客户端忽略；前端据此自行标记/隐藏入口
            "is_adult": minor_mode.is_adult(r["is_adult"]),
        })

    return {
        "code": SUCCESS_CODE,
        # 纯新增：让客户端确认这份清单是否已按未成年模式过滤
        "minor_mode": minor_mode.active(request),
        "total": len(items),
        "alive": sum(1 for x in items if x["status"] == "ok"),
        "ready": sum(1 for x in items if x["video_count"] > 0),
        "server_time": datetime.now().isoformat(),
        "list": items,
    }


# ---------------------------------------------------------------------------
# 18+ 内容标记 / 未成年模式（v1.15.0）
#
# 这两个端点注册在 `/api/v1/{slug}`（MacCMS 兼容通配路由）之前 —— 否则
# `GET /api/v1/minor-mode` 会被当成 slug="minor-mode" 吃进采集接口返回 404。
# ---------------------------------------------------------------------------
@router.get("/api/v1/minor-mode", summary="未成年模式状态（能力诊断用）")
def minor_mode_status(request: Request):
    """
    返回当前请求是否处于未成年模式、部署级强制开关是否打开、以及被标记为
    18+ 的源站列表。纯只读，不改变任何状态；供面板与客户端排障使用。
    """
    return {"code": SUCCESS_CODE, **minor_mode.status(request)}


@router.post("/api/v1/sources/{slug}/adult", summary="设置源站 18+ 内容标记")
def set_source_adult(
    slug: str,
    enabled: str = Query(
        "", description="true/false 或 1/0；也支持 body {\"enabled\": true}"
    ),
    payload: dict = None,
):
    """
    持久化单个源站的「是否包含 18+ 视频内容」标记（`sources.is_adult`）。

    - 只改这一列，不动同步状态、水位线、断点与已入库数据，也不需要重新同步；
    - 立即生效：读路径每次实时查 `is_adult`，没有缓存窗口；
    - 关闭（enabled=false）后，未成年模式下该源站立刻恢复展示与访问；
    - 这是**管理操作**，不受未成年模式影响（面板不带 X-Minor-Mode 头）。

    兼容性与请求级开关一致：面板用查询参数，App 控制台习惯用 body，两者都收，
    body 优先。缺参返回 400 —— 避免误把标记清成「否」。
    """
    slug = validate_slug(slug)
    raw = (payload or {}).get("enabled")
    if raw is None:
        raw = enabled
    if raw is None or str(raw).strip() == "":
        raise HTTPException(status_code=400, detail="必须提供 enabled=true 或 enabled=false")
    if isinstance(raw, bool):
        flag = raw
    else:
        val = str(raw).strip().lower()
        if val not in ("true", "false", "1", "0", "yes", "no", "on", "off"):
            raise HTTPException(status_code=400, detail="enabled 只能是 true 或 false")
        flag = val in ("true", "1", "yes", "on")

    with database.engine.connect() as conn:
        row = conn.execute(
            select(sources.c.name).where(sources.c.slug == slug)
        ).first()
    if not row:
        raise HTTPException(status_code=404, detail=f"源站 {slug} 不存在")
    with database.write_txn() as conn:
        conn.execute(
            sources.update()
            .where(sources.c.slug == slug)
            .values(is_adult=1 if flag else 0)
        )
    # 分类清单有 CATEGORIES_CACHE_SECONDS 秒的进程内缓存，且 key 带未成年模式
    # 状态。标记变了必须清一次，否则面板刚保存完，未成年模式下的分类结果
    # 可能还在用旧的可见源站集合（最长残留一个 TTL）。
    reset_categories_cache()
    log.info("[未成年模式] 源站 %s 18+ 标记 = %s", slug, flag)
    return {
        "code": SUCCESS_CODE,
        "msg": f"源站 {row[0]} 已标记为{'包含' if flag else '不包含'} 18+ 内容",
        "slug": slug,
        "is_adult": flag,
    }


@router.post("/api/v1/sources/adult", summary="批量设置源站 18+ 内容标记")
def bulk_set_source_adult(
    enabled: bool = Query(..., description="true=标记为包含 18+，false=标记为不包含"),
    slugs: str = Query("all", description="逗号分隔的源站 slug；all 或留空=全部源站"),
):
    """
    批量把源站的「是否包含 18+ 视频内容」置为同一个值（`sources.is_adult`）。

    与单站端点语义完全一致，只是把多次往返合成一次；用途是管理面板勾选多行后
    一次改完。**只改这一列**，不动同步状态/水位线/断点/已入库数据，也无需重新同步。

    用法：
      POST /api/v1/sources/adult?enabled=true&slugs=jkun,t1,bfzytv   勾选的站标记为 18+
      POST /api/v1/sources/adult?enabled=false&slugs=jkun,t1        取消标记
      POST /api/v1/sources/adult?enabled=false&slugs=all            全部改回「否」

    显式点名的 slug 里只要有一个不存在就整批拒绝（400/404），不做「部分成功」——
    避免面板里勾选的行和实际改动对不上。
    """
    raw = (slugs or "").strip()
    slug_list = None
    if raw and raw.lower() not in ("all", "*"):
        slug_list = [s.strip() for s in raw.split(",") if s.strip()] or None
        for s in slug_list or []:
            validate_slug(s)

    if slug_list:
        with database.engine.connect() as conn:
            known = {
                r[0] for r in conn.execute(
                    select(sources.c.slug).where(sources.c.slug.in_(slug_list))
                ).all()
            }
        missing = [s for s in slug_list if s not in known]
        if missing:
            raise HTTPException(
                status_code=404,
                detail=f"源站不存在: {', '.join(missing[:5])}"
                       + ("…" if len(missing) > 5 else ""),
            )

    flag = 1 if enabled else 0
    with database.write_txn() as conn:
        stmt = sources.update().values(is_adult=flag)
        if slug_list:
            stmt = stmt.where(sources.c.slug.in_(slug_list))
        n = conn.execute(stmt).rowcount or 0

    # 同单站端点：分类清单有进程内缓存，标记变了必须清一次
    reset_categories_cache()
    log.info(
        "[未成年模式] 批量 18+ 标记 = %s，影响 %s 个源站（%s）",
        enabled, n, "all" if not slug_list else f"{len(slug_list)} 个指定源站",
    )
    return {
        "code": SUCCESS_CODE,
        "msg": f"已将 {n} 个源站标记为{'包含' if enabled else '不包含'} 18+ 内容",
        "updated": n,
        "is_adult": enabled,
    }


# ---------------------------------------------------------------------------
# 智能去广告（adfilter，v1.16.0）
#
# 控制面两条接口 + 一组管理端：
#   GET  /api/v1/adfilter/rules          客户端拉规则包（四态：首次/已最新/降级/不可用）
#   POST /api/v1/adfilter/report         客户端上报命中明细（幂等、限流、逐条丢弃）
#   GET  /api/v1/adfilter/admin/packs    版本清单 + 当前规则（面板用）
#   GET  /api/v1/adfilter/admin/packs/{v} 指定版本详情
#   POST /api/v1/adfilter/admin/packs     发布新版本（服务端校验）
#   POST /api/v1/adfilter/admin/packs/rollback 回滚（新版本号 + 旧内容）
#   GET  /api/v1/adfilter/admin/stats     上报量聚合（不含 url）
#
# 注意：这组路径都是 /api/v1/adfilter/... 两段以上，不会被 /api/v1/{slug}
# 那条 MacCMS 通配路由吃掉；仍放在它之前，避免以后改路由形态时踩坑。
# 响应刻意**不带** code 包装：契约文档 §2.1/§2.2 就是这么写的，客户端按
# changed/version/rules 与 accepted 解析，多包一层反而增加对不齐的风险。
# ---------------------------------------------------------------------------
@router.get("/api/v1/adfilter/rules", summary="去广告规则包拉取（v1.16.0）")
def adfilter_rules(
    response: Response,
    version: int = Query(0, ge=0, description="客户端当前持有的规则包版本号，缺省 0"),
    channel: Optional[str] = Query(
        None, description="规则渠道：stable（缺省）/ beta；未知值按 stable 处理，不报错"
    ),
):
    """
    下发规则包。四态语义（客户端只认这三种响应形态）：

    1. 首次（version=0 或缺省）→ `{changed:true, version:N, rules:{...}}`
    2. 已最新（version == 服务端版本）→ `{changed:false, version:N}`，**不带 rules**
    3. 客户端版本高于服务端（服务端刚回滚过）→ `changed:false` + 服务端当前版本，
       客户端继续用本地较新的包，无副作用
    4. 规则包读不到（库暂不可用 / 从未发布）→ 返回内置默认规则 + version 0，
       同样走上面三条判定，绝不 500 —— 客户端拿不到新规则就用自己内置的，
       控制面故障不该影响播放

    不带 `adfilter` 能力的客户端不会调用本接口；调了也没关系（只读、无副作用）。
    """
    # 规则随时可能被面板改，别让中间层缓存住（客户端自己有 30min TTL）
    response.headers["Cache-Control"] = "no-store"
    if not config.ADFILTER_ENABLED:
        raise HTTPException(status_code=404, detail="adfilter 未启用（ADFILTER_ENABLED=false）")

    state = adfilter.current(channel or adfilter.DEFAULT_CHANNEL)
    if int(version) == state["version"]:
        return {"changed": False, "version": state["version"]}
    if int(version) > state["version"]:
        # 服务端版本更低（回滚过）：不给 rules，让客户端继续持用本地较新的包
        return {"changed": False, "version": state["version"]}
    return {"changed": True, "version": state["version"], "rules": state["rules"]}


@router.post("/api/v1/adfilter/report", summary="去广告命中上报（v1.16.0）")
def adfilter_report(payload: dict = None, request: Request = None):
    """
    批量接收过滤命中明细（客户端本地攒批：≥5 条或规则拉取成功时发，单批 ≤50）。

    防护（这是公网可写端点，四道闸一个都不能少）：
      1. 鉴权必选：匿名 401（走 main.py 的统一鉴权中间件）；
      2. 限流：默认 60 请求/小时/凭据，超限 429 + Retry-After；
      3. 结构校验：items 必须是数组且 ≤50，否则整批 400（客户端会拆小批重试）；
      4. 单条校验：类型错/为负/removed_ms>total_ms/时间偏差>7天/url>2048 → 丢该条。

    **幂等**：按 (user_id, url, at) 去重，重复上报返回 accepted=0，不产生重复行。
    响应里的 dropped（数据不合规）与 duplicates（重复）分开计数，便于排查
    「客户端一直在重试」还是「数据本身有问题」。
    """
    if not config.ADFILTER_ENABLED:
        raise HTTPException(status_code=404, detail="adfilter 未启用（ADFILTER_ENABLED=false）")

    body = payload if isinstance(payload, dict) else {}
    items = body.get("items")
    if items is None:
        items = []
    if not isinstance(items, list):
        raise HTTPException(status_code=400, detail="items 必须是数组")
    limit = max(1, config.ADFILTER_REPORT_MAX_ITEMS)
    if len(items) > limit:
        # 整批拒绝而不是静默截断：客户端会按较小批次重试，不会死循环；
        # 截断则会让客户端误以为前 N 条已入库，直接丢掉后一半数据。
        raise HTTPException(
            status_code=400,
            detail=f"items 单批最多 {limit} 条，当前 {len(items)} 条",
        )

    user_id = auth.request_identity(request)
    wait = adfilter.check_rate(user_id)
    if wait:
        raise HTTPException(
            status_code=429,
            detail=f"上报过于频繁，请 {wait} 秒后重试",
            headers={"Retry-After": str(wait)},
        )

    result = adfilter.ingest(items, user_id)
    return {
        "accepted": result["accepted"],
        "dropped": result["dropped"],
        "duplicates": result["duplicates"],
    }


@router.get("/api/v1/adfilter/admin/packs", summary="规则包版本清单（管理端）")
def adfilter_admin_packs(
    channel: str = Query("stable", description="规则渠道：stable / beta"),
    limit: int = Query(0, ge=0, le=200, description="返回的版本条数，0=用默认保留数"),
):
    """
    当前生效规则 + 历史版本清单（历史项只给版本号/备注/时间/字节数，不含正文）。

    面板「去广告规则包」区块用它一次性画出状态：当前版本、规则 JSON、历史。
    """
    ch = adfilter.normalize_channel(channel)
    state = adfilter.current(ch)
    return {
        "code": SUCCESS_CODE,
        "channel": ch,
        "current": state["version"],
        "rules": state["rules"],
        "packs": adfilter.list_versions(ch, limit or config.ADFILTER_PACK_HISTORY),
        "channels": list(adfilter.CHANNELS),
    }


@router.get("/api/v1/adfilter/admin/packs/{version}", summary="规则包指定版本详情（管理端）")
def adfilter_admin_pack(version: int, channel: str = Query("stable")):
    row = adfilter.get(version, channel)
    if not row:
        raise HTTPException(status_code=404, detail=f"渠道 {channel} 不存在版本 {version}")
    return {"code": SUCCESS_CODE, **row}


@router.post("/api/v1/adfilter/admin/packs", summary="发布规则包新版本（管理端）")
def adfilter_admin_publish(payload: dict = None):
    """
    发布新版本：服务端先校验再入库，版本号在本渠道内严格 +1。

    校验是硬性的 —— 客户端对越界包的处理是「整体丢弃回退内置规则」，
    不是「按钳制后的值跑」。所以这里挡住越界值，等于替客户端兜住发布事故。

    body：`{"rules": {...}, "channel": "stable", "note": "止血：xxx 站误杀"}`

    返回里 `warnings` 是「不拒收但该看一眼」的提示（例如 url_patterns 里放了
    /shop/ 这类商业语义词，容易误杀正常影视目录）。
    """
    body = payload if isinstance(payload, dict) else {}
    rules = body.get("rules")
    if rules is None:
        rules = {}
    try:
        result = adfilter.publish(
            rules,
            body.get("channel") or adfilter.DEFAULT_CHANNEL,
            body.get("note") or "",
        )
    except adfilter.RuleError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except IntegrityError:
        # 两个人同时点发布，后一个撞唯一索引：明确告诉他是版本冲突，不是代码坏了
        raise HTTPException(status_code=409, detail="版本号冲突（有并发发布），请重试一次")
    return {
        "code": SUCCESS_CODE,
        "msg": f"已发布 v{result['version']}",
        **result,
    }


@router.post("/api/v1/adfilter/admin/packs/rollback", summary="回滚规则包（管理端）")
def adfilter_admin_rollback(
    version: int = Query(..., ge=1, description="要回滚到的历史版本号"),
    channel: str = Query("stable"),
    note: str = Query("", description="回滚备注，缺省写「回滚至 vN」"),
):
    """
    回滚 = 用旧版本内容发一个**新版本**（version+1），不改历史行。

    这样客户端永远只需处理「版本变大」，不需要「版本变小」的分支；
    回滚本身也可以被再次回滚（历史都在）。
    """
    try:
        result = adfilter.rollback(version, channel, note)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {
        "code": SUCCESS_CODE,
        "msg": f"已回滚至 v{version} 的内容，发布为 v{result['version']}",
        **result,
    }


@router.get("/api/v1/adfilter/admin/stats", summary="去广告上报统计（管理端）")
def adfilter_admin_stats(days: int = Query(7, ge=1, le=90)):
    """
    上报量聚合：总量 / 近 N 天 / 去重地址数 / 删除时长占比。

    `removed_ratio`（删除时长 ÷ 原始总时长）突增 = 规则可能过激；
    **不返回任何 url** —— 播放地址属于用户隐私，契约明确要求不得进对外接口。
    """
    return {"code": SUCCESS_CODE, **adfilter.stats(days)}


def _tag_stats_brief() -> dict:
    """
    概览用的标签索引摘要（v1.14.0 / B-01）。

    只取三项：是否就绪、已建行数、正在回填的源站。详细的逐站进度在
    `GET /api/v1/tag/status` —— 概览接口每 5 秒被面板轮询一次，
    不能把 50 多个源站的进度行都塞进来。
    """
    try:
        from .. import tags

        st = tags.status()
        return {"ready": st["ready"], "rows": st["rows"], "building": st["building"]}
    except Exception:  # noqa: BLE001 —— 概览不能因为附属信息挂掉
        return {"ready": False, "rows": 0, "building": []}


@router.get("/api/v1/stats")
def stats():
    """管理面板概览数据：源站数、总条数、同步中数量、今日入库量、暂停状态、最近错误。"""

    from sqlalchemy import and_

    from .. import pause, playline, syncer
    from ..maccms import proxy_snapshot

    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    with database.engine.connect() as conn:
        total_sources = conn.execute(select(func.count()).select_from(sources)).scalar() or 0
        enabled_sources = conn.execute(
            select(func.count()).select_from(sources).where(sources.c.enabled == 1)
        ).scalar() or 0
        # 概览"同步中"= 此刻真正在跑的任务数（不是"有断点"的站数）
        unifinished = conn.execute(
            select(func.count()).select_from(sources).where(sources.c.last_page > 0)
        ).scalar() or 0
        total_videos = conn.execute(
            select(func.coalesce(func.sum(sources.c.total_videos), 0))
        ).scalar() or 0
        today_rows = conn.execute(
            select(
                func.coalesce(func.sum(sync_logs.c.inserted), 0),
                func.coalesce(func.sum(sync_logs.c.updated), 0),
                func.count(sync_logs.c.id),
            ).where(sync_logs.c.started_at >= today)
        ).one()
        last_error = conn.execute(
            select(sync_logs.c.slug, sync_logs.c.error, sync_logs.c.started_at)
            .where(and_(sync_logs.c.status == "failed", sync_logs.c.error != ""))
            .order_by(sync_logs.c.id.desc()).limit(1)
        ).first()
    return {
        "code": SUCCESS_CODE,
        "sources_total": total_sources,
        "sources_enabled": enabled_sources,
        "sources_syncing": len(syncer.running_snapshot()),   # 此刻真正在跑的任务数
        "sources_unfinished": unifinished,                   # 全量未跑完（有断点）的站数
        "videos_total": int(total_videos),
        "today_inserted": int(today_rows[0]),
        "today_updated": int(today_rows[1]),
        "today_tasks": int(today_rows[2]),
        "pause": pause.snapshot(),   # 同步暂停状态（面板顶栏开关用）
        # 中转代理运行态：是否启用、走代理的源站、成功/失败计数（排障用）
        "proxy": proxy_snapshot(),
        # 播放线路过滤（v1.11.0）：全局策略 + 读侧累计计数。
        # 注意是「本次进程启动以来」的累计值（重启清零），用于确认策略真的在拦截线路。
        "play_line": {
            k: v for k, v in playline.stats_snapshot().items() if k != "by_source"
        },
        # 数据库连接池 + 同步负载（判断"是不是又堆任务了"最直接的两个指标）
        "db_pool": database.pool_status(),
        "sync_load": syncer.sync_load(),
        # 标签倒排索引（v1.14.0 / B-01）：面板显示「已建 N 行 / 是否就绪」，
        # 也就绪与否直接决定客户端能不能点标签胶囊精确检索
        "tags": _tag_stats_brief(),
        "last_error": (
            {"slug": last_error[0], "error": last_error[1],
             "at": last_error[2].isoformat() if last_error[2] else None}
            if last_error else None
        ),
        "server_time": datetime.now().isoformat(),
    }


@router.get("/api/v1/sources/{slug}")
def source_detail(slug: str, request: Request):
    from .. import syncer

    # v1.15.0：未成年模式下 18+ 源站的详情入口也要挡住（含同步日志），
    # 与「屏蔽展示入口」一致。面板不带 X-Minor-Mode 头，管理不受影响。
    minor_mode.guard(request, validate_slug(slug))
    src, _ = _require_table(slug)
    live = syncer.running_snapshot().get(slug)
    with database.engine.connect() as conn:
        logs = conn.execute(
            select(sync_logs).where(sync_logs.c.slug == slug)
            .order_by(sync_logs.c.id.desc()).limit(10)
        ).mappings().all()
    return {
        "code": SUCCESS_CODE,
        "source": {
            "slug": src["slug"], "name": src["name"], "api_url": src["api_url"],
            "is_adult": minor_mode.is_adult(src["is_adult"]),
            "status": src["status"], "total_videos": src["total_videos"],
            "watermark": src["watermark"], "last_page": src["last_page"],
            "running": live is not None,
            "running_mode": (live or {}).get("mode"),
            "running_seconds": (live or {}).get("elapsed_seconds"),
            "paused": bool(src["paused"]),
            "paused_at": src["paused_at"].isoformat() if src["paused_at"] else None,
            "auto_sync": bool(src["auto_sync"]),
            "synced": _is_synced(src),
            "last_sync_at": src["last_sync_at"].isoformat() if src["last_sync_at"] else None,
        },
        "recent_logs": [
            {
                "id": lg["id"], "mode": lg["mode"], "status": lg["status"],
                "fetched": lg["fetched"], "inserted": lg["inserted"],
                "updated": lg["updated"], "pages": lg["pages"],
                "error": lg["error"],
                "started_at": lg["started_at"].isoformat() if lg["started_at"] else None,
                "finished_at": lg["finished_at"].isoformat() if lg["finished_at"] else None,
            }
            for lg in logs
        ],
    }


@router.post("/api/v1/sources/auto-sync")
def bulk_auto_sync(
    enabled: bool = Query(..., description="true=开启自动同步, false=暂停自动同步"),
    slugs: str = Query("all", description="逗号分隔的源站 slug 列表；all 或留空=全部源站"),
):
    """
    批量设置源站的「自动同步」开关（持久化，重启后仍生效）。

    关闭后该源站不再参与定时增量任务与全站同步，但手动单站同步不受影响。
    用法：
      POST /api/v1/sources/auto-sync?enabled=false&slugs=all          全部暂停自动同步
      POST /api/v1/sources/auto-sync?enabled=true&slugs=jkun,t1,bfzytv 只开启指定源站
    """
    from ..syncer import set_auto_sync_bulk

    raw = (slugs or "").strip()
    slug_list = None
    if raw and raw.lower() not in ("all", "*"):
        slug_list = [s.strip() for s in raw.split(",") if s.strip()]
        if not slug_list:
            slug_list = None
        else:
            for s in slug_list:
                validate_slug(s)
    n = set_auto_sync_bulk(enabled, slug_list)
    return {
        "code": SUCCESS_CODE,
        "msg": f"已{'开启' if enabled else '暂停'} {n} 个源站的自动同步",
        "updated": n,
        "auto_sync": enabled,
    }


@router.post("/api/v1/sources/{slug}/auto-sync")
def set_one_auto_sync(
    slug: str,
    enabled: bool = Query(..., description="true=开启自动同步, false=暂停自动同步"),
):
    """设置单个源站的「自动同步」开关（持久化；不影响手动单站同步）。"""
    from ..syncer import set_source_auto_sync

    src, _ = _require_table(slug)
    set_source_auto_sync(slug, enabled)
    return {
        "code": SUCCESS_CODE,
        "msg": f"源站 {src['name']} 自动同步已{'开启' if enabled else '暂停'}",
        "slug": slug,
        "auto_sync": enabled,
    }


@router.post("/api/v1/sources/{slug}/play-line-policy")
def set_play_line_policy(
    slug: str,
    policy: str = Query(
        ...,
        description="inherit=跟随全局 / off=不过滤 / filter=剔除分享页线路 / whitelist=只保留媒体直链",
    ),
):
    """
    设置单个源站的播放线路策略（v1.11.0，持久化，立即生效）。

    为什么按源站配：只有「第 1 条线路恒为 /share/ 分享页」这类站点需要过滤
    （如非凡资源站，100% 记录如此），一刀切的全局策略会误伤本来正常的站。
    默认 `inherit` 跟随 `PLAY_LINE_POLICY`，其余站点不必动。

    只影响读接口的 `vod_play_list`，不重写 `vod_play_url`，也不需要重新同步。
    """
    from .. import playline

    src, _ = _require_table(slug)
    try:
        value = playline.set_source_policy(slug, policy)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {
        "code": SUCCESS_CODE,
        "msg": f"源站 {src['name']} 播放线路策略 = {value}",
        "slug": slug,
        "play_line_policy": value,
        "effective": playline.effective_policy(slug),
    }


@router.post("/api/v1/sources/{slug}/pause")
def pause_one_source(slug: str):
    """
    暂停单个源站的同步（不影响其他源站）。
    进行中的全量同步会在下一个检查点停止并保存断点；该源站不参与 sync-all 与定时任务。
    """
    from .. import syncer
    from ..syncer import set_source_paused

    src, _ = _require_table(slug)
    was_running = syncer.is_running(slug)
    set_source_paused(slug, True)
    return {
        "code": SUCCESS_CODE,
        "msg": (
            f"源站 {src['name']} 的同步已暂停，将在当前批次结束后停止并保留断点"
            if was_running
            else f"源站 {src['name']} 当前未在同步，暂停标记已记录（下次同步时不执行）"
        ),
        "slug": slug,
        "paused": True,
        "was_running": was_running,
    }


@router.post("/api/v1/sources/{slug}/resume")
def resume_one_source(slug: str):
    """
    继续单个源站的同步：清除暂停标记并按 auto 模式续跑（有断点续全量 / 有水位线走增量）。
    注意：若全局/分层暂停仍生效，任务会被立即拦截为 paused，需先解除上层暂停。
    """
    from .. import pause as pause_ctl
    from ..syncer import resume_source

    src, _ = _require_table(slug)
    resume_source(slug)
    blocked = "full" if pause_ctl.is_paused("full") else (
        "incremental" if pause_ctl.is_paused("incremental") else ""
    )
    return {
        "code": SUCCESS_CODE,
        "msg": f"源站 {src['name']} 的同步已继续" + (
            f"（但当前存在{'全局' if pause_ctl.snapshot()['global'] else ''}暂停，任务将等待）" if blocked else ""
        ),
        "slug": slug,
        "paused": False,
        "blocked_by": blocked,
    }


@router.post("/api/v1/sources/refresh")
def refresh_source_list(full_sync: bool = Query(default=False, description="发现新源站后是否立即全量同步")):
    """立即拉取 latest.json 更新源站列表（可在后台或手动调用时执行）。"""
    new_slugs = refresh_sources()
    if full_sync:
        threading.Thread(
            target=_bg_sync_new, args=(list(new_slugs),), daemon=True
        ).start()
    return {"code": SUCCESS_CODE, "new_sources": new_slugs, "full_sync_started": full_sync}


# ---------------------------------------------------------------------------
# 自定义源站（v1.8.0）
#
# 需求：原来所有源站都只能来自 latest.json，无法登记自己的接口。
# 这里提供 add / patch / delete + 一个不落库的连通性探测。
#
# 「持续存在」是怎么保证的：自定义源站就是 sources 表里一行 origin='custom' 的记录。
# refresh_sources() 遇到它只更新 status（在线状态是客观事实），不改名字/描述，
# 更不会删除。所以重启、刷新源站列表、重新部署都不会丢，也不依赖任何内存状态
# 或额外配置文件。
# ---------------------------------------------------------------------------

_ALLOWED_URL_SCHEMES = ("http://", "https://")


def _clean_api_url(raw: str) -> str:
    """
    校验用户填的 API 地址。

    只放行 http/https：服务端会主动去请求这个地址，`file://`、`gopher://`、`dict://`
    这类 scheme 在 SSRF 场景里能读本地文件或打内网服务，必须在入口挡掉。
    （本项目需要管理员登录才能调这个接口，风险不高，但挡一下成本几乎为零。）
    """
    url = (raw or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="API 地址不能为空")
    if not url.lower().startswith(_ALLOWED_URL_SCHEMES):
        raise HTTPException(status_code=400, detail="API 地址必须以 http:// 或 https:// 开头")
    if any(ch.isspace() for ch in url):
        raise HTTPException(status_code=400, detail="API 地址不能包含空白字符")
    return url[:500]


class CustomSourceIn(BaseModel):
    """添加自定义源站的请求体。"""
    name: str = Field(..., min_length=1, max_length=120, description="源站名称（显示用，可中文）")
    api_url: str = Field(..., min_length=8, max_length=500, description="苹果 CMS 采集接口地址")
    slug: str = Field("", max_length=64, description="自定义标识（同时是表名后缀与 API 路径）；留空由名称推导")
    homepage: str = Field("", max_length=500)
    description: str = Field("", max_length=500)
    probe: bool = Field(True, description="添加前先抓一页验证可用性")
    allow_unreachable: bool = Field(False, description="探测失败也强制添加")
    sync_now: bool = Field(False, description="添加成功后立即后台全量同步")


class ProbeIn(BaseModel):
    """连通性探测请求体。"""
    api_url: str = Field(..., min_length=8, max_length=500)


class CustomSourcePatch(BaseModel):
    """修改自定义源站：只传需要改的字段。"""
    name: Optional[str] = Field(None, max_length=120)
    api_url: Optional[str] = Field(None, max_length=500)
    homepage: Optional[str] = Field(None, max_length=500)
    description: Optional[str] = Field(None, max_length=500)
    enabled: Optional[bool] = None
    auto_sync: Optional[bool] = None


@router.post("/api/v1/sources/probe", summary="测试源站 API 连通性（不落库）")
def probe_source_api(body: ProbeIn):
    """
    抓第 1 页验证地址是不是可用的苹果 CMS 采集接口。

    永远返回 200，用 `ok` 字段表示结果 —— 探测失败是正常业务结果，不该让
    前端去 catch 异常。失败时 `error` 给出人能看懂的原因。
    """
    api_url = _clean_api_url(body.api_url)
    return {"code": SUCCESS_CODE, **probe_source(api_url)}


@router.post("/api/v1/sources/custom", summary="添加自定义源站")
def add_custom_source(body: CustomSourceIn):
    """
    手动登记源站，立即建表并持久化。

    - 身份按 `api_url` 认（与 latest.json 同一套规则），地址重复直接 409
    - `slug` 留空时由名称推导；纯中文名会得到 `s+md5前缀` 这种不好读的标识，
      想要可读就自己填（如 `myhd`），它同时决定表名 `vod_myhd` 和 API 路径
    - `probe=true` 会先抓一页验证；失败默认拒绝入库，确认无误可传
      `allow_unreachable=true` 强制添加（比如源站临时维护中）
    """
    api_url = _clean_api_url(body.api_url)

    probe_result = None
    if body.probe:
        probe_result = probe_source(api_url)
        if not probe_result["ok"] and not body.allow_unreachable:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"源站不可用：{probe_result.get('error')}"
                    "（确认地址无误可传 allow_unreachable=true 强制添加）"
                ),
            )

    try:
        res = register_custom_source(
            name=body.name,
            api_url=api_url,
            slug=(body.slug or "").strip(),
            homepage=body.homepage,
            description=body.description,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    slug = res["slug"]
    sync_started = False
    if body.sync_now:
        from .. import syncer

        if not syncer.is_busy(slug):
            threading.Thread(target=sync_source, args=(slug, "full"), daemon=True).start()
            sync_started = True

    return {
        "code": SUCCESS_CODE,
        "slug": slug,
        "probe": probe_result,
        "sync_started": sync_started,
        "msg": f"已添加源站 {slug}" + ("，正在后台全量同步" if sync_started else ""),
    }


@router.patch("/api/v1/sources/{slug}/custom", summary="修改自定义源站")
def patch_custom_source(slug: str, body: CustomSourcePatch):
    """
    修改自定义源站的名称 / 地址 / 备注 / 开关。

    ⚠️ 改 `api_url` 会**清空该站已入库的数据**并重置水位线与断点 —— 换了接口
    就是换了数据源，保留旧行只会让列表里混进「新源已经没有」的片子，而且
    vod_id 冲突时会被覆盖成新源内容，看起来像数据错乱。
    """
    slug = validate_slug(slug)
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=400, detail="没有需要修改的字段")
    if fields.get("api_url") is not None:
        fields["api_url"] = _clean_api_url(fields["api_url"])

    try:
        res = update_custom_source(slug, **fields)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"code": SUCCESS_CODE, **res}


@router.delete("/api/v1/sources/{slug}/custom", summary="删除自定义源站")
def remove_custom_source(
    slug: str,
    confirm: str = Query(default="", description="必须与 slug 相同，防止误调用"),
    drop_table: bool = Query(default=True, description="是否同时删除该站的数据表（含已入库数据）"),
):
    """
    删除手动添加的源站（可连同数据表一起 DROP）。

    只允许删 `origin='custom'` 的：官方源站删掉之后下一次 refresh 还会回来，
    删它没有意义，反而让「我的源站怎么不见了」变成查不清的谜。
    """
    slug = validate_slug(slug)
    if confirm != slug:
        raise HTTPException(status_code=400, detail="需传 confirm=<slug> 确认删除，防止误操作")

    try:
        res = delete_custom_source(slug, drop_table=drop_table)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return {
        "code": SUCCESS_CODE,
        **res,
        "msg": f"已删除源站 {slug}" + ("（数据表已删除）" if res["dropped"] else "（数据表保留）"),
    }


def _bg_sync_new(slugs: list):
    for slug in slugs:
        try:
            sync_source(slug, mode="full")
        except Exception:
            log.exception("新源站 %s 全量同步失败", slug)


@router.post("/api/v1/sync/{slug}")
def trigger_sync(
    slug: str,
    mode: str = Query(
        default="auto",
        pattern="^(auto|full|incremental|backfill|backfill_play_from)$",
        description="auto / full / incremental / backfill_play_from（只回填播放线路来源）",
    ),
):
    """
    手动触发单源站同步（后台执行，立即返回受理结果）。

    并发受全局闸门（SOURCE_SYNC_CONCURRENCY）限制：名额已满时任务进入排队，
    同一源站已在排队/运行时不会重复提交（返回 status=already_running）。

    `mode=backfill_play_from`（v1.11.0）：只翻 ac=detail 把上游 `vod_play_from`
    回填进库（只写这一列），不动其它字段、不推进水位线、不动断点，可中断可重跑。
    存量库刚升级时用它补齐 provider 信息；不回填也能正常工作（只按 URL 形态判断）。
    """
    from .. import syncer

    if slug != "*":
        _require_table(slug)
    slug = validate_slug(slug) if slug != "*" else "*"
    if syncer.is_busy(slug):
        return {"code": SUCCESS_CODE, "status": "already_running",
                "msg": f"{slug} 已有同步任务在排队或执行中，本次未重复提交"}
    threading.Thread(target=sync_source, args=(slug, mode), daemon=True).start()
    return {"code": SUCCESS_CODE, "status": "accepted", "msg": f"同步任务已受理: {slug} mode={mode}"}


@router.post("/api/v1/sync-all")
def trigger_sync_all(
    mode: str = Query(
        default="auto",
        pattern="^(auto|full|incremental|backfill|backfill_play_from)$",
        description="auto / full / incremental / backfill_play_from",
    )
):
    """
    手动触发全部源站同步（后台执行）。

    v1.14.0 / B-07：新增 `mode=backfill_play_from` —— 对**全部启用源站**并行
    回填上游 `vod_play_from`（只写 play_from 一列，不动其它字段、不推进水位线、
    不动断点，可中断可重跑）。客户端控制台已预留该入口。
    与 `/api/v1/sync/{slug}?mode=backfill_play_from` 语义完全一致，只是范围不同。
    """
    if mode in ("backfill", "backfill_play_from"):
        threading.Thread(target=sync_all, args=("backfill",), daemon=True).start()
        return {
            "code": SUCCESS_CODE,
            "msg": "播放线路来源回填任务已受理（全部启用源站，只写 play_from 一列）",
            "mode": "backfill_play_from",
        }
    threading.Thread(target=sync_all, args=(mode,), daemon=True).start()
    return {"code": SUCCESS_CODE, "msg": f"全量同步任务已受理: mode={mode}"}


@router.post("/api/v1/maintenance/recount")
def recount():
    """重算各源站本地条数（同步中断后校准面板数据）。"""
    from ..syncer import refresh_counts

    threading.Thread(target=refresh_counts, daemon=True).start()
    return {"code": SUCCESS_CODE, "msg": "条数校准任务已受理"}


@router.get("/api/v1/pause")
def pause_status():
    """同步暂停状态（三层级：global / full / incremental）。"""
    from .. import pause

    return {"code": SUCCESS_CODE, **pause.snapshot()}


@router.post("/api/v1/pause")
def pause_sync(
    scope: str = Query(..., pattern="^(global|full|incremental)$",
                      description="暂停范围：global=全部, full=全量, incremental=增量"),
    reason: str = Query("", description="可选备注（目前仅记录在服务日志）"),
):
    """
    暂停同步（优雅停止：当前页面批次完成后保存断点再停，不丢数据）。
    - global：全量+增量同时暂停，且定时调度跳过
    - full：仅暂停全量同步（断点保留，继续时自动续传）
    - incremental：仅暂停增量同步（水位线不动）
    暂停状态持久化，服务重启后仍生效。
    """
    from .. import pause

    if reason:
        log.info("暂停同步 [%s]: %s", scope, reason)
    pause.set_paused(scope, True)
    return {
        "code": SUCCESS_CODE,
        "msg": f"同步暂停已生效: {scope}（进行中的批次将在下一个检查点停止）",
        "state": pause.snapshot(),
    }


@router.post("/api/v1/resume")
def resume_sync(
    scope: str = Query(..., pattern="^(global|full|incremental)$",
                      description="继续范围：global / full / incremental"),
):
    """
    继续同步：清除暂停标记并自动续跑对应范围的同步任务（后台执行）。
    - full：只续跑有断点的源站，从断点页继续
    - incremental：对已有水位线的源站重跑增量
    - global：全部启用源站 auto 模式（有断点续全量，有水位线走增量）
    """
    from .. import pause, syncer

    pause.set_paused(scope, False)
    slugs = syncer.trigger_resume(scope)
    return {
        "code": SUCCESS_CODE,
        "msg": f"同步已继续: {scope}",
        "resumed_sources": slugs,
        "state": pause.snapshot(),
    }


@router.get("/api/v1/logs")
def recent_logs(limit: int = Query(50, ge=1, le=200)):
    with database.engine.connect() as conn:
        rows = conn.execute(
            select(sync_logs).order_by(sync_logs.c.id.desc()).limit(limit)
        ).mappings().all()
    return {
        "code": SUCCESS_CODE,
        "list": [
            {
                "id": r["id"], "slug": r["slug"], "mode": r["mode"], "status": r["status"],
                "fetched": r["fetched"], "inserted": r["inserted"], "updated": r["updated"],
                "pages": r["pages"], "error": r["error"],
                "started_at": r["started_at"].isoformat() if r["started_at"] else None,
                "finished_at": r["finished_at"].isoformat() if r["finished_at"] else None,
            }
            for r in rows
        ],
    }


# ---------------------------------------------------------------------------
# 数据表浏览（v1.12.0，管理面板，只读）
# 逻辑在 tablebrowse.py：表名白名单 + 列名白名单，这是接口里仅有的两处
# 「标识符进 SQL」的位置，改这里之前先读那个模块的安全边界说明。
# ---------------------------------------------------------------------------
@router.get("/api/v1/admin/tables")
def admin_list_tables():
    """
    可浏览的表清单（含列元信息与行数）。settings 表有意不在列表里 ——
    它存密码哈希 / 会话密钥，只读浏览不该把它们端出来。
    """
    from .. import tablebrowse

    return {"code": SUCCESS_CODE, "tables": tablebrowse.list_browsable()}


@router.get("/api/v1/admin/tables/{name}/rows")
def admin_browse_table(
    name: str,
    page: int = Query(1, ge=1, description="页码，超出范围自动夹回边界"),
    page_size: int = Query(20, ge=1, le=100, description="每页条数，最大 100"),
    field: str = Query("", max_length=64,
                       description="按该字段搜索；留空=全表搜索（所有列 OR）"),
    q: str = Query("", max_length=200, description="关键词（部分匹配，大小写不敏感）；留空=不过滤"),
):
    """分页浏览一张表的数据。表不存在 404；字段不存在 400。"""
    from .. import tablebrowse

    try:
        return tablebrowse.browse(name, page, page_size, field, q)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"表不存在或不可浏览: {name}") from None
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"未知字段: {e}") from None


# ---------------------------------------------------------------------------
# REST 风格数据接口（source 参数区分源站）
# ---------------------------------------------------------------------------
@router.get("/api/v1/videos")
def video_list(
    request: Request,
    source: str = Query("", description="源站 slug，支持逗号分隔多源；留空查询全部源站"),
    type_name: str = Query("", description="按分类名称过滤"),
    type_id: int = Query(0, ge=0, description="按分类 ID 过滤（配合 /api/v1/categories 使用）"),
    wd: str = Query("", description="按名称模糊搜索"),
    tag: str = Query(
        "", max_length=64,
        description="v1.14.0：按标签精确检索（与 wd 二选一，同时传以 tag 为准）",
    ),
    ac: str = Query("", description="兼容 MacCMS：ac=detail 时列表返回含播放地址的完整字段"),
    ids: str = Query("", description="按 ID 批量取详情（逗号分隔），返回含播放地址"),
    play_raw: int = Query(0, ge=0, le=1,
                          description="v1.11.0：1=不做线路过滤/排序，返回原始 vod_play_list（排障用）"),
    paging: tuple = Depends(page_params),
):
    """
    视频列表 / 批量详情。

    四种用法：
      1. 列表：      /api/v1/videos?source=jkun&page=1&page_size=20
      2. 批量详情：  /api/v1/videos?source=jkun&ids=134369       （含 vod_play_url）
      3. 完整列表：  /api/v1/videos?source=jkun&ac=detail         （列表带播放地址）
      4. 分类浏览：  /api/v1/videos?source=jkun&type_id=6         （配合 /api/v1/categories）

    v1.11.0（播放线路）：默认按 `PLAY_LINE_POLICY`（源站可覆盖）过滤掉
    `/share/`、`/play/`、`.html` 这类「H5 分享页」线路 —— 它们返回 text/html，
    真实流地址由页面 JS 生成且带会过期的 sign，客户端直接播必然失败。
    `vod_play_url` 原始串不变；过滤只作用于结构化的 `vod_play_list`
    （每条线路新增 `from` / `type` / `playable` / `original_index`）。
    默认响应若真的过滤掉了线路，顶层带 `play_line_filtered: true`；
    `play_raw=1` 可拿到未过滤的原始列表（排障用，同时关闭排序）。

    v1.10.1（纯新增）：详情模式（`ids=` 批量 / `ac=detail` 列表）额外返回
    `vod_actor` / `vod_director` / `vod_area` / `vod_year` / `vod_tag` 五个字段，
    恒为字符串（库里 NULL 也返回 ""），普通列表模式不带（控制体积）。

    v1.10.0（纯新增）：不带筛选条件的全站列表（无 wd / type_id / type_name / ids）
    的 `total` 改为估值，并在响应里带 `total_approximate: true`。
    出处是 sources.total_videos（后台同步一直维护），省掉 52 张表各跑一次 count(*)。
    `pagecount` 随之由估值算出，翻到末页可能有 1~2 页偏差；带筛选时仍是精确值。
    客户端若做「共 N 条 / 最后一页」的强展示，请判断该字段后自行降级为「约 N 条」。
    """
    page, page_size = paging
    # 参数归一化：兼容 `&?ac=detail` 这类多打问号的写法
    source = _qp(request, "source", source)
    ac = _qp(request, "ac", ac).lower()
    ids = _qp(request, "ids", ids)
    type_name = _qp(request, "type_name", type_name)
    wd = _qp(request, "wd", wd)
    tag = _qp(request, "tag", tag)
    # v1.14.1（审计 M2 配合）：关键词超长不再 422，静默截断到 64。
    # 粘贴长句搜索时 422 + 英文 detail 的观感是「搜索坏了」；
    # 截断对 wd（模糊匹配）无害，对 tag 则大概率 tag_found=false → 客户端回退 wd。
    wd = (wd or "").strip()[:64]
    tag = (tag or "").strip()[:64]
    detail_mode = ac in ("detail", "videolist")

    raw_slugs = [s.strip() for s in source.split(",") if s.strip()] if source else None

    if raw_slugs:
        for s in raw_slugs:
            validate_slug(s)
        # v1.15.0：显式指定的 18+ 源站同样要剔除，不能因为「点名了」就放行
        slugs = minor_mode.filter_slugs(request, raw_slugs)
    else:
        clause = minor_mode.source_clause(request)
        cond = [sources.c.enabled == 1] + ([clause] if clause is not None else [])
        with database.engine.connect() as conn:
            slugs = [r[0] for r in conn.execute(
                select(sources.c.slug).where(*cond)
            ).all()]
        if not slugs and not minor_mode.active(request):
            raise HTTPException(status_code=404, detail="暂无源站，请先触发 /api/v1/sources/refresh")

    # 未成年模式下可见源站为空（全被标记 18+，或库里本来就没数据）：
    # 返回标准空列表而不是 404，App 显示「暂无内容」即可，不该弹错误提示。
    # 注意不能落回上面的 else 分支重新枚举全部源站 —— 那会把屏蔽绕过去。
    if not slugs and minor_mode.active(request):
        return {
            "code": SUCCESS_CODE, "page": page, "pagecount": 0, "total": 0,
            "total_approximate": False, "minor_mode": True, "list": [], "query_ms": 0,
        }

    # ---- 用法 2：按 ID 批量取详情（含播放地址） ----
    if ids:
        want: list = []
        for x in ids.split(","):
            try:
                want.append(int(x.strip()))
            except ValueError:
                continue
        if not want:
            raise HTTPException(
                status_code=400,
                detail="ids 参数格式错误，应为逗号分隔的数字，例如 ids=134369 或 ids=1,2,3",
            )
        items: list = []
        play_meta: dict = {}
        existing = database.list_tables("vod_")
        with database.engine.connect() as conn:
            for slug in slugs:
                table = build_vod_table(slug)
                if table.name not in existing:
                    continue
                found = {
                    r["vod_id"]: r
                    for r in conn.execute(
                        select(table).where(table.c.vod_id.in_(want))
                    ).mappings().all()
                }
                for vid in want:  # 按请求中的 id 顺序返回
                    if vid in found:
                        items.append(_detail_item(slug, found[vid], play_raw=bool(play_raw),
                                                  play_meta=play_meta))
        if not items:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"未找到 ids={ids} 的视频（源站: {','.join(slugs)}）。"
                    "该条可能尚未同步入库，可先用 /api/v1/search?wd=关键词 或 "
                    "/api/v1/videos?source=xxx&wd=关键词 搜索确认 vod_id"
                ),
            )
        resp = {"code": SUCCESS_CODE, "page": 1, "pagecount": 1, "total": len(items), "list": items}
        if play_meta.get("filtered"):
            resp["play_line_filtered"] = True
        return resp

    # ---- 用法 1 / 3：列表 ----
    # 走 app/multiquery.py：每表各取 top-K 后进程内归并（精确，见该模块顶部说明）。
    # 只有当翻页深到超出 MERGE_MAX_PER_TABLE 时才回退全量 UNION —— 慢但正确。
    from .. import multiquery

    kw = dict(wd=wd, type_name=type_name, type_id=type_id, detail=detail_mode)
    if tag:
        # ---- 标签精确检索（v1.14.0 / B-01）：与 /api/v1/search?tag= 同一实现 ----
        # 为什么在这里也开一个入口：客户端的「列表页」直接调 /videos，
        # 详情页标签胶囊跳转后仍在本页滚动加载，走的是同一个方法。
        from .. import tags as tags_mod

        tresult = tags_mod.search_page(slugs, tag, page, page_size, type_name)
        if detail_mode and tresult["rows"]:
            multiquery.attach_detail(tresult["rows"])
        play_meta: dict = {}
        resp = {
            "code": SUCCESS_CODE,
            "page": page,
            "pagecount": tresult["pagecount"],
            "total": tresult["total"],
            "total_approximate": False,   # 标签结果集由索引精确计数
            # v1.14.1：标签词是否存在于倒排表；false 时客户端应回退 wd 检索
            "tag_found": tresult["tag_found"],
            "list": (
                [_detail_item(r["source_slug"], r, play_raw=bool(play_raw),
                              play_meta=play_meta)
                 for r in tresult["rows"]]
                if detail_mode
                else [
                    _list_item(r, source=r["source_slug"]) for r in tresult["rows"]
                ]
            ),
            "query_ms": tresult["elapsed_ms"],
        }
        if tresult["partial"]:
            resp["total_is_partial"] = True
            resp["sources_failed"] = tresult["failed"]
        if play_meta.get("filtered"):
            resp["play_line_filtered"] = True
        return resp

    if multiquery.can_merge(page, page_size):
        result = multiquery.merged_page(slugs, page, page_size, **kw)
    else:
        log.info("翻页过深 page=%d page_size=%d，回退 UNION 查询", page, page_size)
        result = multiquery.union_page(slugs, page, page_size, **kw)

    if result["tables"] == 0:
        raise HTTPException(status_code=404, detail="指定源站均无数据")

    play_meta: dict = {}
    resp = {
        "code": SUCCESS_CODE,
        "page": page,
        "pagecount": result["pagecount"],
        "total": result["total"],
        "list": (
            [_detail_item(r["source_slug"], r, play_raw=bool(play_raw), play_meta=play_meta)
             for r in result["rows"]]
            if detail_mode
            else [
                _list_item(r, source=r["source_slug"]) for r in result["rows"]
            ]
        ),
    }
    # 诊断字段（App 可忽略）：total 是否因个别源站查询失败而偏小、用了哪条路径、耗时
    if result["partial"]:
        resp["total_is_partial"] = True
        resp["sources_failed"] = result["failed"]
    # v1.10.0：不带筛选条件时 total 取自 SUM(sources.total_videos)（近似值）。
    # 客户端据此决定分页依据：近似时请以「本页条数 == page_size 就继续翻」为准。
    # v1.14.0 / B-04：**恒出现**（true/false）。原来只在近似时才带，客户端缺字段
    # 与「精确」不可区分，只能猜；契约要求缺省按精确处理，那就显式给 false。
    resp["total_approximate"] = bool(result.get("total_approximate"))
    # v1.11.0：本页有任意条目真的被过滤掉线路时标记（客户端/日志据此识别，
    # 不影响任何既有字段语义；`play_raw=1` 时永远不出现）
    if play_meta.get("filtered"):
        resp["play_line_filtered"] = True
    resp["query_ms"] = result["elapsed_ms"]
    return resp


@router.get("/api/v1/videos/{slug}/{vod_id}")
def video_detail(
    slug: str,
    vod_id: int,
    request: Request,
    play_raw: int = Query(0, ge=0, le=1,
                          description="v1.11.0：1=不做线路过滤/排序，返回原始 vod_play_list（排障用）"),
):
    """
    视频详情（含结构化播放地址列表）。RELATED_IN_DETAIL=true 时附带 related 推荐数组。

    v1.11.0（播放线路）：`vod_play_list` 默认已过滤掉 `/share/`、`/play/`、`.html`
    这类「H5 分享页」线路（返回 text/html，真实流地址由 JS 生成且带过期 sign）。
    每条线路带 `from`（上游 provider）/`type`/`playable`/`original_index`；
    `vod_play_url` 原始串不变。真的过滤掉线路时响应顶层带 `play_line_filtered: true`。

    v1.10.1（纯新增）：返回 `vod_actor` / `vod_director` / `vod_area` / `vod_year` /
    `vod_tag` 五个字段 —— 列 v1.9.0 起就在库里，之前没透出，NAS 模式的客户端拿不到
    演员与明星标签。字段恒为字符串（缺失/NULL 为 ""），客户端不必判 null。

    v1.15.0：未成年模式下 18+ 源站的详情（含播放地址）直接 404，
    光靠列表页隐藏入口是挡不住直接调 API 的。
    """
    minor_mode.guard(request, validate_slug(slug))
    src, table = _require_table(slug)
    with database.engine.connect() as conn:
        row = conn.execute(
            select(table).where(table.c.vod_id == vod_id)
        ).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="视频不存在")
    play_meta: dict = {}
    detail = _detail_item(slug, row, play_raw=bool(play_raw), play_meta=play_meta)
    if config.RELATED_IN_DETAIL:
        try:
            cards, fallback = _related_cards(slug, src["name"], vod_id,
                                             config.RELATED_LIMIT_DEFAULT, table)
            detail["related"] = cards
            detail["related_fallback"] = fallback
        except Exception as e:  # 推荐挂了不能连累详情
            log.warning("[%s] 详情附带推荐失败（忽略）: %s", slug, e)
    resp = {"code": SUCCESS_CODE, "detail": detail}
    if play_meta.get("filtered"):
        resp["play_line_filtered"] = True
    return resp


# ---------------------------------------------------------------------------
# 聚合搜索：跨全部源站按名称搜索（并行查询各源站表，内存合并分页）
# ---------------------------------------------------------------------------
@router.get("/api/v1/search")
def aggregate_search(
    request: Request,
    wd: str = Query("", description="影视名称关键词（与 tag 二选一；超 64 字符自动截断）"),
    tag: str = Query(
        "",
        description="v1.14.0：标签精确检索（分词后逐段精确匹配，不是模糊搜；超 64 字符自动截断）",
    ),
    source: str = Query("", description="限定源站 slug，逗号分隔；留空=搜索全部源站"),
    type_name: str = Query("", description="按分类名称过滤"),
    page: int = Query(1, ge=1),
    page_size: int = Query(
        config.API_PAGE_SIZE_DEFAULT, ge=1, le=config.API_PAGE_SIZE_MAX,
        description="每页条数（默认取 API_PAGE_SIZE_DEFAULT，上限取 API_PAGE_SIZE_MAX）",
    ),
    per_source_limit: int = Query(
        200, ge=10, le=1000,
        description="每个源站最多返回多少条候选（控制单表单次扫描开销）",
    ),
):
    """
    聚合搜索：按名称模糊（`wd`）**或**按标签精确（`tag`）。

    `wd` 与 `tag` 同时传时**以 `tag` 为准**（B-01 约定，已写入 API_SPEC）：
    客户端只会传其一，后端取确定性行为，避免「两个条件 AND 后结果为空」的歧义。

    实现说明（wd 路径）：
    - 名称模糊匹配无法走索引，采用「每源站各查一次 + 并行执行 + 内存归并」，
      避免一次性 UNION 全部表导致单条 SQL 扫上千万行而超时；
    - 单个源站查询失败/超时不影响整体结果（partial 标记 + 失败源站列表）；
    - 结果按 vod_time 倒序（新更新在前），可按 source 限定范围提升速度。

    实现说明（tag 路径，v1.14.0）：
    - 走 video_tags 倒排表 + (tag_norm, source) 索引，`total` 是**精确值**
      （`total_approximate` 恒为 false），空结果返回 200 + list=[]，不是 404；
    - 只有 `/healthz.capabilities` 含 `tag_search` 时客户端才会走这条路，
      索引没建完时后端仍能响应（只是结果不全），不会 500。
    """
    started = time.time()
    # v1.14.1（审计 M2）：去掉 Query max_length 校验（超长 422 + 英文 detail），
    # 改为静默截断 —— 前端粘贴长句搜索不再报错。
    kw = (wd or "").strip()[:64]
    tag = (tag or "").strip()[:64]
    if not kw and not tag:
        raise HTTPException(
            status_code=400, detail="必须提供 wd（名称关键词）或 tag（标签）之一"
        )

    # 1) 确定待搜索的源站
    clause = minor_mode.source_clause(request)
    cond = [sources.c.enabled == 1] + ([clause] if clause is not None else [])
    with database.engine.connect() as conn:
        if source:
            raw_slugs = [s.strip() for s in source.split(",") if s.strip()]
            for s in raw_slugs:
                validate_slug(s)
            # v1.15.0：点名搜索 18+ 源站也不放行，否则搜索成了屏蔽的旁路
            slugs = minor_mode.filter_slugs(request, raw_slugs)
            name_map = {
                r[0]: r[1] for r in conn.execute(
                    select(sources.c.slug, sources.c.name).where(sources.c.slug.in_(slugs))
                ).all()
            } if slugs else {}
            slugs = [s for s in slugs if s in name_map]
        else:
            rows = conn.execute(
                select(sources.c.slug, sources.c.name)
                .where(*cond)
                .order_by(sources.c.id)
            ).all()
            name_map = {r[0]: r[1] for r in rows}
            slugs = list(name_map.keys())

    # 未成年模式下没有可见源站：返回标准空结果，不是 404（与 tag 空结果同语义）
    if not slugs and minor_mode.active(request):
        return {
            "code": SUCCESS_CODE, "wd": kw, "tag": tag or "",
            "page": page, "page_size": page_size, "pagecount": 0, "total": 0,
            "total_approximate": True, "total_is_partial": False,
            "truncated_sources": [], "sources_searched": 0, "sources_failed": [],
            "query_ms": 0, "elapsed_ms": 0, "minor_mode": True, "list": [],
        }

    # ---- tag 路径（v1.14.0 / B-01）----
    # 空源站也返回空结果而不是 404：标签检索的语义是「没有就是没有」，
    # 404 会让客户端弹错误提示（B-01 第 5 条）。
    if tag:
        if not slugs:
            return {
                "code": SUCCESS_CODE, "tag": tag, "page": page, "page_size": page_size,
                "pagecount": 0, "total": 0, "total_approximate": False,
                "query_ms": int((time.time() - started) * 1000), "list": [],
            }
        from .. import tags as tags_mod

        result = tags_mod.search_page(slugs, tag, page, page_size, type_name)
        items = [
            _list_item(r, source=r["source_slug"],
                       source_name=name_map.get(r["source_slug"], ""))
            for r in result["rows"]
        ]
        return {
            "code": SUCCESS_CODE,
            "tag": tag,
            "page": page,
            "page_size": page_size,
            "pagecount": result["pagecount"],
            # 标签结果集可由索引精确计数，不是估值
            "total": result["total"],
            "total_approximate": False,
            "total_is_partial": bool(result["partial"]),
            # v1.14.1：该标签词是否存在于标签倒排表。false = 精确段匹配下没有
            # 任何源站用它做标签（但标题全文检索可能命中）→ 客户端应回退 wd
            "tag_found": result["tag_found"],
            "sources_failed": result["failed"],
            "query_ms": result["elapsed_ms"],
            "elapsed_ms": result["elapsed_ms"],
            "list": items,
        }

    if not slugs:
        raise HTTPException(status_code=404, detail="没有可搜索的源站，请先同步数据")

    # 转义 LIKE 通配符，避免用户输入 % _ 造成全表匹配
    pattern = f"%{_esc_like(kw)}%"
    existing = database.list_tables("vod_")     # v1.10.0 / P6：一次取全，别每源站一次往返

    def search_one(slug: str) -> tuple:
        """查询单个源站表，返回 (slug, rows, truncated, error)。"""
        try:
            table = build_vod_table(slug)
            if table.name not in existing:
                return slug, [], False, None
            where = [table.c.name.like(pattern, escape="\\")]
            if type_name:
                where.append(table.c.type_name == type_name)
            with database.engine.connect() as conn:
                rows = conn.execute(
                    select(
                        table.c.vod_id, table.c.type_id, table.c.type_name,
                        table.c.name, table.c.pic, table.c.remarks, table.c.vod_time,
                    )
                    .where(*where)
                    .order_by(table.c.vod_time.desc(), table.c.vod_id.desc())
                    .limit(per_source_limit)
                ).mappings().all()
            truncated = len(rows) >= per_source_limit
            return slug, rows, truncated, None
        except Exception as e:  # 单源失败不影响整体
            log.warning("聚合搜索 [%s] 失败: %s", slug, e)
            return slug, [], False, str(e)

    # 2) 并行查询（PostgreSQL 支持真正的并行读；线程数受控避免 NAS IO 打满）
    collected: list = []
    failed: list = []
    truncated_sources: list = []
    workers = max(1, min(config.SEARCH_CONCURRENCY, len(slugs)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(search_one, s): s for s in slugs}
        for fut in as_completed(futures):
            slug, rows, truncated, err = fut.result()
            if err:
                failed.append({"slug": slug, "error": err})
                continue
            if truncated:
                truncated_sources.append(slug)
            for r in rows:
                collected.append(_list_item(
                    r, source=slug, source_name=name_map.get(slug, "")))

    # 3) 归并排序 + 分页（按更新时间倒序，同时间按 id 倒序）
    collected.sort(key=lambda x: (x["vod_time"] or "", x["vod_id"]), reverse=True)
    total_found = len(collected)
    pagecount = (total_found + page_size - 1) // page_size if total_found else 0
    start = (page - 1) * page_size
    items = collected[start : start + page_size]

    return {
        "code": SUCCESS_CODE,
        "wd": kw,
        "page": page,
        "page_size": page_size,
        "pagecount": pagecount,
        # 注意：total 为「各源站候选集上限内的命中数」，非全库精确总数（模糊搜索不做全量计数以保证响应速度）
        "total": total_found,
        # 模糊搜索的 total 是候选集内计数，属于近似值（B-04）
        "total_approximate": True,
        "total_is_partial": bool(truncated_sources),
        "truncated_sources": truncated_sources,
        "sources_searched": len(slugs) - len(failed),
        "sources_failed": failed,
        # v1.14.0：与 /api/v1/videos 对齐的耗时字段名（elapsed_ms 保留给老客户端）
        "query_ms": int((time.time() - started) * 1000),
        "elapsed_ms": int((time.time() - started) * 1000),
        "list": items,
    }


# ---------------------------------------------------------------------------
# 相关推荐（v1.9.0，服务端预计算）
#
# 读路径只有两次查询：vod_related 主键查一次 + vod_{slug} IN 补卡片字段，
# P95 < 50ms。预计算结果缺失（新视频还没被重算覆盖）时退化为实时兜底查询，
# 保证任何详情页的推荐栏都不为空。
# ---------------------------------------------------------------------------
def _related_cards(slug: str, source_name: str, vod_id: int, limit: int, table) -> tuple:
    from .. import related

    entries, fallback = related.get_related_entries(slug, vod_id, limit, table)
    cards = related.enrich_cards(slug, source_name, entries, table)
    return cards, fallback


@router.get("/api/v1/related")
def related_videos(
    request: Request,
    slug: str = Query(..., description="源站 slug"),
    id: int = Query(..., ge=1, description="视频 vod_id"),
    limit: int = Query(config.RELATED_LIMIT_DEFAULT, ge=1, le=config.RELATED_LIMIT_MAX,
                       description=f"推荐条数（1~{config.RELATED_LIMIT_MAX}）"),
):
    """
    视频相关推荐（预计算结果直读，P95 < 50ms）。

    返回 {code, slug, vod_id, limit, fallback, computed_at, algo_version, list}：
      - list 为对齐视频卡片结构的数组（含 source/source_name/score），
        source 用于客户端跨站点击跳转
      - fallback=true 表示**整条列表**都来自兜底链（同分类/同地区年代/最新收录），
        客户端可按需弱化展示或改成「猜你想看」；列表永远非空（只要该视频本身存在）
      - 视频不存在返回 404 标准错误结构

    v1.10.0 变更（对接需注意，均为纯新增或语义收紧）：
      - 新增 `algo_version`：当前算法版本。与旧值不同的缓存建议直接丢弃。
      - 卡片新增 `reason`：命中原因，逗号分隔，如 "title,actor" / "director"。
        兜底条目为空串。可按它做「因为你也看过…」这类展示，也可忽略。
      - 语义收紧：**仅同分类+同地区+同年代的配对不再进推荐位**（旧版会给这类
        配对 0.667 的高分，属于 Bug 级误判）。同一部片不会出现在自己的推荐里。
      - 零命中时的兜底条数从 12 条降到 6 条（RELATED_FALLBACK_LIMIT），
        不与真命中混排；`fallback=true` 时 `score` 恒为 0、`mode` 为 "fallback"。
    v1.15.0：未成年模式下 18+ 源站的相关推荐直接 404（推荐卡片也是内容入口）。
    """
    minor_mode.guard(request, validate_slug(slug))
    src, table = _require_table(slug)
    with database.engine.connect() as conn:
        exists = conn.execute(
            select(table.c.vod_id).where(table.c.vod_id == id)
        ).first()
    if not exists:
        raise HTTPException(status_code=404, detail=f"视频不存在: {slug}/{id}")
    from .. import related

    entries, fallback = related.get_related_entries(slug, id, limit, table)
    cards = related.enrich_cards(slug, src["name"], entries, table)
    computed_at = None
    if not fallback:
        with database.engine.connect() as conn:
            row = conn.execute(
                select(related.vod_related.c.computed_at).where(
                    related.vod_related.c.slug == slug, related.vod_related.c.vod_id == id
                )
            ).first()
            computed_at = row[0].isoformat() if row and row[0] else None
    return {
        "code": SUCCESS_CODE,
        "slug": slug,
        "vod_id": id,
        "limit": limit,
        "fallback": fallback,
        "computed_at": computed_at,
        "algo_version": config.RELATED_ALGO_VERSION,
        "list": cards,
    }


# ---------------------------------------------------------------------------
# 明星词库（v1.9.0 可选增强）：面板设置页维护的主演名单。
# 词库中的名字会扫描「缺失演员信息」视频的标题与简介，命中即推断为演员信号
# 参与相关推荐匹配（标题命中且字段稀疏=强信号不打折；仅简介命中=弱信号降权）。
# ---------------------------------------------------------------------------


def _normalize_star_term(raw: str) -> str:
    t = (raw or "").strip()[:64]
    return t


# 分隔符：半角/全角逗号、顿号、竖线、换行、制表符
_STAR_SPLIT_RE = re.compile(r"[,，、|\n\r\t]+")


def _parse_star_terms_input(raw) -> list:
    """
    批量词条解析（自动识别格式，用户怎么粘都行）：
      1. 标准 JSON 数组        ["a","b",] / ["a", "b"]
      2. JSON 数组片段（无括号） "最美宝儿",\n"困困狗",\n"魔鬼爱",
      3. 带引号的混合串        "a", 'b', "c"
      4. 纯分隔符串            a，b、c|d e / 每行一个
    带引号时优先按引号提取（引号内的分隔符不属于词条）；否则按分隔符切。
    """
    if raw is None:
        return []
    if isinstance(raw, list):
        raw = "\n".join(str(x) for x in raw)
    s = str(raw).strip()
    if not s:
        return []
    # 1) 标准 JSON 数组
    if s.startswith("["):
        try:
            data = json.loads(s)
            if isinstance(data, list):
                return [_normalize_star_term(str(x)) for x in data
                        if _normalize_star_term(str(x))]
        except (ValueError, TypeError):
            pass   # 不是合法 JSON，走下面的容错解析
    # 2/3) 含引号：优先提取引号片段（JSON 片段、带引号逗号串都能吃下）
    if '"' in s or "'" in s:
        quoted = re.findall(r"""["']([^"'\n]{1,64}?)["']""", s)
        out = []
        for x in quoted:
            t = _normalize_star_term(x)
            if t:
                out.append(t)
        if out:
            return out
    # 4) 纯分隔符串
    out = []
    for piece in _STAR_SPLIT_RE.split(s):
        t = _normalize_star_term(piece)
        if t:
            out.append(t)
    return out


@router.get("/api/v1/star-terms")
def star_terms_list():
    """
    词库清单（含停用条目，便于面板管理）。

    v1.10.0：`active` 改为返回**进程内当前生效的启用条数**（不再每次请求都
    重新编译词库正则）。刚写完词库时增删改接口会立即 reload，因此 `active`
    在一次写请求之后就是新值；多副本部署时其他实例最迟在下次重启/定时任务后一致。
    """
    from ..models import star_terms

    with database.engine.connect() as conn:
        rows = conn.execute(
            select(star_terms).order_by(star_terms.c.id.desc())
        ).mappings().all()
    from .. import related

    # v1.10.0 / P11：读接口不再重新加载+编译词库正则（万级词库约 10ms 且抖动内存），
    # 直接返回进程内已生效的条数；写接口（增删改）才 reload。
    return {
        "code": SUCCESS_CODE,
        "infer_mode": config.RELATED_STAR_INFER_MODE,
        "factor": config.RELATED_STAR_SIM_FACTOR,
        "active": related.star_stats()["count"],
        "list": [
            {
                "id": r["id"], "term": r["term"], "note": r["note"] or "",
                "aliases": r.get("aliases") or "",
                "enabled": bool(r["enabled"]),
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
            for r in rows
        ],
    }


@router.post("/api/v1/star-terms")
def star_terms_add(payload: dict):
    """
    批量添加词条。输入格式自动识别：
      JSON 数组 / 带引号的 JSON 片段（"名字", 一行一个）/
      逗号、全角逗号、顿号、竖线、换行分隔的纯文本 —— 一次粘贴大量名字即可。
    已存在的名字自动跳过（幂等）。返回 (added, skipped)。
    """
    from ..models import star_terms

    from .. import related

    terms = _parse_star_terms_input((payload or {}).get("terms"))
    note = str((payload or {}).get("note") or "")[:255]
    # 同一批内去重，保持顺序
    seen, ordered = set(), []
    for t in terms:
        if t not in seen:
            seen.add(t)
            ordered.append(t)
    if not ordered:
        raise HTTPException(status_code=400, detail="没有可添加的词条")
    # v1.10.0 / B1：一次 INSERT ... ON CONFLICT DO NOTHING，而不是逐条 try/except。
    # PG 里任何语句报错都会把整个事务置为 aborted，后续语句全部失败、提交时抛
    # PendingRollbackError —— 用户粘一批名单里夹着几个已存在的名字，就会整批 500、
    # 一个字都进不去（SQLite 是语句级回滚，所以 15 个测试全绿也测不出来）。
    stmt = (
        database.dialect_insert(star_terms)
        .values(term=bindparam("term"), note=note, enabled=1)
        .on_conflict_do_nothing(index_elements=["term"])
    )
    with database.write_txn() as conn:
        res = conn.execute(stmt, [{"term": t} for t in ordered])
        added = res.rowcount if (res.rowcount or 0) > 0 else 0
    skipped = max(0, len(ordered) - added)
    # 写操作后只重载一次（旧实现这里 reload 了两遍，等于把万级词库编译两次正则）
    active = related.reload_star_terms()
    return {
        "code": SUCCESS_CODE,
        "added": added,
        "skipped": skipped,
        "active": active,
        "msg": f"已添加 {added} 个词条（跳过已存在 {skipped} 个）"
               "。需要点「重算推荐」或等每日校准后生效",
    }


@router.patch("/api/v1/star-terms/{term_id}")
def star_terms_update(term_id: int, payload: dict):
    """修改词条（名字/备注/别名/启停）。别名变更后需重算才生效。"""
    from ..models import star_terms

    from .. import related

    fields = payload or {}
    changes: dict = {}
    if fields.get("term") is not None:
        t = _normalize_star_term(str(fields["term"]))
        if not t:
            raise HTTPException(status_code=400, detail="词条不能为空")
        changes["term"] = t
    if fields.get("note") is not None:
        changes["note"] = str(fields["note"])[:255]
    if fields.get("aliases") is not None:
        # 别名（v1.9.2）：逗号分隔存库，去重去空、单条上限 64 字、总长 400 字
        seen, parts = set(), []
        for a in re.split(r"[,，、|;/]", str(fields["aliases"])):
            v = a.strip()[:64]
            if v and v not in seen:
                seen.add(v)
                parts.append(v)
        changes["aliases"] = ",".join(parts)[:400]
    if fields.get("enabled") is not None:
        changes["enabled"] = 1 if fields["enabled"] else 0
    if not changes:
        raise HTTPException(status_code=400, detail="没有需要修改的字段")
    with database.write_txn() as conn:
        res = conn.execute(
            star_terms.update().where(star_terms.c.id == term_id).values(**changes)
        )
        if not (res.rowcount or 0):
            raise HTTPException(status_code=404, detail=f"词条不存在: {term_id}")
    return {"code": SUCCESS_CODE, "updated": sorted(changes), "active": related.reload_star_terms()}


@router.delete("/api/v1/star-terms/{term_id}")
def star_terms_delete(term_id: int):
    """删除词条。"""
    from ..models import star_terms

    from .. import related

    with database.write_txn() as conn:
        res = conn.execute(star_terms.delete().where(star_terms.c.id == term_id))
        if not (res.rowcount or 0):
            raise HTTPException(status_code=404, detail=f"词条不存在: {term_id}")
    return {"code": SUCCESS_CODE, "deleted": term_id, "active": related.reload_star_terms()}


@router.post("/api/v1/star-terms/recompute")
def star_terms_recompute():
    """
    词库变更后重算全部源站：① 明星 Tag 回填 + star_map 重建（v1.9.1）；
    ② 相关推荐重算（推断演员信号变化影响打分）。后台串行执行。
    """
    from .. import related, star_tags

    with database.engine.connect() as conn:
        targets = [r[0] for r in conn.execute(
            select(sources.c.slug).where(sources.c.enabled == 1, sources.c.total_videos > 0)
        ).all()]

    def _worker():
        for s in targets:
            star_tags.rebuild_source_safe(s, reason="star_terms_changed")
        for s in targets:
            related.recompute_source_safe(s, reason="star_terms_changed")

    threading.Thread(target=_worker, daemon=True).start()
    return {"code": SUCCESS_CODE, "targets": targets,
            "msg": f"词库变更重算已受理: {len(targets)} 个源站（明星 Tag + 相关推荐）"}


# ---------------------------------------------------------------------------
# 明星作品推荐（v1.9.1）：tag 回填 + 共享明星视频查询
# ---------------------------------------------------------------------------
@router.get("/api/v1/star/related")
def star_related_api(
    request: Request,
    slug: str = Query(..., description="源站 slug"),
    vod_id: int = Query(0, ge=0, description="视频 ID（与 name 二选一，id 优先）"),
    name: str = Query("", max_length=255, description="视频名称（先精确后模糊）"),
    limit: int = Query(config.STAR_RELATED_LIMIT_DEFAULT,
                       ge=1, le=config.STAR_RELATED_LIMIT_MAX),
):
    """
    返回与目标视频共享明星（词库 tag 交集）的其他视频。
    目标视频的明星 = star_map 里的映射（真实演员 ∩ 词库 + 标题命中推断）。

    v1.10.0：`truncated` 改成精确判定 —— 老实现用 `count(distinct vod_id)` 判
    「是否还有更多」，代价是多扫一遍；现在多取 1 条来判断，`truncated` 因此
    可以是 false（旧版在命中不足限制条数时也恒为 true，客户端「加载更多」按钮
    会一直亮着却点不出新内容）。
    """
    from .. import star_tags

    slug = validate_slug(slug)
    minor_mode.guard(request, slug)
    _src, _table = _require_table(slug)
    result = star_tags.star_related(slug, vod_id=vod_id, name=name, limit=limit)
    if result is None:
        raise HTTPException(status_code=404, detail="视频不存在")
    return {"code": SUCCESS_CODE, **result}


@router.get("/api/v1/star/works")
def star_works_api(
    request: Request,
    slug: str = Query(..., description="源站 slug"),
    term: str = Query(..., min_length=1, max_length=64, description="明星名（主名或别名）"),
    limit: int = Query(config.STAR_WORKS_LIMIT_DEFAULT,
                       ge=1, le=config.STAR_WORKS_LIMIT_MAX),
    exclude_id: int = Query(0, ge=0, description="排除的视频 ID（通常是当前详情页视频）"),
):
    """
    按明星查作品（v1.9.2，明星为中心）：直接取该明星的全部映射再按时间排序，
    不受「重叠明星数」影响 —— 客户端按明星切 Tab 时不会漏作品。

    term 无映射 / 视频已下架 → 200 + total:0 + list:[]（不是 404）。
    """
    from .. import star_tags

    slug = validate_slug(slug)
    minor_mode.guard(request, slug)
    _src, _table = _require_table(slug)
    result = star_tags.star_works(slug, term, limit=limit, exclude_id=exclude_id)
    return {"code": SUCCESS_CODE, **result}


@router.get("/api/v1/star/tags/untracked")
def star_tags_untracked(
    request: Request,
    slug: str = Query("", description="源站 slug；留空=全部启用源站抽样"),
    limit: int = Query(100, ge=1, le=500),
    sample: int = Query(0, ge=0, le=200000, description="抽样标题数（0=用配置默认）"),
):
    """
    未入词库的高频标题词 Top N（扩词库用，启发式候选）。
    抽样缺演员视频标题 → 屏蔽已命中词库片段 → 剩余文本 2~4 元 n-gram 频次统计。
    """
    from .. import star_tags

    if slug:
        slug = validate_slug(slug)
        # 诊断接口只返回词频候选、不含视频内容；显式点名 18+ 站时仍要挡住
        minor_mode.guard(request, slug)
        _require_table(slug)
    return {"code": SUCCESS_CODE, **star_tags.untracked_terms(slug=slug, limit=limit, sample=sample)}


@router.post("/api/v1/maintenance/star-tags")
def star_tags_rebuild(
    slug: str = Query("all", description="源站 slug；all=全部启用源站"),
):
    """手动触发明星 Tag 回填 + star_map 重建（后台串行执行，全量幂等）。"""
    from .. import star_tags

    if slug and slug != "all":
        slug = validate_slug(slug)
        _require_table(slug)
        targets = [slug]
    else:
        with database.engine.connect() as conn:
            targets = [r[0] for r in conn.execute(
                select(sources.c.slug).where(
                    sources.c.enabled == 1, sources.c.total_videos > 0
                )
            ).all()]

    def _worker():
        for s in targets:
            star_tags.rebuild_source_safe(s, reason="manual_rebuild")

    threading.Thread(target=_worker, daemon=True).start()
    return {"code": SUCCESS_CODE, "targets": targets, "msg": f"明星 Tag 重建已受理: {len(targets)} 个源站"}


@router.get("/api/v1/star/tags/status")
def star_tags_status():
    """各源站明星 Tag 覆盖情况：总视频数 / 缺演员视频数 / 已映射明星条数。"""
    from .. import star_tags

    return {"code": SUCCESS_CODE, "list": star_tags.status_all()}


@router.get("/api/v1/related/status")
def related_status():
    """各源站相关推荐覆盖率（管理/验收核对：videos / computed / coverage%）。"""
    from .. import related

    return {"code": SUCCESS_CODE, "algo_version": config.RELATED_ALGO_VERSION, "list": related.coverage_all()}


# ---------------------------------------------------------------------------
# 标签索引（v1.14.0 / B-01）
#
# 与 star_tags 的重建入口同一套调度思路：后台串行、幂等、可断点续跑。
# 区别是这里的进度显式落库（tag_index_state），因为百万级回填要能观测、
# 能中途重启接着跑 —— 而明星 Tag 重建是分批小任务，跑完即完。
# ---------------------------------------------------------------------------
@router.get("/api/v1/tag/status")
def tag_index_status():
    """
    标签倒排索引状态：是否就绪（决定 /healthz 是否声明 tag_search）、
    各源站回填进度（cursor / total / indexed）、总行数。

    排查「标签检索没结果」时先看这里：`ready=false` 说明还没建完，
    客户端此时本来就该继续用 wd 全文检索。
    """
    from .. import tags

    return {"code": SUCCESS_CODE, **tags.status()}


@router.post("/api/v1/maintenance/tag-index")
def tag_index_rebuild(
    slug: str = Query("", description="源站 slug；留空=全部启用源站"),
    force: bool = Query(False, description="true=清空该源站标签行后从头重建"),
):
    """
    手动触发标签倒排索引回填（后台，串行，可断点续跑）。

    日常不需要调：同步入库时会增量维护；升级后启动时也会自动补齐。
    用途：① 手工修复（怀疑索引与 vod_tag 不一致）；② 首次全量建完后
    想核对 `indexed` 行数；③ 改了 TAG_INDEX_* 参数后重跑。
    """
    from .. import tags

    if slug and slug != "all":
        slug = validate_slug(slug)
        _require_table(slug)
        slug = slug
    else:
        slug = ""   # 空串 = 全部
    accepted = tags.start_backfill(slug=slug, force=bool(force))
    return {
        "code": SUCCESS_CODE,
        "accepted": accepted,
        "force": bool(force),
        "targets": slug or "all",
        "msg": (
            "标签索引回填任务已受理（后台串行执行，可断点续跑）"
            if accepted else "已有同类回填任务在执行中，本次未重复提交"
        ),
    }


@router.post("/api/v1/maintenance/related-recompute")
def related_recompute(
    slug: str = Query("all", description="源站 slug；all=全部启用源站"),
):
    """手动触发相关推荐重算（后台执行）。算法调参后先用它验证，再考虑改 RELATED_ALGO_VERSION。"""
    from .. import related

    if slug and slug != "all":
        slug = validate_slug(slug)
        _require_table(slug)
        targets = [slug]
    else:
        with database.engine.connect() as conn:
            targets = [r[0] for r in conn.execute(
                select(sources.c.slug).where(
                    sources.c.enabled == 1, sources.c.total_videos > 0
                )
            ).all()]

    def _worker():
        for s in targets:
            related.recompute_source_safe(s, reason="manual_recompute")

    threading.Thread(target=_worker, daemon=True).start()
    return {"code": SUCCESS_CODE, "targets": targets, "msg": f"相关推荐重算已受理: {len(targets)} 个源站"}


# ---------------------------------------------------------------------------
# 苹果 CMS 兼容接口：/api/v1/{slug}?ac=list|detail
# ---------------------------------------------------------------------------
@router.get("/api/v1/{slug}")
def maccms_compat(
    slug: str,
    request: Request,
    ac: str = Query(default="detail", pattern="^(list|detail)$"),
    pg: int = Query(default=1, ge=1),
    ids: str = Query(default="", description="按 ID 批量取详情，逗号分隔"),
    wd: str = Query(default=""),
    play_raw: int = Query(0, ge=0, le=1,
                          description="v1.11.0：1=不做线路过滤/排序（排障用）"),
    paging: tuple = Depends(page_params),
):
    """
    与苹果 CMS 采集接口输出结构一致：
      ?ac=list   -> {code,page,pagecount,limit,total,class,list:[轻量字段]}
      ?ac=detail -> {code,page,pagecount,limit,total,class,list:[全字段含播放地址]}

    v1.11.0：`vod_play_list` 已按策略过滤（默认剔除 /share/、/play/、.html 这类
    分享页线路），`vod_play_url` 原始串保持不变 —— 采集端若自己解析原始串，
    行为与旧版完全一致；用 vod_play_list 的客户端自动拿到可播线路。
    v1.15.0：未成年模式下 18+ 源站的 MacCMS 兼容接口同样屏蔽 —— 第三方采集器
    和播放器内核也会走这条路，漏掉它等于整站屏蔽形同虚设。
    """
    minor_mode.guard(request, validate_slug(slug))
    src, table = _require_table(slug)
    page, page_size = paging
    play_meta: dict = {}
    with database.engine.connect() as conn:
        class_list = _maccms_class_map(conn, table)

        if ac == "detail" and ids:
            id_list = []
            for x in ids.split(","):
                try:
                    id_list.append(int(x.strip()))
                except ValueError:
                    continue
            if not id_list:
                return {"code": MACCMS_CODE, "page": 1, "pagecount": 1, "limit": page_size, "total": 0, "class": class_list, "list": []}
            rows = conn.execute(
                select(table).where(table.c.vod_id.in_(id_list))
            ).mappings().all()
            items = []
            for r in rows:
                # 与 /api/v1/videos/{slug}/{id} 共用同一个构造器：
                # 以前这里手写了另一份字段列表，v1.10.1 加演职员时就漏了这条路径。
                item = _detail_item(slug, r, play_raw=bool(play_raw), play_meta=play_meta)
                if config.RELATED_IN_DETAIL:
                    try:
                        cards, fb = _related_cards(slug, src["name"], r["vod_id"],
                                                   config.RELATED_LIMIT_DEFAULT, table)
                        item["vod_related"] = cards
                        item["vod_related_fallback"] = fb
                    except Exception as e:  # 推荐挂了不能连累采集端详情
                        log.warning("[%s] ac=detail 附带推荐失败（忽略）: %s", slug, e)
                items.append(item)
            resp = {
                "code": MACCMS_CODE, "page": 1, "pagecount": 1, "limit": page_size,
                "total": len(items), "class": class_list,
                "list": items,
            }
            if play_meta.get("filtered"):
                resp["play_line_filtered"] = True
            return resp

        detail = ac == "detail"
        total, pagecount, items = _query_vods(
            conn, table, slug, page, page_size, wd=wd or None, detail=detail,
            play_raw=bool(play_raw), play_meta=play_meta,
        )
    resp = {
        "code": MACCMS_CODE,
        "msg": "数据列表",
        "page": page,
        "pagecount": pagecount,
        "limit": page_size,
        "total": total,
        "class": class_list,
        "list": items,
    }
    if play_meta.get("filtered"):
        resp["play_line_filtered"] = True
    return resp
