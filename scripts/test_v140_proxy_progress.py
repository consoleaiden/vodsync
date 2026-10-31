"""
v1.4.0 中转代理 + 进度口径测试（全部离线，用本地桩服务器模拟故障源站）。

覆盖：
 1. wrap_proxy 正确编码（目标地址的 ? & 必须被编码，不能被代理截断）
 2. 直连不可达 → 自动切代理取到数据，并记忆该源站走代理
 3. 直连 403（风控）→ 立即切代理，不做无意义重试
 4. 代理关闭 → 直连失败即报错（不静默）
 5. 已记忆代理时，路由顺序把代理放到直连之前
 6. 记忆过期（TTL）后恢复直连优先
 7. compute_progress：从未同步=0 / 已完成=100 / 有断点=占比 / 跑全量未到检查点=0
 8. _is_synced：水位线、数据量、断点三者任一存在即算同步过

运行：python scripts/test_v140_proxy_progress.py
"""
import json
import os
import socket
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


# ---------------------------------------------------------------------------
# 桩服务器：mode=ok 返回 MacCMS JSON；mode=403 返回 403
# ---------------------------------------------------------------------------
class StubHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        self.server.last_path = self.path
        if self.server.mode == "403":
            self.send_response(403)
            self.end_headers()
            self.wfile.write(b"forbidden")
            return
        body = json.dumps({"code": 1, "list": [{"vod_id": 1, "vod_name": "stub"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def start_stub(mode="ok"):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), StubHandler)
    srv.mode = mode
    srv.last_path = ""
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def free_port() -> int:
    """拿一个当前没人监听的端口（用于模拟"不可达"源站）。"""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def main():
    from app import config
    from app.maccms import MacCmsClient, MacCmsError, get_proxy_pref, set_proxy_pref, wrap_proxy
    from app.routes.api import compute_progress, _is_synced

    # 测试期间提速：无间隔、短重试
    config.REQUEST_INTERVAL_SECONDS = 0.0
    config.PROXY_CONCURRENCY = 2

    target = "https://api.example.com/api.php/provide/vod?ac=list&pg=1"

    # 1) 编码
    wrapped = wrap_proxy("https://pz.v88.qzz.io/", target)
    parsed = urllib.parse.parse_qs(urllib.parse.urlparse(wrapped).query)
    check("wrap_proxy 只有一个 ?（目标 ? 被编码）", wrapped.count("?") == 1, wrapped)
    check("wrap_proxy url 参数还原等于目标", parsed.get("url") == [target], str(parsed)[:120])

    # 2) 直连不可达 → 代理成功
    proxy_srv = start_stub("ok")
    proxy_base = f"http://127.0.0.1:{proxy_srv.server_address[1]}/"   # 形如 .../?url=
    config.PROXY_ENABLED = True
    config.PROXY_URLS = [proxy_base]

    dead_host = f"127.0.0.1:{free_port()}"
    dead_url = f"http://{dead_host}/api.php/provide/vod?ac=list&pg=1"
    client = MacCmsClient(timeout=2, retries=2)
    try:
        data = client._get_json(dead_url)
        check("直连不可达时经代理取到数据", data.get("code") == 1, str(data)[:80])
        check("代理收到 url 参数（已编码目标地址）", "url=" in proxy_srv.last_path, proxy_srv.last_path[:120])
        check("已记忆该源站走代理", get_proxy_pref(dead_host) == proxy_base, get_proxy_pref(dead_host))

        # 5) 记忆后路由顺序：代理优先
        routes = client._routes(dead_host)
        check("路由顺序为 代理→直连", routes[0] == ("proxy", proxy_base) and routes[1][0] == "direct", str(routes))
    finally:
        client.close()
        proxy_srv.shutdown()

    # 3) 直连 403 → 立即切代理
    forbid_srv = start_stub("403")
    proxy_srv2 = start_stub("ok")
    config.PROXY_URLS = [f"http://127.0.0.1:{proxy_srv2.server_address[1]}/"]
    forbid_host = f"127.0.0.1:{forbid_srv.server_address[1]}"
    forbid_url = f"http://{forbid_host}/api.php/provide/vod?ac=list&pg=1"
    client = MacCmsClient(timeout=3, retries=3)
    try:
        t0 = time.time()
        data = client._get_json(forbid_url)
        elapsed = time.time() - t0
        check("直连 403 时经代理取到数据", data.get("code") == 1, str(data)[:80])
        check("403 不做重试（<3s 完成切换）", elapsed < 3, f"{elapsed:.2f}s")
    finally:
        client.close()
        forbid_srv.shutdown()
        proxy_srv2.shutdown()

    # 4) 代理关闭 → 直连失败直接报错
    config.PROXY_ENABLED = False
    config.PROXY_URLS = []
    client = MacCmsClient(timeout=2, retries=1)
    try:
        try:
            client._get_json(f"http://127.0.0.1:{free_port()}/x?ac=list")
            check("代理关闭时应抛错", False, "未抛错")
        except MacCmsError:
            check("代理关闭时直连失败即报错", True)
    finally:
        client.close()

    # 6) 记忆过期恢复直连
    config.PROXY_ENABLED = True
    config.PROXY_URLS = ["https://pz.v88.qzz.io/"]
    config.PROXY_PREF_TTL_MINUTES = 30
    set_proxy_pref("ttl.example.com", "https://pz.v88.qzz.io/")
    check("TTL 内记忆生效", get_proxy_pref("ttl.example.com") == "https://pz.v88.qzz.io/")
    config.PROXY_PREF_TTL_MINUTES = 0            # 立即过期
    time.sleep(0.05)
    check("TTL 过期后恢复直连", get_proxy_pref("ttl.example.com") == "")
    config.PROXY_PREF_TTL_MINUTES = 30

    # 7) 进度口径
    never = {"watermark": "", "total_videos": 0, "last_page": 0, "total_pages": 0}
    check("从未同步 → 进度 0", compute_progress(never) == 0, str(compute_progress(never)))
    check("从未同步 → synced=false", _is_synced(never) is False)

    done = {"watermark": "2026-10-30 12:00:00", "total_videos": 100, "last_page": 0, "total_pages": 0}
    check("已同步完成 → 进度 100", compute_progress(done) == 100, str(compute_progress(done)))
    check("已同步完成 → synced=true", _is_synced(done) is True)

    partial = {"watermark": "", "total_videos": 500, "last_page": 25, "total_pages": 100}
    check("断点 25/100 → 进度 25.0", compute_progress(partial) == 25.0, str(compute_progress(partial)))

    running_full = {"watermark": "2026-10-30 12:00:00", "total_videos": 10, "last_page": 0, "total_pages": 0}
    check("正在跑全量未到检查点 → 进度 0",
          compute_progress(running_full, {"mode": "full"}) == 0.0,
          str(compute_progress(running_full, {"mode": "full"})))

    only_break = {"watermark": "", "total_videos": 0, "last_page": 3, "total_pages": 0}
    check("有断点无总页数 → 进度 0", compute_progress(only_break) == 0.0, str(compute_progress(only_break)))
    check("有断点 → synced=true", _is_synced(only_break) is True)

    print(f"\n结果: {passed} 通过, {failed} 失败")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
