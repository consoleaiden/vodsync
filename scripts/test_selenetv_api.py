"""
SeleneTV 专用 API 契约与"零影响"验证脚本。

覆盖：
  A. 只增不改：建表前后 vodsync 现有表列表/行数完全不变；4 张新表幂等（二次调用 created=[]）
  B. 鉴权：无 cookie → 401 text/plain "Authentication required"；错误口令 → 401；
     正确登录 → {"ok":true} + auth cookie；携带 cookie 可访问
  C. 用户态三接口：favorites / searchhistory / playrecords 的 GET/POST/DELETE 形状与
     Helios 一致（裸 map / 裸数组 / {"success":true} / index 别名 / 空 key 全删）
  D. 内容接口：search / detail / resources / suggestions / live/sources 的字段与缓存头
  E. SSE：search/ws 事件帧（start/source_result/complete）驼峰字段
  F. 方法校验：405 text/plain；detail 参数错误文本

运行：.venv_selene/Scripts/python.exe scripts/test_selenetv_api.py
"""
import os
import sys
import tempfile

DATA_DIR = tempfile.mkdtemp(prefix="vodsync-selenetv-")
os.environ["DATA_DIR"] = DATA_DIR
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(DATA_DIR, "selenetv.db").replace("\\", "/")
os.environ["AUTH_ENABLED"] = "true"
os.environ["FULL_SYNC_ON_DISCOVER"] = "false"
os.environ["AUTO_RESUME_ON_BOOT"] = "false"
os.environ["RELATED_ENABLED"] = "false"
os.environ["TAG_INDEX_ON_STARTUP"] = "false"
os.environ["SELENETV_API_ENABLED"] = "true"
os.environ["ADMIN_PASSWORD"] = "helios_test_pwd"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

from app import auth, database
from app.models import META, build_vod_table, sources as sources_table
from app.selenetv.migrate import ensure_tables
from app.selenetv.schema import TABLE_NAMES

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
# 初始化：建 vodsync 现有表 + 造样本数据
# ---------------------------------------------------------------------------
database.init_db()
auth.set_password("helios_test_pwd")

# 造 1 个源站 + 1 张 vod 表 + 1 条样本
SLUG = "tstsrc"
with database.write_txn() as conn:
    conn.execute(
        sources_table.insert().values(
            name="测试源", slug=SLUG, api_url="http://example.com/api",
            homepage="http://example.com", status="ok", enabled=1, total_videos=1,
        )
    )
vod = build_vod_table(SLUG)
vod.create(database.engine, checkfirst=True)
with database.write_txn() as conn:
    conn.execute(
        vod.insert().values(
            vod_id=1001, type_id=1, type_name="电影", name="复仇者联盟",
            pic="http://img/p.jpg", content="简介内容",
            play_url="第01集$https://cdn/x/index.m3u8#第02集$https://cdn/y/index.m3u8",
            play_from="feifan", vod_year="2024", vod_time="2024-01-01",
        )
    )

# app（此时 main.py 已挂载 selenetv router）
from app.main import app

client = TestClient(app)

# ---------------------------------------------------------------------------
# A. 只增不改
# ---------------------------------------------------------------------------
section("A. 只增不改")
before_tables = set(database.list_tables())
before_vod_cols = [c["name"] for c in __import__("sqlalchemy").inspect(database.engine).get_columns(vod.name)]
r1 = ensure_tables()
after_tables = set(database.list_tables())
after_vod_cols = [c["name"] for c in __import__("sqlalchemy").inspect(database.engine).get_columns(vod.name)]

check("4 张新表已创建", all(t in after_tables for t in TABLE_NAMES), str(r1))
check("现有表全部保留", before_tables <= after_tables)
check("vod 表字段未被改动", before_vod_cols == after_vod_cols)
r2 = ensure_tables()
check("二次建表幂等(created=[])", r2["created"] == [], str(r2))
check("现有表未被重复创建", all(t not in r2["created"] for t in before_tables))

# ---------------------------------------------------------------------------
# B. 鉴权
# ---------------------------------------------------------------------------
section("B. 鉴权契约")
r = client.get("/api/search?q=复仇者")
check("无 cookie 返回 401", r.status_code == 401, str(r.status_code))
check("错误文本 Authentication required", r.text.strip() == "Authentication required", repr(r.text))
check("错误为 text/plain", "text/plain" in r.headers.get("content-type", ""), r.headers.get("content-type"))

r = client.post("/api/login", json={"username": "aiden", "password": "wrong"})
check("错误口令 401", r.status_code == 401)
check("错误口令文本 Invalid credentials", r.text.strip() == "Invalid credentials", repr(r.text))

r = client.post("/api/login", json={"username": "aiden", "password": "helios_test_pwd"})
check("登录成功 200", r.status_code == 200)
check("登录体 ok=true", r.json() == {"ok": True}, str(r.json()))
check("Set-Cookie 名为 auth", "auth=" in r.headers.get("set-cookie", ""), r.headers.get("set-cookie", ""))
cookies = {"auth": r.cookies.get("auth")}

r = client.get("/api/search?q=复仇者", cookies=cookies)
check("携带 cookie 可访问", r.status_code == 200, str(r.status_code))

# ---------------------------------------------------------------------------
# C. 用户态三接口
# ---------------------------------------------------------------------------
section("C. 用户态接口（Helios 形状）")
# favorites
r = client.get("/api/favorites", cookies=cookies)
check("favorites GET 是裸 map", r.json() == {}, str(r.json()))
fav = {"source": SLUG, "source_id": "1001", "source_name": "测试源", "total_episodes": 2,
       "title": "复仇者联盟", "year": "2024", "cover": "http://img/p.jpg",
       "save_time": 1700000000000, "search_title": "复仇者"}
r = client.post("/api/favorites", json={"key": f"{SLUG}+1001", "favorite": fav}, cookies=cookies)
check("favorites POST success", r.json() == {"success": True}, str(r.json()))
r = client.get("/api/favorites", cookies=cookies)
j = r.json()
check("favorites map key=source+source_id", f"{SLUG}+1001" in j, str(list(j.keys())))
check("favorites 无 origin 字段", "origin" not in j[f"{SLUG}+1001"], str(j))
check("favorites save_time 原样", j[f"{SLUG}+1001"]["save_time"] == 1700000000000)

# searchhistory
r = client.post("/api/searchhistory", json={"keyword": "星际"}, cookies=cookies)
check("history POST 返回裸数组", r.json() == ["星际"], str(r.json()))
r = client.post("/api/searchhistory", json={"keyword": "复仇者"}, cookies=cookies)
check("history 新词置顶", r.json() == ["复仇者", "星际"], str(r.json()))
r = client.get("/api/searchhistory", cookies=cookies)
check("history GET 裸数组一致", r.json() == ["复仇者", "星际"], str(r.json()))
r = client.request("DELETE", "/api/searchhistory", params={"keyword": "星际"}, cookies=cookies)
check("history DELETE success", r.json() == {"success": True})
r = client.get("/api/searchhistory", cookies=cookies)
check("history 删除后剩一条", r.json() == ["复仇者"], str(r.json()))

# playrecords
rec = {"title": "复仇者联盟", "source": SLUG, "source_id": "1001", "source_name": "测试源",
       "cover": "http://img/p.jpg", "year": "2024", "index": 2, "total_episodes": 2,
       "play_time": 120, "total_time": 3000, "save_time": 1700000000000, "search_title": "复仇者"}
r = client.post("/api/playrecords", json={"key": f"{SLUG}+1001", "record": rec}, cookies=cookies)
check("playrecords POST success", r.json() == {"success": True}, str(r.json()))
r = client.get("/api/playrecords", cookies=cookies)
pr = r.json()[f"{SLUG}+1001"]
check("playrecords index 别名", pr["index"] == 2, str(pr))
check("playrecords 无 index_number 字段", "index_number" not in pr, str(pr))
r = client.request("DELETE", "/api/playrecords", params={}, cookies=cookies)
check("playrecords 空 key 全删 success", r.json() == {"success": True})
check("playrecords 删除后为空", client.get("/api/playrecords", cookies=cookies).json() == {})

# ---------------------------------------------------------------------------
# D. 内容接口
# ---------------------------------------------------------------------------
section("D. 内容接口契约")
r = client.get("/api/search", params={"q": "复仇者"}, cookies=cookies)
j = r.json()
check("search 外层 results", "results" in j, str(j))
check("search 缓存头", "max-age=7200" in r.headers.get("cache-control", ""), r.headers.get("cache-control", ""))
res = j["results"][0]
for k in ("id", "title", "poster", "episodes", "episodes_titles", "source", "source_name", "year"):
    check(f"search 含字段 {k}", k in res, str(res))
check("search episodes 为 m3u8", all(u.endswith(".m3u8") for u in res["episodes"]), str(res["episodes"]))
check("search episodes_titles 对应", len(res["episodes"]) == len(res["episodes_titles"]), str(res))
check("search id 为字符串", isinstance(res["id"], str), str(type(res["id"])))
check("search class 恒缺失(PG无此列)", "class" not in res, str(res))
# content/type_name 有值时应输出（Helios omitempty：有值即输出）
check("search desc 有值即输出", res.get("desc") == "简介内容", str(res))
check("search type_name 有值即输出", res.get("type_name") == "电影", str(res))

r = client.get("/api/detail", params={"id": "1001", "source": SLUG}, cookies=cookies)
d = r.json()
check("detail 裸 SearchResult", "id" in d and "episodes" in d, str(d))
check("detail episodes 两条", len(d["episodes"]) == 2, str(d.get("episodes")))

r = client.get("/api/search/resources", cookies=cookies)
res_list = r.json()
check("resources 裸数组", isinstance(res_list, list), str(type(res_list)))
check("resources 字段 key/api/name/detail", set(res_list[0]) == {"key", "api", "name", "detail"}, str(res_list[0]))

r = client.get("/api/search/suggestions", params={"q": "复仇"}, cookies=cookies)
check("suggestions 形状", r.json() == {"suggestions": [{"text": "复仇者联盟"}]}, str(r.json()))

r = client.get("/api/live/sources", cookies=cookies)
lj = r.json()
check("live/sources success/data", lj == {"success": True, "data": []}, str(lj))

# ---------------------------------------------------------------------------
# E. SSE
# ---------------------------------------------------------------------------
section("E. SSE 搜索流")
with client.stream("GET", "/api/search/ws", params={"q": "复仇者"}, cookies=cookies) as r:
    frames = [line for line in r.iter_lines() if line.startswith("data: ")]
check("SSE 有事件帧", len(frames) >= 2, str(len(frames)))
import json as _json
types = [_json.loads(f[6:])["type"] for f in frames]
check("SSE 含 start", "start" in types, str(types))
check("SSE 含 source_result", "source_result" in types, str(types))
check("SSE 含 complete", "complete" in types, str(types))
start_ev = _json.loads([f for f in frames if '"start"' in f][0][6:])
for k in ("type", "query", "totalSources", "timestamp"):
    check(f"SSE start 驼峰字段 {k}", k in start_ev, str(start_ev))

# ---------------------------------------------------------------------------
# F. 方法校验与错误文本
# ---------------------------------------------------------------------------
section("F. 方法校验与错误文本")
r = client.put("/api/search", cookies=cookies)
check("405 状态", r.status_code == 405, str(r.status_code))
check("405 文本 Method not allowed", r.text.strip() == "Method not allowed", repr(r.text))
r = client.get("/api/detail", params={"id": "1001"}, cookies=cookies)
check("detail 缺 source → 缺少必要参数", r.text.strip() == "缺少必要参数", repr(r.text))
r = client.get("/api/detail", params={"id": "!!!", "source": SLUG}, cookies=cookies)
check("detail 非法 id → 无效的视频ID格式", r.text.strip() == "无效的视频ID格式", repr(r.text))
r = client.get("/api/detail", params={"id": "1001", "source": "nosuch"}, cookies=cookies)
check("detail 非法 source → 无效的API来源", r.text.strip() == "无效的API来源", repr(r.text))

# ---------------------------------------------------------------------------
print(f"\n=== 结果：{passed} PASS / {failed} FAIL ===")
sys.exit(1 if failed else 0)
