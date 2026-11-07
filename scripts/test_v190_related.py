"""
v1.9.0 相关推荐（服务端预计算）测试。

覆盖项（对齐需求验收标准）：
 A. 迁移：存量 vod_{slug} 表自动补 6 个推荐信号字段；vod_related 表创建
 B. 抓取链路：extract_row 提取 演员/导演/编剧/地区/年代/标签，占位值（内详/未知/暂无）清洗为空
 C. 归一化：全角/繁体/噪声后缀剥离/序号值摘取（第一季≠第二季、去重键带序号）
 D. 打分引擎：
    D1 字段齐全视频 → 分类/演员命中（权重重分配：缺失字段摊给其余项）
    D2 仅标题可用的系列视频（XX第一季/第二季/第三季）→ 命中同系列，序号不一致降权
    D3 无关联热门视频 → 兜底链返回非空（mode=fallback）
    D4 两字短标题 → 无误配（双阈值门控）
    D5 同名同年重复收录 → 不互推（同片去重）
    D6 增量重算 compute_source(only_ids) 只写指定目标
 E. API 端到端：GET /api/v1/related（正常/未知slug 404/未知视频 404）、
    detail 附带 related、/api/v1/related/status 覆盖率
 F. 同步后自动触发：incremental touched → 推荐表更新

运行：python scripts/test_v190_related.py
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="vodsync-related-")
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(os.environ["DATA_DIR"], "related.db")
os.environ["AUTH_ENABLED"] = "false"
os.environ["FULL_SYNC_ON_DISCOVER"] = "false"
os.environ["AUTO_RESUME_ON_BOOT"] = "false"
os.environ["RELATED_ENABLED"] = "true"

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


SAMPLES = [
    # vod_id, name, type_name, area, year, actor, director, time
    (101, "流浪地球", "科幻片", "中国大陆", "2019", "吴京,屈楚萧,李光洁", "郭帆", "2026-01-01 00:00:00"),
    (102, "流浪地球2", "科幻片", "中国大陆", "2023", "吴京,刘德华", "郭帆", "2026-01-02 00:00:00"),
    (103, "战狼2", "动作片", "中国大陆", "2017", "吴京,吴刚", "吴京", "2026-01-03 00:00:00"),
    (104, "星际穿越", "科幻片", "美国", "2014", "马修·麦康纳,安妮·海瑟薇", "克里斯托弗·诺兰", "2026-01-04 00:00:00"),
    # 仅标题可用的系列（无演员/地区/年代）
    (105, "某某人生第一季", "国产剧", "", "", "", "", "2026-01-05 00:00:00"),
    (106, "某某人生 第二季", "国产剧", "", "", "", "", "2026-01-06 00:00:00"),
    (107, "某某人生第三季", "国产剧", "", "", "", "", "2026-01-07 00:00:00"),
    # 两字短标题：互相不应误配
    (108, "青云志", "古装剧", "中国大陆", "2016", "李易峰", "", "2026-01-08 00:00:00"),
    (109, "青云路", "古装剧", "中国大陆", "2017", "张若昀", "", "2026-01-09 00:00:00"),
    # 无关联热门（分类唯一、无地区/年代信号）→ 零命中走兜底
    (110, "海贼王剧场版", "动漫", "", "", "", "", "2026-01-10 00:00:00"),
    # 同名同年重复收录（同片去重）：111 与 101/112 同片，三条只留最新一条
    (111, "流浪地球", "科幻片", "中国大陆", "2019", "吴京,屈楚萧,李光洁", "郭帆", "2026-01-11 00:00:00"),
    # 噪声后缀标题：归一化后与 101 同名同年 → 也算同片重复收录
    (112, "流浪地球 (HD国语 4K完整版)", "科幻片", "中国大陆", "2019", "吴京,屈楚萧,李光洁", "郭帆", "2026-01-12 00:00:00"),
    # 两字标题 + 零其他信号（不同分类）→ 只剩标题信号，门控必须挡住
    (113, "孤舟", "纪录片", "", "", "", "", "2026-01-13 00:00:00"),
    (114, "孤岛", "综艺", "", "", "", "", "2026-01-14 00:00:00"),
]


def row_of(vod_id):
    for r in SAMPLES:
        if r[0] == vod_id:
            return r
    return None


def main():
    from fastapi.testclient import TestClient
    from sqlalchemy import text as _text

    from app import database, related, syncer
    from app.main import app
    from app.models import build_vod_table, sources, vod_related
    from app.maccms import MacCmsClient

    database.init_db()
    client = TestClient(app)
    SLUG = "t190"

    # ------------------------------------------------------------------
    print("\n[A] 迁移：老库 vod 表自动补推荐信号字段")
    # ------------------------------------------------------------------
    # 手工造一张"老结构"表（没有新字段），再跑一次 _migrate
    with database.write_txn() as conn:
        conn.execute(_text(
            "CREATE TABLE IF NOT EXISTS vod_legacy (vod_id INTEGER PRIMARY KEY, name TEXT)"
        ))
    database._migrate()
    cols = {c["name"] for c in __import__("sqlalchemy").inspect(database.engine).get_columns("vod_legacy")}
    check("老表自动补出 vod_actor", "vod_actor" in cols, sorted(cols))
    check("老表自动补出 vod_year", "vod_year" in cols, sorted(cols))
    check("vod_related 表已创建", database.table_exists("vod_related"))

    # ------------------------------------------------------------------
    print("\n[B] 抓取链路：extract_row 新字段 + 占位值清洗")
    # ------------------------------------------------------------------
    r = MacCmsClient.extract_row({
        "vod_id": 555, "type_id": 6, "type_name": "科幻片",
        "vod_name": "测试片", "vod_pic": "p.jpg", "vod_remarks": "HD",
        "vod_content": "", "vod_play_url": "", "vod_time": "2026-01-01 00:00:00",
        "vod_actor": "内详,张三,张三,未知", "vod_director": "暂无", "vod_writer": "李四",
        "vod_area": "未知", "vod_year": "2024", "vod_tag": "科幻,内详",
    }, {6: "科幻片"})
    check("演员清洗：占位值剔除+去重", r["vod_actor"] == "张三", r["vod_actor"])
    check("导演占位值清洗为空", r["vod_director"] == "", r["vod_director"])
    check("编剧正常保留", r["vod_writer"] == "李四", r["vod_writer"])
    check("地区占位值清洗为空", r["vod_area"] == "", r["vod_area"])
    check("年代保留", r["vod_year"] == "2024", r["vod_year"])
    check("标签占位值清洗", r["vod_tag"] == "科幻", r["vod_tag"])

    # ------------------------------------------------------------------
    print("\n[C] 归一化与序号")
    # ------------------------------------------------------------------
    n1, o1 = related.normalize_title("流浪地球 (HD国语 4K完整版)")
    check("噪声后缀剥离", n1 == "流浪地球", n1)
    n2, o2 = related.normalize_title("某某人生第一季")
    n3, o3 = related.normalize_title("某某人生第二季")
    check("季序号摘取", o1 == frozenset() and o2 == frozenset({"季1"}) and o3 == frozenset({"季2"}),
          f"{o1}/{o2}/{o3}")
    check("去重键带序号（系列不互杀）", related._dup_key(
        {"name": n2, "year": "", "ords": o2}) != related._dup_key({"name": n3, "year": "", "ords": o3}))
    s_series = related.title_similarity(
        n2, o2, "", related._bigrams(n2), n3, o3, "", related._bigrams(n3))
    check("同系列续作命中且序号降权", 0.1 < s_series <= 0.5 * 1.0 + 1e-6, s_series)
    n4, _ = related.normalize_title("青云志")
    n5, _ = related.normalize_title("青云路")
    check("两字短标题门控（LCS<3 且 Jaccard<0.45）",
          related.title_similarity(
              n4, frozenset(), "2020", related._bigrams(n4),
              n5, frozenset(), "2020", related._bigrams(n5)) == 0.0)

    # ------------------------------------------------------------------
    print("\n[D] 计算引擎端到端")
    # ------------------------------------------------------------------
    with database.write_txn() as conn:
        conn.execute(sources.insert().values(
            name="测试源", slug=SLUG, api_url="https://example.com/api.php",
            status="ok", enabled=1, auto_sync=1, origin="custom", total_videos=len(SAMPLES),
        ))
    syncer.ensure_table(SLUG)
    rows = []
    for vid, name, tname, area, year, actor, director, ts in SAMPLES:
        rows.append({
            "vod_id": vid, "type_id": 1, "type_name": tname, "name": name,
            "pic": f"pic/{vid}.jpg", "remarks": "HD", "content": "", "play_url": "",
            "vod_actor": actor, "vod_director": director, "vod_writer": "",
            "vod_area": area, "vod_year": year, "vod_tag": "", "vod_time": ts,
        })
    ins, upd = syncer.upsert_batch(SLUG, rows)
    check("UPSERT 全部入库", ins == len(SAMPLES) and upd == 0, f"{ins}/{upd}")

    stats = related.compute_source(SLUG, reason="test")
    check("全量计算覆盖所有条目", stats["computed"] == len(SAMPLES), stats)
    # 同片去重：101/111/112 同名同年同序号，三条只留最新（112）→ 2 条被判重
    check("同名同年重复收录被去重", stats["duplicates"] == 2, stats)

    def related_of(vid):
        with database.engine.connect() as conn:
            row = conn.execute(
                select(vod_related).where(
                    vod_related.c.slug == SLUG, vod_related.c.vod_id == vid)
            ).mappings().first()
        return json.loads(row["related_json"]) if row and row["related_json"] else []

    # D1 字段齐全 → 分类/演员命中
    rel = related_of(101)
    ids = [e["v"] for e in rel]
    check("101 推荐含 112（同名不同记录但年相同→去重键相同，不应出现）", 112 not in ids, ids)
    check("101 推荐含同分类的 102（流浪地球2）", 102 in ids, ids)
    check("101 推荐含同演员的 103（战狼2·吴京）", 103 in ids, ids)
    # v1.10.0 契约变更（A1-3 内容信号门控）：只有同分类/同地区/同年代、没有任何
    # 内容信号（标题/演员/导演/编剧）的候选不再进推荐位，改走兜底链。
    check("101 不再推荐「仅同分类」的 104（星际穿越，无内容信号）", 104 not in ids, ids)
    check("101 不会推荐重复收录的 111（同片）", 111 not in ids, ids)
    scores = {e["v"]: e["s"] for e in rel}
    check("演员+标题命中分高于纯演员命中（102 > 103）",
          102 in scores and 103 in scores and scores[102] > scores[103],
          f"102={scores.get(102)} 103={scores.get(103)}")
    check("所有命中分 >= 阈值", all(e["s"] >= 0.15 or e.get("m") == "fb" for e in rel), rel[:5])
    check("命中条目带 reason（展示用命中原因）",
          all(e.get("r") for e in rel if e.get("m") != "fb"), rel[:3])

    # D2 仅标题系列 → 命中同系列
    rel = related_of(105)
    ids = [e["v"] for e in rel]
    check("第一季命中第二季/第三季", 106 in ids and 107 in ids, ids)
    check("系列条目非兜底（真实标题命中）", all(e.get("m") != "fb" for e in rel), rel[:5])

    # D3 无关联 → 兜底非空
    rel = related_of(110)
    check("海贼王剧场版仍有推荐（兜底链）", len(rel) > 0, rel)
    check("兜底条目带 fb 标记", any(e.get("m") == "fb" for e in rel), rel[:5])

    # D4 两字/短标题无误配（113/114 除标题外零信号、分类不同）
    rel = related_of(113)
    check("孤舟 不推荐 孤岛（短标题门控，纯标题信号不越阈）",
          not any(e["v"] == 114 and e.get("m") != "fb" for e in rel), rel)
    check("孤舟 走兜底也有内容", len(rel) > 0, rel[:5])

    # D6 增量重算只写指定目标
    with database.write_txn() as conn:
        conn.execute(vod_related.delete().where(
            vod_related.c.slug == SLUG, vod_related.c.vod_id == 103))
    related.compute_source(SLUG, only_ids=[103], reason="test_incr")
    with database.engine.connect() as conn:
        n = conn.execute(select(_text("1")).select_from(vod_related).where(
            vod_related.c.slug == SLUG, vod_related.c.vod_id == 103)).first()
        total = conn.execute(select(_text("count(*)")).select_from(vod_related).where(
            vod_related.c.slug == SLUG)).scalar()
    check("增量重算只补目标条目", n is not None and total == len(SAMPLES), total)

    # ------------------------------------------------------------------
    print("\n[E] API 端到端")
    # ------------------------------------------------------------------
    r = client.get(f"/api/v1/related?slug={SLUG}&id=101")
    body = r.json()
    check("related 接口 200", r.status_code == 200, r.status_code)
    check("返回卡片字段齐全",
          body["list"] and all(
              k in body["list"][0] for k in
              ("vod_id", "vod_name", "vod_pic", "vod_remarks", "type_name", "source", "source_name", "score")
          ), str(body["list"][:1]))
    check("fallback=false（有真实命中）", body["fallback"] is False, body["fallback"])
    check("computed_at 返回", bool(body.get("computed_at")))

    r = client.get(f"/api/v1/related?slug={SLUG}&id=110")
    check("无关联视频 fallback=true", r.json()["fallback"] is True and len(r.json()["list"]) > 0, r.json()["fallback"])

    r = client.get("/api/v1/related?slug=no_such&id=1")
    check("未知 slug 返回 404 标准错误", r.status_code == 404, r.status_code)
    r = client.get(f"/api/v1/related?slug={SLUG}&id=99999")
    check("未知视频返回 404 标准错误", r.status_code == 404, r.status_code)

    r = client.get(f"/api/v1/videos/{SLUG}/101")
    d = r.json()["detail"]
    check("REST 详情附带 related 数组",
          config_attached := ("related" in d), list(d.keys()))
    check("详情 related 条目为卡片结构",
          not d["related"] or "vod_name" in d["related"][0])

    r = client.get(f"/api/v1/{SLUG}?ac=detail&ids=101")
    item = r.json()["list"][0]
    check("MacCMS 兼容详情附带 vod_related", "vod_related" in item, list(item.keys()))

    r = client.get("/api/v1/related/status")
    row = next((x for x in r.json()["list"] if x["slug"] == SLUG), None)
    check("related/status 覆盖率 100%", row and row["coverage"] == 100.0, r.json()["list"][:3])

    # ------------------------------------------------------------------
    print("\n[F] 同步后自动触发（touched → 推荐更新）")
    # ------------------------------------------------------------------
    # 模拟增量写入了 1 条新视频：syncer._note_touched + _kick_related 增量路径
    new_rows = [{
        "vod_id": 120, "type_id": 1, "type_name": "科幻片", "name": "流浪地球：终极篇",
        "pic": "pic/120.jpg", "remarks": "TC", "content": "", "play_url": "",
        "vod_actor": "吴京", "vod_director": "郭帆", "vod_writer": "",
        "vod_area": "中国大陆", "vod_year": "2026", "vod_tag": "", "vod_time": "2026-02-01 00:00:00",
    }]
    syncer.upsert_batch(SLUG, new_rows)
    syncer._note_touched({"touched": []}, new_rows)
    stats2 = related.compute_source(SLUG, only_ids=[120], reason="test_kick")
    check("新视频 120 被增量重算覆盖", stats2["computed"] == 1, stats2)
    rel = related_of(120)
    ids = [e["v"] for e in rel]
    check("新视频命中同演员（吴京）同分类条目", 103 in ids or 102 in ids, ids)

    # ------------------------------------------------------------------
    print("\n[G] 明星词库：批量导入（自动识别 JSON/分隔符）+ 标题/简介推断 + 强弱信号")
    # ------------------------------------------------------------------
    # G1 解析：JSON 数组片段（带引号尾逗号）、纯分隔符、| 分隔
    r = client.post("/api/v1/star-terms", json={
        "terms": '"最美宝儿",\n"困困狗",\n"魔鬼爱",\n"小巨",'
    })
    b = r.json()
    check("JSON 片段（带引号尾逗号）自动识别", r.status_code == 200 and b["added"] == 4, b)
    r = client.post("/api/v1/star-terms", json={"terms": "张三丰，李莫愁|王重阳\n周伯通"})
    check("混合分隔符（，、| 换行）自动识别", r.json()["added"] == 4, r.json())
    r = client.post("/api/v1/star-terms", json={"terms": '["最美宝儿","困困狗"]'})
    check("标准 JSON 数组 + 幂等跳过", r.json()["added"] == 0 and r.json()["skipped"] == 2, r.json())

    # G2 构造稀疏视频：标题命中（强信号）/ 简介命中（弱信号）/ 真实演员
    star_rows = [
        (130, "最美宝儿", "国产剧", "", "", "", "", "2026-03-01 00:00:00", ""),          # 标题命中+稀疏 → strong
        (131, "宝儿穿搭日记", "国产剧", "", "", "", "", "2026-03-02 00:00:00",
         "记录最美宝儿的日常穿搭"),                                                       # 仅简介命中 → inf
        (132, "宝儿上分记", "国产剧", "", "", "最美宝儿,小巨", "", "2026-03-03 00:00:00", ""),  # 真实演员
        (133, "雪山飞狐", "武侠片", "", "", "", "", "2026-03-04 00:00:00", "张三丰客串登场"),  # 仅简介命中
    ]
    rows = []
    for vid, name, tname, area, year, actor, director, ts, content in star_rows:
        rows.append({
            "vod_id": vid, "type_id": 2, "type_name": tname, "name": name,
            "pic": f"pic/{vid}.jpg", "remarks": "HD", "content": content, "play_url": "",
            "vod_actor": actor, "vod_director": director, "vod_writer": "",
            "vod_area": area, "vod_year": year, "vod_tag": "", "vod_time": ts,
        })
    syncer.upsert_batch(SLUG, rows)
    related.compute_source(SLUG, only_ids=[130, 131, 132, 133], reason="test_star")

    rel = related_of(130)
    ids = [e["v"] for e in rel]
    check("标题命中（强信号）命中真实演员条目 132", 132 in ids, ids)
    check("强信号命中简介命中条目 131（同演员词库+同分类）", 131 in ids, ids)
    scores = {e["v"]: e["s"] for e in rel if e.get("m") != "fb"}
    check("强信号不打折（130↔132 分值高于弱信号配对）",
          132 in scores and (131 not in scores or scores[132] > scores[131]),
          f"132={scores.get(132)} 131={scores.get(131)}")

    rel = related_of(133)
    ids = [e["v"] for e in rel]
    check("简介命中（弱信号）不误配到国产权（分类不同且降权后不过阈）",
          130 not in [x for x in ids if x in scores], ids)

    # G3 停用词条 → 演员推断信号消失
    # v1.10.0 契约变更：只有「同分类、无内容信号」的配对不再计 0.6 分进推荐位，
    # 而是整条走兜底链（带 m=fb）—— 「推荐位里是同分类随机片」比「没有推荐」更差。
    tid = client.get("/api/v1/star-terms").json()["list"]
    target = next(x for x in tid if x["term"] == "最美宝儿")
    r = client.patch(f"/api/v1/star-terms/{target['id']}", json={"enabled": False})
    check("停用词条", r.status_code == 200, r.text[:120])
    related.compute_source(SLUG, only_ids=[130], reason="test_star_off")
    rel = related_of(130)
    match_ids = [e["v"] for e in rel if e.get("m") != "fb"]
    fb_ids = [e["v"] for e in rel if e.get("m") == "fb"]
    check("停用后 131 不再以「仅同分类」身份进推荐位", 131 not in match_ids, rel)
    check("停用后 130 走兜底链且非空", bool(fb_ids), rel)

    # G4 删除词条
    r = client.delete(f"/api/v1/star-terms/{target['id']}")
    check("删除词条", r.status_code == 200, r.text[:120])
    r = client.delete("/api/v1/star-terms/999999")
    check("删除不存在的词条返回 404", r.status_code == 404, r.status_code)
    r = client.get("/api/v1/star-terms")
    names = [x["term"] for x in r.json()["list"]]
    check("词库状态含解析模式与生效数", r.json()["infer_mode"] == "when_missing" and r.json()["active"] >= 6, r.json()["active"])

    print(f"\n{'=' * 50}\n通过 {passed} / 失败 {failed}")
    return failed


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
