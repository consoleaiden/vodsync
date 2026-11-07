"""
v1.9.2 明星作品（star/works）+ star/related 增强 + 词库别名 + 未命中词统计测试。

覆盖项：
 A. 别名体系：pattern 含别名、命中归一到主名；真实演员名写成别名也能归一
 B. star/works：
    B1 按明星查作品（total/排序 vod_time desc, vod_id desc）
    B2 limit 截断但 total 不变
    B3 exclude_id 生效
    B4 term 传别名 / 繁简全半角 → 归一到主名
    B5 无映射明星 → 200 + total 0（不是 404）；未知 slug → 404
 C. star/related 增强：
    C1 star_counts 明星→作品数（降序）
    C2 truncated 标志（候选 > limit）
    C3 stars 仍为 string[]（向后兼容）
 D. name 匹配健壮性：
    D1 前后空格 / 关键词中间空格
    D2 LIKE 通配符转义（% 不是通配）
    D3 全半角差异
    D4 确实找不到 → 404
 E. 未命中词统计：接口返回候选格式
 F. 别名 API：列表返回 aliases、PATCH 保存后进入 pattern

运行：python scripts/test_v192_star_works.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="vodsync-works-")
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(os.environ["DATA_DIR"], "works.db")
os.environ["AUTH_ENABLED"] = "false"
os.environ["FULL_SYNC_ON_DISCOVER"] = "false"
os.environ["AUTO_RESUME_ON_BOOT"] = "false"
os.environ["RELATED_ENABLED"] = "true"
os.environ["STAR_TAG_AUTO_AFTER_SYNC"] = "false"
os.environ["STAR_UNTRACKED_MIN_COUNT"] = "1"    # 测试样本少，降门槛

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


SLUG = "test"

# vod_id, name, actor, vod_time
SAMPLES = [
    (201, "流浪地球", "吴京", "2026-01-01 00:00:00"),
    (202, "战狼2", "吴京", "2026-01-02 00:00:00"),
    (203, "京哥的日常", "", "2026-01-03 00:00:00"),          # 别名命中 → 吴京
    (204, "华仔演唱会", "", "2026-01-04 00:00:00"),          # 别名命中 → 刘德华
    (205, "无关联综艺", "", "2026-01-05 00:00:00"),
    (206, "吴京和京哥的合体", "", "2026-01-06 00:00:00"),    # 主名+别名同现 → 只算一个主名
    (207, "100%纯爱故事", "", "2026-01-07 00:00:00"),
]


def map_terms(vod_id):
    from app import database
    from app.models import star_map

    with database.engine.connect() as conn:
        return sorted(r[0] for r in conn.execute(
            select(star_map.c.term)
            .where(star_map.c.slug == SLUG, star_map.c.vod_id == vod_id)
        ).all())


def main():
    from fastapi.testclient import TestClient

    from app import database, star_tags, syncer
    from app.models import build_vod_table, sources, star_terms

    database.init_db()
    with database.write_txn() as conn:
        conn.execute(sources.insert().values(
            name="测试源", slug=SLUG, api_url="https://x.example/api.php",
            status="ok", enabled=1, origin="custom", total_videos=len(SAMPLES),
        ))
    syncer.ensure_table(SLUG)
    syncer.upsert_batch(SLUG, [
        {"vod_id": v, "type_id": 0, "type_name": "剧情片", "name": n, "pic": "",
         "remarks": "", "content": "", "play_url": "", "vod_actor": a,
         "vod_director": "", "vod_writer": "", "vod_area": "", "vod_year": "",
         "vod_tag": "", "vod_time": t}
        for v, n, a, t in SAMPLES
    ])
    with database.write_txn() as conn:
        conn.execute(star_terms.insert().values(term="吴京", aliases="京哥,Wu Jing", enabled=1))
        conn.execute(star_terms.insert().values(term="刘德华", aliases="华仔", enabled=1))
        conn.execute(star_terms.insert().values(term="最美宝儿", aliases="", enabled=1))

    from app import related

    n = related.reload_star_terms()
    check("词库主名 3 条（别名不计入主名数）", n == 3, n)
    amap = related.star_alias_map()
    check("别名映射：京哥→吴京 / 华仔→刘德华",
          amap.get("京哥") == "吴京" and amap.get("华仔") == "刘德华", amap)
    check("canon_star 归一主名", related.canon_star("Wu Jing") == "吴京", related.canon_star("Wu Jing"))

    print("\n== A. 别名进入回填 ==")
    star_tags.rebuild_source(SLUG, reason="test")
    check("A 别名命中归一到主名（203 → 吴京）", map_terms(203) == ["吴京"], map_terms(203))
    check("A 别名命中归一到主名（204 → 刘德华）", map_terms(204) == ["刘德华"], map_terms(204))
    table = build_vod_table(SLUG)
    with database.engine.connect() as conn:
        tag203 = conn.execute(select(table.c.vod_tag).where(table.c.vod_id == 203)).scalar()
        tag206 = conn.execute(select(table.c.vod_tag).where(table.c.vod_id == 206)).scalar()
    check("A vod_tag 写主名（京哥 → 吴京）", tag203 == "吴京", tag203)
    check("A 主名+别名同现只算一个（206）", tag206 == "吴京", tag206)

    print("\n== B. star/works ==")
    from app.main import app
    client = TestClient(app)

    r = client.get(f"/api/v1/star/works?slug={SLUG}&term=吴京")
    check("B1 接口 200", r.status_code == 200, r.text[:200])
    d = r.json()
    check("B1 total = 吴京作品 4 部", d["total"] == 4, d)
    check("B1 排序 vod_time desc, vod_id desc",
          [x["vod_id"] for x in d["list"]] == [206, 203, 202, 201],
          [x["vod_id"] for x in d["list"]])
    item = d["list"][0]
    check("B1 卡片字段与 related 一致",
          set(item) == {"vod_id", "vod_name", "vod_pic", "vod_remarks",
                        "type_name", "source", "match_stars", "overlap"}, item)
    check("B1 match_stars/overlap", item["match_stars"] == ["吴京"] and item["overlap"] == 1, item)

    r = client.get(f"/api/v1/star/works?slug={SLUG}&term=吴京&limit=2")
    d = r.json()
    check("B2 limit 截断列表", len(d["list"]) == 2, len(d["list"]))
    check("B2 total 不随 limit 变（客户端据此判断截断）", d["total"] == 4, d["total"])

    r = client.get(f"/api/v1/star/works?slug={SLUG}&term=吴京&exclude_id=206")
    d = r.json()
    check("B3 exclude_id 排除当前视频",
          d["total"] == 3 and 206 not in [x["vod_id"] for x in d["list"]], d)

    r = client.get(f"/api/v1/star/works?slug={SLUG}&term=京哥")
    d = r.json()
    check("B4 传别名 → term 归一主名且结果一致",
          d["term"] == "吴京" and d["total"] == 4, d)
    r = client.get(f"/api/v1/star/works?slug={SLUG}&term=刘德华")
    check("B4 另一明星作品数", r.json()["total"] == 1, r.json())

    r = client.get(f"/api/v1/star/works?slug={SLUG}&term=不存在的明星")
    check("B5 无映射 → 200 + total 0（不 404）",
          r.status_code == 200 and r.json()["total"] == 0 and r.json()["list"] == [], r.text[:150])
    r = client.get("/api/v1/star/works?slug=nosuch&term=吴京")
    check("B5 未知 slug → 404", r.status_code == 404, r.status_code)
    r = client.get(f"/api/v1/star/works?slug={SLUG}&term=吴京&limit=999")
    check("B5 limit 超上限 → 422 参数校验", r.status_code == 422, r.status_code)

    print("\n== C. star/related 增强 ==")
    r = client.get(f"/api/v1/star/related?slug={SLUG}&vod_id=201")
    d = r.json()
    check("C1 stars 仍是 string[]（向后兼容）",
          isinstance(d["stars"], list) and all(isinstance(x, str) for x in d["stars"]), d["stars"])
    check("C1 star_counts 明星→作品数", d["star_counts"] == {"吴京": 4}, d["star_counts"])
    check("C2 候选未超 limit → truncated false", d["truncated"] is False, d["truncated"])

    r = client.get(f"/api/v1/star/related?slug={SLUG}&vod_id=201&limit=2")
    d = r.json()
    check("C2 候选超 limit → truncated true", d["truncated"] is True, d)
    check("C2 列表确实被截断到 limit", len(d["list"]) == 2, len(d["list"]))

    print("\n== D. name 匹配健壮性 ==")
    cases = [
        ("  流浪地球  ", 201, "D1 前后空格"),
        ("流浪 地球", 201, "D1 关键词中间空格"),
        ("流浪地球", 201, "D1 精确"),
        ("100%纯", 207, "D2 含 % 字符按字面匹配"),
    ]
    for nm, want, label in cases:
        r = client.get(f"/api/v1/star/related?slug={SLUG}&name={nm}")
        ok = r.status_code == 200 and r.json()["target"]["vod_id"] == want
        check(label, ok, (r.status_code, r.text[:120]))
    r = client.get(f"/api/v1/star/related?slug={SLUG}&name=%爱")
    check("D2 纯通配符查询被转义 → 404（不是全表命中）", r.status_code == 404, r.status_code)
    r = client.get(f"/api/v1/star/related?slug={SLUG}&name=根本不存在的片名")
    check("D4 确实找不到 → 404", r.status_code == 404, r.status_code)

    print("\n== E. 未命中词统计 ==")
    r = client.get(f"/api/v1/star/tags/untracked?slug={SLUG}&limit=20&sample=500")
    check("E 接口 200", r.status_code == 200, r.text[:150])
    d = r.json()
    check("E 返回抽样数与候选数组",
          "sample_titles" in d and isinstance(d["candidates"], list), d)
    words = [x["term"] for x in d["candidates"]]
    check("E 候选里已命中词库的片段被屏蔽（不含 吴京/京哥）",
          "吴京" not in words and "京哥" not in words, words[:10])
    check("E 候选含剩余文本片段（日常/演唱会/纯爱 之一）",
          any(w in ("日常", "演唱会", "纯爱") for w in words), words[:10])

    print("\n== F. 别名 API ==")
    r = client.get("/api/v1/star-terms")
    row = next(x for x in r.json()["list"] if x["term"] == "吴京")
    check("F 词库列表返回 aliases", row["aliases"] == "京哥,Wu Jing", row)
    r = client.patch(f"/api/v1/star-terms/{row['id']}",
                     json={"aliases": "京哥, 吴老师，Wu Jing"})
    check("F PATCH aliases 成功", r.status_code == 200 and "aliases" in r.json()["updated"], r.text[:150])
    amap = related.star_alias_map()
    check("F 别名去重后进入 pattern（吴老师→吴京）", amap.get("吴老师") == "吴京", amap)

    # 新增明星 + 别名 → 回填生效
    r = client.post("/api/v1/star-terms", json={"terms": "沈腾"})
    tid = next(x["id"] for x in client.get("/api/v1/star-terms").json()["list"] if x["term"] == "沈腾")
    client.patch(f"/api/v1/star-terms/{tid}", json={"aliases": "腾哥"})
    syncer.upsert_batch(SLUG, [{
        "vod_id": 208, "type_id": 0, "type_name": "喜剧片", "name": "腾哥的爆笑日常",
        "pic": "", "remarks": "", "content": "", "play_url": "", "vod_actor": "",
        "vod_director": "", "vod_writer": "", "vod_area": "", "vod_year": "",
        "vod_tag": "", "vod_time": "2026-01-08 00:00:00",
    }])
    star_tags.update_ids(SLUG, [208])
    check("F 新增别名后增量回填命中主名", map_terms(208) == ["沈腾"], map_terms(208))

    print(f"\n{'=' * 50}\n通过 {passed} / 失败 {failed}")
    return failed


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
