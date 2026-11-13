"""
数据表浏览（v1.12.0，管理面板，只读）。

面板需要一个「随便看看库里有什么」的入口：切表、按字段或全表搜关键词、翻页。
查询逻辑放独立模块而不是塞进 api.py，原因与 playline.py 相同 —— 表/列名不能
参数化，必须先经过白名单校验才能进 SQL，这部分逻辑值得单独写清楚、单独测。

安全边界（重要）：
1. **表名白名单**：只有内置 5 张表 + `vod_{slug}`（且 slug 匹配建表字符集、
   表真实存在）可浏览。表名/列名永远不拼接用户输入 —— 全部来自 SQLAlchemy
   Table 对象的元数据，非法输入在 resolve_table() 就被拒绝（404）。
2. **settings 表有意排除**：里面是密码哈希、API Token、会话签名密钥。
   面板本来就能看 Token（/api/v1/auth/token），但只读浏览不该顺手把哈希和
   密钥也端出来 —— 少一个侧信道就少一条被截图外传的路。
3. **关键词参数化**：值永远走 bind param，只有 LIKE 通配符（% _ \\）被转义，
   不会因为「搜索内容」引入注入面。
4. 接口挂在 /api/v1/admin/* 下，与其他接口一样由 main.py 的 _auth_guard
   统一鉴权（面板未开鉴权时本就无保护，与现有行为一致）。

性能说明（为什么敢对百万级大表这么做）：
- 列表接口的行数对 vod_* 表用 sources.total_videos（recount 维护的缓存值），
  不做实时 count；系统表里的小表实时 count，大表（v1.16.4 起）用
  pg_class.reltuples 估算，避免展开一次下拉就全表扫 6.9M 行的 vod_related。
- 按字段搜索：文本列直接 ILIKE（name 列能吃到 pg_trgm GIN 索引）；数字/时间列
  cast 成字符串再比（cast 后必然顺序扫描 —— 面板场景可接受，不加 hint）。
- 全表搜索（不选字段）：OR 所有列，大表上就是全表扫，页面有文案提示。
  这是管理员排障工具，不是客户端 API，不为它建索引。
"""
import math

from sqlalchemy import String, cast, func, or_, select, text

from . import database
from .apiresp import SUCCESS_CODE
from .models import (
    build_vod_table,
    star_map,
    star_terms,
    sync_logs,
    sources,
    vod_related,
)
from .utils import SAFE_SLUG

# 内置可浏览表（不含动态 vod_{slug}）。顺序即面板下拉顺序。
BUILTIN_TABLES = {
    "sources": sources,
    "sync_logs": sync_logs,
    "star_terms": star_terms,
    "vod_related": vod_related,
    "star_map": star_map,
}

# 关键词长度上限：转义后拼进 LIKE，太长的输入没有意义，纯属给数据库添堵
MAX_KEYWORD_LEN = 200


def resolve_table(name: str):
    """
    表名 -> SQLAlchemy Table 对象；不可浏览返回 None。

    slug 校验用 models 里的 SAFE_SLUG（小写字母数字下划线，与建表规则一致），
    再确认表真实存在 —— 两个条件缺一不可：前者挡 `vod_; DROP TABLE x` 这种
    结构性注入，后者挡 `vod_不存在的slug` 在后续查询里报 500。
    """
    from .utils import SAFE_SLUG

    if name in BUILTIN_TABLES:
        return BUILTIN_TABLES[name]
    if isinstance(name, str) and name.startswith("vod_") and len(name) > 4:
        slug = name[4:]
        if SAFE_SLUG.match(slug) and database.table_exists(name):
            from .models import build_vod_table

            return build_vod_table(slug)
    return None


def _esc_like(kw: str) -> str:
    """转义 LIKE 通配符（\\ % _），与 api._esc_like 同口径。"""
    return kw.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _like_expr(col, pattern: str):
    """
    单列模糊匹配。文本列直接 ilike；非文本列（整数/时间）cast 成 VARCHAR 再比，
    让「按 type_id 搜 1」「按时间搜 2026-11」这类输入也能工作。
    """
    if isinstance(col.type, String):
        return col.ilike(pattern, escape="\\")
    return cast(col, String).ilike(pattern, escape="\\")


def _column_infos(table) -> list:
    """列元信息（面板用列名渲染表头、用类型做 tooltip）。"""
    return [
        {"name": c.name, "type": str(c.type), "pk": bool(c.primary_key)}
        for c in table.columns
    ]


def _system_count(table) -> int:
    """
    系统表行数（面板下拉里的显示辅助值）。

    v1.16.4 性能修复：PG 上对大表改用 `pg_class.reltuples` 统计估算（1~2ms），
    不再每次展开「数据表浏览」都 COUNT(*) 全表扫 6.9M 行的 vod_related ——
    实测那一次扫描冷盘 6.7s / 热盘 1.3s，叠上后台同步抢 IO 时整个接口 48s。

    口径没变：vod_* 表的行数本来就取自 `sources.total_videos` 缓存值，不是实时
    count。小表（估算 < 10 万行）仍走精确 COUNT(*)，SQLite 一律精确 COUNT(*)
    —— 本地测试库都是小表，既有回归断言不受影响。
    """
    if not database.is_sqlite():
        try:
            with database.engine.connect() as conn:
                est = conn.execute(
                    text("SELECT c.reltuples::bigint FROM pg_class c "
                         "JOIN pg_namespace n ON n.oid = c.relnamespace "
                         "WHERE c.relkind = 'r' AND n.nspname = current_schema() "
                         "AND c.relname = :n"),
                    {"n": table.name},
                ).scalar()
            if est is not None and int(est) >= 100_000:
                return max(0, int(est))
        except Exception:  # noqa: BLE001 - 估算拿不到就回落到精确 count
            pass
    try:
        with database.engine.connect() as conn:
            return int(conn.execute(select(func.count()).select_from(table)).scalar() or 0)
    except Exception:  # noqa: BLE001 - 行数只是展示辅助，取不到就显示 0
        return 0


def list_browsable() -> list:
    """
    可浏览表清单（面板下拉）。不查任何一张 vod_* 大表的真实行数 ——
    那些行数用 sources.total_videos（条数校准任务维护的缓存值）。

    vod_{slug} 的 `label` 用源站名称（如「新极品资源站」）而不是 slug：
    面板上看到 vod_s361c088099 与看到「新极品资源站」的信息量完全不同，
    后者才知道自己在看哪个站的数据。表名仍放在 `name` 里（接口契约不变），
    前端只把它放进 option 的 title，鼠标悬停可查。
    """
    existing = database.list_tables()

    # vod_{slug} 的行数与源站名一次性取回（一次查询），避免逐表 count
    vod_meta: dict = {}
    try:
        with database.engine.connect() as conn:
            for slug, total, sname in conn.execute(
                select(sources.c.slug, sources.c.total_videos, sources.c.name)
            ).all():
                vod_meta[slug] = {
                    "rows": int(total or 0),
                    "label": (sname or "").strip(),
                }
    except Exception:  # noqa: BLE001 - sources 表还没建等极端情况，行数显示 0 即可
        pass

    out = []
    for name, tbl in BUILTIN_TABLES.items():
        if name not in existing:
            continue  # 表还没建（如 star_terms 在旧库）就不出现在下拉里
        out.append({
            "name": name,
            "kind": "system",
            "label": name,          # 系统表没有更友好的名字，直接显示表名
            "rows": _system_count(tbl),
            "columns": _column_infos(tbl),
        })

    vod_items = []
    for name in sorted(existing):
        if not name.startswith("vod_") or name == "vod_related":
            continue
        slug = name[4:]
        # v1.16.4：`existing` 本身就是刚从 catalog 取回的真实表名集合，再对
        # 每张表调 resolve_table() -> database.table_exists() 等于 65 次额外的
        # information_schema 往返。表名来自 catalog、不来自用户输入，仍过一遍
        # SAFE_SLUG 保持原有的白名单边界，然后直接用元数据构造 Table。
        if not SAFE_SLUG.match(slug):
            continue
        tbl = build_vod_table(slug)
        meta = vod_meta.get(slug) or {}
        vod_items.append({
            "name": name,
            "kind": "vod",
            # 源站已被删除但表还留着（历史库/手工建表）时不能显示空字符串
            "label": meta.get("label") or f"{slug}（无对应源站）",
            "rows": meta.get("rows", 0),
            "columns": _column_infos(tbl),
        })

    # 同名源站（极少但可能，name 不唯一）在下拉里会变成两条一模一样的选项，
    # 这时退回「名称（表名）」，让管理者仍能区分
    dup = {}
    for it in vod_items:
        dup[it["label"]] = dup.get(it["label"], 0) + 1
    for it in vod_items:
        if dup[it["label"]] > 1:
            it["label"] = f'{it["label"]}（{it["name"]}）'

    # 按显示名排序：源站名比 slug 排序有意义（中文按码点，近似按拼音分组）
    out.extend(sorted(vod_items, key=lambda x: (x["label"], x["name"])))
    return out


def browse(name: str, page: int, page_size: int, field: str = "", q: str = "") -> dict:
    """
    分页浏览一张表。返回 {columns, rows, total, page, page_size, pages}。

    - field 为空 => 全表搜索（OR 所有列）；q 为空 => 不过滤（纯翻页）
    - 排序恒按主键（无主键表用第一列），保证翻页稳定 —— 否则 offset 翻页
      在无序结果集上可能重复/丢行
    - page 超出范围时夹回 [1, pages]，前端「跳转到第 N 页」不用自己算边界
    """
    tbl = resolve_table(name)
    if tbl is None:
        raise KeyError(name)

    cols = list(tbl.columns)
    colnames = [c.name for c in cols]
    if field and field not in colnames:
        raise ValueError(field)

    where = None
    keyword = (q or "").strip()[:MAX_KEYWORD_LEN]
    if keyword:
        pattern = f"%{_esc_like(keyword)}%"
        if field:
            where = _like_expr(tbl.c[field], pattern)
        else:
            where = or_(*(_like_expr(c, pattern) for c in cols))

    order_by = list(tbl.primary_key.columns) or [cols[0]]

    # 注意：不能写 .where(None) —— SQLAlchemy 会把它编译成 `WHERE NULL`
    # （无条件过滤整个表）。where 必须按「有过滤才挂」的方式构建。
    count_stmt = select(func.count()).select_from(tbl)
    rows_stmt = select(tbl).order_by(*order_by)
    if where is not None:
        count_stmt = count_stmt.where(where)
        rows_stmt = rows_stmt.where(where)

    with database.engine.connect() as conn:
        total = int(conn.execute(count_stmt).scalar() or 0)
        pages = max(1, math.ceil(total / page_size))
        page = min(max(1, page), pages)
        rows = conn.execute(
            rows_stmt.offset((page - 1) * page_size).limit(page_size)
        ).mappings().all()

    return {
        # v1.14.0：与其它接口统一的成功码（默认 0；见 config.API_SUCCESS_CODE）
        "code": SUCCESS_CODE,
        "table": name,
        "columns": colnames,
        "rows": [dict(r) for r in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": pages,
    }
