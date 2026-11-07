"""
同步引擎：源站发现、全量同步（断点续传）、增量同步（水位线）。

设计要点：
1. 全量：ac=detail 翻页，多线程抓取，按页批量 UPSERT，每 N 页保存断点到 sources.last_page
2. 增量：ac=list 只取 vod_id/vod_time（轻量），只对 vod_time > 水位线 或 本库缺失的
   ID 拉取 ac=detail&ids=...，百万级存量数据零重复传输
3. 写入：批量 INSERT ... ON CONFLICT(vod_id) DO UPDATE（PostgreSQL 原生语法）
"""
import logging
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

from sqlalchemy import bindparam, func, select, text

from . import config, database, pause
from .maccms import MacCmsClient, MacCmsError
from .models import build_vod_table, forget_vod_table, slugify, sources, sync_logs

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
# 历史背景是 SQLite 只允许一个写者、等锁线程会占满连接池（v1.4.1 的 P0）；
# 换到 PG 后写不再串行，但**同步本身仍会给源站和 NAS 造成压力**，这个闸门继续保留：
# 它限制的是「同时真正在跑的全站同步数」，超出的排队且不占数据库连接。
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
def _find_source_by_api(conn, api_url: str):
    """
    按 API 地址找已登记的源站。

    **源站的身份是 api_url，不是名字**。名字会撞：`slugify("HD 资源")` 和
    `slugify("HD-资源")` 都是 `hd`，只按 slug 判重会让后来那个站被当成「已存在」，
    于是它的 api_url 永远写不进去 —— 该站长期同步的其实是别人家的接口，且没有任何报错。

    这里再兼容一下「尾斜杠」差异（同一 API 被登记成带/不带 `/` 两种写法时不要重复建表）。
    """
    url = api_url[:500]
    cols = (sources.c.id, sources.c.slug, sources.c.api_url, sources.c.origin)
    row = conn.execute(select(*cols).where(sources.c.api_url == url)).mappings().first()
    if row:
        return row
    trim = url.rstrip("/")
    if trim != url:
        row = conn.execute(select(*cols).where(sources.c.api_url == trim)).mappings().first()
        if row:
            return row
    return conn.execute(select(*cols).where(sources.c.api_url == trim + "/")).mappings().first()


def _unique_slug(conn, base: str) -> str:
    """
    slug 已被占用（不同 api_url 但名字撞了）时追加 `_2` / `_3` …

    为什么用下划线：`utils.SAFE_SLUG` 只放行 `[a-z0-9_]`，用 `-` 会让这个源站的
    所有 API 路径直接被 400 拒掉。表名 `vod_hd_2` 也合法。
    """
    if not conn.execute(select(sources.c.id).where(sources.c.slug == base)).first():
        return base
    for i in range(2, 52):
        cand = f"{base}_{i}"
        if not conn.execute(select(sources.c.id).where(sources.c.slug == cand)).first():
            return cand
    # 理论上到不了这里（52 个同名站）；用随机后缀兜底，绝不返回重复值
    return f"{base[:50]}_{secrets.token_hex(3)}"


def refresh_sources(client: MacCmsClient = None) -> list:
    """
    拉取 latest.json，把新的源站登记进 sources 表并创建数据表。
    返回新增源站的 slug 列表。已存在的源站（按 api_url 认）只更新元信息。
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

            src = _find_source_by_api(conn, api_url)
            if src:
                # 已登记：更新元信息。**slug 不再变** —— 它是表名和 API 路径的一部分，
                # 改名等于把已有数据和所有客户端一起废掉。
                if (src.get("origin") or "latest") == "custom":
                    # 用户手动添加的源站：名称/描述是用户填的，latest.json 无权覆盖。
                    # 只更新 status（在线状态是客观事实），其余保持原样。
                    # 这也是「自定义源持续存在」的关键：refresh 既不覆盖，也不删除。
                    conn.execute(
                        sources.update()
                        .where(sources.c.id == src["id"])
                        .values(status=res.get("status") or "ok")
                    )
                    continue
                conn.execute(
                    sources.update()
                    .where(sources.c.id == src["id"])
                    .values(
                        name=name[:120],
                        status=res.get("status") or "ok",
                        description=(res.get("description") or "")[:500],
                    )
                )
                continue

            slug = _unique_slug(conn, slugify(name))
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
                    origin="latest",
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


# ---------------------------------------------------------------------------
# 自定义源站（v1.8.0）
#
# 与 latest.json 自动发现的源站共用 sources 表，靠 origin 列区分：
#   latest —— 由 refresh_sources() 维护，元信息可被 JSON 覆盖
#   custom —— 用户在面板手动登记，refresh_sources() 只更新 status，绝不删除
#
# 「自定义源持续存在」不依赖任何额外存储：它就是 sources 表里一行 origin='custom'
# 的记录。重启、刷新源站列表、重新部署都不会丢。
# ---------------------------------------------------------------------------

def probe_source(api_url: str, timeout: int = None) -> dict:
    """
    抓第 1 页验证一个地址是不是可用的苹果 CMS 采集接口。不落库、不建表。

    用 ac=list 而非 ac=detail：只取一页元信息（几十 KB），比 detail 快一个量级，
    足以判断「域名通不通、是不是这套接口、有没有数据」。
    """
    started = time.time()
    client = MacCmsClient(timeout=min(timeout or config.REQUEST_TIMEOUT, 15), retries=0)
    try:
        data = client.fetch_page(api_url, "list", 1)
    except Exception as exc:  # noqa: BLE001 - 任何异常都要转成人能看懂的一句话
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {str(exc)[:180]}",
            "elapsed_ms": int((time.time() - started) * 1000),
        }
    finally:
        client.close()

    elapsed = int((time.time() - started) * 1000)
    items = data.get("list")
    if not isinstance(items, list):
        return {"ok": False, "error": "响应不是苹果 CMS 格式（缺少 list 字段）", "elapsed_ms": elapsed}
    if not items:
        return {
            "ok": False,
            "error": "接口可访问但列表为空，该源站可能暂无数据",
            "total": int(data.get("total") or 0),
            "elapsed_ms": elapsed,
        }
    return {
        "ok": True,
        "total": int(data.get("total") or 0),
        "pagecount": int(data.get("pagecount") or 0),
        "samples": [(it.get("vod_name") or "").strip() for it in items[:3]],
        "elapsed_ms": elapsed,
    }


def register_custom_source(name: str, api_url: str, slug: str = "",
                           homepage: str = "", description: str = "") -> dict:
    """
    登记手动添加的源站并立即建表。
    抛 ValueError 表示业务冲突（API 已登记 / slug 已占用），由 API 层转 409。
    """
    name = (name or "").strip()[:120]
    api_url = (api_url or "").strip()[:500]
    if not name:
        raise ValueError("源站名称不能为空")
    if not api_url:
        raise ValueError("API 地址不能为空")

    with database.write_txn() as conn:
        exist = _find_source_by_api(conn, api_url)
        if exist:
            raise ValueError(f"该 API 地址已登记为源站 {exist['slug']}")
        if slug:
            slug = slug.strip().lower()
            if conn.execute(select(sources.c.id).where(sources.c.slug == slug)).first():
                raise ValueError(f"标识 {slug} 已被占用，请换一个")
        else:
            # 中文名会变成 s+md5 前缀，用户看不出含义，但作为表名/路径是稳定的；
            # 想要可读标识就在表单里自己填。
            slug = _unique_slug(conn, slugify(name) or "custom")
        conn.execute(
            sources.insert().values(
                name=name,
                slug=slug,
                api_url=api_url,
                homepage=(homepage or "").strip()[:500],
                description=(description or "").strip()[:500],
                status="ok",
                enabled=1,
                auto_sync=1,
                origin="custom",
            )
        )
    ensure_table(slug)
    log.info("自定义源站已登记: %s (%s)", slug, api_url)
    return {"slug": slug, "created": True}


def update_custom_source(slug: str, **fields) -> dict:
    """
    修改自定义源站，接受 name / api_url / homepage / description / enabled / auto_sync。

    **改 api_url 会清空该站已入库的数据并重置水位线**：换了接口就是换了数据源，
    旧行留着只会让列表里混进一批「新源已经没有」的片子，而且 vod_id 冲突时会被
    覆盖成新源内容，看上去像是数据错乱。清空是唯一语义清晰的做法。
    """
    with database.write_txn() as conn:
        src = conn.execute(select(sources).where(sources.c.slug == slug)).mappings().first()
        if not src:
            raise LookupError(f"源站不存在: {slug}")
        if (src["origin"] or "latest") != "custom":
            raise PermissionError("只能修改手动添加的源站；官方源站请用「自动同步」开关控制")

        changes: dict = {}
        if fields.get("name") is not None:
            v = str(fields["name"]).strip()[:120]
            if not v:
                raise ValueError("源站名称不能为空")
            changes["name"] = v
        if fields.get("homepage") is not None:
            changes["homepage"] = str(fields["homepage"]).strip()[:500]
        if fields.get("description") is not None:
            changes["description"] = str(fields["description"]).strip()[:500]
        if fields.get("enabled") is not None:
            changes["enabled"] = 1 if fields["enabled"] else 0
        if fields.get("auto_sync") is not None:
            changes["auto_sync"] = 1 if fields["auto_sync"] else 0

        reset_data = False
        if fields.get("api_url") is not None:
            new_api = str(fields["api_url"]).strip()[:500]
            if not new_api:
                raise ValueError("API 地址不能为空")
            if new_api != (src["api_url"] or ""):
                dup = _find_source_by_api(conn, new_api)
                if dup and dup["slug"] != slug:
                    raise ValueError(f"该 API 地址已登记为源站 {dup['slug']}")
                changes.update(api_url=new_api, watermark="", last_page=0,
                               total_pages=0, total_videos=0)
                reset_data = True

        if changes:
            conn.execute(sources.update().where(sources.c.slug == slug).values(**changes))

    if reset_data:
        table = build_vod_table(slug)
        if database.table_exists(table.name):
            with database.write_txn() as conn:
                conn.execute(table.delete())
        delete_related_rows(slug)
        delete_star_map_rows(slug)
        log.info("源站 %s 的 API 地址已变更，历史数据已清空", slug)

    return {"slug": slug, "updated": sorted(changes), "data_reset": reset_data}


def delete_custom_source(slug: str, drop_table: bool = True) -> dict:
    """
    删除自定义源站。

    只允许删 origin='custom' 的：官方源站删掉之后下一次 refresh 还会回来，
    删它没有意义，反而让「我的源站怎么不见了」变成一个查不清的谜。
    """
    if is_busy(slug):
        raise RuntimeError(f"{slug} 正在同步，请先暂停或等它结束再删除")

    with database.write_txn() as conn:
        src = conn.execute(select(sources).where(sources.c.slug == slug)).mappings().first()
        if not src:
            raise LookupError(f"源站不存在: {slug}")
        if (src["origin"] or "latest") != "custom":
            raise PermissionError("只能删除手动添加的源站；官方源站请关闭自动同步或禁用")
        conn.execute(sources.delete().where(sources.c.slug == slug))

    if drop_table:
        table = build_vod_table(slug)
        table.drop(database.engine, checkfirst=True)
        forget_vod_table(slug)
    delete_related_rows(slug)
    delete_star_map_rows(slug)
    log.info("自定义源站已删除: %s（数据表%s）", slug, "已删除" if drop_table else "保留")
    return {"slug": slug, "dropped": bool(drop_table)}


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
# 批量 UPSERT（PostgreSQL 的 ON CONFLICT；测试后端 SQLite 同语法）
# ---------------------------------------------------------------------------
_UPSERT_COLS = [
    "vod_id", "type_id", "type_name", "name", "pic", "remarks",
    "content", "play_url",
    # 相关推荐信号字段（v1.9.0）：重跑全量回填时能把老行补上这些列
    "vod_actor", "vod_director", "vod_writer", "vod_area", "vod_year", "vod_tag",
    "vod_time",
]

# 可为空的文本列：手工构造的行（老脚本 / 测试 / 外部导入）只给必填字段时补空串。
# 不补的话 executemany 会报 "A value is required for bind parameter 'vod_actor'"（v1.9.0 扩列后
# 老脚本就是这样挂的）。抓取路径（extract_row）本来就带全 15 列，不受影响。
_OPTIONAL_TEXT_COLS = (
    "type_name", "pic", "remarks", "content", "play_url",
    "vod_actor", "vod_director", "vod_writer", "vod_area", "vod_year", "vod_tag",
)


def upsert_batch(slug: str, rows: list) -> tuple:
    """
    批量写入一页数据，返回 (inserted, updated)。
    冲突策略：同 vod_id 时更新全部字段（源站更新了播放地址/封面等也能跟进）。
    """
    if not rows:
        return 0, 0
    rows = [dict(r) for r in rows]
    for r in rows:
        for c in _OPTIONAL_TEXT_COLS:
            r.setdefault(c, "")
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
                vod_actor=bindparam("vod_actor"),
                vod_director=bindparam("vod_director"),
                vod_writer=bindparam("vod_writer"),
                vod_area=bindparam("vod_area"),
                vod_year=bindparam("vod_year"),
                vod_tag=bindparam("vod_tag"),
                vod_time=bindparam("vod_time"),
            )
        )
        # ON CONFLICT 子句：SQLite 与 PostgreSQL 语法一致
        update_cols = {c: stmt.excluded[c] for c in _UPSERT_COLS if c not in ("vod_id", "vod_tag")}
        # vod_tag 特殊处理（v1.9.1）：源站给的 tag 基本缺失，空值不覆盖本地推断的
        # 明星 tag（star_tags 回填结果），源站真给了非空 tag 才更新
        update_cols["vod_tag"] = func.coalesce(
            func.nullif(stmt.excluded["vod_tag"], ""), table.c.vod_tag
        )
        stmt = stmt.on_conflict_do_update(index_elements=["vod_id"], set_=update_cols)
        conn.execute(stmt, rows)
    inserted = sum(1 for i in ids if i not in existing)
    return inserted, len(ids) - inserted


def _note_touched(stats: dict, rows: list):
    """
    记录本轮真正写进库的 vod_id（相关推荐增量重算的依据）。
    上限 RELATED_TOUCHED_CAP：全量同步本来就触发整站重算，不依赖这份清单。
    """
    if not rows:
        return
    t = stats.setdefault("touched", [])
    cap = max(0, config.RELATED_TOUCHED_CAP - len(t))
    if cap:
        t.extend(r["vod_id"] for r in rows[:cap])


def delete_related_rows(slug: str):
    """删除某源站的全部推荐结果（删源站/清数据时调用；表不存在则跳过）。"""
    from .models import vod_related

    try:
        with database.write_txn() as conn:
            conn.execute(vod_related.delete().where(vod_related.c.slug == slug))
    except Exception as e:
        log.warning("[%s] 清理推荐结果失败（忽略）: %s", slug, e)


def delete_star_map_rows(slug: str):
    """删除某源站的明星-视频映射（删源站/清数据时调用，与推荐结果同步清理）。"""
    from .models import star_map

    try:
        with database.write_txn() as conn:
            conn.execute(star_map.delete().where(star_map.c.slug == slug))
    except Exception as e:
        log.warning("[%s] 清理明星映射失败（忽略）: %s", slug, e)


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
    stats["pages_total"] = pagecount   # 供健康度判定计算失败率

    start_page = max(resume_page, 0) + 1 if resume_page else 1
    pg = start_page

    # ---------------------------------------------------------------------
    # 断点只能落在「连续完成的前缀」上
    #
    # 反例（v1.5.2 的静默丢数据）：一个批次 [1,2,3,4] 里第 3 页失败、4 成功，
    # 旧代码用 last_done = max(成功页) = 4 存断点，下一轮从 5 开始 —— 第 3 页永远
    # 不会被再抓，循环还正常结束、_finish_full 清断点推进水位线，面板显示 100%。
    # 现在改用 written_upto：只有 [resume+1 .. written_upto] 全部写入成功才前进，
    # 失败页留在前缀之外，本轮内重试，重启后续传也一定从它开始。
    # ---------------------------------------------------------------------
    written_upto = max(resume_page, 0)
    attempts: dict = {}          # page -> 本次运行内已失败次数
    last_error = ""

    def process_page(page: int) -> dict:
        payload = first if page == 1 else client.fetch_page(src_api, "detail", page)
        cmap = fetch_class_map(payload, {})
        rows = [
            r
            for r in (MacCmsClient.extract_row(it, cmap) for it in payload.get("list") or [])
            if r
        ]
        return {"page": page, "rows": rows}

    while pg <= pagecount:
        # ---- 暂停检查点：下一批次开始前优雅停止，断点保留在真正的完成位置 ----
        if should_stop(slug, "full"):
            _save_checkpoint(slug, written_upto)
            stats["paused"] = True
            stats["paused_at_page"] = written_upto
            log.info("[%s] 全量同步已暂停（断点=%d，可随时继续）", slug, written_upto)
            return stats

        batch_pages = list(range(pg, min(pg + config.PAGE_CONCURRENCY, pagecount + 1)))
        results, failures = [], []
        with ThreadPoolExecutor(max_workers=len(batch_pages)) as pool:
            futures = {pool.submit(process_page, p): p for p in batch_pages}
            for fut in as_completed(futures):
                page = futures[fut]
                try:
                    results.append(fut.result())
                except MacCmsError as e:
                    failures.append(page)
                    log.error("[%s] 第 %d 页抓取失败: %s", slug, page, e)

        if results:
            # 幂等 UPSERT：失败页重试时，同批次已成功的页会再写一遍，不会产生脏数据
            results.sort(key=lambda x: x["page"])
            for res in results:
                ins, upd = upsert_batch(slug, res["rows"])
                _note_touched(stats, res["rows"])
                stats["fetched"] += len(res["rows"])
                stats["inserted"] += ins
                stats["updated"] += upd
                stats["pages"] += 1

            # 只把「和已有前缀连得上」的页计入断点
            got = {r["page"] for r in results}
            for p in got:
                attempts.pop(p, None)   # 本轮已成功，不再计失败次数
            while (written_upto + 1) in got:
                written_upto += 1

        for page in failures:
            attempts[page] = attempts.get(page, 0) + 1
            last_error = f"第 {page} 页抓取失败（第 {attempts[page]} 次）"
            if attempts[page] >= config.PAGE_MAX_ATTEMPTS:
                last_error = (
                    f"第 {page} 页连续 {attempts[page]} 次抓取失败，"
                    f"已保留断点 {written_upto}/{pagecount}"
                )
                break
        stats["failed_pages"] = len(attempts)

        # 有些页是「一次性整批全挂」（源站宕机/被限流），继续重试同批次只是空转
        aborted = (
            last_error if (attempts and max(attempts.values()) >= config.PAGE_MAX_ATTEMPTS) else ""
        )
        if not results and failures:
            aborted = aborted or f"第 {pg}~{batch_pages[-1]} 页整批失败，已保留断点 {written_upto}/{pagecount}"
        if aborted:
            _save_checkpoint(slug, written_upto)
            stats["error"] = aborted
            stats["incomplete"] = True
            log.error("[%s] 全量同步中止: %s", slug, aborted)
            return stats

        # 按进度存断点（和旧的每 N 页一致，避免每批都做一次 count(*) 全表扫描）
        if written_upto and (
            written_upto % config.CHECKPOINT_EVERY_PAGES == 0 or written_upto == pagecount
        ):
            _save_checkpoint(slug, written_upto)

        pg = written_upto + 1
        time.sleep(0.1)

    # ---------------------------------------------------------------------
    # 收尾：只有真的全部写完才清断点、推进水位线。
    # 万一 pagecount 在同步过程中变小（源站删数据），written_upto 会超过 pagecount，
    # 同样视为完成 —— 此时库里的数据比源站多，是安全方向。
    # ---------------------------------------------------------------------
    if written_upto < pagecount:
        _save_checkpoint(slug, written_upto)
        stats["incomplete"] = True
        stats["error"] = last_error or f"仅完成到第 {written_upto}/{pagecount} 页"
        log.error("[%s] 全量同步未完成: %s（断点已保留，可续传）", slug, stats["error"])
        return stats

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
def _classify_error(msg: str) -> str:
    """把底层报错粗分类，用于日志聚合（「2,678 个失败」不如「timeout×1900, http_403×778」有用）。"""
    m = (msg or "").lower()
    if "timed out" in m or "timeout" in m:
        return "timeout"
    if "403" in m:
        return "http_403"
    if "429" in m:
        return "http_429"
    if "404" in m:
        return "http_404"
    if "connect" in m or "getaddrinfo" in m or "translate host" in m:
        return "connect_error"
    if "ssl" in m:
        return "ssl_error"
    if "json" in m:
        return "bad_json"
    return "other"


def _failure_detail(page_errors: dict) -> str:
    """聚合失败原因：{'timeout×1900, http_403×778'} 形式的摘要，随日志行返回面板。"""
    if not page_errors:
        return ""
    from collections import Counter

    counts = Counter(_classify_error(m) for m in page_errors.values())
    return ", ".join(f"{k}×{v}" for k, v in counts.most_common())


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
    failed_list_pages: list = []
    page_errors: dict = {}   # page -> 最近一次失败原因（用于聚合错误类型）

    def scan(page_list: list) -> list:
        """
        扫描一批索引页，返回仍失败的页号列表。

        v1.8.1 两点增强：
        1. 连续 2 个批次整批失败 → 判定源站当前不可达，剩余页直接记为失败，
           不再逐页捶过去（每页内部已有 REQUEST_RETRIES 次重试，继续扫只是空转）。
        2. 暂停时未扫到的页按失败处理 —— 水位线反正不动，续跑时重扫即可。
        """
        todo = list(page_list)
        failed: list = []
        empty_batches = 0
        i = 0
        while i < len(todo):
            # ---- 暂停检查点：list 索引阶段可随时中断（该阶段无副作用，继续时重跑即可）----
            if should_stop(slug, "incremental"):
                stats["paused"] = True
                log.info("[%s] 增量同步已暂停（索引阶段，水位线未动）", slug)
                failed.extend(todo[i:])
                break
            batch = todo[i : i + config.PAGE_CONCURRENCY]
            i += len(batch)
            batch_ok = 0
            with ThreadPoolExecutor(max_workers=len(batch)) as pool:
                futures = {pool.submit(collect, p): p for p in batch}
                for fut in as_completed(futures):
                    page = futures[fut]
                    try:
                        index.update(fut.result())
                        stats["pages"] += 1
                        batch_ok += 1
                        page_errors.pop(page, None)
                    except MacCmsError as e:
                        # 注意：这里绝不能再调一次 fut.result()——对已抛异常的 Future 取结果会
                        # 把同一次失败重抛出去，整站增量直接崩掉（v1.5.2 的 P0-3）。
                        failed.append(page)
                        page_errors[page] = str(e)
                        log.error("[%s] 增量 list 第 %d 页失败: %s", slug, page, e)
            empty_batches = empty_batches + 1 if not batch_ok else 0
            if empty_batches >= 2:
                # 连续 2 批整批失败 = 至少 2×PAGE_CONCURRENCY 次请求（每次内部已重试
                # REQUEST_RETRIES 遍）全部失败 —— 这是源站宕机/被封的强信号。
                # 剩余页直接记为失败交给重试轮与下一轮调度，不再逐页捶过去。
                log.error(
                    "[%s] 增量 list 连续 %d 批整批失败，判定源站当前不可达，"
                    "剩余 %d 页跳过（省掉 %d 次注定失败的请求）",
                    slug, empty_batches, len(todo) - i, len(todo) - i,
                )
                failed.extend(todo[i:])
                break
        return failed

    failed_list_pages = scan(pages)
    # ---- 页级重试（M1）：瞬时抖动当场恢复，不必等下一轮重扫全索引 ----
    # 源站真不可达时重试轮几乎零成功，零进展即提前退出，避免空转。
    retry_round = 1
    while (
        failed_list_pages
        and retry_round < config.PAGE_MAX_ATTEMPTS
        and not stats.get("paused")
    ):
        time.sleep(config.PAGE_RETRY_SLEEP_SECONDS)
        before = stats["pages"]
        log.info("[%s] 增量 list 重试第 %d 轮（%d 页失败）", slug, retry_round, len(failed_list_pages))
        failed_list_pages = scan(failed_list_pages)
        retry_round += 1
        if stats["pages"] == before:
            log.warning("[%s] 重试零进展，提前放弃剩余 %d 页", slug, len(failed_list_pages))
            break
    stats["fetched"] = len(index)

    # 索引不完整 → 漏掉的页里可能有新片，此时推进水位线等于把它们永久跳过。
    # 所以水位线冻结，下次运行重新遍历完整索引（幂等，代价只是多一轮 list）。
    index_complete = not failed_list_pages
    if not index_complete:
        log.warning(
            "[%s] 索引不完整（%d 页失败: %s），本次不推进水位线",
            slug, len(failed_list_pages), ",".join(map(str, failed_list_pages[:10])),
        )

    if not index:
        log.info("[%s] 增量同步: 源站列表为空", slug)
        if not index_complete:
            stats["incomplete"] = True
            stats["error"] = "源站列表页全部抓取失败，水位线保持不变"
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
        if not index_complete:
            stats["incomplete"] = True
            stats["failed_list_pages"] = len(failed_list_pages)
            stats["list_pages_total"] = pagecount
            detail = _failure_detail(page_errors)
            stats["error"] = (
                f"索引页 {len(failed_list_pages)} 个失败"
                + (f"（{detail}）" if detail else "")
                + "，水位线保持不变"
            )
        log.info("[%s] 增量同步: 无更新 (水位线=%s, 在库索引=%d)", slug, watermark, len(index))
        return stats

    # 3) 按 ID 批量拉详情并 UPSERT
    ids = sorted(candidates)
    log.info("[%s] 增量同步: %d 条候选需要拉取详情", slug, len(ids))
    failed_ids: dict = {}      # vod_id -> 该 ID 在索引里的 vod_time（用于给水位线封顶）
    written_times: list = []   # 真正写进库的 vod_time（水位线的唯一依据）
    missing: list = []         # 详情接口没返回的 ID（可能源站已下架）

    def write_payload(payload: dict, chunk_ids: set) -> set:
        """写入一批详情，返回实际写库的 vod_id 集合。"""
        fetch_class_map(payload, class_map)
        rows = []
        for it in payload.get("list") or []:
            row = MacCmsClient.extract_row(it, class_map)
            if row and row["vod_id"] in chunk_ids:
                rows.append(row)
        ins, upd = upsert_batch(slug, rows)
        _note_touched(stats, rows)
        stats["inserted"] += ins
        stats["updated"] += upd
        written_times.extend(r["vod_time"] for r in rows if r.get("vod_time"))
        return {r["vod_id"] for r in rows}

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
            log.error(
                "[%s] 详情批量拉取失败(跳过 %d 条，水位线将封顶在失败点之前): %s",
                slug, len(chunk), e,
            )
            for vid in chunk:
                failed_ids[vid] = index.get(vid, ("",))[0]
            continue
        got = write_payload(payload, set(chunk))
        missing.extend(v for v in chunk if v not in got)

    # 3.1) 详情缺失的 ID 二次补抓一次：源站偶发丢条目时不必等下一轮
    if missing:
        log.info("[%s] %d 条候选详情未返回，二次补抓", slug, len(missing))
        still_missing = []
        for i in range(0, len(missing), 50):
            chunk = missing[i : i + 50]
            try:
                payload = client.fetch_detail_by_ids(src_api, chunk)
            except MacCmsError as e:
                log.error("[%s] 补抓失败(跳过 %d 条): %s", slug, len(chunk), e)
                for vid in chunk:
                    failed_ids[vid] = index.get(vid, ("",))[0]
                continue
            got = write_payload(payload, set(chunk))
            still_missing.extend(v for v in chunk if v not in got)
        missing = still_missing
        if missing:
            # 补抓仍然没有 = 源站详情接口确实没有这条（通常已下架），
            # 不能让它永久卡住水位线，只记录并继续。
            stats["missing"] = len(missing)
            log.warning(
                "[%s] %d 条 ID 在详情接口中不存在（可能已下架），不计入水位线阻塞: %s",
                slug, len(missing), ",".join(map(str, missing[:8])),
            )

    # 4) 推进水位线：只按「真的写进库的最大 vod_time」算
    #
    # 反例（v1.5.2 的 P0-2）：旧代码取 index 里所有 vod_time 的最大值，包括那些
    # 详情拉取失败、根本没入库的记录。水位线一旦越过它们，筛选条件 t > watermark
    # 永远不成立，这些记录再也不会被增量抓到。
    # 现在：有失败 ID 时，把水位线封顶在「所有失败 ID 的最小时间」之前一格，
    # 保证下一轮一定还能选中它们；失败 ID 的时间未知时干脆不动水位线。
    new_wm = watermark
    if not index_complete:
        stats["incomplete"] = True
        stats["failed_list_pages"] = len(failed_list_pages)
        stats["list_pages_total"] = pagecount
        detail = _failure_detail(page_errors)
        stats["error"] = (
            f"索引页 {len(failed_list_pages)} 个失败"
            + (f"（{detail}）" if detail else "")
            + "，水位线保持不变"
        )
    elif failed_ids:
        times = [t for t in failed_ids.values() if t]
        if len(times) < len(failed_ids):
            stats["incomplete"] = True
            stats["error"] = f"{len(failed_ids)} 条详情拉取失败且时间未知，水位线保持不变"
        else:
            floor = min(times)
            safe = [t for t in written_times if t < floor]
            new_wm = max(safe) if safe else watermark
            stats["incomplete"] = True
            stats["error"] = f"{len(failed_ids)} 条详情拉取失败，水位线只推进到 {new_wm or '(未变)'}"
    elif written_times:
        new_wm = max(written_times)

    # 水位线永不倒退（失败 ID 可能是历史漏抓，时间比当前水位线还早）
    if watermark and new_wm < watermark:
        new_wm = watermark

    _mark_synced(slug, new_wm, len(index))
    log.info("[%s] 增量同步完成: %s 新水位线=%s", slug, stats, new_wm)
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
# 源站健康度退避（v1.8.1）
#
# 大面积失败的判定：索引/全量页失败率 >= 50%，或整轮异常（连 pagecount 都没拿到）。
# 失败 → streak+1 并按指数退避（60min → 2h → 4h → ... 封顶 24h），streak 达到
# FAIL_STATUS_DOWN_STREAK 后面板状态显示「离线」。成功 → 全部清零恢复。
# 只影响「调度器什么时候再试」，不影响数据正确性（水位线语义不变）。
# ---------------------------------------------------------------------------
def _is_hard_failure(status: str, stats: dict) -> bool:
    if status == "failed":
        return True
    if status != "incomplete":
        return False
    total = stats.get("list_pages_total") or stats.get("pages_total") or 0
    failed = stats.get("failed_list_pages", stats.get("failed_pages", 0)) or 0
    return total > 0 and failed * 2 >= total


def _apply_health(slug: str, status: str, stats: dict):
    """按本轮结果更新 fail_streak / skip_until / status。在 sync_source 收尾处调用。"""
    if status == "paused":
        return
    try:
        if _is_hard_failure(status, stats):
            with database.write_txn() as conn:
                row = conn.execute(
                    select(sources.c.fail_streak).where(sources.c.slug == slug)
                ).first()
                streak = ((row[0] if row else 0) or 0) + 1
                minutes = min(
                    config.BACKOFF_BASE_MINUTES * (2 ** (streak - 1)),
                    config.BACKOFF_MAX_HOURS * 60,
                )
                skip_until = datetime.now() + timedelta(minutes=minutes)
                values = {"fail_streak": streak, "skip_until": skip_until}
                if streak >= config.FAIL_STATUS_DOWN_STREAK:
                    values["status"] = "down"
                conn.execute(
                    sources.update().where(sources.c.slug == slug).values(**values)
                )
            log.warning(
                "[%s] 源站大面积失败（第 %d 次连续），退避至 %s 后再试",
                slug, streak, skip_until.strftime("%m-%d %H:%M"),
            )
        elif status == "success":
            with database.write_txn() as conn:
                row = conn.execute(
                    select(sources.c.fail_streak, sources.c.status).where(
                        sources.c.slug == slug
                    )
                ).first()
                if not row:
                    return
                streak, cur_status = row
                values = {"fail_streak": 0, "skip_until": None}
                if cur_status == "down":
                    values["status"] = "ok"   # 已恢复在线；下次 refresh 仍会按远端刷新
                if streak or values.get("status") or "skip_until" in values:
                    conn.execute(
                        sources.update().where(sources.c.slug == slug).values(**values)
                    )
                if streak:
                    log.info("[%s] 同步成功，失败退避已解除（此前连续失败 %d 次）", slug, streak)
    except Exception:
        # 健康度记录失败绝不能影响同步结果本身
        log.exception("[%s] 更新源站健康度失败（忽略）", slug)


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
                # 三种终态：完成 / 用户暂停 / 部分完成（有页/有记录没抓到，但断点与水位线都安全）
                if stats.get("paused"):
                    status = "paused"
                elif stats.get("incomplete"):
                    status = "incomplete"
                else:
                    status = "success"
                _finish_log(log_id, status, stats)
                _apply_health(slug, status, stats)
                _kick_related(slug, mode, stats)
                return {"slug": slug, "mode": mode, "status": status, **stats}
            except Exception as e:
                log.exception("[%s] 同步失败", slug)
                _finish_log(log_id, "failed", {"error": str(e)})
                _apply_health(slug, "failed", {"error": str(e)})
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
    跳过：手动暂停的站 + 自动同步已关闭的站 + 处于失败退避期的站（v1.8.1）。
    手动单站触发不受退避限制（用户显式要求时应当照跑）。
    """
    now = datetime.now()
    with database.engine.connect() as conn:
        rows = conn.execute(
            select(sources.c.slug, sources.c.skip_until).where(
                sources.c.enabled == 1,
                sources.c.paused == 0,
                sources.c.auto_sync == 1,
            )
        ).all()
    slugs = [s for s, su in rows if not su or su <= now]
    skipped = [s for s, su in rows if su and su > now]
    if skipped:
        log.info("失败退避：跳过 %d 个源站（%s...）", len(skipped), ",".join(skipped[:5]))
    log.info("开始同步 %d 个源站 (模式=%s，暂停/关闭自动同步的源站已跳过)", len(slugs), force_mode)
    results = []
    with ThreadPoolExecutor(max_workers=config.SOURCE_SYNC_CONCURRENCY) as pool:
        futures = {pool.submit(sync_source, s, force_mode): s for s in slugs}
        for fut in as_completed(futures):
            try:
                results.append(fut.result())
            except Exception as e:  # 理论上 sync_source 内部已兜底
                log.error("源站 %s 同步异常: %s", futures[fut], e)
    ok = sum(1 for r in results if r.get("status") in ("success", "incomplete"))
    partial = sum(1 for r in results if r.get("status") == "incomplete")
    log.info("同步结束: 成功 %d（其中不完整 %d）/ 失败 %d",
             ok, partial, len(results) - ok)
    return results


def _kick_related(slug: str, mode: str, stats: dict):
    """
    同步结束后触发相关推荐重算（后台线程，绝不阻塞/影响同步结果本身）。
      - full   ：数据基本全变了，直接整站重算
      - 增量   ：只重算本轮 touched 的视频（其旧推荐结果里引用它们的条目，
                 引用按 id 存储、卡片读取时实时补齐，无需连带重算）
    """
    if not config.RELATED_ENABLED:
        return
    if mode == "full":
        threading.Thread(target=_related_full_worker, args=(slug,), daemon=True).start()
        return
    touched = stats.get("touched") or []
    if touched:
        threading.Thread(
            target=_related_ids_worker, args=(slug, list(dict.fromkeys(touched))),
            daemon=True,
        ).start()

    # 明星 Tag 回填（v1.9.1）：与推荐重算同节奏 —— 全量整站重建，增量只重算 touched
    if config.STAR_TAG_AUTO_AFTER_SYNC:
        if mode == "full":
            threading.Thread(target=_star_tags_full_worker, args=(slug,), daemon=True).start()
        elif touched:
            threading.Thread(
                target=_star_tags_ids_worker, args=(slug, list(dict.fromkeys(touched))),
                daemon=True,
            ).start()


def _star_tags_full_worker(slug: str):
    from . import star_tags

    star_tags.rebuild_source_safe(slug, reason="after_full_sync")


def _star_tags_ids_worker(slug: str, ids: list):
    from . import star_tags

    try:
        star_tags.update_ids(slug, ids)
    except Exception:
        log.exception("[%s] 明星 Tag 增量回填失败（忽略）", slug)


def _related_full_worker(slug: str):
    from . import related

    try:
        related.recompute_source_safe(slug, reason="after_full_sync")
    except Exception:
        log.exception("[%s] 相关推荐整站重算失败（忽略）", slug)


def _related_ids_worker(slug: str, ids: list):
    from . import related

    try:
        related.recompute_ids_safe(slug, ids, reason="after_incremental")
    except Exception:
        log.exception("[%s] 相关推荐增量重算失败（忽略）", slug)


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


# ---------------------------------------------------------------------------
# 重启恢复（v1.6.0）
#
# 断电/崩溃后内存态（_running）本来就清空了，但数据库里会留下 status='running' 的
# 日志行 —— 不处理的话面板永远挂着一个「进行中」的任务，用户以为是卡住了。
# 这里在启动时统一改成 interrupted，并把有断点的源站排队续跑。
# ---------------------------------------------------------------------------
def mark_interrupted_logs() -> int:
    """把上次进程遗留的 running 日志标记为 interrupted。返回影响行数。"""
    with database.write_txn() as conn:
        res = conn.execute(
            sync_logs.update()
            .where(sync_logs.c.status == "running")
            .values(
                status="interrupted",
                error="服务重启，任务被中断（断点与水位线均已保留，可自动或手动续跑）",
                finished_at=datetime.now(),
            )
        )
    n = res.rowcount or 0
    if n:
        log.warning("已将 %d 条因重启中断的同步记录标记为 interrupted", n)
    return n


def auto_resume_incomplete() -> list:
    """
    启动时续跑「有断点但没跑完」的全量同步。

    返回待续跑的 slug 列表（空表示没有待续任务或已被配置关闭）。
    续跑走正常入口 sync_source(..., "full")，因此同样受暂停标记/并发闸门约束。
    """
    if not config.AUTO_RESUME_ON_BOOT:
        log.info("自动续跑已关闭（AUTO_RESUME_ON_BOOT=false），未完成任务等下一轮调度")
        return []
    with database.engine.connect() as conn:
        rows = conn.execute(
            select(sources.c.slug, sources.c.last_page, sources.c.total_pages).where(
                sources.c.enabled == 1,
                sources.c.paused == 0,
                sources.c.last_page > 0,
            )
        ).all()
    slugs = [r[0] for r in rows]
    if not slugs:
        return []

    delay = max(0, config.AUTO_RESUME_DELAY_SECONDS)

    def _worker():
        if delay:
            time.sleep(delay)
        if pause.is_paused("full"):
            log.warning("自动续跑跳过：全量同步处于暂停状态（%d 个源站待续）", len(slugs))
            return
        for s in slugs:
            if pause.is_paused("full"):
                log.warning("自动续跑提前结束：全量同步被暂停（剩余 %d 个）", len(slugs) - slugs.index(s))
                return
            try:
                sync_source(s, "full")
            except Exception:
                log.exception("自动续跑 %s 失败", s)
        log.info("自动续跑结束：共处理 %d 个源站", len(slugs))

    threading.Thread(target=_worker, daemon=True, name="auto-resume").start()
    detail = ",".join(f"{r[0]}(页{r[1]}/{r[2] or '?'})" for r in rows[:8])
    log.info("检测到 %d 个未完成的全量同步，%d 秒后自动续跑: %s%s",
             len(slugs), delay, detail, " ..." if len(slugs) > 8 else "")
    return slugs
