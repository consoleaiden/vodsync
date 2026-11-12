# -*- coding: utf-8 -*-
"""
v1.16.0「智能去广告（adfilter）规则下发与命中上报」回归测试。

覆盖：
  A. 版本、能力声明与两张新表
  B. 规则拉取四态（首次 / 已最新 / 客户端更高 / 未知参数），响应形态与契约一致
  C. 发布校验：越界、超条数、空数组、非法 mode 全部拒绝；规范化与告警
  D. 回滚：版本号单调 +1、内容取旧版、不存在版本 404
  E. 上报：幂等去重、超 50 整批拒绝、逐条丢弃语义、未知字段忽略
  F. 鉴权：匿名 401、read Token 可上报、admin 端点拒绝 read Token
  G. 限流 429 + Retry-After
  H. 统计不含 url；既有接口零回归（/healthz、/source、sources、MacCMS、minor-mode）
  I. 面板区块与文档契约同步、两份 openapi 一致

运行：python scripts/test_v1160_adfilter.py
"""
import json
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="vodsync-v1160-"))
os.environ.setdefault(
    "DATABASE_URL", "sqlite:///" + os.path.join(os.environ["DATA_DIR"], "v1160.db")
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


def setup():
    from app import adfilter, database, models, syncer

    database.init_db()
    adfilter.ensure_seed()
    # 一张 vod 表 + 一个源站：只为验证「既有内容接口」没被影响
    table = models.build_vod_table("ad_a")
    table.create(database.engine, checkfirst=True)
    with database.write_txn() as conn:
        conn.execute(models.sources.insert().values(
            slug="ad_a", name="测试站-A", api_url="http://ad_a/api.php",
            enabled=1, total_videos=1, watermark="2026-11-12 08:00:00",
            status="ok", auto_sync=1, origin="latest",
        ))
        conn.execute(table.insert().values(
            vod_id=1, type_id=1, type_name="剧情", name="测试片", pic="",
            remarks="HD", content="简介", play_url="第1集$http://x/1.m3u8",
            play_from="m3u8", vod_time="2026-11-12 09:00:00",
            vod_area="大陆", vod_year="2026", vod_tag="剧情", vod_actor="",
        ))
    return syncer  # 只为确保 import 顺序与线上一致


def main():
    from fastapi.testclient import TestClient
    from sqlalchemy import inspect

    from app import __version__, adfilter, auth, caps, config, database, models
    from app.main import app as _app

    setup()
    client = TestClient(_app)

    print("\nA. 版本、能力声明与数据表")
    # 逐版改测试太脆：只看「不低于引入 adfilter 的版本」，openapi 用严格相等
    _ver = tuple(int(x) for x in __version__.split(".")[:3])
    check("A1 版本号 ≥ 1.16.0", _ver >= (1, 16, 0), __version__)
    snap = caps.snapshot(db_ok=True)
    check("A2 capabilities 含 adfilter", "adfilter" in snap, snap)
    st, js = jget(client, "/healthz")
    check("A3 /healthz 下发 adfilter 且版本一致",
          st == 200 and "adfilter" in js.get("capabilities", []) and js.get("version") == __version__,
          (st, js.get("version")))
    old = config.ADFILTER_ENABLED
    config.ADFILTER_ENABLED = False
    try:
        check("A4 关闭开关后不再声明 adfilter",
              "adfilter" not in caps.snapshot(db_ok=True), caps.snapshot(True))
        check("A5 关闭开关后接口 404",
              client.get("/api/v1/adfilter/rules?version=0").status_code == 404, "")
    finally:
        config.ADFILTER_ENABLED = old
    tables = set(inspect(database.engine).get_table_names())
    check("A6 两张新表已建",
          {"adfilter_rule_packs", "adfilter_reports"} <= tables, sorted(tables)[:8])
    check("A7 规则包列齐全",
          {"channel", "version", "rules_json", "note", "created_at"}
          <= {c.name for c in models.adfilter_rule_packs.c}, "")
    check("A8 上报列齐全且带去重键",
          {"user_id", "url", "url_hash", "rule_version", "removed_runs",
           "removed_ms", "total_ms", "at"} <= {c.name for c in models.adfilter_reports.c}, "")

    print("\nB. 规则拉取四态")
    cur = adfilter.current("stable")
    check("B1 启动已播种 v1", cur["version"] == 1, cur["version"])
    st, js = jget(client, "/api/v1/adfilter/rules?version=0")
    check("B2 首次 version=0 → changed=true 且带完整 rules",
          st == 200 and js.get("changed") is True and js.get("version") == 1
          and set(js.get("rules") or {}) == {"thresholds", "url_patterns", "site_overrides"},
          (st, js))
    check("B3 响应不带 code 包装（契约形态）",
          "code" not in js, list(js))
    check("B4 默认规则阈值与客户端内置一致",
          js["rules"]["thresholds"] == {"relative_max_ratio": 0.5, "absolute_max_sec": 300,
                                        "safety_floor": 0.8}, js["rules"]["thresholds"])
    check("B5 默认 url_patterns 为 8 条", len(js["rules"]["url_patterns"]) == 8,
          js["rules"]["url_patterns"])
    st, js = jget(client, "/api/v1/adfilter/rules?version=1")
    check("B6 已最新 → changed=false 且省略 rules",
          st == 200 and js.get("changed") is False and "rules" not in js, js)
    st, js = jget(client, "/api/v1/adfilter/rules?version=99")
    check("B7 客户端版本更高 → changed=false + 服务端版本",
          st == 200 and js.get("changed") is False and js.get("version") == 1, js)
    st, js = jget(client, "/api/v1/adfilter/rules?version=0&channel=beta")
    # beta 渠道从未发布过 → 服务端版本 0，与客户端版本相同 → changed=false 且不带 rules，
    # 客户端继续用自己的内置默认规则（两边内置值一致，没必要多传一份）
    check("B8 channel=beta 未发布时 changed=false + version=0",
          st == 200 and js.get("changed") is False and js.get("version") == 0
          and "rules" not in js, js)
    st, js = jget(client, "/api/v1/adfilter/rules?version=0&channel=whatever&foo=1")
    check("B9 未知 channel / 未知 query 不报错（按 stable）",
          st == 200 and js.get("version") == 1, (st, js.get("version")))
    r = client.get("/api/v1/adfilter/rules?version=0")
    check("B10 带 Cache-Control: no-store",
          r.headers.get("cache-control") == "no-store", r.headers.get("cache-control"))

    print("\nC. 发布校验与规范化")
    bad_cases = [
        ("C1 relative_max_ratio=0.99 越界", {"thresholds": {"relative_max_ratio": 0.99}}),
        ("C2 absolute_max_sec=5 越界", {"thresholds": {"absolute_max_sec": 5}}),
        ("C3 safety_floor=0.05 越界", {"thresholds": {"safety_floor": 0.05}}),
        ("C4 absolute_max_sec=300.5 非整数", {"thresholds": {"absolute_max_sec": 300.5}}),
        ("C5 url_patterns 空数组", {"url_patterns": []}),
        ("C6 url_patterns 65 条", {"url_patterns": ["/p%d/" % i for i in range(65)]}),
        ("C7 url_patterns 含非字符串", {"url_patterns": ["/ad/", 1]}),
        ("C8 site_overrides mode 非法", {"site_overrides": [{"host": "a.com", "mode": "on"}]}),
        ("C9 site_overrides 超 256 条",
         {"site_overrides": [{"host": "h%d.com" % i, "mode": "off"} for i in range(257)]}),
        ("C10 rules 不是对象", ["/ad/"]),
    ]
    for name, rules in bad_cases:
        st, js = jpost(client, "/api/v1/adfilter/admin/packs", {"rules": rules})
        check(name + " → 400", st == 400, (st, str(js)[:120]))
    check("C11 校验失败不产生新版本", adfilter.current("stable")["version"] == 1,
          adfilter.current("stable")["version"])

    st, js = jpost(client, "/api/v1/adfilter/admin/packs", {
        "rules": {
            "thresholds": {"relative_max_ratio": 0.6, "unknown_key": 1},
            "url_patterns": ["/AD/", "/ad/", "/shop/ad/"],
            "site_overrides": [{"host": "A.com ", "mode": "OFF"},
                               {"host": "a.com", "mode": "off", "extra": 1}],
            "unknown_top": True,
        },
        "note": "测试发布",
    })
    check("C12 合法发布 200 且版本 +1",
          st == 200 and js.get("version") == 2, (st, str(js)[:160]))
    rules2 = js.get("rules") or {}
    check("C13 url_patterns 转小写并去重",
          rules2.get("url_patterns") == ["/ad/", "/shop/ad/"], rules2.get("url_patterns"))
    check("C14 未知键被丢弃、阈值被保留",
          "unknown_key" not in (rules2.get("thresholds") or {})
          and rules2["thresholds"]["relative_max_ratio"] == 0.6, rules2.get("thresholds"))
    check("C15 site_overrides host 小写去空白、同 host 后者覆盖",
          rules2.get("site_overrides") == [{"host": "a.com", "mode": "off"}],
          rules2.get("site_overrides"))
    check("C16 语义词告警返回但不拒收",
          any("/shop/" in w for w in (js.get("warnings") or [])), js.get("warnings"))

    st, js = jpost(client, "/api/v1/adfilter/admin/packs", {
        "rules": {"thresholds": {"safety_floor": 0.9}}})
    check("C17 部分字段发布：缺省字段不下发（客户端用内置默认）",
          st == 200 and js["rules"] == {"thresholds": {"safety_floor": 0.9}}, js.get("rules"))
    st, js = jget(client, "/api/v1/adfilter/admin/packs")
    check("C18 版本清单倒序且当前版本正确",
          st == 200 and js.get("current") == 3
          and [p["version"] for p in js.get("packs") or []] == [3, 2, 1], js)
    check("C19 清单不含规则正文（省流量）",
          all("rules_json" not in p and "rules" not in p for p in js.get("packs") or []),
          js.get("packs"))
    st, js = jget(client, "/api/v1/adfilter/admin/packs/2")
    check("C20 指定版本详情含规则正文",
          st == 200 and js.get("version") == 2 and "url_patterns" in (js.get("rules") or {}), js)
    check("C21 不存在版本 404",
          client.get("/api/v1/adfilter/admin/packs/999").status_code == 404, "")

    print("\nD. 回滚")
    st, js = jpost(client, "/api/v1/adfilter/admin/packs/rollback?version=1")
    check("D1 回滚 200，发布为新版本 v4",
          st == 200 and js.get("version") == 4 and js.get("rolled_back_from") == 1, js)
    check("D2 回滚后内容等于 v1",
          js.get("rules", {}).get("thresholds") == adfilter.get(1)["rules"]["thresholds"],
          js.get("rules"))
    row = adfilter.get(4)
    check("D3 新版本备注记录回滚来源（不修改历史行）",
          "回滚至 v1" in (row or {}).get("note", "")
          and adfilter.get(1)["note"] != (row or {}).get("note"), row)
    st, js = jget(client, "/api/v1/adfilter/rules?version=3")
    check("D4 客户端带旧版本拉取 → changed=true 且是新版本号",
          st == 200 and js.get("changed") is True and js.get("version") == 4, js)
    check("D5 回滚不存在版本 404",
          client.post("/api/v1/adfilter/admin/packs/rollback?version=999").status_code == 404, "")

    print("\nE. 上报入库")
    now = int(time.time())
    items = [
        {"url": "https://e.com/a/index.m3u8", "rule_version": 3, "removed_runs": 2,
         "removed_ms": 51600, "total_ms": 13379900, "at": now},
        {"url": "https://e.com/b/index.m3u8", "rule_version": 3, "removed_runs": 1,
         "removed_ms": 17600, "total_ms": 4020900, "at": now + 1},
    ]
    st, js = jpost(client, "/api/v1/adfilter/report", {"items": items})
    check("E1 正常批量 2 条 accepted=2",
          st == 200 and js.get("accepted") == 2 and js.get("dropped") == 0, js)
    st, js = jpost(client, "/api/v1/adfilter/report", {"items": items})
    check("E2 重复上报幂等：accepted=0 duplicates=2",
          st == 200 and js.get("accepted") == 0 and js.get("duplicates") == 2, js)
    st, js = jpost(client, "/api/v1/adfilter/report", {"items": [{}] * 51})
    check("E3 items 超 50 整批 400", st == 400, (st, str(js)[:100]))
    st, js = jpost(client, "/api/v1/adfilter/report", {"items": "x"})
    check("E4 items 不是数组 400", st == 400, (st, str(js)[:100]))
    st, js = jpost(client, "/api/v1/adfilter/report", {"items": [
        {"url": "https://e.com/c/index.m3u8", "rule_version": 1, "removed_runs": 1,
         "removed_ms": 99999, "total_ms": 1000, "at": now},
        {"url": "https://e.com/d/index.m3u8", "rule_version": 1, "removed_runs": 1,
         "removed_ms": 100, "total_ms": 100000, "at": now - 8 * 86400},
        {"url": "https://e.com/" + "x" * 2100, "rule_version": 1, "removed_runs": 1,
         "removed_ms": 100, "total_ms": 100000, "at": now},
        {"url": "", "rule_version": 1, "removed_runs": 1, "removed_ms": 100,
         "total_ms": 100000, "at": now},
        {"url": "https://e.com/ok/index.m3u8", "rule_version": 1, "removed_runs": 1,
         "removed_ms": 100, "total_ms": 100000, "at": now, "unknown_field": "忽略"},
    ]})
    check("E5 逐条丢弃：4 条不合规、1 条入库",
          st == 200 and js.get("accepted") == 1 and js.get("dropped") == 4, js)
    st, js = jpost(client, "/api/v1/adfilter/report", {})
    check("E6 缺 items 视为空批（200 accepted=0）",
          st == 200 and js.get("accepted") == 0, js)

    print("\nF. 鉴权")
    auth.set_enabled(True)
    try:
        st, js = jpost(client, "/api/v1/adfilter/report", {"items": items})
        check("F1 匿名上报 401", st == 401, st)
        check("F2 匿名拉规则 401",
              client.get("/api/v1/adfilter/rules?version=0").status_code == 401, "")
        tok = auth.create_scoped_token("read", "adfilter-test")
        hdr = {"Authorization": "Bearer " + tok["token"]}
        st, js = jget(client, "/api/v1/adfilter/rules?version=0", headers=hdr)
        check("F3 read Token 可拉规则", st == 200, st)
        old = config.TOKEN_SCOPE_ENFORCE
        config.TOKEN_SCOPE_ENFORCE = True
        try:
            st, js = jpost(client, "/api/v1/adfilter/report",
                           {"items": [{"url": "https://e.com/r/index.m3u8", "rule_version": 1,
                                       "removed_runs": 1, "removed_ms": 10,
                                       "total_ms": 1000, "at": int(time.time())}]}, headers=hdr)
            check("F4 打开 scope 强制后 read Token 仍可上报", st == 200, (st, js))
            st, _ = jget(client, "/api/v1/adfilter/admin/packs", headers=hdr)
            check("F5 read Token 访问管理端 403", st == 403, st)
            st, _ = jpost(client, "/api/v1/adfilter/admin/packs",
                          {"rules": {"thresholds": {"safety_floor": 0.6}}}, headers=hdr)
            check("F6 read Token 发布被拒 403", st == 403, st)
        finally:
            config.TOKEN_SCOPE_ENFORCE = old
        auth.revoke_scoped_token(tok["id"])
    finally:
        auth.set_enabled(False)
    check("F7 关闭鉴权后上报恢复可用",
          jpost(client, "/api/v1/adfilter/report", {"items": []})[0] == 200, "")

    print("\nG. 限流")
    adfilter.reset_rate()
    old_rate = config.ADFILTER_REPORT_RATE_PER_HOUR
    config.ADFILTER_REPORT_RATE_PER_HOUR = 2
    try:
        codes = [jpost(client, "/api/v1/adfilter/report", {"items": []})[0] for _ in range(3)]
        check("G1 超过每小时额度返回 429", codes[:2] == [200, 200] and codes[2] == 429, codes)
        r = client.post("/api/v1/adfilter/report", json={"items": []})
        check("G2 429 带 Retry-After 且 code=42901",
              r.status_code == 429 and r.headers.get("retry-after")
              and r.json().get("code") == 42901, (r.status_code, r.json().get("code")))
    finally:
        config.ADFILTER_REPORT_RATE_PER_HOUR = old_rate
        adfilter.reset_rate()

    print("\nH. 统计与既有接口零回归")
    st, js = jget(client, "/api/v1/adfilter/admin/stats")
    check("H1 统计可用且总数为正",
          st == 200 and js.get("available") is True and js.get("total", 0) >= 3, js)
    check("H2 统计不含任何 url 字段或地址（隐私）",
          "url" not in js and "http" not in json.dumps(js), js)
    check("H3 removed_ratio 是 0~1 的小数",
          0 <= float(js.get("removed_ratio") or 0) <= 1, js.get("removed_ratio"))
    st, js = jget(client, "/source")
    check("H4 /source 正常且字段未变",
          st == 200 and js.get("code") == 0 and isinstance(js.get("list"), list)
          and {"slug", "name", "is_adult", "video_count"} <= set(js["list"][0]), js.get("total"))
    st, js = jget(client, "/api/v1/sources")
    check("H5 /api/v1/sources 正常", st == 200 and js.get("code") == 0, st)
    st, js = jget(client, "/api/v1/videos?source=ad_a&page_size=5")
    check("H6 视频列表正常", st == 200 and js.get("code") == 0, st)
    st, js = jget(client, "/api/v1/videos/ad_a/1")
    check("H7 视频详情正常", st == 200 and js.get("code") == 0, st)
    st, js = jget(client, "/api/v1/ad_a?ac=list")
    check("H8 MacCMS 兼容接口正常", st == 200 and js.get("code") == 1, st)
    st, js = jget(client, "/api/v1/minor-mode")
    check("H9 未成年模式诊断正常", st == 200 and "active" in js, js)

    print("\nI. 面板与文档契约")
    html = open(os.path.join(ROOT, "app", "web", "index.html"), encoding="utf-8").read()
    check("I1 面板有规则包区块与按钮",
          all(k in html for k in ("adfilterToggle", "adfilterWrap", "btnAdPublish",
                                 "btnAdReload", "adHistory")), "")
    check("I2 面板走管理端接口",
          "/api/v1/adfilter/admin/packs" in html and "/rollback?version=" in html, "")
    check("I3 规则包区块已接入懒加载 SEC_DEFS",
          "adfilter: { btn: \"#adfilterToggle\"" in html, "")
    cdoc = open(os.path.join(ROOT, "docs", "CLIENT_API.md"), encoding="utf-8").read()
    adoc = open(os.path.join(ROOT, "docs", "API_SPEC_FOR_AI.md"), encoding="utf-8").read()
    check("I4 CLIENT_API 有 1.6 节与 adfilter",
          "### 1.6 智能去广告" in cdoc and "/api/v1/adfilter/rules" in cdoc, "")
    check("I5 API_SPEC 有 4.28 节与能力行",
          "### 4.28 智能去广告" in adoc and "| `adfilter` |" in adoc, "")
    mdoc = open(os.path.join(ROOT, "docs", "MIGRATION_NOTES.md"), encoding="utf-8").read()
    check("I6 MIGRATION_NOTES 有 2.3 节与两张表",
          "## 2.3 v1.16.0" in mdoc and "adfilter_rule_packs" in mdoc
          and "adfilter_reports" in mdoc, "")
    spec = json.load(open(os.path.join(ROOT, "openapi.json"), encoding="utf-8"))
    spec2 = json.load(open(os.path.join(ROOT, "docs", "openapi.json"), encoding="utf-8"))
    check("I7 两份 openapi 一致且版本与代码一致 / 65 方法",
          spec == spec2 and spec["info"]["version"] == __version__
          and sum(len(v) for v in spec["paths"].values()) == 65, spec["info"]["version"])
    check("I8 openapi 收录 6 条 adfilter 路径",
          len([p for p in spec["paths"] if "adfilter" in p]) == 6,
          [p for p in spec["paths"] if "adfilter" in p])

    print(f"\n==== v1.16.0 adfilter 测试: {passed} passed, {failed} failed ====")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
