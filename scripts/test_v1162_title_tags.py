"""
v1.16.2 标题词库回填修复回归测试。

背景（线上实测反例）：乐播资源站 337,986 行、vod_actor 全空，词库里有
「香草少女」，标题含该词 118 行却 vod_tag 全空、star_map 无映射。根因是
夜间全量重建的轮转水位被增量同步污染，加上回写逻辑本身有四个缺陷。

覆盖项：
 A. 命中与回写
    A1 标题命中多个词库词 -> vod_tag 写多个词（香草少女 反例）
    A2 有演员行 + 标题命中 -> 仍然写 vod_tag（旧实现二选一，完全跳过标题）
    A3 源站已有标签 + 标题命中 -> 合并保留，不整体覆盖
    A4 无命中 + 源站已有标签 -> 不清空（旧实现会清空，还连倒排一起删）
    A5 演员命中只进 star_map，不写 vod_tag（保持既有契约）
    A6 star_map.src 按词标记 actor/title
    A7 纯 ASCII 短词（er/ss/jk）被过滤，不污染标签
 B. 幂等与清理
    B1 重复 rebuild 不产生额外写（tagged=0）
    B2 词库停用/标题改名 -> 过期推断段精确移除，原生段保留
    B3 vod_tag 不超过 VARCHAR(255)，不切出半个词
 C. 倒排一致性
    C1 主表 vod_tag 与 video_tags 完全一致
    C2 写时推断（upsert_batch）新片入库即有标签，且倒排同步
 D. 全量重建水位（R1）
    D1 rebuild_source 写 star_rebuild_state
    D2 update_ids 不推进 star_rebuild_state
 E. 标签索引状态自愈（R5）
    E0 缺 state 行时 ready() 不放宽（保住 v1.14.0 灰度契约）
    E1 ensure_state_rows 给缺行的源站补 done/pending 状态
    E2 补齐后 ready() 由 false 变 true，tag_search 恢复声明
    E3 补齐的是真 done（processed==total，不误触发自愈回填）
    E4 连一行倒排都没有的站仍判未就绪

运行：python scripts/test_v1162_title_tags.py
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# 必须先设环境变量，再 import app.* —— 历史脚本 test_v191 就是把
# from app.apiresp import ... 放在前面，导致 config 先加载成 compose 的
# postgres://postgres:5432，在 Windows 上必然连不上。
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="vodsync-v1162-")
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(os.environ["DATA_DIR"], "t.db")
os.environ["AUTH_ENABLED"] = "false"
os.environ["FULL_SYNC_ON_DISCOVER"] = "false"
os.environ["AUTO_RESUME_ON_BOOT"] = "false"
os.environ["RELATED_ENABLED"] = "true"
os.environ["STAR_TAG_AUTO_AFTER_SYNC"] = "false"
os.environ["TAG_SEARCH_ENABLED"] = "true"
os.environ["TAG_INDEX_ON_SYNC"] = "true"
os.environ["STAR_TAG_REBUILD_SLEEP_MS"] = "0"

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


SLUG = "lebo"
SLUG2 = "newsite"


def row(conn, vid, slug=SLUG):
    from app.models import build_vod_table

    t = build_vod_table(slug)
    return conn.execute(
        select(t.c.name, t.c.vod_actor, t.c.vod_tag).where(t.c.vod_id == vid)
    ).mappings().first()


def mapping(conn, vid, slug=SLUG):
    from app.models import star_map

    return sorted(
        (r[0], r[1]) for r in conn.execute(
            select(star_map.c.term, star_map.c.src).where(
                star_map.c.slug == slug, star_map.c.vod_id == vid
            )
        )
    )


def inverted(conn, vid, slug=SLUG):
    from app.models import video_tags

    return sorted(r[0] for r in conn.execute(
        select(video_tags.c.tag_norm).where(
            video_tags.c.source == slug, video_tags.c.vod_id == vid
        )
    ))


def put(slug, vid, name, actor="", tag="", upsert=True):
    from app import database, syncer

    syncer.ensure_table(slug)
    syncer.upsert_batch(slug, [{
        "vod_id": vid, "type_id": 0, "type_name": "伦理片", "name": name,
        "pic": "", "remarks": "", "content": "", "play_url": "",
        "vod_actor": actor, "vod_director": "", "vod_writer": "",
        "vod_area": "", "vod_year": "", "vod_tag": tag,
        "vod_time": "2026-11-01 00:00:00",
    }])
    return database


def main():
    from app import config, database, related, star_tags, syncer, tags
    from app.models import (
        build_vod_table, sources, star_map, star_rebuild_state,
        star_terms, tag_index_state, video_tags,
    )

    database.init_db()
    with database.write_txn() as conn:
        for slug in (SLUG, SLUG2):
            conn.execute(sources.insert().values(
                name="乐播资源站" if slug == SLUG else "新站", slug=slug,
                api_url="https://x.example/api.php", status="ok", enabled=1,
                origin="custom", total_videos=10,
            ))
        for term, enabled in [
            ("香草少女", 1), ("少女", 1), ("麻豆", 1), ("九尾狐狸", 1),
            ("爆乳", 1), ("jk", 1), ("停用词", 0),
        ]:
            conn.execute(star_terms.insert().values(term=term, enabled=enabled))

    # 写时推断会立刻给入库行加标签；本测试要验证 rebuild 的纯效果，
    # 先关掉写时推断，C2 再单独打开验证。
    config.STAR_TAG_INFER_ON_SYNC = False

    rows = [
        (201, "香草少女 麻豆 完整版", "桥本有菜,三上悠亚", ""),      # A2
        (202, "香草少女 麻豆 完整版", "", ""),                      # A1
        (203, "香草少女 特辑", "", ""),
        (204, "完全无关的作品", "", "国产,高清"),                    # A4
        (205, "香草少女 新作", "", "国产"),                         # A3
        (206, "香草少女 九尾狐狸 jk 合集", "", ""),                  # A7
        (207, "普通作品", "麻豆", "源站标签"),                       # A5：标题不含词库词
    ]
    for vid, name, actor, tag in rows:
        put(SLUG, vid, name, actor, tag)

    related.reload_star_terms()

    print("\n== A/B. rebuild_source 命中与合并回写 ==")
    stats = star_tags.rebuild_source(SLUG, reason="test")
    with database.engine.connect() as conn:
        r201, r202, r204, r205, r206, r207 = (row(conn, v) for v in (201, 202, 204, 205, 206, 207))
        check("A1 标题多词命中写入 vod_tag", r202["vod_tag"] == "香草少女,麻豆", r202["vod_tag"])
        check("A2 有演员行也写标题命中（旧实现完全跳过）",
              r201["vod_tag"] == "香草少女,麻豆", r201["vod_tag"])
        check("A3 源站原生标签保留并合并", r205["vod_tag"] == "国产,香草少女", r205["vod_tag"])
        check("A4 无命中不清空源站标签", r204["vod_tag"] == "国产,高清", r204["vod_tag"])
        check("A5 演员命中只进 star_map，不写 vod_tag",
              r207["vod_tag"] == "源站标签" and mapping(conn, 207) == [("麻豆", "actor")],
              (r207["vod_tag"], mapping(conn, 207)))
        check("A6 src 按词标记（201 全部 title）",
              all(s == "title" for _t, s in mapping(conn, 201)), mapping(conn, 201))
        check("A7 纯 ASCII 短词 jk 被过滤",
              "jk" not in r206["vod_tag"] and "九尾狐狸" in r206["vod_tag"],
              r206["vod_tag"])
        check("A1 多个词都能进 star_map",
              [t for t, _ in mapping(conn, 202)] == ["香草少女", "麻豆"], mapping(conn, 202))

        print("\n== C1. 主表与倒排一致 ==")
        ok = True
        for vid, *_ in rows:
            cur = row(conn, vid)["vod_tag"]
            want = sorted(x.strip() for x in cur.split(",") if x.strip())
            got = inverted(conn, vid)
            if want != got:
                ok = False
                print(f"    vid={vid} tag={cur!r} 倒排={got}")
        check("C1 每个视频 vod_tag 与 video_tags 完全一致", ok)

    print("\n== B1/B2. 幂等与过期段清理 ==")
    stats2 = star_tags.rebuild_source(SLUG, reason="test-again")
    check("B1 重复重建零额外写入", stats2["tagged"] == 0, stats2)
    # 停用「麻豆」+ 给 205 改一个不命中的标题：推断段应被移除，原生段「国产」保留
    with database.write_txn() as conn:
        conn.execute(star_terms.update().where(star_terms.c.term == "麻豆").values(enabled=0))
        t = build_vod_table(SLUG)
        conn.execute(t.update().where(t.c.vod_id == 205).values(name="完全无关的新名字"))
    related.reload_star_terms()
    star_tags.rebuild_source(SLUG, reason="term-disabled")
    with database.engine.connect() as conn:
        check("B2a 停用词后过期推断段被移除、原生段保留",
              row(conn, 205)["vod_tag"] == "国产", row(conn, 205)["vod_tag"])
        check("B2b 麻豆从 202 的 tag 里消失",
              row(conn, 202)["vod_tag"] == "香草少女", row(conn, 202)["vod_tag"])
        check("B2c 倒排跟随清理", inverted(conn, 205) == ["国产"], inverted(conn, 205))

    print("\n== B3. 长度上限 ==")
    long_tag = ",".join(f"标签{i}" for i in range(20))
    merged = star_tags.merge_vod_tag(long_tag, set(), {"新词"})
    check("B3 不超过 255 字符", len(merged) <= 255, len(merged))
    check("B3 不切出半个词", all(seg for seg in merged.split(",")), merged[-40:])
    check("B3 新词在额度内被追加", "新词" in merged, merged[-40:])

    print("\n== C2. 写时推断（同步入库即有标签）==")
    # B2 停用了「麻豆」，这里恢复，避免用例之间互相污染
    with database.write_txn() as conn:
        conn.execute(star_terms.update().where(star_terms.c.term == "麻豆").values(enabled=1))
    related.reload_star_terms()
    config.STAR_TAG_INFER_ON_SYNC = True
    put(SLUG, 301, "香草少女 麻豆 最新作", actor="", tag="")
    with database.engine.connect() as conn:
        check("C2 新片入库即写入标题命中词",
              row(conn, 301)["vod_tag"] == "香草少女,麻豆", row(conn, 301)["vod_tag"])
        check("C2 倒排同步落库",
              inverted(conn, 301) == ["香草少女", "麻豆"], inverted(conn, 301))
    # 源站重推空 tag 不应抹掉写时推断的值
    put(SLUG, 301, "香草少女 麻豆 最新作", actor="", tag="")
    with database.engine.connect() as conn:
        check("C2 重推空 tag 不覆盖本地推断",
              row(conn, 301)["vod_tag"] == "香草少女,麻豆", row(conn, 301)["vod_tag"])

    print("\n== D. 全量重建水位（R1）==")
    with database.engine.connect() as conn:
        before = conn.execute(select(star_rebuild_state.c.last_full_at).where(
            star_rebuild_state.c.slug == SLUG)).scalar()
    star_tags.update_ids(SLUG, [301])
    with database.engine.connect() as conn:
        after = conn.execute(select(star_rebuild_state.c.last_full_at).where(
            star_rebuild_state.c.slug == SLUG)).scalar()
    check("D1 全量重建写入了水位", before is not None)
    check("D2 增量 update_ids 不推进水位（夜间轮转不再被增量污染）", before == after,
          (before, after))

    print("\n== E. 标签索引状态自愈（R5）==")
    # 模拟线上真实情况：面板手动新增源站，已同步数据、写时维护已建好倒排，
    # 但没有 tag_index_state 行（线上 65 个启用站 / 57 行 state，缺 8 行）。
    # 关键取舍：ready() 不能为了这个场景放宽 —— 「手动新增、倒排已齐」与
    # 「索引还没回填完」的数据形态完全一样，放宽就等于废掉 v1.14.0 的灰度
    # 契约（A7：索引未就绪时不声明 tag_search）。状态行是唯一真源，自愈统一
    # 由 main.py 启动引导调用 ensure_state_rows() 完成。
    with database.write_txn() as conn:
        conn.execute(video_tags.delete().where(video_tags.c.source == SLUG2))
        conn.execute(tag_index_state.delete().where(tag_index_state.c.slug == SLUG2))
    put(SLUG2, 401, "香草少女 合集", tag="")
    with database.write_txn() as conn:
        conn.execute(tag_index_state.delete().where(tag_index_state.c.slug == SLUG2))
    tags.invalidate_ready_cache()
    check("E0 缺 state 行时 ready() 不放宽（保住 v1.14.0 灰度契约）",
          tags.ready() is False)
    res = tags.ensure_state_rows()
    check("E1 补齐缺失状态行并标 done", SLUG2 in res.get("filled_done", []), res)
    tags.invalidate_ready_cache()
    check("E2 补齐后 ready() 变 true（tag_search 恢复声明）", tags.ready() is True)
    # 补的必须是「真 done」：processed==total，否则会被 needs_repair() 当成
    # v1.14.0 的历史假 done 行，触发一次没有必要的全量重扫。
    with database.engine.connect() as conn:
        st = conn.execute(
            select(tag_index_state.c.status, tag_index_state.c.processed,
                   tag_index_state.c.total)
            .where(tag_index_state.c.slug == SLUG2)
        ).mappings().first()
    check("E3 补齐的是真 done（processed==total，不误触发自愈回填）",
          st is not None and st["status"] == "done"
          and int(st["processed"] or 0) == int(st["total"] or 0)
          and int(st["total"] or 0) > 0,
          dict(st) if st else None)
    # 语义不能被放宽：连一行倒排都没有的站仍应判未就绪
    with database.write_txn() as conn:
        conn.execute(video_tags.delete().where(video_tags.c.source == SLUG2))
        conn.execute(tag_index_state.delete().where(tag_index_state.c.slug == SLUG2))
    tags.invalidate_ready_cache()
    check("E4 真的没有倒排时 ready() 仍为 false", tags.ready() is False)

    print(f"\n==== {passed} passed / {failed} failed ====")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
