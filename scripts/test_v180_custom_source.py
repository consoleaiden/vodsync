"""
v1.8.0 自定义源站测试。

覆盖：
  A. API 地址校验（scheme 白名单 / 空白字符 / 空值）
  B. 连通性探测（成功 / 失败拒绝 / 强制添加 / 跳过探测）
  C. 添加：建表、origin 标记、地址去重（含尾斜杠）、slug 生成与冲突
  D. 持久化：重新读库仍在；refresh_sources 不覆盖名字、不删除
  E. 修改：改名、改地址清空数据并重置水位线、官方源拒绝
  F. 删除：confirm 校验、官方源拒绝、DROP 表与保留表、同 slug 可重建

探测与 latest.json 均用假客户端，不访问外网。

运行：python scripts/test_v180_custom_source.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="vodsync-custom-")
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(os.environ["DATA_DIR"], "custom.db")
os.environ["AUTH_ENABLED"] = "false"
os.environ["FULL_SYNC_ON_DISCOVER"] = "false"
os.environ["PROXY_ENABLED"] = "false"      # 端到端段要直连本地 mock 源站，别走中转
# 这台机器可能配了全局 HTTP_PROXY，httpx 默认 trust_env=True 会把 127.0.0.1 也丢给代理，
# 导致本地 mock 源站连不上（表现为 "重试 0 次仍失败 (None)"）。
# 生产环境添加内网源站时同样要注意这一条。
os.environ["NO_PROXY"] = "127.0.0.1,localhost"
os.environ["no_proxy"] = "127.0.0.1,localhost"

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
# 探测结果可控的假实现：api.py 里是 `from ..syncer import probe_source`，
# 所以必须替换 app.routes.api 上的这个名字，改 syncer 上的不起作用。
# ---------------------------------------------------------------------------
PROBE = {"ok": True, "total": 1200, "pagecount": 24,
         "samples": ["示例影片A", "示例影片B"], "elapsed_ms": 12}


def fake_probe(api_url, timeout=None):
    return dict(PROBE)


class FakeLatestClient:
    """冒充 MacCmsClient，只提供 refresh_sources 需要的 _get_json。"""

    def __init__(self, resources):
        self.resources = resources

    def _get_json(self, url):
        return {"resources": self.resources}

    def close(self):
        pass


def main():
    from fastapi.testclient import TestClient
    from sqlalchemy import func, select

    from app import database, syncer
    from app.main import app
    from app.models import build_vod_table, sources

    import app.routes.api as api_mod

    real_probe = api_mod.probe_source     # 保留真实实现，供端到端段使用
    api_mod.probe_source = fake_probe

    database.init_db()
    client = TestClient(app)

    API_A = "https://api.alpha.example.com/api.php/provide/vod/"
    API_B = "https://api.beta.example.com/api.php/provide/vod/"

    def db_row(slug):
        with database.engine.connect() as conn:
            return conn.execute(
                select(sources).where(sources.c.slug == slug)
            ).mappings().first()

    def count_rows(slug):
        table = build_vod_table(slug)
        if not database.table_exists(table.name):
            return -1
        with database.engine.connect() as conn:
            return conn.execute(select(func.count()).select_from(table)).scalar()

    # ------------------------------------------------------------------
    print("\n[A] API 地址校验")
    # ------------------------------------------------------------------
    r = client.post("/api/v1/sources/custom",
                    json={"name": "x", "api_url": "ftp://files.example.com/vod.json"})
    check("ftp:// 被拒绝 400", r.status_code == 400, r.status_code)

    r = client.post("/api/v1/sources/custom",
                    json={"name": "x", "api_url": "api.example.com/provide/vod"})
    check("缺少 scheme 被拒绝 400", r.status_code == 400, r.status_code)

    r = client.post("/api/v1/sources/custom",
                    json={"name": "x", "api_url": "https://api ex ample.com/vod"})
    check("含空白字符被拒绝 400", r.status_code == 400, r.status_code)

    r = client.post("/api/v1/sources/custom", json={"name": "x", "api_url": ""})
    check("空地址被 pydantic 拒绝 422", r.status_code == 422, r.status_code)

    # ------------------------------------------------------------------
    print("\n[B] 连通性探测")
    # ------------------------------------------------------------------
    r = client.post("/api/v1/sources/probe", json={"api_url": API_A})
    check("探测成功返回 ok=true", r.status_code == 200 and r.json()["ok"] is True, r.text[:120])
    check("探测返回 total/pagecount/samples",
          r.json().get("total") == 1200 and r.json().get("pagecount") == 24
          and len(r.json().get("samples") or []) == 2, str(r.json())[:150])

    r = client.post("/api/v1/sources/probe", json={"api_url": "file:///etc/passwd"})
    check("探测接口同样拦截非法 scheme", r.status_code == 400, r.status_code)

    PROBE.update({"ok": False, "error": "ConnectError: connection refused"})
    r = client.post("/api/v1/sources/custom",
                    json={"name": "探测失败站", "api_url": "https://fail.example.com/api/"})
    check("探测失败默认拒绝入库 400", r.status_code == 400, r.status_code)
    check("拒绝时给出原因", "ConnectError" in r.text, r.text[:120])

    r = client.post("/api/v1/sources/custom",
                    json={"name": "探测失败但强加", "api_url": "https://fail.example.com/api/",
                          "allow_unreachable": True})
    check("allow_unreachable 强制添加成功", r.status_code == 200, r.text[:120])
    check("强制添加后 origin=custom",
          (db_row(r.json()["slug"]) or {}).get("origin") == "custom")

    PROBE.update({"ok": True, "total": 1200, "pagecount": 24,
                  "samples": ["示例影片A"], "elapsed_ms": 5})
    r = client.post("/api/v1/sources/custom",
                    json={"name": "不探测站", "api_url": "https://noprobe.example.com/api/",
                          "probe": False})
    check("probe=false 跳过探测直接入库", r.status_code == 200, r.text[:120])

    # ------------------------------------------------------------------
    print("\n[C] 添加与去重")
    # ------------------------------------------------------------------
    r = client.post("/api/v1/sources/custom",
                    json={"name": "阿尔法资源", "api_url": API_A, "slug": "alpha",
                          "description": "手动添加"})
    check("显式 slug 添加成功", r.status_code == 200 and r.json()["slug"] == "alpha", r.text[:150])

    row = db_row("alpha")
    check("source 行已写入", row is not None and row["name"] == "阿尔法资源")
    check("origin 标记为 custom", row["origin"] == "custom")
    check("enabled/auto_sync 默认开启", row["enabled"] == 1 and row["auto_sync"] == 1)
    check("数据表已创建", database.table_exists("vod_alpha"))

    lst = client.get("/api/v1/sources").json()["list"]
    alpha = next((x for x in lst if x["slug"] == "alpha"), None)
    check("列表接口返回 origin 字段", alpha is not None and alpha["origin"] == "custom",
          str(alpha)[:120] if alpha else "not found")

    r = client.post("/api/v1/sources/custom",
                    json={"name": "重复地址", "api_url": API_A})
    check("相同 api_url 拒绝 409", r.status_code == 409, r.status_code)

    r = client.post("/api/v1/sources/custom",
                    json={"name": "尾斜杠变体", "api_url": API_A.rstrip("/")})
    check("尾斜杠差异也判重 409", r.status_code == 409, r.status_code)

    r = client.post("/api/v1/sources/custom",
                    json={"name": "占位站", "api_url": API_B, "slug": "alpha"})
    check("显式 slug 冲突 409", r.status_code == 409, r.status_code)

    r = client.post("/api/v1/sources/custom",
                    json={"name": "我的影视", "api_url": "https://c.example.com/api/"})
    slug_cn = r.json()["slug"]
    check("纯中文名生成 s+hash 形式 slug", slug_cn.startswith("s") and len(slug_cn) == 11,
          f"got {slug_cn}")

    r = client.post("/api/v1/sources/custom",
                    json={"name": "我的影视", "api_url": "https://d.example.com/api/"})
    check("同名不同地址自动加 _2 后缀", r.json()["slug"].endswith("_2"), r.json()["slug"])

    # ------------------------------------------------------------------
    print("\n[D] 持久化与 refresh 行为")
    # ------------------------------------------------------------------
    # 重新构造一个连接读库，模拟进程重启后仍能读到
    with database.engine.connect() as conn:
        again = conn.execute(
            select(sources.c.name, sources.c.origin).where(sources.c.slug == "alpha")
        ).first()
    check("重新读库记录仍在（持久化）", again is not None and again[0] == "阿尔法资源")

    # latest.json 里出现同一个 API：名字不同、status=ok。
    # 先把库里标成 down，才能看出 refresh 确实把 status 更新回来了
    # （ONLY_OK_SOURCES 默认 true，传 down 会在入口就被跳过，验证不到这条分支）。
    with database.write_txn() as conn:
        conn.execute(sources.update().where(sources.c.slug == "alpha").values(status="down"))

    syncer.refresh_sources(FakeLatestClient([{
        "api": API_A, "name": "官方改名了", "status": "ok", "description": "官方描述",
    }]))
    row = db_row("alpha")
    check("refresh 不覆盖自定义源的名称", row["name"] == "阿尔法资源", row["name"])
    check("refresh 不覆盖自定义源的描述", row["description"] == "手动添加", row["description"])
    check("refresh 仍更新在线状态", row["status"] == "ok", row["status"])
    check("refresh 不会删除自定义源", row is not None)

    # latest.json 里没有的源也不能被删（refresh 只增不减）
    syncer.refresh_sources(FakeLatestClient([]))
    check("refresh 空列表也不删任何源", db_row("alpha") is not None)

    # ------------------------------------------------------------------
    print("\n[E] 修改")
    # ------------------------------------------------------------------
    r = client.patch("/api/v1/sources/alpha/custom", json={"name": "阿尔法资源2"})
    check("PATCH 改名成功", r.status_code == 200 and "name" in r.json()["updated"], r.text[:120])
    check("改名已生效", db_row("alpha")["name"] == "阿尔法资源2")

    r = client.patch("/api/v1/sources/alpha/custom", json={})
    check("PATCH 空 body 拒绝 400", r.status_code == 400, r.status_code)

    r = client.patch("/api/v1/sources/alpha/custom", json={"auto_sync": False})
    check("PATCH 只改 auto_sync", r.status_code == 200 and r.json()["updated"] == ["auto_sync"],
          str(r.json())[:120])
    check("auto_sync 已关闭", db_row("alpha")["auto_sync"] == 0)
    client.patch("/api/v1/sources/alpha/custom", json={"auto_sync": True})

    # 准备数据后退水：改 api_url 必须清空已有数据与水位线
    table = build_vod_table("alpha")
    with database.write_txn() as conn:
        conn.execute(table.insert(), [
            {"vod_id": i, "name": f"旧片{i}", "pic": "", "remarks": "", "content": "",
             "play_url": "", "vod_time": "2026-01-01 00:00:00"}
            for i in range(1, 4)
        ])
        conn.execute(sources.update().where(sources.c.slug == "alpha").values(
            watermark="2026-01-05 12:00:00", last_page=7, total_pages=20, total_videos=3))
    check("改地址前已入库 3 条", count_rows("alpha") == 3, count_rows("alpha"))

    r = client.patch("/api/v1/sources/alpha/custom", json={"api_url": API_B})
    check("PATCH 改 api_url 成功", r.status_code == 200, r.text[:120])
    check("响应标记 data_reset=true", r.json()["data_reset"] is True)
    check("改地址后数据被清空", count_rows("alpha") == 0, count_rows("alpha"))
    row = db_row("alpha")
    check("水位线已重置", row["watermark"] == "", row["watermark"])
    check("断点已重置", row["last_page"] == 0, row["last_page"])
    check("api_url 已更新", row["api_url"] == API_B, row["api_url"])

    r = client.patch("/api/v1/sources/alpha/custom", json={"api_url": "ftp://x/y"})
    check("PATCH 也校验 scheme 400", r.status_code == 400, r.status_code)

    # 官方源不可改
    with database.write_txn() as conn:
        conn.execute(sources.insert().values(
            name="官方源", slug="official", api_url="https://official.example.com/api/",
            status="ok", enabled=1, auto_sync=1, origin="latest"))
    syncer.ensure_table("official")

    r = client.patch("/api/v1/sources/official/custom", json={"name": "x"})
    check("PATCH 官方源 403", r.status_code == 403, r.status_code)

    r = client.patch("/api/v1/sources/nosuch/custom", json={"name": "x"})
    check("PATCH 不存在的源 404", r.status_code == 404, r.status_code)

    # ------------------------------------------------------------------
    print("\n[F] 删除")
    # ------------------------------------------------------------------
    r = client.delete("/api/v1/sources/alpha/custom")
    check("缺少 confirm 拒绝 400", r.status_code == 400, r.status_code)

    r = client.delete("/api/v1/sources/alpha/custom?confirm=wrong")
    check("confirm 不匹配拒绝 400", r.status_code == 400, r.status_code)

    r = client.delete("/api/v1/sources/official/custom?confirm=official")
    check("删除官方源 403", r.status_code == 403, r.status_code)
    check("官方源仍存在", db_row("official") is not None)

    r = client.delete("/api/v1/sources/alpha/custom?confirm=alpha")
    check("删除自定义源成功", r.status_code == 200, r.text[:120])
    check("返回 dropped=true", r.json()["dropped"] is True)
    check("source 记录已删除", db_row("alpha") is None)
    check("数据表已 DROP", not database.table_exists("vod_alpha"))

    # 同一 slug 重建：验证 forget_vod_table 把 META 里的旧 Table 也移除了
    r = client.post("/api/v1/sources/custom",
                    json={"name": "重建源", "api_url": "https://reborn.example.com/api/",
                          "slug": "alpha"})
    check("删除后可用同 slug 重新添加", r.status_code == 200, r.text[:200])
    check("重建后表再次存在", database.table_exists("vod_alpha"))

    # drop_table=false 只删记录、保留数据表
    r = client.delete("/api/v1/sources/alpha/custom?confirm=alpha&drop_table=false")
    check("drop_table=false 删除成功", r.status_code == 200, r.text[:120])
    check("返回 dropped=false", r.json()["dropped"] is False)
    check("记录已删但表保留",
          db_row("alpha") is None and database.table_exists("vod_alpha"))

    # ------------------------------------------------------------------
    print("\n[G] 端到端：真实 HTTP 源站（本地 mock）")
    # ------------------------------------------------------------------
    import json as _json
    import threading as _threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    items = [{
        "vod_id": i, "type_id": 1, "type_name": "电影",
        "vod_name": f"端到端影片{i}", "vod_pic": "https://img.example.com/x.jpg",
        "vod_remarks": "HD", "vod_content": "这是一段简介。" * 5,
        "vod_play_url": "第1集$https://cdn.example.com/x.m3u8",
        "vod_time": "2026-11-01 10:00:00",
    } for i in range(1, 6)]

    class _MockHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = _json.dumps(
                {"code": 1, "page": 1, "pagecount": 1, "total": len(items), "list": items},
                ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _MockHandler)
    _threading.Thread(target=srv.serve_forever, daemon=True).start()
    mock_api = f"http://127.0.0.1:{srv.server_address[1]}/api.php/provide/vod/"

    # 这一段换成真实 probe_source（前面所有用例都把探测替换成了假函数）
    api_mod.probe_source = real_probe
    r = client.post("/api/v1/sources/probe", json={"api_url": mock_api})
    check("真实 HTTP 探测成功", r.status_code == 200 and r.json().get("ok") is True, r.text[:200])
    check("探测解析出 total / pagecount",
          r.json().get("total") == 5 and r.json().get("pagecount") == 1, str(r.json())[:160])
    check("探测返回片名样本", (r.json().get("samples") or [""])[0] == "端到端影片1",
          str(r.json().get("samples")))

    r = client.post("/api/v1/sources/custom", json={
        "name": "端到端源", "api_url": mock_api, "slug": "e2e"})
    check("真实源站添加成功", r.status_code == 200, r.text[:200])

    # 同步直接调用（阻塞），避免后台线程让断言时序不稳
    result = syncer.sync_source("e2e", "full")
    check("全量同步成功", result.get("status") == "success", str(result)[:200])
    check("数据已入库 5 条", count_rows("e2e") == 5, count_rows("e2e"))

    v = client.get("/api/v1/videos?source=e2e&page_size=10").json()
    check("列表接口端到端可读",
          v.get("total") == 5 and len(v.get("list") or []) == 5, str(v)[:160])

    lst = client.get("/api/v1/sources").json()["list"]
    e2e = next((x for x in lst if x["slug"] == "e2e"), None)
    check("面板列表显示入库条数", e2e is not None and e2e["total_videos"] == 5,
          str(e2e)[:160])

    r = client.delete("/api/v1/sources/e2e/custom?confirm=e2e")
    check("端到端删除成功", r.status_code == 200 and r.json()["dropped"] is True, r.text[:160])
    srv.shutdown()

    print(f"\n结果: 通过 {passed} / {passed + failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
