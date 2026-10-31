"""
v1.4.1 连接池耗尽 / 并发失控 回归测试（离线，不访问外网）。

复现的真实故障（NAS 上 v1.4.0）：
  面板连点十几个「全量」→ 每个请求各起一个线程 → 十几个写线程抢 SQLite 唯一的写锁
  → 等锁线程占着连接池连接 → 池满（5+10）→ /healthz 与面板全部 500/超时 → 容器被判不健康

覆盖点：
  1. 连接池参数按配置生效（pool_size / max_overflow / pool_timeout）
  2. GET /api/v1/videos 不再泄漏连接（连续 40 次后 checked_out 归零）
  3. 全局并发闸门：6 个源站同时触发，实际并发 ≤ SOURCE_SYNC_CONCURRENCY
  4. 防重入：同一源站重复触发返回 already_running，不会双写
  5. 写事务串行化：8 线程并发写不出现 "database is locked"
  6. /healthz 在连接池耗尽时仍返回 200（status=degraded），不会让 Docker 反复重启容器
  7. _save_checkpoint 在写事务外统计条数，断点与条数均正确

运行：python scripts/test_v141_pool.py
"""
import os
import shutil
import sys
import tempfile
import threading
import time

# 必须在导入 app 之前设置
_TMP = tempfile.mkdtemp(prefix="vodsync-pool-test-")
os.environ["DATA_DIR"] = _TMP
os.environ["FULL_SYNC_ON_DISCOVER"] = "false"
os.environ["SOURCE_SYNC_CONCURRENCY"] = "2"
os.environ["DB_POOL_SIZE"] = "10"
os.environ["DB_MAX_OVERFLOW"] = "20"
os.environ["DB_POOL_TIMEOUT"] = "10"
os.environ["AUTH_ENABLED"] = "false"   # 测试只关心业务逻辑，关掉鉴权

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


def make_source(slug: str, rows: int = 3):
    """注册一个源站并写入若干行（不联网）。"""
    from app import database, syncer
    from app.models import sources
    from sqlalchemy import select

    syncer.ensure_table(slug)
    with database.engine.connect() as conn:
        exists = conn.execute(select(sources.c.slug).where(sources.c.slug == slug)).first()
    if not exists:
        with database.write_txn() as conn:
            conn.execute(sources.insert().values(
                name=f"测试站{slug}", slug=slug,
                api_url=f"http://127.0.0.1:1/{slug}/api.php",
                enabled=1, auto_sync=1, paused=0,
            ))
    data = [{
        "vod_id": i, "type_id": 1, "type_name": "电影", "name": f"{slug}-片{i}",
        "pic": "", "remarks": "", "content": "", "play_url": "", "vod_time": "2026-10-30 10:00:00",
    } for i in range(1, rows + 1)]
    syncer.upsert_batch(slug, data)


def main():
    from app import config, database, syncer
    from app.main import app

    print("[1] 连接池参数")
    pool = database.engine.pool
    check("pool_size 按配置生效 (10)", pool.size() == 10, str(pool.size()))
    check("max_overflow 按配置生效 (20)", pool._max_overflow == 20, str(pool._max_overflow))
    check("pool_timeout 按配置生效 (10)", pool._timeout == 10, str(pool._timeout))
    check("并发闸门上限 = SOURCE_SYNC_CONCURRENCY", syncer.sync_load()["limit"] == 2,
          str(syncer.sync_load()))

    database.init_db()
    for s in ("po_a", "po_b", "po_c", "po_d", "po_e", "po_f"):
        make_source(s)

    with TestClient(app) as client:
        # ---- 2. 连接不泄漏 ----
        print("[2] GET /api/v1/videos 连接归还")
        codes = []
        for _ in range(40):
            r = client.get("/api/v1/videos?page=1&page_size=5")
            codes.append(r.status_code)
        time.sleep(0.3)   # 等启动引导线程的 refresh_counts 收尾
        leaked = pool.checkedout()
        check("40 次列表请求全部 200", set(codes) == {200}, str(set(codes))[:120])
        check("请求后连接全部归还（无泄漏）", leaked == 0, f"仍占用 {leaked} 个连接")

    # ---- 3. 全局并发闸门 ----
    print("[3] 全局并发闸门")
    peak = {"cur": 0, "max": 0}
    guard = threading.Lock()
    orig_full = syncer.full_sync

    def fake_full(slug, client):
        with guard:
            peak["cur"] += 1
            peak["max"] = max(peak["max"], peak["cur"])
        time.sleep(0.35)          # 模拟抓取+写入耗时
        with guard:
            peak["cur"] -= 1
        return {"fetched": 0, "inserted": 0, "updated": 0, "pages": 0}

    syncer.full_sync = fake_full
    try:
        results = []
        rlock = threading.Lock()

        def run(slug):
            r = syncer.sync_source(slug, "full")
            with rlock:
                results.append(r)

        t0 = time.time()
        threads = [threading.Thread(target=run, args=(s,))
                   for s in ("po_a", "po_b", "po_c", "po_d", "po_e", "po_f")]
        for t in threads:
            t.start()
        # ---- 4. 防重入（在跑期间重复触发同一站）----
        time.sleep(0.15)
        dup = syncer.sync_source("po_a", "full")
        check("同一源站重复触发返回 already_running", dup.get("status") == "already_running", str(dup))
        for t in threads:
            t.join()
        elapsed = time.time() - t0
        check("6 个源站同时触发，实际并发不超过 2", peak["max"] <= 2, f"峰值并发 {peak['max']}")
        check("全部任务最终成功完成", all(r.get("status") == "success" for r in results),
              str([r.get("status") for r in results]))
        check("任务按闸门排队执行（耗时符合串行分批）", elapsed >= 0.35 * 3 - 0.05,
              f"{elapsed:.2f}s")
    finally:
        syncer.full_sync = orig_full

    # ---- 5. 写事务串行化：并发写不报 database is locked ----
    print("[5] 并发写不报 database is locked")
    errors = []

    def write_loop(slug):
        try:
            for i in range(20):
                syncer.upsert_batch(slug, [{
                    "vod_id": 10_000 + i, "type_id": 2, "type_name": "剧集",
                    "name": f"并发{i}", "pic": "", "remarks": "", "content": "",
                    "play_url": "", "vod_time": "2026-10-30 11:00:00",
                }])
        except Exception as e:  # noqa: BLE001
            errors.append(f"{slug}: {type(e).__name__}: {e}")

    ts = [threading.Thread(target=write_loop, args=(s,)) for s in ("po_a", "po_b", "po_c", "po_d")]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    check("4 线程 × 20 次并发写全部成功", not errors, str(errors)[:200])
    check("并发写后连接全部归还", pool.checkedout() == 0, f"仍占用 {pool.checkedout()}")

    # ---- 6. /healthz 在池耗尽时仍 200 ----
    print("[6] 连接池耗尽时 /healthz 仍 200")
    held = []
    saved_timeout = pool._timeout
    try:
        for _ in range(10 + 20):          # size + max_overflow
            held.append(database.engine.connect())
        pool._timeout = 1                 # 缩短等待，测试更快
        # 直接用 TestClient 发请求但不用 with（避免触发 startup 里的 init_db 抢连接）
        bare = TestClient(app)
        r = bare.get("/healthz")
        body = r.json() if r.status_code == 200 else {}
        check("池满时 /healthz 返回 200", r.status_code == 200, str(r.status_code))
        check("池满时 /healthz 标记 db=false / degraded",
              body.get("db") is False and body.get("status") == "degraded", str(body)[:160])
    finally:
        pool._timeout = saved_timeout
        for c in held:
            c.close()
    time.sleep(0.1)
    check("释放后连接池恢复", pool.checkedout() == 0, f"仍占用 {pool.checkedout()}")

    # ---- 7. 断点保存（统计在写事务之外）----
    print("[7] 断点保存")
    syncer._save_checkpoint("po_a", 7)
    from sqlalchemy import select
    from app.models import sources
    with database.engine.connect() as conn:
        row = conn.execute(select(sources.c.last_page, sources.c.total_videos)
                           .where(sources.c.slug == "po_a")).first()
    check("断点已写入 (last_page=7)", row[0] == 7, str(row))
    check("条数已刷新 (3 条基础 + 20 条并发写)", row[1] == 23, str(row))

    print(f"\n{'=' * 46}\n通过 {passed} / {passed + failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    try:
        code = main()
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
    sys.exit(code)
