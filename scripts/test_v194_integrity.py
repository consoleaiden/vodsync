"""
v1.10.0 一致性 / 算法 / 性能结构回归测试（对应 REVIEW_2026-11-07 的 W1~W4）。

覆盖项：
 W1 数据与功能级 Bug
   B1 批量加词条：单条 INSERT ... ON CONFLICT DO NOTHING（PG 事务级中止不再整批失败）
   B2 reset_algo_version 真的提交（DELETE 走 write_txn）
   B3 词库删改后重建：旧 vod_tag 被清掉（不再永久残留）
   B4 GBK 源站解码兜底 + 乱码丢弃（不再把 ��� 写进库）
 W2 算法与调参
   A1 证据先验：续作 > 同分类随机片；仅同分类/地区的配对不进推荐位（走兜底）
   A2 演员相似度改余弦：客串（1 对 10）不再得 1.0
   A3 演职员归一化：繁体 吳京 与简体 吴京 共享信号
   A5 标题否定词（访谈/盘点…）→ 强推断降为弱推断
   A6 标题相似度分母改 max(len)：「英雄」vs「英雄本色」不再 0.8
   A4 兜底链条数上限 RELATED_FALLBACK_LIMIT
   P11 GET /star-terms 不再重载词库；写操作只重载一次
 W3 性能结构
   P3 _load_feats 走 keyset 分页（SQL 里不再有 OFFSET）
   P5 增量重算窗口合并（多次 touched → 一次 compute）
   P6/P10 分类接口缓存 + 表存在性一次取全
 W4 数据库层
   P7 不带筛选的 /videos 用 SUM(total_videos) 估值并标 total_approximate
   P8 SQLite 后端跳过 pg_trgm（PG 才建）
   P9 启动只重算「条数为 0」的源站
 其他顺手修的小 Bug
   C2 star/related 的 truncated 用 limit+1 判定（下架行不再导致恒 true）
   C3 MacCMS 兼容列表的 wd 转义（搜 % 不再全表匹配）
   C5 build_vod_table 并发首次构造不抛 InvalidRequestError
   C1 夜间重建按「上次重建时间」轮转（不再被 mapped>=tv 卡死）

运行：python scripts/test_v194_integrity.py
"""
import inspect
import json
import os
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="vodsync-v194-")
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(os.environ["DATA_DIR"], "v194.db")
os.environ["AUTH_ENABLED"] = "false"
os.environ["FULL_SYNC_ON_DISCOVER"] = "false"
os.environ["AUTO_RESUME_ON_BOOT"] = "false"
os.environ["RELATED_ENABLED"] = "true"
os.environ["RELATED_STAR_INFER_MODE"] = "when_missing"

from sqlalchemy import select  # noqa: E402

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


SLUG = "t194"

# vod_id, name, type_name, area, year, actor, director, time
SAMPLES = [
    (101, "流浪地球", "科幻片", "中国大陆", "2019", "吴京,屈楚萧,李光洁", "郭帆", "2026-01-01 00:00:00"),
    (102, "流浪地球2", "科幻片", "中国大陆", "2023", "吴京,刘德华", "郭帆", "2026-01-02 00:00:00"),
    (103, "星际穿越", "科幻片", "美国", "2014", "马修·麦康纳", "诺兰", "2026-01-03 00:00:00"),
    (104, "戰狼2", "动作片", "中国大陆", "2017", "吳京,吳剛", "", "2026-01-04 00:00:00"),
    (105, "孤舟", "纪录片", "", "", "", "", "2026-01-05 00:00:00"),
    # 缺演员、标题命中词库（B3 的清理对象）：101/104 是真实演员行，不会被写 tag
    (106, "吴京的战狼合集电影", "动作片", "", "", "", "", "2026-01-06 00:00:00"),
]


def main():
    from fastapi.testclient import TestClient
    from sqlalchemy import bindparam, text as _text
    from sqlalchemy import event as _event

    from app import config, database, maccms, related, star_tags, syncer
    from app.main import app
    from app.models import META, build_vod_table, sources, star_map, star_terms, vod_related
    from app.maccms import MacCmsClient

    database.init_db()
    client = TestClient(app)

    with database.write_txn() as conn:
        conn.execute(sources.insert().values(
            name="测试源194", slug=SLUG, api_url="https://example.com/api.php",
            status="ok", enabled=1, auto_sync=1, origin="custom", total_videos=len(SAMPLES),
        ))
    syncer.ensure_table(SLUG)
    syncer.upsert_batch(SLUG, [{
        "vod_id": vid, "type_id": 1, "type_name": tname, "name": name,
        "pic": f"pic/{vid}.jpg", "remarks": "HD", "content": "", "play_url": "",
        "vod_actor": actor, "vod_director": director, "vod_writer": "",
        "vod_area": area, "vod_year": year, "vod_tag": "", "vod_time": ts,
    } for vid, name, tname, area, year, actor, director, ts in SAMPLES])

    # ==================================================================
    print("\n[W1-B1] 批量加词条：单条 ON CONFLICT，不再逐条 try/except")
    # ==================================================================
    r = client.post("/api/v1/star-terms", json={"terms": "吴京，沈腾、郭帆"})
    check("首次批量添加 3 条", r.status_code == 200 and r.json()["added"] == 3, r.text[:160])
    # 混着已存在的名字 + 新名字：PG 上旧实现会整批 500
    r = client.post("/api/v1/star-terms", json={"terms": "吴京,沈腾,新演员甲,新演员乙"})
    b = r.json()
    check("重复词条与新词条混批：不再整批失败", r.status_code == 200, r.text[:160])
    check("混批：新增 2、跳过 2", b.get("added") == 2 and b.get("skipped") == 2, b)
    with database.engine.connect() as conn:
        n = conn.execute(_text("select count(*) from star_terms")).scalar()
    check("混批后库里正好 5 条（3+2）", n == 5, n)
    src = inspect.getsource(__import__("app.routes.api", fromlist=["x"]).star_terms_add)
    check("实现是 ON CONFLICT DO NOTHING（源码级）", "on_conflict_do_nothing" in src, "")
    check("实现不再逐条 try/except 吞异常", "except Exception" not in src, "")
    r = client.post("/api/v1/star-terms", json={"terms": ""})
    check("空词条返回 400", r.status_code == 400, r.status_code)

    # ==================================================================
    print("\n[W1-B2] reset_algo_version 的 DELETE 真的提交")
    # ==================================================================
    with database.write_txn() as conn:
        conn.execute(vod_related.delete())
        conn.execute(vod_related.insert().values(
            slug=SLUG, vod_id=1, related_json="[]", algo_version="1"))
        conn.execute(vod_related.insert().values(
            slug=SLUG, vod_id=2, related_json="[]", algo_version="1"))
        conn.execute(vod_related.insert().values(
            slug=SLUG, vod_id=3, related_json="[]",
            algo_version=config.RELATED_ALGO_VERSION))
    n = related.reset_algo_version()
    with database.engine.connect() as conn:
        left = conn.execute(_text("select count(*) from vod_related")).scalar()
        versions = [r[0] for r in conn.execute(_text("select algo_version from vod_related")).all()]
    check("返回值 = 删除 2 条", n == 2, n)
    check("旧版本行确实被删掉（事后只剩 1 条）", left == 1 and versions == [config.RELATED_ALGO_VERSION],
          f"left={left} versions={versions}")

    # ==================================================================
    print("\n[W1-B3] 词库删改后重建：旧 vod_tag 被清掉")
    # ==================================================================
    with database.write_txn() as conn:
        conn.execute(star_terms.delete())
        conn.execute(star_terms.insert().values(term="吴京", enabled=1))
    related.reload_star_terms()
    star_tags.rebuild_source(SLUG, reason="t_b3")
    with database.engine.connect() as conn:
        tags = dict(conn.execute(
            select(build_vod_table(SLUG).c.vod_id, build_vod_table(SLUG).c.vod_tag)
            .where(build_vod_table(SLUG).c.vod_id.in_([101, 106]))
        ).all())
    check("重建后缺演员行（106，标题含吴京）写入 吴京", tags.get(106) == "吴京", tags)
    check("真实演员行（101）的源站 tag 不被改写", (tags.get(101) or "") == "", tags)
    with database.write_txn() as conn:
        conn.execute(star_terms.delete())
        conn.execute(star_terms.insert().values(term="沈腾", enabled=1))
    related.reload_star_terms()
    star_tags.rebuild_source(SLUG, reason="t_b3b")
    with database.engine.connect() as conn:
        t106 = conn.execute(
            select(build_vod_table(SLUG).c.vod_tag).where(build_vod_table(SLUG).c.vod_id == 106)
        ).scalar()
        nmap = conn.execute(_text("select count(*) from star_map")).scalar()
    check("词库换人后 106 的旧 tag 被清空（B3 修复点）", (t106 or "") == "", repr(t106))
    check("star_map 同时清空", nmap == 0, nmap)
    with database.write_txn() as conn:
        conn.execute(star_terms.delete())
        conn.execute(star_terms.insert().values(term="吴京", enabled=1))
    related.reload_star_terms()
    star_tags.rebuild_source(SLUG, reason="t_b3c")

    # ==================================================================
    print("\n[W1-B4] 源站编码兜底 + 乱码丢弃")
    # ==================================================================
    gbk = json.dumps({"code": 1, "list": [{"vod_name": "流浪地球"}]}, ensure_ascii=False).encode("gbk")
    d = maccms.decode_payload(gbk, "text/html")
    check("GBK 响应按 gb18030 解码，片名正确", d["list"][0]["vod_name"] == "流浪地球", d)
    bom = b"\xef\xbb\xbf" + json.dumps({"code": 1}, ensure_ascii=False).encode("utf-8")
    check("UTF-8 BOM 正常解析", maccms.decode_payload(bom, "application/json")["code"] == 1)
    ctype = json.dumps({"n": "中文"}, ensure_ascii=False).encode("gb18030")
    check("Content-Type 里的 charset 优先于猜测",
          maccms.decode_payload(ctype, "application/json; charset=gb18030")["n"] == "中文")
    bad = b'{"list": [{"vod_name": "\xff\xfe\x80"}]}'
    try:
        maccms.decode_payload(bad, "application/json")
        ok, msg = False, "没有抛异常（乱码会入库）"
    except maccms.MacCmsError as e:
        ok, msg = True, str(e)[:60]
    check("不可解码字节 → 抛 MacCmsError（整页丢弃而不是写 ���）", ok, msg)
    try:
        maccms.decode_payload('{"n": "abc\ufffd"}'.encode("utf-8"), "application/json")
        ok2 = False
    except maccms.MacCmsError:
        ok2 = True
    check("响应里已含 U+FFFD → 同样丢弃", ok2)

    # ==================================================================
    print("\n[W2-A1] 证据先验 + 内容信号门控")
    # ==================================================================
    f101 = related._feat(101, "流浪地球", "科幻片", "中国大陆", "2019",
                         frozenset({"吴京", "屈楚萧", "李光洁"}), "郭帆", "", "2026-01-01")
    f102 = related._feat(102, "流浪地球2", "科幻片", "中国大陆", "2023",
                         frozenset({"吴京", "刘德华"}), "郭帆", "", "2026-01-02")
    f103 = related._feat(103, "星际穿越", "科幻片", "美国", "2014",
                         frozenset({"马修·麦康纳"}), "诺兰", "", "2026-01-03")
    s_seq, r_seq = related._score_one(f101, f102)
    s_cls, r_cls = related._score_one(f101, f103)
    check("续作（标题+演员+导演）命中且分数合理", s_seq > 0.5, (s_seq, r_seq))
    check("reason 里内容信号排在语境信号前", r_seq.split(",")[0] in ("title", "actor"), r_seq)
    check("仅同分类/无内容信号的配对 → 0 分（不再 0.667）", s_cls == 0.0, (s_cls, r_cls))
    # 反过来：只有标题高度相似、其它字段全缺（稀疏数据）也应该过阈
    t1 = related._feat(1, "某某人生第一季", "国产剧", "", "", frozenset(), "", "", "")
    t2 = related._feat(2, "某某人生第二季", "国产剧", "", "", frozenset(), "", "", "")
    s_sparse, _ = related._score_one(t1, t2)
    check("稀疏数据（仅标题）仍能命中续作", s_sparse > config.RELATED_MIN_SCORE, s_sparse)
    check("字段越少分数不再自动封顶（稀疏 < 字段齐全）", s_sparse < s_seq, (s_sparse, s_seq))

    # ==================================================================
    print("\n[W2-A2/A3/A6] 演员余弦相似度 / 繁简归一 / 标题分母")
    # ==================================================================
    many = frozenset(f"演员{i}" for i in range(10))
    one = frozenset({"演员0"})
    check("客串（1 对 10）不再得 1.0", related._set_sim(many, one) < 0.5,
          related._set_sim(many, one))
    check("完全相同的名单仍是 1.0", related._set_sim(many, many) == 1.0)
    check("对称性（A→B 与 B→A 同值）",
          related._set_sim(many, one) == related._set_sim(one, many))
    fa = related._feat(1, "战狼", "动作片", "中国大陆", "2017", frozenset({"吳京"}), "", "", "")
    fb = related._feat(2, "战狼2", "动作片", "中国大陆", "2017", frozenset({"吴京"}), "", "", "")
    check("繁体 吳京 与简体 吴京 归一后共享演员", fa["actor"] == fb["actor"] == frozenset({"吴京"}),
          (fa["actor"], fb["actor"]))
    na, oa = related.normalize_title("英雄")
    nb, ob = related.normalize_title("英雄本色")
    sim_short = related.title_similarity(na, oa, "", related._bigrams(na),
                                        nb, ob, "", related._bigrams(nb))
    check("「英雄」vs「英雄本色」不再 0.8（分母改 max(len)）", sim_short < 0.7, sim_short)

    # ==================================================================
    print("\n[W2-A5] 标题否定词 → 强推断降级")
    pat = related.star_pattern()
    _, src_strong = related._infer_strength("吴京最新动作大片", "", "", "", "", pat)
    _, src_neg = related._infer_strength("盘点吴京十大名场面", "", "", "", "", pat)
    check("标题命中 + 字段稀疏 → strong", src_strong == "strong", src_strong)
    check("标题含否定词（盘点）→ 降为 inf", src_neg == "inf", src_neg)

    # ==================================================================
    print("\n[W2-A4] 兜底链条数上限")
    # ==================================================================
    related.compute_source(SLUG, reason="t_a4")
    with database.engine.connect() as conn:
        raw = conn.execute(
            select(vod_related.c.related_json).where(
                vod_related.c.slug == SLUG, vod_related.c.vod_id == 105)
        ).scalar()
    entries = json.loads(raw) if raw else []
    check("零命中条目走兜底且非空", bool(entries) and all(e.get("m") == "fb" for e in entries), entries)
    check(f"兜底条数 <= RELATED_FALLBACK_LIMIT({config.RELATED_FALLBACK_LIMIT})",
          len(entries) <= config.RELATED_FALLBACK_LIMIT, len(entries))
    check("兜底条数 <= 推荐条数上限", len(entries) <= config.RELATED_LIMIT_DEFAULT, len(entries))

    # ==================================================================
    print("\n[W2-P11] GET /star-terms 不再重载词库")
    # ==================================================================
    calls = {"n": 0}
    orig_reload = related.reload_star_terms

    def _counting_reload():
        calls["n"] += 1
        return orig_reload()

    related.reload_star_terms = _counting_reload
    try:
        client.get("/api/v1/star-terms")
        after_get = calls["n"]
        client.post("/api/v1/star-terms", json={"terms": "测试演员丙"})
        after_add = calls["n"]
    finally:
        related.reload_star_terms = orig_reload
    check("读接口零次重载", after_get == 0, after_get)
    check("写接口只重载一次（旧实现两次）", after_add == 1, after_add)

    # ==================================================================
    print("\n[W3-P3] _load_feats 走 keyset 分页（无 OFFSET）")
    # ==================================================================
    seen_sql = []

    def _cap(conn, cursor, statement, params, context, executemany):  # noqa: ANN001
        seen_sql.append((statement, params))

    _event.listen(database.engine, "before_cursor_execute", _cap)
    try:
        related._load_feats(SLUG)
    finally:
        _event.remove(database.engine, "before_cursor_execute", _cap)
    vod_selects = [(st, pr) for st, pr in seen_sql if "FROM vod_t194" in st]
    data_selects = [(st, pr) for st, pr in vod_selects if "vod_id >" in st]
    check("确实从 vod 表读数据", bool(vod_selects), len(seen_sql))
    check("按 vod_id 游标推进（WHERE vod_id > ?，keyset 分页）",
          bool(data_selects), [st[:90] for st, _p in vod_selects][:2])
    # SQLAlchemy 的 SQLite 方言即使只给 limit 也会渲染 "LIMIT ? OFFSET ?"（offset=0），
    # 所以判据是「offset 参数恒为 0」而不是「SQL 里没有 OFFSET 字样」。
    offsets = []
    for st, pr in data_selects:
        if "OFFSET" in st.upper():
            seq = pr if isinstance(pr, (list, tuple)) else (pr,)
            offsets.append(seq[-1])
    check("OFFSET 参数恒为 0（没有深分页）", all(int(o or 0) == 0 for o in offsets), offsets)

    # ==================================================================
    print("\n[W3-P5] 增量重算窗口合并")
    # ==================================================================
    orig_compute = related.compute_source
    batch_calls = []

    def _fake_compute(slug, only_ids=None, reason=""):
        batch_calls.append((slug, tuple(only_ids or [])))
        return {"computed": len(only_ids or []), "total": len(only_ids or [])}

    old_debounce = config.RELATED_INCREMENT_DEBOUNCE_SECONDS
    old_ratio = config.RELATED_INCREMENT_FULL_RATIO
    related.compute_source = _fake_compute
    try:
        config.RELATED_INCREMENT_DEBOUNCE_SECONDS = 600
        config.RELATED_INCREMENT_FULL_RATIO = 10.0    # 10 倍行数才算「改动过大」
        related.queue_recompute_ids(SLUG, [101, 102], reason="t_p5")
        related.queue_recompute_ids(SLUG, [102, 103], reason="t_p5")
        pending = related.pending_incremental()
        done = related.flush_incremental(force=True)
        config.RELATED_INCREMENT_DEBOUNCE_SECONDS = old_debounce
    finally:
        related.compute_source = orig_compute
    check("窗口内两次入队合并为 3 个 id（去重）", pending.get(SLUG) == 3, pending)
    check("合并后只触发一次重算", len(batch_calls) == 1, batch_calls)
    check("合并后的 id 是并集", batch_calls and batch_calls[0][1] == (101, 102, 103), batch_calls)
    check("flush 后队列清空", related.pending_incremental() == {}, related.pending_incremental())
    check("处理结果回报 slug/id 数", done and done[0][0] == SLUG, done)
    # 关掉合并窗口 → 立刻重算（旧行为仍可用）
    batch_calls.clear()
    related.compute_source = _fake_compute
    try:
        config.RELATED_INCREMENT_DEBOUNCE_SECONDS = 0
        related.queue_recompute_ids(SLUG, [104], reason="t_p5_now")
        config.RELATED_INCREMENT_DEBOUNCE_SECONDS = old_debounce
    finally:
        related.compute_source = orig_compute
    check("窗口=0 时立即重算", len(batch_calls) == 1, batch_calls)
    # 改动量超过整站比例 → 直接整站重算（only_ids=None）
    batch_calls.clear()
    related.compute_source = _fake_compute
    try:
        config.RELATED_INCREMENT_DEBOUNCE_SECONDS = 600
        config.RELATED_INCREMENT_FULL_RATIO = 0.0001
        related.queue_recompute_ids(SLUG, [101, 102], reason="t_p5_bulk")
        related.flush_incremental(force=True)
    finally:
        config.RELATED_INCREMENT_DEBOUNCE_SECONDS = old_debounce
        config.RELATED_INCREMENT_FULL_RATIO = old_ratio
        related.compute_source = orig_compute
    check("改动量超过整站比例 → 转为整站重算", batch_calls and batch_calls[0][1] == (), batch_calls)

    # ==================================================================
    print("\n[W3-P6/P10] 分类接口缓存 + 表存在性一次取全")
    # ==================================================================
    from app.routes import api as api_mod

    api_mod.reset_categories_cache()
    r1 = client.get("/api/v1/categories")
    r2 = client.get("/api/v1/categories")
    b1, b2 = r1.json(), r2.json()
    check("首次未命中缓存", b1.get("cached") is False, b1.get("cached"))
    check("第二次命中缓存", b2.get("cached") is True, b2.get("cached"))
    check("缓存前后结果一致", [x["type_name"] for x in b1["list"]] == [x["type_name"] for x in b2["list"]])

    # ==================================================================
    print("\n[W4-P7] total 近似（无筛选）与精确（有筛选）")
    # ==================================================================
    from app import multiquery

    multiquery.reset_total_cache()
    with database.write_txn() as conn:
        conn.execute(sources.update().where(sources.c.slug == SLUG)
                     .values(total_videos=len(SAMPLES)))
    r = client.get("/api/v1/videos")
    b = r.json()
    check("无筛选：带 total_approximate=true", b.get("total_approximate") is True, b.keys())
    check("无筛选：total 取 sources.total_videos 之和", b["total"] == len(SAMPLES), b["total"])
    r = client.get("/api/v1/videos?type_name=科幻片")
    b2 = r.json()
    check("有筛选：不带 total_approximate（精确统计）", "total_approximate" not in b2, b2.keys())
    check("有筛选：total 为精确 3 条（科幻片）", b2["total"] == 3, b2["total"])

    # ==================================================================
    print("\n[W4-P8/P9] pg_trgm 在 SQLite 跳过 / 启动只补零条数源站")
    # ==================================================================
    check("SQLite 后端跳过 pg_trgm", database.ensure_search_indexes().get("skipped") == "sqlite",
          database.ensure_search_indexes())
    syncer.ensure_table("t194zero")
    with database.write_txn() as conn:
        conn.execute(sources.insert().values(
            name="零条数站", slug="t194zero", api_url="https://x.example/api",
            status="ok", enabled=1, total_videos=0, origin="custom"))
    with database.write_txn() as conn:
        conn.execute(sources.update().where(sources.c.slug == SLUG).values(total_videos=999))
    touched = syncer.refresh_counts(only_zero=True)
    with database.engine.connect() as conn:
        zero_rows = conn.execute(
            select(sources.c.total_videos).where(sources.c.slug == "t194zero")).scalar()
        busy_rows = conn.execute(
            select(sources.c.total_videos).where(sources.c.slug == SLUG)).scalar()
    check("only_zero 只处理条数为 0 的源站（本次 1 个）", touched == 1, touched)
    check("零条数源站被校准为真实值 0", zero_rows == 0, zero_rows)
    check("有数据的源站不被启动重扫（仍是 999，交给夜间校准）", busy_rows == 999, busy_rows)

    # ==================================================================
    print("\n[其他] C2 truncated / C3 LIKE 转义 / C5 表构造并发 / C1 夜间轮转")
    # ==================================================================
    for vid in (201, 202, 203):
        syncer.upsert_batch(SLUG, [{
            "vod_id": vid, "type_id": 1, "type_name": "动作片", "name": f"战狼外传{vid}",
            "pic": "", "remarks": "", "content": "", "play_url": "",
            "vod_actor": "吴京", "vod_director": "", "vod_writer": "",
            "vod_area": "中国大陆", "vod_year": "2020", "vod_tag": "",
            "vod_time": f"2026-02-0{vid - 200} 00:00:00",
        }])
    star_tags.rebuild_source(SLUG, reason="t_c2")
    r = client.get(f"/api/v1/star/related?slug={SLUG}&vod_id=101&limit=2")
    b = r.json()
    check("limit=2 时 truncated=true（候选更多）", b.get("truncated") is True, b.get("truncated"))
    r = client.get(f"/api/v1/star/related?slug={SLUG}&vod_id=101&limit=50")
    check("limit=50 足够时 truncated=false（不再恒 true）", r.json().get("truncated") is False,
          r.json().get("truncated"))
    r = client.get(f"/api/v1/{SLUG}?ac=list&wd=%")
    check("MacCMS 列表 wd=% 已转义 → 不返回全表", r.json()["total"] == 0, r.json()["total"])
    r = client.get(f"/api/v1/{SLUG}?ac=list&wd=_")
    check("wd=_ 同样被转义", r.json()["total"] == 0, r.json()["total"])

    errors = []

    def _build(_i):
        try:
            build_vod_table("t194race")
        except Exception as e:      # noqa: BLE001
            errors.append(repr(e))

    threads = [threading.Thread(target=_build, args=(i,)) for i in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    check("8 线程并发首次构造同一张表不抛异常", not errors, errors[:1])
    META.remove(build_vod_table("t194race")) if "vod_t194race" in META.tables else None
    from app.models import _vod_table_cache
    _vod_table_cache.pop("t194race", None)

    from app import scheduler, star_tags as st_mod
    rebuilt = []
    orig_rebuild = st_mod.rebuild_source_safe
    st_mod.rebuild_source_safe = lambda slug, reason="": rebuilt.append(slug) or {"slug": slug}
    old_iv = config.STAR_TAG_NIGHTLY_MIN_INTERVAL_HOURS
    try:
        config.STAR_TAG_NIGHTLY_MIN_INTERVAL_HOURS = 0   # 关掉「刚重建过就跳过」
        scheduler._job_star_tags_nightly()
    finally:
        config.STAR_TAG_NIGHTLY_MIN_INTERVAL_HOURS = old_iv
        st_mod.rebuild_source_safe = orig_rebuild
    check("夜间重建按预算执行且不空转", 0 < len(rebuilt) <= config.STAR_TAG_NIGHTLY_BUDGET, rebuilt)

    print(f"\n{'=' * 50}\n通过 {passed} / 失败 {failed}")
    return failed


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
