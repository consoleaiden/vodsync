"""
数据迁移脚本回归测试（SQLite -> SQLite，走与 PG 完全相同的代码路径）。

覆盖点：
  1. 表结构：sources / sync_logs / 全部 vod_* 都建出来
  2. 行数一致：元数据表 + 数据表（含跨批翻页，batch=100 / 500 行）
  3. 关键字段保留：watermark（增量水位线）、last_page/total_pages（断点）、
     paused、auto_sync、last_sync_at、total_videos —— 决定迁移后能否直接增量
  4. 内容保真：中文、NULL、超长字段（按列长度截断）、时间字符串 -> datetime
  5. 幂等：重复执行不产生重复行
  6. --replace：源库改过的行能覆盖过去
  7. --only-meta：只搬元数据，不建 vod 表
  8. --dry-run：不写目标库

运行：python scripts/test_migration.py
"""
import contextlib
import importlib.util
import io
import os
import shutil
import sys
import tempfile
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from sqlalchemy import create_engine, func, inspect, select, text  # noqa: E402

from app.models import META, build_vod_table, sources, sync_logs  # noqa: E402

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


def load_migrator():
    path = os.path.join(ROOT, "scripts", "migrate_sqlite_to_pg.py")
    spec = importlib.util.spec_from_file_location("migrate_sqlite_to_pg", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run_quiet(fn, *a, **kw):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = fn(*a, **kw)
    return code, buf.getvalue()


def build_source(path):
    """构造一个有代表性的源库：3 个源站 + 2 张数据表。"""
    url = f"sqlite:///{path}"
    eng = create_engine(url, future=True)
    META.create_all(eng)
    t_a, t_b = build_vod_table("alpha"), build_vod_table("beta")
    t_a.create(eng, checkfirst=True)
    t_b.create(eng, checkfirst=True)

    with eng.begin() as c:
        c.execute(sources.insert(), [
            {   # 已同步 + 有水位线（最关键：迁移后要能直接增量）
                "name": "甲站", "slug": "alpha", "api_url": "https://a.example.com/api.php",
                "status": "ok", "enabled": 1, "total_videos": 500,
                "watermark": "2026-10-30 12:00:00", "last_page": 0, "total_pages": 0,
                "paused": 0, "auto_sync": 1,
                "last_sync_at": datetime(2026, 10, 30, 12, 0, 1, 123456),
            },
            {   # 全量进行到一半 + 被单独暂停
                "name": "乙站", "slug": "beta", "api_url": "https://b.example.com/api.php",
                "status": "down", "enabled": 1, "total_videos": 3,
                "watermark": "", "last_page": 7, "total_pages": 40,
                "paused": 1, "auto_sync": 0,
                "last_sync_at": datetime(2026, 10, 30, 9, 30, 0),
                "paused_at": datetime(2026, 10, 30, 9, 31, 0),
            },
            {   # 从未同步
                "name": "丙站", "slug": "gamma", "api_url": "https://c.example.com/api.php",
                "status": "ok", "enabled": 1, "total_videos": 0,
                "watermark": "", "last_page": 0, "total_pages": 0,
                "paused": 0, "auto_sync": 1,
                "last_sync_at": None, "paused_at": None,
            },
        ])
        c.execute(sync_logs.insert(), [
            {"slug": "alpha", "mode": "full", "status": "success",
             "fetched": 500, "inserted": 500, "updated": 0, "pages": 5, "error": ""},
            {"slug": "beta", "mode": "full", "status": "running",
             "fetched": 60, "inserted": 60, "updated": 0, "pages": 7, "error": "中断"},
        ])
        # 500 行用于测跨批翻页（batch=100）
        c.execute(t_a.insert(), [
            {"vod_id": 10000 + i, "type_id": i % 5, "type_name": f"分类{i % 5}",
             "name": f"影片{i}", "pic": f"https://img.example.com/{i}.jpg",
             "remarks": "HD", "content": "简介" * 10,
             "play_url": f"HD$https://play.example.com/{i}.m3u8",
             "vod_time": "2026-10-30 10:00:00"}
            for i in range(500)
        ])
        # 边界数据：中文、空简介、超长封面（>1024 应被截断）
        c.execute(t_b.insert(), [
            {"vod_id": 1, "type_id": 1, "type_name": "电影", "name": "测试影片·中文",
             "pic": "https://x.example.com/" + "a" * 1100, "remarks": "更新至第10集",
             "content": "", "play_url": "L1$第1集$https://p.example.com/1.m3u8#第2集$https://p.example.com/2.m3u8",
             "vod_time": "2026-10-29 23:59:59"},
            {"vod_id": 2, "type_id": 2, "type_name": "", "name": "无分类",
             "pic": "", "remarks": "", "content": None, "play_url": "", "vod_time": ""},
            {"vod_id": 3, "type_id": 3, "type_name": "剧集", "name": "带引号'与\"双引号\"",
             "pic": "", "remarks": "", "content": "含换行\n的简介", "play_url": "x",
             "vod_time": "2026-10-28 00:00:00"},
            # SQLite 允许、PostgreSQL 拒绝的 NUL 字节
            {"vod_id": 4, "type_id": 4, "type_name": "其他", "name": "含NUL", "pic": "",
             "remarks": "", "content": "简介\x00里有NUL", "play_url": "", "vod_time": ""},
        ])
    eng.dispose()
    return url


def count(eng, name):
    with eng.connect() as c:
        return int(c.execute(select(func.count()).select_from(META.tables[name])).scalar() or 0)


def main():
    tmp = tempfile.mkdtemp(prefix="vodsync-mig-test-")
    mig = load_migrator()
    try:
        src = os.path.join(tmp, "src.db")
        tgt = os.path.join(tmp, "tgt.db")
        src_url = build_source(src)
        tgt_url = f"sqlite:///{tgt}"

        print("[1] 首次迁移（batch=100，强制跨批翻页）")
        code, out = run_quiet(mig.main, ["--source", src, "--target", tgt_url, "--batch", "100"])
        check("退出码 0", code == 0, f"exit={code}")
        tgt_eng = create_engine(tgt_url, future=True)
        insp = inspect(tgt_eng)
        names = set(insp.get_table_names())
        check("元数据表已建", {"sources", "sync_logs"} <= names, str(sorted(names)))
        check("vod_alpha 已建", "vod_alpha" in names)
        check("vod_beta 已建", "vod_beta" in names)

        print("[2] 行数一致")
        src_eng = create_engine(src_url, future=True)
        for t in ("sources", "sync_logs", "vod_alpha", "vod_beta"):
            a, b = count(src_eng, t), count(tgt_eng, t)
            check(f"{t} 行数 {a} == {b}", a == b, f"源 {a} / 目标 {b}")
        check("vod_alpha = 500 行", count(tgt_eng, "vod_alpha") == 500)

        print("[3] 关键字段保留（决定迁移后能否直接增量）")
        with tgt_eng.connect() as c:
            rows = {r["slug"]: r for r in c.execute(select(sources)).mappings().all()}
        a = rows["alpha"]
        check("alpha.watermark 保留", a["watermark"] == "2026-10-30 12:00:00", repr(a["watermark"]))
        check("alpha.total_videos 保留", a["total_videos"] == 500)
        check("alpha.last_sync_at 是 datetime", isinstance(a["last_sync_at"], datetime), type(a["last_sync_at"]))
        check("alpha.last_sync_at 微秒保真",
              str(a["last_sync_at"]).startswith("2026-10-30 12:00:01.123456"), str(a["last_sync_at"]))
        b = rows["beta"]
        check("beta.last_page 保留 = 7", b["last_page"] == 7, repr(b["last_page"]))
        check("beta.total_pages 保留 = 40", b["total_pages"] == 40)
        check("beta.paused 保留 = 1", b["paused"] == 1)
        check("beta.auto_sync 保留 = 0", b["auto_sync"] == 0)
        check("beta.status 保留 = down", b["status"] == "down")
        check("gamma 未同步态保留", rows["gamma"]["watermark"] == "" and rows["gamma"]["total_videos"] == 0)

        print("[4] 内容保真")
        with tgt_eng.connect() as c:
            t = build_vod_table("alpha")
            row = c.execute(select(t).where(t.c.vod_id == 10420)).mappings().first()
            check("影片名称一致", row["name"] == "影片420", row["name"])
            check("播放地址完整", row["play_url"] == "HD$https://play.example.com/420.m3u8", row["play_url"])
            tb = build_vod_table("beta")
            r1 = c.execute(select(tb).where(tb.c.vod_id == 1)).mappings().first()
            check("超长 pic 被截断到 1024", len(r1["pic"]) == 1024, f"len={len(r1['pic'])}")
            check("中文与换行保真", r1["name"] == "测试影片·中文")
            r2 = c.execute(select(tb).where(tb.c.vod_id == 2)).mappings().first()
            check("NULL content 保持 NULL", r2["content"] is None, repr(r2["content"]))
            r3 = c.execute(select(tb).where(tb.c.vod_id == 3)).mappings().first()
            check("含引号名称保真", r3["name"] == "带引号'与\"双引号\"", repr(r3["name"]))
            check("含换行简介保真", r3["content"] == "含换行\n的简介", repr(r3["content"]))
            check("synced_at 是 datetime", isinstance(r3["synced_at"], datetime), type(r3["synced_at"]))
            r4 = c.execute(select(tb).where(tb.c.vod_id == 4)).mappings().first()
            check("NUL 字节已剔除（PG 不接受 \\x00）",
                  "\x00" not in (r4["content"] or "") and r4["content"] == "简介里有NUL",
                  repr(r4["content"]))

        print("[5] 幂等：重复执行不产生重复行")
        before = count(tgt_eng, "vod_alpha")
        code2, _ = run_quiet(mig.main, ["--source", src, "--target", tgt_url, "--batch", "100"])
        check("重复执行退出码 0", code2 == 0)
        check("vod_alpha 行数不变", count(tgt_eng, "vod_alpha") == before, f"{before} -> {count(tgt_eng, 'vod_alpha')}")
        check("sources 行数不变（3）", count(tgt_eng, "sources") == 3)

        print("[6] --replace 覆盖已有行")
        with src_eng.begin() as c:
            c.execute(sources.update().where(sources.c.slug == "alpha").values(name="甲站改名", last_page=3))
        run_quiet(mig.main, ["--source", src, "--target", tgt_url, "--replace"])
        with tgt_eng.connect() as c:
            nm = c.execute(select(sources.c.name).where(sources.c.slug == "alpha")).scalar()
            lp = c.execute(select(sources.c.last_page).where(sources.c.slug == "alpha")).scalar()
        check("--replace 更新了 name", nm == "甲站改名", nm)
        check("--replace 更新了 last_page", lp == 3, lp)
        with src_eng.begin() as c:  # 复原，避免影响后续断言
            c.execute(sources.update().where(sources.c.slug == "alpha").values(name="甲站", last_page=0))

        print("[7] --only-meta 只搬元数据")
        tgt2 = os.path.join(tmp, "tgt2.db")
        code3, _ = run_quiet(mig.main, ["--source", src, "--target", f"sqlite:///{tgt2}", "--only-meta"])
        check("only-meta 退出码 0", code3 == 0, f"exit={code3}")
        e2 = create_engine(f"sqlite:///{tgt2}", future=True)
        n2 = set(inspect(e2).get_table_names())
        check("only-meta 建了 sources", "sources" in n2)
        check("only-meta 不建 vod 表", not any(x.startswith("vod_") for x in n2), str(sorted(n2)))
        check("only-meta sources = 3 行", count(e2, "sources") == 3)

        print("[8] --dry-run 不写目标库")
        tgt3 = os.path.join(tmp, "tgt3.db")
        code4, out4 = run_quiet(mig.main, ["--source", src, "--target", f"sqlite:///{tgt3}", "--dry-run"])
        check("dry-run 退出码 0", code4 == 0)
        check("dry-run 报告总行数", "503" in out4 or "500" in out4, out4[-200:].replace("\n", " "))
        if os.path.exists(tgt3):
            e3 = create_engine(f"sqlite:///{tgt3}", future=True)
            check("dry-run 不建任何表", len(inspect(e3).get_table_names()) == 0,
                  str(inspect(e3).get_table_names()))
        else:
            check("dry-run 不创建目标文件", True)

        print("[9] 源库不存在时给出明确错误")
        code5, out5 = run_quiet(mig.main, ["--source", os.path.join(tmp, "nope.db"),
                                          "--target", tgt_url])
        check("缺源库退出码 2", code5 == 2, f"exit={code5}")
        check("提示 --source", "--source" in out5, out5[:120])

        print("[10] --tables 只迁指定源站")
        tgt4 = os.path.join(tmp, "tgt4.db")
        run_quiet(mig.main, ["--source", src, "--target", f"sqlite:///{tgt4}", "--tables", "beta"])
        e4 = create_engine(f"sqlite:///{tgt4}", future=True)
        n4 = set(inspect(e4).get_table_names())
        check("只建了 vod_beta", "vod_beta" in n4 and "vod_alpha" not in n4, str(sorted(n4)))

        print("[11] 索引在数据写完后创建")
        idx_names = {i["name"] for i in inspect(tgt_eng).get_indexes("vod_alpha")}
        for want in ("idx_alpha_type", "idx_alpha_time", "idx_alpha_name"):
            check(f"{want} 已创建", want in idx_names, str(sorted(idx_names)))

        print("[12] 目标库不可达时明确报错（退出码 3）")
        code6, out6 = run_quiet(mig.main, ["--source", src,
                                          "--target", "sqlite:///" + os.path.join(tmp, "nodir", "x.db")])
        check("不可达退出码 3", code6 == 3, f"exit={code6}")
        check("给出排查清单", "docker ps" in out6 and "通用排查" in out6,
              out6[-160:].replace("\n", " "))

        print("[13] --repair 补齐中断的迁移（模拟 SSH 断线）")
        tgt5 = os.path.join(tmp, "tgt5.db")
        tgt5_url = f"sqlite:///{tgt5}"
        run_quiet(mig.main, ["--source", src, "--target", tgt5_url, "--batch", "100"])
        e5 = create_engine(tgt5_url, future=True)
        # 破坏现场：alpha 只留前 120 行（模拟迁到一半断线），beta 全清空
        with e5.begin() as c:
            c.execute(text("DELETE FROM vod_alpha WHERE vod_id > 10119"))
            c.execute(text("DELETE FROM vod_beta"))
        check("破坏后 alpha = 120 行", count(e5, "vod_alpha") == 120, count(e5, "vod_alpha"))

        code7, out7 = run_quiet(mig.main, ["--source", src, "--target", tgt5_url,
                                           "--batch", "100", "--repair"])
        check("repair 退出码 0", code7 == 0, f"exit={code7}\n{out7[-500:]}")
        check("repair 后 alpha = 500", count(e5, "vod_alpha") == 500, count(e5, "vod_alpha"))
        check("repair 后 beta = 4", count(e5, "vod_beta") == 4, count(e5, "vod_beta"))
        check("repair 有「全部一致」", "全部一致" in out7, out7[-200:].replace("\n", " "))
        check("repair 报告补了 2 张表", "补齐 2 张表" in out7, out7[-600:].replace("\n", " "))

        # 再跑一次：没有差异时应直接说明「无需补齐」，且不改动数据
        code8, out8 = run_quiet(mig.main, ["--source", src, "--target", tgt5_url, "--repair"])
        check("二次 repair 退出码 0", code8 == 0, f"exit={code8}")
        check("二次 repair 提示无需补齐", "无需补齐" in out8, out8[-200:].replace("\n", " "))
        check("二次 repair 未改行数", count(e5, "vod_alpha") == 500)

        check("repair 与 --verify 互斥", run_quiet(
            mig.main, ["--source", src, "--target", tgt5_url, "--repair", "--verify"])[0] == 2)

        src_eng.dispose()
        tgt_eng.dispose()
        for e in (e2, e4, e5):
            e.dispose()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    print(f"结果：{passed} 通过 / {failed} 失败")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
