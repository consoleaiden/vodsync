"""
v1.10.0 性能基准（对比 REVIEW_2026-11-07 第 1 节的旧数字）。

测什么：
  1. _load_feats（keyset 分页）耗时与内存峰值
  2. compute_source 整站重算，默认参数（cap=140 / BRUTE_FORCE_MAX=400）
  3. 同一份数据把 cap 调回 600（旧默认）看差多少
  4. 增量重算 compute_source(only_ids=[1]) 的固定成本（合并窗口把它从「每批一次」降到「每窗口一次」）

运行：python scripts/bench_v110_perf.py [行数，默认 10000]
"""
import os
import random
import sys
import tempfile
import time
import tracemalloc

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="vodsync-bench110-")
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(os.environ["DATA_DIR"], "b.db")
os.environ["AUTH_ENABLED"] = "false"
os.environ["FULL_SYNC_ON_DISCOVER"] = "false"
os.environ["RELATED_STAR_INFER_MODE"] = "off"

from app import config, database, related, syncer  # noqa: E402
from app.models import build_vod_table, sources  # noqa: E402

SLUG = "bench"
POOL = "的一是了我不人在他有这上们来到时大地为子中你说生国年着就那和要她出也得里后自以会家可下而过天去能对小多然于心学么之都好看起发当没成只如事把还用第样道想作种开美总从无情己面最女但现前些所同日手又行意动方期它头经长儿回位分爱老因很给名法间斯知世什两次使身者被高已亲其进此话常与活正感"


def build_rows(n: int) -> list:
    random.seed(7)
    cats = [("电影", 30), ("电视剧", 13), ("动漫", 26), ("综艺", 32)]
    areas = ["大陆", "香港", "美国", "日本", "韩国"]
    rows = []
    for i in range(1, n + 1):
        if i % 20 == 0:
            name = f"流浪地球{i % 9}"          # 5% 系列片：共享前缀（真实片名的典型形态）
        else:
            name = "".join(random.choice(POOL) for _ in range(4))
        tn, tid = cats[i % 4]
        rows.append({
            "vod_id": i, "type_id": tid, "type_name": tn, "name": name,
            "pic": "", "remarks": "HD", "content": "简介" * 20, "play_url": "",
            "vod_actor": f"演员{i % 800},演员{(i * 7) % 800}", "vod_director": f"导演{i % 400}",
            "vod_writer": "", "vod_area": areas[i % 5],
            "vod_year": str(2005 + i % 20), "vod_tag": "",
            "vod_time": f"2026-{1 + i % 12:02d}-01 00:00:00",
        })
    return rows


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 10000
    database.init_db()
    with database.write_txn() as conn:
        conn.execute(sources.insert().values(
            name="bench", slug=SLUG, api_url="http://bench/api", status="ok",
            enabled=1, total_videos=n, origin="custom"))
    syncer.ensure_table(SLUG)
    t0 = time.time()
    rows = build_rows(n)
    for i in range(0, n, 1000):
        syncer.upsert_batch(SLUG, rows[i:i + 1000])
    print(f"[准备] 入库 {n} 行耗时 {time.time() - t0:.1f}s")
    print(f"[配置] weights={config.RELATED_WEIGHTS}")
    print(f"[配置] cap={config.RELATED_CANDIDATE_CAP} brute_max={config.RELATED_BRUTE_FORCE_MAX} "
          f"fallback_limit={config.RELATED_FALLBACK_LIMIT} prior={config.RELATED_EVIDENCE_PRIOR}")

    tracemalloc.start()
    t0 = time.time()
    kept, dup, post, by_time, dup_ids = related._load_feats(SLUG)
    load_s = time.time() - t0
    _cur, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    print(f"[load] {len(kept)} 行 kept, {load_s:.2f}s, 峰值 {peak / 1048576:.0f}MB "
          f"({peak / max(1, len(kept)) / 1024:.2f}KB/行), postings={post is not None}")

    t0 = time.time()
    st = related.compute_source(SLUG, reason="bench-default")
    s = time.time() - t0
    print(f"[full] 默认参数 {s:.1f}s / {n} 视频 = {s * 1000 / n:.2f} ms/video "
          f"（fallback 条目 {st['fallback_entries']}）")

    t0 = time.time()
    related.compute_source(SLUG, only_ids=[1], reason="bench-incr")
    s2 = time.time() - t0
    print(f"[incr] 单条增量 {s2:.2f}s（合并窗口内每站每个窗口只付这一份成本）")

    config.RELATED_CANDIDATE_CAP = 600
    t0 = time.time()
    related.compute_source(SLUG, reason="bench-cap600")
    s3 = time.time() - t0
    print(f"[full] cap=600（旧默认）{s3:.1f}s = {s3 * 1000 / n:.2f} ms/video，"
          f"提速 {s3 / s:.1f}×")


if __name__ == "__main__":
    main()
