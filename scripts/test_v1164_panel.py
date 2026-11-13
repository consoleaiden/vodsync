# -*- coding: utf-8 -*-
"""
v1.16.4「管理面板性能 + 词库分页批量 + 源站屏蔽」回归测试。

覆盖：
  A. 版本号、两份 openapi 一致、新增端点都登记
  B. 明星词库分页（50/100/200/500、页码夹回、q 筛选、LIKE 转义、ids_only）
  C. 批量删除 / 启用 / 停用（一次事务、一次词库重载、参数校验）
  D. 源站屏蔽（暂停位精确还原、不影响业务列、持久化、无匹配 404）
  E. 只读统计快照缓存（同一 key 只算一次，refresh=1 强制重算）
  F. 合并轮询接口（字段口径与单独接口一致、sections 过滤、降级路径存在）
  G. 表清单不再逐表查 information_schema、行数口径不破
  H. 面板 HTML/JS 接线（分页档位、多选、屏蔽、差量渲染、node --check）
  I. 既有业务接口零回归（/api/v1/sources、/source、/healthz）

运行：python scripts/test_v1164_panel.py
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="vodsync-v1164-"))
os.environ.setdefault(
    "DATABASE_URL", "sqlite:///" + os.path.join(os.environ["DATA_DIR"], "v1164.db")
)
os.environ.setdefault("AUTH_ENABLED", "false")
os.environ.setdefault("FULL_SYNC_ON_DISCOVER", "false")
os.environ.setdefault("AUTO_RESUME_ON_BOOT", "false")
os.environ.setdefault("RELATED_ENABLED", "false")
os.environ.setdefault("TAG_INDEX_ON_STARTUP", "false")
os.environ.setdefault("ADFILTER_ENABLED", "true")

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


def jget(client, path, **kw):
    r = client.get(path, **kw)
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, {}


def jpost(client, path, body=None, **kw):
    r = client.post(path, json=body, **kw)
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, {}


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def main():
    from fastapi.testclient import TestClient
    from sqlalchemy import text

    from app import __version__, config, database, panel, related, star_tags, tablebrowse
    from app.main import app as _app
    from app.models import sources, star_terms

    database.init_db()
    client = TestClient(_app)
    panel.cache_clear()

    # 造 3 个源站（其中 s2 预先被手动暂停，用来验证「取消屏蔽还原暂停位」）
    with database.write_txn() as conn:
        for slug, paused in (("s1", 0), ("s2", 1), ("s3", 0)):
            conn.execute(sources.insert().values(
                name=f"源站{slug}", slug=slug, api_url=f"https://{slug}.example.com/api.php",
                enabled=1, total_videos=1000, paused=paused, auto_sync=1, is_adult=0,
            ))
        conn.execute(text("CREATE TABLE IF NOT EXISTS vod_s1 (vod_id INTEGER PRIMARY KEY, name VARCHAR(255))"))
        conn.execute(text("CREATE TABLE IF NOT EXISTS vod_s3 (vod_id INTEGER PRIMARY KEY, name VARCHAR(255))"))

    print("\nA. 版本与契约")
    ver = tuple(int(x) for x in __version__.split(".")[:3])
    check("A1 版本号 >= 1.16.4", ver >= (1, 16, 4), __version__)
    spec = json.loads(read("openapi.json"))
    spec2 = json.loads(read("docs/openapi.json"))
    check("A2 两份 openapi 完全一致且版本对齐",
          spec == spec2 and spec["info"]["version"] == __version__, spec["info"]["version"])
    for p in ("/api/v1/admin/overview", "/api/v1/admin/source-blocks",
              "/api/v1/star-terms/batch"):
        check(f"A3 openapi 收录 {p}", p in spec["paths"])
    methods = {m.upper() for v in spec["paths"].values() for m in v}
    check("A4 openapi 仍是标准方法集", methods <= {"GET", "POST", "PATCH", "PUT", "DELETE", "HEAD", "OPTIONS", "TRACE"}, methods)
    params = {p["name"] for p in spec["paths"]["/api/v1/star-terms"]["get"]["parameters"]}
    check("A5 star-terms 暴露分页参数", {"page", "page_size", "q", "ids_only"} <= params, sorted(params))
    for p in ("/api/v1/admin/tables", "/api/v1/star/tags/status", "/api/v1/related/status"):
        ps = {x["name"] for x in spec["paths"][p]["get"]["parameters"]}
        check(f"A6 {p} 暴露 refresh", "refresh" in ps, sorted(ps))

    print("\nB. 明星词库分页")
    st, r = jpost(client, "/api/v1/star-terms",
                  {"terms": ["甲", "乙", "丙", "丁", "戊", "己", "庚"], "note": "批次A"})
    check("B1 批量添加成功", st == 200 and r.get("added") == 7, (st, r))
    st, r = jget(client, "/api/v1/star-terms")
    check("B2 不传 page_size 仍是旧行为（全量 + 老字段）",
          st == 200 and len(r["list"]) == 7 and "infer_mode" in r and "active" in r
          and r["page_size"] == 7 and r["pages"] == 1, (st, len(r.get("list", []))))
    ids_all = [x["id"] for x in r["list"]]
    for size in (50, 100, 200, 500):
        st, r2 = jget(client, f"/api/v1/star-terms?page=1&page_size={size}")
        check(f"B3 page_size={size} 生效",
              st == 200 and r2["page_size"] == size and len(r2["list"]) == 7, (st, r2.get("page_size")))
    st, r3 = jget(client, "/api/v1/star-terms?page=99&page_size=50")
    check("B4 越界页码夹回第 1 页", st == 200 and r3["page"] == 1, r3.get("page"))
    st, r4 = jget(client, "/api/v1/star-terms?page=1&page_size=7")
    check("B5 非法档位夹到最近的 50（不是崩 400）",
          st == 200 and r4["page_size"] == 50, r4.get("page_size"))
    # 只留 3 条，便于验证「每页 50」默认场景下的页边界
    with database.write_txn() as conn:
        conn.execute(star_terms.delete().where(star_terms.c.term.in_(["丁", "戊", "己", "庚"])))
        conn.execute(star_terms.update().where(star_terms.c.term == "乙").values(enabled=0))
        conn.execute(star_terms.update().where(star_terms.c.term == "甲").values(aliases="别名甲", note="备注甲"))
    st, r5 = jget(client, "/api/v1/star-terms?page=1&page_size=50")
    check("B6 total / enabled_total 是全库口径而非本页",
          st == 200 and r5["total"] == 3 and r5["enabled_total"] == 2, (r5.get("total"), r5.get("enabled_total")))
    st, r6 = jget(client, "/api/v1/star-terms?q=%E5%88%AB%E5%90%8D%E7%94%B2")
    check("B7 q 命中别名", st == 200 and r6["total"] == 1 and r6["list"][0]["term"] == "甲", r6.get("total"))
    st, r7 = jget(client, "/api/v1/star-terms?q=%E5%A4%87%E6%B3%A8")
    check("B8 q 命中备注", st == 200 and r7["total"] == 1, r7.get("total"))
    st, r8 = jget(client, "/api/v1/star-terms?q=%25")
    check("B9 LIKE 通配符被转义（% 不当通配）", st == 200 and r8["total"] == 0, r8.get("total"))
    st, r9 = jget(client, "/api/v1/star-terms?ids_only=1&q=")
    check("B10 ids_only 返回全部 id 且不返回词条体",
          st == 200 and sorted(r9["ids"]) == sorted([x["id"] for x in r5["list"]])
          and "list" not in r9, (st, r9.get("ids")))
    st, r10 = jget(client, "/api/v1/star-terms?ids_only=1&q=%E7%94%B2")
    check("B11 ids_only 遵循 q 筛选", st == 200 and len(r10["ids"]) == 1, r10.get("ids"))

    print("\nC. 批量管理")
    three = [x["id"] for x in r5["list"]]
    real_reload = related.reload_star_terms
    calls = {"n": 0}

    def counting_reload():
        calls["n"] += 1
        return real_reload()

    related.reload_star_terms = counting_reload
    try:
        st, r = jpost(client, "/api/v1/star-terms/batch",
                      {"action": "disable", "ids": three[:2]})
        check("C1 批量停用 2 条且只 reload 一次",
              st == 200 and r.get("affected") == 2 and calls["n"] == 1, (st, r, calls))
        _, rr = jget(client, "/api/v1/star-terms")
        check("C2 停用落到库里", sum(1 for x in rr["list"] if not x["enabled"]) == 2,
              [x["enabled"] for x in rr["list"]])
        st, r = jpost(client, "/api/v1/star-terms/batch",
                      {"action": "enable", "ids": three[:2]})
        check("C3 批量启用还原", st == 200 and r.get("affected") == 2, (st, r))
        st, r = jpost(client, "/api/v1/star-terms/batch",
                      {"action": "delete", "ids": three[:2] + [999999]})
        check("C4 批量删除只影响存在的 id", st == 200 and r.get("affected") == 2, (st, r))
        _, rr = jget(client, "/api/v1/star-terms")
        check("C5 词库剩 1 条", rr["total"] == 1, rr["total"])
    finally:
        related.reload_star_terms = real_reload
    st, _ = jpost(client, "/api/v1/star-terms/batch", {"action": "bogus", "ids": [1]})
    check("C6 非法 action 400", st == 400, st)
    st, _ = jpost(client, "/api/v1/star-terms/batch", {"action": "delete", "ids": []})
    check("C7 空 ids 400", st == 400, st)
    st, _ = jpost(client, "/api/v1/star-terms/batch", {"action": "delete", "ids": ["x"]})
    check("C8 非整数 ids 400", st == 400, st)
    st, r = jget(client, "/api/v1/star-terms")
    check("C9 批量操作不影响单条接口契约", st == 200 and "infer_mode" in r, st)
    last_id = r["list"][0]["id"]
    # 单条 PATCH/DELETE 仍可用（不因新增批量端点被路由吞掉）
    r_patch = client.patch(f"/api/v1/star-terms/{last_id}", json={"note": "单条仍可用"})
    check("C10 单条 PATCH 仍可用", r_patch.status_code == 200, r_patch.status_code)
    r_del = client.delete(f"/api/v1/star-terms/{last_id}")
    check("C11 单条 DELETE 仍可用", r_del.status_code == 200, r_del.status_code)

    print("\nD. 源站屏蔽")
    st, r = jget(client, "/api/v1/admin/source-blocks")
    check("D1 初始名单为空", st == 200 and r["blocked"] == [], (st, r))
    st, r = jpost(client, "/api/v1/admin/source-blocks", {"blocked": True, "slugs": ["s1", "s2"]})
    check("D2 批量屏蔽 2 个源站", st == 200 and sorted(r["blocked"]) == ["s1", "s2"], (st, r))
    with database.engine.connect() as conn:
        rows = {x[0]: dict(zip(("paused", "enabled", "auto_sync", "is_adult", "total_videos"), x[1:]))
                for x in conn.execute(text(
                    "SELECT slug, paused, enabled, auto_sync, is_adult, total_videos FROM sources"
                )).all()}
    check("D3 屏蔽后两站都被标记暂停", rows["s1"]["paused"] == 1 and rows["s2"]["paused"] == 1, rows)
    check("D4 业务列一字未改",
          rows["s1"]["enabled"] == 1 and rows["s1"]["auto_sync"] == 1
          and rows["s1"]["is_adult"] == 0 and rows["s1"]["total_videos"] == 1000, rows["s1"])
    blocks = panel.get_blocks()
    check("D5 记住了屏蔽前的暂停位（s2 原本就是暂停）",
          blocks["s1"]["paused_before"] == 0 and blocks["s2"]["paused_before"] == 1, blocks)
    check("D6 屏蔽名单持久化在 settings",
          '"s1"' in (database.engine.connect().execute(
              text("SELECT value FROM settings WHERE key = 'panel_blocked_sources'")).scalar() or ""), "")
    st, r = jpost(client, "/api/v1/admin/source-blocks", {"blocked": False, "slugs": ["s1", "s2"]})
    check("D7 取消屏蔽名单清空", st == 200 and r["blocked"] == [], (st, r))
    with database.engine.connect() as conn:
        paused = {x[0]: x[1] for x in conn.execute(text("SELECT slug, paused FROM sources")).all()}
    check("D8 取消屏蔽精确还原暂停位（s1 恢复 / s2 保持暂停）",
          paused["s1"] == 0 and paused["s2"] == 1, paused)
    st, _ = jpost(client, "/api/v1/admin/source-blocks", {"blocked": True, "slugs": ["nope"]})
    check("D9 不存在的 slug 404", st == 404, st)
    st, _ = jpost(client, "/api/v1/admin/source-blocks", {"blocked": True, "slugs": []})
    check("D10 空 slugs 400", st == 400, st)
    st, _ = jpost(client, "/api/v1/admin/source-blocks", {"slugs": ["s1"]})
    check("D11 缺 blocked 400", st == 400, st)
    st, src = jget(client, "/api/v1/sources")
    check("D12 /api/v1/sources 仍返回全部 3 个源站（客户端集合不变）",
          st == 200 and len(src["list"]) == 3, len(src.get("list", [])))
    st, cat = jget(client, "/source")
    check("D13 /source 目录仍返回 3 个源站", st == 200 and len(cat.get("list", [])) == 3,
          len(cat.get("list", [])))

    print("\nE. 只读统计快照缓存")
    # 用固定返回值替换底层统计函数：这里要验证的是「路由层缓存」，不是统计口径
    # 本身（后者由 v1.9.x / v1.12.x 的既有套件覆盖）。
    for key, mod, fn_name, path, stub in (
        ("star.tags.status", star_tags, "status_all", "/api/v1/star/tags/status",
         [{"slug": "s1", "videos": 1, "no_actor_videos": 0, "star_mapped": 0}]),
        ("related.status", related, "coverage_all", "/api/v1/related/status",
         [{"slug": "s1", "videos": 1, "computed": 1, "coverage": 100.0}]),
        ("admin.tables", tablebrowse, "list_browsable", "/api/v1/admin/tables",
         [{"name": "sources", "kind": "system", "label": "sources", "rows": 3, "columns": []}]),
    ):
        panel.cache_clear()
        orig = getattr(mod, fn_name)
        counter = {"n": 0}

        def counted(stub=stub, counter=counter):
            counter["n"] += 1
            return stub

        setattr(mod, fn_name, counted)
        try:
            st1, j1 = jget(client, path)
            st2, j2 = jget(client, path)
            st3, j3 = jget(client, path + "?refresh=1")
            check(f"E {key} 三次请求 200", st1 == st2 == st3 == 200, (st1, st2, st3))
            check(f"E {key} 同 key 只算一次 / refresh 强制重算",
                  counter["n"] == 2, counter)
            check(f"E {key} 返回 cache_age_s 且不改响应主体",
                  "cache_age_s" in j1 and j1.get("list", j1.get("tables")) is not None
                  and j1.get("list", j1.get("tables")) == j2.get("list", j2.get("tables")), "")
        finally:
            setattr(mod, fn_name, orig)
    panel.cache_clear()

    print("\nF. 合并轮询接口")
    st, ov = jget(client, "/api/v1/admin/overview")
    check("F1 一次拿到四份数据 + 屏蔽名单",
          st == 200 and all(k in ov for k in ("stats", "pause", "sources", "logs", "blocks")),
          (st, list(ov)))
    _, s1 = jget(client, "/api/v1/stats")
    check("F2 stats 口径与单独接口一致",
          ov["stats"]["sources_total"] == s1["sources_total"]
          and ov["stats"]["videos_total"] == s1["videos_total"], "")
    _, p1 = jget(client, "/api/v1/pause")
    check("F3 pause 口径与单独接口一致", ov["pause"].get("global") == p1.get("global"), "")
    _, l1 = jget(client, "/api/v1/sources")
    check("F4 sources 口径与单独接口一致", len(ov["sources"]["list"]) == len(l1["list"]), "")
    st, ov2 = jget(client, "/api/v1/admin/overview?sections=stats")
    check("F5 sections 过滤生效（不取 sources/logs）",
          st == 200 and "stats" in ov2 and "sources" not in ov2 and "logs" not in ov2, list(ov2))
    check("F6 blocks 始终返回", "blocks" in ov2, list(ov2))

    print("\nG. 表清单性能不变量")
    real_exists = database.table_exists
    hits = {"n": 0}

    def counting_exists(name):
        hits["n"] += 1
        return real_exists(name)

    database.table_exists = counting_exists
    try:
        items = tablebrowse.list_browsable()
    finally:
        database.table_exists = real_exists
    check("G1 list_browsable 不再逐表查 information_schema", hits["n"] == 0, hits)
    names = {t["name"] for t in items}
    check("G2 系统表与 vod 表都还在",
          {"sources", "sync_logs", "star_terms", "vod_related", "star_map", "vod_s1", "vod_s3"} <= names,
          sorted(names))
    check("G3 settings 仍不可浏览", "settings" not in names, "")
    check("G4 每项仍有 label/rows/columns",
          all(t.get("label") and "rows" in t and "columns" in t for t in items), "")

    print("\nH. 面板 HTML / JS 接线")
    html = read("app/web/index.html")
    js = html[html.index("<script>"):]
    ids = re.findall(r'\bid="([^"]+)"', html)
    check("H1 无重复 id", len(ids) == len(set(ids)), [i for i in set(ids) if ids.count(i) > 1])
    used = set(re.findall(r'\$\("#([\w-]+)"\)', js))
    check("H2 JS 引用的 id 都存在", not (used - set(ids)), sorted(used - set(ids)))
    for v in ("50", "100", "200", "500"):
        check(f"H3 每页档位 {v} 存在", f'<option value="{v}"' in html, "")
    check("H4 默认 50 条", '<option value="50" selected>' in html, "")
    for b in ("btnStarSelAll", "btnStarSelInvert", "btnStarSelNone",
              "btnStarBatchDel", "btnStarBatchOn", "btnStarBatchOff",
              "btnBlockSel", "btnUnblockSel", "btnSourceShowBlocked",
              "btnStarStatsRefresh"):
        check(f"H5 按钮 {b} 有 id", f'id="{b}"' in html, "")
    check("H6 面板走分页接口", "page_size=${size}" in js and "ids_only=1" in js, "")
    check("H7 面板走合并接口 + 保留降级路径",
          "/api/v1/admin/overview?sections=" in js and "refreshAllLegacy" in js
          and "if (secOpen.sources) jobs.push(loadSources())" in js
          and "if (secOpen.logs) jobs.push(loadLogs())" in js, "")
    check("H8 源站表差量渲染短路", "sig === sourcesSig" in js and "innerHTML" in js, "")
    check("H9 屏蔽调用新端点", "/api/v1/admin/source-blocks" in js, "")
    check("H10 表清单强制刷新带 refresh=1", "?refresh=1" in js, "")
    check("H11 折叠区块仍首展才加载", "_loaded" in js and "secLoadOpen()" in js, "")
    if shutil.which("node"):
        p = os.path.join(tempfile.gettempdir(), "v1164_syntax.js")
        with open(p, "w", encoding="utf-8") as f:
            f.write(html[html.index("<script>") + len("<script>"):html.rindex("</script>")])
        r = subprocess.run(["node", "--check", p], capture_output=True, text=True, encoding="utf-8")
        check("H12 node --check 通过", r.returncode == 0, (r.stderr or "")[:500])

    print("\nI. 既有业务接口零回归")
    st, h = jget(client, "/healthz")
    check("I1 /healthz 版本对齐", st == 200 and h.get("version") == __version__, h.get("version"))
    st, _ = jget(client, "/api/v1/pause")
    check("I2 /api/v1/pause 正常", st == 200, st)
    st, _ = jget(client, "/api/v1/logs?limit=20")
    check("I3 /api/v1/logs 正常", st == 200, st)
    st, _ = jget(client, "/api/v1/admin/tables/sources/rows?page=1&page_size=3")
    check("I4 表浏览分页正常", st == 200, st)
    st, s = jget(client, "/api/v1/stats")
    check("I5 /api/v1/stats 字段未缩水",
          st == 200 and {"sources_total", "videos_total", "today_inserted", "db_pool"} <= set(s),
          sorted(s)[:12])

    print(f"\n{'=' * 52}\n==== v1.16.4 管理面板测试: {passed} passed, {failed} failed ====")
    return failed


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
