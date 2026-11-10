"""
v1.14.1 标签回填进度修复回归测试。

背景（真实线上 bug，用户截图报的）：
    tag_index_state.progress = cursor * 100 / total
但两者量纲不同：
    cursor = 已处理到的**上游 vod_id 水位**（供断点续跑用 WHERE vod_id > cursor）
    total  = 表里的**行数**
上游 vod_id 稀疏、且经常不从 1 开始（截图中某站最大 ID 109567 却只有 74467 条），
于是面板出现 147.1% / 172.6% 这种不存在的进度。

修复：进度改用「已处理条数 processed / 总条数 total」，两者同量纲；
      cursor 只留在接口里做断点参考，面板改列名并把 cursor 放进 tooltip。

覆盖：
 A. _progress 纯函数语义（完成态 / 无分母 / 超界钳制 / 绝不回退到 cursor）
 B. 复现截图：cursor 远大于 total 的 running 行，进度不得 >100
 C. 稀疏 ID 端到端回填：processed == total、进度 100%
 D. 续跑种子：存量行 processed=0 但 cursor 已推进 → 用 COUNT(vod_id<=cursor) 补算
 E. 迁移：老库（无 processed 列）经 _migrate() 自动补列
 F. 面板：tagPct 夹取、表头改成「已处理 / 总条数」、不再裸插值 s.progress
 G. 契约：/api/v1/tag/status 每行都有 processed，且 progress ∈ [0,100]

运行：python scripts/test_v1141_tagprogress.py
（F 段需要 node；无 node 时标记 SKIP）
"""
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="vodsync-v1141-"))
os.environ.setdefault("DATABASE_URL",
                      "sqlite:///" + os.path.join(os.environ["DATA_DIR"], "v1141.db"))
os.environ.setdefault("AUTH_ENABLED", "false")
os.environ.setdefault("FULL_SYNC_ON_DISCOVER", "false")
os.environ.setdefault("AUTO_RESUME_ON_BOOT", "false")
os.environ.setdefault("RELATED_ENABLED", "false")
os.environ.setdefault("TAG_INDEX_ON_STARTUP", "false")
os.environ.setdefault("TAG_INDEX_SLEEP_MS", "0")

HTML = os.path.join(ROOT, "app", "web", "index.html")

passed = failed = skipped = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


def skip(name, why=""):
    global skipped
    skipped += 1
    print(f"  SKIP  {name}  {why}")


def main():
    from app import auth, config, database, tags

    database.init_db()
    auth.ensure_init()
    config.TAG_INDEX_BATCH = 10          # 小批 → 多轮 upsert，覆盖循环里累加 processed
    config.TAG_INDEX_SLEEP_MS = 0

    # ---------------- A. _progress 纯函数 ----------------
    print("\nA. _progress 语义")
    P = tags._progress
    check("A1 完成态 → 100", P("done", 0, 100) == 100.0, P("done", 0, 100))
    check("A2 running 半程 → 50", P("running", 50, 100) == 50.0, P("running", 50, 100))
    check("A3 processed 超 total（并发插新片）→ 钳到 100",
          P("running", 120, 100) == 100.0, P("running", 120, 100))
    check("A4 total=0 → 0（不除零）", P("running", 5, 0) == 0.0, P("running", 5, 0))
    check("A5 done 但 0 条 → 0", P("done", 0, 0) == 0.0, P("done", 0, 0))
    # 关键回归：processed=0 的 running 行，即便 cursor 远大于 total 也必须是 0
    check("A6 processed=0 时不回退用 cursor（147% 那一版会返回 147.1）",
          P("running", 0, 74467) == 0.0, P("running", 0, 74467))

    # ---------------- B. 复现截图：cursor >> total 的 running 行 ----------------
    print("\nB. 复现用户截图（cursor 与 total 量纲不同）")
    from app.models import sources, tag_index_state

    with database.write_txn() as conn:
        conn.execute(tag_index_state.delete())
        conn.execute(tag_index_state.insert().values(
            slug="s360", cursor=109567, processed=70000, total=74467,
            indexed=1876, status="running", error=""))
        conn.execute(tag_index_state.insert().values(
            slug="s378893", cursor=171908, processed=0, total=170592,
            indexed=5172, status="running", error=""))
        conn.execute(tag_index_state.insert().values(
            slug="s5f1", cursor=133167, processed=133167, total=125463,
            indexed=3193, status="running", error=""))
        conn.execute(tag_index_state.insert().values(
            slug="s778", cursor=165652, processed=165652, total=182982,
            indexed=0, status="running", error=""))
    st = {s["slug"]: s for s in tags.status()["sources"]}
    check("B1 109567/74467 → 94.0%（不是 147.1%）",
          st["s360"]["progress"] == 94.0, st["s360"]["progress"])
    check("B2 171908/170592 但 processed=0 → 0.0%（不是 100.7%）",
          st["s378893"]["progress"] == 0.0, st["s378893"]["progress"])
    check("B3 processed 超 total → 钳到 100.0",
          st["s5f1"]["progress"] == 100.0, st["s5f1"]["progress"])
    check("B4 正常半程 165652/182982 → 90.5%",
          st["s778"]["progress"] == 90.5, st["s778"]["progress"])
    check("B5 所有行 progress ∈ [0,100]",
          all(0.0 <= s["progress"] <= 100.0 for s in st.values()),
          {k: v["progress"] for k, v in st.items()})
    check("B6 每行都带 processed 字段",
          all("processed" in s for s in st.values()), list(st["s360"].keys()))
    check("B7 cursor 仍在（断点调试要用）", st["s360"]["cursor"] == 109567)

    # ---------------- C. 稀疏 ID 端到端 ----------------
    print("\nC. 稀疏 vod_id 端到端回填")
    from app import syncer

    with database.write_txn() as conn:
        conn.execute(sources.delete().where(sources.c.slug == "sparse"))
        conn.execute(sources.insert().values(
            name="稀疏源", slug="sparse", api_url="https://sparse/api.php",
            status="ok", enabled=1, total_videos=0, origin="custom"))
    syncer.ensure_table("sparse")
    ids = list(range(50000, 52000, 50))          # 40 条，ID 从 5 万起、间隔 50
    rows = [{
        "vod_id": i, "type_id": 1, "type_name": "电影", "name": f"稀疏片{i}",
        "pic": "", "remarks": "HD", "content": "x",
        "play_url": "第01集$https://cdn/1.m3u8", "play_from": "ffm3u8",
        "vod_actor": "", "vod_director": "", "vod_writer": "",
        "vod_area": "大陆", "vod_year": "2024",
        "vod_tag": "悬疑,犯罪" if i % 100 == 0 else "喜剧",
        "vod_time": "2026-11-09 10:00:00",
    } for i in ids]
    syncer.upsert_batch("sparse", rows)

    res = tags.rebuild_source("sparse", force=True)
    check("C1 回填完成", res.get("status") == "done", res)
    sp = {s["slug"]: s for s in tags.status()["sources"]}["sparse"]
    check("C2 total = 行数 40", sp["total"] == 40, sp["total"])
    check("C3 cursor = 最大 vod_id 51950（不是条数）", sp["cursor"] == ids[-1], sp["cursor"])
    check("C4 processed = 40（已处理条数）", sp["processed"] == 40, sp["processed"])
    check("C5 进度 100.0（旧算法这里会算出 1298.8%）", sp["progress"] == 100.0, sp["progress"])

    # ---------------- D. 续跑种子（存量行 processed=0） ----------------
    print("\nD. 断点续跑的 processed 补算")
    mid = ids[20]                                  # 第 21 条
    with database.write_txn() as conn:
        conn.execute(tag_index_state.update()
                     .where(tag_index_state.c.slug == "sparse")
                     .values(cursor=mid, processed=0, status="running"))
    tags.rebuild_source("sparse", force=False)
    sp = {s["slug"]: s for s in tags.status()["sources"]}["sparse"]
    # 若种子没补算，processed 只会是 cursor 之后的 19 条
    check("D1 续跑后 processed 仍等于 total（种子补算生效）",
          sp["processed"] == 40, sp["processed"])
    check("D2 续跑后进度 100.0", sp["progress"] == 100.0, sp["progress"])
    check("D3 标签行没被重复累加", sp["indexed"] > 0, sp["indexed"])

    # ---------------- E. 迁移补列 ----------------
    print("\nE. 存量库迁移")
    db_path = os.environ["DATABASE_URL"].replace("sqlite:///", "")
    try:
        import sqlite3
        raw = sqlite3.connect(db_path)
        raw.execute("ALTER TABLE tag_index_state DROP COLUMN processed")
        raw.commit()
        raw.close()
        database._migrate()
        import sqlalchemy as sa
        cols = {c["name"] for c in sa.inspect(database.engine).get_columns("tag_index_state")}
        check("E1 _migrate() 自动补出 processed 列", "processed" in cols, sorted(cols))
    except Exception as e:                          # noqa: BLE001
        skip("E1 SQLite 不支持 DROP COLUMN，跳过迁移复现", str(e)[:60])

    # ---------------- F. 面板渲染 ----------------
    print("\nF. 面板")
    html = io.open(HTML, encoding="utf-8").read()
    check("F1 表头改成「已处理 / 总条数」", "已处理 / 总条数" in html)
    check("F2 不再出现旧的「游标 / 总条数」表头", "游标 / 总条数" not in html)
    check("F3 百分比走 tagPct 夹取", "${tagPct(s.progress)}%" in html)
    check("F4 不再裸插值 s.progress", "${s.progress}%" not in html)
    check("F5 cursor 降级为 tooltip（断点调试仍可见）",
          "断点游标 vod_id=" in html)

    m = re.search(r"function tagPct\(p\) \{[\s\S]*?\n\}", html)
    if not m:
        check("F6 切出 tagPct 源码", False, "未匹配到函数")
    else:
        check("F6 切出 tagPct 源码", True)
    node = shutil.which("node")
    if not node:
        skip("F7 tagPct 边界值（node 未安装）")
    else:
        snippet = m.group(0) + "\nconsole.log(JSON.stringify([tagPct(147.1), tagPct(-5), " \
                                "tagPct('abc'), tagPct(100), tagPct(0), tagPct(null)]));"
        r = subprocess.run([node, "-e", snippet], capture_output=True, text=True)
        if r.returncode != 0:
            check("F7 tagPct 边界值", False, r.stderr[:200])
        else:
            vals = json.loads(r.stdout.strip())
            check("F7 tagPct 夹取边界 [147.1→100, -5→0, 'abc'→0, 100, 0, null→0]",
                  vals == [100, 0, 0, 100, 0, 0], vals)
        # 整段 JS 语法（防止改渲染逻辑时手滑）
        scripts = re.findall(r"<script>(.*?)</script>", html, re.S)
        tmp = os.path.join(tempfile.mkdtemp(), "panel.js")
        io.open(tmp, "w", encoding="utf-8").write(scripts[-1])
        r = subprocess.run([node, "--check", tmp], capture_output=True, text=True)
        check("F8 面板 JS 语法通过 node --check", r.returncode == 0, r.stderr[:200])
        shutil.rmtree(os.path.dirname(tmp), ignore_errors=True)

    # ---------------- G. 接口契约 ----------------
    print("\nG. 接口契约")
    from app.main import app  # noqa: E402
    from fastapi.testclient import TestClient

    body = TestClient(app).get("/api/v1/tag/status").json()
    check("G1 200 + code 字段", "code" in body, list(body.keys())[:5])
    check("G2 sources 每行都有 processed / progress",
          all({"processed", "progress", "total"} <= set(s) for s in body["sources"]),
          body["sources"][:1])
    check("G3 进度全部 0–100",
          all(0.0 <= s["progress"] <= 100.0 for s in body["sources"]),
          [s["progress"] for s in body["sources"]])

    print()
    print("=" * 50)
    print(f"通过 {passed} / 失败 {failed} / 跳过 {skipped}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
