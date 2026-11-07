# ---------------------------------------------------------------------------
# 明星 Tag 回填 + 明星作品推荐（v1.9.1）
#
# 数据流：
#   star_terms（词库，面板维护，已录入大量数据）
#       ↓ 编译为单个正则（一次扫描出全部命中）
#   vod_{slug}.vod_tag   缺演员的行：标题命中词库 → 写入明星名（多个逗号分隔）
#   star_map             全量重建：真实演员 ∩ 词库（src=actor）+ 标题命中（src=title）
#       ↓ (slug, term) 索引查询
#   GET /api/v1/star/related   明星相关视频，毫秒级，不打大表
#
# 高性能方案（为什么快）：
#   1. 单正则扫描：词库 N 个名字编译成一个 alternation 正则，一个标题一次
#      扫描出全部命中，复杂度 O(标题长)，与词库大小解耦 —— 对比逐词判断的
#      O(N×标题长) 快 N 倍；词表按长度降序排列，「吴京安」优先于「吴京」。
#      词库到十万级再考虑换 Aho-Corasick 自动机（pyahocorasick），当前规模
#      单正则已足够（万级词库编译一次 ~10ms，扫描每标题 ~µs 级）。
#   2. 数据库侧过滤下推 + 流式 keyset 分页（vod_id > last_id）：每批只取
#      STAR_TAG_BATCH_SIZE 行，内存恒定，不做 OFFSET 深分页（越翻越慢）。
#   3. 真实演员行不跑正则：演员串切分归一化后与词库集合做交集，O(演员数)。
#   4. 批量写库：vod_tag 用 executemany 批量 UPDATE，star_map 批量插入。
#   5. 倒排映射表：API 查询命中 idx_star_map_term 索引，代价 O(该明星的视频数)，
#      而不是对 220 万行 LIKE '%吴京%' 全表扫（那要几百 ms 且不可索引）。
#   6. 重建是全量幂等的（先删 star_map 再批量插）：词库增删后历史未命中行
#      会被重新评估 —— 这是有意设计，不是浪费。
# ---------------------------------------------------------------------------
import logging
import time
from datetime import datetime

from sqlalchemy import bindparam, func, select
from sqlalchemy import and_, or_

from . import config, database
from .models import build_vod_table, star_map, star_terms
from .related import _star_norm, star_pattern

log = logging.getLogger("vodsync.star_tags")


def _library_set() -> frozenset:
    """启用词库的归一化名字集合（真实演员行做交集用）。"""
    with database.engine.connect() as conn:
        rows = conn.execute(
            select(star_terms.c.term).where(star_terms.c.enabled == 1)
        ).all()
    return frozenset(
        t for t in (_star_norm(r[0]).strip() for r in rows) if len(t) >= 2
    )


def _actors_of(actor_raw: str) -> set:
    """演员串切分 + 归一化（中英文逗号统一）。"""
    out = set()
    for x in (actor_raw or "").replace("，", ",").split(","):
        t = _star_norm(x).strip()
        if t:
            out.add(t)
    return out


def _hits_for_row(name_raw: str, actor_raw: str, pattern, library: frozenset):
    """
    返回 (明星名集合, 来源)：
      真实演员行 → 演员 ∩ 词库（不跑正则，快）
      缺演员行   → 标题单正则扫描
    """
    if (actor_raw or "").strip():
        return _actors_of(actor_raw) & library, "actor"
    if pattern is None:
        return set(), "title"
    return set(pattern.findall(_star_norm(name_raw or ""))), "title"


def _flush_tag_updates(table, upd_rows: list):
    """批量回写 vod_tag（executemany，一次事务一批）。"""
    with database.write_txn() as conn:
        conn.execute(
            table.update().where(table.c.vod_id == bindparam("vid")),
            [{"vid": r["vid"], "vod_tag": r["tag"]} for r in upd_rows],
        )


def _flush_map_insert(rows: list):
    """批量插入 star_map（主键冲突跳过 —— 同批内已去重，防御并发重建）。"""
    stmt = database.dialect_insert(star_map)
    stmt = stmt.on_conflict_do_nothing(index_elements=["slug", "vod_id", "term"])
    with database.write_txn() as conn:
        conn.execute(stmt, rows)


def _scan_and_collect(slug: str, pattern, library: frozenset) -> dict:
    """
    流式扫描 vod_{slug} 全表（keyset 分页），产出：
      tag_rows  待回写 vod_tag 的行（只对缺演员行回填，真实演员 tag 保留源站原值）
      map_rows  待插入 star_map 的行（真实演员 + 标题命中都进映射，API 才有完整作品列表）
    """
    table = build_vod_table(slug)
    tag_rows, map_rows = [], []
    scanned = 0
    last_id = -1
    now = datetime.now()
    while True:
        with database.engine.connect() as conn:
            rows = conn.execute(
                select(
                    table.c.vod_id, table.c.name, table.c.vod_actor, table.c.vod_tag,
                )
                .where(table.c.vod_id > last_id)
                .order_by(table.c.vod_id)
                .limit(config.STAR_TAG_BATCH_SIZE)
            ).mappings().all()
        if not rows:
            break
        last_id = rows[-1]["vod_id"]
        scanned += len(rows)

        batch_tag, batch_map = [], []
        for r in rows:
            hits, src = _hits_for_row(r["name"], r["vod_actor"], pattern, library)
            if not hits:
                continue
            hits = sorted(hits)[: config.STAR_TAG_MAX_PER_VIDEO]
            for h in hits:
                batch_map.append(
                    {"slug": slug, "vod_id": r["vod_id"], "term": h,
                     "src": src, "computed_at": now}
                )
            if src == "title":
                new_tag = ",".join(hits)
                if (r["vod_tag"] or "") != new_tag:
                    batch_tag.append({"vid": r["vod_id"], "tag": new_tag})
        if batch_tag:
            _flush_tag_updates(table, batch_tag)
            tag_rows.extend(batch_tag)
        if batch_map:
            _flush_map_insert(batch_map)
            map_rows.extend(batch_map)
    return {"scanned": scanned, "tagged": len(tag_rows), "mapped": len(map_rows)}


def rebuild_source(slug: str, reason: str = "manual") -> dict:
    """
    全量重建单源站：vod_tag 回填 + star_map 重建。幂等，可重复执行。
    词库为空时跳过（保留旧映射不动 —— 词库被清空大概率是误操作）。
    """
    started = time.time()
    pattern = star_pattern()
    library = _library_set()
    if pattern is None or not library:
        return {"slug": slug, "skipped": "star_terms_empty", "reason": reason}
    table = build_vod_table(slug)
    if not database.table_exists(table.name):
        return {"slug": slug, "skipped": "no_table", "reason": reason}

    # 全量重建：先清该源旧映射（幂等），扫描过程中分批插新映射
    with database.write_txn() as conn:
        conn.execute(star_map.delete().where(star_map.c.slug == slug))

    stats = _scan_and_collect(slug, pattern, library)
    elapsed = round(time.time() - started, 1)
    log.info(
        "[明星Tag] %s 重建完成(%s): 扫描 %d 行, 回填 tag %d 行, 映射 %d 条, 耗时 %ss",
        slug, reason, stats["scanned"], stats["tagged"], stats["mapped"], elapsed,
    )
    return {"slug": slug, "reason": reason, "elapsed_s": elapsed, **stats}


def update_ids(slug: str, ids: list) -> dict:
    """
    增量回填：只重算指定 vod_id（增量同步的 touched ids）。
    先删这些 id 的旧映射，再按当前数据重算插入 + 回写 tag。
    """
    ids = list(dict.fromkeys(int(i) for i in ids if i))[: config.RELATED_TOUCHED_CAP]
    if not ids:
        return {"updated": 0}
    pattern = star_pattern()
    library = _library_set()
    if pattern is None or not library:
        return {"updated": 0}
    table = build_vod_table(slug)
    if not database.table_exists(table.name):
        return {"updated": 0}

    now = datetime.now()
    tag_rows, map_rows = [], []
    with database.engine.connect() as conn:
        rows = conn.execute(
            select(table.c.vod_id, table.c.name, table.c.vod_actor, table.c.vod_tag)
            .where(table.c.vod_id.in_(ids))
        ).mappings().all()
    for r in rows:
        hits, src = _hits_for_row(r["name"], r["vod_actor"], pattern, library)
        hits = sorted(hits)[: config.STAR_TAG_MAX_PER_VIDEO]
        if src == "title":
            new_tag = ",".join(hits)
            if (r["vod_tag"] or "") != new_tag:
                tag_rows.append({"vid": r["vod_id"], "tag": new_tag})
        for h in hits:
            map_rows.append(
                {"slug": slug, "vod_id": r["vod_id"], "term": h,
                 "src": src, "computed_at": now}
            )
    if map_rows or tag_rows:
        # 无论这次有没有命中，先把传入 ids 的旧映射清掉：
        # 视频改名/演员更新后可能不再命中任何词库项，不清会残留过期映射
        with database.write_txn() as conn:
            conn.execute(
                star_map.delete().where(
                    and_(star_map.c.slug == slug, star_map.c.vod_id.in_(ids))
                )
            )
        if tag_rows:
            _flush_tag_updates(table, tag_rows)
        if map_rows:
            _flush_map_insert(map_rows)
    return {"updated": len(tag_rows) + len(map_rows)}


# ---------------------------------------------------------------------------
# 明星作品推荐查询（API 用）
# ---------------------------------------------------------------------------
def resolve_target(table, vod_id: int, name: str):
    """按 id 或名称定位目标视频；名称先精确后模糊（取最近更新的一条）。"""
    with database.engine.connect() as conn:
        if vod_id:
            return conn.execute(
                select(table.c.vod_id, table.c.name)
                .where(table.c.vod_id == vod_id)
            ).first()
        nm = (name or "").strip()
        if not nm:
            return None
        row = conn.execute(
            select(table.c.vod_id, table.c.name).where(table.c.name == nm)
        ).first()
        if not row:
            row = conn.execute(
                select(table.c.vod_id, table.c.name)
                .where(table.c.name.like(f"%{nm}%"))
                .order_by(table.c.vod_time.desc(), table.c.vod_id.desc())
                .limit(1)
            ).first()
        return row


def star_related(slug: str, vod_id: int = 0, name: str = "", limit: int = 20):
    """
    明星作品推荐：与目标视频共享明星（star_map 交集）的其他视频。
    返回 None = 目标视频不存在；否则 dict(target/stars/list)。
    """
    table = build_vod_table(slug)
    target = resolve_target(table, vod_id, name)
    if not target:
        return None
    target_id = target[0]

    with database.engine.connect() as conn:
        # 目标明星集：优先 star_map（含历史推断），缺失时从 vod_tag/vod_actor 现算兜底
        terms = [r[0] for r in conn.execute(
            select(star_map.c.term)
            .where(star_map.c.slug == slug, star_map.c.vod_id == target_id)
        ).all()]
        if not terms:
            row = conn.execute(
                select(table.c.vod_actor, table.c.vod_tag)
                .where(table.c.vod_id == target_id)
            ).first()
            if row:
                pat = star_pattern()
                if row[1]:
                    terms = [t for t in row[1].split(",") if t]
                if not terms and pat is not None:
                    terms = sorted(_actors_of(row[0]) & _library_set())
        if not terms:
            return {"target": {"vod_id": target_id, "vod_name": target[1]},
                    "stars": [], "list": []}

        # 候选：共享任一明星的其他视频，按重叠明星数降序（走 idx_star_map_term）
        cand = conn.execute(
            select(star_map.c.vod_id, func.count().label("ov"))
            .where(
                star_map.c.slug == slug,
                star_map.c.term.in_(terms),
                star_map.c.vod_id != target_id,
            )
            .group_by(star_map.c.vod_id)
            .order_by(func.count().desc())
            .limit(limit)
        ).all()
        if not cand:
            return {"target": {"vod_id": target_id, "vod_name": target[1]},
                    "stars": terms, "list": []}

        cand_ids = [r[0] for r in cand]
        ov_map = {r[0]: r[1] for r in cand}
        # 每个候选与本目标重叠的明星名（展示「因为都主演了 X」）
        pairs = conn.execute(
            select(star_map.c.vod_id, star_map.c.term)
            .where(
                star_map.c.slug == slug,
                star_map.c.vod_id.in_(cand_ids),
                star_map.c.term.in_(terms),
            )
        ).all()
        matched = {}
        for vid, term in pairs:
            matched.setdefault(vid, []).append(term)

        # 卡片字段回表（IN 查询，走主键），补 vod_time 用于同分排序
        cards = conn.execute(
            select(
                table.c.vod_id, table.c.name, table.c.pic, table.c.remarks,
                table.c.type_name, table.c.vod_time,
            ).where(table.c.vod_id.in_(cand_ids))
        ).mappings().all()
        info = {r["vod_id"]: r for r in cards}

    out = []
    for vid in cand_ids:
        c = info.get(vid)
        if not c:      # 已下架的条目：映射还在但视频没了 → 跳过
            continue
        out.append({
            "vod_id": vid,
            "vod_name": c["name"],
            "vod_pic": c["pic"],
            "vod_remarks": c["remarks"],
            "type_name": c["type_name"],
            "source": slug,
            "match_stars": sorted(matched.get(vid, [])),
            "overlap": ov_map.get(vid, 0),
            "_t": c["vod_time"],
        })
    # 重叠数优先（降序）；同分按更新时间降序 —— 利用 sort 稳定性先按时间排再按重叠数排
    out.sort(key=lambda x: (x["_t"] or "", x["vod_id"]), reverse=True)
    out.sort(key=lambda x: -x["overlap"])
    for o in out:
        o.pop("_t", None)
    return {"target": {"vod_id": target_id, "vod_name": target[1]},
            "stars": terms, "list": out[:limit]}


def status_all() -> list:
    """各源站明星 tag 覆盖情况（面板/核对用）。"""
    from .models import sources

    out = []
    with database.engine.connect() as conn:
        slugs = [r[0] for r in conn.execute(
            select(sources.c.slug).where(sources.c.enabled == 1)
        ).all()]
        map_counts = dict(conn.execute(
            select(star_map.c.slug, func.count())
            .group_by(star_map.c.slug)
        ).all())
    for slug in slugs:
        table = build_vod_table(slug)
        if not database.table_exists(table.name):
            continue
        with database.engine.connect() as conn:
            videos = conn.execute(
                select(func.count()).select_from(table)
            ).scalar() or 0
            no_actor = conn.execute(
                select(func.count()).select_from(table).where(
                    or_(table.c.vod_actor == "", table.c.vod_actor.is_(None))
                )
            ).scalar() or 0
        out.append({
            "slug": slug,
            "videos": videos,
            "no_actor_videos": no_actor,
            "star_mapped": map_counts.get(slug, 0),
        })
    return out


def rebuild_source_safe(slug: str, reason: str = "manual") -> dict:
    """吞异常版（后台线程用）：失败只记日志，不影响调用方。"""
    try:
        return rebuild_source(slug, reason=reason)
    except Exception:
        log.exception("[明星Tag] %s 重建失败（忽略）", slug)
        return {"slug": slug, "error": True}
