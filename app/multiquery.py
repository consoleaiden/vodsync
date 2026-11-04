"""
多源（多表）列表查询：并行 top-K 归并，替代「UNION ALL + 全量排序」。

为什么原来的写法会慢
--------------------
每个源站一张 vod_{slug} 表。不带 source 查询时，旧实现把所有表 `UNION ALL`
成一个子查询，外面再套 `count(*)` 和 `ORDER BY vod_time DESC ... OFFSET n LIMIT m`：

  - `count(*)` 必须把每张表的全部行都算一遍（本机 52 表 / 220 万行）；
  - `ORDER BY` 作用在 UNION 子查询上，**用不到任何索引**，每次请求都要把
    两百多万行物化后排序，然后丢掉前 offset 行；
  - 于是首页 4~8 秒、翻到第 20 页 10 秒以上，两个请求就把 NAS 的 IO 打满。

现在的做法
----------
每张表各查自己的 top-K（`ORDER BY vod_time DESC, vod_id DESC LIMIT K`，直接走
`idx_{slug}_time` 倒序扫描），然后把 K×表数 行在进程内做多路归并。

**结果是精确的，不是近似**：
全局第 N 行（1-based）必然落在它所属那张表的前 N 行之内 —— 排在它前面的行，
不会因为跨表而变多。所以每表取 top-(offset+limit) 就足以还原任意一页。

代价与边界
----------
- 内存里最多 表数 × K 行，且只取瘦列（不含 content/play_url 大文本）；
- K 封顶 `MERGE_MAX_PER_TABLE`，超过（翻页过深）就由调用方回退到 `union_page()`，
  慢但保证正确，而不是悄悄少给数据；
- 列表接口不需要 content/play_url；只有 `ac=detail` 才需要，且只对**当前页那几十行**
  再补一次查询（两段式），避免把几十万条大文本搬进内存。
"""
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import func, literal, literal_column, select, union_all

from . import config, database
from .models import build_vod_table

log = logging.getLogger("vodsync.multiquery")

# 列表页需要的「瘦列」。content / play_url 是 TEXT 大字段，只在 detail 模式补当前页。
THIN_COLS = ("vod_id", "type_id", "type_name", "name", "pic", "remarks", "vod_time")

# total 缓存：key 是 slugs 元组（筛选条件下的 total 不缓存，参数太分散）
_total_cache: dict = {}
_total_lock = threading.Lock()
_TOTAL_CACHE_MAX_KEYS = 64


def _esc_like(kw: str) -> str:
    """转义 LIKE 通配符，避免用户输入 % / _ 变成全表匹配。"""
    return kw.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _filters(table, wd: str, type_name: str, type_id: int) -> list:
    where = []
    if wd:
        where.append(table.c.name.like(f"%{_esc_like(wd)}%", escape="\\"))
    if type_name:
        where.append(table.c.type_name == type_name)
    if type_id:
        where.append(table.c.type_id == type_id)
    return where


def can_merge(page: int, page_size: int) -> bool:
    """这一页是否在「每表 top-K」能精确覆盖的范围内。"""
    return (page - 1) * page_size + page_size <= max(page_size, config.MERGE_MAX_PER_TABLE)


def reset_total_cache() -> None:
    with _total_lock:
        _total_cache.clear()


# ---------------------------------------------------------------------------
# total
# ---------------------------------------------------------------------------
def _count_one(slug: str, wd: str, type_name: str, type_id: int, existing: set) -> tuple:
    """单表计数，返回 (行数, 是否失败)。"""
    table = build_vod_table(slug)
    if table.name not in existing:
        return 0, False
    try:
        with database.engine.connect() as conn:
            n = conn.execute(
                select(func.count())
                .select_from(table)
                .where(*_filters(table, wd, type_name, type_id))
            ).scalar() or 0
        return int(n), False
    except Exception as e:  # 单表失败不该让整页 500，降级为「total 偏小」
        log.warning("统计 %s 条数失败: %s", slug, e)
        return 0, True


def _total(slugs: list, wd: str, type_name: str, type_id: int, existing: set) -> tuple:
    """并行统计总行数，返回 (total, partial, from_cache)。"""
    cacheable = config.TOTAL_CACHE_SECONDS > 0 and not wd and not type_name and not type_id
    key = tuple(slugs)
    if cacheable:
        with _total_lock:
            hit = _total_cache.get(key)
        if hit and hit[0] > time.time():
            return hit[1], hit[2], True

    total, partial = 0, False
    workers = max(1, min(config.MERGE_CONCURRENCY, len(slugs)))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="count") as pool:
        for n, bad in pool.map(
            lambda s: _count_one(s, wd, type_name, type_id, existing), slugs
        ):
            total += n
            partial = partial or bad

    if cacheable:
        with _total_lock:
            if len(_total_cache) > _TOTAL_CACHE_MAX_KEYS:
                _total_cache.clear()
            _total_cache[key] = (time.time() + config.TOTAL_CACHE_SECONDS, total, partial)
    return total, partial, False


# ---------------------------------------------------------------------------
# 单表 top-K
# ---------------------------------------------------------------------------
def _page_one(slug: str, k: int, wd: str, type_name: str, type_id: int, existing: set) -> tuple:
    """取单表前 k 行（瘦列），返回 (slug, rows_dict, error)。"""
    table = build_vod_table(slug)
    if table.name not in existing:
        return slug, [], None
    try:
        cols = [table.c[c] for c in THIN_COLS]
        stmt = (
            select(*cols)
            .where(*_filters(table, wd, type_name, type_id))
            .order_by(table.c.vod_time.desc(), table.c.vod_id.desc())
            .limit(k)
        )
        with database.engine.connect() as conn:
            rows = [dict(r) for r in conn.execute(stmt).mappings().all()]
        return slug, rows, None
    except Exception as e:
        log.warning("多源归并 [%s] 查询失败: %s", slug, e)
        return slug, [], str(e)


def _attach_detail(items: list) -> None:
    """给当前页补齐 content / play_url（按源站分组 IN 查询，只查这几十行）。"""
    by_slug: dict = {}
    for r in items:
        by_slug.setdefault(r["source_slug"], []).append(r["vod_id"])
    for slug, ids in by_slug.items():
        table = build_vod_table(slug)
        try:
            with database.engine.connect() as conn:
                found = {
                    x["vod_id"]: x
                    for x in conn.execute(
                        select(table.c.vod_id, table.c.content, table.c.play_url)
                        .where(table.c.vod_id.in_(ids))
                    ).mappings().all()
                }
        except Exception as e:
            log.warning("[%s] 补详情失败（列表仍可用）: %s", slug, e)
            found = {}
        for r in items:
            if r["source_slug"] == slug:
                d = found.get(r["vod_id"])
                r["content"] = (d["content"] if d else "") or ""
                r["play_url"] = (d["play_url"] if d else "") or ""


# ---------------------------------------------------------------------------
# 主入口：并行归并
# ---------------------------------------------------------------------------
def merged_page(
    slugs: list,
    page: int,
    page_size: int,
    *,
    wd: str = "",
    type_name: str = "",
    type_id: int = 0,
    detail: bool = False,
) -> dict:
    """
    返回 {total, pagecount, rows, partial, failed, strategy, elapsed_ms}。

    `rows` 每项是 dict，含 THIN_COLS 全部字段 + `source_slug`；detail=True 时另含
    `content` / `play_url`。
    """
    started = time.time()
    offset = (page - 1) * page_size
    k = offset + page_size

    existing = database.list_tables("vod_")
    live = [s for s in slugs if build_vod_table(s).name in existing]
    total, partial, from_cache = _total(slugs, wd, type_name, type_id, existing)
    pagecount = (total + page_size - 1) // page_size

    collected: list = []
    failed: list = []
    workers = max(1, min(config.MERGE_CONCURRENCY, len(slugs)))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="merge") as pool:
        futures = [
            pool.submit(_page_one, s, k, wd, type_name, type_id, existing) for s in slugs
        ]
        for fut in futures:  # 顺序取结果即可，反正最后要整体排序
            slug, rows, err = fut.result()
            if err:
                failed.append({"slug": slug, "error": err})
                continue
            for r in rows:
                r["source_slug"] = slug
                collected.append(r)

    # 与 SQL 的 ORDER BY vod_time DESC, vod_id DESC 完全同口径：
    # vod_time 在入库时已归一为字符串（空串代表无时间），字符串比较即可，
    # 且格式固定为 "YYYY-MM-DD HH:MM:SS"，字典序 == 时间序。
    collected.sort(key=lambda r: (r["vod_time"] or "", r["vod_id"] or 0), reverse=True)
    items = collected[offset : offset + page_size]

    if detail and items:
        _attach_detail(items)

    return {
        "total": total,
        "pagecount": pagecount,
        "rows": items,
        "partial": bool(partial or failed),
        "failed": failed,
        "strategy": "merge",
        "total_cached": from_cache,
        "tables": len(live),
        "elapsed_ms": int((time.time() - started) * 1000),
    }


# ---------------------------------------------------------------------------
# 回退路径：翻页深到超出 MERGE_MAX_PER_TABLE 时用
#
# 这条 SQL 就是 v1.6.x 的原实现，保留是为了「宁可慢，也不能悄悄少给数据」。
# 正常无限滚动的 App 翻不到这么深（默认 3000 / 30 = 第 100 页）。
# ---------------------------------------------------------------------------
def union_page(
    slugs: list,
    page: int,
    page_size: int,
    *,
    wd: str = "",
    type_name: str = "",
    type_id: int = 0,
    detail: bool = False,
) -> dict:
    started = time.time()
    existing = database.list_tables("vod_")

    union_parts = []
    params: dict = {}
    for i, slug in enumerate(slugs):
        table = build_vod_table(slug)
        if table.name not in existing:
            continue
        where = []
        if wd:
            tpl = f"%{_esc_like(wd)}%"
            where.append(table.c.name.like(tpl, escape="\\"))
            params[f"wd_{i}"] = tpl
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
        if detail:
            cols += [table.c.content, table.c.play_url]
        union_parts.append(select(*cols).where(*where))

    if not union_parts:
        return {
            "total": 0, "pagecount": 0, "rows": [], "partial": False, "failed": [],
            "strategy": "union", "total_cached": False, "tables": 0,
            "elapsed_ms": int((time.time() - started) * 1000),
        }

    merged = union_all(*union_parts).subquery()
    with database.engine.connect() as conn:
        total = conn.execute(select(func.count()).select_from(merged), params).scalar() or 0
        pagecount = (total + page_size - 1) // page_size
        rows = conn.execute(
            select(merged)
            .order_by(literal_column("vod_time").desc(), literal_column("vod_id").desc())
            .offset((page - 1) * page_size)
            .limit(page_size),
            params,
        ).mappings().all()

    return {
        "total": total,
        "pagecount": pagecount,
        "rows": [dict(r) for r in rows],
        "partial": False,
        "failed": [],
        "strategy": "union",
        "total_cached": False,
        "tables": len(union_parts),
        "elapsed_ms": int((time.time() - started) * 1000),
    }
