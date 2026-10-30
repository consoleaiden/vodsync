"""v1.1 新增功能测试：聚合搜索、统计接口、管理面板、旧库自动迁移（临时脚本，测完删除）。"""
import os
import sqlite3
import subprocess
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEST_DIR = os.path.join(BASE, "data-test")
os.environ["FULL_SYNC_ON_DISCOVER"] = "false"
os.environ["DATA_DIR"] = TEST_DIR
sys.path.insert(0, BASE)

WEB_MARK = "VodSync 管理面板"


def main():
    from fastapi.testclient import TestClient
    from app import database, syncer
    from app.main import app
    from app.models import _vod_table_cache, sources

    database.init_db()

    # ---- 准备两个源站各 3 / 2 条数据 ----
    data = {
        "t1": [
            {"vod_id": 1, "type_id": 6, "type_name": "国产剧", "name": "测试剧A", "pic": "http://p/1.jpg",
             "remarks": "更新至10集", "content": "简介A", "play_url": "第01集$http://s/1.m3u8",
             "vod_time": "2026-10-29 20:00:00"},
            {"vod_id": 2, "type_id": 1, "type_name": "电影", "name": "测试片B", "pic": "http://p/2.jpg",
             "remarks": "HD", "content": "简介B", "play_url": "HD$http://s/b.m3u8",
             "vod_time": "2026-10-30 09:00:00"},
            {"vod_id": 3, "type_id": 1, "type_name": "电影", "name": "测试片C", "pic": "",
             "remarks": "正片", "content": "", "play_url": "", "vod_time": "2026-10-28 08:00:00"},
        ],
        "t2": [
            {"vod_id": 101, "type_id": 1, "type_name": "电影", "name": "测试片D", "pic": "",
             "remarks": "HD中字", "content": "D", "play_url": "", "vod_time": "2026-10-30 10:30:00"},
            {"vod_id": 102, "type_id": 2, "type_name": "连续剧", "name": "无关影片E", "pic": "",
             "remarks": "", "content": "", "play_url": "", "vod_time": "2026-10-27 10:00:00"},
        ],
    }
    with database.engine.begin() as conn:
        for slug, rows in data.items():
            conn.execute(sources.insert().values(
                name=f"测试站{slug}", slug=slug, api_url=f"http://{slug}.example.com/api.php/provide/vod/"))
    for slug, rows in data.items():
        syncer.ensure_table(slug)
        syncer.upsert_batch(slug, rows)
        syncer._mark_synced(slug, "2026-10-30 10:30:00", len(rows))

    with TestClient(app) as c:
        # ---- 1. 聚合搜索：跨源站命中 ----
        r = c.get("/api/v1/search", params={"wd": "测试"})
        assert r.status_code == 200, r.text
        j = r.json()
        assert j["total"] == 4, f"跨源命中数应为4, 实际{j['total']}"
        assert j["sources_searched"] == 2, j
        names = [x["vod_name"] for x in j["list"]]
        assert names == ["测试片D", "测试片B", "测试剧A", "测试片C"], f"排序错误: {names}"
        assert j["list"][0]["source"] == "t2" and j["list"][0]["source_name"] == "测试站t2"
        print(f"[1] 聚合搜索 OK: 命中 {j['total']} 条, 2 源站, 耗时 {j['elapsed_ms']}ms, 顺序 {names}")

        # ---- 2. 聚合搜索：限定源站 + 分类过滤 ----
        r = c.get("/api/v1/search", params={"wd": "测试", "source": "t1"})
        assert r.json()["total"] == 3, r.json()["total"]
        r = c.get("/api/v1/search", params={"wd": "测试", "type_name": "电影"})
        assert r.json()["total"] == 3, f"分类过滤失败: {r.json()['total']}"
        print("[2] 限定源站 + 分类过滤 OK")

        # ---- 3. 聚合搜索：通配符转义（不应把 % 当成全匹配） ----
        r = c.get("/api/v1/search", params={"wd": "%"})
        assert r.json()["total"] == 0, f"通配符未转义，误命中 {r.json()['total']} 条"
        r = c.get("/api/v1/search", params={"wd": "_"})
        assert r.json()["total"] == 0, "下划线未转义"
        print("[3] LIKE 通配符转义 OK（% 与 _ 均不误匹配）")

        # ---- 4. 统计接口 ----
        r = c.get("/api/v1/stats")
        j = r.json()
        assert j["sources_total"] == 2 and j["videos_total"] == 5, j
        assert "today_inserted" in j and "server_time" in j
        print(f"[4] /api/v1/stats OK: 源站 {j['sources_total']}, 总条数 {j['videos_total']}")

        # ---- 5. 源站列表含进度字段 ----
        r = c.get("/api/v1/sources")
        s = [x for x in r.json()["list"] if x["slug"] == "t1"][0]
        assert s["total_pages"] == 0 and s["progress"] == 100, s
        assert c.get("/api/v1/sources/t1").status_code == 200
        print("[5] /api/v1/sources 进度字段 OK")

        # ---- 6. 管理面板页面 ----
        r = c.get("/")
        assert r.status_code == 200 and WEB_MARK in r.text
        assert "/api/v1/stats" in r.text and "/api/v1/search" in r.text
        assert 'id="btnSyncAllFull"' in r.text
        print(f"[6] 管理面板 OK: HTTP {r.status_code}, {len(r.text)} 字节, 含统计/搜索/操作区")

        # ---- 7. 资源站 API 清单 /source ----
        r = c.get("/source")
        assert r.status_code == 200, r.text
        j = r.json()
        assert j["total"] == 2 and j["alive"] == 2 and j["ready"] == 2, j
        one = [x for x in j["list"] if x["slug"] == "t1"][0]
        assert one["api"].endswith("/api.php/provide/vod/"), one
        assert one["video_count"] == 3 and one["synced"] is True
        assert one["local_api"] == "/api/v1/t1" and one["local_list_api"].endswith("source=t1")
        assert len(j["list"]) == 2 and j["list"][0]["video_count"] >= j["list"][1]["video_count"]
        assert c.get("/source", params={"only_ready": True}).json()["total"] == 2
        assert c.get("/source", params={"status": "down"}).json()["total"] == 0
        assert c.get("/source", params={"status": "ok"}).json()["total"] == 2
        print(f"[7] /source OK: {j['total']} 个源站, alive={j['alive']}, ready={j['ready']}, "
              f"字段含 api/local_api/video_count")

    # ---- 清理 ----
    with database.engine.begin() as conn:
        for slug in data:
            conn.execute(sources.delete().where(sources.c.slug == slug))
            conn.execute(__import__("sqlalchemy").text(f"DROP TABLE vod_{slug}"))
            _vod_table_cache.pop(slug, None)

    # ---- 7. 旧库自动迁移（无 total_pages 列的旧库启动后应自动补列） ----
    old_db = os.path.join(TEST_DIR, "old.db")
    if os.path.exists(old_db):
        os.remove(old_db)
    con = sqlite3.connect(old_db)
    con.execute("""CREATE TABLE sources (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name VARCHAR(128) NOT NULL, slug VARCHAR(64) NOT NULL UNIQUE,
        api_url VARCHAR(512) NOT NULL, homepage VARCHAR(512), description VARCHAR(512), status VARCHAR(16),
        enabled INTEGER, total_videos INTEGER, watermark VARCHAR(32), last_page INTEGER,
        last_sync_at DATETIME, created_at DATETIME, updated_at DATETIME)""")
    con.execute("INSERT INTO sources (name, slug, api_url, last_page) VALUES ('旧站','old','http://x/api', 12)")
    con.commit(); con.close()

    code = (
        "import os,sys;"
        f"sys.path.insert(0, r'{BASE}');"
        f"os.environ['DATA_DIR']=r'{TEST_DIR}';"
        f"os.environ['DATABASE_URL']='sqlite:///{old_db.replace(os.sep, '/')}';"
        "from app import database;database.init_db();"
        "import sqlite3;"
        f"c=sqlite3.connect(r'{old_db}');"
        "cols=[r[1] for r in c.execute('PRAGMA table_info(sources)')];"
        "print('COLS_HAS_TOTAL_PAGES', 'total_pages' in cols);"
        "print('OLD_ROW_KEPT', c.execute('SELECT slug,total_pages FROM sources').fetchone());"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=BASE)
    print("[7] 旧库迁移输出:", out.stdout.strip().replace("\n", " | "), out.stderr.strip()[-200:] if out.returncode else "")
    assert "COLS_HAS_TOTAL_PAGES True" in out.stdout, "迁移失败：未添加 total_pages 列"
    assert "('old', 0)" in out.stdout or "('old', None)" in out.stdout, "迁移后旧数据丢失或默认值异常"
    print("[7] 旧库自动迁移 OK（保留原数据，新增列默认 0）")

    print("ALL_V11_TESTS_PASSED")


if __name__ == "__main__":
    main()
