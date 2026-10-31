"""
端到端并发压测（离线，自动起桩源站 + 起真实 uvicorn 进程）。

用途：在 NAS/本地复现并验证「面板连点十几个全量」这类场景不会再打爆服务。
检查项：
  1. 12 个源站同时触发全量，服务端同时真正在跑的不超过 SOURCE_SYNC_CONCURRENCY 个
  2. 期间反复请求 /healthz、/api/v1/stats、/api/v1/sources 全部 200（不出现连接池 500）
  3. 结束后连接池 checked_out 归零（没有泄漏）

运行：python scripts/loadtest_concurrency.py
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

N_SOURCES = 12
LIMIT = 2                    # SOURCE_SYNC_CONCURRENCY
PAGES = 3                    # 每个源站 3 页，制造持续写入
ITEMS_PER_PAGE = 20


# ---------------------------------------------------------------------------
# 桩源站：按 pg 参数返回 MacCMS 格式 JSON
# ---------------------------------------------------------------------------
class StubHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        from urllib.parse import parse_qs, urlparse

        q = parse_qs(urlparse(self.path).query)
        pg = int((q.get("pg") or ["1"])[0])
        items = [{
            "vod_id": pg * 1000 + i,
            "type_id": 1,
            "type_name": "电影",
            "vod_name": f"压测片-{pg}-{i}",
            "vod_pic": "",
            "vod_remarks": "HD",
            "vod_content": "简介",
            "vod_play_url": "第01集$http://example.com/1.m3u8",
            "vod_time": "2026-10-31 10:00:00",
        } for i in range(ITEMS_PER_PAGE)]
        body = json.dumps({
            "code": 1, "page": pg, "pagecount": PAGES,
            "total": PAGES * ITEMS_PER_PAGE, "list": items, "class": [],
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def get(url):
    with urllib.request.urlopen(url, timeout=10) as r:
        return r.status, json.loads(r.read().decode())


def main():
    data_dir = tempfile.mkdtemp(prefix="vodsync-load-")
    stub = ThreadingHTTPServer(("127.0.0.1", 0), StubHandler)
    stub_port = stub.server_address[1]
    threading.Thread(target=stub.serve_forever, daemon=True).start()

    # ---- 建库 + 注册 N_SOURCES 个指向桩源站的源站 ----
    os.environ["DATA_DIR"] = data_dir
    os.environ["FULL_SYNC_ON_DISCOVER"] = "false"
os.environ["AUTH_ENABLED"] = "false"   # 测试只关心业务逻辑，关掉鉴权
    from app import database, syncer
    from app.models import sources

    database.init_db()
    slugs = [f"load{i:02d}" for i in range(N_SOURCES)]
    for i, s in enumerate(slugs):
        syncer.ensure_table(s)
        with database.write_txn() as conn:
            conn.execute(sources.insert().values(
                name=f"压测站{i}", slug=s,
                api_url=f"http://127.0.0.1:{stub_port}/api.php/provide/vod",
                enabled=1, auto_sync=1, paused=0,
            ))
    database.engine.dispose()      # 让子进程独占数据库文件

    port = free_port()
    env = dict(os.environ, DATA_DIR=data_dir, FULL_SYNC_ON_DISCOVER="false",
               SOURCE_SYNC_CONCURRENCY=str(LIMIT), PAGE_CONCURRENCY="2",
               REQUEST_INTERVAL_SECONDS="0", PROXY_ENABLED="false")
    log_path = os.path.join(data_dir, "server.log")
    log_file = open(log_path, "w+", encoding="utf-8", errors="replace")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
         "--port", str(port), "--log-level", "warning"],
        cwd=ROOT, env=env, stdout=log_file, stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        # 等服务就绪
        for _ in range(60):
            try:
                get(base + "/healthz")
                break
            except Exception:
                time.sleep(0.5)
        else:
            raise RuntimeError("服务未能启动")

        print(f"[准备] 桩源站 :{stub_port} / 应用 :{port} / {N_SOURCES} 个源站 / 并发上限 {LIMIT}")

        # ---- 同时触发 12 个全量同步 ----
        codes = []
        for s in slugs:
            req = urllib.request.Request(f"{base}/api/v1/sync/{s}?mode=full", method="POST")
            with urllib.request.urlopen(req, timeout=10) as r:
                codes.append(r.status)
        print(f"[触发] {len(codes)} 个同步请求，全部受理: {set(codes) == {200}}")

        # ---- 边跑边探活，记录峰值并发与失败次数 ----
        peak_running = 0
        peak_queued = 0
        probe_fail = 0
        probes = 0
        deadline = time.time() + 90
        while time.time() < deadline:
            try:
                st, stats = get(base + "/api/v1/stats")
                probes += 1
                if st != 200:
                    probe_fail += 1
                load = stats.get("sync_load") or {}
                peak_running = max(peak_running, load.get("running", 0))
                peak_queued = max(peak_queued, load.get("queued", 0))

                s2, srcs = get(base + "/api/v1/sources")
                probes += 1
                if s2 != 200:
                    probe_fail += 1
                s3, _ = get(base + "/healthz")
                probes += 1
                if s3 != 200:
                    probe_fail += 1

                # 结束条件：全部源站都已生成水位线（全量跑完）且没有在跑/排队的任务
                done = all(x["synced"] and not x["running"] and not x["queued"]
                           for x in srcs["list"])
                if done and load.get("running", 0) == 0 and load.get("queued", 0) == 0:
                    break
            except Exception as e:  # noqa: BLE001
                probe_fail += 1
                probes += 1
                print("  探活异常:", e)
            time.sleep(0.4)

        time.sleep(0.5)            # 等最后一个任务的收尾写入落盘
        st, stats = get(base + "/api/v1/stats")
        pool = stats.get("db_pool") or {}
        print(f"[结果] 峰值同时运行 = {peak_running}（上限 {LIMIT}）")
        print(f"[结果] 峰值排队     = {peak_queued}")
        print(f"[结果] 探活 {probes} 次，失败 {probe_fail} 次")
        print(f"[结果] 结束时连接池 = {pool}")
        print(f"[结果] 入库总条数   = {stats.get('videos_total')}（期望 {N_SOURCES * PAGES * ITEMS_PER_PAGE}）")
        _, srcs = get(base + "/api/v1/sources")
        bad = [x for x in srcs["list"] if x["total_videos"] != PAGES * ITEMS_PER_PAGE]
        if bad:
            for x in bad[:8]:
                print("   异常源站:", x["slug"], "total=", x["total_videos"],
                      "last_page=", x["last_page"], "total_pages=", x["total_pages"],
                      "running=", x["running"], "queued=", x["queued"])
            try:
                log_file.flush()
                with open(log_path, encoding="utf-8", errors="replace") as f:
                    print("   服务端输出尾部:", f.read()[-2000:])
            except Exception:
                pass

        ok = (peak_running <= LIMIT and probe_fail == 0
              and pool.get("checked_out", 0) == 0
              and stats.get("videos_total") == N_SOURCES * PAGES * ITEMS_PER_PAGE)
        print("\nLOADTEST_OK" if ok else "\nLOADTEST_FAILED")
        return 0 if ok else 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        stub.shutdown()
        shutil.rmtree(data_dir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
