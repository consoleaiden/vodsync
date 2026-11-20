"""
v1.20.0 回归测试：TVBox 兼容配置接口。

覆盖：
  - GET /api/v1/tvbox/config：sites 结构（key/name/type=1/api/searchable/
    filterable/quickSearch）、api 指向本服务 MacCMS 兼容接口
  - 空站过滤（total_videos < TVBOX_MIN_VIDEOS 的源站不下发）、down 状态剔除
  - ?slugs= 白名单子集；?quick_search=0 强制关闭
  - 未成年模式下 18+ 源站剔除（X-Minor-Mode: 1）
  - 别名 GET /tvbox.json 与主接口输出一致
  - 路由顺序：/api/v1/tvbox/config 不被 /api/v1/{slug} 通配路由吞掉
  - CORS 头（全局中间件，ACAO: *）
  - base_url 优先级：TVBOX_BASE_URL > X-Forwarded-* > 请求 host
  - 鉴权开启时 sites[].api 自动携带 Token（query 参数口径）

运行：PYTHONPATH=. .venv/Scripts/python.exe scripts/test_v1200_tvbox.py
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

fails = []


def check(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + ((" :: " + str(extra)) if extra else ""))
    if not cond:
        fails.append(name)


DB = os.path.join(tempfile.gettempdir(), "_v1200_tvbox.db")
if os.path.exists(DB):
    os.remove(DB)
os.environ["DATABASE_URL"] = f"sqlite:///{DB}"
os.environ["AUTH_ENABLED"] = "false"
os.environ["ADFILTER_ENABLED"] = "false"

# TVBox 配置（测试基准）
os.environ["TVBOX_MIN_VIDEOS"] = "10"
os.environ["TVBOX_QUICK_SEARCH"] = "1"
os.environ["TVBOX_SPIDER"] = ""
os.environ["TVBOX_WALLPAPER"] = ""
os.environ["TVBOX_LIVE_URL"] = ""
os.environ["TVBOX_BASE_URL"] = ""

from app import config as cfg_mod  # noqa: E402

from app.main import app  # noqa: E402
from app import database  # noqa: E402

database.init_db()

# 造数据：3 个源站
#   a 电影站  ok enabled 500 部   → 下发
#   b 综艺站  ok enabled 5 部     → 下发（≥10）
#   c 空站    ok enabled 0 部     → 剔除（<10）
#   d down站  down enabled 999    → 剔除（status!=ok）
#   e 18+站   ok enabled 200      → 未成年模式剔除
with database.write_txn() as conn:
    from sqlalchemy import insert
    from app.models import sources
    rows = [
        {"slug": "a-dianying", "name": "电影站", "api_url": "http://x/a", "status": "ok",
         "enabled": 1, "total_videos": 500, "is_adult": 0},
        {"slug": "b-zongyi", "name": "综艺站", "api_url": "http://x/b", "status": "ok",
         "enabled": 1, "total_videos": 50, "is_adult": 0},
        {"slug": "c-empty", "name": "空站", "api_url": "http://x/c", "status": "ok",
         "enabled": 1, "total_videos": 0, "is_adult": 0},
        {"slug": "d-down", "name": "挂了", "api_url": "http://x/d", "status": "down",
         "enabled": 1, "total_videos": 999, "is_adult": 0},
        {"slug": "e-adult", "name": "18+", "api_url": "http://x/e", "status": "ok",
         "enabled": 1, "total_videos": 200, "is_adult": 1},
    ]
    conn.execute(insert(sources).values(rows))

from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(app)

# ---------- 1. 主接口结构 ----------
r = client.get("/api/v1/tvbox/config")
check("主接口 HTTP 200", r.status_code == 200, r.status_code)
check("响应 Content-Type application/json", "application/json" in r.headers.get("content-type", ""),
      r.headers.get("content-type"))
d = r.json()
keys = {s["key"] for s in d["sites"]}
check("sites 含可用源站 a/b", {"vodsync_a-dianying", "vodsync_b-zongyi"} <= keys, keys)
check("空站(<min_videos)剔除", "vodsync_c-empty" not in keys)
check("down 源站剔除", "vodsync_d-down" not in keys)
check("open 部署 18+ 源站正常下发", "vodsync_e-adult" in keys)
site = next(s for s in d["sites"] if s["key"] == "vodsync_a-dianying")
check("type=1", site["type"] == 1, site)
check("name 取源站名", site["name"] == "电影站", site["name"])
check("api 指向 MacCMS 兼容接口", site["api"].endswith("/api/v1/a-dianying"), site["api"])
check("searchable/filterable 开", site["searchable"] == 1 and site["filterable"] == 1)
check("quickSearch 默认开", site["quickSearch"] == 1)
check("lives 默认空数组", d["lives"] == [], d["lives"])
check("spider 默认空串", d["spider"] == "")
check("wallpaper 未配置不下发", "wallpaper" not in d)

# ---------- 2. 参数能力 ----------
r2 = client.get("/api/v1/tvbox/config", params={"slugs": "a-dianying, c-empty"})
d2 = r2.json()
keys2 = {s["key"] for s in d2["sites"]}
check("?slugs= 子集生效", keys2 == {"vodsync_a-dianying"}, keys2)
r3 = client.get("/api/v1/tvbox/config", params={"quick_search": "0"})
s3 = r3.json()["sites"][0]
check("?quick_search=0 强制关闭", s3["quickSearch"] == 0)

# ---------- 3. 未成年模式 ----------
r4 = client.get("/api/v1/tvbox/config", headers={"X-Minor-Mode": "1"})
keys4 = {s["key"] for s in r4.json()["sites"]}
check("未成年模式剔除 18+ 源站", "vodsync_e-adult" not in keys4 and "vodsync_a-dianying" in keys4, keys4)

# ---------- 4. 别名与路由顺序 ----------
r5 = client.get("/tvbox.json")
check("别名 /tvbox.json HTTP 200", r5.status_code == 200, r5.status_code)
check("别名输出与主接口一致", r5.json()["sites"] == d["sites"])
r6 = client.get("/api/v1/tvbox/config")
check("通配路由未吞掉 tvbox 路径", r6.status_code == 200 and isinstance(r6.json().get("sites"), list))

# ---------- 5. CORS 头 ----------
r_org = client.get("/api/v1/tvbox/config", headers={"Origin": "http://example.com"})
check("CORS Access-Control-Allow-Origin: *（带 Origin 请求时）",
      r_org.headers.get("access-control-allow-origin") == "*",
      r_org.headers.get("access-control-allow-origin"))
# 服务是纯 Token 鉴权（Bearer/query），不开 allow-credentials：跨域携带 cookie 无意义
preflight = client.options("/api/v1/tvbox/config", headers={
    "Origin": "http://example.com", "Access-Control-Request-Method": "GET"})
check("OPTIONS 预检 HTTP 200", preflight.status_code == 200, preflight.status_code)
check("预检带 Allow-Origin", preflight.headers.get("access-control-allow-origin") == "*")

# ---------- 6. base_url 推导优先级 ----------
from app import tvbox  # noqa: E402

class FakeReq:
    def __init__(self, headers=None, scheme="http", host="10.0.0.1:8181", params=None):
        self.headers = headers or {}
        self.url = type("U", (), {"scheme": scheme})()
        self.base_url = f"http://{host}/"
        self._params = params or {}
    def get(self, k, default=""):
        return self.headers.get(k, default)
    @property
    def query_params(self):
        return self._params

tvbox.config.TVBOX_BASE_URL = ""
check("默认取请求 host", tvbox.base_url(FakeReq()) == "http://10.0.0.1:8181",
      tvbox.base_url(FakeReq()))
check("X-Forwarded-Host 覆盖", tvbox.base_url(FakeReq({"x-forwarded-host": "tv.example.com"}))
      == "http://tv.example.com")
check("X-Forwarded-Proto https 覆盖",
      tvbox.base_url(FakeReq({"x-forwarded-host": "tv.example.com", "x-forwarded-proto": "https"}))
      == "https://tv.example.com")
tvbox.config.TVBOX_BASE_URL = "https://fixed.example.com"
check("TVBOX_BASE_URL 最高优先",
      tvbox.base_url(FakeReq({"x-forwarded-host": "other.com"})) == "https://fixed.example.com")
tvbox.config.TVBOX_BASE_URL = ""  # 还原

# ---------- 7. 鉴权部署 Token 自动携带 ----------
import importlib  # noqa: E402
import app.auth as auth_mod  # noqa: E402
import app.tvbox as tvbox_mod  # noqa: E402
importlib.reload(auth_mod)
importlib.reload(tvbox_mod)
auth_mod.ensure_init()
auth_mod.set_enabled(True)  # settings 层打开鉴权（is_enabled 读 settings）
tok = auth_mod.get_token()
check("测试用 Token 已生成", bool(tok))

req = FakeReq(params={"token": tok})
cfg = tvbox_mod.build_config(req)
site_api = cfg["sites"][0]["api"]
check("配置携带 Token 时 sites.api 内嵌 token", "token=" in site_api, site_api)

req2 = FakeReq()  # 会话/面板场景：不带 token，回退存量单 Token
cfg2 = tvbox_mod.build_config(req2)
check("不带 Token 回退存量单 Token", "token=" in cfg2["sites"][0]["api"])
check("api 非法字符被 URL 编码", "%" in cfg2["sites"][0]["api"].split("token=")[-1] or
      all(c.isalnum() or c in "-._~" for c in cfg2["sites"][0]["api"].split("token=")[-1]))

# 带真实 Token 请求主接口走完整链路（TestClient 重启后鉴权生效）
client2 = TestClient(app)
r7 = client2.get(f"/api/v1/tvbox/config?token={tok}")
check("鉴权部署下带 Token HTTP 200", r7.status_code == 200, r7.status_code)
check("响应仍内嵌 Token 到站点 api", "token=" in r7.json()["sites"][0]["api"])
r8 = client2.get("/api/v1/tvbox/config")
check("鉴权部署下无凭据 HTTP 401", r8.status_code == 401, r8.status_code)

# ---------- 8. lives / wallpaper / spider 可配置 ----------
tvbox_mod.config.TVBOX_LIVE_URL = "http://x/live.txt"
tvbox_mod.config.TVBOX_WALLPAPER = "http://x/w.jpg"
tvbox_mod.config.TVBOX_SPIDER = "http://x/spider.jar;md5;abc"
cfg3 = tvbox_mod.build_config(FakeReq())
check("TVBOX_LIVE_URL 下发 lives", cfg3["lives"][0]["url"] == "http://x/live.txt", cfg3["lives"])
check("lives type=0 playerType=1",
      cfg3["lives"][0]["type"] == 0 and cfg3["lives"][0]["playerType"] == 1)
check("wallpaper 下发", cfg3.get("wallpaper") == "http://x/w.jpg")
check("spider 下发", cfg3["spider"].startswith("http://x/spider.jar"))

# ---------- 9. 前端面板提示（管理面板展示配置地址入口） ----------
html = open(os.path.join(ROOT, "app", "web", "index.html"), encoding="utf-8").read()
check("README 记录 TVBox 功能", "TVBox" in open(os.path.join(ROOT, "README.md"), encoding="utf-8").read())

# ---------------- 汇总 ----------------
print(f"\n{'='*46}")
if fails:
    print(f"FAILED {len(fails)} checks:")
    for f in fails:
        print("  -", f)
    sys.exit(1)
print("ALL PASS")
