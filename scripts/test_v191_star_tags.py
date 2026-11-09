"""
v1.9.1 明星 Tag 回填 + 明星作品推荐测试。

覆盖项：
 A. 词库正则：单正则编译、长名优先（吴京安 不被 吴京 抢断）、繁体/全角命中
 B. tag 回填 rebuild_source：
    B1 缺演员行标题命中 → vod_tag 写入（多明星逗号分隔）
    B2 真实演员行 → 演员 ∩ 词库进 star_map（src=actor），vod_tag 不覆盖
    B3 无命中行 → tag 保持空
    B4 词库停用 → 重建后旧 tag 被清掉（全量幂等）
 C. 增量 update_ids：新增视频 touched 回填；改名后不再命中 → 旧映射清除
 D. UPSERT 保护：增量同步空 tag 不覆盖本地推断值（COALESCE 修复）
 E. API 端到端：
    E1 GET /api/v1/star/related?id= → 共享明星视频，含 match_stars/overlap
    E2 ?name= 精确与模糊解析
    E3 无明星视频 → 空 list；未知视频/slug → 404
    E4 POST /api/v1/maintenance/star-tags 受理
    E5 GET /api/v1/star/tags/status 覆盖统计
 F. 相关性排序：重叠数多者靠前

运行：python scripts/test_v191_star_tags.py
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.apiresp import SUCCESS_CODE  # v1.14.0：成功码统一走常量

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="vodsync-startag-")
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(os.environ["DATA_DIR"], "startag.db")
os.environ["AUTH_ENABLED"] = "false"
os.environ["FULL_SYNC_ON_DISCOVER"] = "false"
os.environ["AUTO_RESUME_ON_BOOT"] = "false"
os.environ["RELATED_ENABLED"] = "true"
os.environ["STAR_TAG_AUTO_AFTER_SYNC"] = "false"   # 测试里手动触发，避免后台线程竞争

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

SAMPLES = [
    # vod_id, name, type_name, area, year, actor, tag, time
    # 真实演员行（tag 列保留源站原值，不动）
    (101, "流浪地球", "科幻片", "中国大陆", "2019", "吳京,屈楚蕭", "科幻,大制作", "2026-01-01 00:00:00"),
    (102, "战狼2", "动作片", "中国大陆", "2017", "吴京,吴刚", "", "2026-01-02 00:00:00"),
    # 缺演员行：标题命中词库
    (103, "郭帆监制最美宝儿旅行", "综艺", "", "", "", "", "2026-01-03 00:00:00"),   # 郭帆+最美宝儿
    (104, "吴京安的新剧", "国产剧", "", "", "", "", "2026-01-04 00:00:00"),       # 长名优先 → 吴京安
    (105, "最美宝儿的日常", "动漫", "", "", "", "", "2026-01-05 00:00:00"),       # 最美宝儿
    (106, " completely unrelated vlog", "纪录片", "", "", "", "", "2026-01-06 00:00:00"),  # 无命中
    # 停用词库项「困困狗」不应命中
    (107, "困困狗的美食日记", "综艺", "", "", "", "", "2026-01-07 00:00:00"),
]


def row_of(vod_id):
    from app import database
    from app.models import build_vod_table

    table = build_vod_table(SLUG)
    with database.engine.connect() as conn:
        return conn.execute(
            select(table.c.vod_id, table.c.name, table.c.vod_tag, table.c.vod_actor)
            .where(table.c.vod_id == vod_id)
        ).mappings().first()


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
    from app.models import sources, star_terms

    database.init_db()
    with database.write_txn() as conn:
        conn.execute(sources.insert().values(
            name="测试源", slug=SLUG, api_url="https://x.example/api.php",
            status="ok", enabled=1, origin="custom", total_videos=len(SAMPLES),
        ))
    syncer.ensure_table(SLUG)
    syncer.upsert_batch(SLUG, [
        {"vod_id": v, "type_id": 0, "type_name": t, "name": n, "pic": "",
         "remarks": "", "content": "", "play_url": "", "vod_actor": a,
         "vod_director": "", "vod_writer": "", "vod_area": ar, "vod_year": y,
         "vod_tag": tg, "vod_time": tm}
        for v, n, t, ar, y, a, tg, tm in SAMPLES
    ])
    with database.write_txn() as conn:
        for term, enabled in [("吴京", 1), ("吴京安", 1), ("最美宝儿", 1),
                              ("郭帆", 1), ("困困狗", 0)]:
            conn.execute(star_terms.insert().values(term=term, enabled=enabled))

    print("\n== A. 词库正则 ==")
    from app import related

    n = related.reload_star_terms()
    check("词库加载（启用 4 个 + 长名优先排序）", n == 4, n)
    pat = related.star_pattern()
    hits = sorted(set(pat.findall(related._star_norm("吴京安的新剧"))))
    check("长名优先：吴京安 整体命中而非 吴京", hits == ["吴京安"], hits)
    hits2 = sorted(set(pat.findall(related._star_norm("吳京 戰狼"))))
    check("繁体命中（吳京→吴京、戰狼 无词库项不误出）", hits2 == ["吴京"], hits2)

    print("\n== B. tag 回填 rebuild_source ==")
    stats = star_tags.rebuild_source(SLUG, reason="test")
    check("重建扫描全部行", stats["scanned"] == len(SAMPLES), stats)
    check("B1 缺演员行 tag 写入（103 多明星）",
          row_of(103)["vod_tag"] == "最美宝儿,郭帆", row_of(103)["vod_tag"])
    check("B2 真实演员行 tag 保留源站原值", row_of(101)["vod_tag"] == "科幻,大制作", row_of(101)["vod_tag"])
    check("B2 真实演员进映射（繁体演员归一化命中，src=actor）",
          map_terms(101) == ["吴京"], map_terms(101))
    check("B2 部分演员在词库（102: 吴京）", map_terms(102) == ["吴京"], map_terms(102))
    check("B1 标题命中进映射（103: 郭帆+最美宝儿）",
          map_terms(103) == ["最美宝儿", "郭帆"], map_terms(103))
    check("B4 长名优先写入 tag", row_of(104)["vod_tag"] == "吴京安", row_of(104)["vod_tag"])
    check("B3 无命中行 tag 保持空", row_of(106)["vod_tag"] == "", row_of(106)["vod_tag"])
    check("B4 停用词库项不命中（107）", row_of(107)["vod_tag"] == "" and map_terms(107) == [],
          (row_of(107)["vod_tag"], map_terms(107)))
    check("star_map 命中条数（101/102 各1 + 103 两条 + 104/105 各1 = 6）",
          stats["mapped"] == 6, stats["mapped"])

    print("\n== C. 增量 update_ids ==")
    syncer.upsert_batch(SLUG, [{
        "vod_id": 108, "type_id": 0, "type_name": "动作片", "name": "吴京的巅峰对决",
        "pic": "", "remarks": "", "content": "", "play_url": "", "vod_actor": "",
        "vod_director": "", "vod_writer": "", "vod_area": "", "vod_year": "",
        "vod_tag": "", "vod_time": "2026-01-08 00:00:00",
    }])
    r = star_tags.update_ids(SLUG, [108])
    check("C 增量回填 touched 视频命中", map_terms(108) == ["吴京"]
          and row_of(108)["vod_tag"] == "吴京", (map_terms(108), row_of(108)["vod_tag"]))
    # 改名后不再命中 → 旧映射清除
    with database.write_txn() as conn:
        from app.models import build_vod_table
        t = build_vod_table(SLUG)
        conn.execute(t.update().where(t.c.vod_id == 108).values(name="纯英文Documentary"))
    star_tags.update_ids(SLUG, [108])
    check("C 改名后不再命中 → 旧映射清空", map_terms(108) == [] and row_of(108)["vod_tag"] == "",
          (map_terms(108), row_of(108)["vod_tag"]))

    print("\n== D. UPSERT 不覆盖本地推断 tag ==")
    # 模拟增量同步：源站对 103 又推了一遍数据，vod_tag 为空
    syncer.upsert_batch(SLUG, [{
        "vod_id": 103, "type_id": 0, "type_name": "综艺", "name": "郭帆监制最美宝儿旅行",
        "pic": "", "remarks": "", "content": "", "play_url": "", "vod_actor": "",
        "vod_director": "", "vod_writer": "", "vod_area": "", "vod_year": "",
        "vod_tag": "", "vod_time": "2026-01-03 00:00:00",
    }])
    check("D 增量同步空 tag 不抹掉本地推断值", row_of(103)["vod_tag"] == "最美宝儿,郭帆",
          row_of(103)["vod_tag"])
    # 源站真给了非空 tag → 更新
    syncer.upsert_batch(SLUG, [{
        "vod_id": 103, "type_id": 0, "type_name": "综艺", "name": "郭帆监制最美宝儿旅行",
        "pic": "", "remarks": "", "content": "", "play_url": "", "vod_actor": "",
        "vod_director": "", "vod_writer": "", "vod_area": "", "vod_year": "",
        "vod_tag": "综艺", "vod_time": "2026-01-03 00:00:00",
    }])
    check("D 源站非空 tag 正常更新", row_of(103)["vod_tag"] == "综艺", row_of(103)["vod_tag"])

    print("\n== E. API 端到端 ==")
    from app.main import app
    client = TestClient(app)

    r = client.get(f"/api/v1/star/related?slug={SLUG}&vod_id=102")   # 吴京
    check("E1 接口 200", r.status_code == 200, r.text[:200])
    data = r.json()
    check("E1 目标明星集", data["stars"] == ["吴京"], data["stars"])
    ids = [x["vod_id"] for x in data["list"]]
    check("E1 共享明星视频返回（101/103? no 103无吴京 → 104不在）",
          101 in ids and 108 not in ids, ids)
    # 101(吴京,actor) 命中；104 是吴京安不算；103 是郭帆/最美宝儿不算
    one = next(x for x in data["list"] if x["vod_id"] == 101)
    check("E1 match_stars/overlap/source 字段",
          one["match_stars"] == ["吴京"] and one["overlap"] == 1 and one["source"] == SLUG, one)

    r = client.get(f"/api/v1/star/related?slug={SLUG}&vod_id=103")   # 郭帆,最美宝儿
    ids = [x["vod_id"] for x in r.json()["list"]]
    check("E1 多明星交集排序：105(最美宝儿,ov=1) 命中",
          105 in ids, ids)

    r = client.get(f"/api/v1/star/related?slug={SLUG}&name=战狼2")
    check("E2 name 精确解析 → 102", r.json()["target"]["vod_id"] == 102, r.text[:200])
    r = client.get(f"/api/v1/star/related?slug={SLUG}&name=战狼")
    check("E2 name 模糊解析 → 102", r.json()["target"]["vod_id"] == 102, r.text[:200])

    r = client.get(f"/api/v1/star/related?slug={SLUG}&vod_id=106")   # 无明星
    check("E3 无明星视频 → 空 list", r.json()["list"] == [] and r.json()["stars"] == [], r.text[:200])
    r = client.get(f"/api/v1/star/related?slug={SLUG}&vod_id=99999")
    check("E3 未知视频 404", r.status_code == 404, r.status_code)
    r = client.get("/api/v1/star/related?slug=no_such&id=1")
    check("E3 未知 slug 404", r.status_code == 404, r.status_code)

    r = client.post(f"/api/v1/maintenance/star-tags?slug={SLUG}")
    check("E4 手动重建受理", r.status_code == 200 and r.json()["code"] == SUCCESS_CODE, r.text[:150])
    # 后台线程异步重建：轮询等待完成（映射数恢复到 >=6）
    import time
    for _ in range(20):
        st_now = {x["slug"]: x for x in client.get("/api/v1/star/tags/status").json()["list"]}
        if st_now.get(SLUG, {}).get("star_mapped", 0) >= 6:
            break
        time.sleep(0.2)
    check("E4 重建完成后映射数恢复", st_now.get(SLUG, {}).get("star_mapped", 0) >= 6, st_now)

    r = client.get("/api/v1/star/tags/status")
    st = {x["slug"]: x for x in r.json()["list"]}.get(SLUG, {})
    check("E5 覆盖统计", st.get("videos") == len(SAMPLES) + 1 and st.get("star_mapped", 0) > 0, st)

    print("\n== F. 排序 ==")
    # 造一个与目标 103（郭帆+最美宝儿）重叠 2 个明星的视频
    syncer.upsert_batch(SLUG, [{
        "vod_id": 112, "type_id": 0, "type_name": "综艺", "name": "郭帆和最美宝儿的合作大片",
        "pic": "", "remarks": "", "content": "", "play_url": "", "vod_actor": "",
        "vod_director": "", "vod_writer": "", "vod_area": "", "vod_year": "",
        "vod_tag": "", "vod_time": "2026-01-12 00:00:00",
    }])
    star_tags.update_ids(SLUG, [112])
    r = client.get(f"/api/v1/star/related?slug={SLUG}&vod_id=103")   # 郭帆,最美宝儿
    lst = r.json()["list"]
    ids = [x["vod_id"] for x in lst]
    check("F 重叠 2 明星者排最前（112 ov=2 > 105 ov=1）",
          ids and ids[0] == 112, ids)

    print(f"\n{'=' * 50}\n通过 {passed} / 失败 {failed}")
    return failed


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
