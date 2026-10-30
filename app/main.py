"""
应用入口：FastAPI 应用 + 启动初始化（建库、首次拉取源站列表、启动调度器）。

启动参数（见 Dockerfile CMD）:
  uvicorn app.main:app --host 0.0.0.0 --port 8000
"""
import logging
import os
import threading

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

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
    version="1.2.2",
)

app.include_router(router)

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
    """管理面板（单页应用，无需构建）。API Key 由页面内交互式输入。"""
    return FileResponse(os.path.join(WEB_DIR, "index.html"), media_type="text/html")


@app.on_event("startup")
def on_startup():
    database.init_db()
    log.info("数据库已初始化: %s", config.DATABASE_URL)

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
