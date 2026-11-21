"""
SeleneTV 专用 API —— 10 条 Helios 风格路由。

与 Helios main.go 注册的路由逐条对应，响应形状/错误文本/缓存头全部复刻：
  POST   /api/login
  GET    /api/search
  GET    /api/search/ws        (SSE)
  GET    /api/search/resources
  GET    /api/search/suggestions
  GET    /api/detail
  GET|POST|DELETE /api/favorites
  GET|POST|DELETE /api/searchhistory
  GET|POST|DELETE /api/playrecords
  GET    /api/live/sources

挂载方式（见 app/selenetv/__init__.py + main.py 改动）：
  app.include_router(selenetv_router)
"""
import json
import logging
import re
from typing import Any, Dict

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.responses import PlainTextResponse

from . import repository as repo
from . import security, serializers
from .serializers import extract_episodes, search_result

log = logging.getLogger("vodsync.selenetv.router")

selenetv_router = APIRouter()

# Helios detail: id 必须匹配 ^[\w-]+$
_RE_ID = re.compile(r"^[\w-]+$")

# Helios search/detail 缓存头
_CACHE_HEADERS = {
    "Cache-Control": "public, max-age=7200, s-maxage=7200",
    "CDN-Cache-Control": "public, s-maxage=7200",
    "Vercel-CDN-Cache-Control": "public, s-maxage=7200",
    "Netlify-Vary": "query",
}

# 本模块自己的受保护路径（供 main._auth_guard 精确放行）
PROTECTED_PATHS = {
    "/api/search", "/api/search/ws", "/api/search/resources",
    "/api/search/suggestions", "/api/detail", "/api/favorites",
    "/api/searchhistory", "/api/playrecords", "/api/live/sources",
    "/api/adfilter/rules",  # [广告过滤] 规则包拉取（Helios Cookie 鉴权）
    # [18+ Tab] 源站/分类/视频（Helios Cookie 鉴权 + allow_adult 守卫）
    "/api/adult/permission", "/api/adult/sources",
    "/api/adult/categories", "/api/adult/videos",
}
PUBLIC_PATHS = {"/api/login"}


def is_selenetv_path(path: str) -> bool:
    """
    给 vodsync 全局鉴权闸门用的判断：只放行本模块精确注册的 10 条路径。

    同时尊重 SELENETV_API_ENABLED 开关——关闭时不放行，避免未挂载的路径
    绕过 vodsync 原有闸门（保持关闭态与改造前行为完全一致）。
    """
    import os

    if os.environ.get("SELENETV_API_ENABLED", "true").strip().lower() != "true":
        return False
    if path in PROTECTED_PATHS or path in PUBLIC_PATHS:
        return True
    # [直播源融合] /live/playlist/{key} 是 app 裸 GET 拉取 m3u 的公开端点：
    # 绕过 vodsync 会话闸门（router 内不做鉴权，内容仅为公开 IPTV 列表）。
    if path.startswith("/live/playlist/"):
        return True
    return False


def _json(data: Any, status: int = 200, headers: Dict[str, str] = None) -> Response:
    """紧凑 JSON（对齐 sonic 无空格输出）。"""
    content = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return Response(
        content=content,
        status_code=status,
        media_type="application/json",
        headers=headers,
    )


def _guard(request: Request) -> Response | None:
    """鉴权失败返回 Helios text/plain 响应，否则 None。"""
    if not security.request_authenticated(request):
        return security.text_error(401, "Authentication required")
    return None


def _method_guard(request: Request, allowed: set) -> Response | None:
    """方法不符 → 405 Method not allowed（Helios 纯文本）。"""
    if request.method not in allowed:
        return security.text_error(405, "Method not allowed")
    return None


def _allow_adult(request: Request) -> bool:
    """当前用户是否允许访问 18+ 源（未登录由 _guard 先挡，这里只为过滤用）。"""
    return security.allows_adult(request)


# ---------------------------------------------------------------------------
# ① POST /api/login —— 免鉴权
# ---------------------------------------------------------------------------
@selenetv_router.api_route("/api/login", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def login(request: Request):
    err = _method_guard(request, {"POST"})
    if err:
        return err
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return security.text_error(400, "Invalid request body")
    if not isinstance(body, dict):
        return security.text_error(400, "Invalid request body")
    username = str(body.get("username") or "")
    password = str(body.get("password") or "")
    if not username or not password:
        return security.text_error(400, "Username and password are required")
    if not security.check_credentials(username, password):
        return security.text_error(401, "Invalid credentials")
    try:
        return security.issue_login_response(username, password)
    except Exception:  # noqa: BLE001
        log.exception("selenetv login failed")
        return security.text_error(500, "Internal server error")


# ---------------------------------------------------------------------------
# ② GET /api/search —— 多源聚合搜索，{"results":[...]}
# ---------------------------------------------------------------------------
@selenetv_router.api_route("/api/search", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def search(request: Request):
    err = _method_guard(request, {"GET"}) or _guard(request)
    if err:
        return err
    q = (request.query_params.get("q") or "").strip()
    if not q:
        return _json({"results": []}, headers=_CACHE_HEADERS)

    srcs = repo.list_sources(enabled_only=True, allow_adult=_allow_adult(request))
    if not srcs:
        return _json({"results": []}, headers=_CACHE_HEADERS)

    # 与 vodsync 原生聚合搜索一致：一次取全 vod_ 表集合 + 线程池并发，
    # 避免串行遍历 64 源站导致 710 万行规模下整体超时（P0 性能修复）。
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from .. import config as _config

    known = repo.existing_vod_tables()

    def _one(src):
        rows = repo.search_in_source(src["slug"], q, limit=100, known_tables=known)
        out = []
        for row in rows:
            episodes, titles = extract_episodes(
                row.get("play_url") or "", row.get("content") or ""
            )
            if not episodes:                       # Helios 过滤无 m3u8 的结果
                continue
            out.append(search_result(
                vod_id=row.get("vod_id"),
                name=row.get("name") or "",
                pic=row.get("pic") or "",
                episodes=episodes,
                episodes_titles=titles,
                source=src["slug"],
                source_name=src["name"] or "",
                year=row.get("vod_year") or "",
                content=row.get("content"),
                type_name=row.get("type_name"),
            ))
        return out

    all_results = []
    workers = max(1, min(getattr(_config, "SEARCH_CONCURRENCY", 4), len(srcs)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_one, src): src for src in srcs}
        for fut in as_completed(futures):
            src = futures[fut]
            try:
                all_results.extend(fut.result())
            except Exception as exc:  # noqa: BLE001 - 单源失败吞掉（Helios 行为）
                log.warning("selenetv search[%s] failed: %s", src["slug"], exc)
    return _json({"results": all_results}, headers=_CACHE_HEADERS)


# ---------------------------------------------------------------------------
# ③ GET /api/search/ws —— SSE 流式搜索（驼峰事件 + 毫秒时间戳）
# ---------------------------------------------------------------------------
@selenetv_router.api_route("/api/search/ws", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def search_ws(request: Request):
    # 注意：Helios 该 handler 无方法校验，这里也不做 405
    err = _guard(request)
    if err:
        return err
    q = (request.query_params.get("q") or "").strip()
    if not q:
        return Response(
            content=serializers.sse_frame({"error": "搜索关键词不能为空"}),
            status_code=400,
            media_type="text/event-stream",
        )
    srcs = repo.list_sources(enabled_only=True, allow_adult=_allow_adult(request))
    if not srcs:
        return security.text_error(500, "No API sites configured")

    def event_stream():
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from .. import config as _config

        yield serializers.sse_frame(serializers.sse_start(q, len(srcs)))
        total_results = 0
        completed = 0
        known = repo.existing_vod_tables()

        def _one(src):
            rows = repo.search_in_source(src["slug"], q, limit=100, known_tables=known)
            results = []
            for row in rows:
                episodes, titles = extract_episodes(
                    row.get("play_url") or "", row.get("content") or ""
                )
                if not episodes:
                    continue
                results.append(search_result(
                    vod_id=row.get("vod_id"), name=row.get("name") or "",
                    pic=row.get("pic") or "", episodes=episodes,
                    episodes_titles=titles, source=src["slug"],
                    source_name=src["name"] or "", year=row.get("vod_year") or "",
                    content=row.get("content"), type_name=row.get("type_name"),
                ))
            return results

        workers = max(1, min(getattr(_config, "SEARCH_CONCURRENCY", 4), len(srcs)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_one, src): src for src in srcs}
            for fut in as_completed(futures):
                src = futures[fut]
                try:
                    results = fut.result()
                    yield serializers.sse_frame(
                        serializers.sse_source_result(src["slug"], src["name"] or "", results)
                    )
                    total_results += len(results)
                except Exception as exc:  # noqa: BLE001
                    yield serializers.sse_frame(
                        serializers.sse_source_error(src["slug"], src["name"] or "", str(exc))
                    )
                completed += 1
        yield serializers.sse_frame(serializers.sse_complete(total_results, completed))

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET",
            "Access-Control-Allow-Headers": "Content-Type",
        },
    )


# ---------------------------------------------------------------------------
# ④ GET /api/search/resources —— 裸数组 [{key,api,name,detail}]
# ---------------------------------------------------------------------------
@selenetv_router.api_route("/api/search/resources", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def search_resources(request: Request):
    err = _method_guard(request, {"GET"}) or _guard(request)
    if err:
        return err
    sites = [
        {"key": s["slug"], "api": s["api_url"] or "", "name": s["name"] or "",
         "detail": s["homepage"] or ""}
        for s in repo.list_sources(enabled_only=True, allow_adult=_allow_adult(request))
    ]
    return _json(sites)


# ---------------------------------------------------------------------------
# ⑤ GET /api/search/suggestions —— {"suggestions":[{"text":...}]}
# ---------------------------------------------------------------------------
@selenetv_router.api_route("/api/search/suggestions", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def search_suggestions(request: Request):
    err = _method_guard(request, {"GET"}) or _guard(request)
    if err:
        return err
    q = (request.query_params.get("q") or "").strip()
    if not q:
        return _json({"suggestions": []})
    srcs = repo.list_sources(enabled_only=True, allow_adult=_allow_adult(request))
    if not srcs:
        return _json({"suggestions": []})
    # Helios 只查第一个配置站点
    titles = repo.suggestions(srcs[0]["slug"], q, limit=10)
    return _json({"suggestions": [{"text": t} for t in titles]})


# ---------------------------------------------------------------------------
# ⑥ GET /api/detail —— 裸 SearchResult
# ---------------------------------------------------------------------------
@selenetv_router.api_route("/api/detail", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def detail(request: Request):
    err = _method_guard(request, {"GET"}) or _guard(request)
    if err:
        return err
    vid = request.query_params.get("id") or ""
    source = request.query_params.get("source") or ""
    if not vid or not source:
        return security.text_error(400, "缺少必要参数")
    if not _RE_ID.match(vid):
        return security.text_error(400, "无效的视频ID格式")
    src = repo.get_source(source)
    if not src:
        return security.text_error(400, "无效的API来源")
    # [用户管理] 18+ 权限守卫：无权限用户不得访问标记为 18+ 的源（404 不暴露存在性）
    if not _allow_adult(request):
        from .. import minor_mode

        if minor_mode.is_adult(src.get("is_adult")):
            return security.text_error(404, "无效的API来源")
    row = repo.get_detail(source, vid)
    if not row:
        return security.text_error(500, "获取视频详情失败: 未找到该影片")
    episodes, titles = extract_episodes(row.get("play_url") or "", row.get("content") or "")
    result = search_result(
        vod_id=row.get("vod_id"), name=row.get("name") or "",
        pic=row.get("pic") or "", episodes=episodes, episodes_titles=titles,
        source=source, source_name=src["name"] or "", year=row.get("vod_year") or "",
        content=row.get("content"), type_name=row.get("type_name"),
    )
    return _json(result, headers=_CACHE_HEADERS)


# ---------------------------------------------------------------------------
# ⑦ GET|POST|DELETE /api/favorites
# ---------------------------------------------------------------------------
@selenetv_router.api_route("/api/favorites", methods=["GET", "POST", "DELETE", "PUT", "PATCH"])
async def favorites(request: Request):
    err = _method_guard(request, {"GET", "POST", "DELETE"}) or _guard(request)
    if err:
        return err
    if request.method == "GET":
        rows = repo.favorites_all()
        return _json({k: serializers.favorite_to_dict(v) for k, v in rows.items()})
    if request.method == "POST":
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return security.text_error(400, "Failed to read request body")
        if not isinstance(body, dict) or "key" not in body or "favorite" not in body:
            return security.text_error(400, "Invalid request body")
        try:
            repo.favorites_upsert(str(body["key"]), body["favorite"])
        except ValueError as exc:
            return security.text_error(400, str(exc))
        except Exception as exc:  # noqa: BLE001
            log.exception("favorites upsert failed")
            return security.text_error(500, f"Failed to upsert favorite: {exc}")
        return _json(serializers.success_true())
    # DELETE
    key = request.query_params.get("key") or ""
    try:
        repo.favorites_delete(key)
    except ValueError as exc:
        return security.text_error(400, str(exc))
    except Exception as exc:  # noqa: BLE001
        log.exception("favorites delete failed")
        return security.text_error(500, f"Failed to delete favorite: {exc}")
    return _json(serializers.success_true())


# ---------------------------------------------------------------------------
# ⑧ GET|POST|DELETE /api/searchhistory
# ---------------------------------------------------------------------------
@selenetv_router.api_route("/api/searchhistory", methods=["GET", "POST", "DELETE", "PUT", "PATCH"])
async def search_history(request: Request):
    err = _method_guard(request, {"GET", "POST", "DELETE"}) or _guard(request)
    if err:
        return err
    if request.method == "GET":
        return _json(repo.history_get())
    if request.method == "POST":
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return security.text_error(400, "Invalid request body")
        keyword = str((body or {}).get("keyword") or "") if isinstance(body, dict) else ""
        if not keyword:
            return security.text_error(400, "keyword parameter is required")
        try:
            return _json(repo.history_update(keyword))
        except Exception:  # noqa: BLE001
            log.exception("history update failed")
            return security.text_error(500, "Failed to update search history")
    # DELETE：keyword 为空 = 清空全部
    keyword = request.query_params.get("keyword") or ""
    try:
        repo.history_delete(keyword if keyword else None)
    except Exception:  # noqa: BLE001
        log.exception("history delete failed")
        return security.text_error(500, "Failed to delete search history keyword")
    return _json(serializers.success_true())


# ---------------------------------------------------------------------------
# ⑨ GET|POST|DELETE /api/playrecords
# ---------------------------------------------------------------------------
@selenetv_router.api_route("/api/playrecords", methods=["GET", "POST", "DELETE", "PUT", "PATCH"])
async def play_records(request: Request):
    err = _method_guard(request, {"GET", "POST", "DELETE"}) or _guard(request)
    if err:
        return err
    if request.method == "GET":
        rows = repo.playrecords_all()
        return _json({k: serializers.playrecord_to_dict(v) for k, v in rows.items()})
    if request.method == "POST":
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return security.text_error(400, "Failed to read request body")
        if not isinstance(body, dict) or "key" not in body or "record" not in body:
            return security.text_error(400, "Invalid request body")
        try:
            repo.playrecords_upsert(str(body["key"]), body["record"])
        except ValueError as exc:
            return security.text_error(400, str(exc))
        except Exception as exc:  # noqa: BLE001
            log.exception("playrecords upsert failed")
            return security.text_error(500, f"Failed to upsert play record: {exc}")
        return _json(serializers.success_true())
    # DELETE：key 为空 = 删除全部
    key = request.query_params.get("key") or ""
    try:
        repo.playrecords_delete(key if key else None)
    except ValueError as exc:
        return security.text_error(400, str(exc))
    except Exception as exc:  # noqa: BLE001
        log.exception("playrecords delete failed")
        return security.text_error(500, f"Failed to delete play record: {exc}")
    return _json(serializers.success_true())


# ---------------------------------------------------------------------------
# ⑩ GET /api/live/sources —— {"success":true,"data":[...]}
# ---------------------------------------------------------------------------
def _live_base_url(request: Request) -> str:
    """本地 M3U 端点的对外基址：配置优先，其次按请求 Host 推导（自适应 IP/域名/端口）。"""
    import os

    configured = os.environ.get("SELENETV_LIVE_BASE_URL", "").strip().rstrip("/")
    if configured:
        return configured
    # 反代场景优先取 X-Forwarded-Proto/Host，否则用请求自身的 scheme/netloc。
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme).split(",")[0].strip()
    host = request.headers.get("x-forwarded-host", request.headers.get("host", request.url.netloc))
    host = host.split(",")[0].strip()
    return f"{scheme}://{host}"


def _local_playlist_marker() -> str:
    """仓储里 url 用此前缀表示"由本地端点分发"，router 据此动态生成绝对 url。"""
    return "local:"


@selenetv_router.api_route("/api/live/sources", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def live_sources(request: Request):
    err = _method_guard(request, {"GET"}) or _guard(request)
    if err:
        return err
    base = _live_base_url(request)
    marker = _local_playlist_marker()
    rows = repo.live_sources_all()
    data = []
    for r in rows:
        raw_url = r["url"] or ""
        if raw_url.startswith(marker) or raw_url == "":
            # iptv4 等本地托管源：url 指向 /live/playlist/{key}（免鉴权，app 裸 GET）。
            # 注意：老的空 url 记录也统一指向本地端点；若确有外部源请填完整 http(s) url。
            url = f"{base}/live/playlist/{r['key']}"
        else:
            url = raw_url
        item = {
            "key": r["key"], "name": r["name"] or "", "url": url,
            "epg": r["epg"] or "", "from": "config", "disabled": bool(r["disabled"]),
        }
        if r.get("ua"):                            # ua,omitempty
            item["ua"] = r["ua"]
        data.append(item)
    return _json({"success": True, "data": data})


# ---------------------------------------------------------------------------
# ⑪ [直播源融合] GET /live/playlist/{key} —— 免鉴权分发 m3u 原文
# 非 /api/ 路径：app 用 HttpURLConnection 裸 GET（不带 Cookie）拉播放列表，
# 故必须公开；内容仅为公开 IPTV 播放列表，不含任何敏感信息。
# ---------------------------------------------------------------------------
@selenetv_router.api_route(
    "/live/playlist/{key}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD"]
)
async def live_playlist(request: Request, key: str):
    err = _method_guard(request, {"GET", "HEAD"})
    if err:
        return err
    row = repo.live_playlist_get(key)
    if not row or not row.get("content"):
        return security.text_error(404, "live playlist not found")
    content = row["content"]
    headers = {
        "Cache-Control": "public, max-age=300",
        "X-Content-Type-Options": "nosniff",
    }
    if row.get("fetched_at"):
        headers["ETag"] = f'"{key}-{row["fetched_at"]}"'
    return Response(
        content=content,
        status_code=200,
        media_type="application/vnd.apple.mpegurl; charset=utf-8",
        headers=headers,
    )

# ---------------------------------------------------------------------------
# ⑫ [广告过滤] GET /api/adfilter/rules —— 规则包拉取（Helios Cookie 鉴权，只读）
#
# 契约与 /api/v1/adfilter/rules 完全一致（四态：changed/version/rules），
# 差别只在鉴权：SeleneTV 客户端没有 vodsync 的 API Token（late-show-app 走
# Bearer Token，SeleneTV 登录体系是 Helios Cookie），故在本模块下提供同形
# 端点，走 SeleneTV 自己的鉴权。只读、无副作用，复用 app/adfilter 现有逻辑。
# ---------------------------------------------------------------------------
@selenetv_router.get("/api/adfilter/rules")
def selenetv_adfilter_rules(
    request: Request,
    response: Response,
    version: int = 0,
    channel: str | None = None,
):
    err = _guard(request)
    if err:
        return err
    from .. import adfilter as _adfilter
    from .. import config as _config

    # 规则随时可能被面板改，别让中间层缓存住（客户端自己有 30min TTL）
    response.headers["Cache-Control"] = "no-store"
    if not _config.ADFILTER_ENABLED:
        return JSONResponse(status_code=404, content={"error": "adfilter disabled"})
    if version < 0:
        version = 0
    state = _adfilter.current(_adfilter.normalize_channel(channel))
    try:
        v = int(version)
    except (TypeError, ValueError):
        v = 0
    if v == state["version"]:
        return {"changed": False, "version": state["version"]}
    if v > state["version"]:
        # 服务端版本更低（回滚过）：不给 rules，客户端继续用本地较新的包
        return {"changed": False, "version": state["version"]}
    return {"changed": True, "version": state["version"], "rules": state["rules"]}


# ---------------------------------------------------------------------------
# ⑬ [18+ Tab] GET /api/adult/permission —— 当前登录用户是否有 18+ 访问权限
#
# 客户端在登录后/返回首页时轮询此端点决定「18+」tab 的显隐：
#   - 未登录/登录失效 → _guard 401 → 客户端隐藏 tab
#   - 登录但无权限   → {"allowed": false} → 客户端隐藏 tab
#   - 登录且有权限   → {"allowed": true}
# ---------------------------------------------------------------------------
@selenetv_router.get("/api/adult/permission")
def selenetv_adult_permission(request: Request):
    err = _guard(request)
    if err:
        return err
    # no-store：权限随时可能被面板改动，且客户端要求「失效即隐藏」
    return _json({"allowed": bool(_allow_adult(request))},
                 headers={"Cache-Control": "no-store"})


def _adult_guard(request: Request) -> Response | None:
    """18+ Tab 公共守卫前半段：鉴权 + allow_adult 权限。"""
    err = _guard(request)
    if err:
        return err
    if not _allow_adult(request):
        return security.text_error(403, "Adult content access denied")
    return None


def _adult_source_or_error(request: Request, slug: str):
    """18+ Tab 需要定位源站的端点的公共守卫：鉴权 + 权限 + 源站有效性。

    返回 (source_row, None) 或 (None, error_response)。
    无 18+ 权限返回 403（tab 本身就不会展示给无权用户，403 足够明确）。
    """
    err = _adult_guard(request)
    if err:
        return None, err
    if not slug:
        return None, security.text_error(400, "缺少必要参数")
    src = repo.get_source(slug)
    if not src:
        return None, security.text_error(400, "无效的API来源")
    return src, None


# ---------------------------------------------------------------------------
# ⑭ [18+ Tab] GET /api/adult/sources —— 18+ 源站列表 [{"key","name","detail"}]
# 形状与 /api/search/resources 对齐，客户端可复用同一套展示逻辑。
# ---------------------------------------------------------------------------
@selenetv_router.get("/api/adult/sources")
def selenetv_adult_sources(request: Request):
    err = _adult_guard(request)
    if err:
        return err
    sites = [
        {"key": s["slug"], "api": s["api_url"] or "", "name": s["name"] or "",
         "detail": s["homepage"] or ""}
        for s in repo.list_adult_sources(enabled_only=True)
    ]
    return _json(sites, headers={"Cache-Control": "no-store"})


# ---------------------------------------------------------------------------
# ⑮ [18+ Tab] GET /api/adult/categories?source=slug —— 单源分类列表
# ---------------------------------------------------------------------------
@selenetv_router.get("/api/adult/categories")
def selenetv_adult_categories(request: Request):
    source = (request.query_params.get("source") or "").strip()
    _, err = _adult_source_or_error(request, source)
    if err:
        return err
    cats = repo.list_categories(source)
    return _json(cats, headers={"Cache-Control": "no-store"})


# ---------------------------------------------------------------------------
# ⑯ [18+ Tab] GET /api/adult/videos?source=slug&type_id=&page=&size=
# 单源分类视频分页列表：{"list":[{vod_id,name,pic,remarks,vod_year,type_name,
# type_id}],"total","page","size"}
# ---------------------------------------------------------------------------
@selenetv_router.get("/api/adult/videos")
def selenetv_adult_videos(request: Request):
    source = (request.query_params.get("source") or "").strip()
    _, err = _adult_source_or_error(request, source)
    if err:
        return err
    try:
        page = int(request.query_params.get("page") or 1)
    except ValueError:
        page = 1
    try:
        size = int(request.query_params.get("size") or 25)
    except ValueError:
        size = 25
    type_raw = (request.query_params.get("type_id") or "").strip()
    type_id: int | None = None
    if type_raw:
        try:
            type_id = int(type_raw)
        except ValueError:
            return security.text_error(400, "无效的分类ID")
    data = repo.list_videos(source, type_id=type_id, page=page, size=size)
    return _json(data, headers={"Cache-Control": "no-store"})
