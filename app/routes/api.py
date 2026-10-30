"""
对外 API：
1. REST 风格：/api/v1/videos?source={slug} 列表、/api/v1/videos/{slug}/{vod_id} 详情
   —— 通过 source 参数区分源站，支持逗号分隔多源合并
2. 苹果 CMS 兼容风格：/api/v1/{slug}?ac=list|detail&pg=1&ids=1,2
   —— 输出结构与 MacCMS 采集接口一致，方便现有播放器/采集器直接对接
3. 管理接口：源站列表、手动触发同步、查看同步日志
"""
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, select, text

from .. import config, database
from ..models import build_vod_table, sources, sync_logs
from ..syncer import ensure_table, refresh_sources, sync_all, sync_source
from ..utils import check_api_key, page_params, parse_play_url, validate_slug

log = logging.getLogger("vodsync.api")
router = APIRouter(dependencies=[Depends(check_api_key)])


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


def _query_vods(
    conn,
    table,
    page: int,
    page_size: int,
    type_id: int = None,
    wd: str = None,
    detail: bool = False,
):
    """通用分页查询：按更新时间倒序。返回 (total, pagecount, items)。"""
    where = []
    params: dict = {}
    if type_id:
        where.append(table.c.type_id == type_id)
    if wd:
        where.append(table.c.name.like(f"%{wd}%"))
        params["wd"] = f"%{wd}%"

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
        item = {
            "vod_id": r["vod_id"],
            "type_id": r["type_id"],
            "type_name": r["type_name"],
            "vod_name": r["name"],
            "vod_pic": r["pic"],
            "vod_remarks": r["remarks"],
            "vod_time": r["vod_time"],
        }
        if detail:
            item["vod_content"] = r["content"]
            item["vod_play_url"] = r["play_url"]
            item["vod_play_from"] = "vodsync"
            item["vod_play_list"] = parse_play_url(r["play_url"])  # 额外提供的结构化播放列表
        items.append(item)
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


def _detail_item(source: str, row) -> dict:
    """详情条目：原始播放地址 + 结构化播放列表（列表接口与详情接口共用）。"""
    return {
        "source": source,
        "vod_id": row["vod_id"],
        "type_id": row["type_id"],
        "type_name": row["type_name"],
        "vod_name": row["name"],
        "vod_pic": row["pic"],
        "vod_remarks": row["remarks"],
        "vod_time": row["vod_time"],
        "vod_content": row["content"],
        "vod_play_url": row["play_url"],
        "vod_play_from": "vodsync",
        "vod_play_list": parse_play_url(row["play_url"]),
    }


# ---------------------------------------------------------------------------
# 分类接口：供客户端做「分类浏览」入口
# ---------------------------------------------------------------------------
@router.get("/api/v1/categories")
def categories(
    source: str = Query("", description="源站 slug，逗号分隔多源；留空=全部源站合并"),
):
    """
    分类清单（type_id / type_name 去重 + 各分类条数）。

    用法：
      /api/v1/categories?source=jkun          单源站分类
      /api/v1/categories?source=jkun,t1       多源合并（同名分类会合并计数）
      /api/v1/categories                      全部源站合并
    拿到 type_id 后即可用 GET /api/v1/videos?source=..&type_id=.. 按分类翻页。
    """
    from collections import defaultdict

    slugs = [s.strip() for s in source.split(",") if s.strip()] if source else None
    if slugs:
        for s in slugs:
            validate_slug(s)
    else:
        with database.engine.connect() as conn:
            slugs = [r[0] for r in conn.execute(
                select(sources.c.slug).where(sources.c.enabled == 1)
            ).all()]

    merged: dict = defaultdict(lambda: {"type_id": None, "type_name": "", "count": 0, "sources": []})
    with database.engine.connect() as conn:
        for slug in slugs:
            table = build_vod_table(slug)
            if not database.table_exists(table.name):
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
    return {"code": 1, "total": len(items), "list": items}


# ---------------------------------------------------------------------------
# 管理接口
# ---------------------------------------------------------------------------
@router.get("/healthz")
def healthz():
    with database.engine.connect() as conn:
        conn.execute(text("SELECT 1"))
    return {"status": "ok", "time": datetime.now().isoformat()}


@router.get("/api/v1/sources")
def list_sources():
    """
    所有源站及本地同步状态。

    字段口径（易混淆，特意分开）：
      running       此刻是否有同步任务在跑（只有 true 才应该提供「暂停」操作）
      running_mode  正在跑的类型：full / incremental
      running_seconds 本次已运行秒数
      syncing       全量同步是否「未跑完」（有断点），不代表正在跑，仅用于展示进度条
      paused        该源站是否被单独暂停（持久化）
    """
    from .. import syncer

    running = syncer.running_snapshot()

    def _progress(row, live) -> float:
        """全量进度百分比：有断点按断点算；已完成=100；刚开始还没拿到总页数=0。"""
        if row["last_page"] and row["total_pages"]:
            return round(row["last_page"] * 100 / row["total_pages"], 1)
        if row["last_page"]:
            return 0.0
        if live and live.get("mode") == "full":
            return 0.0          # 正在跑全量但还没到第一个检查点
        return 100.0            # 没有断点且不在跑 → 视为已完成

    with database.engine.connect() as conn:
        rows = conn.execute(
            select(
                sources.c.slug, sources.c.name, sources.c.api_url, sources.c.status,
                sources.c.enabled, sources.c.total_videos, sources.c.watermark,
                sources.c.last_page, sources.c.total_pages, sources.c.last_sync_at,
                sources.c.paused, sources.c.paused_at,
            ).order_by(sources.c.id)
        ).mappings().all()
    return {
        "code": 1,
        "list": [
            {
                "slug": r["slug"],
                "name": r["name"],
                "api_url": r["api_url"],
                "status": r["status"],
                "enabled": bool(r["enabled"]),
                "total_videos": r["total_videos"],
                "watermark": r["watermark"],
                "syncing": r["last_page"] > 0,   # 有断点说明全量未完成（不代表正在跑）
                # 真实运行态：此刻是否有任务在跑（决定是否显示「暂停」按钮）
                "running": r["slug"] in running,
                "running_mode": running.get(r["slug"], {}).get("mode"),
                "running_seconds": running.get(r["slug"], {}).get("elapsed_seconds"),
                # 全量同步进度（0-100）；已完成 100，刚开始跑未拿到总页数为 0
                "progress": _progress(r, running.get(r["slug"])),
                "last_page": r["last_page"],
                "total_pages": r["total_pages"],
                "last_sync_at": r["last_sync_at"].isoformat() if r["last_sync_at"] else None,
                # 单源站暂停（该源站独立于全局暂停，两者任一命中即不参与同步）
                "paused": bool(r["paused"]),
                "paused_at": r["paused_at"].isoformat() if r["paused_at"] else None,
            }
            for r in rows
        ],
    }


@router.get("/source")
def source_catalog(
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
    """
    with database.engine.connect() as conn:
        rows = conn.execute(
            select(
                sources.c.name, sources.c.slug, sources.c.api_url, sources.c.homepage,
                sources.c.description, sources.c.status, sources.c.enabled,
                sources.c.total_videos, sources.c.watermark, sources.c.last_sync_at,
            )
            .where(sources.c.enabled == 1)
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
        })

    return {
        "code": 1,
        "total": len(items),
        "alive": sum(1 for x in items if x["status"] == "ok"),
        "ready": sum(1 for x in items if x["video_count"] > 0),
        "server_time": datetime.now().isoformat(),
        "list": items,
    }


@router.get("/api/v1/stats")
def stats():
    """管理面板概览数据：源站数、总条数、同步中数量、今日入库量、暂停状态、最近错误。"""
    from sqlalchemy import and_

    from .. import pause, syncer

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
        "code": 1,
        "sources_total": total_sources,
        "sources_enabled": enabled_sources,
        "sources_syncing": len(syncer.running_snapshot()),   # 此刻真正在跑的任务数
        "sources_unfinished": unifinished,                   # 全量未跑完（有断点）的站数
        "videos_total": int(total_videos),
        "today_inserted": int(today_rows[0]),
        "today_updated": int(today_rows[1]),
        "today_tasks": int(today_rows[2]),
        "pause": pause.snapshot(),   # 同步暂停状态（面板顶栏开关用）
        "last_error": (
            {"slug": last_error[0], "error": last_error[1],
             "at": last_error[2].isoformat() if last_error[2] else None}
            if last_error else None
        ),
        "server_time": datetime.now().isoformat(),
    }


@router.get("/api/v1/sources/{slug}")
def source_detail(slug: str):
    from .. import syncer

    src, _ = _require_table(slug)
    live = syncer.running_snapshot().get(slug)
    with database.engine.connect() as conn:
        logs = conn.execute(
            select(sync_logs).where(sync_logs.c.slug == slug)
            .order_by(sync_logs.c.id.desc()).limit(10)
        ).mappings().all()
    return {
        "code": 1,
        "source": {
            "slug": src["slug"], "name": src["name"], "api_url": src["api_url"],
            "status": src["status"], "total_videos": src["total_videos"],
            "watermark": src["watermark"], "last_page": src["last_page"],
            "running": live is not None,
            "running_mode": (live or {}).get("mode"),
            "running_seconds": (live or {}).get("elapsed_seconds"),
            "paused": bool(src["paused"]),
            "paused_at": src["paused_at"].isoformat() if src["paused_at"] else None,
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
        "code": 1,
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
        "code": 1,
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
    return {"code": 1, "new_sources": new_slugs, "full_sync_started": full_sync}


def _bg_sync_new(slugs: list):
    for slug in slugs:
        try:
            sync_source(slug, mode="full")
        except Exception:
            log.exception("新源站 %s 全量同步失败", slug)


@router.post("/api/v1/sync/{slug}")
def trigger_sync(slug: str, mode: str = Query(default="auto", pattern="^(auto|full|incremental)$")):
    """手动触发单源站同步（后台执行，立即返回任务受理结果）。"""
    _require_table(slug) if slug != "*" else None
    threading.Thread(target=sync_source, args=(validate_slug(slug), mode), daemon=True).start()
    return {"code": 1, "msg": f"同步任务已受理: {slug} mode={mode}"}


@router.post("/api/v1/sync-all")
def trigger_sync_all(mode: str = Query(default="auto", pattern="^(auto|full|incremental)$")):
    """手动触发全部源站同步（后台执行）。"""
    threading.Thread(target=sync_all, args=(mode,), daemon=True).start()
    return {"code": 1, "msg": f"全量同步任务已受理: mode={mode}"}


@router.post("/api/v1/maintenance/recount")
def recount():
    """重算各源站本地条数（同步中断后校准面板数据）。"""
    from ..syncer import refresh_counts

    threading.Thread(target=refresh_counts, daemon=True).start()
    return {"code": 1, "msg": "条数校准任务已受理"}


@router.get("/api/v1/pause")
def pause_status():
    """同步暂停状态（三层级：global / full / incremental）。"""
    from .. import pause

    return {"code": 1, **pause.snapshot()}


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
        "code": 1,
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
        "code": 1,
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
        "code": 1,
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
# REST 风格数据接口（source 参数区分源站）
# ---------------------------------------------------------------------------
@router.get("/api/v1/videos")
def video_list(
    request: Request,
    source: str = Query("", description="源站 slug，支持逗号分隔多源；留空查询全部源站"),
    type_name: str = Query("", description="按分类名称过滤"),
    type_id: int = Query(0, ge=0, description="按分类 ID 过滤（配合 /api/v1/categories 使用）"),
    wd: str = Query("", description="按名称模糊搜索"),
    ac: str = Query("", description="兼容 MacCMS：ac=detail 时列表返回含播放地址的完整字段"),
    ids: str = Query("", description="按 ID 批量取详情（逗号分隔），返回含播放地址"),
    paging: tuple = Depends(page_params),
):
    """
    视频列表 / 批量详情。

    四种用法：
      1. 列表：      /api/v1/videos?source=jkun&page=1&page_size=20
      2. 批量详情：  /api/v1/videos?source=jkun&ids=134369       （含 vod_play_url）
      3. 完整列表：  /api/v1/videos?source=jkun&ac=detail         （列表带播放地址）
      4. 分类浏览：  /api/v1/videos?source=jkun&type_id=6         （配合 /api/v1/categories）
    """
    page, page_size = paging
    # 参数归一化：兼容 `&?ac=detail` 这类多打问号的写法
    source = _qp(request, "source", source)
    ac = _qp(request, "ac", ac).lower()
    ids = _qp(request, "ids", ids)
    type_name = _qp(request, "type_name", type_name)
    wd = _qp(request, "wd", wd)
    detail_mode = ac in ("detail", "videolist")

    slugs = [s.strip() for s in source.split(",") if s.strip()] if source else None

    if slugs:
        for s in slugs:
            validate_slug(s)
    else:
        with database.engine.connect() as conn:
            slugs = [r[0] for r in conn.execute(
                select(sources.c.slug).where(sources.c.enabled == 1)
            ).all()]
        if not slugs:
            raise HTTPException(status_code=404, detail="暂无源站，请先触发 /api/v1/sources/refresh")

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
        with database.engine.connect() as conn:
            for slug in slugs:
                table = build_vod_table(slug)
                if not database.table_exists(table.name):
                    continue
                found = {
                    r["vod_id"]: r
                    for r in conn.execute(
                        select(table).where(table.c.vod_id.in_(want))
                    ).mappings().all()
                }
                for vid in want:  # 按请求中的 id 顺序返回
                    if vid in found:
                        items.append(_detail_item(slug, found[vid]))
        if not items:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"未找到 ids={ids} 的视频（源站: {','.join(slugs)}）。"
                    "该条可能尚未同步入库，可先用 /api/v1/search?wd=关键词 或 "
                    "/api/v1/videos?source=xxx&wd=关键词 搜索确认 vod_id"
                ),
            )
        return {"code": 1, "page": 1, "pagecount": 1, "total": len(items), "list": items}

    # ---- 用法 1 / 3：列表（多源合并：UNION ALL 子查询 + 外层排序分页） ----
    from sqlalchemy import literal, literal_column, union_all

    union_parts = []
    params: dict = {}
    for i, slug in enumerate(slugs):
        table = build_vod_table(slug)
        if not database.table_exists(table.name):
            continue
        where = []
        if wd:
            where.append(table.c.name.like(f"%{wd}%"))
            params[f"wd_{i}"] = f"%{wd}%"
        if type_name:
            where.append(table.c.type_name == type_name)
            params[f"tn_{i}"] = type_name
        if type_id:
            where.append(table.c.type_id == type_id)
            params[f"ti_{i}"] = type_id
        cols = [
            table.c.vod_id,
            table.c.type_id,
            table.c.type_name,
            table.c.name,
            table.c.pic,
            table.c.remarks,
            table.c.vod_time,
            literal(slug).label("source_slug"),
        ]
        if detail_mode:
            cols += [table.c.content, table.c.play_url]
        union_parts.append(select(*cols).where(*where))

    if not union_parts:
        raise HTTPException(status_code=404, detail="指定源站均无数据")

    merged = union_all(*union_parts).subquery()
    total = database.engine.connect().execute(
        select(func.count()).select_from(merged), params
    ).scalar() or 0
    pagecount = (total + page_size - 1) // page_size
    rows = database.engine.connect().execute(
        select(merged)
        .order_by(literal_column("vod_time").desc(), literal_column("vod_id").desc())
        .offset((page - 1) * page_size)
        .limit(page_size),
        params,
    ).mappings().all()

    return {
        "code": 1,
        "page": page,
        "pagecount": pagecount,
        "total": total,
        "list": (
            [_detail_item(r["source_slug"], r) for r in rows]
            if detail_mode
            else [
                {
                    "source": r["source_slug"],
                    "vod_id": r["vod_id"],
                    "type_id": r["type_id"],
                    "type_name": r["type_name"],
                    "vod_name": r["name"],
                    "vod_pic": r["pic"],
                    "vod_remarks": r["remarks"],
                    "vod_time": r["vod_time"],
                }
                for r in rows
            ]
        ),
    }


@router.get("/api/v1/videos/{slug}/{vod_id}")
def video_detail(slug: str, vod_id: int):
    """视频详情（含结构化播放地址列表）。"""
    _src, table = _require_table(slug)
    with database.engine.connect() as conn:
        row = conn.execute(
            select(table).where(table.c.vod_id == vod_id)
        ).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="视频不存在")
    return {"code": 1, "detail": _detail_item(slug, row)}


# ---------------------------------------------------------------------------
# 聚合搜索：跨全部源站按名称搜索（并行查询各源站表，内存合并分页）
# ---------------------------------------------------------------------------
@router.get("/api/v1/search")
def aggregate_search(
    wd: str = Query(..., min_length=1, max_length=64, description="影视名称关键词（必填）"),
    source: str = Query("", description="限定源站 slug，逗号分隔；留空=搜索全部源站"),
    type_name: str = Query("", description="按分类名称过滤"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    per_source_limit: int = Query(
        200, ge=10, le=1000,
        description="每个源站最多返回多少条候选（控制单表单次扫描开销）",
    ),
):
    """
    跨源站聚合搜索。

    实现说明：
    - 由于名称模糊匹配无法走索引，采用「每源站各查一次 + 并行执行 + 内存归并」策略，
      避免一次性 UNION 全部表导致单条 SQL 扫描上千万行而超时；
    - 单个源站查询失败/超时不影响整体结果（返回 partial 标记与失败源站列表）；
    - 结果按 vod_time 倒序（新更新在前），可按 source 限定范围提升速度。
    """
    started = time.time()
    kw = wd.strip()
    if not kw:
        raise HTTPException(status_code=400, detail="搜索关键词不能为空")

    # 1) 确定待搜索的源站
    with database.engine.connect() as conn:
        if source:
            slugs = [s.strip() for s in source.split(",") if s.strip()]
            for s in slugs:
                validate_slug(s)
            name_map = {
                r[0]: r[1] for r in conn.execute(
                    select(sources.c.slug, sources.c.name).where(sources.c.slug.in_(slugs))
                ).all()
            }
            slugs = [s for s in slugs if s in name_map]
        else:
            rows = conn.execute(
                select(sources.c.slug, sources.c.name)
                .where(sources.c.enabled == 1)
                .order_by(sources.c.id)
            ).all()
            name_map = {r[0]: r[1] for r in rows}
            slugs = list(name_map.keys())

    if not slugs:
        raise HTTPException(status_code=404, detail="没有可搜索的源站，请先同步数据")

    # 转义 LIKE 通配符，避免用户输入 % _ 造成全表匹配
    esc = kw.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    pattern = f"%{esc}%"

    def search_one(slug: str) -> tuple:
        """查询单个源站表，返回 (slug, rows, truncated, error)。"""
        try:
            table = build_vod_table(slug)
            if not database.table_exists(table.name):
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

    # 2) 并行查询（SQLite WAL 支持并发读；线程数受控避免 NAS IO 打满）
    collected: list = []
    failed: list = []
    truncated_sources: list = []
    workers = min(8, len(slugs))
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
                collected.append({
                    "source": slug,
                    "source_name": name_map.get(slug, slug),
                    "vod_id": r["vod_id"],
                    "type_id": r["type_id"],
                    "type_name": r["type_name"],
                    "vod_name": r["name"],
                    "vod_pic": r["pic"],
                    "vod_remarks": r["remarks"],
                    "vod_time": r["vod_time"],
                })

    # 3) 归并排序 + 分页（按更新时间倒序，同时间按 id 倒序）
    collected.sort(key=lambda x: (x["vod_time"] or "", x["vod_id"]), reverse=True)
    total_found = len(collected)
    pagecount = (total_found + page_size - 1) // page_size if total_found else 0
    start = (page - 1) * page_size
    items = collected[start : start + page_size]

    return {
        "code": 1,
        "wd": kw,
        "page": page,
        "page_size": page_size,
        "pagecount": pagecount,
        # 注意：total 为「各源站候选集上限内的命中数」，非全库精确总数（模糊搜索不做全量计数以保证响应速度）
        "total": total_found,
        "total_is_partial": bool(truncated_sources),
        "truncated_sources": truncated_sources,
        "sources_searched": len(slugs) - len(failed),
        "sources_failed": failed,
        "elapsed_ms": int((time.time() - started) * 1000),
        "list": items,
    }


# ---------------------------------------------------------------------------
# 苹果 CMS 兼容接口：/api/v1/{slug}?ac=list|detail
# ---------------------------------------------------------------------------
@router.get("/api/v1/{slug}")
def maccms_compat(
    slug: str,
    ac: str = Query(default="detail", pattern="^(list|detail)$"),
    pg: int = Query(default=1, ge=1),
    ids: str = Query(default="", description="按 ID 批量取详情，逗号分隔"),
    wd: str = Query(default=""),
    paging: tuple = Depends(page_params),
):
    """
    与苹果 CMS 采集接口输出结构一致：
      ?ac=list   -> {code,page,pagecount,limit,total,class,list:[轻量字段]}
      ?ac=detail -> {code,page,pagecount,limit,total,class,list:[全字段含播放地址]}
    """
    src, table = _require_table(slug)
    page, page_size = paging
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
                return {"code": 1, "page": 1, "pagecount": 1, "limit": page_size, "total": 0, "class": class_list, "list": []}
            rows = conn.execute(
                select(table.c.vod_id, table.c.type_id, table.c.type_name, table.c.name,
                       table.c.pic, table.c.remarks, table.c.vod_time,
                       table.c.content, table.c.play_url)
                .where(table.c.vod_id.in_(id_list))
            ).mappings().all()
            return {
                "code": 1, "page": 1, "pagecount": 1, "limit": page_size,
                "total": len(rows), "class": class_list,
                "list": [
                    {
                        "vod_id": r["vod_id"], "type_id": r["type_id"], "type_name": r["type_name"],
                        "vod_name": r["name"], "vod_pic": r["pic"], "vod_remarks": r["remarks"],
                        "vod_time": r["vod_time"], "vod_content": r["content"],
                        "vod_play_url": r["play_url"], "vod_play_from": "vodsync",
                        "vod_play_list": parse_play_url(r["play_url"]),
                    }
                    for r in rows
                ],
            }

        detail = ac == "detail"
        total, pagecount, items = _query_vods(
            conn, table, page, page_size, wd=wd or None, detail=detail
        )
    return {
        "code": 1,
        "msg": "数据列表",
        "page": page,
        "pagecount": pagecount,
        "limit": page_size,
        "total": total,
        "class": class_list,
        "list": items,
    }
