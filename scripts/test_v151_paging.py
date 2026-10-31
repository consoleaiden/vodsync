"""
v1.5.1 分页行为测试（为 App 三列宫格布局校准每页条数）。

覆盖项：
 1. /api/v1/videos 默认 page_size 取 API_PAGE_SIZE_DEFAULT
 2. 显式 page_size=30 生效，list 长度与 pagecount 一致
 3. page_size 超过 API_PAGE_SIZE_MAX 时返回 422（不是静默截断）
 4. /api/v1/search 默认 page_size 同样跟随配置（v1.5.1 由写死的 20 改为读配置）
 5. 最后一页不足一页时按实际条数返回，pagecount 向上取整
 6. /api/v1/{slug}（MacCMS 兼容）的 limit 字段与请求一致

运行：python scripts/test_v151_paging.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="vodsync-page-")
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(os.environ["DATA_DIR"], "test.db")
os.environ["AUTH_ENABLED"] = "false"
os.environ["FULL_SYNC_ON_DISCOVER"] = "false"
os.environ["API_PAGE_SIZE_DEFAULT"] = "30"     # 模拟 App 想要的 3 列 × 10 行
os.environ["API_PAGE_SIZE_MAX"] = "100"

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


def main():
    from fastapi.testclient import TestClient

    from app import config, database, syncer
    from app.main import app
    from app.models import sources

    database.init_db()

    slug = "pg1"
    with database.engine.begin() as conn:
        conn.execute(sources.insert().values(
            name="分页测试站", slug=slug, api_url="http://x/api.php",
            status="ok", enabled=1, auto_sync=1,
        ))
    syncer.ensure_table(slug)

    from app.models import build_vod_table
    table = build_vod_table(slug)
    # 造 65 条：page_size=30 时应为 3 页（30/30/5）
    with database.engine.begin() as conn:
        conn.execute(table.insert(), [
            {"vod_id": 10000 + i, "type_id": 6, "type_name": "电影",
             "name": f"测试影片{i:03d}", "pic": "", "remarks": "",
             "content": "", "play_url": "", "vod_time": f"2026-10-31 10:{i:02d}:00"}
            for i in range(65)
        ])

    client = TestClient(app)

    print("[1] 默认每页条数")
    check("配置读取 API_PAGE_SIZE_DEFAULT", config.API_PAGE_SIZE_DEFAULT == 30, config.API_PAGE_SIZE_DEFAULT)
    r = client.get(f"/api/v1/videos?source={slug}")
    body = r.json()
    check("/videos 默认返回 30 条", r.status_code == 200 and len(body["list"]) == 30, len(body.get("list", [])))
    check("/videos pagecount=3", body.get("pagecount") == 3, body.get("pagecount"))
    check("/videos total=65", body.get("total") == 65, body.get("total"))

    print("[2] 显式 page_size")
    r = client.get(f"/api/v1/videos?source={slug}&page=3&page_size=30")
    body = r.json()
    check("第 3 页返回剩余 5 条", len(body["list"]) == 5, len(body["list"]))
    r = client.get(f"/api/v1/videos?source={slug}&page=1&page_size=10")
    check("page_size=10 生效", len(r.json()["list"]) == 10, len(r.json()["list"]))
    check("page_size=10 时 pagecount=7", r.json()["pagecount"] == 7, r.json()["pagecount"])

    print("[3] 越界与非法值")
    r = client.get(f"/api/v1/videos?source={slug}&page_size=101")
    check("超过 MAX 返回 422", r.status_code == 422, r.status_code)
    r = client.get(f"/api/v1/videos?source={slug}&page_size=0")
    check("page_size=0 返回 422", r.status_code == 422, r.status_code)

    print("[4] 聚合搜索默认分页（v1.5.1 修复点）")
    r = client.get("/api/v1/search?wd=测试")
    body = r.json()
    check("search 默认 page_size 跟随配置", body.get("page_size") == 30, body.get("page_size"))
    check("search 返回 30 条", len(body.get("list", [])) == 30, len(body.get("list", [])))

    print("[5] MacCMS 兼容接口")
    r = client.get(f"/api/v1/{slug}?ac=list&pg=1&page_size=30")
    body = r.json()
    check("limit 字段回显 30", body.get("limit") == 30, body.get("limit"))
    check("list 长度 30", len(body.get("list", [])) == 30, len(body.get("list", [])))
    check("pagecount 一致", body.get("pagecount") == 3, body.get("pagecount"))

    print()
    print(f"结果：{passed} 通过 / {failed} 失败")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
