"""
v1.14.1 修订回归：明星Tag回填必须同步标签倒排表 + 存量定点修补。

线上实证（2026-11-11，群晖 vodsync 容器）：
  - tag_index_state 57 站全 done、processed=6,583,578/6,583,588（索引任务确实跑完）
  - 但 s0c4e337e00 有 77,020 条视频带 vod_tag、video_tags 中 0 行；
    s430327adfe 有 4,270 条、同样 0 行 —— 两站都是夜间 star_tags 回填过的
  - 根因：star_tags._flush_tag_updates 只 UPDATE 主表，不维护 video_tags；
    syncer.upsert_batch 才是唯一维护倒排的写入路径

覆盖：
 A. 源码契约：_flush_tag_updates 带 slug、同事务 index_rows、savepoint 隔离
 B. 端到端：模拟明星Tag回填 → 倒排表立刻有行；标签清空 → 旧行删除
 C. sync_source_tags：只扫有标签行补齐存量；clean=True 洗掉孤儿行；
    不新建 state 行（否则默认 pending 会撤回 tag_search 能力声明）
 D. sync_all_sources 汇总与单站失败隔离

运行：python scripts/test_v1141_startag_sync.py
"""
import os
import re
import sys
import tempfile
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="vodsync-startag-"))
os.environ.setdefault("DATABASE_URL",
                      "sqlite:///" + os.path.join(os.environ["DATA_DIR"], "startag.db"))
os.environ.setdefault("AUTH_ENABLED", "false")
os.environ.setdefault("FULL_SYNC_ON_DISCOVER", "false")
os.environ.setdefault("AUTO_RESUME_ON_BOOT", "false")
os.environ.setdefault("RELATED_ENABLED", "false")
os.environ.setdefault("TAG_INDEX_ON_STARTUP", "false")
os.environ.setdefault("TAG_INDEX_SLEEP_MS", "0")

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


def main():
    from sqlalchemy import func, insert, select, update

    from app import config, database, models, star_tags, tags

    database.init_db()
    config.TAG_SEARCH_ENABLED = True
    config.TAG_SEARCH_FORCE = False

    # ---------------- A. 源码契约 ----------------
    print("\nA. 源码契约（明星Tag回填同事务维护倒排）")
    src_star = open(os.path.join(ROOT, "app", "star_tags.py"), encoding="utf-8").read()
    src_tags = open(os.path.join(ROOT, "app", "tags.py"), encoding="utf-8").read()
    check("A1 _flush_tag_updates 形参带 slug",
          bool(re.search(r"def _flush_tag_updates\(slug: str, table", src_star)), "")
    check("A2 同事务调用 tags.index_rows",
          "tags.index_rows(" in src_star, "")
    check("A3 savepoint 隔离（索引失败不回滚主表）",
          "conn.begin_nested()" in src_star, "")
    check("A4 两处调用点都传 slug",
          len(re.findall(r"_flush_tag_updates\(slug, table,", src_star)) == 2,
          len(re.findall(r"_flush_tag_updates\(slug, table,", src_star)))
    check("A5 只在 TAG_SEARCH_ENABLED 时同步",
          "if config.TAG_SEARCH_ENABLED and upd_rows:" in src_star, "")
    check("A6 tags 新增 sync_source_tags / sync_all_sources",
          "def sync_source_tags(" in src_tags and "def sync_all_sources(" in src_tags, "")
    check("A7 定点修补只扫有标签行（不扫全表）",
          'table.c.vod_tag != ""' in src_tags, "")
    check("A8 state 用 update 而非 upsert（不插 pending 行）",
          "tag_index_state.update()" in src_tags and
          "绝不新插一行" in src_tags, "")

    # ---------------- 建站 + 灌数据 ----------------
    slug = "startag_sync"
    now = datetime.now()
    with database.write_txn() as conn:
        conn.execute(insert(models.sources).values(
            slug=slug, name="明星回填测试站", api_url="http://x/api.php",
            enabled=1, created_at=now, updated_at=now,
        ).prefix_with("OR IGNORE"))
        # 模拟线上该站已有 done 进度行
        conn.execute(insert(models.tag_index_state).values(
            slug=slug, cursor=3, processed=3, indexed=0, total=3,
            status="done", error="", updated_at=now,
        ).prefix_with("OR IGNORE"))
    table = models.build_vod_table(slug)
    table.create(database.engine, checkfirst=True)
    with database.write_txn() as conn:
        conn.execute(insert(table), [
            {"vod_id": i, "type_id": 1, "type_name": "剧情",
             "name": f"测试片{i}", "pic": "", "remarks": "", "vod_time": now,
             "vod_area": "大陆", "vod_year": "2026", "vod_tag": ""}
            for i in (1, 2, 3)
        ])

    def n_tags(vod_id=None):
        with database.engine.connect() as conn:
            q = select(func.count()).select_from(models.video_tags).where(
                models.video_tags.c.source == slug)
            if vod_id is not None:
                q = q.where(models.video_tags.c.vod_id == vod_id)
            return conn.execute(q).scalar() or 0

    # ---------------- B. 明星Tag回填端到端 ----------------
    print("\nB. 明星Tag回填 → 倒排表同步")
    check("B1 初始倒排为空", n_tags() == 0, n_tags())
    star_tags._flush_tag_updates(slug, table, [
        {"vid": 1, "tag": "杨幂,刘诗诗"},
        {"vid": 2, "tag": "杨幂"},
    ])
    check("B2 回填后立即写入倒排（3 行）", n_tags() == 3, n_tags())
    check("B3 1 号视频两个标签", n_tags(1) == 2, n_tags(1))
    with database.engine.connect() as conn:
        v1 = conn.execute(select(table.c.vod_tag).where(table.c.vod_id == 1)).scalar()
    check("B4 主表 vod_tag 同步落库", v1 == "杨幂,刘诗诗", v1)
    r = tags.search_page([slug], "杨幂", page=1, page_size=10)
    check("B5 回填的明星名可被 tag= 精确检索（total=2）",
          r["total"] == 2 and r["tag_found"] is True, (r["total"], r.get("tag_found")))

    # 词库删改后标签被清空：旧倒排行必须跟着删
    star_tags._flush_tag_updates(slug, table, [{"vid": 1, "tag": ""}])
    check("B6 标签清空后旧倒排行被删（1 号归零）", n_tags(1) == 0, n_tags(1))
    check("B7 2 号不受影响", n_tags(2) == 1, n_tags(2))

    # ---------------- C. 存量定点修补 ----------------
    print("\nC. sync_source_tags 存量补齐")
    # 绕过倒排直接改主表：复现 v1.14.1 之前 star_tags 留下的历史数据
    with database.write_txn() as conn:
        conn.execute(update(table).where(table.c.vod_id == 3)
                     .values(vod_tag="周迅,赵薇"))
        # 再塞一行「主表已无此视频」的孤儿倒排行
        conn.execute(insert(models.video_tags).values(
            source=slug, vod_id=999, tag_norm="幽灵"))
    check("C1 造出存量缺口（3 号 0 行 + 1 行孤儿）", n_tags(3) == 0 and n_tags() == 2, n_tags())

    with database.engine.connect() as conn:
        n_state_before = conn.execute(
            select(func.count()).select_from(models.tag_index_state)).scalar()

    res = tags.sync_source_tags(slug)
    check("C2 只扫有标签行（2 号+3 号，不碰 1 号空标签）",
          res["scanned"] == 2, res)
    check("C3 3 号标签补齐（2 行）", n_tags(3) == 2, n_tags(3))
    check("C4 2 号保持 1 行（幂等不重复）", n_tags(2) == 1, n_tags(2))
    check("C5 clean=False 保留孤儿行", n_tags(999) == 1, n_tags(999))
    with database.engine.connect() as conn:
        n_state_after = conn.execute(
            select(func.count()).select_from(models.tag_index_state)).scalar()
        st = conn.execute(select(models.tag_index_state)
                          .where(models.tag_index_state.c.slug == slug)).mappings().first()
    check("C6 不新建 state 行（避免 pending 撤回能力）",
          n_state_before == n_state_after, (n_state_before, n_state_after))
    check("C7 state.status 仍为 done", st["status"] == "done", st["status"])
    check("C8 state.indexed 刷新为实测行数", st["indexed"] == res["rows"],
          (st["indexed"], res["rows"]))

    res2 = tags.sync_source_tags(slug, clean=True)
    check("C9 clean=True 洗掉孤儿行", n_tags(999) == 0, n_tags(999))
    check("C10 clean 后有效标签行仍在", n_tags() == res2["rows"] and n_tags() == 3,
          (n_tags(), res2["rows"]))

    # ---------------- D. 批量入口 ----------------
    print("\nD. sync_all_sources 汇总")
    allres = tags.sync_all_sources(slug=slug, clean=True)
    check("D1 处理 1 个站", allres["stations"] == 1, allres["stations"])
    check("D2 无失败站", allres["failed"] == [], allres["failed"])
    check("D3 rows 汇总与实测一致", allres["rows"] == n_tags(), (allres["rows"], n_tags()))
    check("D4 detail 含每站 scanned/written/rows",
          all(k in allres["detail"][0] for k in ("scanned", "written", "rows")),
          allres["detail"][0])
    check("D5 ready() 未被修补动作破坏",
          tags.ready() is True, tags.ready())

    print(f"\n通过 {passed} / 失败 {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
