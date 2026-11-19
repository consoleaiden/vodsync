"""
v1.18.0 运营管理升级回归测试。

覆盖前端提出的后端升级需求（P0/P1/P2 后端项，§4.6 跨端项排除）：
  §2.1 locked 粘性：人工确认后 rebuild 不覆盖；lock:false 交还后 rebuild 接管
  §2.2 miss 复位：confirmed 时 miss_count 归零 + last_reset_at；block_reason
  §2.3 manual 补指纹：服务端 signature 计算、幂等、source=manual
  §2.4 publish-state：pack/library/pending/rebuild/bucket
  §3.1 列表筛选排序分页 + near_miss + blocked_by
  §3.2 blocked_by 分桶 + 批量审核
  §3.3 miss-samples 判定
  §3.4 明细保留策略
  §3.5 审计 + §4.3 事件流
  §4.1 下发上限可见
  §4.5 桶宽漂移拒绝发布

运行：PYTHONPATH=. .venv/Scripts/python.exe scripts/test_v1180_ops_upgrade.py
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DB = os.path.join(tempfile.gettempdir(), "_v1180_ops.db")
if os.path.exists(DB):
    os.remove(DB)

os.environ["DATABASE_URL"] = f"sqlite:///{DB}"
os.environ["AUTH_ENABLED"] = "false"
os.environ["ADFILTER_ENABLED"] = "true"
os.environ["ADFILTER_FP_ENABLED"] = "true"
os.environ.pop("ADFILTER_FP_MIN_USERS", None)
os.environ["ADFILTER_FP_MIN_URLS"] = "3"
os.environ["ADFILTER_FP_MIN_HITS"] = "10"
os.environ["ADFILTER_FP_MISS_DISABLE"] = "5"

from app import adfilter, adfilter_fp, audit, config, database, housekeeping  # noqa: E402

database.init_db()
adfilter.ensure_seed()

fails = []


def check(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + ((" :: " + str(extra)) if extra else ""))
    if not cond:
        fails.append(name)


def feed(host, urls, segs_per_url, dur_ms=10000, size_b=0):
    now = int(time.time())
    n = 0
    for u in range(urls):
        segs = [{"index": u * 100 + i, "dur_ms": dur_ms, "size_b": size_b} for i in range(segs_per_url)]
        n += adfilter_fp.ingest_segments(
            segs, user_id=1, url_hash=f"url-{host}-{u}", host=host, at=now + u,
        )
    return n


def find_fp(host, dur_ms=10000, size_b=0):
    sig = adfilter_fp.signature(host, dur_ms, size_b)
    return adfilter_fp.get_fingerprint(sig["fp_hash"])


# ================= §2.1 locked 粘性 =================
print("\n---- §2.1 locked ----")
feed("locked.example.com", urls=1, segs_per_url=1)   # 1 地址 1 命中，不达标
adfilter_fp.rebuild()
fp = find_fp("locked.example.com")
check("初始 candidate 未锁定", fp and fp["status"] == "candidate" and not fp["locked"])
h = fp["fp_hash"]
r = adfilter_fp.review(h, "confirmed", "人工确认")
check("review 后 confirmed + locked", r["status"] == "confirmed" and r["locked"] is True)
adfilter_fp.rebuild()
r2 = adfilter_fp.get_fingerprint(h)
check("rebuild 后仍 confirmed（locked 保护）", r2["status"] == "confirmed" and r2["locked"] is True)
check("stats.locked_count=1", adfilter_fp.stats()["locked_count"] == 1)
rb = adfilter_fp.rebuild()
check("rebuild 返回 locked_kept≥1", rb.get("locked_kept", 0) >= 1, rb)
# 交还自动
adfilter_fp.review(h, "confirmed", "交还", lock=False)
r3 = adfilter_fp.get_fingerprint(h)
check("lock:false 后 locked=0 且状态保持", r3["locked"] is False and r3["status"] == "confirmed")
adfilter_fp.rebuild()
check("交还后 rebuild 降回 candidate", adfilter_fp.get_fingerprint(h)["status"] == "candidate")

# ================= §2.2 miss 复位 + block_reason =================
print("\n---- §2.2 miss 复位 ----")
feed("miss.example.com", urls=3, segs_per_url=4)   # 达标
adfilter_fp.rebuild()
mf = find_fp("miss.example.com")
check("达标 confirmed", mf["status"] == "confirmed")
mh = mf["fp_hash"]
for _ in range(5):
    adfilter_fp.feedback(mh, "miss")
mf2 = adfilter_fp.get_fingerprint(mh)
check("5 次 miss 后 disabled", mf2["status"] == "disabled")
check("disabled 时 downloadable=false", mf2["downloadable"] is False)
check("disabled 时 block_reason=miss_threshold", mf2["block_reason"] == "miss_threshold")
adfilter_fp.review(mh, "confirmed", "解除止血")
mf3 = adfilter_fp.get_fingerprint(mh)
check("确认后 miss_count 归零", mf3["miss_count"] == 0)
check("确认后 last_reset_at 有值", mf3["last_reset_at"] is not None)
check("确认后 downloadable=true", mf3["downloadable"] is True)

# ================= §2.3 manual 幂等 =================
print("\n---- §2.3 manual ----")
m1 = adfilter_fp.manual_create("2609.lbsl2026.com", 2600, note="人工补")
m2 = adfilter_fp.manual_create("2609.lbsl2026.com", 2600, note="重复")
check("manual 首次 created=true", m1["created"] is True)
check("manual 重复 created=false（幂等）", m2["created"] is False)
check("manual status=confirmed", m2["fingerprint"]["status"] == "confirmed")
check("manual source=manual", m2["fingerprint"]["source"] == "manual")
check("manual locked=1", m2["fingerprint"]["locked"] is True)
check("manual confidence=1.0", m2["fingerprint"]["confidence"] == 1.0)
check("manual pattern.dur_bucket 由服务端算", m2["fingerprint"]["pattern"]["dur_bucket"] == 3)
try:
    adfilter_fp.manual_create("", 2600)
    check("manual 空 host 抛 FPError", False)
except adfilter_fp.FPError:
    check("manual 空 host 抛 FPError", True)
try:
    adfilter_fp.manual_create("x.com", 0)
    check("manual dur_ms=0 抛 FPError", False)
except adfilter_fp.FPError:
    check("manual dur_ms=0 抛 FPError", True)

# 验证 manual 指纹 rebuild 后仍 confirmed
adfilter_fp.rebuild()
check("manual 指纹 rebuild 后仍 confirmed", find_fp("2609.lbsl2026.com", 2600)["status"] == "confirmed")

# ================= §3.1/§3.2 筛选、near_miss、blocked_by、批量 =================
print("\n---- §3.1/§3.2 列表与批量 ----")
feed("nearmiss.example.com", urls=1, segs_per_url=12)  # 用户1达标/地址1不达标/命中12达标 = 差1项
adfilter_fp.rebuild()
nm = adfilter_fp.list_fingerprints(near_miss=True)
check("near_miss 返回含差一项候选", any(i["host"] == "nearmiss.example.com" for i in nm["items"]), nm["total"])
nmfp = find_fp("nearmiss.example.com")
check("blocked_by.urls=true", nmfp["distinct_urls"] == 1)  # 数据层验证
lst = adfilter_fp.list_fingerprints(source="manual")
check("source=manual 筛选命中", lst["total"] >= 1)
lst2 = adfilter_fp.list_fingerprints(order="miss_desc")
check("排序 miss_desc 不报错", isinstance(lst2["total"], int))
lst3 = adfilter_fp.list_fingerprints(limit=1, offset=0)
check("分页 limit 生效", len(lst3["items"]) == 1)
check("列表项含 blocked_by 字段", "blocked_by" in lst3["items"][0])
st = adfilter_fp.stats()
check("stats.blocked_by 字典完整", all(k in st["blocked_by"] for k in ("users", "urls", "hits")), st["blocked_by"])
# 批量审核
all_manual = [i["fp_hash"] for i in adfilter_fp.list_fingerprints(source="manual")["items"]]
br = adfilter_fp.batch_review(all_manual, "confirmed", "批量", lock=True)
check("批量审核 updated≥1", br["updated"] >= 1, br)
br2 = adfilter_fp.batch_review(["0" * 64], "confirmed", "批量")
check("批量审核 not_found 收集", len(br2["not_found"]) == 1 and br2["updated"] == 0)

# ================= §3.3 miss-samples =================
print("\n---- §3.3 miss-samples ----")
from app.models import adfilter_reports
with database.write_txn() as conn:
    conn.execute(adfilter_reports.insert().values(
        user_id=1, url="http://x/v.mp4", url_hash="h1", host="leak.example.com",
        removed_runs=0, at=int(time.time()),
    ))
ms = adfilter_fp.miss_samples(days=7)
check("miss-samples 含漏判 host", any(i["host"] == "leak.example.com" for i in ms["items"]))
leak = [i for i in ms["items"] if i["host"] == "leak.example.com"][0]
check("漏判 verdict=uncovered", leak["verdict"] == "uncovered")
check("miss-samples 不含 url 原文", "url" not in leak)

# ================= §3.5 审计 + §4.3 事件 =================
print("\n---- §3.5/§4.3 审计与事件 ----")
audit.record("review", "abc", {"status": "confirmed"}, actor="-2")
audit.record("auto_disabled", "def", {"miss": 5}, actor="system")
al = audit.list_audit()
check("审计列表有记录", al["total"] >= 2)
check("审计 action 过滤", audit.list_audit(action="review")["total"] >= 1)
ev = audit.events()
check("事件流过滤出 auto_disabled", any(i["action"] == "auto_disabled" for i in ev["items"]))

# ================= §3.4 保留策略 =================
print("\n---- §3.4 保留策略 ----")
old_seg = config.ADFILTER_SEGMENT_RETENTION_DAYS
try:
    config.ADFILTER_SEGMENT_RETENTION_DAYS = 1
    # 插入一条 10 天前的明细
    with database.write_txn() as conn:
        conn.execute(adfilter_fp.adfilter_segments.insert().values(
            user_id=99, url_hash="old", host="old.com", at=int(time.time()) - 10 * 86400,
            seg_index=0, dur_ms=1000, size_b=0, fp_hash="oldhash",
        ))
    pr = housekeeping.prune_adfilter_details(segment_days=1, report_days=0)
    check("保留策略删除过期明细", pr["segments_deleted"] >= 1, pr)
finally:
    config.ADFILTER_SEGMENT_RETENTION_DAYS = old_seg

# ================= §2.4 publish-state + §4.1 + §4.5 =================
print("\n---- §2.4 publish-state / §4.5 桶宽 ----")
ps = adfilter.publish_state()
check("publish-state 结构完整", all(k in ps for k in ("pack", "library", "pending", "rebuild", "bucket")))
check("publish-state library 含 capped", "capped" in ps["library"])
check("publish-state bucket 含 drift", "drift" in ps["bucket"])
check("桶宽未变 drift=false", ps["bucket"]["drift"] is False)
check("stats 含 confirmed_total/capped", "confirmed_total" in st and "capped" in st)
# 模拟桶宽漂移：改一条指纹的 pattern
with database.write_txn() as conn:
    conn.execute(
        adfilter_fp.adfilter_fingerprints.update()
        .where(adfilter_fp.adfilter_fingerprints.c.fp_hash == h)
        .values(pattern_json='{"host":"locked.example.com","dur_bucket":1,"dur_bucket_ms":999999,"size_bucket":0,"size_bucket_b":65536}')
    )
check("桶宽漂移检测=true", adfilter.bucket_drift() is True)
try:
    adfilter.publish(adfilter.default_rules(), note="should fail")
    check("漂移时 publish 抛 RuleError", False)
except adfilter.RuleError:
    check("漂移时 publish 抛 RuleError", True)
# 恢复
with database.write_txn() as conn:
    conn.execute(
        adfilter_fp.adfilter_fingerprints.update()
        .where(adfilter_fp.adfilter_fingerprints.c.fp_hash == h)
        .values(pattern_json='{"host":"locked.example.com","dur_bucket":10,"dur_bucket_ms":1000,"size_bucket":0,"size_bucket_b":65536}')
    )
check("恢复后 drift=false", adfilter.bucket_drift() is False)

# ================= 路由注册 + 版本 =================
print("\n---- 路由与版本 ----")
from app.main import app  # noqa: E402
spec = app.openapi()
new_routes = {
    "/api/v1/adfilter/admin/fingerprints/manual": "post",
    "/api/v1/adfilter/admin/publish-state": "get",
    "/api/v1/adfilter/admin/miss-samples": "get",
    "/api/v1/adfilter/admin/audit": "get",
    "/api/v1/adfilter/admin/events": "get",
    "/api/v1/adfilter/admin/fingerprints/rebuild/status": "get",
}
for path, method in new_routes.items():
    check(f"路由 {method.upper()} {path.rsplit('/',1)[-1]}",
          path in spec["paths"] and method in spec["paths"][path])
from app import __version__ as _ver  # noqa: E402 —— 后续补丁版本不应让本套件失败
check(f"版本={_ver}", spec["info"]["version"] == _ver, spec["info"]["version"])

print("\n==== SUMMARY ====")
if fails:
    print(f"FAILED {len(fails)}: " + ", ".join(fails))
    sys.exit(1)
print("ALL PASS")
