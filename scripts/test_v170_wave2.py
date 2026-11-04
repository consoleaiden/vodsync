"""
v1.7.0 数据层测试（Wave 2）。

覆盖：
  A. 多源列表：并行 top-K 归并 vs 全量 UNION，逐页逐条比对必须完全一致
     - 交错时间、同时间不同 id、空表、不存在的表
     - page_size 不能整除总数时的末页
     - detail 模式（content / play_url 两段式补取）
     - 筛选条件（wd / type_name / type_id）下的一致性
     - LIKE 通配符转义（wd 里带 % 不该变成全表匹配）
     - total 缓存命中 / can_merge 边界 / 翻页过深走 union 路径
  B. sync_logs 清理：按天数、按每源站保留条数、总行数上限，running 行不删
  C. WAL checkpoint 在非 SQLite 下是 no-op，SQLite 下能跑通
  D. 源站 slug 碰撞：两个同名 ASCII 站必须各自入库、各自建表，api_url 不能互相覆盖

运行：python scripts/test_v170_wave2.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="vodsync-wave2-")
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(os.environ["DATA_DIR"], "w2.db")
os.environ["AUTH_ENABLED"] = "false"
os.environ["FULL_SYNC_ON_DISCOVER"] = "false"
os.environ["MERGE_MAX_PER_TABLE"] = "20"     # 让「翻页过深回退」在测试里可触发
os.environ["TOTAL_CACHE_SECONDS"] = "60"

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
    from datetime import datetime, timedelta

    from fastapi.testclient import TestClient
    from sqlalchemy import delete, insert, select, func

    from app import config, database, housekeeping, multiquery, syncer
    from app.main import app
    from app.models import build_vod_table, sources, sync_logs

    database.init_db()

    # ---------------- 准备数据 ----------------
    # 三个源站：t1 6 条、t2 9 条、t3 只有表没有数据。
    # 时间故意交错，并造两组「同 vod_time 不同 vod_id」，验证二级排序。
    plan = {
        "t1": [
            (1, "2026-11-01 10:00:00", "剧A"),
            (2, "2026-11-01 12:00:00", "剧B"),
            (3, "2026-11-02 09:00:00", "剧C"),
            (4, "2026-11-02 09:00:00", "剧D"),   # 与 3 同时间
            (5, "2026-11-03 08:00:00", "剧E"),
            (6, "2026-11-04 08:00:00", "剧F"),
        ],
        "t2": [
            (101, "2026-11-01 09:00:00", "片A"),
            (102, "2026-11-01 11:00:00", "片B"),
            (103, "2026-11-02 09:00:00", "片C"),  # 与 t1 的 3/4 同时间
            (104, "2026-11-02 10:00:00", "片D"),
            (105, "2026-11-02 11:00:00", "片E"),
            (106, "2026-11-03 12:00:00", "片F"),
            (107, "2026-11-03 13:00:00", "片G"),
            (108, "2026-11-04 07:00:00", "片H"),
            (109, "2026-11-04 09:00:00", "片I"),
        ],
        "t3": [],
    }
    with database.write_txn() as conn:
        for slug, rows in plan.items():
            conn.execute(
                sources.insert().values(
                    name=f"测试站{slug}", slug=slug,
                    api_url=f"http://{slug}.example.com/api.php/provide/vod/",
                    status="ok", enabled=1, auto_sync=1,
                )
            )
    for slug, rows in plan.items():
        syncer.ensure_table(slug)
        table = build_vod_table(slug)
        if rows:
            with database.write_txn() as conn:
                conn.execute(
                    insert(table),
                    [
                        {
                            "vod_id": vid, "type_id": 6 if vid % 2 else 1,
                            "type_name": "国产剧" if vid % 2 else "电影",
                            "name": name, "pic": f"http://p/{vid}.jpg",
                            "remarks": "HD", "content": f"简介{vid}",
                            "play_url": f"第01集$http://s/{vid}.m3u8", "vod_time": t,
                        }
                        for vid, t, name in rows
                    ],
                )

    slugs = ["t1", "t2", "t3"]

    print("[A1] 归并与全量 UNION 逐页一致")
    # 全量基准：直接从 union_page 拿（它内部就是 SQL 排序，作为参照实现）
    base_all = []
    for pg in range(1, 20):
        r = multiquery.union_page(slugs, pg, 4)
        if not r["rows"]:
            break
        base_all += [(x["source_slug"], x["vod_id"]) for x in r["rows"]]

    merged_all = []
    for pg in range(1, 20):
        r = multiquery.merged_page(slugs, pg, 4)
        if not r["rows"]:
            break
        merged_all += [(x["source_slug"], x["vod_id"]) for x in r["rows"]]

    check("总数一致 (15)", len(base_all) == 15 and len(merged_all) == 15,
          f"union={len(base_all)} merge={len(merged_all)}")
    check("逐条顺序完全一致", base_all == merged_all,
          f"first diff={next(((i, a, b) for i, (a, b) in enumerate(zip(base_all, merged_all)) if a != b), 'none')}")
    check("空表 t3 不影响结果", not any(s == "t3" for s, _ in merged_all))
    check("同时间按 vod_id 倒序", merged_all.index(("t1", 4)) < merged_all.index(("t1", 3)),
          str(merged_all))

    print("[A2] page_size 边界")
    r1 = multiquery.merged_page(slugs, 1, 7)
    r2 = multiquery.merged_page(slugs, 3, 7)
    r3 = multiquery.merged_page(slugs, 4, 7)
    check("首页 7 条", len(r1["rows"]) == 7, len(r1["rows"]))
    check("末页 1 条（15 = 7+7+1）", len(r2["rows"]) == 1, len(r2["rows"]))
    check("越界页返回空", r3["rows"] == [], r3["rows"])
    check("pagecount = 3", r1["pagecount"] == 3, r1["pagecount"])

    print("[A3] detail 模式补 content/play_url")
    r = multiquery.merged_page(slugs, 1, 5, detail=True)
    check("detail 行含 content", all(x.get("content") for x in r["rows"]), str(r["rows"][:1]))
    check("detail 行含 play_url", all("m3u8" in (x.get("play_url") or "") for x in r["rows"]))
    plain = multiquery.merged_page(slugs, 1, 5, detail=False)
    check("非 detail 不拉大字段", "content" not in plain["rows"][0], str(plain["rows"][0].keys()))

    print("[A4] 筛选条件一致性")
    for name, kw in [
        ("按名称", dict(wd="剧")),
        ("按分类名", dict(type_name="电影")),
        ("按分类ID", dict(type_id=6)),
    ]:
        a = [(x["source_slug"], x["vod_id"]) for x in multiquery.union_page(slugs, 1, 50, **kw)["rows"]]
        b = [(x["source_slug"], x["vod_id"]) for x in multiquery.merged_page(slugs, 1, 50, **kw)["rows"]]
        check(f"{name}筛选结果一致 ({len(a)} 条)", a == b, f"union={a} merge={b}")

    print("[A5] LIKE 通配符转义")
    r = multiquery.merged_page(["t1"], 1, 50, wd="%")
    check("wd=% 不当成通配符", r["total"] == 0, r["total"])
    r = multiquery.merged_page(["t1"], 1, 50, wd="剧")
    check("wd=剧 正常匹配 6 条", r["total"] == 6, r["total"])

    print("[A6] total 缓存与翻页上限")
    multiquery.reset_total_cache()
    a = multiquery.merged_page(slugs, 1, 4)
    b = multiquery.merged_page(slugs, 2, 4)
    check("首次未命中缓存", a["total_cached"] is False, a["total_cached"])
    check("第二次命中缓存", b["total_cached"] is True, b["total_cached"])
    check("缓存下 total 一致", a["total"] == b["total"] == 15, (a["total"], b["total"]))
    check("can_merge: 第 1 页(上限20)", multiquery.can_merge(1, 4) is True)
    check("can_merge: 24>20 超出", multiquery.can_merge(6, 4) is False, "6*4=24 > 20")
    # 翻页过深：走 union 路径仍要正确
    deep = multiquery.union_page(slugs, 3, 4)
    check("深页回退 union 结果正确",
          [(x["source_slug"], x["vod_id"]) for x in deep["rows"]] == base_all[8:12],
          str([(x["source_slug"], x["vod_id"]) for x in deep["rows"]]))

    print("[A7] API 层接线")
    client = TestClient(app)
    r = client.get("/api/v1/videos?page=1&page_size=5")
    body = r.json()
    check("不带 source 返回 200", r.status_code == 200, r.status_code)
    check("API 总数 15", body.get("total") == 15, body.get("total"))
    check("API 返回 query_ms", isinstance(body.get("query_ms"), int), body.keys())
    check("API 列表项字段完整",
          set(body["list"][0]) == {"source", "vod_id", "type_id", "type_name",
                                   "vod_name", "vod_pic", "vod_remarks", "vod_time"},
          str(body["list"][0].keys()))
    r = client.get("/api/v1/videos?page=1&page_size=3&ac=detail")
    check("ac=detail 带播放地址",
          r.status_code == 200 and "vod_play_list" in r.json()["list"][0], r.status_code)
    r = client.get("/api/v1/videos?source=t1&page=1&page_size=3")
    check("指定单源仍正确", r.json()["total"] == 6, r.json().get("total"))

    print("[B] sync_logs 清理")
    now = datetime.now()
    with database.write_txn() as conn:
        conn.execute(delete(sync_logs))
        rows = []
        # t1: 1 条很老的 + 8 条近期的（每源站保留 50 条 → 都不该被「条数」删掉）
        rows.append(dict(slug="t1", mode="full", status="success",
                         started_at=now - timedelta(days=90), finished_at=now - timedelta(days=90)))
        for i in range(8):
            rows.append(dict(slug="t1", mode="incremental", status="success",
                             started_at=now - timedelta(days=i), finished_at=now))
        # t2: 70 条近期（超过每源站 50 条 → 只保留最近 50）
        for i in range(70):
            rows.append(dict(slug="t2", mode="incremental", status="success",
                             started_at=now - timedelta(minutes=i), finished_at=now))
        # 一条 running，任何策略都不能删（批量 insert 要求每行键集一致，故显式给 None）
        rows.append(dict(slug="t3", mode="full", status="running",
                         started_at=now, finished_at=None))
        conn.execute(insert(sync_logs), rows)

    stats = housekeeping.prune_sync_logs()
    check("清理返回被删行数", stats["deleted"] >= 1, stats)
    with database.engine.connect() as conn:
        remain_t1 = conn.execute(select(func.count()).select_from(sync_logs)
                                 .where(sync_logs.c.slug == "t1")).scalar()
        remain_t2 = conn.execute(select(func.count()).select_from(sync_logs)
                                 .where(sync_logs.c.slug == "t2")).scalar()
        running = conn.execute(select(func.count()).select_from(sync_logs)
                              .where(sync_logs.c.status == "running")).scalar()
        oldest_t1 = conn.execute(select(func.min(sync_logs.c.started_at))
                                 .where(sync_logs.c.slug == "t1")).scalar()
    check("超期日志被删（t1 90 天前那条）", remain_t1 == 8, remain_t1)
    check("t1 保留条数 > 保留天数策略时按天数", oldest_t1 >= now - timedelta(days=31), oldest_t1)
    check("t2 每源站最多保留 50 条", remain_t2 == config.SYNC_LOG_KEEP_PER_SOURCE, remain_t2)
    check("running 日志不被删", running == 1, running)

    print("[C] WAL checkpoint")
    result = housekeeping.checkpoint_wal()
    check("SQLite 下能执行", result.get("engine") == "sqlite", result)
    check("返回 wal 大小字段", "wal_mb" in result, result)

    print("[D] 源站 slug 碰撞")
    # 两个纯 ASCII 站名 slugify 后相同（"HD 资源" 与 "HD-资源" 都 -> "hd"）
    from app.models import slugify
    check("两个站名 slugify 相同（前提成立）",
          slugify("HD 资源") == slugify("HD-资源") == "hd",
          f"{slugify('HD 资源')} vs {slugify('HD-资源')}")
    fake = [
        {"name": "HD 资源", "api": "http://hd1.example.com/api.php/provide/vod/", "status": "ok"},
        {"name": "HD-资源", "api": "http://hd2.example.com/api.php/provide/vod/", "status": "ok"},
    ]

    class _FakeClient:
        def _get_json(self, _url):
            return {"resources": fake}

        def close(self):
            pass

    new_slugs = syncer.refresh_sources(client=_FakeClient())
    check("两个站都入库（不再被吞掉）", len(new_slugs) == 2, new_slugs)
    check("第二个站获得去重后缀", new_slugs == ["hd", "hd_2"], new_slugs)
    with database.engine.connect() as conn:
        got = {r["slug"]: r["api_url"] for r in conn.execute(
            select(sources.c.slug, sources.c.api_url).where(sources.c.slug.in_(["hd", "hd_2"]))
        ).mappings().all()}
    check("两个 api_url 各自独立",
          got.get("hd") == "http://hd1.example.com/api.php/provide/vod/" and
          got.get("hd_2") == "http://hd2.example.com/api.php/provide/vod/", got)
    check("hd_2 的 slug 能通过 API 校验",
          client.get("/api/v1/videos?source=hd_2&page=1").status_code in (200, 404),
          "不应 400")
    # 幂等：再刷一次不应新增
    again = syncer.refresh_sources(client=_FakeClient())
    check("重复刷新不新增源站", again == [], again)
    with database.engine.connect() as conn:
        n = conn.execute(select(func.count()).select_from(sources)
                         .where(sources.c.slug.in_(["hd", "hd_2", "hd_3"]))).scalar()
    check("不产生 hd_3", n == 2, n)

    print()
    print("=" * 46)
    print(f"结果：{passed} 通过 / {failed} 失败")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
