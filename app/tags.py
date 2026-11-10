"""
标签精确检索（v1.14.0 / B-01）。

问题
----
详情页的标签胶囊（"悬疑"）点进去，客户端要的是「带这个标签的影片」。
旧实现只能把标签当全文关键词走 `name LIKE '%悬疑%'`，命中集完全不同：
搜不到「片名不含悬疑但标签是悬疑」的片，又会混进「悬疑喜剧」「反悬疑」。

方案
----
1. 归一化：按 `, ， / | 、` 切开 vod_tag，逐段去首尾空白、压缩内部空白、
   ASCII 转小写（CJK 原样，不做子串包含）。
2. 落表：`video_tags(source, vod_id, tag_norm)`，主键去重，`(tag_norm, source)`
   索引支撑「某标签的影片」。
3. 增量：同步入库的同一事务里顺手维护（syncer.upsert_batch 调用 index_rows）。
4. 存量：`rebuild_source()` 按 vod_id keyset 分页回填，游标存 tag_index_state，
   可断点续跑、可观测（indexed / total / cursor），失败不阻塞正常同步。
5. 灰度：所有启用源站回填完才在 `/healthz.capabilities` 里声明 `tag_search`
   —— 客户端探到才切精确检索，没探到继续用 wd，两边都不需要回滚。

为什么不做中文分词：标签本身就是短词，用户维护的 `vod_tag` 已经是「悬疑,犯罪」
这种人工分隔的形态；再上一层分词（jieba 之类）只会引入词典依赖和不可解释的
切分结果。按分隔符切 + 精确匹配，语义与界面完全一致。
"""
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from sqlalchemy import func, literal, select, union_all

from . import config, database
from .models import build_vod_table, sources, tag_index_state, video_tags

log = logging.getLogger("vodsync.tags")

# 分隔符：半角/全角逗号、斜杠、竖线、顿号（源站写法五花八门）
_SPLIT_RE = re.compile(r"[,，/|｜、;；]+")
_SPACE_RE = re.compile(r"\s+")

# 单个标签最长 64 字符（与列宽一致）：超长的是脏数据，截断而不是报错
MAX_TAG_LEN = 64

_ready_cache = {"at": 0.0, "val": False}
_ready_lock = threading.Lock()

_build_lock = threading.Lock()
_building: set = set()      # 正在回填的 slug（防重复触发）


# ---------------------------------------------------------------------------
# 归一化
# ---------------------------------------------------------------------------
def normalize(tag: str) -> str:
    """标签归一化：压空白 + ASCII 小写。空/超长返回空串。"""
    t = _SPACE_RE.sub(" ", (tag or "").strip())
    if not t:
        return ""
    # 只对 ASCII 做大小写折叠（"Sci-Fi" -> "sci-fi"）；CJK 无大小写，原样保留
    t = "".join(ch.lower() if ch.isascii() else ch for ch in t)
    return t[:MAX_TAG_LEN]


def split_tags(raw: str) -> list:
    """
    把 vod_tag 原始串切成归一化标签列表（保序去重）。

    去重很关键：同一部片 `"悬疑,悬疑"` 或 `"动作/动作 "` 只留一行，
    否则主键冲突且 count 会虚高。
    """
    out, seen = [], set()
    for part in _SPLIT_RE.split(raw or ""):
        t = normalize(part)
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def tags_of(raw: str) -> list:
    """对外暴露：原始 vod_tag -> 展示用标签列表（保留原大小写）。"""
    out, seen = [], set()
    for part in _SPLIT_RE.split(raw or ""):
        t = _SPACE_RE.sub(" ", (part or "").strip())[:MAX_TAG_LEN]
        n = normalize(t)
        if n and n not in seen:
            seen.add(n)
            out.append(t)
    return out


# ---------------------------------------------------------------------------
# 写入（增量）
# ---------------------------------------------------------------------------
def _delete_ids(conn, slug: str, ids: list) -> None:
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        conn.execute(
            video_tags.delete().where(
                video_tags.c.source == slug, video_tags.c.vod_id.in_(chunk)
            )
        )


def index_rows(slug: str, rows: list, conn=None) -> int:
    """
    为一批视频维护标签行（先删后插，幂等）。

    `rows` 每项需含 vod_id 与 vod_tag。返回写入的 (视频, 标签) 行数。
    传 conn 时复用调用方事务（同步入库路径），否则自己开一个写事务。
    """
    if not rows:
        return 0
    pairs, ids = [], []
    for r in rows:
        vid = r.get("vod_id")
        if vid is None:
            continue
        ids.append(vid)
        for t in split_tags(r.get("vod_tag") or ""):
            pairs.append({"source": slug, "vod_id": vid, "tag_norm": t})
    # 即使一个标签都没有也要删旧行：源站把 tag 清空了，倒排表必须跟着清
    def _run(c):
        if ids:
            _delete_ids(c, slug, ids)
        if pairs:
            for i in range(0, len(pairs), 1000):
                stmt = database.dialect_insert(video_tags).values(pairs[i:i + 1000])
                c.execute(stmt.on_conflict_do_nothing(
                    index_elements=["source", "vod_id", "tag_norm"]
                ))
    if conn is not None:
        _run(conn)
    else:
        with database.write_txn() as c:
            _run(c)
    return len(pairs)


def delete_source_rows(slug: str) -> None:
    """删源站时清掉它的标签行（表不存在/库不可用时只记日志）。"""
    try:
        with database.write_txn() as conn:
            conn.execute(video_tags.delete().where(video_tags.c.source == slug))
        with database.write_txn() as conn:
            conn.execute(tag_index_state.delete().where(tag_index_state.c.slug == slug))
    except Exception as e:  # noqa: BLE001
        log.warning("[%s] 清理标签索引失败（忽略）: %s", slug, e)


# ---------------------------------------------------------------------------
# 全量回填（可断点续跑）
# ---------------------------------------------------------------------------
def _state_upsert(conn, slug: str, **vals) -> None:
    now = datetime.now()
    stmt = database.dialect_insert(tag_index_state).values(
        slug=slug, updated_at=now, **vals
    )
    set_ = {k: v for k, v in vals.items() if k != "slug"}
    set_["updated_at"] = now
    conn.execute(stmt.on_conflict_do_update(index_elements=["slug"], set_=set_))


def _get_state(conn, slug: str):
    return conn.execute(
        select(tag_index_state).where(tag_index_state.c.slug == slug)
    ).mappings().first()


def rebuild_source(slug: str, force: bool = False) -> dict:
    """
    为单个源站重建标签索引。

    - 默认断点续跑：从 tag_index_state.cursor 之后继续；
    - force=True：清空该源站全部标签行并从 0 开始；
    - 每批写完立即提交（游标与行数同事务），进程被杀也只丢最后一批。
    """
    table = build_vod_table(slug)
    if not database.table_exists(table.name):
        with database.write_txn() as conn:
            _state_upsert(conn, slug, cursor=0, processed=0, indexed=0, total=0,
                          status="done", error="", started_at=None,
                          finished_at=datetime.now())
        return {"slug": slug, "status": "done", "indexed": 0, "total": 0, "note": "no_table"}

    with database.write_txn() as conn:
        total = conn.execute(select(func.count()).select_from(table)).scalar() or 0
        if force:
            conn.execute(video_tags.delete().where(video_tags.c.source == slug))
            _state_upsert(conn, slug, cursor=0, processed=0, indexed=0, total=int(total),
                          status="running", error="",
                          started_at=datetime.now(), finished_at=None)
            cursor, processed, indexed = 0, 0, 0
        else:
            st = _get_state(conn, slug)
            cursor = int(st["cursor"] or 0) if st else 0
            indexed = int(st["indexed"] or 0) if st else 0
            processed = int(st["processed"] or 0) if st else 0
            # 存量库刚补上 processed 列时是 0，但 cursor 已经推进过 —— 用一次
            # COUNT(vod_id <= cursor) 把「已处理条数」补回来，否则续跑的第一屏
            # 会从 0% 跳变（主键范围计数，走 PK 索引，代价可接受且只做一次）。
            if processed == 0 and cursor > 0:
                processed = conn.execute(
                    select(func.count()).select_from(table).where(table.c.vod_id <= cursor)
                ).scalar() or 0
            started_at = (st["started_at"] if st and st["started_at"] else None)
            _state_upsert(conn, slug, cursor=cursor, processed=int(processed),
                          indexed=indexed, total=int(total),
                          status="running", error="",
                          started_at=started_at or datetime.now(),
                          finished_at=None)

    batch = max(100, config.TAG_INDEX_BATCH)
    written = 0
    while True:
        with database.engine.connect() as conn:
            rows = conn.execute(
                select(table.c.vod_id, table.c.vod_tag)
                .where(table.c.vod_id > cursor)
                .order_by(table.c.vod_id)
                .limit(batch)
            ).mappings().all()
        if not rows:
            break
        with database.write_txn() as conn:
            n = index_rows(slug, [dict(r) for r in rows], conn=conn)
            cursor = int(rows[-1]["vod_id"])
            processed += len(rows)      # 与 total 同量纲：条数，不是 ID
            indexed += n
            _state_upsert(conn, slug, cursor=cursor, processed=int(processed),
                          indexed=indexed)
        written += n
        if config.TAG_INDEX_SLEEP_MS > 0:
            time.sleep(config.TAG_INDEX_SLEEP_MS / 1000.0)
        if len(rows) < batch:
            break

    with database.write_txn() as conn:
        _state_upsert(conn, slug, status="done",
                      processed=int(min(processed, int(total) or int(processed))),
                      finished_at=datetime.now())
    invalidate_ready_cache()
    log.info("[%s] 标签索引回填完成: 新增 %d 行 / 共 %d 条视频", slug, written, total)
    return {"slug": slug, "status": "done", "indexed": indexed, "total": int(total)}


def rebuild_all(slug: str = "", force: bool = False) -> list:
    """
    回填全部（或指定）源站。串行执行：同步任务才是 IO 的主角，回填让路。

    返回每个源站的结果列表。单个站失败不影响其它站（状态记 failed，可重试）。
    """
    with database.engine.connect() as conn:
        q = select(sources.c.slug).where(sources.c.enabled == 1).order_by(sources.c.id)
        if slug:
            q = select(sources.c.slug).where(sources.c.slug == slug)
        slugs = [r[0] for r in conn.execute(q).all()]
    out = []
    for s in slugs:
        try:
            out.append(rebuild_source(s, force=force))
        except Exception as e:  # noqa: BLE001
            log.exception("[%s] 标签索引回填失败", s)
            try:
                with database.write_txn() as conn:
                    _state_upsert(conn, s, status="failed", error=str(e)[:200],
                                  finished_at=datetime.now())
            except Exception:  # noqa: BLE001
                pass
            out.append({"slug": s, "status": "failed", "error": str(e)[:200]})
    invalidate_ready_cache()
    return out


def start_backfill(slug: str = "", force: bool = False) -> bool:
    """后台启动回填（接口用）。已在跑的 slug 不重复启动。"""
    with _build_lock:
        key = slug or "*"
        if key in _building:
            return False
        _building.add(key)

    def _run():
        try:
            rebuild_all(slug=slug, force=force)
        finally:
            with _build_lock:
                _building.discard(key)

    threading.Thread(target=_run, daemon=True, name="tag-backfill").start()
    return True


def building() -> list:
    with _build_lock:
        return sorted(_building)


# ---------------------------------------------------------------------------
# 就绪判定（能力声明用）
# ---------------------------------------------------------------------------
def invalidate_ready_cache() -> None:
    with _ready_lock:
        _ready_cache["at"] = 0.0


def ready() -> bool:
    """
    索引是否就绪（可以对外声明 tag_search）。

    判定：所有启用且**已有数据表**的源站都 status=done，且至少写过一行标签。
    缓存 30 秒：/healthz 是客户端连接测试接口，会被高频调用，不能每次都查库。
    """
    if not config.TAG_SEARCH_ENABLED:
        return False
    if config.TAG_SEARCH_FORCE:
        return True
    now = time.time()
    with _ready_lock:
        if _ready_cache["at"] > now:
            return _ready_cache["val"]
    try:
        with database.engine.connect() as conn:
            enabled = [r[0] for r in conn.execute(
                select(sources.c.slug).where(sources.c.enabled == 1)
            ).all()]
            done = {
                r[0] for r in conn.execute(
                    select(tag_index_state.c.slug).where(tag_index_state.c.status == "done")
                ).all()
            }
            rows = conn.execute(select(func.count()).select_from(video_tags)).scalar() or 0
        existing = database.list_tables("vod_")
        need = [s for s in enabled if build_vod_table(s).name in existing]
        val = bool(need) and bool(rows) and all(s in done for s in need)
    except Exception as e:  # noqa: BLE001
        log.debug("标签索就绪判定失败（按未就绪处理）: %s", e)
        val = False
    with _ready_lock:
        _ready_cache["at"] = now + 30
        _ready_cache["val"] = val
    return val


def _progress(status: str, processed, total) -> float:
    """
    回填进度（0–100）。

    **只认 processed / total**：两者都是「条数」。绝不拿 cursor（vod_id 水位）当分子
    —— 上游 vod_id 稀疏且可能从几万起，`cursor/total` 会算出 147%、172% 这种
    不存在的进度（v1.14.1 修的就是这个）。

    三重保险：完成态恒 100；无分母给 0；最后再夹到 [0,100]（并发同步一边跑一边
    插新片时 total 是开跑时的快照，processed 可能短暂超过它）。
    """
    t = int(total or 0)
    if status == "done":
        return 100.0 if t else 0.0
    if t <= 0:
        return 0.0
    p = int(processed or 0) * 100.0 / t
    return round(max(0.0, min(100.0, p)), 1)


def status() -> dict:
    """索引状态（面板/维护接口展示：进度、游标、就绪与否）。"""
    with database.engine.connect() as conn:
        rows = conn.execute(
            select(tag_index_state).order_by(tag_index_state.c.slug)
        ).mappings().all()
        total_tags = conn.execute(select(func.count()).select_from(video_tags)).scalar() or 0
    return {
        "ready": ready(),
        "enabled": config.TAG_SEARCH_ENABLED,
        "on_sync": config.TAG_INDEX_ON_SYNC,
        "building": building(),
        "rows": int(total_tags),
        "sources": [
            {
                "slug": r["slug"],
                "status": r["status"],
                "cursor": r["cursor"],
                # 已处理条数（与 total 同量纲）；存量库迁移后由续跑逻辑补算
                "processed": r["processed"],
                "total": r["total"],
                "indexed": r["indexed"],
                "progress": _progress(r["status"], r["processed"], r["total"]),
                "error": r["error"] or "",
                "finished_at": r["finished_at"].isoformat() if r["finished_at"] else None,
            }
            for r in rows
        ],
    }


# ---------------------------------------------------------------------------
# 检索
# ---------------------------------------------------------------------------
# 与 multiquery.THIN_COLS 保持一致：列表卡片要地区/年份（v1.14.0）
_COLS = ("vod_id", "type_id", "type_name", "name", "pic", "remarks", "vod_time",
         "vod_area", "vod_year")


def _join(table):
    """vod_{slug} JOIN video_tags ON vod_id，且限定在该源站。"""
    return table.join(video_tags, video_tags.c.vod_id == table.c.vod_id)


def _count_by_source(slugs: list, tag_norm: str, type_name: str, existing: set) -> tuple:
    """返回 ({slug: count}, failed_list)。有 type_name 时只能 join 后过滤。"""
    out, failed = {}, []
    if not type_name:
        try:
            with database.engine.connect() as conn:
                rows = conn.execute(
                    select(video_tags.c.source, func.count())
                    .where(video_tags.c.source.in_(slugs), video_tags.c.tag_norm == tag_norm)
                    .group_by(video_tags.c.source)
                ).all()
            # 只统计真实存在数据表的源站，避免历史残留行虚高
            for s, n in rows:
                if build_vod_table(s).name in existing:
                    out[s] = int(n)
            return out, failed
        except Exception as e:  # noqa: BLE001
            log.warning("标签计数失败（回落逐站统计）: %s", e)
    for s in slugs:
        table = build_vod_table(s)
        if table.name not in existing:
            continue
        try:
            with database.engine.connect() as conn:
                n = conn.execute(
                    select(func.count())
                    .select_from(_join(table))
                    .where(video_tags.c.source == s, video_tags.c.tag_norm == tag_norm,
                           *([table.c.type_name == type_name] if type_name else []))
                ).scalar() or 0
            out[s] = int(n)
        except Exception as e:  # noqa: BLE001
            failed.append({"slug": s, "error": str(e)})
    return out, failed


def _page_one(slug: str, tag_norm: str, k: int, type_name: str, existing: set) -> tuple:
    table = build_vod_table(slug)
    if table.name not in existing:
        return slug, [], None
    try:
        stmt = (
            select(*(table.c[c] for c in _COLS))
            .select_from(_join(table))
            .where(video_tags.c.source == slug, video_tags.c.tag_norm == tag_norm,
                   *([table.c.type_name == type_name] if type_name else []))
            .order_by(table.c.vod_time.desc(), table.c.vod_id.desc())
            .limit(k)
        )
        with database.engine.connect() as conn:
            rows = [dict(r) for r in conn.execute(stmt).mappings().all()]
        return slug, rows, None
    except Exception as e:  # noqa: BLE001
        log.warning("[%s] 标签检索失败: %s", slug, e)
        return slug, [], str(e)


def search_page(slugs: list, tag: str, page: int = 1, page_size: int = 18,
                type_name: str = "") -> dict:
    """
    按标签精确检索，返回与 /api/v1/videos 同构的分页结果。

    {total, pagecount, rows, partial, failed, strategy, elapsed_ms}
    total 是**精确值**（tag 结果集可由索引直接计数，不需要估值）。
    """
    started = time.time()
    tag_norm = normalize(tag)
    if not tag_norm:
        return {"total": 0, "pagecount": 0, "rows": [], "partial": False,
                "failed": [], "strategy": "tag", "elapsed_ms": 0}

    existing = database.list_tables("vod_")
    counts, failed = _count_by_source(slugs, tag_norm, type_name, existing)
    total = sum(counts.values())
    pagecount = (total + page_size - 1) // page_size if total else 0
    offset = (page - 1) * page_size
    if total == 0:
        return {"total": 0, "pagecount": 0, "rows": [], "partial": bool(failed),
                "failed": failed, "strategy": "tag",
                "elapsed_ms": int((time.time() - started) * 1000)}

    k = offset + page_size
    live = [s for s in slugs if counts.get(s)]
    if k <= max(page_size, config.TAG_SEARCH_PER_SOURCE_LIMIT):
        collected = []
        workers = max(1, min(config.MERGE_CONCURRENCY, len(live)))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="tag") as pool:
            for slug, rows, err in pool.map(
                lambda s: _page_one(s, tag_norm, k, type_name, existing), live
            ):
                if err:
                    failed.append({"slug": slug, "error": err})
                    continue
                for r in rows:
                    r["source_slug"] = slug
                    collected.append(r)
        collected.sort(key=lambda r: (r["vod_time"] or "", r["vod_id"] or 0), reverse=True)
        items = collected[offset:offset + page_size]
        strategy = "tag-merge"
    else:
        # 翻页超出每表 top-K 上限：回退 UNION（慢但保证不漏行）
        items = _union_page(live, tag_norm, type_name, offset, page_size)
        strategy = "tag-union"

    return {
        "total": total,
        "pagecount": pagecount,
        "rows": items,
        "partial": bool(failed),
        "failed": failed,
        "strategy": strategy,
        "elapsed_ms": int((time.time() - started) * 1000),
    }


def _union_page(slugs: list, tag_norm: str, type_name: str, offset: int, limit: int) -> list:
    parts = []
    for s in slugs:
        table = build_vod_table(s)
        parts.append(
            select(
                *(table.c[c] for c in _COLS), literal(s).label("source_slug"),
            )
            .select_from(_join(table))
            .where(video_tags.c.source == s, video_tags.c.tag_norm == tag_norm,
                   *([table.c.type_name == type_name] if type_name else []))
        )
    if not parts:
        return []
    merged = union_all(*parts).subquery()
    from sqlalchemy import literal_column

    with database.engine.connect() as conn:
        rows = conn.execute(
            select(merged)
            .order_by(literal_column("vod_time").desc(), literal_column("vod_id").desc())
            .offset(offset).limit(limit)
        ).mappings().all()
    return [dict(r) for r in rows]
