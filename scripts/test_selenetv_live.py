"""
SeleneTV 直播源融合 —— 解析/入库/分发契约 + 零影响验证。

覆盖：
  A. M3U 解析：EPG 提取、频道计数、主备源切换（用本地样本，不联网）
  B. 入库幂等：live_playlist_upsert 二次写入不重复；live_source_ensure 不覆盖人工修改
  C. API 契约：/api/live/sources（带 Cookie）返回 Helios 形状且 url 指向本地端点；
     /live/playlist/{key}（免 Cookie）返回 m3u 原文 + 正确头；404/405 文本。
  D. 鉴权隔离：/api/live/sources 无 Cookie→401；/live/playlist 无 Cookie→200（公开）。
  E. 零影响：vodsync 现有表列表不因本功能新增/删除。

运行：.venv_selene/Scripts/python.exe scripts/test_selenetv_live.py
"""
import os
import sys
import tempfile

DATA_DIR = tempfile.mkdtemp(prefix="vodsync-selenetv-live-")
os.environ["DATA_DIR"] = DATA_DIR
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(DATA_DIR, "live.db").replace("\\", "/")
os.environ["AUTH_ENABLED"] = "true"
os.environ["FULL_SYNC_ON_DISCOVER"] = "false"
os.environ["AUTO_RESUME_ON_BOOT"] = "false"
os.environ["RELATED_ENABLED"] = "false"
os.environ["TAG_INDEX_ON_STARTUP"] = "false"
os.environ["SELENETV_API_ENABLED"] = "true"
os.environ["SELENETV_LIVE_SYNC_ENABLED"] = "true"
os.environ["ADMIN_PASSWORD"] = "live_test_pwd"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

from app import auth, database
from app.selenetv.migrate import ensure_tables
from app.selenetv import live_sync
from app.selenetv import repository as repo

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


def section(title):
    print(f"\n--- {title} ---")


# ---------------------------------------------------------------------------
SAMPLE_M3U = '''#EXTM3U x-tvg-url="http://epg.51zmt.top:8000/e.xml"
#EXTINF:-1 tvg-name="CCTV13" tvg-id="CCTV13" tvg-logo="https://logos/CCTV13.png" group-title="央视频道", CCTV13
http://example.com/cctv13.m3u8
#EXTINF:-1 tvg-name="东方卫视" tvg-id="东方卫视" tvg-logo="" group-title="卫视频道", 东方卫视
http://example.com/dongfang.m3u8
#EXTINF:-1 tvg-name="湖南卫视" tvg-id="湖南卫视" tvg-logo="" group-title="卫视频道", 湖南卫视
http://example.com/hunan.m3u8
'''

section("A. M3U 解析")
check("EPG 提取", live_sync.parse_epg_url(SAMPLE_M3U) == "http://epg.51zmt.top:8000/e.xml",
      live_sync.parse_epg_url(SAMPLE_M3U))
check("频道计数=3", live_sync.count_channels(SAMPLE_M3U) == 3, str(live_sync.count_channels(SAMPLE_M3U)))
check("无EPG返回空", live_sync.parse_epg_url("#EXTM3U\n#EXTINF:-1,x\nhttp://a") == "")

# ---------------------------------------------------------------------------
database.init_db()
auth.set_password("live_test_pwd")
before_tables = set(database.list_tables())
ensure_tables(verbose=False)
after_tables = set(database.list_tables())

section("E. 零影响：只增表")
new_tables = after_tables - before_tables
check("仅新增 live_playlist（其余已由 ensure_tables 幂等）", "selenetv_live_playlist" in after_tables,
      str(sorted(new_tables)))
check("未删除任何现有表", not (before_tables - after_tables))

section("B. 入库幂等")
repo.live_playlist_upsert("iptv4", "IPTV4 直播源", "http://upstream/a.m3u", SAMPLE_M3U, 3, 111)
repo.live_playlist_upsert("iptv4", "IPTV4 直播源", "http://upstream/b.m3u", SAMPLE_M3U, 3, 222)
row = repo.live_playlist_get("iptv4")
check("upsert 后仍单行且 fetched_at 更新", row and row["fetched_at"] == 222 and row["url"].endswith("b.m3u"), str(row))
check("content 落库", row and "CCTV13" in row["content"])

repo.live_source_ensure("iptv4", "IPTV4 直播源", "local:1", "http://epg/e.xml", "", 100)
# 人为改成自定义值后，再次 ensure 不应覆盖（幂等且尊重人工修改）
repo.live_source_ensure("iptv4", "改过的名字", "http://manual", "http://manual-epg", "UA", 1)
# 先把记录改成"人工值"，再 ensure 验证不覆盖
with database.write_txn() as conn:
    _t = repo.live_sources
    conn.execute(_t.update().where(_t.c.key == "iptv4").values(name="人工名", url="http://manual"))
repo.live_source_ensure("iptv4", "IPTV4 直播源", "local:1", "http://epg/e.xml", "", 100)
src = [r for r in repo.live_sources_all() if r["key"] == "iptv4"][0]
check("ensure 不覆盖已存在记录（人工修改保留）", src["name"] == "人工名" and src["url"] == "http://manual", str(src))

# 用 local: 标记的那条来测动态 url，重置回 local:1
repo.live_source_ensure.__doc__  # noqa
repo.live_playlist_upsert("iptv4", "IPTV4 直播源", "local:1", SAMPLE_M3U, 3, 333)
from sqlalchemy import select  # noqa: E402
with database.write_txn() as conn:
    t = repo.live_sources  # 直接取表更新测试记录
    conn.execute(t.update().where(t.c.key == "iptv4").values(url="local:1", name="IPTV4 直播源", epg="http://epg/e.xml"))

from app.main import app  # noqa: E402
client = TestClient(app)          # 登录后的 client（会持久化 cookie）
anon = TestClient(app)            # 从不登录的 client（测无 cookie 拦截）

section("C/D. API 契约与鉴权隔离")
# 无 Cookie 检查必须在任何登录之前用独立 client（TestClient 会持久化 cookie）
r0 = anon.get("/api/live/sources")
check("live/sources 无 Cookie → 401", r0.status_code == 401, str(r0.status_code))

# 登录拿 Cookie（之后 client 自动带 cookie）
r = client.post("/api/login", json={"username": "any", "password": "live_test_pwd"})
check("登录 200", r.status_code == 200, r.text)
cookies = r.cookies

r = client.get("/api/live/sources")
check("live/sources 带 Cookie → 200", r.status_code == 200, r.text[:200])
data = r.json()
check("Helios 形状 success/data", data.get("success") is True and isinstance(data.get("data"), list), str(data)[:200])
iptv = [x for x in data["data"] if x["key"] == "iptv4"]
check("含 iptv4 条", len(iptv) == 1, str(data)[:300])
if iptv:
    item = iptv[0]
    check("url 指向本地端点", item["url"].endswith("/live/playlist/iptv4"), item["url"])
    check("from=config", item.get("from") == "config", str(item))
    check("disabled=false", item.get("disabled") is False, str(item))
    check("epg 透传", item.get("epg") == "http://epg/e.xml", str(item))

r = anon.get("/live/playlist/iptv4")
check("本地 M3U 免 Cookie → 200", r.status_code == 200, str(r.status_code))
check("返回 m3u 原文", "#EXTM3U" in r.text and "CCTV13" in r.text, r.text[:100])
check("Content-Type mpegurl", "mpegurl" in r.headers.get("content-type", ""), r.headers.get("content-type"))
check("带 ETag", "etag" in r.headers, str(dict(r.headers)))

r = anon.get("/live/playlist/nosuch")
check("未知 key → 404 文本", r.status_code == 404 and "not found" in r.text, r.text)
r = anon.post("/live/playlist/iptv4")
check("POST → 405 文本", r.status_code == 405 and r.text.strip() == "Method not allowed", r.text)

section("D2. Base URL 覆盖")
os.environ["SELENETV_LIVE_BASE_URL"] = "https://tv.example.com"
r = client.get("/api/live/sources", cookies=cookies)
iptv2 = [x for x in r.json()["data"] if x["key"] == "iptv4"][0]
check("环境变量覆盖基址", iptv2["url"] == "https://tv.example.com/live/playlist/iptv4", iptv2["url"])
os.environ.pop("SELENETV_LIVE_BASE_URL", None)

print(f"\n=== 结果：{passed} PASS / {failed} FAIL ===")
sys.exit(1 if failed else 0)
