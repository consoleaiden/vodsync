"""
v1.9.0 相关推荐性能基准（验收：批处理 1 万视频内存峰值 < 256MB、给出实测耗时）。

生成 10,000 条合成视频（标题/演员/分类/年代均带真实分布特征），
跑 compute_source 全量计算，报告：
  - 内存峰值（tracemalloc）
  - 总耗时 / 单条均耗时
  - 推荐表写入行数与 JSON 平均大小
  - 读接口耗时（预计算直读路径）

运行：python scripts/bench_v190_related.py
"""
import json
import os
import random
import sys
import tempfile
import time
import tracemalloc

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="vodsync-bench-")
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(os.environ["DATA_DIR"], "bench.db")
os.environ["AUTH_ENABLED"] = "false"

TOTAL = int(os.environ.get("BENCH_ROWS", "10000"))

random.seed(42)

CLASSES = ["科幻片", "动作片", "爱情片", "国产剧", "美剧", "日剧", "动漫", "综艺", "纪录片"]
AREAS = ["中国大陆", "美国", "日本", "韩国", "中国香港", "英国", "法国"]
ACTORS = [f"演员{i:03d}" for i in range(600)]
DIRECTORS = [f"导演{i:03d}" for i in range(200)]
TITLE_PARTS = ["星际", "风云", "传奇", "迷雾", "追光", "破晓", "暗夜", "山海", "狂澜", "归途",
               "无双", "风暴", "孤城", "掠影", "长歌", "灼心", "回流", "惊蛰", "白夜", "密室"]


def gen_rows(n: int) -> list:
    rows = []
    for i in range(n):
        # 30% 的条目属于"系列"（共享前缀 + 季/部序号），模拟标题核心信号
        if i % 10 < 3:
            base = f"{random.choice(TITLE_PARTS)}{random.choice(TITLE_PARTS)}系列"
            season = random.randint(1, 4)
            name = f"{base}第{season}季"
        else:
            name = f"{random.choice(TITLE_PARTS)}{random.choice(TITLE_PARTS)}{random.choice(['','之','记','录','传'])}{i % 97}"
        n_actor = random.randint(0, 5)
        rows.append({
            "vod_id": 1000 + i,
            "type_id": random.randint(1, 9),
            "type_name": random.choice(CLASSES),
            "name": name,
            "pic": f"pic/{i}.jpg",
            "remarks": "HD",
            "content": "",
            "play_url": "",
            # 40% 条目缺演员（模拟数据稀疏）
            "vod_actor": ",".join(random.sample(ACTORS, n_actor)) if random.random() > 0.4 else "",
            "vod_director": random.choice(DIRECTORS) if random.random() > 0.5 else "",
            "vod_writer": "",
            "vod_area": random.choice(AREAS) if random.random() > 0.3 else "",
            "vod_year": str(random.randint(2000, 2026)) if random.random() > 0.2 else "",
            "vod_tag": "",
            "vod_time": f"2026-01-{(i % 28) + 1:02d} 00:00:00",
        })
    return rows


def main():
    from app import database, related, syncer
    from app.models import sources, vod_related
    from sqlalchemy import func, select

    database.init_db()
    SLUG = "bench"
    with database.write_txn() as conn:
        conn.execute(sources.insert().values(
            name="基准", slug=SLUG, api_url="https://bench.example/api.php",
            status="ok", enabled=1, origin="custom", total_videos=TOTAL,
        ))
    syncer.ensure_table(SLUG)

    print(f"生成 {TOTAL} 条合成数据（含系列/稀疏字段分布）...")
    rows = gen_rows(TOTAL)
    t0 = time.time()
    for i in range(0, len(rows), 1000):
        syncer.upsert_batch(SLUG, rows[i : i + 1000])
    print(f"入库耗时 {time.time() - t0:.1f}s")

    tracemalloc.start()
    t0 = time.time()
    stats = related.compute_source(SLUG, reason="bench")
    elapsed = time.time() - t0
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    with database.engine.connect() as conn:
        n = conn.execute(select(func.count()).select_from(vod_related)).scalar()
        sample = conn.execute(
            select(vod_related.c.related_json).limit(200)
        ).scalars().all()
    avg_json = sum(len(s) for s in sample) / max(1, len(sample))

    # 读路径耗时（预计算直读）
    table = __import__("app.models", fromlist=["build_vod_table"]).build_vod_table(SLUG)
    t0 = time.time()
    rounds = 200
    for i in range(rounds):
        vid = 1000 + (i * 37) % TOTAL
        entries, fb = related.get_related_entries(SLUG, vid, 12, table)
        related.enrich_cards(SLUG, "基准", entries, table)
    read_ms = (time.time() - t0) / rounds * 1000

    print("\n===== 性能基准结果 =====")
    print(f"视频条数          : {TOTAL}")
    print(f"计算总耗时        : {elapsed:.1f}s（单条均 {elapsed / TOTAL * 1000:.2f}ms）")
    print(f"内存峰值          : {peak / 1048576:.1f} MB")
    print(f"推荐表行数        : {n}")
    print(f"兜底条目数        : {stats.get('fallback_entries')}")
    print(f"related_json 均长 : {avg_json:.0f} 字节")
    print(f"读接口均耗时      : {read_ms:.2f} ms（含 IN 补卡片）")
    print(f"验收（<256MB）    : {'PASS' if peak / 1048576 < 256 else 'FAIL'}")
    print(f"验收（<50ms）     : {'PASS' if read_ms < 50 else 'FAIL'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
