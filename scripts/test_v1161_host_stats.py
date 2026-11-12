# -*- coding: utf-8 -*-
"""
v1.16.1「adfilter 按站点分布统计 / 误杀主动发现」回归测试。

覆盖：
  A. 版本号、host 列迁移、openapi 与两份规范一致
  B. host 归一化（大小写、端口、userinfo、IPv6、无 scheme、畸形、超长）
  C. 上报写时落 host
  D. 按 host 聚合口径（逐条 ratio 的算术平均/最大、runs、去重地址、last_at、排序）
     —— 含「全局平均被稀释」的反例：全局 < 0.15，但单站 0.30
  E. 异常判定阈值与 min_reports 护栏、严格大于边界、服务端阈值可配置
  F. limit / days 窗口、参数越界 400
  G. 隐私：不含 url 原文；既有字段一字未改
  H. 老行回填（含「已有规则包时 ensure_seed 仍回填」）、幂等
  I. 面板与文档契约同步
  J. 既有接口零回归

运行：python scripts/test_v1161_host_stats.py
"""
import json
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="vodsync-v1161-"))
os.environ.setdefault(
    "DATABASE_URL", "sqlite:///" + os.path.join(os.environ["DATA_DIR"], "v1161.db")
)
os.environ.setdefault("AUTH_ENABLED", "false")
os.environ.setdefault("FULL_SYNC_ON_DISCOVER", "false")
os.environ.setdefault("AUTO_RESUME_ON_BOOT", "false")
os.environ.setdefault("RELATED_ENABLED", "false")
os.environ.setdefault("TAG_INDEX_ON_STARTUP", "false")
os.environ.setdefault("ADFILTER_ENABLED", "true")

passed = failed = 0
NOW = int(time.time())


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


def mk(host, i, ratio=0.01, runs=1, at=None, scheme="https"):
    """构造一条上报：total_ms 固定 10 万，便于精确核对逐条 ratio。"""
    total = 100000
    return {
        "url": f"{scheme}://{host}/p/{i}/index.m3u8",
        "at": NOW - (i if at is None else at),
        "rule_version": 1,
        "removed_runs": runs,
        "removed_ms": int(total * ratio),
        "total_ms": total,
    }


def by_host(js, host):
    return next((h for h in js.get("hosts", []) if h["host"] == host), None)


def main():
    from fastapi.testclient import TestClient
    from sqlalchemy import inspect

    from app import __version__, adfilter, config, database, models
    from app.main import app as _app

    database.init_db()
    adfilter.ensure_seed()
    client = TestClient(_app)

    print("\nA. 版本、迁移与契约")
    ver = tuple(int(x) for x in __version__.split(".")[:3])
    check("A1 版本号 ≥ 1.16.1", ver >= (1, 16, 1), __version__)
    cols = {c["name"] for c in inspect(database.engine).get_columns("adfilter_reports")}
    check("A2 adfilter_reports 有 host 列", "host" in cols, sorted(cols))
    spec = json.load(open(os.path.join(ROOT, "openapi.json"), encoding="utf-8"))
    spec2 = json.load(open(os.path.join(ROOT, "docs", "openapi.json"), encoding="utf-8"))
    check("A3 两份 openapi 一致且版本与代码一致", spec == spec2
          and spec["info"]["version"] == __version__, spec["info"]["version"])
    check("A4 仍是 58 paths / 65 methods（无新增端点）",
          len(spec["paths"]) == 58 and sum(len(v) for v in spec["paths"].values()) == 65,
          (len(spec["paths"]), sum(len(v) for v in spec["paths"].values())))
    params = {p["name"] for p in spec["paths"]["/api/v1/adfilter/admin/stats"]["get"]["parameters"]}
    check("A5 stats 新增 limit / min_reports 参数",
          {"days", "limit", "min_reports"} <= params, params)
    check("A6 阈值默认口径与前端建议一致",
          config.ADFILTER_HOST_SUSPECT_RATIO == 0.15
          and config.ADFILTER_HOST_SUSPECT_RUNS == 4
          and config.ADFILTER_HOST_MIN_REPORTS == 5
          and config.ADFILTER_HOST_STATS_LIMIT == 20,
          (config.ADFILTER_HOST_SUSPECT_RATIO, config.ADFILTER_HOST_SUSPECT_RUNS,
           config.ADFILTER_HOST_MIN_REPORTS, config.ADFILTER_HOST_STATS_LIMIT))

    print("\nB. host 归一化")
    check("B1 去端口 / userinfo / 路径 / query + 小写",
          adfilter.host_of("https://USER:PW@2609.LBSL2026.COM:8443/a/1.m3u8?x=1")
          == "2609.lbsl2026.com", adfilter.host_of("https://USER:PW@2609.LBSL2026.COM:8443/a/1.m3u8?x=1"))
    check("B2 去掉末尾点", adfilter.host_of("http://Example.COM./x.m3u8") == "example.com")
    check("B3 IPv6 去方括号", adfilter.host_of("http://[2001:DB8::1]:8080/x.m3u8") == "2001:db8::1")
    check("B4 无 scheme 也能解析", adfilter.host_of("cdn.example.com/x/1.m3u8") == "cdn.example.com")
    check("B5 空值 / 畸形 → 占位符", adfilter.host_of("") == adfilter.HOST_UNKNOWN
          and adfilter.host_of("   ") == adfilter.HOST_UNKNOWN
          and adfilter.host_of("://bad") == adfilter.HOST_UNKNOWN)
    check("B6 超长 host 截断到 253",
          len(adfilter.host_of("https://" + "a" * 400 + "/x.m3u8")) == 253)

    print("\nC. 上报写时落 host")
    items = [mk("Bad.Example.com", 1, 0.30, 6), mk("bad.example.com:9000", 2, 0.30, 6)]
    r = adfilter.ingest(items, 7)
    check("C1 两条均入库", r["accepted"] == 2, r)
    with database.engine.connect() as conn:
        rows = conn.execute(models.adfilter_reports.select()).all()
    check("C2 不同端口归到同一 host",
          {x._mapping["host"] for x in rows} == {"bad.example.com"},
          [x._mapping["host"] for x in rows])

    # 造数据：坏站 6 条 0.30/6 段，正常站 30 条 0.01/1 段，孤例站 1 条 0.90/9 段。
    # 全局 removed_ratio = (6*30000 + 30*1000 + 90000) / 37*100000 ≈ 0.081 < 0.15，
    # 但坏站逐条平均是 0.30 —— 正是「全站平均稀释掉单站异常」的场景。
    seq = 10
    more = []
    for _ in range(4):
        more.append(mk("bad.example.com", seq, 0.30, 6)); seq += 1
    for _ in range(30):
        more.append(mk("normal.example.com", seq, 0.01, 1)); seq += 1
    more.append(mk("few.example.com", seq, 0.90, 9)); seq += 1
    adfilter.ingest(more, 7)

    print("\nD. 按站点聚合口径")
    st, js = jget(client, "/api/v1/adfilter/admin/stats?days=7")
    check("D1 接口 200 且 available", st == 200 and js.get("available") is True, js)
    check("D2 hosts 是数组且 3 个站点", isinstance(js.get("hosts"), list)
          and len(js["hosts"]) == 3 and js.get("hosts_total") == 3, js.get("hosts_total"))
    bad = by_host(js, "bad.example.com")
    normal = by_host(js, "normal.example.com")
    check("D3 坏站 6 条 / 6 个去重地址",
          bad and bad["reports"] == 6 and bad["distinct_urls"] == 6, bad)
    check("D4 逐条 ratio 算术平均 = 0.30（不是整数除法 0）",
          bad and bad["avg_removed_ratio"] == 0.3, bad)
    check("D5 max_removed_ratio = 0.30 / avg_removed_runs = 6.0",
          bad and bad["max_removed_ratio"] == 0.3 and bad["avg_removed_runs"] == 6.0, bad)
    check("D6 last_at 是 epoch 秒",
          bad and isinstance(bad["last_at"], int) and bad["last_at"] > 1700000000, bad)
    check("D7 正常站 30 条且不疑似",
          normal and normal["reports"] == 30 and normal["suspicious"] is False, normal)
    check("D8 全局占比被稀释（<0.15）但单站 0.30",
          0 < js["removed_ratio"] < 0.15 and bad["avg_removed_ratio"] == 0.3,
          (js["removed_ratio"], bad["avg_removed_ratio"]))
    check("D9 疑似站点排最前", js["hosts"][0]["host"] == "bad.example.com",
          [h["host"] for h in js["hosts"]])
    check("D10 suspicious_count 只数全量里的疑似站点",
          js.get("suspicious_count") == 1, js.get("suspicious_count"))
    check("D11 thresholds 与 host_limit 回显",
          js.get("thresholds") == {"avg_removed_ratio": 0.15, "avg_removed_runs": 4.0,
                                  "min_reports": 5}
          and js.get("host_limit") == 20, js.get("thresholds"))

    print("\nE. 判定阈值与 min_reports 护栏")
    check("E1 坏站两条口径都命中",
          bad["suspicious"] is True and set(bad["reasons"]) == {"avg_removed_ratio", "avg_removed_runs"},
          bad["reasons"])
    check("E2 单条极端值不判疑似（样本不足）",
          by_host(js, "few.example.com")["suspicious"] is False
          and by_host(js, "few.example.com")["reasons"] == [],
          by_host(js, "few.example.com"))
    st, j2 = jget(client, "/api/v1/adfilter/admin/stats?days=7&min_reports=1")
    check("E3 min_reports=1 时孤例站被标疑似",
          by_host(j2, "few.example.com")["suspicious"] is True,
          by_host(j2, "few.example.com"))
    st, j3 = jget(client, "/api/v1/adfilter/admin/stats?days=7&min_reports=100")
    check("E4 min_reports=100 时全部不疑似 / suspicious_count=0",
          j3["suspicious_count"] == 0 and all(not h["suspicious"] for h in j3["hosts"]),
          j3["suspicious_count"])
    # 严格大于：构造恰好 0.15 / 4 段、5 条样本的边界站点
    edge = [mk("edge.example.com", seq + k, 0.15, 4) for k in range(5)]
    adfilter.ingest(edge, 7)
    st, j4 = jget(client, "/api/v1/adfilter/admin/stats?days=7")
    edge_h = by_host(j4, "edge.example.com")
    check("E5 恰好等于阈值不判疑似（严格 >）",
          edge_h and edge_h["avg_removed_ratio"] == 0.15
          and edge_h["avg_removed_runs"] == 4.0 and edge_h["suspicious"] is False,
          edge_h)
    old_ratio, old_runs = config.ADFILTER_HOST_SUSPECT_RATIO, config.ADFILTER_HOST_SUSPECT_RUNS
    config.ADFILTER_HOST_SUSPECT_RATIO, config.ADFILTER_HOST_SUSPECT_RUNS = 0.99, 99
    try:
        st, j5 = jget(client, "/api/v1/adfilter/admin/stats?days=7")
        check("E6 服务端阈值可调且随响应回显",
              j5["suspicious_count"] == 0 and j5["thresholds"]["avg_removed_ratio"] == 0.99,
              j5["thresholds"])
    finally:
        config.ADFILTER_HOST_SUSPECT_RATIO, config.ADFILTER_HOST_SUSPECT_RUNS = old_ratio, old_runs

    print("\nF. limit / days / 参数校验")
    st, j6 = jget(client, "/api/v1/adfilter/admin/stats?days=7&limit=1")
    check("F1 limit=1 只返回 1 个站点但 hosts_total 不变",
          len(j6["hosts"]) == 1 and j6["hosts_total"] == 4 and j6["host_limit"] == 1,
          (len(j6["hosts"]), j6["hosts_total"]))
    adfilter.ingest([mk("old.example.com", 0, 0.5, 9, at=3 * 86400)], 7)
    st, j7 = jget(client, "/api/v1/adfilter/admin/stats?days=1")
    check("F2 days 窗口排除 3 天前的站点（累计 total 仍计入）",
          by_host(j7, "old.example.com") is None
          and j7["hosts_total"] == 4 and j7["total"] > j7["recent"],
          (j7["hosts_total"], j7["total"], j7["recent"]))
    st, j8 = jget(client, "/api/v1/adfilter/admin/stats?days=30")
    check("F3 放宽窗口后老站点出现", by_host(j8, "old.example.com") is not None,
          [h["host"] for h in j8["hosts"]])
    st, _ = jget(client, "/api/v1/adfilter/admin/stats?limit=999")
    check("F4 limit 上界 200 → 400", st == 400, st)
    st, _ = jget(client, "/api/v1/adfilter/admin/stats?min_reports=99999")
    check("F5 min_reports 越界 → 400", st == 400, st)
    st, j9 = jget(client, "/api/v1/adfilter/admin/stats")
    base = {"available", "total", "recent", "days", "distinct_urls", "removed_ratio", "last_at"}
    check("F6 不传新参数时既有 7 个字段全在（老调用方零影响）",
          st == 200 and base <= set(j9) and j9["days"] == 7, j9)

    print("\nG. 隐私与响应形态")
    check("G1 顶层无 url 字段、响应中无 http 地址",
          "url" not in j9 and "http" not in json.dumps(j9, ensure_ascii=False), "")
    check("G2 站点项字段是前端约定的 6 项 + 2 项标记",
          {"host", "reports", "distinct_urls", "avg_removed_ratio", "max_removed_ratio",
           "avg_removed_runs", "last_at", "suspicious", "reasons"} <= set(bad), bad)
    check("G3 接口仍是 code 包装", j9.get("code") == 0, j9.get("code"))

    print("\nH. 老行回填")
    tbl = models.adfilter_reports
    with database.write_txn() as conn:
        conn.execute(tbl.update().where(tbl.c.host == "bad.example.com").values(host=""))
        # 一条只有路径、没有域名的老行，回填后应落 '-'，不能被反复扫
        conn.execute(tbl.insert().values(
            user_id=7, url="/just/a/path.m3u8", url_hash="x" * 64, host="",
            removed_runs=1, removed_ms=1, total_ms=100, at=NOW - 5,
        ))
    n = adfilter._backfill_hosts()
    check("H1 回填返回受影响行数", n == 7, n)
    with database.engine.connect() as conn:
        pending = conn.execute(tbl.select().where(tbl.c.host == "")).all()
        unknown = conn.execute(tbl.select().where(tbl.c.host == adfilter.HOST_UNKNOWN)).all()
    check("H2 回填后无待回填行", not pending, len(pending))
    check("H3 不可解析 url 落 '-' 占位", len(unknown) == 1, len(unknown))
    check("H4 回填幂等（第二次 0 行）", adfilter._backfill_hosts() == 0)
    with database.write_txn() as conn:
        conn.execute(tbl.update().where(tbl.c.host == "normal.example.com").values(host=""))
    adfilter.ensure_seed()   # 已有规则包，仍必须走到回填（曾踩过「提前 return」）
    with database.engine.connect() as conn:
        left = conn.execute(tbl.select().where(tbl.c.host == "")).all()
    check("H5 ensure_seed 在已有版本时仍回填", not left, len(left))
    check("H6 回填后 bad 站统计恢复 6 条",
          by_host(jget(client, "/api/v1/adfilter/admin/stats?days=7")[1], "bad.example.com")["reports"] == 6)

    print("\nI. 面板与文档同步")
    html = open(os.path.join(ROOT, "app", "web", "index.html"), encoding="utf-8").read()
    check("I1 面板有按站点分布区块与刷新按钮",
          'id="adHosts"' in html and 'id="btnAdHostsReload"' in html
          and "renderAdHosts" in html)
    check("I2 有一键止血按钮且明确「尚未发布」",
          'data-host-act="off"' in html and "尚未发布" in html
          and "仍需人工点" in html)
    check("I3 疑似行有高亮样式", "ad-suspicious" in html)
    adoc = open(os.path.join(ROOT, "docs", "API_SPEC_FOR_AI.md"), encoding="utf-8").read()
    cdoc = open(os.path.join(ROOT, "docs", "CLIENT_API.md"), encoding="utf-8").read()
    mdoc = open(os.path.join(ROOT, "docs", "MIGRATION_NOTES.md"), encoding="utf-8").read()
    check("I4 三份文档都写了 hosts / suspicious / 2.4 迁移",
          all(x in adoc for x in ("hosts_total", "suspicious", "min_reports"))
          and "按站点发现误杀" in cdoc
          and "## 2.4 v1.16.1" in mdoc and "adfilter_reports.host" in mdoc)

    print("\nJ. 既有接口零回归")
    st, h = jget(client, "/healthz")
    check("J1 /healthz 版本与能力", st == 200 and h["version"] == __version__
          and "adfilter" in h["capabilities"], (st, h.get("version")))
    st, r1 = jget(client, "/api/v1/adfilter/rules?version=0")
    check("J2 rules 四态仍正常", st == 200 and r1["changed"] is True
          and "url_patterns" in r1["rules"], r1)
    st, r2 = jget(client, "/api/v1/adfilter/rules?version=999")
    check("J3 rules 超前版本仍 changed:false", st == 200 and r2["changed"] is False, r2)
    st, p = jget(client, "/api/v1/adfilter/admin/packs")
    check("J4 规则包管理端点仍正常", st == 200 and p.get("current", 0) >= 1, p.get("current"))
    st, s = jget(client, "/api/v1/sources")
    check("J5 /api/v1/sources 仍正常", st == 200, st)

    print(f"\n==== v1.16.1 host stats 测试: {passed} passed, {failed} failed ====")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
