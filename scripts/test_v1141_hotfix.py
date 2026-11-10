"""
v1.14.1 线上三问题热修回归测试。

线上报告（用户 2026-11-10 实测 mosang.asia:8181）：
 1. 公网入口全线 502（os error 11004）：反代 upstream 用了只有 AAAA 记录的
    公网域名 —— 属部署问题，仓库内补文档排障条目与样例警示；
 2. /healthz.capabilities 拿着合法 API Token 也 401/40101 —— 该路径从未是
    真实路由（文档写的是「字段路径」），且不在鉴权白名单。修复：把端点做出来
    （免鉴权，与 /healthz 同级）；
 3. tag_search 已声明但 ?tag=夜店 0 条（wd=夜店 2571 条）—— 两个成因：
    a) 时序漏洞：v1.14.0 把「表还不存在」的源站直接标 done（no_table），之后
       建表+数据进来、增量维护若失败，ready() 恒 true。修复：记 no_table，
       存量错误行（done+processed=0+有数据）续跑时自愈重扫；
    b) 语义差异：tag 是 vod_tag 精确段匹配，wd 是标题全文匹配，命中集天然
       不同 —— 响应新增 tag_found，false 时客户端应回退 wd 检索。

覆盖：
 A. /healthz.capabilities 免鉴权 200，与 /healthz.capabilities 字段同源
 B. no_table 时序：先回填后建表，ready() 不声明 tag_search
 C. 存量自愈：done+processed=0+cursor=0+表有数据 → 续跑从头重扫
 D. tag_found：存在标签 true / 不存在 false
 E. 文档与样例：DEPLOY_HTTPS 第 7 节、nginx 样例警示注释、API_SPEC tag_found

运行：python scripts/test_v1141_hotfix.py
"""
import os
import re
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="vodsync-v1141h-"))
os.environ.setdefault("DATABASE_URL",
                      "sqlite:///" + os.path.join(os.environ["DATA_DIR"], "v1141h.db"))
os.environ.setdefault("AUTH_ENABLED", "false")
os.environ.setdefault("FULL_SYNC_ON_DISCOVER", "false")
os.environ.setdefault("AUTO_RESUME_ON_BOOT", "false")
os.environ.setdefault("RELATED_ENABLED", "false")
os.environ.setdefault("TAG_INDEX_ON_STARTUP", "false")
os.environ.setdefault("TAG_INDEX_SLEEP_MS", "0")

passed = failed = skipped = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


def main():
    from app import auth, caps, config, database, models, tags

    database.init_db()
    auth.ensure_init()
    config.TAG_INDEX_BATCH = 10

    # ---------------- A. /healthz.capabilities 端点 ----------------
    print("\nA. /healthz.capabilities 独立端点（免鉴权）")
    from fastapi.testclient import TestClient
    from app.main import app as _app
    client = TestClient(_app)
    paths = client.get("/openapi.json").json()["paths"]
    check("A0 路由存在（openapi 注册）", "/healthz.capabilities" in paths,
          sorted(list(paths))[:8])

    from fastapi.testclient import TestClient
    client = TestClient(_app)

    r = client.get("/healthz.capabilities")
    j = r.json()
    check("A1 无凭据 200", r.status_code == 200, r.status_code)
    check("A2 返回 capabilities 数组", isinstance(j.get("capabilities"), list)
          and len(j["capabilities"]) >= 7, j.get("capabilities"))
    check("A3 带 API Token 也 200（不再 40101）",
          client.get("/healthz.capabilities",
                     headers={"Authorization": "Bearer vs_whatever"}).status_code == 200, "")
    r2 = client.get("/healthz").json()
    check("A4 与 /healthz 的 capabilities 字段同源",
          j["capabilities"] == r2["capabilities"], (j.get("capabilities"), r2.get("capabilities")))
    check("A5 /healthz 原路径不受影响", client.get("/healthz").status_code == 200, "")

    # ---------------- B/C. 标签回填时序与自愈 ----------------
    print("\nB. no_table 时序（先回填后建表）")
    from datetime import datetime
    from sqlalchemy import insert, select, func

    # 造一个「登记了但表还没建」的启用源站
    with database.write_txn() as conn:
        conn.execute(insert(models.sources).values(
            slug="hotfix_nt", name="热修站", api_url="http://x/api.php",
            enabled=1, created_at=datetime.now(), updated_at=datetime.now(),
        ).prefix_with("OR IGNORE"))
    res = tags.rebuild_source("hotfix_nt")
    check("B1 表不存在记 no_table（不再是 done）",
          res["status"] == "no_table", res)
    with database.engine.connect() as conn:
        st = conn.execute(select(models.tag_index_state)
                          .where(models.tag_index_state.c.slug == "hotfix_nt")
                          ).mappings().first()
    check("B2 state.status=no_table", st is not None and st["status"] == "no_table",
          dict(st) if st else None)
    tags.invalidate_ready_cache()
    check("B3 ready() 不声明（no_table 不算 done）", tags.ready() is False, tags.ready())

    # 建表 + 灌数据（name 含夜店、vod_tag 不含 → 复现语义差异），再看自愈
    table = models.build_vod_table("hotfix_nt")
    table.create(database.engine, checkfirst=True)   # 与 syncer.ensure_table 同法
    now = datetime.now()
    rows = [{"vod_id": i, "type_id": 1, "type_name": "剧情", "name": f"夜店风云{i}",
             "pic": "", "remarks": "HD", "vod_time": now, "vod_area": "大陆",
             "vod_year": "2026", "vod_tag": "悬疑,犯罪" if i % 2 == 0 else "喜剧"}
            for i in range(1, 26)]
    with database.write_txn() as conn:
        conn.execute(insert(table), rows)

    print("\nC. 存量自愈（done+processed=0+有数据 → 重扫）")
    res = tags.rebuild_source("hotfix_nt")   # 续跑：撞上自愈分支
    check("C1 自愈后回填完成", res["status"] == "done" and res["indexed"] > 0, res)
    with database.engine.connect() as conn:
        st = conn.execute(select(models.tag_index_state)
                          .where(models.tag_index_state.c.slug == "hotfix_nt")
                          ).mappings().first()
        n_tags = conn.execute(
            select(func.count()).select_from(models.video_tags)
            .where(models.video_tags.c.source == "hotfix_nt")).scalar()
    check("C2 processed 补齐到 total", int(st["processed"]) == 25, st["processed"])
    check("C3 标签行已写入（12 悬疑/犯罪片 24 行 + 13 喜剧片 13 行）",
          n_tags == 37, n_tags)
    tags.invalidate_ready_cache()
    check("C4 ready() 现在可以声明（单站全 done）", tags.ready() is True, tags.ready())
    # 已完成的站再续跑一次不得重复触发自愈
    res2 = tags.rebuild_source("hotfix_nt")
    check("C5 已完成的站续跑不重扫（indexed 不变）",
          res2["indexed"] == res["indexed"], (res, res2))

    # 线上实况复现：done + processed=0 + cursor>0（v1.14.0 真跑过一部分、
    # processed 列是后补的恒 0）→ 也要自愈（v1.14.1 初版条件含 cursor==0，
    # 这类行永远修不回来）
    with database.write_txn() as conn:
        from app.models import tag_index_state as _tis
        conn.execute(_tis.update().where(_tis.c.slug == "hotfix_nt")
                     .values(status="done", processed=0, cursor=99999, total=25))
    tags.invalidate_ready_cache()
    check("C6 needs_repair 发现假 done 行", tags.needs_repair() is True, "")
    res3 = tags.rebuild_source("hotfix_nt")
    check("C7 cursor>0 且 processed=0 的 done 行自愈重扫",
          res3["status"] == "done" and res3["indexed"] > 0, res3)
    tags.invalidate_ready_cache()
    check("C8 修完 needs_repair=False", tags.needs_repair() is False, "")
    check("C9 ready() 保持 true", tags.ready() is True, tags.ready())

    # ---------------- D. tag_found 信号 ----------------
    print("\nD. tag_found 信号")
    t = tags.search_page(["hotfix_nt"], "悬疑")
    check("D1 存在标签 tag_found=true", t["total"] == 12 and t["tag_found"] is True,
          (t["total"], t.get("tag_found")))
    t2 = tags.search_page(["hotfix_nt"], "夜店")
    check("D2 标题词不在标签表 tag_found=false（total=0）",
          t2["total"] == 0 and t2["tag_found"] is False, (t2["total"], t2.get("tag_found")))

    r = client.get("/api/v1/search", params={"tag": "夜店"})
    j = r.json()
    check("D3 /api/v1/search?tag= 透传 tag_found=false",
          j.get("total") == 0 and j.get("tag_found") is False, j.get("tag_found"))
    r = client.get("/api/v1/search", params={"tag": "悬疑"})
    check("D4 /api/v1/search?tag= tag_found=true", r.json().get("tag_found") is True, "")
    r = client.get("/api/v1/videos", params={"tag": "悬疑", "source": "hotfix_nt"})
    check("D5 /api/v1/videos?tag= tag_found=true", r.json().get("tag_found") is True, "")

    # ---------------- E. 文档与样例 ----------------
    print("\nE. 文档与样例")
    dh = open(os.path.join(ROOT, "docs", "DEPLOY_HTTPS.md"), encoding="utf-8").read()
    check("E1 DEPLOY_HTTPS 有 11004 排障节",
          "os error 11004" in dh and "AAAA" in dh and "A 记录" in dh, "")
    check("E2 过渡地址已记录", "100.84.176.119:8181" in dh, "")
    ng = open(os.path.join(ROOT, "deploy", "nginx-vodsync.conf"), encoding="utf-8").read()
    check("E3 nginx 样例有 upstream 域名警示", "11004" in ng and "严禁写公网域名" in ng, "")
    spec = open(os.path.join(ROOT, "docs", "API_SPEC_FOR_AI.md"), encoding="utf-8").read()
    check("E4 API_SPEC 记载 tag_found", "tag_found" in spec, "")
    check("E5 API_SPEC 记载 /healthz.capabilities 真实端点",
          "healthz.capabilities" in spec and "独立端点" in spec, "")

    print()
    print("=" * 50)
    print(f"通过 {passed} / 失败 {failed} / 跳过 {skipped}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
