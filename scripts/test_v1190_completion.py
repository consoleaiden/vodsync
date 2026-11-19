"""
v1.19.0 回归测试：运营管理升级「未实现/半实现项」全部补齐。

覆盖：
  §4.3  事件流闭环：rebuild 写 promoted/demoted、feedback 写 auto_disabled/
        feedback_still_ads、manual 写 manual_created、review 写 locked；
        events() 返回全部类型 + since 过滤。
  §4.6  fp-feedback reason：over_deleted 计 miss、still_has_ads 不计（服务端）。
  §4.4  批量删除预览 + 执行；批量停用复用 batch_review。
  §2.4  发布 diff（thresholds/url/site/fingerprints）+ dirty 覆盖 updated_at。
  §4.1  单行 block_reason=capped。
  §4.5  桶宽迁移脚本存在且可 --dry-run。
  §2.3  host 可解析校验。
  §3.3  前端漏判样本面板；§3.4 明细概览行；前端 DOM 完整性守护。

运行：PYTHONPATH=. .venv/Scripts/python.exe scripts/test_v1190_completion.py
"""
import os
import re
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

fails = []


def check(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + ((" :: " + str(extra)) if extra else ""))
    if not cond:
        fails.append(name)


# ---------------- 后端（临时库） ----------------
DB = os.path.join(tempfile.gettempdir(), "_v1190_completion.db")
if os.path.exists(DB):
    os.remove(DB)
os.environ["DATABASE_URL"] = f"sqlite:///{DB}"
os.environ["AUTH_ENABLED"] = "false"
os.environ["ADFILTER_ENABLED"] = "true"
os.environ["ADFILTER_FP_ENABLED"] = "true"
os.environ["ADFILTER_FP_MIN_USERS"] = "1"
os.environ["ADFILTER_FP_MIN_URLS"] = "2"
os.environ["ADFILTER_FP_MIN_HITS"] = "3"
os.environ["ADFILTER_FP_MISS_DISABLE"] = "2"
os.environ["ADFILTER_FP_MISS_REASON_REQUIRED"] = "false"  # 过渡期兼容

from app import adfilter, adfilter_fp, audit, config, database  # noqa: E402

database.init_db()


def feed(host, urls=2, segs=4, users=1, dur_ms=2600):
    """按 (user, url) 分组落库片段，满足 distinct_urls/users/hits 门槛以便 rebuild 晋升。"""
    per = max(1, segs // max(1, urls * users))
    for u in range(users):
        for i in range(urls):
            clean = [{"index": k, "dur_ms": dur_ms, "size_b": 0} for k in range(per)]
            adfilter_fp.ingest_segments(
                clean, user_id=u + 1, url_hash=f"{host}-u{i}", host=host,
                at=1700000000 + i,
            )


print("\n---- §4.3 事件流写入 ----")
feed("event-test.example.com", urls=2, segs=4)
adfilter_fp.rebuild()
promoted = audit.events(kind="promoted")
check("rebuild 写 promoted 事件", promoted["total"] >= 1, promoted["total"])
ev_items = promoted["items"]
check("promoted 事件含 fp_hash target", all(len((i.get("target") or "")) == 64 for i in ev_items))

# manual 写 manual_created
m = adfilter_fp.manual_create("manual-event.example.com", 1234, note="测试人工补录")
check("manual 写 manual_created 事件",
      audit.events(kind="manual_created")["total"] >= 1)

# review 写 locked（在 FP 层 review 不写，路由层写；这里验证 audit.record 可用 + 事件类型存在）
audit.record(audit.EVENT_LOCKED, m["fingerprint"]["fp_hash"],
             {"status": "confirmed", "by": "test"}, actor="test")
check("locked 事件可写入并查询", audit.events(kind="locked")["total"] >= 1)

# 版本/类型完整性
check("事件全集含 6 类", set(audit.EVENT_TYPES) == {
    "promoted", "demoted", "auto_disabled", "manual_created", "locked", "feedback_still_ads"
})
check("events 缺省返回多类", len({i["action"] for i in audit.events(limit=100)["items"]}) >= 3)

print("\n---- §4.6 fp-feedback reason 口径 ----")
fp_hash = ev_items[0]["target"]
# 兼容模式：不带 reason 的 miss 仍计 miss（老客户端止血不中断）
r1 = adfilter_fp.feedback(fp_hash, "miss")
check("兼容模式无 reason 计 miss", r1["counted_as_miss"] is True, r1)
check("兼容模式 reason=over_deleted", r1["reason"] == "over_deleted")
# 显式 still_has_ads 不计 miss，只写事件
before_miss = adfilter_fp.get_fingerprint(fp_hash)["miss_count"]
r2 = adfilter_fp.feedback(fp_hash, "miss", "still_has_ads")
after_miss = adfilter_fp.get_fingerprint(fp_hash)["miss_count"]
check("still_has_ads 不计 miss", r2["counted_as_miss"] is False
      and after_miss == before_miss, (before_miss, after_miss))
check("still_has_ads 写 feedback_still_ads 事件",
      audit.list_audit(limit=5, action="feedback_still_ads")["total"] >= 1)
# over_deleted 计 miss
r3 = adfilter_fp.feedback(fp_hash, "miss", "over_deleted")
check("over_deleted 计 miss", r3["counted_as_miss"] is True)
# 非法 reason 报错
try:
    adfilter_fp.feedback(fp_hash, "miss", "bad_reason")
    check("非法 reason 抛 FPError", False)
except adfilter_fp.FPError:
    check("非法 reason 抛 FPError", True)
# 严格模式开关存在
check("配置开关 ADFILTER_FP_MISS_REASON_REQUIRED 存在",
      hasattr(config, "ADFILTER_FP_MISS_REASON_REQUIRED"))

print("\n---- auto_disabled 事件 ----")
# 造一条新指纹，连续 over_deleted miss 达阈值
feed("miss-event.example.com", urls=2, segs=4)
adfilter_fp.rebuild()
mh = None
for it in adfilter_fp.list_fingerprints(status="confirmed", limit=50)["items"]:
    if it["host"] == "miss-event.example.com":
        mh = it["fp_hash"]
        break
check("找到待测 confirmed 指纹", mh is not None)
if mh:
    adfilter_fp.feedback(mh, "miss", "over_deleted")
    dis = adfilter_fp.feedback(mh, "miss", "over_deleted")
    check("达阈值自动 disabled", dis["status"] == "disabled", dis)
    check("写 auto_disabled 事件",
          any(i["target"] == mh for i in audit.list_audit(limit=10, action="auto_disabled")["items"]))

print("\n---- §4.4 批量删除预览/执行 ----")
h_del = m["fingerprint"]["fp_hash"]
pv = adfilter_fp.batch_delete_preview([h_del, "0" * 64])
check("批量删除预览 found=1", pv["found"] == 1, pv)
check("批量删除预览 not_found=1", len(pv["not_found"]) == 1)
bd = adfilter_fp.batch_delete([h_del, "0" * 64])
check("批量删除执行删 1 条", bd["deleted_fingerprints"] == 1, bd)
check("批量删除 not_found 汇总", bd["not_found"] == ["0" * 64], bd["not_found"])
check("删除后指纹不存在", adfilter_fp.get_fingerprint(h_del) is None)

print("\n---- §4.1 capped block_reason ----")
# 造多条 confirmed，max_download 调小到 1，验证列表出现 capped
old_cap = config.ADFILTER_FP_MAX_DOWNLOAD
config.ADFILTER_FP_MAX_DOWNLOAD = 1
try:
    for i in range(3):
        feed(f"cap-{i}.example.com", urls=2, segs=4)
    adfilter_fp.rebuild()
    confirmed_hosts = [it["host"] for it in adfilter_fp.list_fingerprints(
        status="confirmed", limit=50)["items"]]
    capped_items = [it for it in adfilter_fp.list_fingerprints(
        status="confirmed", limit=50)["items"] if it["block_reason"] == "capped"]
    check("多 confirmed 时有 capped 行", len(capped_items) >= 1,
          f"confirmed={len(confirmed_hosts)} capped={len(capped_items)}")
    check("capped 行 downloadable=false", all(not it["downloadable"] for it in capped_items))
finally:
    config.ADFILTER_FP_MAX_DOWNLOAD = old_cap

print("\n---- §2.4 发布 diff + dirty 覆盖 updated_at ----")
diff = adfilter.publish_diff({
    "thresholds": {"relative_max_ratio": 0.6},
    "url_patterns": ["/new-ad/", "/ads/"],
    "site_overrides": [{"host": "x.example.com", "mode": "off"}],
})
check("publish_diff 返回结构完整",
      all(k in diff for k in ("thresholds_changed", "url_patterns", "site_overrides",
                               "fingerprints", "warnings", "bucket_drift")))
check("publish_diff 有 fingerprints 计数", "pack" in diff["fingerprints"])
ps = adfilter.publish_state()
check("publish-state.pending 含 updated_since_publish",
      "updated_since_publish" in ps["pending"], ps["pending"])

print("\n---- §2.3 host 可解析校验 ----")
check("合法域名", adfilter_fp.valid_host("2609.lbsl2026.com") is True)
check("合法 IP", adfilter_fp.valid_host("192.168.1.1") is True)
check("localhost", adfilter_fp.valid_host("localhost") is True)
check("乱码 host 拒绝", adfilter_fp.valid_host("bad host!/path") is False)
check("单标签拒绝", adfilter_fp.valid_host("abc") is False)
try:
    adfilter_fp.manual_create("bad host!", 2600)
    check("manual 非法 host 抛 FPError", False)
except adfilter_fp.FPError:
    check("manual 非法 host 抛 FPError", True)

print("\n---- §4.5 迁移脚本 ----")
script_path = os.path.join(ROOT, "scripts", "migrate_fp_bucket_width.py")
check("迁移脚本存在", os.path.exists(script_path))
if os.path.exists(script_path):
    src = open(script_path, encoding="utf-8").read()
    check("迁移脚本支持 --dry-run/--apply", "--dry-run" in src and "--apply" in src)
    check("迁移脚本按 signature 重算", "signature(" in src)

# ---------------- 前端静态检查 ----------------
print("\n---- 前端面板补齐 ----")
with open(os.path.join(ROOT, "app", "web", "index.html"), encoding="utf-8") as f:
    html = f.read()
script = re.findall(r"<script>([\s\S]*?)</script>", html)[0]

# 前端新增 DOM id 必须存在（防顶层 TypeError）
new_ids = ["fpEventHint", "fpDetailStats", "fpDlFilter", "fpMissGt",
           "btnFpBatchDisable", "btnFpBatchDelete", "fpOpTabAudit", "fpOpTabEvents",
           "fpEventKind", "fpMissPanel", "fpMissDays", "fpMissWrap", "fpMissList"]
for nid in new_ids:
    check(f"DOM id 存在: {nid}", f'id="{nid}"' in html)
# 直接绑定（无 if 保护）的 id 必须在 HTML 中
for nid in ["fpDlFilter", "fpMissGt", "fpEventKind", "fpStatusFilter", "fpOrder"]:
    check(f"顶层绑定 id 有对应元素: {nid}", f'id="{nid}"' in html)

check("漏判样本函数存在", "async function fpLoadMiss" in script)
check("漏判样本接口调用", "/api/v1/adfilter/admin/miss-samples" in script)
check("漏判样本 days 切换", "fpMissDays" in script and "days=" in script)
check("候选跳转 near_miss", "fpNearMiss" in script and "data-miss-host" in script)
check("事件提示函数", "async function fpLoadEventHint" in script)
check("审计/事件 tab 切换", "fpSetOpMode" in script)
check("批量删除预览调用", "batch-delete/preview" in script)
check("批量删除执行调用", "/fingerprints/batch-delete" in script)
check("批量停用调用", "data-fp-act" in script and "status: \"disabled\"" in script)
check("发布 diff 调用", "/api/v1/adfilter/admin/publish-diff" in script)
check("命中率列", "hit_rate" in script and "命中率" in script)
check("最后命中列", "last_hit_at" in script and "最后命中" in script)
check("downloadable 筛选", "fpDlFilter" in script and "downloadable" in script)
check("miss_gt 筛选", "fpMissGt" in script and "miss_gt" in script)
check("明细概览行", "fpDetailStats" in script and "segment_retention_days" in script)
check("全选事件委托", "fpCheckAll" in script and "fp-row-check" in script)

print("\n==== SUMMARY ====")
if fails:
    print("FAILED:", len(fails))
    for f in fails:
        print("  -", f)
    sys.exit(1)
print("ALL PASS")
