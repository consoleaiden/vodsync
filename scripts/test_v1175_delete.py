"""
v1.17.5 回归测试：指纹物理删除接口 delete_fingerprint()。

背景：v1.17.4 及之前面板只有「确认/停用/退回」，都只改 status，无法真正
删除指纹；测试残留数据（如 ad-example.com）会永久留在库里和面板上。
v1.17.5 新增 DELETE /api/v1/adfilter/admin/fingerprints/{fp_hash}：
  - 同一个事务里同时删 adfilter_fingerprints（聚合）和 adfilter_segments（明细）
  - 只删聚合表的话 rebuild() 会从明细把指纹重新生成 —— 本测试重点验证不复活
  - 不存在的 fp_hash 抛 LookupError（路由映射 404）
  - 空 fp_hash 抛 FPError（路由映射 400）
  - 删除 confirmed 指纹后 downloadable() 不再返回它
  - 删除一条不影响其他指纹

运行：PYTHONPATH=. .venv/Scripts/python.exe scripts/test_v1175_delete.py
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DB = os.path.join(tempfile.gettempdir(), "_v1175_delete.db")
if os.path.exists(DB):
    os.remove(DB)

os.environ["DATABASE_URL"] = f"sqlite:///{DB}"
os.environ["ADFILTER_ENABLED"] = "true"
os.environ["ADFILTER_FP_ENABLED"] = "true"
os.environ.pop("ADFILTER_FP_MIN_USERS", None)   # 默认 1（单用户场景）
os.environ["ADFILTER_FP_MIN_URLS"] = "3"
os.environ["ADFILTER_FP_MIN_HITS"] = "10"
os.environ["ADFILTER_FP_MISS_DISABLE"] = "5"

from app import adfilter_fp, config, database  # noqa: E402

database.init_db()

fails = []


def check(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + ((" :: " + str(extra)) if extra else ""))
    if not cond:
        fails.append(name)


# ---------------- 1. 造两条不同 host 的指纹（一条待测删除，一条对照）----------------
now = int(time.time())
seg = {"index": 0, "dur_ms": 10000, "size_b": 131072}  # dur_bucket=10, size_bucket=1 w/ default bucket

sig_a = adfilter_fp.signature("del-test.example.com", 10000, 131072)
sig_b = adfilter_fp.signature("keep-test.example.com", 10000, 131072)
FP_A = sig_a["fp_hash"]
FP_B = sig_b["fp_hash"]

# 用 ingest_segments 写入明细（每次调用 = 一次上报：一个 url + at + 片段列表）
def feed(host, urls, segs_per_url):
    inserted = 0
    for u_idx in range(urls):
        # 一次上报带 segs_per_url 个片段（不同 index），at 每次递增避免去重
        segs = [{**seg, "index": u_idx * 100 + i} for i in range(segs_per_url)]
        inserted += adfilter_fp.ingest_segments(
            segs, user_id=1, url_hash=f"url-{host}-{u_idx}", host=host, at=now + u_idx,
        )
    return inserted

# 3 地址 × 每地址 4 片段 = 12 条明细，distinct_urls=3 -> 达标
ins_a = feed("del-test.example.com", urls=3, segs_per_url=4)
check("造数据 A：ingest 写入 12 条明细", ins_a == 12, ins_a)
feed("keep-test.example.com", urls=3, segs_per_url=4)          # 对照指纹也达标

rebuilt = adfilter_fp.rebuild()
check("rebuild 生成 2 条指纹", rebuilt.get("created") == 2, rebuilt)

# ---------------- 2. 确认两条都 confirmed、可下发 ----------------
fa = adfilter_fp.get_fingerprint(FP_A)
fb = adfilter_fp.get_fingerprint(FP_B)
check("指纹 A 达标 confirmed", fa and fa["status"] == "confirmed", fa and fa["status"])
check("指纹 B 达标 confirmed", fb and fb["status"] == "confirmed", fb and fb["status"])
dl_before = adfilter_fp.downloadable()
check("删除前 downloadable 含 2 条", len(dl_before) == 2, len(dl_before))

# 数一下 A 的明细行数（删除应全部清掉）
with database.engine.connect() as conn:
    from sqlalchemy import select, func
    seg_a_before = conn.execute(
        select(func.count()).select_from(adfilter_fp.adfilter_segments)
        .where(adfilter_fp.adfilter_segments.c.fp_hash == FP_A)
    ).scalar()
check("指纹 A 有明细行", seg_a_before == 12, seg_a_before)

# ---------------- 3. 核心：删除 A ----------------
res = adfilter_fp.delete_fingerprint(FP_A)
check("删除返回聚合 1 行", res["deleted_fingerprints"] == 1, res)
check("删除返回明细 12 行", res["deleted_segments"] == 12, res)
check("删除后 A 详情为 None", adfilter_fp.get_fingerprint(FP_A) is None)
check("删除后 B 仍存在（不误删）", adfilter_fp.get_fingerprint(FP_B) is not None)

with database.engine.connect() as conn:
    seg_a_after = conn.execute(
        select(func.count()).select_from(adfilter_fp.adfilter_segments)
        .where(adfilter_fp.adfilter_segments.c.fp_hash == FP_A)
    ).scalar()
check("删除后 A 明细为 0（两表同删）", seg_a_after == 0, seg_a_after)

# ---------------- 4. 关键回归：rebuild 后 A 不复活 ----------------
adfilter_fp.rebuild()
check("rebuild 后 A 仍不存在（不复活）", adfilter_fp.get_fingerprint(FP_A) is None)
check("rebuild 后 B 仍存在", adfilter_fp.get_fingerprint(FP_B) is not None)
dl_after = adfilter_fp.downloadable()
check("删除后 downloadable 只剩 B（1 条）", len(dl_after) == 1, len(dl_after))
check("downloadable 不含已删 A", all(
    (x.get("fp_hash") if isinstance(x, dict) else getattr(x, "fp_hash", None)) != FP_A
    for x in dl_after
))

# ---------------- 5. 异常路径：不存在 / 空 hash ----------------
try:
    adfilter_fp.delete_fingerprint("0" * 64)
    check("删除不存在的指纹抛 LookupError", False, "未抛异常")
except LookupError:
    check("删除不存在的指纹抛 LookupError", True)
except Exception as e:
    check("删除不存在的指纹抛 LookupError", False, type(e).__name__)

for bad in ("", "   ", None):
    try:
        adfilter_fp.delete_fingerprint(bad)
        check(f"空 fp_hash({bad!r}) 抛 FPError", False, "未抛异常")
    except adfilter_fp.FPError:
        check(f"空 fp_hash({bad!r}) 抛 FPError", True)
    except Exception as e:
        check(f"空 fp_hash({bad!r}) 抛 FPError", False, type(e).__name__)

# ---------------- 6. 路由层：DELETE 接口注册 + 404 映射 ----------------
from app.main import app  # noqa: E402
spec = app.openapi()
route_path = "/api/v1/adfilter/admin/fingerprints/{fp_hash}"
check("DELETE 路由已注册", route_path in spec["paths"] and "delete" in spec["paths"].get(route_path, {}),
      list(spec["paths"].get(route_path, {}).keys()))
from app import __version__ as _cur_ver  # noqa: E402
check("openapi 版本与代码一致", spec["info"]["version"] == _cur_ver, spec["info"]["version"])

print("\n==== SUMMARY ====")
if fails:
    print(f"FAILED: {len(fails)} -> {fails}")
    sys.exit(1)
print("ALL PASS")
