"""冒烟测试：真实拉取源站列表 + 合成数据入库 + 全接口实测（临时脚本，测完删除）。"""
import os

# 必须在导入 app 之前设置：避免测试期间触发全量同步
os.environ["FULL_SYNC_ON_DISCOVER"] = "false"
os.environ["DATA_DIR"] = os.path.join(os.path.dirname(__file__), "..", "data-test")
os.environ["AUTH_ENABLED"] = "false"   # 测试只关心业务逻辑，关掉鉴权

import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient

def main():
    from app import database, syncer
    from app.main import app

    # ---- 0. 初始化元数据表（应用中由 startup 事件执行） ----
    database.init_db()

    # ---- 1. 真实拉取 latest.json 源站列表 ----
    new_slugs = syncer.refresh_sources()
    print(f"[1] refresh_sources: 登记 {len(new_slugs)} 个新源站 -> {new_slugs[:5]}")
    assert len(new_slugs) > 10, "源站数量异常"

    # ---- 2. 合成数据入库（验证 UPSERT 与统计） ----
    slug = "smoketest"
    syncer.ensure_table(slug)
    rows = [
        {"vod_id": 1, "type_id": 6, "type_name": "国产剧", "name": "测试剧A",
         "pic": "http://p/1.jpg", "remarks": "更新至10集", "content": "简介A",
         "play_url": "第01集$http://s/1.m3u8#第02集$http://s/2.m3u8",
         "vod_time": "2026-10-29 20:00:00"},
        {"vod_id": 2, "type_id": 1, "type_name": "电影", "name": "测试片B",
         "pic": "http://p/2.jpg", "remarks": "HD", "content": "简介B",
         "play_url": "线路1$HD$http://s/b.m3u8", "vod_time": "2026-10-30 09:00:00"},
        {"vod_id": 3, "type_id": 1, "type_name": "电影", "name": "测试片C",
         "pic": "", "remarks": "正片", "content": "", "play_url": "", "vod_time": "2026-10-28 08:00:00"},
    ]
    ins, upd = syncer.upsert_batch(slug, rows)
    print(f"[2] 首次 upsert: inserted={ins} updated={upd}")
    assert (ins, upd) == (3, 0), "首次写入应为 3 insert / 0 update"
    # 重复写入验证去重更新
    rows[0]["remarks"] = "更新至11集"
    ins2, upd2 = syncer.upsert_batch(slug, rows)
    print(f"[2] 重复 upsert: inserted={ins2} updated={upd2}")
    assert (ins2, upd2) == (0, 3), "重复写入应为 0 insert / 3 update"

    # 把 smoketest 注册成源站以便 API 路由可用（total_videos 由同步完成后刷新，此处模拟）
    from app.models import sources
    with database.engine.begin() as conn:
        conn.execute(sources.insert().values(
            name="冒烟测试站", slug=slug, api_url="http://example.com/api.php/provide/vod/"))
    syncer._mark_synced(slug, "", 3)

    # ---- 3. 全接口实测 ----
    with TestClient(app) as client:
        r = client.get("/healthz"); assert r.status_code == 200 and r.json()["status"] == "ok"; print("[3] /healthz OK")

        r = client.get("/api/v1/sources")
        assert r.status_code == 200
        sl = [s for s in r.json()["list"] if s["slug"] == slug]
        assert sl and sl[0]["total_videos"] == 3, f"sources 统计异常: {sl}"
        print(f"[3] /api/v1/sources OK ({len(r.json()['list'])} 源站, smoketest=3条)")

        r = client.get("/api/v1/videos", params={"source": slug, "page": 1, "page_size": 2})
        j = r.json(); assert r.status_code == 200 and j["total"] == 3 and len(j["list"]) == 2
        assert j["list"][0]["source"] == slug
        print(f"[3] /api/v1/videos?source=... OK total={j['total']} 首条={j['list'][0]['vod_name']}")

        r = client.get("/api/v1/videos", params={"wd": "测试片"})
        j = r.json(); assert j["total"] == 2, f"按名称搜索异常: {j['total']}"
        print(f"[3] /api/v1/videos?wd=... OK 搜索到 {j['total']} 条")

        r = client.get(f"/api/v1/videos/{slug}/1")
        j = r.json()["detail"]
        assert j["vod_name"] == "测试剧A" and j["vod_remarks"] == "更新至11集"
        assert j["vod_play_list"][0]["episodes"][0]["url"] == "http://s/1.m3u8"
        print("[3] /api/v1/videos/{slug}/{id} OK 播放地址解析正常")

        r = client.get(f"/api/v1/{slug}", params={"ac": "list", "pg": 1, "page_size": 2})
        j = r.json()
        assert j["code"] == 1 and j["total"] == 3 and len(j["list"]) == 2
        assert "vod_play_url" not in j["list"][0] and j["class"], "ac=list 不应含播放地址"
        print(f"[3] /api/v1/{slug}?ac=list OK (MacCMS 兼容, class={j['class']})")

        r = client.get(f"/api/v1/{slug}", params={"ac": "detail", "ids": "1,2"})
        j = r.json()
        assert j["total"] == 2 and j["list"][0]["vod_play_url"]
        print(f"[3] /api/v1/{slug}?ac=detail&ids=1,2 OK")

    # ---- 4. maccms 客户端真实请求一次（验证解析） ----
    from app.maccms import MacCmsClient
    from app.models import slugify
    with database.engine.connect() as conn:
        from sqlalchemy import select
        api_url = conn.execute(
            select(sources.c.api_url).where(sources.c.slug == new_slugs[0])
        ).scalar()
    c = MacCmsClient(timeout=15)
    try:
        payload = c.fetch_page(api_url, "list", 1)
        sample = (payload.get("list") or [{}])[0]
        row = MacCmsClient.extract_row(sample, {})
        print(f"[4] 真实源站请求 OK: {api_url} total={payload.get('total')} 提取样例: "
              f"id={row and row['vod_id']} name={row and row['name']!r} time={row and row['vod_time']!r}")
        assert row and row["name"], "字段提取异常"
    finally:
        c.close()

    # ---- 5. 清理测试数据 ----
    with database.engine.begin() as conn:
        conn.execute(sources.delete().where(sources.c.slug == slug))
        from sqlalchemy import text as T
        conn.execute(T(f"DROP TABLE vod_{slug}"))
    from app.models import _vod_table_cache
    _vod_table_cache.pop(slug, None)
    print("[5] 测试数据已清理")
    print("ALL_SMOKE_TESTS_PASSED")


if __name__ == "__main__":
    main()
