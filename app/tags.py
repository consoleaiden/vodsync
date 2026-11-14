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


def sync_source_tags(slug: str, batch: int = 2000, clean: bool = False) -> dict:
    """
    定点修补：把主表有 `vod_tag` 的行同步进倒排表（运维/排障用）。

    与 `rebuild_source(force=True)` 的区别：只扫「有标签」的行（线上约占
    3%），不碰 650 万条无标签视频 —— 全量重扫在线上要跑约 10 小时，定点
    修补是秒级。`clean=True` 时先清掉该站全部倒排行再重建，顺带洗掉「主表
    标签已清空、倒排里还留着」的孤儿行；整个过程单事务原子，中途失败回滚。

    典型场景：v1.14.1 之前 `star_tags` 夜间回填只写主表、不同步倒排，升级后
    历史数据需要一次性补齐（`scripts/sync_star_tags.py`）。
    """
    table = build_vod_table(slug)
    if not database.table_exists(table.name):
        return {"slug": slug, "scanned": 0, "written": 0, "rows": 0,
                "skipped": "no_table"}

    batches, scanned, cursor = [], 0, 0
    with database.engine.connect() as conn:
        while True:
            rows = conn.execute(
                select(table.c.vod_id, table.c.vod_tag)
                .where(table.c.vod_id > cursor,
                       table.c.vod_tag.isnot(None),
                       table.c.vod_tag != "")
                .order_by(table.c.vod_id)
                .limit(batch)
            ).mappings().all()
            if not rows:
                break
            batches.append([dict(r) for r in rows])
            cursor = rows[-1]["vod_id"]
            scanned += len(rows)

    written = 0
    with database.write_txn() as conn:
        if clean:
            conn.execute(video_tags.delete().where(video_tags.c.source == slug))
        for rows in batches:
            written += index_rows(slug, rows, conn=conn)
        actual = conn.execute(
            select(func.count()).where(video_tags.c.source == slug)
        ).scalar() or 0
        # 只更新已有进度行的 indexed（面板「已写行」列）。绝不新插一行：
        # 默认 status=pending 会让 ready() 撤回 tag_search 能力声明。
        conn.execute(
            tag_index_state.update()
            .where(tag_index_state.c.slug == slug)
            .values(indexed=int(actual))
        )
    invalidate_ready_cache()
    return {"slug": slug, "scanned": scanned, "written": written,
            "rows": int(actual), "clean": clean}


def sync_all_sources(slug: str = "", clean: bool = False) -> dict:
    """
    对所有启用源站跑定点修补（`sync_source_tags`），返回汇总。

    生产镜像只 COPY 了 app/，运维在容器里一行即可调用：
      docker exec vodsync python -c "from app import tags; print(tags.sync_all_sources(clean=True))"
    单站失败不影响其它站；`detail` 里每站的 scanned/written/rows 可直接
    用来核对「主表有标签视频数」与「倒排表行数」是否对齐。
    """
    with database.engine.connect() as conn:
        q = select(sources.c.slug).where(sources.c.enabled == 1).order_by(sources.c.id)
        if slug:
            q = select(sources.c.slug).where(sources.c.slug == slug)
        slugs = [r[0] for r in conn.execute(q).all()]
    detail, failed = [], []
    for s in slugs:
        try:
            detail.append(sync_source_tags(s, clean=clean))
        except Exception as e:  # noqa: BLE001
            log.exception("[%s] 标签倒排定点修补失败", s)
            failed.append({"slug": s, "error": str(e)[:200]})
    return {
        "stations": len(slugs),
        "scanned": sum(d.get("scanned", 0) for d in detail),
        "written": sum(d.get("written", 0) for d in detail),
        "rows": sum(d.get("rows", 0) for d in detail),
        "failed": failed,
        "detail": detail,
    }


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
        # 表还不存在：记 no_table 而不是 done。
        # 曾写 done —— 若回填先于源站建表跑（新源站刚登记、表还没建出来），
        # 该站会被永久标成「已完成」；之后表建出来、数据同步进来，一旦增量
        # 维护失败（savepoint 吞掉，可稍后补回），state 仍是 done，
        # ready() 恒 true → /healthz 声明 tag_search 但这个站实际搜不到。
        # 改记 no_table：面板可见，且不满足就绪判定；表建出来后启动检查/
        # 手动回填会真正处理它。
        with database.write_txn() as conn:
            _state_upsert(conn, slug, cursor=0, processed=0, indexed=0, total=0,
                          status="no_table", error="",
                          started_at=None, finished_at=datetime.now())
        return {"slug": slug, "status": "no_table", "indexed": 0, "total": 0,
                "note": "no_table"}

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
            # 存量自愈（v1.14.1）：done 且 processed==0 且表里有数据 =「没真正跑过」
            # 的铁证 —— 真回填完成的行 processed 必然 == total > 0（processed 是
            # v1.14.1 才加的列，v1.14.0 的 done 行一律 0；no_table 误标的行也是 0）。
            # 这类行在旧版启动时被 ready() 的「全 done」判定挡住，永远不会被
            # 重新处理 —— 线上实况：41 个站全部 done 但 processed=0，索引仅
            # 3.3% 覆盖。满足条件就从头重扫（幂等，先删后插按 vod_id 覆盖）。
            force_redo = (
                st is not None and st["status"] == "done"
                and int(processed) == 0 and int(total) > 0
            )
            if force_redo:
                cursor, processed, indexed = 0, 0, 0
                started_at = None
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


def needs_repair() -> bool:
    """
    是否存在「假 done」行：status=done 但 processed==0 且表里有数据。

    v1.14.0 的 done 行没有 processed 列（迁移补列后恒 0），no_table 误标的行
    也是 0 —— 真正回填完成的行 processed == total > 0。这个判定让启动引导
    在 ready()==True 时也能触发一次自愈回填（rebuild_source 见 force_redo）。
    结果缓存 30 秒，与 ready() 同频。
    """
    now = time.time()
    with _ready_lock:
        key = "__repair__"
        if _ready_cache.get(key) and _ready_cache[key]["at"] > now:
            return _ready_cache[key]["val"]
    try:
        with database.engine.connect() as conn:
            rows = conn.execute(
                select(tag_index_state.c.slug).where(
                    tag_index_state.c.status == "done",
                    tag_index_state.c.processed == 0,
                    tag_index_state.c.total > 0,
                ).limit(1)
            ).all()
        val = bool(rows)
    except Exception:  # noqa: BLE001
        val = False
    with _ready_lock:
        _ready_cache[key] = {"at": now + 30, "val": val}
    return val


# ---------------------------------------------------------------------------
# 就绪判定（能力声明用）
# ---------------------------------------------------------------------------
def ensure_state_rows() -> dict:
    """
    v1.16.2 / R5：给「启用 + 有 vod 表 + 缺 tag_index_state 行」的源站补状态行。

    现象：面板手动新增源站后会同步入库，upsert_batch 的写时维护已经把
    video_tags 倒排建好了，但 nobody 给 tag_index_state 插那一行 ——
    ready() 的 all(s in done) 恒 false，/healthz 不声明 tag_search，
    App 的标签检索整体回退成标题全文搜索。线上 65 个启用站 / 57 行 state，
    缺的正是后来手动新增的 8 个站（倒排各有 205~169456 行，数据其实是齐的）。

    只做 count，不扫大表、不重建倒排：有倒排数据的站直接标 done，
    没有的标 pending 交给启动自愈/手动端点回填。
    """
    try:
        with database.engine.connect() as conn:
            enabled = [r[0] for r in conn.execute(
                select(sources.c.slug).where(sources.c.enabled == 1)
            ).all()]
            have = {r[0] for r in conn.execute(
                select(tag_index_state.c.slug)
            ).all()}
            existing = database.list_tables("vod_")
            missing = [s for s in enabled
                       if s not in have and build_vod_table(s).name in existing]
            if not missing:
                return {"filled_done": [], "filled_pending": []}
            tag_counts = dict(conn.execute(
                select(video_tags.c.source, func.count())
                .where(video_tags.c.source.in_(missing))
                .group_by(video_tags.c.source)
            ).all())
            totals = {}
            for s in missing:
                try:
                    totals[s] = int(conn.execute(
                        select(func.count()).select_from(build_vod_table(s))
                    ).scalar() or 0)
                except Exception:  # noqa: BLE001
                    totals[s] = 0
        done, pending = [], []
        with database.write_txn() as conn:
            for s in missing:
                n = int(tag_counts.get(s, 0))
                total = totals.get(s, 0)
                ok = n > 0
                conn.execute(
                    database.dialect_insert(tag_index_state).values(
                        slug=s, status="done" if ok else "pending",
                        cursor=0, processed=total if ok else 0,
                        indexed=n, total=total,
                        finished_at=datetime.now() if ok else None,
                    ).on_conflict_do_nothing(index_elements=["slug"])
                )
                (done if ok else pending).append(s)
        invalidate_ready_cache()
        if done or pending:
            log.info("补齐标签索引状态行: done=%d pending=%d", len(done), len(pending))
        return {"filled_done": done, "filled_pending": pending}
    except Exception as e:  # noqa: BLE001
        log.warning("补齐标签索引状态行失败（忽略）: %s", e)
        return {"filled_done": [], "filled_pending": [], "error": str(e)[:200]}


def invalidate_ready_cache() -> None:
    with _ready_lock:
        _ready_cache["at"] = 0.0
        _ready_cache.pop("__repair__", None)   # needs_repair 的缓存一并失效


def ready() -> bool:
    """
    索引是否就绪（可以对外声明 tag_search）。

    判定：所有启用且**已有数据表**的源站都 status=done，且至少写过一行标签。
    缓存 30 秒：/healthz 是客户端连接测试接口，会被高频调用，不能每次都查库。

    v1.16.2 / R5 设计取舍：**状态行是唯一真源**，ready() 保持纯读，绝不在
    这里猜「没状态行但有倒排算不算就绪」。原因是「面板手动新增、倒排已由写时
    维护建好」和「索引还没回填完」在数据形态上完全一样（video_tags 有行、
    tag_index_state 缺行），一旦放宽，v1.14.0 的「全站回填完才声明 tag_search」
    灰度契约就形同虚设。缺行由 ensure_state_rows() 在启动引导集中补齐。
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
        # v1.16.2 / R5：面板手动新增的源站（倒排齐、缺状态行）由
        # ensure_state_rows() 在启动引导补齐，不在这里放宽判据。
        # 试过用「有倒排行就视同就绪」来兜底，结果和 v1.14.0 契约用例
        # 「索引未就绪时不声明 tag_search」直接冲突：两者数据形态完全一样，
        # 无法区分，只会把灰度能力声明变成永远为 true。故回到严格判据。
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
        # 源站名称映射：面板「标签索引」列表展示源站名称而非难读的标识（v1.16.5）
        name_map = {
            r["slug"]: r["name"]
            for r in conn.execute(
                select(sources.c.slug, sources.c.name)
            ).mappings().all()
        }
    return {
        "ready": ready(),
        "enabled": config.TAG_SEARCH_ENABLED,
        "on_sync": config.TAG_INDEX_ON_SYNC,
        "building": building(),
        "rows": int(total_tags),
        "sources": [
            {
                "slug": r["slug"],
                "name": name_map.get(r["slug"], r["slug"]),
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
                # v1.14.1：明确告诉客户端「这个标签词不在任何源站的标签倒排表里」。
                # tag 检索是 vod_tag 的**精确段匹配**，与 wd 的标题全文匹配命中集
                # 天然不同 —— 某些词（如片名里的「夜店」）可能只出现在标题里。
                # tag_found=false 时客户端应回退 wd 检索，别把空结果直接怼给用户。
                "tag_found": False,
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
        "tag_found": True,
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
