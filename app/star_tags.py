# ---------------------------------------------------------------------------
# 明星 Tag 回填 + 明星作品推荐（v1.9.1，v1.16.2 重构回填链路）
#
# 数据流：
#   star_terms（词库，面板维护，3400+ 条）
#       ↓ 编译为单个正则（一次扫描出全部命中）
#   ① 写时：syncer.upsert_batch → infer_on_ingest → 合并进 vod_tag + video_tags
#   ② 全量：rebuild_source → 标题命中写 vod_tag；标题∪演员命中进 star_map
#       ↓ (slug, term) 索引查询
#   GET /api/v1/star/related   明星相关视频，毫秒级，不打大表
#   GET /api/v1/videos?tag=    标签推荐（video_tags 倒排）
#
# v1.16.2 修的四个真实缺陷（线上乐播站实测反例：33.8 万行、actor 全空，
#   词库有「香草少女」、标题含该词 118 行，vod_tag 却全空）：
#   1. 夜间轮转用 max(star_map.computed_at) 当「上次重建时间」，而每 6h
#      的增量同步 update_ids() 也刷新它 —— 大站永远「刚重建过」，全量重建
#      从未轮到（乐播只有 383 条映射，真跑过的站是 10 万条量级）。
#      → 独立 star_rebuild_state 水位，只有全量重建完成才推进。
#   2. _hits_for_row 旧实现「有演员就只看演员」，标题正则一次都不跑。
#   3. _scan_and_collect 只有 src=="title" 才写 vod_tag，且用推断值整体
#      覆盖源站原生标签；无命中时还会把已有标签清空。
#      → 合并式回写 merge_vod_tag：保留原生段，只替换上次推断段。
#   4. 面板手动新增的源站缺 tag_index_state 行，会让 ready() 恒 false、把
#      tag_search 从 /healthz 摘掉（线上 65 个启用站 / 57 行 state）。
#      → tags.ensure_state_rows() 在启动引导补齐状态行；ready() 保持严格
#        灰度判据（不能放宽，否则「新增站」和「未回填完」无法区分）。
#
# 性能（为什么这套比反复全量重扫划算）：
#   1. 单正则扫描：N 个词编译成一个 alternation，O(标题长) 与词库规模解耦，
#      长词优先（「吴京安」优先于「吴京」）；万级词库编译 ~10ms、每标题 µs 级。
#      到十万级再考虑 Aho-Corasick（pyahocorasick）。
#   2. 写时推断每页几十条，成本可忽略 —— 新片从入库那一刻就有标签，
#      全量重建从「常规路径」降为「一次性存量回填 + 低频校准」。
#   3. keyset 分页（vod_id > last_id）+ 批大小 STAR_TAG_BATCH_SIZE +
#      批间休眠，内存恒定、不与同步抢满 IO。
#   4. 只写「标签发生变化」的行（cur_tag != new_tag），重复重建零写入。
#   5. star_map / video_tags 都是倒排，读侧 O(命中视频数)，不对大表 LIKE。
# ---------------------------------------------------------------------------
import logging
import re
import time
from datetime import datetime

from sqlalchemy import bindparam, func, select
from sqlalchemy import and_, or_

from . import config, database
from .models import build_vod_table, star_map, star_rebuild_state, star_terms
from .related import _star_norm, canon_hits, canon_star, star_alias_map, star_pattern

log = logging.getLogger("vodsync.star_tags")

# vod_tag 的分隔符：与 tags.split_tags 保持一致（源站写法五花八门）
_TAG_SPLIT_RE = re.compile(r"[,，/|｜、;；]+")
# vod_tag 是 VARCHAR(255)，合并时按段累加，绝不切出半个词
_TAG_MAX_CHARS = 255


def _library_set() -> frozenset:
    """启用词库的主名集合（归一化）。真实演员行做交集用。"""
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


def _actor_hits(actor_raw: str, library: frozenset) -> set:
    """
    真实演员名 → 词库主名集合。别名也认（v1.9.2）：
    如「京哥」在别名表里 → 归一为「吴京」。不在词库的演员直接丢弃。
    """
    amap = star_alias_map()
    hits = set()
    for name in _actors_of(actor_raw):
        main = amap.get(name)
        if main and main in library:
            hits.add(main)
    return hits


def _filter_hits(hits: set) -> set:
    """过滤纯 ASCII 的过短词（词库里 er/ss/jk 这类 2 字母词会污染标签）。

    中文词仍按 reload_star_terms() 既有的 len>=2 规则；这里只额外约束 ASCII。
    """
    n = config.STAR_TAG_MIN_ASCII_LEN
    if n <= 0:
        return hits
    return {t for t in hits if not (t.isascii() and len(t) < n)}


def _hits_for_row(name_raw: str, actor_raw: str, pattern, library: frozenset):
    """
    v1.16.2：返回 (写 vod_tag 的集合, 进 star_map 的集合, 来自演员的集合)。

    旧实现是「有演员就只看演员」的二选一 —— 只要 vod_actor 非空，标题正则
    一次都不跑，且 _scan_and_collect 只有 src=="title" 才回写 vod_tag。于是
    演员字段非空（哪怕值并不在词库里）的视频，标题里明明有多个词库词，
    vod_tag 和 star_map 双双为空。

    现在两路都算。口径严格按需求：**只有标题命中写 vod_tag**；演员命中仍
    只进 star_map（保持 v1.9.1 起的既有语义，不改变「真实演员行 tag 保留
    源站原值」的契约）。单正则扫一个标题 µs 级，全库多跑一遍很便宜。
    """
    actor_hits = _filter_hits(
        _actor_hits(actor_raw, library) if (actor_raw or "").strip() else set()
    )
    title_hits = set()
    if pattern is not None:
        title_hits = _filter_hits(
            canon_hits(pattern.findall(_star_norm(name_raw or ""))) & library
        )
    if config.STAR_TAG_UNION_HITS:
        return title_hits, title_hits | actor_hits, actor_hits
    if actor_hits:  # 旧语义（回滚开关，正常不启用）
        return set(), actor_hits, actor_hits
    return title_hits, title_hits, set()


def _tag_key(s: str) -> str:
    """标签段比较键：与词库主名同一套归一化（NFKC/小写/繁转简）。"""
    return _star_norm(s or "").strip()


def merge_vod_tag(cur_tag: str, old_inferred, new_inferred, max_n: int = None) -> str:
    """
    v1.16.2：合并式回写，替代旧的整体覆盖。

      kept = cur_tag 的段 - 本服务上次写进去的推断段（old_inferred）
      out  = kept + 本次命中段（new_inferred，最多 max_n 个）

    这样：① 源站原生标签（如「巨乳」）不会被冲掉；② 词库删词/标题改名后
    过期推断段能精确移除；③ 重复跑幂等（cur_tag 相同就不写库）。
    old_inferred 取自在 star_map 里该视频的旧映射，由调用方批量预取。
    """
    max_n = config.STAR_TAG_MAX_PER_VIDEO if max_n is None else max_n
    old_keys = {_tag_key(t) for t in (old_inferred or ())}
    kept, seen = [], set()
    for part in _TAG_SPLIT_RE.split(cur_tag or ""):
        s = part.strip()
        if not s:
            continue
        k = _tag_key(s)
        if not k or k in old_keys or k in seen:
            continue
        seen.add(k)
        kept.append(s)
    add = []
    for t in sorted(new_inferred or ())[:max_n]:
        k = _tag_key(t)
        if k and k not in seen:
            seen.add(k)
            add.append(t)
    out, used = [], 0
    for s in kept + add:
        cost = len(s) + (1 if out else 0)
        if used + cost > _TAG_MAX_CHARS:
            break
        out.append(s)
        used += cost
    return ",".join(out)


def _old_inferred_map(conn, slug: str, ids: list) -> dict:
    """批量取「本服务上次写进去的推断段」：star_map 里该批视频的全部 term。

    一次 IN 查询（每 500 个 id 一片），避免逐行查库。全量重建时这些旧行
    马上会被删掉，所以必须在删除之前取。
    """
    out: dict = {}
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        if not chunk:
            continue
        for vid, term in conn.execute(
            select(star_map.c.vod_id, star_map.c.term).where(
                star_map.c.slug == slug,
                star_map.c.vod_id.in_(chunk),
                # 只有 src=title 的词是上次写进 vod_tag 的推断段；
                # actor 命中的词从来不写 vod_tag，不能拿来当清理依据。
                star_map.c.src == "title",
            )
        ).all():
            out.setdefault(int(vid), set()).add(term)
    return out


def _flush_tag_updates(slug: str, table, upd_rows: list):
    """批量回写 vod_tag（一次事务一批），并同步标签倒排表。

    v1.14.1 修订：`vod_tag` 有两条写入路径 —— `syncer.upsert_batch` 在同一
    事务里维护 `video_tags`，而这里（明星词库回填）以前只 UPDATE 主表。结果
    夜间补进去的明星标签既搜不到、也不计入索引行数：线上实测两个源站共
    81,290 条视频有标签，`video_tags` 里却是 0 行，且每晚重建会持续制造
    新的不一致。现在同事务顺手维护倒排，用 savepoint 隔离 —— 索引是附加
    能力，它失败绝不能让明星Tag回填整体回滚。
    """
    with database.write_txn() as conn:
        conn.execute(
            table.update().where(table.c.vod_id == bindparam("vid")),
            [{"vid": r["vid"], "vod_tag": r["tag"]} for r in upd_rows],
        )
        if config.TAG_SEARCH_ENABLED and upd_rows:
            from . import tags

            try:
                with conn.begin_nested():
                    tags.index_rows(
                        slug,
                        [{"vod_id": r["vid"], "vod_tag": r["tag"]} for r in upd_rows],
                        conn=conn,
                    )
            except Exception as e:  # noqa: BLE001
                log.warning("[%s] 明星Tag 回填后标签倒排同步失败（不影响主表）: %s",
                            slug, e)


def _flush_map_insert(rows: list):
    """批量插入 star_map（主键冲突跳过 —— 同批内已去重，防御并发重建）。"""
    stmt = database.dialect_insert(star_map)
    stmt = stmt.on_conflict_do_nothing(index_elements=["slug", "vod_id", "term"])
    with database.write_txn() as conn:
        conn.execute(stmt, rows)


def _delete_map_ids(slug: str, ids: list):
    """按批删掉这批视频的旧 star_map 行（幂等重建）。"""
    with database.write_txn() as conn:
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            if chunk:
                conn.execute(star_map.delete().where(
                    star_map.c.slug == slug, star_map.c.vod_id.in_(chunk)
                ))


def _scan_and_collect(slug: str, pattern, library: frozenset) -> dict:
    """
    流式扫描 vod_{slug} 全表（keyset 分页，内存恒定）。

    v1.16.2 三处关键变化：
      1. 所有行都跑标题正则 + 演员交集取并集（旧实现二选一）；
      2. 所有行都回写 vod_tag（旧实现只有 src=="title" 才写）；
      3. 用合并式回写，保留源站原生标签，只替换上一次的推断段。
    每批先读旧映射（拿到上一次的推断段）→ 删旧映射 → 写 tag/倒排 → 插新映射。
    不再一次性 DELETE 整站（大事务 + 拿不到旧推断段），孤儿行由读侧 JOIN 自然过滤。
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
        ids = [r["vod_id"] for r in rows]

        # 必须在删旧映射之前取：这些 term 就是上一次写进 vod_tag 的推断段
        with database.engine.connect() as conn:
            old_map = _old_inferred_map(conn, slug, ids)

        batch_tag, batch_map = [], []
        for r in rows:
            tag_hits, map_hits, actor_hits = _hits_for_row(
                r["name"], r["vod_actor"], pattern, library
            )
            inferred_tag = sorted(tag_hits)[: config.STAR_TAG_MAX_PER_VIDEO]
            inferred_map = sorted(map_hits)[: config.STAR_TAG_MAX_PER_VIDEO]
            cur_tag = r["vod_tag"] or ""
            if config.STAR_TAG_MERGE_TAG:
                new_tag = merge_vod_tag(
                    cur_tag, old_map.get(r["vod_id"], set()), inferred_tag
                )
                write_tag = cur_tag != new_tag
            else:  # 旧语义（回滚开关，正常不启用）
                new_tag = ",".join(inferred_tag)
                write_tag = (cur_tag != new_tag
                             and (bool(new_tag) or config.STAR_TAG_CLEAR_STALE))
            if write_tag:
                batch_tag.append({"vid": r["vod_id"], "tag": new_tag})
            for h in inferred_map:
                batch_map.append(
                    {"slug": slug, "vod_id": r["vod_id"], "term": h,
                     "src": "actor" if h in actor_hits else "title",
                     "computed_at": now}
                )
        _delete_map_ids(slug, ids)
        if batch_tag:
            _flush_tag_updates(slug, table, batch_tag)
            tag_rows.extend(batch_tag)
        if batch_map:
            _flush_map_insert(batch_map)
            map_rows.extend(batch_map)
        if config.STAR_TAG_REBUILD_SLEEP_MS > 0:
            time.sleep(config.STAR_TAG_REBUILD_SLEEP_MS / 1000.0)
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

    # v1.16.2：不再一次性 DELETE 整站。_scan_and_collect 按批「先读旧推断段 →
    # 删该批映射 → 写新值」，既能做合并式回写，也避免几十万行的大事务。
    stats = _scan_and_collect(slug, pattern, library)
    elapsed = round(time.time() - started, 1)
    _mark_full_rebuild(slug, stats, elapsed)
    log.info(
        "[明星Tag] %s 重建完成(%s): 扫描 %d 行, 回填 tag %d 行, 映射 %d 条, 耗时 %ss",
        slug, reason, stats["scanned"], stats["tagged"], stats["mapped"], elapsed,
    )
    return {"slug": slug, "reason": reason, "elapsed_s": elapsed, **stats}


def _mark_full_rebuild(slug: str, stats: dict, elapsed: float):
    """只有全量重建完成才推进水位 —— 夜间轮转据此选站，增量回填绝不碰它。"""
    now = datetime.now()
    vals = {
        "slug": slug,
        "last_full_at": now,
        "rows_scanned": int(stats.get("scanned", 0)),
        "rows_tagged": int(stats.get("tagged", 0)),
        "rows_mapped": int(stats.get("mapped", 0)),
        "elapsed_s": int(elapsed),
    }
    stmt = database.dialect_insert(star_rebuild_state).values(**vals)
    set_ = {k: v for k, v in vals.items() if k != "slug"}
    with database.write_txn() as conn:
        conn.execute(stmt.on_conflict_do_update(index_elements=["slug"], set_=set_))


def update_ids(slug: str, ids: list) -> dict:
    """
    增量回填：只重算指定 vod_id（增量同步的 touched ids）。
    先删这些 id 的旧映射，再按当前数据重算插入 + 回写 tag。
    """
    ids = list(dict.fromkeys(int(i) for i in ids if i))[: config.STAR_TAG_TOUCHED_CAP]
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
        old_map = _old_inferred_map(conn, slug, ids)
    for r in rows:
        tag_hits, map_hits, actor_hits = _hits_for_row(
            r["name"], r["vod_actor"], pattern, library
        )
        inferred_tag = sorted(tag_hits)[: config.STAR_TAG_MAX_PER_VIDEO]
        inferred_map = sorted(map_hits)[: config.STAR_TAG_MAX_PER_VIDEO]
        cur_tag = r["vod_tag"] or ""
        if config.STAR_TAG_MERGE_TAG:
            new_tag = merge_vod_tag(
                cur_tag, old_map.get(r["vod_id"], set()), inferred_tag
            )
            write_tag = cur_tag != new_tag
        else:
            new_tag = ",".join(inferred_tag)
            write_tag = (cur_tag != new_tag
                         and (bool(new_tag) or config.STAR_TAG_CLEAR_STALE))
        if write_tag:
            tag_rows.append({"vid": r["vod_id"], "tag": new_tag})
        for h in inferred_map:
            map_rows.append(
                {"slug": slug, "vod_id": r["vod_id"], "term": h,
                 "src": "actor" if h in actor_hits else "title",
                 "computed_at": now}
            )
    # 无论这次有没有命中，都要先清掉传入 ids 的旧映射：
    # 视频改名/演员更新后可能不再命中任何词库项，不清会残留过期映射。
    _delete_map_ids(slug, ids)
    if tag_rows:
        _flush_tag_updates(slug, table, tag_rows)
    if map_rows:
        _flush_map_insert(map_rows)
    return {"updated": len(tag_rows) + len(map_rows)}


def infer_on_ingest(slug: str, rows: list, existing_tags: dict, conn) -> int:
    """
    v1.16.2 写时推断：同步入库的同一事务里，把标题/演员命中的词库词合并进
    每行的 vod_tag。只改内存里的 rows，不单独写库 —— 主表 vod_tag 与
    video_tags 倒排随后由 upsert_batch 一起落库。

    为什么放在写时：老视频的标签本来就只能靠夜间全量重建补，而夜间轮转
    会被增量同步打乱（见 star_rebuild_state 注释）。写时推断让**新片从入库
    那一刻就有标签**，存量则靠一次性分批回填，全量重建退化为校准/兜底。
    每页几十条 × 单正则 µs 级，对同步吞吐影响可忽略。
    """
    if not config.STAR_TAG_INFER_ON_SYNC or not rows:
        return 0
    pattern = star_pattern()
    if pattern is None:
        return 0
    library = _library_set()
    if not library:
        return 0
    ids = [r["vod_id"] for r in rows if r.get("vod_id") is not None]
    old_map = _old_inferred_map(conn, slug, ids)
    patched = 0
    for r in rows:
        vid = r.get("vod_id")
        if vid is None:
            continue
        tag_hits, _map_hits, _actor = _hits_for_row(
            r.get("name", ""), r.get("vod_actor", ""), pattern, library
        )
        inferred = sorted(tag_hits)[: config.STAR_TAG_MAX_PER_VIDEO]
        upstream = r.get("vod_tag") or ""
        base = upstream or (existing_tags.get(vid) or "")
        if config.STAR_TAG_MERGE_TAG:
            merged = merge_vod_tag(base, old_map.get(vid, set()), inferred)
        else:
            merged = ",".join(inferred) or base
        # 空结果不写：upsert 的 COALESCE 保护会把空串解释为「保留旧值」，
        # 清理过期推断段交给 update_ids / 全量重建，不在同步主链路做。
        if merged:
            r["vod_tag"] = merged
            patched += 1
    return patched


# ---------------------------------------------------------------------------
# 明星作品推荐查询（API 用）
# ---------------------------------------------------------------------------
def _esc_like(kw: str) -> str:
    """转义 LIKE 通配符（\\ % _），避免用户输入变成全表匹配。"""
    return kw.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def resolve_target(table, vod_id: int, name: str):
    """
    按 id 或名称定位目标视频（v1.9.2 健壮性加强）：
      ① 精确匹配
      ② 名称 LIKE 模糊（通配符已转义，取最近更新一条）
      ③ 空白差异兜底（「流浪地球 2」↔「流浪地球2」）
      ④ 全半角差异兜底（NFKC 归一：ＨＤ → HD）
    仍找不到返回 None —— 调用方保持 404 语义（客户端主走 vod_id 路径）。
    """
    import re as _re
    import unicodedata

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
        if row:
            return row

        def _like(expr, value):
            return conn.execute(
                select(table.c.vod_id, table.c.name)
                .where(expr.like(f"%{_esc_like(value)}%", escape="\\"))
                .order_by(table.c.vod_time.desc(), table.c.vod_id.desc())
                .limit(1)
            ).first()

        # 依次尝试：原串 → 去空白 → 全半角归一
        variants = [nm]
        no_space = _re.sub(r"\s+", "", nm)
        if no_space and no_space != nm:
            variants.append(no_space)
        nfkc = unicodedata.normalize("NFKC", nm).strip()
        if nfkc and nfkc not in variants:
            variants.append(nfkc)
        for v in variants:
            row = _like(table.c.name, v)
            if row:
                return row
        # 最后兜底：库里名字去空格后再比（标题中间有空格但客户端没带）
        if no_space:
            row = _like(func.replace(table.c.name, " ", ""), no_space)
            if row:
                return row
        return None


def star_related(slug: str, vod_id: int = 0, name: str = "", limit: int = 20):
    """
    明星作品推荐：与目标视频共享明星（star_map 交集）的其他视频。
    返回 None = 目标视频不存在；否则 dict(target/stars/star_counts/truncated/list)。

    v1.9.2 新增（纯新增字段，向后兼容）：
      star_counts  该视频每位明星在本源站的作品数（降序），客户端渲染 Tab 角标
      truncated    候选总数超过 limit 被截断 → true（提示「查看更多」）
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
                    terms = sorted(_actor_hits(row[0], _library_set()))
        if not terms:
            return {"target": {"vod_id": target_id, "vod_name": target[1]},
                    "stars": [], "star_counts": {}, "truncated": False, "list": []}

        # 每位明星在本源站的作品数（Tab 角标用），降序
        star_counts = {
            term: int(n) for term, n in sorted(
                conn.execute(
                    select(star_map.c.term, func.count())
                    .where(star_map.c.slug == slug, star_map.c.term.in_(terms))
                    .group_by(star_map.c.term)
                ).all(),
                key=lambda r: -r[1],
            )
        }
        # stars 跟随 star_counts 的顺序（作品多的在前）—— 客户端 Tab 顺序稳定
        terms = list(star_counts)

        cond = [
            star_map.c.slug == slug,
            star_map.c.term.in_(terms),
            star_map.c.vod_id != target_id,
        ]
        # 候选：共享任一明星的其他视频，按重叠明星数降序（走 idx_star_map_term）。
        # v1.10.0 / P12：多取 1 条即可判断「有没有被截断」，不再为每次请求做
        # count(distinct vod_id)（8 位明星 × 各几千部 = 数万行扫描）。
        cand = conn.execute(
            select(star_map.c.vod_id, func.count().label("ov"))
            .where(*cond)
            .group_by(star_map.c.vod_id)
            .order_by(func.count().desc(), star_map.c.vod_id.asc())
            .limit(limit + 1)
        ).all()
        truncated = len(cand) > limit
        cand = cand[:limit]
        if not cand:
            return {"target": {"vod_id": target_id, "vod_name": target[1]},
                    "stars": terms, "star_counts": star_counts,
                    "truncated": False, "list": []}

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
    out = out[:limit]
    return {"target": {"vod_id": target_id, "vod_name": target[1]},
            "stars": terms, "star_counts": star_counts,
            "truncated": bool(truncated), "list": out}


def star_works(slug: str, term: str, limit: int = 30, exclude_id: int = 0) -> dict:
    """
    按明星查作品（v1.9.2，明星为中心）。

    与 star/related 的本质区别：不按「重叠明星数」全局排序后截断，而是直接取
    该明星的全部映射再排序 —— 目标视频命中多位明星时，作品少的明星也不会被
    挤出列表（客户端按明星切 Tab 的数据来源）。查询命中 idx_star_map_term。

    term 做与词库一致的归一化（trim/全半角/繁简），别名归一到主名。
    """
    table = build_vod_table(slug)
    canon = canon_star(term) or _star_norm(term or "").strip()
    cond = [star_map.c.slug == slug, star_map.c.term == canon]
    if exclude_id:
        cond.append(star_map.c.vod_id != exclude_id)
    # join 而不是「先取 id 再回表」：排序需要 vod_time；同时天然过滤已下架/已清理的行
    joined = table.join(
        star_map, and_(star_map.c.vod_id == table.c.vod_id, star_map.c.slug == slug)
    )
    with database.engine.connect() as conn:
        total = conn.execute(
            select(func.count()).select_from(joined).where(*cond)
        ).scalar() or 0
        rows = conn.execute(
            select(
                table.c.vod_id, table.c.name, table.c.pic, table.c.remarks,
                table.c.type_name,
            )
            .select_from(joined)
            .where(*cond)
            .order_by(table.c.vod_time.desc(), table.c.vod_id.desc())
            .limit(limit)
        ).mappings().all()
    return {
        "slug": slug,
        "term": canon,
        "total": int(total),
        "list": [
            {
                "vod_id": r["vod_id"],
                "vod_name": r["name"],
                "vod_pic": r["pic"],
                "vod_remarks": r["remarks"],
                "type_name": r["type_name"],
                "source": slug,
                "match_stars": [canon],
                "overlap": 1,
            }
            for r in rows
        ],
    }


def status_all() -> list:
    """
    各源站明星 tag 覆盖情况（面板/核对用）。

    v1.10.0 / P13：表存在性一次取全、只用一个连接、视频数与「缺演员数」合并成
    一次扫描（PG/SQLite 均支持 COUNT(*) FILTER）。旧实现每站 3 次查询 + 3 次借连接。
    注意 videos 仍取真实 count(*)（不用 sources.total_videos）—— 这是核对用的
    统计接口，数字必须和库里的行对得上。
    """
    from .models import sources

    with database.engine.connect() as conn:
        rows = conn.execute(
            select(sources.c.slug)
            .where(sources.c.enabled == 1).order_by(sources.c.id)
        ).all()
        map_counts = dict(conn.execute(
            select(star_map.c.slug, func.count()).group_by(star_map.c.slug)
        ).all())
        # v1.16.2 / R1：把真正的「上次全量重建」水位一并暴露出来。运维按站
        # 回填时要能一眼看出哪些站没跑过（last_full_at=None），而不是靠
        # star_map 的最新 computed_at 猜 —— 后者每 6h 被增量同步刷新过。
        try:
            rebuild_rows = {
                r[0]: r for r in conn.execute(
                    select(star_rebuild_state.c.slug,
                           star_rebuild_state.c.last_full_at,
                           star_rebuild_state.c.rows_scanned,
                           star_rebuild_state.c.rows_tagged,
                           star_rebuild_state.c.rows_mapped,
                           star_rebuild_state.c.elapsed_s)
                ).all()
            }
        except Exception:  # noqa: BLE001  老库还没建新表时不让核对接口挂掉
            rebuild_rows = {}
        existing = database.list_tables("vod_")
        out = []
        for (slug,) in rows:
            table = build_vod_table(slug)
            if table.name not in existing:
                continue
            no_actor_cond = or_(table.c.vod_actor == "", table.c.vod_actor.is_(None))
            try:
                videos, no_actor = conn.execute(
                    select(
                        func.count(),
                        func.count().filter(no_actor_cond),
                    ).select_from(table)
                ).first() or (0, 0)
            except Exception:
                # 老版本 SQLite 不支持 FILTER：退回两次统计
                videos = conn.execute(
                    select(func.count()).select_from(table)
                ).scalar() or 0
                no_actor = conn.execute(
                    select(func.count()).select_from(table).where(no_actor_cond)
                ).scalar() or 0
            rb = rebuild_rows.get(slug)
            out.append({
                "slug": slug,
                "videos": int(videos or 0),
                "no_actor_videos": int(no_actor or 0),
                "star_mapped": map_counts.get(slug, 0),
                "last_full_rebuild": rb[1].isoformat() if rb and rb[1] else None,
                "last_full_scanned": int(rb[2] or 0) if rb else 0,
                "last_full_tagged": int(rb[3] or 0) if rb else 0,
                "last_full_mapped": int(rb[4] or 0) if rb else 0,
                "last_full_elapsed_s": int(rb[5] or 0) if rb else 0,
            })
    return out


def rebuild_source_safe(slug: str, reason: str = "manual") -> dict:
    """吞异常版（后台线程用）：失败只记日志，不影响调用方。"""
    try:
        return rebuild_source(slug, reason=reason)
    except Exception:
        log.exception("[明星Tag] %s 重建失败（忽略）", slug)
        return {"slug": slug, "error": True}


# ---------------------------------------------------------------------------
# 未入词库高频词统计（v1.9.2，扩词库用）
#
# 为什么需要：词库是标题推断的唯一信号来源，词库覆盖不到的名字推不出来。
# 做法（启发式，结果是候选不是结论）：
#   1. 抽样「缺演员」视频标题 —— 词库推断的主力人群
#   2. 用词库正则把已命中片段替换成占位符，只分析剩余文本
#   3. 剩余中文串切 2~4 元 n-gram，统计**文档频次**（同一标题只计一次）
#   4. 去掉停用词/低频词；长词优先（短词被同频长词包含则丢弃）
# 结果给人工在面板确认后加入词库 —— 自动入库会把「第二季」这种噪声写进词库。
# ---------------------------------------------------------------------------
_CJK_RE = re.compile(r"[\u4e00-\u9fff]+")
_untracked_cache: dict = {"key": None, "at": 0.0, "data": None}
_UNTRACKED_TTL_SECONDS = 600


def untracked_terms(slug: str = "", limit: int = 100, sample: int = 0) -> dict:
    """返回 {"sample_titles": n, "candidates": [{"term", "count"}...]}（10 分钟缓存）。"""
    from .models import sources

    limit = max(1, min(500, limit))
    sample = sample or config.STAR_UNTRACKED_SAMPLE
    cache_key = (slug, limit, sample)
    now = time.time()
    if (_untracked_cache["key"] == cache_key
            and now - _untracked_cache["at"] < _UNTRACKED_TTL_SECONDS):
        return _untracked_cache["data"]

    with database.engine.connect() as conn:
        if slug:
            slugs = [slug]
        else:
            slugs = [r[0] for r in conn.execute(
                select(sources.c.slug).where(
                    sources.c.enabled == 1, sources.c.total_videos > 0
                ).order_by(sources.c.total_videos.desc())
            ).all()]
    slugs = [s for s in slugs if build_vod_table(s).name in database.list_tables("vod_")]
    if not slugs:
        return {"sample_titles": 0, "candidates": []}

    per = max(200, sample // len(slugs))
    titles: list = []
    for s in slugs:
        table = build_vod_table(s)
        with database.engine.connect() as conn:
            rows = conn.execute(
                select(table.c.name)
                .where(or_(table.c.vod_actor == "", table.c.vod_actor.is_(None)))
                .order_by(table.c.vod_time.desc())
                .limit(per)
            ).all()
        titles.extend(r[0] for r in rows if r[0])

    pattern = star_pattern()
    df: dict = {}
    for raw in titles:
        norm = _star_norm(raw)
        # 已命中词库的片段替换成占位符 —— 只在「剩余文本」里找候选
        residual = pattern.sub("\u0001", norm) if pattern is not None else norm
        grams = set()
        for seg in _CJK_RE.findall(residual):
            L = len(seg)
            for n in (2, 3, 4):
                if L < n:
                    break
                for i in range(L - n + 1):
                    grams.add(seg[i:i + n])
        for g in grams:
            df[g] = df.get(g, 0) + 1

    stops = [x.strip() for x in config.STAR_UNTRACKED_STOPWORDS.split(",") if x.strip()]
    min_count = config.STAR_UNTRACKED_MIN_COUNT
    cands = [
        (g, c) for g, c in df.items()
        if c >= min_count and not any(s in g for s in stops)
    ]
    # 长词优先：按频次降序、长度降序处理；短词若被已保留的长词包含则丢弃
    cands.sort(key=lambda x: (-x[1], -len(x[0])))
    kept: list = []
    for g, c in cands:
        if any(g in kg for kg, _kc in kept[:400]):
            continue
        kept.append((g, c))
        if len(kept) >= limit * 3:     # 多留一些再截断，避免被长词挤空
            break
    data = {
        "sample_titles": len(titles),
        "candidates": [{"term": g, "count": c} for g, c in kept[:limit]],
    }
    _untracked_cache.update({"key": cache_key, "at": now, "data": data})
    return data
