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
    return path in PROTECTED_PATHS or path in PUBLIC_PATHS


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

    srcs = repo.list_sources(enabled_only=True)
    all_results = []
    for src in srcs:
        try:
            rows = repo.search_in_source(src["slug"], q, limit=100)
        except Exception as exc:  # noqa: BLE001 - 单源失败吞掉（Helios 行为）
            log.warning("selenetv search[%s] failed: %s", src["slug"], exc)
            continue
        for row in rows:
            episodes, titles = extract_episodes(row.get("play_url") or "", row.get("content") or "")
            if not episodes:                       # Helios 过滤无 m3u8 的结果
                continue
            all_results.append(search_result(
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
    srcs = repo.list_sources(enabled_only=True)
    if not srcs:
        return security.text_error(500, "No API sites configured")

    def event_stream():
        yield serializers.sse_frame(serializers.sse_start(q, len(srcs)))
        total_results = 0
        completed = 0
        for src in srcs:
            try:
                rows = repo.search_in_source(src["slug"], q, limit=100)
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
                yield serializers.sse_frame(
                    serializers.sse_source_result(src["slug"], src["name"] or "", results)
                )
                total_results += len(results)
                completed += 1
            except Exception as exc:  # noqa: BLE001
                yield serializers.sse_frame(
                    serializers.sse_source_error(src["slug"], src["name"] or "", str(exc))
                )
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
        for s in repo.list_sources(enabled_only=True)
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
    srcs = repo.list_sources(enabled_only=True)
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
@selenetv_router.api_route("/api/live/sources", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def live_sources(request: Request):
    err = _method_guard(request, {"GET"}) or _guard(request)
    if err:
        return err
    rows = repo.live_sources_all()
    data = []
    for r in rows:
        item = {
            "key": r["key"], "name": r["name"] or "", "url": r["url"] or "",
            "epg": r["epg"] or "", "from": "config", "disabled": bool(r["disabled"]),
        }
        if r.get("ua"):                            # ua,omitempty
            item["ua"] = r["ua"]
        data.append(item)
    return _json({"success": True, "data": data})
