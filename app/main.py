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
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from sqlalchemy.exc import OperationalError, TimeoutError as SADBTimeoutError
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import __version__, apiresp, config, database
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
    version=__version__,
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
        if not auth.request_authorized(request):
            # v1.14.0 / B-11：401 同时给出 code=40101 与 error=invalid_token，
            # detail 保留中文（老客户端与面板读的就是它）
            return JSONResponse(
                status_code=401,
                content=apiresp.error_body(
                    401,
                    "未授权：请求头需携带 Authorization: Bearer <token>"
                    "（也支持 X-API-Key 头或 ?token= 参数）；浏览器需先登录管理面板",
                    error="invalid_token",
                ),
                headers={"WWW-Authenticate": "Bearer"},
            )
        # v1.14.0 / B-08：权限粒度。默认 TOKEN_SCOPE_ENFORCE=false —— 全部放行，
        # 等价「所有 Token 都是 admin」，保证升级当天现网零影响。
        if config.TOKEN_SCOPE_ENFORCE and auth.requires_admin(request.method, path):
            scope = auth.request_scope(request)
            if scope != "admin" and not auth.legacy_client_exempt(request):
                log.info("权限不足: %s %s scope=%s", request.method, path, scope or "-")
                return JSONResponse(
                    status_code=403,
                    content=apiresp.error_body(
                        403,
                        "该接口需要 admin 权限的 Token（当前 Token 为只读）",
                        error="insufficient_scope",
                        required="admin",
                    ),
                )
        return await call_next(request)

    if auth.has_session(request):
        return await call_next(request)

    if request.method in ("GET", "HEAD") and "text/html" in request.headers.get("accept", ""):
        target = path + (("?" + request.url.query) if request.url.query else "")
        return RedirectResponse(f"/login?next={quote(target, safe='')}", status_code=303)
    return JSONResponse(status_code=401, content=apiresp.error_body(401, "请先登录管理面板"))


# ---------------------------------------------------------------------------
# 统一错误包络（v1.14.0 / B-11）
#
# 目标：客户端能可靠区分「成功 / 业务失败 / 鉴权失败」，而不是靠中文字符串猜。
# 关键取舍：
#   - 业务失败一律非 2xx（不再出现 HTTP 200 + code:1 表示失败）；
#   - body 同时给 code（程序判据）、error（稳定语义串）、detail（中文，老客户端
#     与面板一直读它，删了就白屏），另加 message 同值供新客户端使用；
#   - 500 不回显堆栈/异常原文，只给一句可读提示 + 服务端日志。
#
# 这三个 handler 只影响**异常路径**，正常响应的字段一个都没动。
# ---------------------------------------------------------------------------
@app.exception_handler(StarletteHTTPException)
async def _http_error_handler(request: Request, exc: StarletteHTTPException):
    status = exc.status_code or 500
    detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail or "")
    body = apiresp.error_body(status, detail)
    # 429 等场景 HTTPException 自带 headers（Retry-After），必须原样带出去，
    # 否则客户端就不知道要等多久
    headers = getattr(exc, "headers", None) or None
    return JSONResponse(status_code=status, content=body, headers=headers)


@app.exception_handler(RequestValidationError)
async def _validation_error_handler(request: Request, exc: RequestValidationError):
    """
    参数校验失败：FasAPI 默认返回 422 + 结构复杂的 detail 数组，
    客户端拿不到「哪个字段错了」。统一成 400 + 40001 + field。
    """
    field = ""
    try:
        errs = exc.errors()
        if errs:
            loc = [str(x) for x in errs[0].get("loc", []) if x not in ("query", "path", "body")]
            field = ".".join(loc)
            msg = errs[0].get("msg", "")
        else:
            msg = ""
    except Exception:  # noqa: BLE001
        msg = ""
    text = f"参数错误{f'：{field} {msg}' if field else ''}".strip()
    return JSONResponse(
        status_code=400,
        content=apiresp.error_body(400, text, field=field or None),
    )


@app.exception_handler(Exception)
async def _unhandled_error_handler(request: Request, exc: Exception):
    """未捕获异常：日志留全量堆栈，响应只给一句人话（不回显内部细节）。"""
    log.exception("未处理异常: %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content=apiresp.error_body(500, "服务内部错误，请稍后重试（详情见服务端日志）"),
    )



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
        # 注意 code 必须是失败码：v1.13 这里写的是 code:0，客户端按「code==0 即成功」
        # 会把它当成功响应去解析 data，直接空列表 —— 属于 B-11 要消灭的形态。
        content=apiresp.error_body(
            503,
            "服务繁忙：数据库连接池已满，请稍后重试（可减少同时运行的全量同步数量）",
            error="service_unavailable",
            retry_after=5,
        ),
        headers={"Retry-After": "5"},
    )


@app.exception_handler(OperationalError)
def _db_operational_handler(request: Request, exc: OperationalError):  # noqa: ANN001
    log.error("数据库操作失败: %s %s", request.url.path, exc)
    return JSONResponse(
        status_code=503,
        content=apiresp.error_body(
            503, "数据库繁忙或暂不可用，请稍后重试",
            error="service_unavailable", retry_after=5,
        ),
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
    # 注意：不要在这里打印 config.DATABASE_URL —— 它带着 PG 密码。
    # init_db() 内部用 render_as_string(hide_password=True) 打了脱敏版本。
    database.init_db()

    from . import auth

    auth.ensure_init()   # 初始化面板密码 / Token / 鉴权开关（首次写默认值）

    from . import pause

    pause.load_state()   # 恢复持久化的同步暂停状态（重启不丢）

    from . import syncer

    # 断电 / 崩溃 / docker restart 后，上一次留下的 running 日志永远不会自己结束，
    # 面板会一直显示「进行中」。启动时统一改成 interrupted。
    try:
        syncer.mark_interrupted_logs()
    except Exception:
        log.exception("标记中断任务失败（不影响启动）")

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
            # 已同步过的库：启动时只校准「条数为 0」的源站（v1.10.0 / P9）。
            # 全量 count(*) 52 张表要几十秒，会拖住调度器启动；精确值交给夜间校准。
            syncer.refresh_counts(only_zero=True)
        # 无论首次与否，启动定时调度
        from .scheduler import start_scheduler

        # 先跑一次数据维护：清掉积压的同步日志、顺手回收 WAL。
        # 放在调度器启动前，避免和第一次定时触发撞车。
        if config.HOUSEKEEPING_ENABLED:
            try:
                from . import housekeeping

                housekeeping.run_all()
            except Exception:
                log.exception("启动时数据维护失败（不影响服务）")

        start_scheduler()

        # 相关推荐存量补齐（v1.9.0）：对覆盖率不满的源站逐站后台计算。
        # 标题是唯一全量信号且权重自动重分配，所以**当天即可用**；
        # 演员/地区/年代字段由夜间回填任务分批补上，推荐质量随之提升。
        if config.RELATED_ENABLED:
            try:
                from . import related

                related.startup_backfill()
            except Exception:
                log.exception("相关推荐启动补齐检查失败（不影响服务）")

        # 续跑上次没做完的全量同步（有断点的源站）。
        # 放在最后：先让服务能正常响应，再由这个线程按并发闸门慢慢跑。
        try:
            syncer.auto_resume_incomplete()
        except Exception:
            log.exception("自动续跑检查失败（不影响服务）")

        # 搜索索引（v1.10.0 / P8）：pg_trgm GIN，让 name LIKE '%kw%' 能走索引。
        # 后台线程 + CREATE INDEX CONCURRENTLY（不锁表），首次建完后续自动跳过。
        if config.PG_TRGM_ENABLED:
            threading.Thread(
                target=_ensure_search_indexes_bg, daemon=True, name="pg-trgm"
            ).start()

        # 标签倒排索引（v1.14.0 / B-01）：没建完的后台补齐。
        # 可断点续跑（游标在 tag_index_state），建完的源站不会重扫；
        # 单独线程 + 串行写 + 批间休眠，绝不和同步任务抢 IO。
        # 建完之前 /healthz 不声明 tag_search，客户端继续用 wd 检索。
        if config.TAG_SEARCH_ENABLED and config.TAG_INDEX_ON_STARTUP:
            try:
                from . import tags

                if not tags.ready():
                    log.info("标签索引未就绪，后台开始回填（可断点续跑）")
                    tags.start_backfill()
            except Exception:
                log.exception("标签索引回填启动失败（检索能力暂不声明）")
    except Exception:
        log.exception("启动引导失败（服务仍可用，可稍后通过 API 手动触发）")


def _ensure_search_indexes_bg():
    """后台建 pg_trgm 搜索索引（失败只记日志，不影响任何功能）。"""
    try:
        database.ensure_search_indexes()
    except Exception:
        log.exception("创建搜索索引失败（搜索退化为顺序扫描，功能不受影响）")


@app.on_event("shutdown")
def on_shutdown():
    from .scheduler import stop_scheduler

    stop_scheduler()
