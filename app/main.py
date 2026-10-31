"""
应用入口：FastAPI 应用 + 启动初始化（建库、首次拉取源站列表、启动调度器）。

启动参数（见 Dockerfile CMD）:
  uvicorn app.main:app --host 0.0.0.0 --port 8000
"""
import logging
import os
import threading
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from sqlalchemy.exc import OperationalError, TimeoutError as SADBTimeoutError

from . import config, database
from .routes.api import router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("vodsync.main")

WEB_DIR = os.path.join(os.path.dirname(__file__), "web")

app = FastAPI(
    title="VodSync 影视数据同步服务",
    description=(
        "遍历 ziyuanzhan latest.json 源站列表，定时增量同步苹果 CMS(MacCMS) 影视数据到本地数据库，"
        "并对外提供统一的视频列表/详情 API（source 参数区分源站）。"
    ),
    version="1.5.1",
)

app.include_router(router)


# ---------------------------------------------------------------------------
# 访问控制闸门（v1.5.0）
#
# 放在 CORS 之前注册，这样 CORS 处于最外层 —— 401 响应也会带上 CORS 头，
# 浏览器里的 H5 才能读到错误信息，而不是一个不透明的 Network Error。
#
# 放行规则：
#   /healthz、/login、/api/v1/auth/{login,state}、静态资源 —— 永远可访问
#   /api/**        —— Token（Bearer / X-API-Key / ?token=）或登录会话 cookie
#   其它（面板页面、/docs）—— 必须已登录，未登录跳登录页
# 鉴权关闭时全部放行（面板里可随时开关）。
# ---------------------------------------------------------------------------
@app.middleware("http")
async def _auth_guard(request: Request, call_next):
    from . import auth

    path = request.url.path
    if request.method == "OPTIONS" or not auth.is_enabled() or auth.is_public(path):
        return await call_next(request)

    if auth.is_api_path(path):
        if auth.request_authorized(request):
            return await call_next(request)
        return JSONResponse(
            status_code=401,
            content={
                "detail": (
                    "未授权：请求头需携带 Authorization: Bearer <token>"
                    "（也支持 X-API-Key 头或 ?token= 参数）；浏览器需先登录管理面板"
                ),
                "code": 401,
            },
            headers={"WWW-Authenticate": "Bearer"},
        )

    if auth.has_session(request):
        return await call_next(request)

    if request.method in ("GET", "HEAD") and "text/html" in request.headers.get("accept", ""):
        target = path + (("?" + request.url.query) if request.url.query else "")
        return RedirectResponse(f"/login?next={quote(target, safe='')}", status_code=303)
    return JSONResponse(status_code=401, content={"detail": "请先登录管理面板", "code": 401})



# ---------------------------------------------------------------------------
# 数据库繁忙时的统一响应
#
# 连接池排队超时（TimeoutError）或 SQLite 写锁超时（OperationalError: database is locked）
# 属于"服务临时过载"，不是代码 bug。返回 503 + 明确提示，让客户端知道该退避重试，
# 而不是抛 500 让面板/App 显示一堆堆栈。
# ---------------------------------------------------------------------------
@app.exception_handler(SADBTimeoutError)
def _db_pool_timeout_handler(request: Request, exc: SADBTimeoutError):  # noqa: ANN001
    log.error("数据库连接池等待超时: %s %s", request.url.path, exc)
    return JSONResponse(
        status_code=503,
        content={
            "detail": "服务繁忙：数据库连接池已满，请稍后重试（可减少同时运行的全量同步数量）",
            "code": 0,
        },
        headers={"Retry-After": "5"},
    )


@app.exception_handler(OperationalError)
def _db_operational_handler(request: Request, exc: OperationalError):  # noqa: ANN001
    log.error("数据库操作失败: %s %s", request.url.path, exc)
    return JSONResponse(
        status_code=503,
        content={"detail": "数据库繁忙或暂不可用，请稍后重试", "code": 0},
        headers={"Retry-After": "5"},
    )

# 客户端开发支持：放行浏览器跨域调用（H5 / Web App 直接可调；原生 App 不受影响）
# 可通过环境变量 CORS_ORIGINS 限制来源，逗号分隔；默认允许全部。
_cors_origins = [
    o.strip() for o in os.environ.get("CORS_ORIGINS", "*").split(",") if o.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins or ["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/", include_in_schema=False)
def admin_panel():
    """管理面板（单页应用，无需构建）: 登录态由 HttpOnly cookie 维持。"""
    return FileResponse(os.path.join(WEB_DIR, "index.html"), media_type="text/html")


@app.get("/login", include_in_schema=False)
def login_page(request: Request):
    """登录页：已登录或未启用鉴权时直接跳回面板。"""
    from . import auth

    if not auth.is_enabled():
        return RedirectResponse("/", status_code=303)
    if auth.has_session(request):
        return RedirectResponse(_safe_next(request.query_params.get("next")), status_code=303)
    return FileResponse(os.path.join(WEB_DIR, "login.html"), media_type="text/html")


def _safe_next(raw):
    """登录后跳转地址：只允许站内相对路径，避免开放重定向。"""
    if not raw or not raw.startswith("/") or raw.startswith("//"):
        return "/"
    return raw


@app.on_event("startup")
def on_startup():
    database.init_db()
    log.info("数据库已初始化: %s", config.DATABASE_URL)

    from . import auth

    auth.ensure_init()   # 初始化面板密码 / Token / 鉴权开关（首次写默认值）

    from . import pause

    pause.load_state()   # 恢复持久化的同步暂停状态（重启不丢）

    # 后台执行首次初始化：拉源站列表 -> （可选）全量同步 -> 启动调度器
    threading.Thread(target=_bootstrap, daemon=True).start()


def _bootstrap():
    """首次启动引导：若 sources 为空则拉取 latest.json 并触发全量同步。"""
    from . import syncer
    from .models import sources
    from sqlalchemy import select, func

    try:
        with database.engine.connect() as conn:
            count = conn.execute(select(func.count()).select_from(sources)).scalar() or 0
        if count == 0:
            log.info("首次启动: 正在拉取源站列表...")
            new_slugs = syncer.refresh_sources()
            log.info("首次启动: 登记 %d 个源站", len(new_slugs))
            if config.FULL_SYNC_ON_DISCOVER:
                # 后台并行全量同步，不阻塞服务启动
                threading.Thread(target=syncer.sync_all, daemon=True).start()
        else:
            # 已同步过的库：启动时校准本地条数（同步中断时 total_videos 可能滞后）
            syncer.refresh_counts()
        # 无论首次与否，启动定时调度
        from .scheduler import start_scheduler

        start_scheduler()
    except Exception:
        log.exception("启动引导失败（服务仍可用，可稍后通过 API 手动触发）")


@app.on_event("shutdown")
def on_shutdown():
    from .scheduler import stop_scheduler

    stop_scheduler()
