"""
SeleneTV 18+ Tab —— /api/adult/* 四端点契约 + 权限守卫验证。

覆盖：
  A. 未登录：permission/sources/categories/videos 全部 401（Helios 纯文本）。
  B. 登录无 18+ 权限用户（allow_adult=0）：permission {"allowed":false}；
     sources/categories/videos 403。
  C. 登录有 18+ 权限用户（allow_adult=1）：
     - permission {"allowed":true}
     - sources 只含 18+ 源（enabled 过滤、禁用源不出现）
     - categories 返回该源分类（type_id 升序、空 type_name 剔除）
     - videos 分页：total/list/page/size 契约 + type_id 过滤 + 翻页
  D. 参数错误：无效源站 400、无效 type_id 400。
  E. 零影响：既有 /api/search/resources 行为不变。

运行：.venv_selene/Scripts/python.exe scripts/test_selenetv_adult.py
"""
import os
import sys
import tempfile

DATA_DIR = tempfile.mkdtemp(prefix="vodsync-selenetv-adult-")
os.environ["DATA_DIR"] = DATA_DIR
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(DATA_DIR, "adult.db").replace("\\", "/")
os.environ["AUTH_ENABLED"] = "true"
os.environ["FULL_SYNC_ON_DISCOVER"] = "false"
os.environ["AUTO_RESUME_ON_BOOT"] = "false"
os.environ["RELATED_ENABLED"] = "false"
os.environ["TAG_INDEX_ON_STARTUP"] = "false"
os.environ["SELENETV_API_ENABLED"] = "true"
os.environ["SELENETV_LIVE_SYNC_ENABLED"] = "false"
os.environ["ADMIN_PASSWORD"] = "panel_pwd_123"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

from app import database
from app.models import build_vod_table, sources as sources_table
from app.selenetv.migrate import ensure_tables
from app.selenetv import users as users_repo

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


ensure_tables(database.engine)
database.init_db()

# 造 2 个源站：一个 18+（启用）、一个 18+（禁用）、一个普通源
SLUG_A = "adlsrc"
SLUG_OFF = "adloff"
SLUG_N = "normsrc"
with database.write_txn() as conn:
    conn.execute(
        sources_table.insert().values(
            name="18+测试源", slug=SLUG_A, api_url="http://a.example/api",
            homepage="http://a.example", status="ok", enabled=1, total_videos=6,
            is_adult=1,
        )
    )
    conn.execute(
        sources_table.insert().values(
            name="18+停用源", slug=SLUG_OFF, api_url="http://off.example/api",
            homepage="http://off.example", status="ok", enabled=0, total_videos=0,
            is_adult=1,
        )
    )
    conn.execute(
        sources_table.insert().values(
            name="普通源", slug=SLUG_N, api_url="http://n.example/api",
            homepage="http://n.example", status="ok", enabled=1, total_videos=1,
        )
    )

vod = build_vod_table(SLUG_A)
vod.create(database.engine, checkfirst=True)
with database.write_txn() as conn:
    # 2 个分类（type_id=1 电影 / type_id=2 剧集），共 6 条：电影 4 条、剧集 2 条
    for i in range(1, 5):
        conn.execute(
            vod.insert().values(
                vod_id=1000 + i, type_id=1, type_name="电影", name=f"成人影片{i:02d}",
                pic=f"http://img/{i}.jpg", content="简介",
                play_url=f"第01集$https://cdn/{i}/index.m3u8",
                play_from="feifan", vod_year="2024", vod_time=f"2024-01-0{i}",
            )
        )
    for i in range(5, 7):
        conn.execute(
            vod.insert().values(
                vod_id=1000 + i, type_id=2, type_name="剧集", name=f"成人剧集{i:02d}",
                pic=f"http://img/{i}.jpg", content="简介",
                play_url=f"第01集$https://cdn/{i}/index.m3u8",
                play_from="feifan", vod_year="2025", vod_time=f"2025-02-0{i-4}",
            )
        )
    # 一条空 type_name 的行：应被分类列表剔除
    conn.execute(
        vod.insert().values(
            vod_id=1099, type_id=0, type_name="", name="无分类行",
            pic="", content="", play_url="", play_from="", vod_year="", vod_time="",
        )
    )

# 建 3 个用户：无权限 / 有权限 / owner 兼容回退（不建表记录走面板密码）
users_repo.create("no_adult", "pwd_aaa", allow_adult=0)
users_repo.create("yes_adult", "pwd_bbb", allow_adult=1)

# app（此时 main.py 已挂载 selenetv router）
from app.main import app

client = TestClient(app)

# ---------------------------------------------------------------------------
section("A. 未登录全部 401")
for path in ("/api/adult/permission", "/api/adult/sources",
             f"/api/adult/categories?source={SLUG_A}", f"/api/adult/videos?source={SLUG_A}"):
    r = client.get(path)
    check(f"未登录 {path} 401", r.status_code == 401, f"got {r.status_code}")
    check(f"未登录 {path} 纯文本", "text/plain" in r.headers.get("content-type", ""), r.headers.get("content-type"))

# ---------------------------------------------------------------------------
section("B. 登录 + 无 18+ 权限")
r = client.post("/api/login", json={"username": "no_adult", "password": "pwd_aaa"})
check("无权用户登录 200", r.status_code == 200, f"{r.status_code} {r.text[:120]}")
ck_no = {"auth": r.cookies.get("auth")}

r = client.get("/api/adult/permission", cookies=ck_no)
check("permission 200", r.status_code == 200, f"{r.status_code} {r.text[:120]}")
check("allowed=false", r.json() == {"allowed": False}, str(r.json()))

r = client.get("/api/adult/sources", cookies=ck_no)
check("sources 403", r.status_code == 403, f"{r.status_code}")
check("403 文本 Adult content access denied",
      r.text.strip() == "Adult content access denied", repr(r.text))

r = client.get(f"/api/adult/categories?source={SLUG_A}", cookies=ck_no)
check("categories 403", r.status_code == 403, f"{r.status_code}")

r = client.get(f"/api/adult/videos?source={SLUG_A}", cookies=ck_no)
check("videos 403", r.status_code == 403, f"{r.status_code}")

# ---------------------------------------------------------------------------
section("C. 登录 + 有 18+ 权限")
r = client.post("/api/login", json={"username": "yes_adult", "password": "pwd_bbb"})
check("有权用户登录 200", r.status_code == 200, f"{r.status_code} {r.text[:120]}")
ck_yes = {"auth": r.cookies.get("auth")}

r = client.get("/api/adult/permission", cookies=ck_yes)
check("permission allowed=true", r.json() == {"allowed": True}, str(r.json()))
check("permission no-store", r.headers.get("cache-control") == "no-store", r.headers.get("cache-control"))

r = client.get("/api/adult/sources", cookies=ck_yes)
check("sources 200", r.status_code == 200, f"{r.status_code} {r.text[:120]}")
sites = r.json()
check("只含启用的 18+ 源", [s["key"] for s in sites] == [SLUG_A], str(sites))
check("源站字段齐全", all(k in sites[0] for k in ("key", "api", "name", "detail")), str(sites[0]))

r = client.get(f"/api/adult/categories?source={SLUG_A}", cookies=ck_yes)
check("categories 200", r.status_code == 200, f"{r.status_code} {r.text[:120]}")
cats = r.json()
check("分类 2 个且剔除空 type_name",
      [(c["type_id"], c["type_name"]) for c in cats] == [(1, "电影"), (2, "剧集")], str(cats))

r = client.get(f"/api/adult/videos?source={SLUG_A}&type_id=1&page=1&size=3", cookies=ck_yes)
check("videos 200", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
d = r.json()
check("total=4（type_id=1）", d["total"] == 4, str(d["total"]))
check("list 3 条", len(d["list"]) == 3, str(len(d["list"])))
check("page/size 回显", d["page"] == 1 and d["size"] == 3, str((d["page"], d["size"])))
check("瘦行字段（无 content/play_url）",
      all(k in d["list"][0] for k in ("vod_id", "name", "pic", "vod_year", "type_name", "type_id"))
      and "content" not in d["list"][0] and "play_url" not in d["list"][0],
      str(d["list"][0].keys()))
check("排序 vod_time 倒序", [v["vod_id"] for v in d["list"]] == [1004, 1003, 1002], str([v["vod_id"] for v in d["list"]]))

r = client.get(f"/api/adult/videos?source={SLUG_A}&type_id=1&page=2&size=3", cookies=ck_yes)
d2 = r.json()
check("第2页 1 条", len(d2["list"]) == 1, str(len(d2["list"])))
check("第2页末条 vod_id=1001", d2["list"][0]["vod_id"] == 1001, str(d2["list"][0]))

r = client.get(f"/api/adult/videos?source={SLUG_A}&page=1&size=100", cookies=ck_yes)
check("不带 type_id = 全部分类 total=7", r.json()["total"] == 7, str(r.json()["total"]))

r = client.get(f"/api/adult/videos?source={SLUG_A}&type_id=99", cookies=ck_yes)
check("未知分类 total=0", r.status_code == 200 and r.json()["total"] == 0, r.text[:120])

# ---------------------------------------------------------------------------
section("D. 参数错误")
r = client.get("/api/adult/categories?source=nope", cookies=ck_yes)
check("无效源站 categories 400", r.status_code == 400, f"{r.status_code} {r.text[:80]}")
r = client.get("/api/adult/videos?source=nope", cookies=ck_yes)
check("无效源站 videos 400", r.status_code == 400, f"{r.status_code}")
r = client.get("/api/adult/categories", cookies=ck_yes)
check("缺 source 参数 400", r.status_code == 400, f"{r.status_code}")
r = client.get(f"/api/adult/videos?source={SLUG_A}&type_id=abc", cookies=ck_yes)
check("无效 type_id 400", r.status_code == 400, f"{r.status_code}")

# ---------------------------------------------------------------------------
section("E. 零影响：既有接口行为不变")
r = client.get("/api/search/resources", cookies=ck_yes)
keys = [s["key"] for s in r.json()]
check("有权限用户 resources 含 18+ 源与普通源", SLUG_A in keys and SLUG_N in keys, str(keys))

r = client.get("/api/search/resources", cookies=ck_no)
keys = [s["key"] for s in r.json()]
check("无权限用户 resources 不含 18+ 源", SLUG_A not in keys and SLUG_N in keys, str(keys))

r = client.get(f"/api/detail?source={SLUG_A}&id=1001", cookies=ck_no)
check("无权限用户 detail 访问 18+ 源 404", r.status_code == 404, f"{r.status_code}")
r = client.get(f"/api/detail?source={SLUG_A}&id=1001", cookies=ck_yes)
check("有权限用户 detail 访问 18+ 源 200", r.status_code == 200, f"{r.status_code}")

# ---------------------------------------------------------------------------
print(f"\n===== 结果：{passed} passed, {failed} failed =====")
sys.exit(1 if failed else 0)
