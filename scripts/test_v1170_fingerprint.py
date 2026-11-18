"""
v1.17.0 回归测试：广告指纹库（Phase 2，2a~2d 全链路）。

覆盖
----
2a  segments 可选字段落库（adfilter_segments），老上报不带字段行为不变、幂等
2b  rebuild 聚类：distinct_users/urls/hits 达标才 candidate→confirmed；disabled 永不自动恢复
2c  confirmed 指纹随规则包下发（fingerprints 字段），空库时不注入（老规则逐字不变）
2d  反馈闭环：hit 记 run_hits；miss 累计达阈值自动 disabled 并从下发列表移除
    审核状态机：人工 confirm/candidate/disabled，人工优先于门槛
    接口层：fp-feedback（read token 白名单）、管理端 6 个端点存在
    边界：segments 非法字段逐段丢弃、超长截断、fp_hash 归一化稳定、误杀不对外暴露 url

运行：PYTHONPATH=. .venv/Scripts/python.exe scripts/test_v1170_fingerprint.py
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DB = os.path.join(tempfile.gettempdir(), "_v1170_fingerprint.db")
if os.path.exists(DB):
    os.remove(DB)

os.environ["DATABASE_URL"] = f"sqlite:///{DB}"
os.environ["ADFILTER_ENABLED"] = "true"
os.environ["ADFILTER_FP_ENABLED"] = "true"
os.environ["ADFILTER_FP_MIN_USERS"] = "3"
os.environ["ADFILTER_FP_MIN_URLS"] = "3"
os.environ["ADFILTER_FP_MIN_HITS"] = "10"
os.environ["ADFILTER_FP_MISS_DISABLE"] = "5"
os.environ["ADFILTER_FP_MAX_DOWNLOAD"] = "128"

from app import adfilter, adfilter_fp, auth, config, database  # noqa: E402

database.init_db()
adfilter.ensure_seed()

fails = []


def check(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + ((" :: " + str(extra)) if extra else ""))
    if not cond:
        fails.append(name)


# ---------------- 归一化与校验（纯函数） ----------------
s1 = adfilter_fp.signature("AD-Example.COM", 10000, 131072)
s2 = adfilter_fp.signature("ad-example.com", 10500, 140000)
check("fp_hash 站点大小写归一 + 时长/大小同桶稳定", s1["fp_hash"] == s2["fp_hash"],
      (s1["fp_hash"][:10], s2["fp_hash"][:10]))
s3 = adfilter_fp.signature("ad-example.com", 20000, 131072)
check("不同时长桶 -> 不同指纹", s1["fp_hash"] != s3["fp_hash"])

clean, drop = adfilter_fp.validate_segments([
    {"index": 0, "dur_ms": 10000, "size_b": 65536},
    {"index": -1, "dur_ms": 5000},       # index 负 -> 丢
    {"dur_ms": 0},                       # dur_ms 0 -> 丢
    {"index": 3, "dur_ms": 15000},
])
check("segments 合法段保留 2 条", len(clean) == 2, clean)
check("segments 非法段丢弃计数", drop.get("segment_field_invalid") == 2, drop)
clean2, drop2 = adfilter_fp.validate_segments(
    [{"index": i, "dur_ms": 1000} for i in range(60)]
)
check("segments 超长截断到上限", len(clean2) == config.ADFILTER_FP_MAX_SEGMENTS, len(clean2))
check("segments 超长有计数", drop2.get("segments_too_many") == 60 - config.ADFILTER_FP_MAX_SEGMENTS, drop2)

# ---------------- 2a：上报落库 + 幂等 ----------------
now = int(time.time())
segs = [
    {"index": 0, "dur_ms": 10000, "size_b": 131072},
    {"index": 1, "dur_ms": 15000, "size_b": 131072},
]
item = {
    "url": "https://ad-example.com/2026/abc/index.m3u8",
    "rule_version": 1, "removed_runs": 2, "removed_ms": 25000,
    "total_ms": 1000000, "at": now, "segments": segs,
}
r1 = adfilter.ingest([item], user_id=10)
check("上报 accepted=1", r1["accepted"] == 1, r1)
check("上报 segments_accepted=2", r1["segments_accepted"] == 2, r1)
r2 = adfilter.ingest([item], user_id=10)
check("重复上报 accepted=0（幂等）", r2["accepted"] == 0, r2)
check("重复上报不产生重复 segments", r2["segments_accepted"] == 0, r2)

# 老上报不带 segments：完全走老路径
old_item = {
    "url": "https://old-site.com/x/index.m3u8",
    "rule_version": 1, "removed_runs": 1, "removed_ms": 10000,
    "total_ms": 500000, "at": now,
}
r3 = adfilter.ingest([old_item], user_id=11)
check("老上报（无 segments）accepted=1", r3["accepted"] == 1, r3)
check("老上报 segments_accepted=0", r3["segments_accepted"] == 0, r3)

# 直接查 segments 表行数
from sqlalchemy import func, select  # noqa: E402
from app.models import adfilter_segments as seg_tbl  # noqa: E402
with database.engine.connect() as conn:
    n_seg = conn.execute(select(func.count()).select_from(seg_tbl)).scalar()
check("segments 表共 2 行（去重后）", n_seg == 2, n_seg)

# ---------------- 2b：聚类 + 置信度 ----------------
# 造 3 个用户 x 3 个地址，同一 fp（时长桶 10s）共 9+ 条命中
target = segs[0]  # dur 10000 -> dur_bucket 10
for uid in (21, 22, 23):
    for k in (1, 2, 3):
        it = {
            "url": f"https://ad-example.com/2026/{uid}{k}/index.m3u8",
            "rule_version": 1, "removed_runs": 1, "removed_ms": 10000,
            "total_ms": 900000, "at": now + uid + k,
            "segments": [target],
        }
        adfilter.ingest([it], user_id=uid)

res = adfilter_fp.rebuild()
check("rebuild 有扫描结果", res["scanned"] >= 2, res)
target_hash = adfilter_fp.signature("ad-example.com", 10000, 131072)["fp_hash"]
fp = adfilter_fp.get_fingerprint(target_hash)
check("目标指纹已生成", fp is not None)
check("目标指纹 distinct_users>=3", fp and fp["distinct_users"] >= 3, fp and fp["distinct_users"])
check("目标指纹 distinct_urls>=3", fp and fp["distinct_urls"] >= 3, fp and fp["distinct_urls"])
check("目标指纹达标晋升 confirmed", fp and fp["status"] == "confirmed", fp and fp["status"])

# 样本不足的另一指纹保持 candidate
little = adfilter_fp.signature("ad-example.com", 15000, 131072)["fp_hash"]
fp_little = adfilter_fp.get_fingerprint(little)
check("样本不足指纹保持 candidate", fp_little and fp_little["status"] == "candidate",
      fp_little and fp_little["status"])

# ---------------- 2c：下发注入 + 包体兼容 ----------------
dl = adfilter_fp.downloadable()
check("downloadable 含 confirmed 指纹", any(x["fp_hash"] == target_hash for x in dl), len(dl))
check("downloadable 不含 candidate", all(x["fp_hash"] != little for x in dl))

# v1.17.1 阻断性回归：pattern_json 必须带桶字段，下发字段不能回落 0
from app.models import adfilter_fingerprints as fp_tbl  # noqa: E402
dl_target = next(x for x in dl if x["fp_hash"] == target_hash)
check("下发 dur_bucket=10（非 0）", dl_target["dur_bucket"] == 10, dl_target)
check("下发 size_bucket=2（非 0）", dl_target["size_bucket"] == 2, dl_target)
check("下发 dur_bucket_ms=1000", dl_target["dur_bucket_ms"] == config.ADFILTER_FP_DUR_BUCKET_MS, dl_target)
check("下发 size_bucket_b=65536", dl_target["size_bucket_b"] == config.ADFILTER_FP_SIZE_BUCKET_B, dl_target)
with database.engine.connect() as conn:
    import json as _json  # noqa: E402
    raw_pj = conn.execute(
        select(fp_tbl.c.pattern_json).where(fp_tbl.c.fp_hash == target_hash)
    ).scalar()
pat = _json.loads(raw_pj)
for k, v in (("dur_bucket", 10), ("size_bucket", 2),
             ("dur_bucket_ms", 1000), ("size_bucket_b", 65536)):
    check("pattern_json 含 %s=%s" % (k, v), pat.get(k) == v, pat)

pub = adfilter.publish(adfilter.default_rules(), note="test fp publish")
check("发布包含 fingerprints 字段", "fingerprints" in pub["rules"], pub["rules"].keys())
pub_target = next(x for x in pub["rules"]["fingerprints"] if x["fp_hash"] == target_hash)
check("发布包指纹 dur_bucket 非 0", pub_target["dur_bucket"] == 10, pub_target)
cur = adfilter.current()
check("当前规则包可读到 fingerprints", "fingerprints" in cur["rules"])

# v1.17.1 存量修复：手工把 pattern_json 写回「旧格式（无桶字段）」，rebuild 后应自动修复
old_pattern = {"host": "ad-example.com", "hit_count": 9, "distinct_users": 3, "distinct_urls": 9}
with database.write_txn() as conn:
    conn.execute(
        fp_tbl.update().where(fp_tbl.c.fp_hash == target_hash).values(
            pattern_json=_json.dumps(old_pattern)
        )
    )
check("存量坏数据注入成功（下发桶字段回落 0）",
      next(x for x in adfilter_fp.downloadable() if x["fp_hash"] == target_hash)["dur_bucket"] == 0)
adfilter_fp.rebuild()  # 幂等重跑，修复存量
check("rebuild 修复存量后 dur_bucket=10",
      next(x for x in adfilter_fp.downloadable() if x["fp_hash"] == target_hash)["dur_bucket"] == 10)

# ---------------- 2d：反馈闭环 ----------------
fb1 = adfilter_fp.feedback(target_hash, adfilter_fp.FEEDBACK_HIT)
check("feedback hit 返回 found", fb1["found"] is True, fb1)
fp_after_hit = adfilter_fp.get_fingerprint(target_hash)
check("hit 累加 run_hits", fp_after_hit["run_hits"] >= 1, fp_after_hit["run_hits"])

for _ in range(config.ADFILTER_FP_MISS_DISABLE):
    fb = adfilter_fp.feedback(target_hash, adfilter_fp.FEEDBACK_MISS)
check("miss 达阈值自动 disabled", fb["status"] == "disabled", fb)
check("disabled 后不可下发", fb["downloadable"] is False)
check("downloadable 已移除该指纹", all(x["fp_hash"] != target_hash for x in adfilter_fp.downloadable()))
fp_disabled = adfilter_fp.get_fingerprint(target_hash)
check("误杀计数正确", fp_disabled["miss_count"] == config.ADFILTER_FP_MISS_DISABLE, fp_disabled["miss_count"])

# disabled 永不自动恢复：再 rebuild 一次
adfilter_fp.rebuild()
check("disabled 经 rebuild 不自动恢复", adfilter_fp.get_fingerprint(target_hash)["status"] == "disabled")

# 未知指纹反馈不报错
fb_unknown = adfilter_fp.feedback("0" * 64, "hit")
check("未知指纹反馈 found=false", fb_unknown["found"] is False, fb_unknown)

# 非法反馈参数抛 FPError
try:
    adfilter_fp.feedback(target_hash, "bogus")
    check("非法 kind 抛 FPError", False)
except adfilter_fp.FPError:
    check("非法 kind 抛 FPError", True)

# ---------------- 审核状态机 ----------------
reviewed = adfilter_fp.review(target_hash, "confirmed", "人工恢复")
check("人工 confirm 可覆盖 disabled", reviewed["status"] == "confirmed", reviewed["status"])
check("人工 confirm 写 note", reviewed["note"] == "人工恢复")
reviewed2 = adfilter_fp.review(target_hash, "disabled", "再次止血")
check("人工 disabled 生效", reviewed2["status"] == "disabled")
try:
    adfilter_fp.review(target_hash, "bogus")
    check("非法 status 抛 FPError", False)
except adfilter_fp.FPError:
    check("非法 status 抛 FPError", True)
try:
    adfilter_fp.review("f" * 64, "confirmed")
    check("未知指纹 review 抛 LookupError", False)
except LookupError:
    check("未知指纹 review 抛 LookupError", True)

# ---------------- 概览统计 ----------------
st = adfilter_fp.stats()
check("stats 有 by_status", set(st["by_status"]) == {"candidate", "confirmed", "disabled"}, st["by_status"])
check("stats config 含阈值", st["config"]["min_users"] == 3)
check("列表分页可用", adfilter_fp.list_fingerprints(limit=1)["total"] >= 1)

# ---------------- 接口层 ----------------
check("fp-feedback 在 read-scope 白名单", "/api/v1/adfilter/fp-feedback" in auth._READ_SCOPE_WRITES)
from app.main import app as fastapi_app  # noqa: E402
paths = set(fastapi_app.openapi()["paths"])
for p in (
    "/api/v1/adfilter/fp-feedback",
    "/api/v1/adfilter/admin/fingerprints",
    "/api/v1/adfilter/admin/fingerprints/overview",
    "/api/v1/adfilter/admin/fingerprints/rebuild",
    "/api/v1/adfilter/admin/fingerprints/review",
    "/api/v1/adfilter/admin/fingerprints/publish",
):
    check(f"路由存在 {p}", p in paths)

# ---------------- 汇总 ----------------
print("\n==== SUMMARY ====")
if fails:
    print(f"FAILED {len(fails)}: " + ", ".join(fails))
    sys.exit(1)
print("ALL PASS")
