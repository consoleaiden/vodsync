# -*- coding: utf-8 -*-
"""
v1.15.0「源站 18+ 标记与未成年模式」回归测试。

覆盖：
  A. 数据兼容：迁移补列、存量源站默认「否」、NULL/脏值按「否」、版本与能力声明
  B. 标记管理：面板保存端点、参数校验、标记下发、立即生效（无缓存窗口）
  C. 现有调用零影响：不带开关时源站清单/列表/搜索/分类/详情/MacCMS 全部照旧
  D. 未成年模式：Header 通道下所有内容入口被屏蔽，显式点名 18+ 站也不能绕过
  E. 查询参数通道与优先级：minor_mode / minor 别名、头优先、显式 0 可关
  F. 关闭即恢复：不带开关立刻恢复原有展示与访问，不需要重建/重同步
  G. 管理操作与其它业务不受影响
  H. 服务端强制开关 MINOR_MODE_FORCE
  I. 可见源站为空时返回 200 + 空列表（不是 404）
  J. 标记不被源站刷新洗掉；面板与 OpenAPI 同步

运行：python scripts/test_v1150_minor_mode.py
"""
import json
import os
import sys
import tempfile
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="vodsync-v1150-"))
os.environ.setdefault(
    "DATABASE_URL", "sqlite:///" + os.path.join(os.environ["DATA_DIR"], "v1150.db")
)
os.environ.setdefault("AUTH_ENABLED", "false")
os.environ.setdefault("FULL_SYNC_ON_DISCOVER", "false")
os.environ.setdefault("AUTO_RESUME_ON_BOOT", "false")
os.environ.setdefault("RELATED_ENABLED", "false")
os.environ.setdefault("TAG_INDEX_ON_STARTUP", "false")
os.environ.setdefault("TOTAL_APPROXIMATE_ENABLED", "true")
os.environ.setdefault("MINOR_MODE_FORCE", "false")

passed = failed = 0

MINOR = {"X-Minor-Mode": "1"}
MINOR_OFF = {"X-Minor-Mode": "0"}
SLUGS = ("safe_a", "safe_b", "adult_x")


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


def setup():
    from sqlalchemy import insert, select

    from app import auth, database, models

    database.init_db()
    auth.ensure_init()

    # DDL 必须在写事务之外执行：SQLite 下事务内 CREATE TABLE 会与后续
    # INSERT 抢锁，报 "database is locked"（与 syncer.ensure_table 同法）
    tables = {slug: models.build_vod_table(slug) for slug in SLUGS}
    for table in tables.values():
        table.create(database.engine, checkfirst=True)

    now = datetime.now()
    with database.write_txn() as conn:
        for slug in SLUGS:
            conn.execute(insert(models.sources).values(
                slug=slug, name=f"测试站-{slug}", api_url=f"http://{slug}/api.php",
                enabled=1, total_videos=3, watermark="2026-11-11 08:00:00",
                status="ok", auto_sync=1, origin="latest",
                created_at=now, updated_at=now,
            ))
            conn.execute(insert(tables[slug]), [
                {"vod_id": i, "type_id": 1, "type_name": "剧情",
                 "name": f"{slug} 测试片{i}", "pic": "", "remarks": "HD",
                 "content": "简介", "play_url": "第1集$http://x/1.m3u8",
                 "play_from": "m3u8", "vod_time": f"2026-11-11 09:0{i}:00",
                 "vod_area": "大陆", "vod_year": "2026", "vod_tag": "剧情",
                 "vod_actor": "", "synced_at": now}
                for i in (1, 2, 3)
            ])
    with database.engine.connect() as conn:
        return {r[0]: r[1] for r in conn.execute(
            select(models.sources.c.slug, models.sources.c.is_adult)
        ).all()}


def main():
    from sqlalchemy import select, text

    from app import __version__, caps, config, database, minor_mode, models

    print("\nA. 数据兼容与版本")
    before = setup()
    check("A1 版本号 1.15.1", __version__ == "1.15.1", __version__)
    check("A2 capabilities 声明 minor_mode",
          "minor_mode" in caps.snapshot(db_ok=True), caps.snapshot(db_ok=True))
    check("A3 sources 表存在 is_adult 列", "is_adult" in {c.name for c in models.sources.c}, "")
    check("A4 存量源站自动补列且默认 0",
          all(before.get(s) == 0 for s in SLUGS), before)
    check("A5 is_adult 归一化（NULL/0/脏值按否）",
          [minor_mode.is_adult(v) for v in (None, 0, "", "x", 2, 1, "1")]
          == [False, False, False, False, False, True, True], "")
    with database.write_txn() as conn:
        conn.execute(models.sources.update()
                     .where(models.sources.c.slug == "safe_a")
                     .values(is_adult=None))
    check("A6 NULL 也不会被误判成 18+", "safe_a" not in minor_mode.blocked_slugs(), "")

    from fastapi.testclient import TestClient
    from app.main import app as _app

    client = TestClient(_app)
    check("A7 TestClient 路由可用（/healthz 200）",
          client.get("/healthz").status_code == 200, "")

    print("\nB. 18+ 标记管理与下发")
    r = client.post("/api/v1/sources/adult_x/adult?enabled=true")
    check("B1 保存标记 200 + is_adult=true",
          r.status_code == 200 and r.json().get("is_adult") is True, r.text[:200])
    st, js = jget(client, "/source")
    check("B2 /source 下发 is_adult",
          any(x["slug"] == "adult_x" and x["is_adult"] is True for x in js["list"]), js["list"])
    st2, js2 = jget(client, "/api/v1/sources")
    check("B3 /api/v1/sources 下发 is_adult + minor_mode=false",
          js2.get("minor_mode") is False
          and any(x["slug"] == "adult_x" and x["is_adult"] is True for x in js2["list"]), "")
    st3, js3 = jget(client, "/api/v1/sources/adult_x")
    check("B4 单站详情下发 is_adult",
          js3.get("source", {}).get("is_adult") is True, js3.get("source"))
    st4, js4 = jget(client, "/api/v1/minor-mode")
    check("B5 /api/v1/minor-mode 是真实端点（没被 /{slug} 吃掉）",
          st4 == 200 and js4.get("adult_sources") == ["adult_x"], (st4, js4))
    check("B6 缺 enabled 返回 400",
          client.post("/api/v1/sources/adult_x/adult").status_code == 400, "")
    check("B7 非法 enabled 返回 400",
          client.post("/api/v1/sources/adult_x/adult?enabled=maybe").status_code == 400, "")
    check("B8 不存在的源站返回 404",
          client.post("/api/v1/sources/nope/adult?enabled=true").status_code == 404, "")
    r = client.post("/api/v1/sources/adult_x/adult?enabled=false", json={"enabled": True})
    check("B9 body 优先于 query（两个都收）",
          r.status_code == 200 and r.json().get("is_adult") is True, r.text[:200])

    print("\nC. 不带开关：现有 API 调用零影响")
    _, cat = jget(client, "/source")
    check("C1 /source 仍含全部 3 站", len(cat["list"]) == 3, len(cat["list"]))
    _, vids = jget(client, "/api/v1/videos")
    check("C2 全源列表 9 条", len(vids.get("list", [])) == 9, len(vids.get("list", [])))
    _, one = jget(client, "/api/v1/videos?source=adult_x")
    check("C3 指定 18+ 站仍可访问", len(one.get("list", [])) == 3, one)
    _, sea = jget(client, "/api/v1/search?wd=测试片")
    check("C4 搜索仍含 18+ 站",
          any(x.get("source") == "adult_x" for x in sea.get("list", [])), "")
    _, cats = jget(client, "/api/v1/categories")
    check("C5 分类仍含 18+ 站",
          any("adult_x" in (x.get("sources") or []) for x in cats.get("list", [])), "")
    st, _ = jget(client, "/api/v1/videos/adult_x/1")
    check("C6 单条详情 200", st == 200, st)
    st, macc = jget(client, "/api/v1/adult_x?ac=list")
    check("C7 MacCMS 兼容接口 200 且 code 恒为 1",
          st == 200 and macc.get("code") == 1, (st, macc.get("code")))
    st, _ = jget(client, "/api/v1/sources/adult_x")
    check("C8 单源站详情 200", st == 200, st)
    st, _ = jget(client, "/api/v1/related?slug=adult_x&id=1")
    check("C9 相关推荐未被屏蔽（非 404）", st != 404, st)

    print("\nD. 未成年模式（X-Minor-Mode: 1）")
    _, cat = jget(client, "/source", headers=MINOR)
    check("D1 /source 展示入口屏蔽 18+ 站",
          cat.get("minor_mode") is True and len(cat["list"]) == 2
          and all(x["slug"] != "adult_x" for x in cat["list"]), cat["list"])
    _, srcs = jget(client, "/api/v1/sources", headers=MINOR)
    check("D2 /api/v1/sources 同样屏蔽",
          srcs.get("minor_mode") is True
          and all(x["slug"] != "adult_x" for x in srcs["list"]), "")
    _, vids = jget(client, "/api/v1/videos", headers=MINOR)
    check("D3 全源列表只剩非 18+ 站（6 条）",
          len(vids.get("list", [])) == 6
          and all(x.get("source") != "adult_x" for x in vids.get("list", [])),
          len(vids.get("list", [])))
    st, one = jget(client, "/api/v1/videos?source=adult_x", headers=MINOR)
    check("D4 显式点名 18+ 站 → 200 + 空列表（不是 404）",
          st == 200 and one.get("list") == [] and one.get("total") == 0, (st, one))
    _, mix = jget(client, "/api/v1/videos?source=safe_a,adult_x", headers=MINOR)
    check("D5 多源混合只保留非 18+ 站",
          len(mix.get("list", [])) == 3
          and all(x.get("source") == "safe_a" for x in mix.get("list", [])), "")
    st, ids = jget(client, "/api/v1/videos?source=adult_x&ids=1", headers=MINOR)
    check("D6 ids 批量详情不是旁路",
          st == 200 and ids.get("list") == [], (st, ids))
    _, sea = jget(client, "/api/v1/search?wd=测试片", headers=MINOR)
    check("D7 搜索结果不含 18+ 站",
          all(x.get("source") != "adult_x" for x in sea.get("list", [])), "")
    st, sea2 = jget(client, "/api/v1/search?wd=测试片&source=adult_x", headers=MINOR)
    check("D8 搜索点名 18+ 站也返回空结果",
          st == 200 and sea2.get("list") == [], (st, sea2))
    _, cats = jget(client, "/api/v1/categories", headers=MINOR)
    check("D9 分类不含 18+ 站（缓存未串味）",
          all("adult_x" not in (x.get("sources") or []) for x in cats.get("list", [])), "")
    st, _ = jget(client, "/api/v1/videos/adult_x/1", headers=MINOR)
    check("D10 视频详情 404", st == 404, st)
    st, _ = jget(client, "/api/v1/adult_x?ac=list", headers=MINOR)
    check("D11 MacCMS 兼容接口 404（采集端不能绕过）", st == 404, st)
    st, _ = jget(client, "/api/v1/related?slug=adult_x&id=1", headers=MINOR)
    check("D12 相关推荐 404", st == 404, st)
    st, _ = jget(client, "/api/v1/star/works?slug=adult_x&term=某人", headers=MINOR)
    check("D13 明星作品 404", st == 404, st)
    st, _ = jget(client, "/api/v1/sources/adult_x", headers=MINOR)
    check("D14 单源站详情 404", st == 404, st)
    st, _ = jget(client, "/api/v1/star/tags/untracked?slug=adult_x", headers=MINOR)
    check("D15 诊断接口点名 18+ 站 404", st == 404, st)
    _, status = jget(client, "/api/v1/minor-mode", headers=MINOR)
    check("D16 状态接口 active=true", status.get("active") is True, status)
    st, _ = jget(client, "/api/v1/videos/safe_a/1", headers=MINOR)
    check("D17 非 18+ 源站完全不受影响", st == 200, st)
    st, batch = jget(client, "/api/v1/videos?ids=1", headers=MINOR)
    check("D18 不带 source 的 ids 批量详情也不含 18+ 站内容",
          st == 200 and all(x.get("source") != "adult_x" for x in batch.get("list", [])),
          (st, batch.get("list")))
    st, tagged = jget(client, "/api/v1/videos?tag=剧情", headers=MINOR)
    check("D19 /videos 标签检索路径同样过滤",
          st == 200 and all(x.get("source") != "adult_x" for x in tagged.get("list", [])),
          (st, tagged.get("list")))
    st, _ = jget(client, "/api/v1/star/related?slug=adult_x&vod_id=1", headers=MINOR)
    check("D20 明星相关推荐 404", st == 404, st)
    st, _ = jget(client, "/api/v1/adult_x?ac=list&minor_mode=1")
    check("D21 MacCMS 用查询参数通道也被屏蔽（播放器内核无法加头）", st == 404, st)
    _, capsjs = jget(client, "/healthz.capabilities")
    check("D22 能力清单端点声明 minor_mode",
          "minor_mode" in capsjs.get("capabilities", []), capsjs)

    print("\nE. 查询参数通道与优先级")
    _, cat = jget(client, "/source?minor_mode=1")
    check("E1 ?minor_mode=1 生效", len(cat["list"]) == 2, len(cat["list"]))
    _, cat = jget(client, "/source?minor=1")
    check("E2 ?minor=1 别名生效", len(cat["list"]) == 2, len(cat["list"]))
    _, cat = jget(client, "/source?minor_mode=1", headers=MINOR_OFF)
    check("E3 请求头优先：X-Minor-Mode: 0 覆盖 ?minor_mode=1",
          len(cat["list"]) == 3, len(cat["list"]))
    _, cat = jget(client, "/source?minor_mode=0")
    check("E4 显式 0 不屏蔽", len(cat["list"]) == 3, len(cat["list"]))

    print("\nF. 关闭未成年模式立即恢复")
    _, cat = jget(client, "/source")
    check("F1 不带开关 → 3 站全回来", len(cat["list"]) == 3, len(cat["list"]))
    st, _ = jget(client, "/api/v1/videos/adult_x/1")
    check("F2 详情立即恢复 200", st == 200, st)
    _, cats = jget(client, "/api/v1/categories")
    check("F3 分类立即恢复含 18+ 站",
          any("adult_x" in (x.get("sources") or []) for x in cats.get("list", [])), "")
    _, vids = jget(client, "/api/v1/videos")
    check("F4 列表立即恢复 9 条", len(vids.get("list", [])) == 9, "")

    print("\nG. 管理操作与其它业务不受影响")
    r = client.post("/api/v1/sources/adult_x/adult?enabled=false", headers=MINOR)
    check("G1 未成年模式下仍可修改标记（管理操作不受影响）",
          r.status_code == 200 and r.json().get("is_adult") is False, r.text[:200])
    _, cat = jget(client, "/source", headers=MINOR)
    check("G2 关闭标记后读路径立即生效（无缓存窗口）",
          len(cat["list"]) == 3, len(cat["list"]))
    client.post("/api/v1/sources/adult_x/adult?enabled=true")
    _, stats = jget(client, "/api/v1/stats")
    _, stats_m = jget(client, "/api/v1/stats", headers=MINOR)
    check("G3 概览统计不受未成年模式影响",
          stats.get("sources_total") == stats_m.get("sources_total") == 3,
          (stats.get("sources_total"), stats_m.get("sources_total")))
    _, logs = jget(client, "/api/v1/logs", headers=MINOR)
    check("G4 同步日志接口仍可访问", logs.get("code") == 0, logs)
    with database.write_txn() as conn:
        conn.execute(models.sources.update()
                     .where(models.sources.c.slug == "adult_x")
                     .values(name="刷新后的名字", status="ok", description="refresh 只改元信息"))
    with database.engine.connect() as conn:
        val = conn.execute(select(models.sources.c.is_adult)
                           .where(models.sources.c.slug == "adult_x")).scalar()
    check("G5 源站元信息刷新不洗掉 18+ 标记", val == 1, val)

    # 真实调用 syncer.refresh_sources（喂一个假客户端，避免打外网），验证
    # INSERT 不带 is_adult（走 server_default）、UPDATE 只改元信息这两条路径
    from app import syncer

    class _FakeClient:
        def _get_json(self, url):
            return {"resources": [{
                "api": "http://adult_x/api.php", "name": "刷新后的名字",
                "description": "refresh 只改元信息", "status": "ok",
            }]}

        def close(self):
            pass

    syncer.refresh_sources(client=_FakeClient())
    with database.engine.connect() as conn:
        r = conn.execute(select(models.sources.c.name, models.sources.c.is_adult)
                         .where(models.sources.c.slug == "adult_x")).first()
    check("G6 真实 refresh_sources 不改 is_adult",
          r is not None and r[1] == 1 and r[0] == "刷新后的名字", r)

    # 分类清单有 300s 进程内缓存：先制造一份「标记前」的未成年模式缓存，
    # 再改标记，验证保存端点会清缓存（否则 5 分钟内分类仍漏出 18+ 站）
    _, c0 = jget(client, "/api/v1/categories", headers=MINOR)
    client.post("/api/v1/sources/safe_a/adult?enabled=true")
    _, c1 = jget(client, "/api/v1/categories", headers=MINOR)
    check("G7 改标记后分类缓存立即失效",
          any("adult_x" in (x.get("sources") or []) for x in c0.get("list", []))
          is False  # 前置条件：adult_x 本来就被屏蔽
          and all("safe_a" not in (x.get("sources") or []) for x in c1.get("list", [])),
          (c0.get("list"), c1.get("list")))
    client.post("/api/v1/sources/safe_a/adult?enabled=false")

    print("\nH. 服务端强制开关 MINOR_MODE_FORCE")
    config.MINOR_MODE_FORCE = True
    try:
        _, cat = jget(client, "/source")
        check("H1 强制开关下不带头的请求也被屏蔽", len(cat["list"]) == 2, len(cat["list"]))
        _, cat = jget(client, "/source", headers=MINOR_OFF)
        check("H2 显式 X-Minor-Mode: 0 也无法绕过强制开关",
              len(cat["list"]) == 2, len(cat["list"]))
        _, st = jget(client, "/api/v1/minor-mode")
        check("H3 状态接口 forced=true", st.get("forced") is True, st)
    finally:
        config.MINOR_MODE_FORCE = False

    print("\nI. 全部源站都是 18+ 时的空结果语义")
    for s in ("safe_a", "safe_b"):
        client.post(f"/api/v1/sources/{s}/adult?enabled=true")
    st, vids = jget(client, "/api/v1/videos", headers=MINOR)
    check("I1 列表返回 200 + 空列表 + total=0",
          st == 200 and vids.get("list") == [] and vids.get("total") == 0
          and vids.get("minor_mode") is True, (st, vids))
    st, sea = jget(client, "/api/v1/search?wd=测试片", headers=MINOR)
    check("I2 搜索返回 200 + 空列表", st == 200 and sea.get("list") == [], (st, sea))
    for s in ("safe_a", "safe_b"):
        client.post(f"/api/v1/sources/{s}/adult?enabled=false")

    print("\nJ. 面板 / 契约同步")
    html = open(os.path.join(ROOT, "app", "web", "index.html"), encoding="utf-8").read()
    check("J1 面板有 18+ 列与 adultSel 下拉",
          "18+ 内容" in html and "adultSel" in html and "/adult?enabled=" in html, "")
    for p in ("openapi.json", os.path.join("docs", "openapi.json")):
        spec = json.load(open(os.path.join(ROOT, p), encoding="utf-8"))
        check(f"J2 {p} 版本与新增端点",
              spec["info"]["version"] == "1.15.1"
              and "/api/v1/minor-mode" in spec["paths"]
              and "/api/v1/sources/{slug}/adult" in spec["paths"]
              and "/api/v1/sources/adult" in spec["paths"], "")
    cdoc = open(os.path.join(ROOT, "docs", "CLIENT_API.md"), encoding="utf-8").read()
    adoc = open(os.path.join(ROOT, "docs", "API_SPEC_FOR_AI.md"), encoding="utf-8").read()
    check("J3 客户端文档含 X-Minor-Mode 契约",
          "X-Minor-Mode" in cdoc and "is_adult" in cdoc and "4.27" in adoc, "")
    nginx = open(os.path.join(ROOT, "deploy", "nginx-vodsync.conf"), encoding="utf-8").read()
    check("J4 反代样例显式透传 X-Minor-Mode",
          "proxy_set_header X-Minor-Mode" in nginx and "$http_x_minor_mode" in nginx, "")

    print("\nK. 批量 18+ 标记（面板多选）")
    r = client.post("/api/v1/sources/adult?enabled=true&slugs=safe_a,safe_b")
    check("K1 批量标记 200 且 updated=2",
          r.status_code == 200 and r.json().get("updated") == 2, r.text[:200])
    _, js = jget(client, "/api/v1/minor-mode")
    check("K2 批量生效：两站都进 adult_sources",
          set(js.get("adult_sources") or []) == {"safe_a", "safe_b", "adult_x"},
          js.get("adult_sources"))
    _, js = jget(client, "/api/v1/sources", headers=MINOR)
    check("K3 三站全标记后未成年模式列表为空",
          js.get("list") == [], js.get("list"))

    r = client.post("/api/v1/sources/adult?enabled=false&slugs=safe_a,safe_b")
    check("K4 批量取消 200 且 updated=2",
          r.status_code == 200 and r.json().get("updated") == 2, r.text[:200])
    _, js = jget(client, "/api/v1/sources", headers=MINOR)
    check("K5 取消后立刻恢复可见",
          sorted(x["slug"] for x in js["list"]) == ["safe_a", "safe_b"], js.get("list"))

    r = client.post("/api/v1/sources/adult?enabled=true&slugs=safe_a,nope")
    _, js = jget(client, "/api/v1/minor-mode")
    check("K6 含不存在 slug 时整批拒绝且不改任何数据",
          r.status_code == 404 and "safe_a" not in (js.get("adult_sources") or []),
          (r.status_code, js.get("adult_sources")))

    r = client.post("/api/v1/sources/adult?enabled=true&slugs=all")
    _, js = jget(client, "/api/v1/minor-mode")
    check("K7 slugs=all 全量标记",
          r.status_code == 200 and len(js.get("adult_sources") or []) == 3,
          (r.status_code, js.get("adult_sources")))
    client.post("/api/v1/sources/adult?enabled=false&slugs=all")
    _, js = jget(client, "/api/v1/minor-mode")
    check("K8 slugs=all 取消后无残留", js.get("adult_sources") == [], js.get("adult_sources"))
    check("K9 缺 enabled 返回 422",
          client.post("/api/v1/sources/adult?slugs=safe_a").status_code in (400, 422), "")

    html = open(os.path.join(ROOT, "app", "web", "index.html"), encoding="utf-8").read()
    check("K10 面板有批量标记按钮且走批量端点",
          "btnAdultSelOn" in html and "btnAdultSelOff" in html
          and "/api/v1/sources/adult?enabled=" in html, "")
    check("K11 轮询在表格操作期间跳过重渲染",
          "function sourcesBusy()" in html
          and "if (!force && sourcesBusy()) return;" in html
          and "loadSources(true)" in html, "")

    print(f"\n==== v1.15.0 未成年模式测试: {passed} passed, {failed} failed ====")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
