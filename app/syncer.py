"""
同步引擎：源站发现、全量同步（断点续传）、增量同步（水位线）。

设计要点：
1. 全量：ac=detail 翻页，多线程抓取，按页批量 UPSERT，每 N 页保存断点到 sources.last_page
2. 增量：ac=list 只取 vod_id/vod_time（轻量），只对 vod_time > 水位线 或 本库缺失的
   ID 拉取 ac=detail&ids=...，百万级存量数据零重复传输
3. 写入：单条 INSERT ... ON CONFLICT(vod_id) DO UPDATE 批量执行（SQLite/PG 通用）
"""
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from sqlalchemy import bindparam, func, select, text

from . import config, database, pause
from .maccms import MacCmsClient, MacCmsError
from .models import build_vod_table, slugify, sources, sync_logs

log = logging.getLogger("vodsync.syncer")


# ---------------------------------------------------------------------------
# 运行时状态：哪些源站「此刻」正在执行同步（进程内内存，重启即清空）
#
# 注意与 sources 表里的字段区分：
#   last_page > 0   表示全量同步「未跑完」（有断点），不代表正在跑
#   _running[slug]  表示此刻真的有一个同步任务在跑
# 只有后者才允许提供「暂停」操作。
# ---------------------------------------------------------------------------
_running: dict[str, dict] = {}
_running_lock = threading.Lock()

# ---------------------------------------------------------------------------
# 全局并发闸门（v1.4.1 修复连接池耗尽的关键）
#
# 为什么需要：面板上连点十几个「全量」时，每次请求各起一个线程，没有任何上限。
# SQLite 只允许一个写者，十几个写线程会互相等锁，每个等锁线程占着一个连接池连接，
# 池满后连 /healthz、面板刷新都 30s 超时 → 表现为整个服务"挂了"。
# 这里用一个信号量把「同时真正在跑的源站同步」限制在 SOURCE_SYNC_CONCURRENCY 个，
# 其余排队等待（排队不占数据库连接）。
# ---------------------------------------------------------------------------
_sync_slots: threading.BoundedSemaphore = None
_sync_slots_lock = threading.Lock()

# 已排队或正在跑的源站：同一个源站不允许被并发触发两次（否则同表双写）
_pending: set[str] = set()


def _slots() -> threading.BoundedSemaphore:
    """惰性创建并发闸门（读取时用最新配置，便于测试与环境变量覆盖）。"""
    global _sync_slots
    with _sync_slots_lock:
        if _sync_slots is None:
            _sync_slots = threading.BoundedSemaphore(max(1, config.SOURCE_SYNC_CONCURRENCY))
        return _sync_slots


def reset_sync_slots(size: int = None):
    """重建并发闸门（测试 / 运行时调整并发时使用）。"""
    global _sync_slots
    with _sync_slots_lock:
        _sync_slots = threading.BoundedSemaphore(max(1, size or config.SOURCE_SYNC_CONCURRENCY))
    return _sync_slots


def sync_load() -> dict:
    """当前同步负载：正在跑 / 排队中。供面板与 /stats 展示。"""
    with _running_lock:
        running = len(_running)
        queued = max(0, len(_pending) - running)
    return {"running": running, "queued": queued, "limit": max(1, config.SOURCE_SYNC_CONCURRENCY)}


def queued_slugs() -> set:
    """已排队但还没轮到执行的源站（并发闸门外的等待者）。"""
    with _running_lock:
        return set(_pending) - set(_running)


def is_busy(slug: str) -> bool:
    """该源站是否已在排队或运行中（用于避免重复提交）。"""
    with _running_lock:
        return slug in _pending



def mark_running(slug: str, mode: str):
    """登记某源站开始同步。"""
    with _running_lock:
        _running[slug] = {"mode": mode, "started_at": time.time()}


def mark_stopped(slug: str):
    """登记某源站同步结束（成功/失败/暂停均调用）。"""
    with _running_lock:
        _running.pop(slug, None)


def is_running(slug: str) -> bool:
    with _running_lock:
        return slug in _running


def running_snapshot() -> dict:
    """当前正在同步的源站快照：{slug: {"mode", "started_at", "elapsed_seconds"}}。"""
    now = time.time()
    with _running_lock:
        return {
            slug: {
                "mode": info["mode"],
                "started_at": info["started_at"],
                "elapsed_seconds": round(now - info["started_at"], 1),
            }
            for slug, info in _running.items()
        }


# ---------------------------------------------------------------------------
# 源站发现：拉取 latest.json，登记新源站并建表
# ---------------------------------------------------------------------------
def refresh_sources(client: MacCmsClient = None) -> list:
    """
    拉取 latest.json，把新的源站登记进 sources 表并创建数据表。
    返回新增源站的 slug 列表。已存在的源站只更新状态/描述等元信息。
    """
    own_client = client is None
    client = client or MacCmsClient()
    try:
        data = client._get_json(config.LATEST_JSON_URL)
    finally:
        if own_client:
            client.close()

    resources = data.get("resources") or []
    new_slugs = []
    with database.write_txn() as conn:
        for res in resources:
            api_url = (res.get("api") or "").strip()
            name = (res.get("name") or "").strip()
            if not api_url or not name:
                continue
            if config.ONLY_OK_SOURCES and res.get("status") != "ok":
                continue
            slug = slugify(name)
            exists = conn.execute(
                select(sources.c.id).where(sources.c.slug == slug)
            ).first()
            if exists:
                conn.execute(
                    sources.update()
                    .where(sources.c.slug == slug)
                    .values(status=res.get("status") or "ok", description=(res.get("description") or "")[:500])
                )
                continue
            conn.execute(
                sources.insert().values(
                    name=name[:120],
                    slug=slug,
                    api_url=api_url[:500],
                    homepage=(res.get("link") or "")[:500],
                    description=(res.get("description") or "")[:500],
                    status=res.get("status") or "ok",
                    enabled=1,
                    auto_sync=1,
                )
            )
            new_slugs.append(slug)

    for slug in new_slugs:
        ensure_table(slug)

    if new_slugs:
        log.info("发现 %d 个新源站: %s", len(new_slugs), ",".join(new_slugs))
    return new_slugs


def ensure_table(slug: str):
    """创建 vod_{slug} 表（幂等）。"""
    table = build_vod_table(slug)
    table.create(database.engine, checkfirst=True)


def get_source(conn, slug: str):
    return conn.execute(select(sources).where(sources.c.slug == slug)).mappings().first()


# ---------------------------------------------------------------------------
# 单源站暂停（与全局/全量/增量三层级暂停相互独立，任一命中即停止）
# ---------------------------------------------------------------------------
def is_source_paused(slug: str) -> bool:
    with database.engine.connect() as conn:
        return bool(
            conn.execute(select(sources.c.paused).where(sources.c.slug == slug)).scalar() or 0
        )


def set_source_paused(slug: str, paused: bool):
    """设置单源站暂停标记（持久化在 sources 表，重启后仍生效）。"""
    with database.write_txn() as conn:
        conn.execute(
            sources.update()
            .where(sources.c.slug == slug)
            .values(paused=1 if paused else 0, paused_at=datetime.now() if paused else None)
        )
    log.info("[%s] 单源站暂停标记 = %s", slug, "暂停" if paused else "继续")


def should_stop(slug: str, mode: str) -> bool:
    """某源站在某模式下是否应停止：全局/分层暂停 或 该源站被单独暂停。"""
    return pause.is_paused(mode) or is_source_paused(slug)


# ---------------------------------------------------------------------------
# 自动同步开关：控制该源站是否参与「定时任务 / 全站同步」
# （手动单站同步不受影响；状态持久化在 sources 表，重启后仍生效）
# ---------------------------------------------------------------------------
def set_source_auto_sync(slug: str, enabled: bool) -> None:
    with database.write_txn() as conn:
        conn.execute(
            sources.update().where(sources.c.slug == slug).values(
                auto_sync=1 if enabled else 0
            )
        )
    log.info("[%s] 自动同步 = %s", slug, "开启" if enabled else "暂停")


def set_auto_sync_bulk(enabled: bool, slugs: list = None) -> int:
    """
    批量设置自动同步开关。slugs 为空/None 表示全部源站。
    返回实际更新的源站数量。
    """
    stmt = sources.update().values(auto_sync=1 if enabled else 0)
    if slugs:
        stmt = stmt.where(sources.c.slug.in_(slugs))
    with database.write_txn() as conn:
        res = conn.execute(stmt)
    n = res.rowcount or 0
    log.info("批量自动同步 = %s: 更新 %d 个源站%s",
             "开启" if enabled else "暂停", n,
             f" ({','.join(slugs[:5])}...)" if slugs and len(slugs) > 5 else "")
    return n


# ---------------------------------------------------------------------------
# 批量 UPSERT（SQLite / PostgreSQL 通用语法）
# ---------------------------------------------------------------------------
_UPSERT_COLS = ["vod_id", "type_id", "type_name", "name", "pic", "remarks", "content", "play_url", "vod_time"]


def upsert_batch(slug: str, rows: list) -> tuple:
    """
    批量写入一页数据，返回 (inserted, updated)。
    冲突策略：同 vod_id 时更新全部字段（源站更新了播放地址/封面等也能跟进）。
    """
    if not rows:
        return 0, 0
    table = build_vod_table(slug)
    # 先查询本批中已存在的 id，用于统计 inserted/updated
    ids = [r["vod_id"] for r in rows]
    existing = set()
    with database.write_txn() as conn:
        for i in range(0, len(ids), 500):
            chunk = ids[i : i + 500]
            found = conn.execute(
                select(table.c.vod_id).where(table.c.vod_id.in_(chunk))
            )
            existing.update(r[0] for r in found)

        stmt = (
            database.dialect_insert(table)
            .values(
                vod_id=bindparam("vod_id"),
                type_id=bindparam("type_id"),
                type_name=bindparam("type_name"),
                name=bindparam("name"),
                pic=bindparam("pic"),
                remarks=bindparam("remarks"),
                content=bindparam("content"),
                play_url=bindparam("play_url"),
                vod_time=bindparam("vod_time"),
            )
        )
        # ON CONFLICT 子句：SQLite 与 PostgreSQL 语法一致
        update_cols = {c: stmt.excluded[c] for c in _UPSERT_COLS if c != "vod_id"}
        stmt = stmt.on_conflict_do_update(index_elements=["vod_id"], set_=update_cols)
        conn.execute(stmt, rows)
    inserted = sum(1 for i in ids if i not in existing)
    return inserted, len(ids) - inserted


def fetch_class_map(payload: dict, class_map: dict) -> dict:
    """从响应的 class 数组合并分类 id->name 映射。"""
    for c in payload.get("class") or []:
        try:
            class_map[int(c.get("type_id"))] = (c.get("type_name") or "").strip()
        except (TypeError, ValueError):
            continue
    return class_map


# ---------------------------------------------------------------------------
# 全量同步（断点续传）
# ---------------------------------------------------------------------------
def full_sync(slug: str, client: MacCmsClient) -> dict:
    src_api = None
    with database.engine.connect() as conn:
        src = get_source(conn, slug)
        if not src:
            raise MacCmsError(f"源站 {slug} 不存在")
        src_api = src["api_url"]
        resume_page = src["last_page"] or 0

    stats = {"fetched": 0, "inserted": 0, "updated": 0, "pages": 0}
    class_map: dict = {}

    first = client.fetch_page(src_api, "detail", 1)
    fetch_class_map(first, class_map)
    pagecount = int(first.get("pagecount") or 0)
    total = int(first.get("total") or 0)
    # 记录总页数，供管理面板显示进度（断点续传时也保持有效）
    with database.write_txn() as conn:
        conn.execute(
            sources.update().where(sources.c.slug == slug).values(total_pages=pagecount)
        )
    log.info("[%s] 全量同步开始: total=%s pagecount=%s 断点=%s", slug, total, pagecount, resume_page)

    start_page = max(resume_page, 0) + 1 if resume_page else 1
    pg = start_page

    def process_page(page: int) -> dict:
        payload = first if page == 1 else client.fetch_page(src_api, "detail", page)
        cmap = fetch_class_map(payload, {})
        rows = [
            r
            for r in (MacCmsClient.extract_row(it, cmap) for it in payload.get("list") or [])
            if r
        ]
        return {"page": page, "rows": rows}

    consecutive_fail = 0
    while pg <= pagecount:
        # ---- 暂停检查点：下一批次开始前优雅停止，断点保留在第 pg-1 页 ----
        if should_stop(slug, "full"):
            _save_checkpoint(slug, pg - 1)
            stats["paused"] = True
            stats["paused_at_page"] = pg - 1
            log.info("[%s] 全量同步已暂停（断点=%d，可随时继续）", slug, pg - 1)
            return stats

        batch_pages = list(range(pg, min(pg + config.PAGE_CONCURRENCY, pagecount + 1)))
        results = []
        with ThreadPoolExecutor(max_workers=len(batch_pages)) as pool:
            futures = {pool.submit(process_page, p): p for p in batch_pages}
            for fut in as_completed(futures):
                try:
                    results.append(fut.result())
                    consecutive_fail = 0
                except MacCmsError as e:
                    log.error("[%s] 第 %s 页抓取失败: %s", slug, futures[fut], e)
                    consecutive_fail += 1
        if results:
            results.sort(key=lambda x: x["page"])
            for res in results:
                ins, upd = upsert_batch(slug, res["rows"])
                stats["fetched"] += len(res["rows"])
                stats["inserted"] += ins
                stats["updated"] += upd
                stats["pages"] += 1
            last_done = max(r["page"] for r in results)
            if last_done % config.CHECKPOINT_EVERY_PAGES == 0 or last_done == pagecount:
                _save_checkpoint(slug, last_done)
            pg = last_done + 1
        elif consecutive_fail >= config.PAGE_CONCURRENCY:
            # 整批失败，直接退出，断点已保证下次续传
            log.error("[%s] 连续整批失败，中止全量（断点=%d）", slug, pg - 1)
            _save_checkpoint(slug, pg - 1)
            stats["error"] = f"页面抓取连续失败于第 {pg} 页"
            return stats
        time.sleep(0.1)

    _finish_full(slug, class_map)
    log.info("[%s] 全量同步完成: %s", slug, stats)
    return stats


def _table_count(table) -> int:
    """
    单独用读连接统计行数。

    为什么不放在写事务里：count(*) 要扫整棵表（十万~百万行，几百毫秒），
    放进写事务就等于「持写锁做全表扫描」，其他源站的写全部排队，是连接池被占满的帮凶。
    WAL 下读不阻塞写，所以独立读连接拿数更划算（数字最多滞后一次检查点）。
    """
    try:
        with database.engine.connect() as conn:
            return conn.execute(select(func.count()).select_from(table)).scalar() or 0
    except Exception as e:  # 统计失败不影响断点保存
        log.warning("统计 %s 条数失败: %s", table.name, e)
        return None


def _save_checkpoint(slug: str, page: int):
    """
    保存全量同步断点，同时刷新本库条数。
    为什么在这里算条数：全量同步可能持续数小时，若不刷新，管理面板会一直显示 0 条。
    """
    table = build_vod_table(slug)
    count = _table_count(table)
    values = {"last_page": page, "last_sync_at": datetime.now()}
    if count is not None:
        values["total_videos"] = count
    with database.write_txn() as conn:
        conn.execute(sources.update().where(sources.c.slug == slug).values(**values))


def refresh_counts(slug: str = None) -> int:
    """
    重算本地条数（用于同步中断/异常后的数据校准，避免面板显示与实际不符）。
    slug 为空则重算全部源站。返回更新的源站数量。
    """
    with database.engine.connect() as conn:
        if slug:
            slugs = [slug]
        else:
            slugs = [r[0] for r in conn.execute(select(sources.c.slug)).all()]
    done = 0
    for s in slugs:
        try:
            table = build_vod_table(s)
            if not database.table_exists(table.name):
                continue
            count = _table_count(table)
            if count is None:
                continue
            with database.write_txn() as conn:
                conn.execute(
                    sources.update().where(sources.c.slug == s).values(total_videos=count)
                )
            done += 1
        except Exception as e:
            log.warning("重算 %s 条数失败: %s", s, e)
    log.info("已重算 %d 个源站的本地条数", done)
    return done


def _finish_full(slug: str, class_map: dict):
    """全量完成后：清断点、写入水位线为库内最大 vod_time、刷新条数。"""
    table = build_vod_table(slug)
    with database.engine.connect() as conn:
        max_time = conn.execute(select(func.max(table.c.vod_time))).scalar() or ""
    count = _table_count(table)
    with database.write_txn() as conn:
        conn.execute(
            sources.update()
            .where(sources.c.slug == slug)
            .values(
                last_page=0,
                total_pages=0,
                watermark=max_time[:19],
                total_videos=count if count is not None else 0,
                last_sync_at=datetime.now(),
            )
        )
    log.info("[%s] 水位线=%s 本库条数=%s", slug, max_time, count)


# ---------------------------------------------------------------------------
# 增量同步
# ---------------------------------------------------------------------------
def incremental_sync(slug: str, client: MacCmsClient) -> dict:
    with database.engine.connect() as conn:
        src = get_source(conn, slug)
        if not src:
            raise MacCmsError(f"源站 {slug} 不存在")
        src_api = src["api_url"]
        watermark = src["watermark"] or ""

    stats = {"fetched": 0, "inserted": 0, "updated": 0, "pages": 0}
    class_map: dict = {}

    # 1) 轻量遍历 ac=list，收集 id -> (vod_time, type_id, name)
    index: dict = {}
    first = client.fetch_page(src_api, "list", 1)
    fetch_class_map(first, class_map)
    pagecount = int(first.get("pagecount") or 0)

    def collect(page: int) -> dict:
        payload = first if page == 1 else client.fetch_page(src_api, "list", page)
        cmap = fetch_class_map(payload, {})
        return {
            it.get("vod_id"): (
                (it.get("vod_time") or "").strip()[:19],
                it.get("type_id"),
                cmap.get(int(it.get("type_id") or 0), ""),
            )
            for it in payload.get("list") or []
            if it.get("vod_id")
        }

    pages = list(range(1, pagecount + 1))
    for i in range(0, len(pages), config.PAGE_CONCURRENCY):
        # ---- 暂停检查点：list 索引阶段可随时中断（该阶段无副作用，继续时重跑即可）----
        if should_stop(slug, "incremental"):
            stats["paused"] = True
            log.info("[%s] 增量同步已暂停（索引阶段，水位线未动）", slug)
            return stats
        batch = pages[i : i + config.PAGE_CONCURRENCY]
        with ThreadPoolExecutor(max_workers=len(batch)) as pool:
            for fut in as_completed({pool.submit(collect, p): p for p in batch}):
                try:
                    index.update(fut.result())
                    stats["pages"] += 1
                except MacCmsError as e:
                    log.error("[%s] 增量 list 第 %s 页失败: %s", slug, fut.result() and "", e)
        stats["fetched"] = len(index)

    if not index:
        log.info("[%s] 增量同步: 源站列表为空", slug)
        return stats

    # 2) 筛选候选：vod_time > 水位线（新更新），可选检测缺失 ID
    table = build_vod_table(slug)
    candidates = {vid for vid, (t, _tid, _n) in index.items() if t and t > watermark}

    if config.INCR_CHECK_MISSING and len(index) < 200000:
        # 检测本库缺失的 ID（源站数据被重置/换库时可兜底；大表较慢，默认关闭）
        all_ids = list(index.keys())
        with database.engine.connect() as conn:
            for i in range(0, len(all_ids), 900):
                chunk = all_ids[i : i + 900]
                found = set(
                    conn.execute(select(table.c.vod_id).where(table.c.vod_id.in_(chunk))).scalars()
                )
                candidates.update(set(chunk) - found)

    if not candidates:
        _mark_synced(slug, watermark, len(index))
        log.info("[%s] 增量同步: 无更新 (水位线=%s, 在库索引=%d)", slug, watermark, len(index))
        return stats

    # 3) 按 ID 批量拉详情并 UPSERT
    ids = sorted(candidates)
    log.info("[%s] 增量同步: %d 条候选需要拉取详情", slug, len(ids))
    for i in range(0, len(ids), 50):
        # ---- 暂停检查点：详情拉取阶段中断，水位线不动，继续时重新筛选候选 ----
        if should_stop(slug, "incremental"):
            stats["paused"] = True
            stats["paused_candidates_left"] = len(ids) - i
            log.info("[%s] 增量同步已暂停（详情阶段，水位线未动）", slug)
            return stats
        chunk = ids[i : i + 50]
        try:
            payload = client.fetch_detail_by_ids(src_api, chunk)
        except MacCmsError as e:
            log.error("[%s] 详情批量拉取失败(跳过 %d 条): %s", slug, len(chunk), e)
            continue
        fetch_class_map(payload, class_map)
        rows = []
        for it in payload.get("list") or []:
            row = MacCmsClient.extract_row(it, class_map)
            if row and row["vod_id"] in set(chunk):
                rows.append(row)
        ins, upd = upsert_batch(slug, rows)
        stats["inserted"] += ins
        stats["updated"] += upd

    # 4) 推进水位线
    max_time = watermark
    for vid, (t, _tid, _n) in index.items():
        if t > max_time:
            max_time = t
    _mark_synced(slug, max_time, len(index))
    log.info("[%s] 增量同步完成: %s 新水位线=%s", slug, stats, max_time)
    return stats


def _mark_synced(slug: str, watermark: str, _index_size: int):
    """同步成功后：更新水位线/时间，并刷新本地总条数。"""
    table = build_vod_table(slug)
    count = _table_count(table)
    values = {"watermark": watermark[:19], "last_sync_at": datetime.now()}
    if count is not None:
        values["total_videos"] = count
    with database.write_txn() as conn:
        conn.execute(sources.update().where(sources.c.slug == slug).values(**values))


# ---------------------------------------------------------------------------
# 任务入口
# ---------------------------------------------------------------------------
def sync_source(slug: str, mode: str = "auto") -> dict:
    """
    同步单个源站。
    mode: auto（无水位线则全量，否则增量）/ full / incremental

    两道保护（v1.4.1）：
    1. 同一源站不允许并发触发 —— 面板连点两下不会变成两个线程双写同一张表；
    2. 全局并发闸门 —— 同时真正在跑的源站不超过 SOURCE_SYNC_CONCURRENCY 个，
       其余排队（排队不占数据库连接，避免把连接池占满）。
    """
    with database.engine.connect() as conn:
        src = get_source(conn, slug)
    if not src:
        raise MacCmsError(f"源站 {slug} 不存在")
    if mode == "auto":
        mode = "full" if not src["watermark"] or src["last_page"] else "incremental"

    # ---- 暂停检查：入口处直接受理为"已暂停"，不启动网络请求 ----
    if should_stop(slug, mode):
        log.info("[%s] 同步被暂停控制拦截 (mode=%s)", slug, mode)
        return {"slug": slug, "mode": mode, "status": "paused"}

    # ---- 防重入：同一源站已在排队/运行中，直接返回 ----
    with _running_lock:
        if slug in _pending:
            log.info("[%s] 已有同步任务在执行，跳过本次触发 (mode=%s)", slug, mode)
            return {"slug": slug, "mode": mode, "status": "already_running"}
        _pending.add(slug)

    try:
        # ---- 并发闸门：排队等待，不占数据库连接 ----
        with _slots():
            log_id = _start_log(slug, mode)
            client = MacCmsClient()
            mark_running(slug, mode)      # 进入运行态：此后该源站才允许「暂停」
            try:
                ensure_table(slug)
                if mode == "full":
                    stats = full_sync(slug, client)
                else:
                    stats = incremental_sync(slug, client)
                _finish_log(log_id, "paused" if stats.get("paused") else "success", stats)
                return {"slug": slug, "mode": mode,
                        "status": "paused" if stats.get("paused") else "success", **stats}
            except Exception as e:
                log.exception("[%s] 同步失败", slug)
                _finish_log(log_id, "failed", {"error": str(e)})
                return {"slug": slug, "mode": mode, "status": "failed", "error": str(e)}
            finally:
                mark_stopped(slug)        # 退出运行态
                client.close()
    finally:
        with _running_lock:
            _pending.discard(slug)


def trigger_resume(scope: str) -> list:
    """
    暂停解除后自动续跑对应范围的同步（后台执行）。

    scope:
      full        -> 只续跑有断点（last_page>0）的源站，mode=full，从断点继续
      incremental -> 跑所有已有水位线源站的增量
      global      -> 全部启用源站 mode=auto（有断点续全量，有水位线走增量）
    返回受影响的 slug 列表。
    """
    with database.engine.connect() as conn:
        q = select(sources.c.slug).where(sources.c.enabled == 1, sources.c.paused == 0)
        if scope == "full":
            # 全量续跑针对的是用户显式发起的任务，不筛自动同步开关
            q = q.where(sources.c.last_page > 0)
        elif scope == "incremental":
            q = q.where(sources.c.watermark != "", sources.c.auto_sync == 1)
        else:  # global
            q = q.where(sources.c.auto_sync == 1)
        slugs = [r[0] for r in conn.execute(q).all()]
    mode = {"full": "full", "incremental": "incremental"}.get(scope, "auto")
    threading.Thread(target=_resume_worker, args=(slugs, mode), daemon=True).start()
    log.info("继续同步 [%s]: %d 个源站, mode=%s", scope, len(slugs), mode)
    return slugs


def resume_source(slug: str) -> dict:
    """
    继续单个源站：清除该源站的暂停标记，并按 auto 模式续跑
    （有断点续全量、有水位线走增量）。返回受理结果。
    """
    set_source_paused(slug, False)
    threading.Thread(target=sync_source, args=(slug, "auto"), daemon=True).start()
    return {"slug": slug, "resumed": True}


def _resume_worker(slugs: list, mode: str):
    for s in slugs:
        try:
            sync_source(s, mode)
        except Exception:
            log.exception("续跑源站 %s 失败", s)


def sync_all(force_mode: str = "auto") -> list:
    """
    并行同步所有源站（定时任务与「全站同步」共用入口）。
    跳过：手动暂停的站 + 自动同步已关闭的站。
    """
    with database.engine.connect() as conn:
        slugs = [r[0] for r in conn.execute(
            select(sources.c.slug).where(
                sources.c.enabled == 1,
                sources.c.paused == 0,
                sources.c.auto_sync == 1,
            )
        ).all()]
    log.info("开始同步 %d 个源站 (模式=%s，暂停/关闭自动同步的源站已跳过)", len(slugs), force_mode)
    results = []
    with ThreadPoolExecutor(max_workers=config.SOURCE_SYNC_CONCURRENCY) as pool:
        futures = {pool.submit(sync_source, s, force_mode): s for s in slugs}
        for fut in as_completed(futures):
            try:
                results.append(fut.result())
            except Exception as e:  # 理论上 sync_source 内部已兜底
                log.error("源站 %s 同步异常: %s", futures[fut], e)
    ok = sum(1 for r in results if r.get("status") == "success")
    log.info("同步结束: 成功 %d / 失败 %d", ok, len(results) - ok)
    return results


def _start_log(slug: str, mode: str) -> int:
    with database.write_txn() as conn:
        res = conn.execute(
            sync_logs.insert().values(slug=slug, mode=mode, status="running")
        )
        return res.inserted_primary_key[0]


def _finish_log(log_id: int, status: str, stats: dict):
    with database.write_txn() as conn:
        conn.execute(
            sync_logs.update()
            .where(sync_logs.c.id == log_id)
            .values(
                status=status,
                fetched=stats.get("fetched", 0) or 0,
                inserted=stats.get("inserted", 0) or 0,
                updated=stats.get("updated", 0) or 0,
                pages=stats.get("pages", 0) or 0,
                error=(stats.get("error") or "")[:2000],
                finished_at=datetime.now(),
            )
        )
