"""
v1.12.0 管理面板「数据表浏览」测试。

功能：面板只读查看数据库内容 —— 切表、按字段/全表搜索、翻页、每页条数、页码跳转。
后端两个接口：
  GET /api/v1/admin/tables            可浏览表清单（含列元信息与行数）
  GET /api/v1/admin/tables/{t}/rows   分页浏览（page / page_size / field / q）

覆盖项：
 A. 表清单：内置表 + vod_{slug} 动态表；行数口径（系统表实时 count、vod 表用
    sources.total_videos 缓存值）；settings 有意不在清单里（存密码哈希/密钥）
 B. 分页：无过滤翻页、每页条数、pages 计算、页码越界夹回 [1, pages]
 C. 按字段搜索：文本列 / 整数列（cast）/ 大小写不敏感
 D. 全表搜索（不选字段）：所有列 OR，含时间列按字符串匹配
 E. LIKE 转义：% _ \\ 不当通配符
 F. 安全：坏表名 / 不存在的 vod 表 -> 404；坏字段名 -> 400；settings -> 404
    （表名列名永不拼接用户输入，关键词走 bind param）
 G. 接口参数校验：page/page_size 越界 -> 422（FastAPI Query 边界）
 H. 回归：既有接口不受影响（/api/v1/sources 正常）

运行：python scripts/test_v1120_tablebrowse.py
"""
import os
import sys
import tempfile

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="vodsync-v1120-")
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(os.environ["DATA_DIR"], "v1120.db")
os.environ["AUTH_ENABLED"] = "false"
os.environ["FULL_SYNC_ON_DISCOVER"] = "false"
os.environ["AUTO_RESUME_ON_BOOT"] = "false"
os.environ["RELATED_ENABLED"] = "false"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


SLUG = "t1120"


def main():
    from fastapi.testclient import TestClient

    from app import database, syncer, tablebrowse
    from app.main import app
    from app.models import sources, sync_logs, star_terms

    database.init_db()
    client = TestClient(app)

    with database.write_txn() as conn:
        for i in range(1, 8):  # 7 行，方便测分页边界（3 条/页 -> 3 页）
            conn.execute(sources.insert().values(
                name=f"测试源站{i}", slug=f"s{i}", api_url="https://example.com/api.php",
                status="ok", enabled=i % 2, total_videos=100 + i,
            ))
        conn.execute(sync_logs.insert().values(slug="s1", mode="full", status="success"))
        conn.execute(star_terms.insert().values(term="张三", note="测试词条"))

    syncer.ensure_table(SLUG)
    syncer.upsert_batch(SLUG, [
        {"vod_id": vid, "type_id": ttype, "type_name": tname, "name": name,
         "pic": "", "remarks": remarks, "content": f"简介{vid}", "play_url": f"第1集$https://x/{vid}.m3u8",
         "vod_time": f"2026-11-0{vid} 00:00:00"}
        for vid, (ttype, tname, name, remarks) in enumerate([
            (1, "国产剧", "三体", "更新至30集"),
            (2, "电影", "流浪地球2", "HD国语"),
            (3, "电影", "满江红", "TC"),
        ], start=1)
    ])

    # ---- A. 表清单 ----
    r = client.get("/api/v1/admin/tables")
    check("A1 表清单 200", r.status_code == 200, r.status_code)
    tables = {t["name"]: t for t in r.json()["tables"]}
    check("A2 内置表在清单里",
          all(n in tables for n in ("sources", "sync_logs", "star_terms", "vod_related", "star_map")),
          list(tables))
    check("A3 settings 有意排除", "settings" not in tables, list(tables))
    check("A4 vod 动态表在清单里", SLUG in tables or f"vod_{SLUG}" in tables, list(tables))
    vod = tables.get(f"vod_{SLUG}")
    check("A5 vod 表行数用 total_videos 缓存值", vod and vod["rows"] == 0,
          vod and vod["rows"])  # upsert 不更新 total_videos，缓存值就是 0 —— 口径如此
    check("A6 列元信息带类型", vod and any(c["type"].startswith("VARCHAR") for c in vod["columns"]),
          vod and vod["columns"][:2])
    src = tables["sources"]
    check("A7 系统表行数实时 count", src["rows"] == 7, src["rows"])

    # ---- B. 分页 ----
    r = client.get("/api/v1/admin/tables/sources/rows?page=2&page_size=3").json()
    check("B1 total 正确", r["total"] == 7, r["total"])
    check("B2 pages 计算", r["pages"] == 3, r["pages"])
    check("B3 第 2 页 3 行且按主键稳定排序", [x["id"] for x in r["rows"]] == [4, 5, 6],
          [x["id"] for x in r["rows"]])
    r = client.get("/api/v1/admin/tables/sources/rows?page=99&page_size=3").json()
    check("B4 页码越界夹回末页", r["page"] == 3 and len(r["rows"]) == 1, (r["page"], len(r["rows"])))
    r = client.get("/api/v1/admin/tables/sources/rows?page=1&page_size=100").json()
    check("B5 page_size=100 全量", len(r["rows"]) == 7, len(r["rows"]))

    # ---- C. 按字段搜索 ----
    # name 造的是 f"测试源站{i}"（i=1..7），关键词「源站1」只命中「测试源站1」
    r = client.get("/api/v1/admin/tables/sources/rows?field=name&q=源站1").json()
    check("C1 文本列部分匹配", r["total"] == 1 and r["rows"][0]["name"] == "测试源站1",
          (r["total"], [x["name"] for x in r["rows"]]))
    # enabled = i%2 -> 1,0,1,0,1,0,1，值为 1 的有 4 条（整数列走 cast 后匹配）
    r = client.get("/api/v1/admin/tables/sources/rows?field=enabled&q=1").json()
    check("C2 整数列 cast 后可搜", r["total"] == 4, r["total"])
    r = client.get("/api/v1/admin/tables/sources/rows?field=slug&q=S1").json()
    check("C3 大小写不敏感", r["total"] == 1 and r["rows"][0]["slug"] == "s1", r["total"])

    # ---- D. 全表搜索 ----
    r = client.get("/api/v1/admin/tables/sources/rows?q=s1").json()
    check("D1 全表搜索（slug 命中）", r["total"] == 1, r["total"])
    r = client.get(f"/api/v1/admin/tables/vod_{SLUG}/rows?q=2026-11").json()
    check("D2 时间列按字符串命中", r["total"] == 3, r["total"])
    r = client.get(f"/api/v1/admin/tables/vod_{SLUG}/rows?field=type_name&q=电影").json()
    check("D3 vod 表字段搜索", r["total"] == 2 and all(x["type_name"] == "电影" for x in r["rows"]), r["total"])

    # ---- E. LIKE 转义 ----
    r = client.get("/api/v1/admin/tables/sources/rows?field=slug&q=%").json()
    check("E1 % 不当通配符", r["total"] == 0, r["total"])
    r = client.get("/api/v1/admin/tables/sources/rows?field=slug&q=_").json()
    check("E2 _ 不当通配符", r["total"] == 0, r["total"])
    r = client.get("/api/v1/admin/tables/sources/rows?field=api_url&q=https%3A%2F%2Fexample.com").json()
    check("E3 正常关键词仍可命中", r["total"] == 7, r["total"])

    # ---- F. 安全 ----
    check("F1 坏表名 404", client.get("/api/v1/admin/tables/sources;drop/rows").status_code == 404)
    check("F2 不存在的 vod 表 404",
          client.get("/api/v1/admin/tables/vod_nosuch/rows").status_code == 404)
    check("F3 settings 不可浏览 404",
          client.get("/api/v1/admin/tables/settings/rows").status_code == 404)
    check("F4 未知字段 400",
          client.get("/api/v1/admin/tables/sources/rows?field=id;drop&q=x").status_code == 400)
    # 注入式关键词只是普通字符串，不报错也不删表
    r = client.get("/api/v1/admin/tables/sources/rows?q=';drop table sources;--").json()
    check("F5 注入关键词被当普通文本", r["total"] == 0 and client.get("/api/v1/sources").status_code == 200,
          r["total"])

    # ---- G. 参数校验 ----
    check("G1 page=0 -> 422", client.get("/api/v1/admin/tables/sources/rows?page=0").status_code == 422)
    check("G2 page_size=101 -> 422",
          client.get("/api/v1/admin/tables/sources/rows?page_size=101").status_code == 422)
    check("G3 合法 page_size=50 -> 200",
          client.get("/api/v1/admin/tables/sources/rows?page_size=50").status_code == 200)

    # ---- H. 回归 ----
    check("H1 既有接口不受影响", client.get("/api/v1/sources").status_code == 200)
    check("H2 healthz 正常", client.get("/healthz").status_code == 200)
    # 模块级直接调用与接口返回一致（接口是薄封装）
    direct = tablebrowse.browse("sources", 1, 3, "", "")
    api = client.get("/api/v1/admin/tables/sources/rows?page=1&page_size=3").json()
    check("H3 模块与接口同构", direct["total"] == api["total"] and len(direct["rows"]) == len(api["rows"]))

    print()
    print("=" * 50)
    print(f"通过 {passed} / 失败 {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
