# -*- coding: utf-8 -*-
"""
v1.16.3「adfilter 新阈值登记 + 漏判样本隔离」回归测试。

覆盖：
  A. 版本号、两份 openapi 一致、无新增端点
  B. _THRESHOLDS 两项登记且区间与前端 clamp 一致；default_rules 仍 3 项
  C. normalize：保留新字段、越界/类型错拒绝、边界放行、未知键仍静默丢弃
  D. publish → 回显 / 入库 / /rules 下发都带新字段
  E. stats 核心反例：举报样本不进 hosts 聚合、不稀释 avg、单独返回 miss_*，
     并验证「只有举报样本的站」信息不丢
  F. 隐私（不含 url 原文）+ 既有字段一字未改
  G. 面板 / 文档同步、既有接口零回归

运行：python scripts/test_v1163_adfilter.py
"""
import json
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# 必须先设环境变量再 import app.*：历史脚本 test_v191 把 import 放前面，
# 导致 config 先加载 compose 的 postgres://postgres:5432，在 Windows 上必然连不上。
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="vodsync-v1163-"))
os.environ.setdefault(
    "DATABASE_URL", "sqlite:///" + os.path.join(os.environ["DATA_DIR"], "v1163.db")
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


def jpost(client, path, body=None, **kw):
    r = client.post(path, json=body, **kw)
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, {}


def mk(host, i, ratio=0.30, runs=6, miss=False):
    """有效样本 total_ms=10 万；举报样本是 spec §2.2 的 0/0/0。"""
    at = NOW - i
    if miss:
        return {
            "url": f"https://{host}/miss/{i}/index.m3u8", "at": at, "rule_version": 1,
            "removed_runs": 0, "removed_ms": 0, "total_ms": 0,
        }
    total = 100000
    return {
        "url": f"https://{host}/p/{i}/index.m3u8", "at": at, "rule_version": 1,
        "removed_runs": runs, "removed_ms": int(total * ratio), "total_ms": total,
    }


def by_host(js, host):
    return next((h for h in js.get("hosts", []) if h["host"] == host), None)


def main():
    from fastapi.testclient import TestClient

    from app import __version__, adfilter, database
    from app.main import app as _app

    database.init_db()
    adfilter.ensure_seed()
    client = TestClient(_app)

    print("\nA. 版本与契约")
    ver = tuple(int(x) for x in __version__.split(".")[:3])
    check("A1 版本号 >= 1.16.3", ver >= (1, 16, 3), __version__)
    spec = json.load(open(os.path.join(ROOT, "openapi.json"), encoding="utf-8"))
    spec2 = json.load(open(os.path.join(ROOT, "docs", "openapi.json"), encoding="utf-8"))
    check("A2 两份 openapi 一致且版本与代码一致",
          spec == spec2 and spec["info"]["version"] == __version__,
          spec["info"]["version"])
    check("A3 无新增端点（仍 58 paths / 65 methods）",
          len(spec["paths"]) == 58
          and sum(len(v) for v in spec["paths"].values()) == 65,
          (len(spec["paths"]), sum(len(v) for v in spec["paths"].values())))

    print("\nB. 阈值白名单登记")
    th = adfilter._THRESHOLDS
    check("B1 两个新阈值已登记且区间与前端 clamp 一致",
          th.get("safety_floor_high_confidence") == (0.4, 1.0, False)
          and th.get("duplicate_max_ratio") == (0.3, 0.9, False), th)
    check("B2 原有 3 项区间一字未改",
          all(th.get(k) == v for k, v in {
              "relative_max_ratio": (0.05, 0.95, False),
              "absolute_max_sec": (10, 1800, True),
              "safety_floor": (0.5, 1.0, False),
          }.items()), th)
    check("B3 default_rules 仍是 3 项（字段缺省 = 客户端内置默认）",
          adfilter.default_rules()["thresholds"] == {
              "relative_max_ratio": 0.5, "absolute_max_sec": 300, "safety_floor": 0.8,
          }, adfilter.default_rules()["thresholds"])

    print("\nC. normalize 行为")
    clean = adfilter.normalize({"thresholds": {
        "safety_floor_high_confidence": 0.45,
        "duplicate_max_ratio": 0.75,
    }})
    check("C1 新字段被保留（不再被白名单静默丢弃）",
          clean == {"thresholds": {
              "safety_floor_high_confidence": 0.45,
              "duplicate_max_ratio": 0.75,
          }}, clean)
    bad_cases = [
        ("C2 high_confidence=0.39 越界", {"thresholds": {"safety_floor_high_confidence": 0.39}}),
        ("C3 high_confidence=1.01 越界", {"thresholds": {"safety_floor_high_confidence": 1.01}}),
        ("C4 duplicate=0.29 越界", {"thresholds": {"duplicate_max_ratio": 0.29}}),
        ("C5 duplicate=0.91 越界", {"thresholds": {"duplicate_max_ratio": 0.91}}),
        ("C6 新字段传字符串", {"thresholds": {"duplicate_max_ratio": "0.8"}}),
        ("C7 新字段传 bool", {"thresholds": {"safety_floor_high_confidence": True}}),
    ]
    for name, rules in bad_cases:
        try:
            adfilter.normalize(rules)
            check(name + " → 拒绝", False, "未抛 RuleError")
        except adfilter.RuleError:
            check(name + " → 拒绝", True)
    lo = adfilter.normalize({"thresholds": {
        "safety_floor_high_confidence": 0.4, "duplicate_max_ratio": 0.3}})["thresholds"]
    hi = adfilter.normalize({"thresholds": {
        "safety_floor_high_confidence": 1.0, "duplicate_max_ratio": 0.9}})["thresholds"]
    check("C8 边界值 0.4/0.3 与 1.0/0.9 均放行",
          lo == {"safety_floor_high_confidence": 0.4, "duplicate_max_ratio": 0.3}
          and hi == {"safety_floor_high_confidence": 1.0, "duplicate_max_ratio": 0.9},
          (lo, hi))
    check("C9 未知键仍静默丢弃（向前兼容不变）",
          adfilter.normalize({"thresholds": {"unknown_key": 1}, "unknown_top": True})
          == {"thresholds": {}}, "")

    print("\nD. 发布与下发")
    st, js = jpost(client, "/api/v1/adfilter/admin/packs", {
        "rules": {"thresholds": {
            "safety_floor_high_confidence": 0.45,
            "duplicate_max_ratio": 0.75,
        }},
        "note": "v1.16.3 新阈值",
    })
    check("D1 发布 200 且回显原样保留两个新字段",
          st == 200 and js.get("rules", {}).get("thresholds") == {
              "safety_floor_high_confidence": 0.45, "duplicate_max_ratio": 0.75,
          }, (st, js))
    st, r = jget(client, "/api/v1/adfilter/rules?version=0")
    check("D2 /rules 下发包含新字段（客户端能读到）",
          st == 200 and r.get("rules", {}).get("thresholds", {}).get(
              "safety_floor_high_confidence") == 0.45, r)
    cur = adfilter.current("stable")
    check("D3 入库规则正文含新字段",
          cur["rules"]["thresholds"]["duplicate_max_ratio"] == 0.75, cur["rules"])

    print("\nE. 漏判样本隔离（核心反例）")
    # 坏站 5 条有效样本（0.30 / 6 段）+ 20 条 0/0/0 举报。
    # 修复前：avg_ratio 被稀释到 5*0.30/25 = 0.06、avg_runs 到 30/25 = 1.2，
    # 两条口径都不再命中，误杀站点从面板上消失。
    good = [mk("bad.example.com", i) for i in range(10, 15)]
    miss = [mk("bad.example.com", i, miss=True) for i in range(20, 40)]
    only_miss = [mk("onlymiss.example.com", i, miss=True) for i in range(40, 43)]
    r = adfilter.ingest(good + miss + only_miss, 7)
    check("E1 0/0/0 举报样本能正常入库（校验未误杀）",
          r["accepted"] == 28 and r["dropped"] == 0, r)
    st, js = jget(client, "/api/v1/adfilter/admin/stats?days=7")
    bad = by_host(js, "bad.example.com")
    check("E2 坏站仍按 5 条有效样本统计（举报样本未混入 hosts）",
          st == 200 and bad and bad["reports"] == 5 and bad["distinct_urls"] == 5, bad)
    check("E3 avg_removed_ratio 仍是 0.30（修复前会被稀释到 0.06）",
          bad and bad["avg_removed_ratio"] == 0.3, bad)
    check("E4 avg_removed_runs 仍是 6.0（修复前会被稀释到 1.2）",
          bad and bad["avg_removed_runs"] == 6.0, bad)
    check("E5 误杀判定未被稀释，仍标 suspicious 且两条口径都命中",
          bad and bad["suspicious"] is True
          and set(bad["reasons"]) == {"avg_removed_ratio", "avg_removed_runs"}, bad)
    check("E6 miss_reports 单独计数 = 23（20 + 3）",
          js.get("miss_reports") == 23, js.get("miss_reports"))
    mh = {h["host"]: h for h in js.get("miss_hosts", [])}
    check("E7 miss_hosts 按举报数降序，含只有举报的站点",
          mh.get("bad.example.com", {}).get("reports") == 20
          and mh.get("onlymiss.example.com", {}).get("reports") == 3
          and js["miss_hosts"][0]["host"] == "bad.example.com", js.get("miss_hosts"))
    check("E8 只有举报的站不污染 hosts 分布",
          by_host(js, "onlymiss.example.com") is None
          and js.get("hosts_total") == 1, (js.get("hosts_total"), js.get("hosts")))
    check("E9 miss_hosts 项只给 host / reports / last_at",
          all(set(h) == {"host", "reports", "last_at"} for h in js.get("miss_hosts", [])),
          js.get("miss_hosts"))
    check("E10 last_at 是 epoch 秒",
          all(isinstance(h["last_at"], int) and h["last_at"] > 1700000000
              for h in js.get("miss_hosts", [])), js.get("miss_hosts"))

    print("\nF. 隐私与既有字段零回归")
    check("F1 响应不含 url 原文 / 任何 http 地址",
          "url" not in js and "http" not in json.dumps(js, ensure_ascii=False), "")
    base = {"code", "available", "total", "recent", "days", "distinct_urls",
            "removed_ratio", "last_at", "hosts", "hosts_total", "suspicious_count",
            "thresholds", "host_limit"}
    check("F2 v1.16.0 / v1.16.1 既有字段一个没少", base <= set(js), sorted(set(js)))
    check("F3 全局 total / recent 仍计入全部上报（口径未变）",
          js["total"] == 28 and js["recent"] == 28, (js["total"], js["recent"]))
    check("F4 全局 removed_ratio 不受 0/0/0 影响（仍 0.30）",
          abs(js["removed_ratio"] - 0.3) < 1e-6, js["removed_ratio"])
    check("F5 阈值回显未变",
          js.get("thresholds") == {"avg_removed_ratio": 0.15, "avg_removed_runs": 4.0,
                                  "min_reports": 5}, js.get("thresholds"))

    print("\nG. 面板 / 文档 / 既有接口")
    html = open(os.path.join(ROOT, "app", "web", "index.html"), encoding="utf-8").read()
    check("G1 面板提示列出两个新阈值与区间",
          "safety_floor_high_confidence" in html and "duplicate_max_ratio" in html
          and "0.4~1.0" in html and "0.3~0.9" in html, "")
    check("G2 面板展示漏判举报", "miss_reports" in html and "漏判举报" in html, "")
    adoc = open(os.path.join(ROOT, "docs", "API_SPEC_FOR_AI.md"), encoding="utf-8").read()
    mdoc = open(os.path.join(ROOT, "docs", "MIGRATION_NOTES.md"), encoding="utf-8").read()
    check("G3 文档同步（API 契约 + 2.5 迁移节）",
          "miss_reports" in adoc and "## 2.5 v1.16.3" in mdoc
          and "## 2.4 v1.16.1" in mdoc, "")
    st, h = jget(client, "/healthz")
    check("G4 /healthz 版本与 adfilter 能力",
          st == 200 and h["version"] == __version__
          and "adfilter" in h["capabilities"], (st, h.get("version")))
    st, r1 = jget(client, "/api/v1/adfilter/rules?version=999")
    check("G5 rules 超前版本仍 changed:false",
          st == 200 and r1.get("changed") is False, r1)
    st, p = jget(client, "/api/v1/adfilter/admin/packs")
    check("G6 规则包管理端点仍正常",
          st == 200 and p.get("current", 0) >= 2, p.get("current"))

    print(f"\n==== v1.16.3 adfilter 测试: {passed} passed, {failed} failed ====")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
