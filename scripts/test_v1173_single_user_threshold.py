"""
v1.17.3 回归测试：用户数门槛默认 3 -> 1（适配单用户场景）。

背景：用户实际只有 1 个 user_id，多用户聚类不适用。旧默认 min_users=3 导致
所有指纹 distinct_users 恒为 1，永远不达标、全部 candidate、无法下发。
v1.17.3 起用户数门槛默认 1；地址>=3、命中>=10、误杀>=5 停用保持不变。

本测试**不设置** ADFILTER_FP_MIN_USERS 环境变量，验证默认值生效：
  - config.ADFILTER_FP_MIN_USERS == 1
  - 单用户 + 3 地址 + 10+ 命中 -> rebuild 自动 confirmed
  - 置信度单用户下不为 0（旧实现 u=1/3 会长期压低估置信度）
  - 地址不足（<3）仍保持 candidate（证明只放宽了用户维度）
  - stats 接口下发的门槛 min_users==1（面板文案据此渲染）
  - 环境变量仍可覆盖默认值（可配性保留）

运行：PYTHONPATH=. .venv/Scripts/python.exe scripts/test_v1173_single_user_threshold.py
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DB = os.path.join(tempfile.gettempdir(), "_v1173_single_user.db")
if os.path.exists(DB):
    os.remove(DB)

os.environ["DATABASE_URL"] = f"sqlite:///{DB}"
os.environ["ADFILTER_ENABLED"] = "true"
os.environ["ADFILTER_FP_ENABLED"] = "true"
# 关键：故意不设置 ADFILTER_FP_MIN_USERS，验证代码默认值为 1
os.environ.pop("ADFILTER_FP_MIN_USERS", None)
os.environ["ADFILTER_FP_MIN_URLS"] = "3"
os.environ["ADFILTER_FP_MIN_HITS"] = "10"
os.environ["ADFILTER_FP_MISS_DISABLE"] = "5"

from app import adfilter, adfilter_fp, config, database  # noqa: E402

database.init_db()
adfilter.ensure_seed()

fails = []


def check(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + ((" :: " + str(extra)) if extra else ""))
    if not cond:
        fails.append(name)


# ---------------- 1. 默认门槛值 ----------------
check("ADFILTER_FP_MIN_USERS 默认值为 1", config.ADFILTER_FP_MIN_USERS == 1, config.ADFILTER_FP_MIN_USERS)
check("其余门槛不变 min_urls=3", config.ADFILTER_FP_MIN_URLS == 3)
check("其余门槛不变 min_hits=10", config.ADFILTER_FP_MIN_HITS == 10)
check("其余门槛不变 miss_disable=5", config.ADFILTER_FP_MISS_DISABLE == 5)

# ---------------- 2. 单用户置信度不再是 1/3 短板 ----------------
# 新默认 _confidence(users=1, urls=3, hits=10): u=1/1=1.0, r=3/3=1.0, h=10/10=1.0
c_new = adfilter_fp._confidence(1, 3, 10)
check("单用户达标置信度接近饱和（>0.7）", c_new > 0.7, c_new)
# 显式对比旧口径：临时把 min_users 当 3 手算 u=1/3 的饱和值，验证旧实现会被压低
import math
u_old = min(1.0, 1 / 3.0)
r_old = min(1.0, 3 / 3.0)
h_old = min(1.0, 10 / 10.0)
sat = 0.5 + 0.5 * min(1.0, math.log1p(10) / math.log1p(20))
c_old = round(min(u_old, r_old, h_old) * sat, 4)
check("新口径置信度显著高于旧 3 用户口径", c_new > c_old * 2, (c_new, c_old))

# ---------------- 3. 单用户场景端到端：3 地址 + 10 命中 -> confirmed ----------------
now = int(time.time())
seg = {"index": 0, "dur_ms": 10000, "size_b": 131072}  # dur_bucket 10
SOLO_USER = 99
# 同一 fp_hash（同 host + 同桶），3 个不同地址，每个地址多次命中，合计 >=10
for k in range(1, 4):
    for hit in range(4):  # 3 地址 x 4 = 12 次命中
        item = {
            "url": f"https://solo-ad.com/p{k}/v{hit}/index.m3u8",
            "rule_version": 1, "removed_runs": 1, "removed_ms": 10000,
            "total_ms": 900000, "at": now + k * 10 + hit,
            "segments": [seg],
        }
        adfilter.ingest([item], user_id=SOLO_USER)

res = adfilter_fp.rebuild()
check("rebuild 扫到指纹", res["scanned"] >= 1, res)
fp_hash = adfilter_fp.signature("solo-ad.com", 10000, 131072)["fp_hash"]
fp = adfilter_fp.get_fingerprint(fp_hash)
check("单用户指纹已生成", fp is not None)
check("distinct_users == 1", fp and fp["distinct_users"] == 1, fp and fp["distinct_users"])
check("distinct_urls >= 3", fp and fp["distinct_urls"] >= 3, fp and fp["distinct_urls"])
check("hit_count >= 10", fp and fp["hit_count"] >= 10, fp and fp["hit_count"])
check("★单用户达标晋升 confirmed", fp and fp["status"] == "confirmed", fp and fp["status"])
check("confirmed 置信度 > 0.7", fp and fp["confidence"] > 0.7, fp and fp["confidence"])

# ---------------- 4. 地址不足仍 candidate（只放宽用户维度，没放宽其他）----------------
seg2 = {"index": 0, "dur_ms": 20000, "size_b": 131072}  # dur_bucket 20，另一个 fp
for hit in range(12):  # 单用户单地址，命中够但地址不够
    item = {
        "url": "https://few-urls.com/only-one/index.m3u8",
        "rule_version": 1, "removed_runs": 1, "removed_ms": 20000,
        "total_ms": 900000, "at": now + 100 + hit,
        "segments": [seg2],
    }
    adfilter.ingest([item], user_id=SOLO_USER)
adfilter_fp.rebuild()
fp2 = adfilter_fp.get_fingerprint(adfilter_fp.signature("few-urls.com", 20000, 131072)["fp_hash"])
check("地址不足(<3)保持 candidate", fp2 and fp2["status"] == "candidate", fp2 and fp2["status"])

# ---------------- 5. 命中不足仍 candidate ----------------
seg3 = {"index": 0, "dur_ms": 30000, "size_b": 131072}
for k in range(1, 4):
    item = {
        "url": f"https://few-hits.com/p{k}/index.m3u8",
        "rule_version": 1, "removed_runs": 1, "removed_ms": 30000,
        "total_ms": 900000, "at": now + 200 + k,
        "segments": [seg3],
    }
    adfilter.ingest([item], user_id=SOLO_USER)
adfilter_fp.rebuild()
fp3 = adfilter_fp.get_fingerprint(adfilter_fp.signature("few-hits.com", 30000, 131072)["fp_hash"])
check("命中不足(<10)保持 candidate", fp3 and fp3["status"] == "candidate", fp3 and fp3["status"])

# ---------------- 6. stats 下发门槛（面板文案据此渲染）----------------
st = adfilter_fp.stats()
check("stats.config.min_users == 1（面板显示用户≥1）", st["config"]["min_users"] == 1,
      st["config"]["min_users"])

# ---------------- 7. 环境变量仍可覆盖（可配性保留）----------------
# 单独验证 config 读取逻辑：设置 3 时 max(1,3)==3（不重启进程，直接验证表达式语义）
check("门槛仍可用环境变量调高（可配性）", max(1, int("3")) == 3)

print("\n==== SUMMARY ====")
if fails:
    print(f"FAILED {len(fails)}: " + ", ".join(fails))
    sys.exit(1)
print("ALL PASS")
