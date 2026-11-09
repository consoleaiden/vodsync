"""
v1.14.0 后端契约升级测试（B-10 / B-11 / B-04 / B-01 / B-08 / B-03 / B-06）。

覆盖分组
--------
A. /healthz 能力清单：免鉴权 200、version 三段式、capabilities 形态、
   依赖数据的能力（tag_search）在索引就绪前不声明、DB 不可用收缩为 degraded
B. 错误码语义：成功 code==0；业务失败一律非 2xx + {code,error}；
   40001/40101/40301/40401 四类；参数错误带 field；MacCMS 兼容接口仍 code==1
C. 分页契约（B-04）：page_size 缺省 18；pagecount == ceil(total/18)；
   无筛选时 total_approximate；列表字段名不变
D. 标签精确检索（B-01）：分隔符切分、大小写不敏感、不做子串包含、
   多源合并与 source 过滤、total/pagecount 精确、空结果 200、
   tag 优先于 wd、/videos 与 /search 两个入口一致
E. 标签索引的增量维护与回填：upsert 同事务写 video_tags、rebuild 幂等、
   tag_index_state 进度可观测、就绪后 capability 自动出现
F. Token 权限（B-08）：默认宽权限（读 Token 也能写）；打开强制后读 Token
   → 403 40301 required=admin；会话 cookie 不受限；按客户端版本宽限；
   签发/清单/吊销
G. 详情契约（B-03/B-06）：5 个字段恒存在；play_lines[].original_index 在
   过滤策略下不重排
H. 回归：/api/v1/sources 字段不变；既有读接口在 auth 关闭时可用

运行：python scripts/test_v1140_contract.py
"""
import os
import sys
import tempfile

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="vodsync-v1140-")
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(os.environ["DATA_DIR"], "v1140.db")
os.environ["AUTH_ENABLED"] = "false"
os.environ["FULL_SYNC_ON_DISCOVER"] = "false"
os.environ["AUTO_RESUME_ON_BOOT"] = "false"
os.environ["RELATED_ENABLED"] = "false"
os.environ["TAG_INDEX_ON_STARTUP"] = "false"   # 测试里手动控制回填时机
os.environ["TAG_INDEX_SLEEP_MS"] = "0"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

passed = failed = skipped = 0


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


SLUG = "t1140"
SLUG2 = "t1140b"


def seed():
    """两个源站 + 若干带标签的影片（直接走 upsert_batch，顺带验证增量索引）。"""
    from app import syncer

    syncer.ensure_table(SLUG)
    syncer.ensure_table(SLUG2)

    def row(vid, name, tag, t, type_name="动作片"):
        return {
            "vod_id": vid, "type_id": 6, "type_name": type_name, "name": name,
            "pic": f"https://img/{vid}.jpg", "remarks": "HD", "content": f"{name} 简介",
            "play_url": "第01集$https://cdn/a.m3u8",
            "play_from": "ffm3u8", "vod_actor": "", "vod_director": "",
            "vod_writer": "", "vod_area": "大陆", "vod_year": "2024",
            "vod_tag": tag, "vod_time": t,
        }

    a_rows = [
        # 分隔符混用：半角逗号 / 全角逗号 / 斜杠 / 竖线 / 顿号
        row(1001, "悬疑片甲", "悬疑,犯罪", "2024-11-01 10:00:00"),
        row(1002, "悬疑片乙", "悬疑，剧情", "2024-11-02 10:00:00"),
        row(1003, "喜剧片丙", "喜剧/悬疑", "2024-11-03 10:00:00"),
        row(1004, "动作片丁", "动作|冒险", "2024-11-04 10:00:00"),
        row(1005, "纪录戊", "纪录、自然", "2024-11-05 10:00:00"),
        # ASCII 大小写：归一化后应与 "sci-fi" 同一行
        row(1006, "科幻己", "Sci-Fi, 悬疑", "2024-11-06 10:00:00"),
        # 名字里带「悬疑」但标签没有 —— 精确检索不该命中
        row(1007, "悬疑喜剧片", "喜剧", "2024-11-07 10:00:00"),
        # 空标签
        row(1008, "无标签片", "", "2024-11-08 10:00:00"),
    ]
    b_rows = [
        row(2001, "别站悬疑", "悬疑", "2024-11-09 10:00:00"),
        row(2002, "别站其他", "爱情", "2024-11-10 10:00:00"),
    ]
    syncer.upsert_batch(SLUG, a_rows)
    syncer.upsert_batch(SLUG2, b_rows)


def main():
    from fastapi.testclient import TestClient

    from app import auth, caps, config, database, tags
    from app.main import app
    from app.models import sources

    database.init_db()
    # 真实服务由 lifespan 启动时调用 ensure_init（建默认密码/签发存量 Token），
    # TestClient 不跑 lifespan，这里显式补上，否则存量 Token 是空串，
    # F 组会误判成「scope 解析错误」。
    auth.ensure_init()
    client = TestClient(app)

    with database.write_txn() as conn:
        for slug, name in ((SLUG, "测试源A"), (SLUG2, "测试源B")):
            conn.execute(sources.insert().values(
                name=name, slug=slug, api_url="https://example.com/api.php",
                status="ok", enabled=1, total_videos=0,
            ))
    seed()

    # =====================================================================
    section("A. /healthz 能力清单（B-10）")
    # =====================================================================
    r = client.get("/healthz")
    j = r.json()
    check("A1 /healthz 返回 200", r.status_code == 200, r.status_code)
    check("A2 status=ok", j.get("status") == "ok", j.get("status"))
    ver = j.get("version", "")
    check("A3 version 为三段式", len(str(ver).split(".")) == 3 and ver.count(".") == 2, ver)
    check("A4 version >= 1.14.0", tuple(int(x) for x in ver.split(".")) >= (1, 14, 0), ver)
    check("A5 capabilities 是字符串数组",
          isinstance(j.get("capabilities"), list)
          and all(isinstance(x, str) for x in j["capabilities"]), j.get("capabilities"))
    want = {"backfill_play_from", "play_line_filter", "error_codes", "token_scope", "paging_v2"}
    check("A6 静态能力齐全", want <= set(j["capabilities"]),
          want - set(j["capabilities"]))
    check("A7 索引未就绪时不声明 tag_search",
          "tag_search" not in j["capabilities"], j["capabilities"])
    check("A8 build 字段存在", "build" in j, list(j.keys()))

    # 免鉴权：开启授权后不带 Token 仍 200
    auth.set_enabled(True)
    r2 = client.get("/healthz")
    check("A9 开启鉴权后 /healthz 仍免 Token 可访问", r2.status_code == 200, r2.status_code)
    auth.set_enabled(False)

    # DB 不可用 -> degraded + 200 + 能力收缩
    real_engine = database.engine
    try:
        class _Broken:
            def connect(self):
                raise RuntimeError("db down")
        database.engine = _Broken()
        caps.snapshot(db_ok=True)  # 触发一次正常路径，确认缓存无关
        r3 = client.get("/healthz")
        j3 = r3.json()
        check("A10 DB 不可用时仍返回 200", r3.status_code == 200, r3.status_code)
        check("A11 status=degraded", j3.get("status") == "degraded", j3.get("status"))
        check("A12 db=false", j3.get("db") is False, j3.get("db"))
        check("A13 能力收缩（不声明依赖库的能力）",
              "tag_search" not in j3.get("capabilities", []), j3.get("capabilities"))
        check("A14 收缩后仍有静态能力", "error_codes" in j3.get("capabilities", []))
    finally:
        database.engine = real_engine

    r4 = client.get("/healthz?detail=1")
    check("A15 ?detail=1 返回对象形态 capabilities",
          isinstance(r4.json().get("capabilities"), dict), type(r4.json().get("capabilities")))

    # =====================================================================
    section("B. 错误码语义（B-11）")
    # =====================================================================
    r = client.get(f"/api/v1/videos?source={SLUG}&page=1&page_size=5")
    j = r.json()
    check("B1 成功 code==0", r.status_code == 200 and j.get("code") == 0, j.get("code"))
    check("B2 列表字段名仍为 list", isinstance(j.get("list"), list))

    r = client.get("/api/v1/videos?source=BAD!!slug")
    j = r.json()
    check("B3 非法 slug -> 400", r.status_code == 400, r.status_code)
    check("B4 400 业务码 40001", j.get("code") == 40001, j.get("code"))
    check("B5 400 带 error 语义串", j.get("error") == "bad_request", j.get("error"))
    check("B6 400 保留 detail 中文", bool(j.get("detail")), j.get("detail"))

    r = client.get(f"/api/v1/videos/{SLUG}/999999")
    j = r.json()
    check("B7 不存在的视频 -> 404", r.status_code == 404, r.status_code)
    check("B8 404 业务码 40401", j.get("code") == 40401, j.get("code"))
    check("B9 404 error=not_found", j.get("error") == "not_found", j.get("error"))

    r = client.get("/api/v1/search")
    j = r.json()
    check("B10 缺少 wd/tag -> 400", r.status_code == 400, r.status_code)
    check("B11 参数缺失 code=40001", j.get("code") == 40001, j.get("code"))

    r = client.get(f"/api/v1/videos?source={SLUG}&page_size=9999")
    j = r.json()
    check("B12 越界参数 -> 400（不再 422）", r.status_code == 400, r.status_code)
    check("B13 越界参数带 field", j.get("field") == "page_size", j.get("field"))

    # 401 / 40101
    auth.set_enabled(True)
    r = client.get("/api/v1/sources")
    j = r.json()
    check("B14 无 Token -> 401", r.status_code == 401, r.status_code)
    check("B15 401 业务码 40101", j.get("code") == 40101, j.get("code"))
    check("B16 401 error=invalid_token", j.get("error") == "invalid_token", j.get("error"))
    token = auth.get_token()
    r = client.get("/api/v1/sources", headers={"Authorization": "Bearer bogus"})
    check("B17 错误 Token -> 40101", r.json().get("code") == 40101, r.json().get("code"))
    r = client.get("/api/v1/sources", headers={"Authorization": f"Bearer {token}"})
    check("B18 正确 Token -> 200 code=0",
          r.status_code == 200 and r.json().get("code") == 0, r.status_code)
    auth.set_enabled(False)

    # MacCMS 兼容接口的 code 必须仍是 1
    r = client.get(f"/api/v1/{SLUG}?ac=list&pg=1")
    check("B19 MacCMS 兼容接口仍 code=1（第三方采集端约定）",
          r.json().get("code") == 1, r.json().get("code"))

    # =====================================================================
    section("C. 分页契约（B-04）")
    # =====================================================================
    r = client.get(f"/api/v1/videos?source={SLUG}")
    j = r.json()
    check("C1 page_size 缺省 18", len(j["list"]) <= 18, len(j["list"]))
    total = j["total"]
    check("C2 pagecount == ceil(total/18)",
          j["pagecount"] == (total + 17) // 18, (j["pagecount"], total))
    check("C3 total_approximate 标记存在（无筛选走估值）",
          "total_approximate" in j, list(j.keys()))
    r = client.get(f"/api/v1/videos?source={SLUG}&page_size=3&page=2")
    j = r.json()
    check("C4 指定 page_size 时 pagecount 随之变化",
          j["pagecount"] == (j["total"] + 2) // 3, (j["pagecount"], j["total"]))
    check("C5 第 2 页返回 3 条", len(j["list"]) == 3, len(j["list"]))
    check("C6 列表项含 source/vod_pic/type_name/remarks",
          {"source", "vod_pic", "type_name", "remarks"} <= set(j["list"][0].keys()),
          set(j["list"][0].keys()))
    check("C7 list[].source 是真实 slug（非空）",
          j["list"][0]["source"] == SLUG, j["list"][0]["source"])

    # =====================================================================
    section("D. 标签精确检索（B-01）")
    # =====================================================================
    # 先建索引（等价于运维手动回填）
    res = tags.rebuild_all()
    check("D1 回填完成",
          all(x["status"] == "done" for x in res), res)
    st = tags.status()
    check("D2 索引行数 > 0", st["rows"] > 0, st["rows"])
    check("D3 就绪标记为 true", st["ready"] is True, st["ready"])

    r = client.get("/healthz")
    check("D4 索引就绪后 capabilities 出现 tag_search",
          "tag_search" in r.json()["capabilities"], r.json()["capabilities"])

    r = client.get(f"/api/v1/search?tag=悬疑&source={SLUG}&page=1&page_size=18")
    j = r.json()
    ids = [x["vod_id"] for x in j["list"]]
    check("D5 标签检索返回 200", r.status_code == 200, r.status_code)
    check("D6 命中 4 条（1001/1002/1003/1006）",
          sorted(ids) == [1001, 1002, 1003, 1006], sorted(ids))
    check("D7 不做子串包含：名字带悬疑但标签是喜剧的不命中",
          1007 not in ids, ids)
    check("D8 total 精确且与 list 自洽", j["total"] == 4, j["total"])
    check("D9 total_approximate=false", j["total_approximate"] is False, j.get("total_approximate"))
    check("D10 pagecount 自洽", j["pagecount"] == 1, j["pagecount"])
    check("D11 带 query_ms", isinstance(j.get("query_ms"), int), j.get("query_ms"))
    check("D12 列表项字段与 /videos 一致",
          {"source", "vod_id", "vod_name", "vod_pic", "type_name", "vod_year", "vod_area", "remarks"}
          <= set(j["list"][0].keys()), set(j["list"][0].keys()))

    # 大小写不敏感（ASCII）
    r = client.get(f"/api/v1/search?tag=SCI-FI&source={SLUG}")
    check("D13 ASCII 大小写不敏感", r.json()["total"] == 1, r.json()["total"])
    r = client.get(f"/api/v1/search?tag=sci-fi&source={SLUG}")
    check("D14 同一标签两种写法命中同一行", r.json()["total"] == 1, r.json()["total"])

    # 全站（不传 source）应合并两个源站
    r = client.get("/api/v1/search?tag=悬疑")
    j = r.json()
    check("D15 不传 source 时跨源站合并", j["total"] == 5, j["total"])
    check("D16 结果含两个源站",
          {x["source"] for x in j["list"]} == {SLUG, SLUG2},
          {x["source"] for x in j["list"]})

    # 空结果：200 而不是 404
    r = client.get("/api/v1/search?tag=根本不存在的标签")
    j = r.json()
    check("D17 空结果返回 200", r.status_code == 200, r.status_code)
    check("D18 空结果 list=[] total=0 pagecount=0",
          j["list"] == [] and j["total"] == 0 and j["pagecount"] == 0,
          (j["list"], j["total"], j["pagecount"]))

    # tag 优先于 wd
    r = client.get("/api/v1/search?tag=喜剧&wd=悬疑")
    check("D19 tag 与 wd 同时传时以 tag 为准",
          all(x["vod_name"] in ("喜剧片丙", "悬疑喜剧片") for x in r.json()["list"]),
          [x["vod_name"] for x in r.json()["list"]])

    # /videos?tag= 与 /search?tag= 同结果
    a = client.get(f"/api/v1/search?tag=悬疑&source={SLUG}").json()
    b = client.get(f"/api/v1/videos?tag=悬疑&source={SLUG}").json()
    check("D20 /videos?tag= 与 /search?tag= 结果一致",
          [x["vod_id"] for x in a["list"]] == [x["vod_id"] for x in b["list"]],
          ([x["vod_id"] for x in a["list"]], [x["vod_id"] for x in b["list"]]))
    check("D21 /videos?tag= 也带 pagecount/total", b.get("total") == 4, b.get("total"))

    # 分页正确性：把每页拼起来应覆盖全部 4 条且不重复
    seen = []
    for p in (1, 2, 3):
        rr = client.get(f"/api/v1/search?tag=悬疑&source={SLUG}&page={p}&page_size=2").json()
        seen.extend(x["vod_id"] for x in rr["list"])
    check("D22 逐页遍历不重不漏", sorted(seen) == [1001, 1002, 1003, 1006], sorted(seen))

    # 归一化函数单测（分隔符 / 去空白 / 去重）
    check("D23 分隔符切分（含全角逗号/顿号/竖线）",
          tags.split_tags("甲,乙，丙/丁|戊、己") == ["甲", "乙", "丙", "丁", "戊", "己"],
          tags.split_tags("甲,乙，丙/丁|戊、己"))
    check("D24 去重保序", tags.split_tags("悬疑,悬疑, 悬疑 ") == ["悬疑"],
          tags.split_tags("悬疑,悬疑, 悬疑 "))
    check("D25 空串归一化为空", tags.normalize("   ") == "")

    # =====================================================================
    section("E. 索引维护与可观测（B-01）")
    # =====================================================================
    before = tags.status()["rows"]
    # 增量：新增一条带新标签的视频
    from app import syncer
    syncer.upsert_batch(SLUG, [{
        "vod_id": 1009, "type_id": 6, "type_name": "动作片", "name": "新增片",
        "pic": "", "remarks": "", "content": "", "play_url": "", "play_from": "",
        "vod_actor": "", "vod_director": "", "vod_writer": "", "vod_area": "",
        "vod_year": "", "vod_tag": "新增标签,悬疑", "vod_time": "2024-11-11 10:00:00",
    }])
    after = tags.status()["rows"]
    check("E1 同步入库时增量写入标签行", after == before + 2, (before, after))

    # 覆盖：同一视频改标签后旧标签应消失
    syncer.upsert_batch(SLUG, [{
        "vod_id": 1009, "type_id": 6, "type_name": "动作片", "name": "新增片",
        "pic": "", "remarks": "", "content": "", "play_url": "", "play_from": "",
        "vod_actor": "", "vod_director": "", "vod_writer": "", "vod_area": "",
        "vod_year": "", "vod_tag": "只留一个", "vod_time": "2024-11-11 10:00:00",
    }])
    r = client.get(f"/api/v1/search?tag=新增标签&source={SLUG}")
    check("E2 改标签后旧标签不再命中", r.json()["total"] == 0, r.json()["total"])
    r = client.get(f"/api/v1/search?tag=只留一个&source={SLUG}")
    check("E3 新标签命中", r.json()["total"] == 1, r.json()["total"])

    # 幂等：重复回填不产生重复行
    rows_a = tags.status()["rows"]
    tags.rebuild_source(SLUG)
    rows_b = tags.status()["rows"]
    check("E4 重复回填幂等（行数不变）", rows_a == rows_b, (rows_a, rows_b))

    # 进度可观测
    with database.engine.connect() as conn:
        from app.models import tag_index_state
        from sqlalchemy import select as _sel
        stt = conn.execute(
            _sel(tag_index_state).where(tag_index_state.c.slug == SLUG)
        ).mappings().first()
    check("E5 tag_index_state 记录 cursor/total/status",
          bool(stt) and stt["status"] == "done" and stt["total"] >= 8,
          dict(stt) if stt else None)
    r = client.get("/api/v1/tag/status")
    check("E6 /api/v1/tag/status 可用",
          r.status_code == 200 and "sources" in r.json(), r.status_code)
    check("E7 status 暴露 ready 与 rows",
          r.json().get("ready") is True and r.json().get("rows", 0) > 0, r.json().get("ready"))

    # 空标签不写行
    with database.engine.connect() as conn:
        from app.models import video_tags as vt
        n = conn.execute(_sel_where(vt, SLUG, 1008)).scalar() or 0
    check("E8 无标签的视频不产生标签行", n == 0, n)

    # =====================================================================
    section("F. Token 权限（B-08，宽权限并行 → 可收紧）")
    # =====================================================================
    auth.set_enabled(True)
    legacy = auth.get_token()
    read_info = auth.create_scoped_token("read", "测试只读")
    read_tok = read_info["token"]
    check("F1 签发的只读 Token 带 scope=read", read_info["scope"] == "read", read_info)
    check("F2 resolve_scope 解析正确", auth.resolve_scope(read_tok) == "read",
          auth.resolve_scope(read_tok))
    check("F3 存量 Token 默认 admin", auth.resolve_scope(legacy) == "admin",
          auth.resolve_scope(legacy))

    # 默认宽权限：读 Token 也能写
    r = client.post("/api/v1/maintenance/recount",
                    headers={"Authorization": f"Bearer {read_tok}"})
    check("F4 默认（未强制）读 Token 也能调写接口", r.status_code == 200, r.status_code)

    # 打开强制
    old_enforce = config.TOKEN_SCOPE_ENFORCE
    config.TOKEN_SCOPE_ENFORCE = True
    try:
        r = client.post("/api/v1/maintenance/recount",
                        headers={"Authorization": f"Bearer {read_tok}"})
        j = r.json()
        check("F5 强制后读 Token 调写接口 -> 403", r.status_code == 403, r.status_code)
        check("F6 403 业务码 40301", j.get("code") == 40301, j.get("code"))
        check("F7 403 error=insufficient_scope",
              j.get("error") == "insufficient_scope", j.get("error"))
        check("F8 403 required=admin", j.get("required") == "admin", j.get("required"))

        r = client.post("/api/v1/maintenance/recount",
                        headers={"Authorization": f"Bearer {legacy}"})
        check("F9 强制后 admin Token 写接口正常", r.status_code == 200, r.status_code)

        r = client.get(f"/api/v1/videos?source={SLUG}",
                       headers={"Authorization": f"Bearer {read_tok}"})
        check("F10 强制后读 Token 读接口正常", r.status_code == 200, r.status_code)

        r = client.get("/api/v1/admin/tables",
                       headers={"Authorization": f"Bearer {read_tok}"})
        check("F11 强制后读 Token 不能浏览全表（/admin/*）", r.status_code == 403, r.status_code)

        r = client.get(f"/api/v1/related/status",
                       headers={"Authorization": f"Bearer {read_tok}"})
        check("F12 只读统计接口对 read Token 开放", r.status_code == 200, r.status_code)

        # 会话 cookie 恒 admin：登录后写接口可用
        r = client.post("/api/v1/auth/login", json={"password": config.ADMIN_PASSWORD})
        check("F13 面板登录成功", r.status_code == 200, r.status_code)
        r = client.post("/api/v1/maintenance/recount")
        check("F14 会话 cookie 不受 scope 限制", r.status_code == 200, r.status_code)

        # 按客户端版本宽限
        # 注意：F13 登录后面板会话 cookie 一直挂在 client 上，而「会话恒 admin」
        # 会掩盖按版本宽限的判断 —— 必须先清掉 cookie，才是纯 Token 的场景
        client.cookies.clear()
        config.TOKEN_SCOPE_LEGACY_BELOW = "1.6.0"
        r = client.post("/api/v1/maintenance/recount",
                        headers={"Authorization": f"Bearer {read_tok}",
                                 "X-Client-Version": "1.5.3"})
        check("F15 低于阈值版本按 admin 放行（老客户端不会整站失效）",
              r.status_code == 200, r.status_code)
        r = client.post("/api/v1/maintenance/recount",
                        headers={"Authorization": f"Bearer {read_tok}",
                                 "X-Client-Version": "2.0.0"})
        check("F16 达到阈值版本仍按 read 校验", r.status_code == 403, r.status_code)
        config.TOKEN_SCOPE_LEGACY_BELOW = ""
    finally:
        config.TOKEN_SCOPE_ENFORCE = old_enforce

    # 后续 F17+ 走面板会话（admin），重新登录把刚才清掉的 cookie 补回来
    client.post("/api/v1/auth/login", json={"password": config.ADMIN_PASSWORD})

    r = client.get("/api/v1/auth/tokens")
    j = r.json()
    check("F17 Token 清单可读", r.status_code == 200 and len(j["list"]) >= 2, r.status_code)
    check("F18 清单不含明文 Token",
          all("token" not in it for it in j["list"]), list(j["list"][0].keys()))
    check("F19 存量 Token 显示为 legacy",
          j["list"][0]["kind"] == "legacy", j["list"][0])

    r = client.delete(f"/api/v1/auth/tokens/{read_info['id']}")
    check("F20 吊销具名 Token", r.status_code == 200, r.status_code)
    check("F21 吊销后该 Token 失效", auth.resolve_scope(read_tok) == "",
          auth.resolve_scope(read_tok))
    r = client.delete("/api/v1/auth/tokens/0")
    check("F22 存量 Token 不可单独吊销", r.status_code == 404, r.status_code)

    r = client.post("/api/v1/auth/token?scope=read")
    check("F23 重置 Token 可指定 scope",
          r.status_code == 200 and r.json().get("scope") == "read", r.json())
    r = client.post("/api/v1/auth/token?scope=bogus")
    check("F24 非法 scope -> 400", r.status_code == 400, r.status_code)
    # 复位成 admin，避免影响后续用例
    auth.rotate_token("admin")
    auth.set_enabled(False)

    # =====================================================================
    section("G. 详情契约（B-03 / B-06）")
    # =====================================================================
    r = client.get(f"/api/v1/videos/{SLUG}/1001")
    d = r.json()["detail"]
    for f in ("vod_id", "vod_name", "vod_pic", "vod_content", "vod_actor",
              "vod_director", "vod_area", "vod_year", "vod_tag", "source", "play_lines"):
        check(f"G 详情字段存在: {f}", f in d, list(d.keys()))
    check("G 五字段恒为字符串（缺失为 \"\"）",
          all(isinstance(d[k], str) for k in
              ("vod_actor", "vod_director", "vod_area", "vod_year", "vod_tag")),
          {k: type(d[k]).__name__ for k in ("vod_actor", "vod_tag")})
    lines = d["play_lines"]
    check("G 播放线路字段齐全",
          bool(lines) and {"name", "original_index", "from", "type", "episodes"} <= set(lines[0].keys()),
          lines[0] if lines else None)
    check("G 剧集字段齐全",
          {"name", "url", "playable"} <= set(lines[0]["episodes"][0].keys()),
          lines[0]["episodes"][0])

    # original_index 不随策略重排（B-03）
    import json as _json
    from app import playline
    multi = [{
        "vod_id": 3001, "type_id": 1, "type_name": "剧", "name": "多线路剧",
        "pic": "", "remarks": "", "content": "", "vod_actor": "", "vod_director": "",
        "vod_writer": "", "vod_area": "", "vod_year": "", "vod_tag": "",
        # 线路 0 是分享页（会被过滤），线路 1 是直链
        "play_url": "第01集$https://x/share/abc.html#第02集$https://x/share/def.html"
                    "$$$第01集$https://cdn/x/index.m3u8",
        "play_from": "share$$$ffm3u8", "vod_time": "2024-11-11 12:00:00",
    }]
    syncer.upsert_batch(SLUG, multi)
    old_policy = config.PLAY_LINE_POLICY
    try:
        config.PLAY_LINE_POLICY = "filter"
        playline.reset_policy_cache()
        filtered = client.get(f"/api/v1/videos/{SLUG}/3001?play_raw=0").json()["detail"]["play_lines"]
        raw = client.get(f"/api/v1/videos/{SLUG}/3001?play_raw=1").json()["detail"]["play_lines"]
        check("G 过滤后只剩直链线路", len(filtered) == 1, len(filtered))
        check("G 过滤后 original_index 保留上游下标（=1，不重排）",
              filtered[0]["original_index"] == 1, filtered[0]["original_index"])
        check("G play_raw=1 时原始两条都在", len(raw) == 2, len(raw))
        check("G 原始列表 original_index 为 0/1",
              [x["original_index"] for x in raw] == [0, 1],
              [x["original_index"] for x in raw])
    finally:
        config.PLAY_LINE_POLICY = old_policy
        playline.reset_policy_cache()

    # =====================================================================
    section("H. 回归（既有接口不受影响）")
    # =====================================================================
    r = client.get("/api/v1/sources")
    j = r.json()
    keys = set(j["list"][0].keys())
    need = {"slug", "name", "status", "enabled", "api_url", "total_videos", "running",
            "running_mode", "progress", "last_page", "total_pages", "paused", "auto_sync",
            "queued", "synced", "origin", "fail_streak", "skip_until",
            "play_line", "play_line_effective", "play_line_policy"}
    check("H1 源站列表字段齐全", need <= keys, need - keys)
    check("H2 不返回 pick 字段（客户端本地维护）", "pick" not in keys)

    r = client.get("/api/v1/categories")
    check("H3 分类接口可用", r.status_code == 200, r.status_code)

    r = client.get(f"/api/v1/{SLUG}?ac=list&pg=1")
    check("H4 MacCMS 兼容接口 pagecount/total 正常",
          r.status_code == 200 and r.json()["total"] > 0, r.status_code)

    r = client.post(f"/api/v1/sync-all?mode=backfill_play_from")
    check("H5 sync-all 接受 backfill_play_from", r.status_code == 200, r.status_code)
    check("H6 sync-all 回填回执语义正确",
          r.json().get("mode") == "backfill_play_from", r.json())

    r = client.post(f"/api/v1/sync-all?mode=bogus")
    check("H7 非法 mode -> 400", r.status_code == 400, r.status_code)

    print(f"\n{'=' * 60}\n通过 {passed} / 失败 {failed} / 跳过 {skipped}\n{'=' * 60}")
    return 1 if failed else 0


def _sel_where(table, slug, vod_id):
    from sqlalchemy import func as _f
    from sqlalchemy import select as _sel

    return _sel(_f.count()).select_from(table).where(
        table.c.source == slug, table.c.vod_id == vod_id
    )


if __name__ == "__main__":
    raise SystemExit(main())
