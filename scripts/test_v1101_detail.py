"""
v1.10.1 详情字段透出（vod_actor / vod_tag / vod_area / vod_year / vod_director）测试。

背景：这些列 v1.9.0 起就在库里，但 `_detail_item` 一直没透出 —— NAS 模式下客户端
拿不到演员与标签，演员只能靠 `star/related` 的 stars 兜底，标签没有任何数据源、
整行隐藏。

为什么单独有这份测试：
`_detail_item` 有 3 个调用点，其中「多源归并列表」的行是 multiquery 组装的 dict，
不是完整表行 —— 直接 `row["vod_actor"]` 会在那一路上抛 KeyError（搜索/列表 500）。
所以这里除了验字段值，还要验「行里没有这些列时也不炸」，以及「字段永远存在且为 str」。

覆盖项：
 A. 单条详情
    A1 200 且 5 个字段值正确
    A2 5 个字段都是 str（None 也要变成 ""，客户端不用判 null）
    A3 该行为空字段时（无演员/无标签）字段仍存在、值为 ""
    A4 库里的 NULL 也返回 ""（老库/脏数据）
 B. 其它两条路径
    B1 `?ids=` 批量详情带同样字段
    B2 `?ac=detail` 归并路径带同样字段（值非空）
    B3 `?ac=detail` 深翻页走 UNION 路径也带同样字段
    B4 列表模式（不带 detail）**不带**这 5 个键（体积控制，别偷偷全量下发）
 C. 容错（本次改动的核心风险）
    C1 `_pick` 对「完全没有这些列」的 dict 返回 ""，不抛异常
    C2 用这种瘦行调用 `_detail_item` 不抛异常（模拟归并路径的旧行）
    C3 源码级：5 个字段都走 `_pick`，没有一处 `row["vod_actor"]`
 D. 回归
    D1 不存在的 vod_id → 404
    D2 未知 slug → 404
    D3 原有字段（vod_name/vod_pic/vod_content/vod_play_list）未被破坏

运行：python scripts/test_v1101_detail.py
"""
import inspect
import os
import sys
import tempfile

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="vodsync-v1101-")
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(os.environ["DATA_DIR"], "v1101.db")
os.environ["AUTH_ENABLED"] = "false"
os.environ["FULL_SYNC_ON_DISCOVER"] = "false"
os.environ["AUTO_RESUME_ON_BOOT"] = "false"
os.environ["RELATED_ENABLED"] = "true"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text as _text  # noqa: E402

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


SLUG = "t1101"
FIELDS = ("vod_actor", "vod_director", "vod_area", "vod_year", "vod_tag")

# vod_id, name, type_name, area, year, actor, director, tag, time
SAMPLES = [
    (201, "流浪地球", "科幻片", "中国大陆", "2019",
     "吴京,屈楚萧,李光洁", "郭帆", "吴京", "2026-02-01 00:00:00"),
    (202, "流浪地球2", "科幻片", "中国大陆", "2023",
     "吴京,刘德华", "郭帆", "吴京,刘德华", "2026-02-02 00:00:00"),
    (203, "孤舟", "纪录片", "", "", "", "", "", "2026-02-03 00:00:00"),
    (204, "星际穿越", "科幻片", "美国", "2014",
     "马修·麦康纳", "诺兰", "", "2026-02-04 00:00:00"),
]


def main():
    from fastapi.testclient import TestClient

    from app import config, database, syncer
    from app.main import app
    from app.models import sources
    from app.routes.api import _detail_item, _pick

    database.init_db()
    client = TestClient(app)

    with database.write_txn() as conn:
        conn.execute(sources.insert().values(
            name="测试源1101", slug=SLUG, api_url="https://example.com/api.php",
            status="ok", enabled=1, auto_sync=1, origin="custom", total_videos=len(SAMPLES),
        ))
    syncer.ensure_table(SLUG)
    syncer.upsert_batch(SLUG, [{
        "vod_id": vid, "type_id": 1, "type_name": tname, "name": name,
        "pic": f"pic/{vid}.jpg", "remarks": "HD",
        "content": "简介" * 3, "play_url": "第01集$https://e/1.m3u8",
        "vod_actor": actor, "vod_director": director, "vod_writer": "",
        "vod_area": area, "vod_year": year, "vod_tag": tag, "vod_time": ts,
    } for vid, name, tname, area, year, actor, director, tag, ts in SAMPLES])

    # ==================================================================
    print("\n[A] 单条详情 /api/v1/videos/{slug}/{vod_id}")
    # ==================================================================
    # 详情接口外层包了一层 {code, detail}（客户端读 data.detail.*）
    r = client.get(f"/api/v1/videos/{SLUG}/201")
    check("A1 详情 200", r.status_code == 200, r.status_code)
    d = r.json().get("detail") or {}
    check("A1 vod_actor 有值", d.get("vod_actor") == "吴京,屈楚萧,李光洁", d.get("vod_actor"))
    check("A1 vod_director 有值", d.get("vod_director") == "郭帆", d.get("vod_director"))
    check("A1 vod_area 有值", d.get("vod_area") == "中国大陆", d.get("vod_area"))
    check("A1 vod_year 有值", d.get("vod_year") == "2019", d.get("vod_year"))
    check("A1 vod_tag 有值", d.get("vod_tag") == "吴京", d.get("vod_tag"))
    check("A2 5 个字段都是 str 且非 None",
          all(isinstance(d.get(f), str) for f in FIELDS),
          {f: type(d.get(f)).__name__ for f in FIELDS})

    d3 = client.get(f"/api/v1/videos/{SLUG}/203").json().get("detail") or {}
    check("A3 空字段行：5 个字段仍存在（不缺 key）",
          all(f in d3 for f in FIELDS), [f for f in FIELDS if f not in d3])
    check("A3 空字段行：值为空串", all(d3.get(f) == "" for f in FIELDS),
          {f: d3.get(f) for f in FIELDS})

    with database.write_txn() as conn:
        conn.execute(_text(
            f"update vod_{SLUG} set vod_actor = NULL, vod_tag = NULL where vod_id = 204"
        ))
    d4 = client.get(f"/api/v1/videos/{SLUG}/204").json().get("detail") or {}
    check("A4 库里 NULL → 返回空串（不是 None）",
          d4.get("vod_actor") == "" and d4.get("vod_tag") == "",
          (d4.get("vod_actor"), d4.get("vod_tag")))

    # ==================================================================
    print("\n[B] 其它三条路径")
    # ==================================================================
    r = client.get(f"/api/v1/videos?source={SLUG}&ids=201")
    items = r.json().get("list") or []
    check("B1 ids 批量详情：带 5 个字段",
          bool(items) and all(items[0].get(f) == v for f, v in zip(
              FIELDS, ("吴京,屈楚萧,李光洁", "郭帆", "中国大陆", "2019", "吴京"))),
          items[0] if items else r.text[:160])

    r = client.get(f"/api/v1/videos?source={SLUG}&ac=detail&page=1&page_size=10")
    items = r.json().get("list") or []
    b2 = next((x for x in items if x["vod_id"] == 202), None)
    check("B2 ac=detail 归并路径：带 5 个字段且非空",
          bool(b2) and all(b2.get(f) for f in FIELDS),
          b2 if b2 else r.text[:160])
    check("B2 归并路径取值正确",
          bool(b2) and b2.get("vod_tag") == "吴京,刘德华" and b2.get("vod_area") == "中国大陆",
          b2)

    # 深翻页：让 can_merge 返回 False，强制走 union_page
    old_merge_max = config.MERGE_MAX_PER_TABLE
    config.MERGE_MAX_PER_TABLE = 1
    try:
        r = client.get(f"/api/v1/videos?source={SLUG}&ac=detail&page=2&page_size=2")
        body = r.json()
        items = body.get("list") or []
        check("B3 深翻页走 UNION（列表非空）", r.status_code == 200 and len(items) > 0,
              (r.status_code, body.get("strategy"), len(items)))
        check("B3 UNION 路径：带 5 个字段且非空",
              all(all(it.get(f) for f in FIELDS) for it in items),
              items)
    finally:
        config.MERGE_MAX_PER_TABLE = old_merge_max

    r = client.get(f"/api/v1/videos?source={SLUG}&page=1&page_size=10")
    items = r.json().get("list") or []
    check("B4 列表模式不带这 5 个键（体积控制）",
          bool(items) and not any(f in items[0] for f in FIELDS),
          items[0] if items else None)

    # ==================================================================
    print("\n[C] 容错：行里没有这些列时不能炸")
    # ==================================================================
    thin = {"vod_id": 901, "type_id": 1, "type_name": "科幻片", "name": "瘦行",
            "pic": "", "remarks": "HD", "vod_time": "", "content": "", "play_url": ""}
    check("C1 _pick 缺列返回空串", _pick(thin, "vod_actor") == "", "")
    check("C1 _pick 值为 None 返回空串", _pick({"vod_actor": None}, "vod_actor") == "", "")
    try:
        item = _detail_item(SLUG, thin)
        ok, err = True, None
    except Exception as e:      # noqa: BLE001
        item, ok, err = {}, False, repr(e)
    check("C2 瘦行调用 _detail_item 不抛异常", ok, err)
    check("C2 瘦行：5 个字段存在且为 \"\"",
          ok and all(item.get(f) == "" for f in FIELDS), item)

    src = inspect.getsource(_detail_item)
    check("C3 源码里没有 row[\"vod_actor\"] 这类硬取",
          all(f'row["{f}"]' not in src and f"row['{f}']" not in src for f in FIELDS), "")
    check("C3 5 个字段都走 _pick", all(f'_pick(row, "{f}")' in src for f in FIELDS), "")

    # ==================================================================
    print("\n[D] 回归")
    # ==================================================================
    check("D1 不存在的 vod_id → 404",
          client.get(f"/api/v1/videos/{SLUG}/99999").status_code == 404, "")
    check("D2 未知 slug → 404",
          client.get("/api/v1/videos/nosuchslug/201").status_code == 404, "")

    d = client.get(f"/api/v1/videos/{SLUG}/201").json().get("detail") or {}
    check("D3 原有字段未被破坏",
          d.get("vod_name") == "流浪地球" and d.get("vod_pic") == "pic/201.jpg"
          and d.get("vod_content") and d.get("vod_play_from") == "vodsync"
          and isinstance(d.get("vod_play_list"), list),
          {k: d.get(k) for k in ("vod_name", "vod_pic", "vod_play_from")})
    check("D3 source 字段仍是请求的 slug", d.get("source") == SLUG, d.get("source"))

    print("\n" + "=" * 50)
    print(f"通过 {passed} / 失败 {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
