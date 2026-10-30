"""
v1.2 暂停/继续功能测试。
覆盖：
  [1] /api/v1/pause 初始状态（全未暂停）
  [2] POST /api/v1/pause?scope=... 三个层级独立生效 + 状态持久化文件
  [3] 暂停后 sync_source 入口拦截（full / incremental / global）
  [4] 真实全量同步中途暂停 -> 断点保存 + 日志 status=paused
  [5] 继续后从断点续传（last_page 前进）
  [6] resume API（含自动续跑受理）与 stats.pause 字段
  [7] 管理面板包含暂停控件
运行：rm -rf data-test && python scripts/test_v12_pause.py
"""
import json
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATA_DIR", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data-test"))
os.environ["FULL_SYNC_ON_DISCOVER"] = "false"

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from app import database, pause, syncer  # noqa: E402
from app.main import app  # noqa: E402

PASS = []


def ok(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print(f"[{len(PASS)}] {name} OK {detail}")
    else:
        print(f"!! FAIL: {name} {detail}")
        sys.exit(1)


def main():
    database.init_db()
    syncer.refresh_sources()

    from fastapi.testclient import TestClient

    client = TestClient(app)

    # ---- [1] 初始状态 ----
    r = client.get("/api/v1/pause").json()
    ok("pause 初始状态", r["code"] == 1 and not r["global"] and not r["full"] and not r["incremental"], str(r))

    # ---- [2] 三层级独立暂停 + 持久化 ----
    client.post("/api/v1/pause?scope=full&reason=test")
    r = client.get("/api/v1/pause").json()
    ok("暂停 full 生效", r["full"] is True and r["global"] is False and r["incremental"] is False)
    ok("暂停状态已持久化", os.path.exists(pause._file_path()))
    state_file = pause._file_path()
    with open(state_file, encoding="utf-8") as f:
        saved = json.load(f)
    ok("持久化内容正确", saved.get("full") is True)

    client.post("/api/v1/pause?scope=incremental")
    r = client.get("/api/v1/pause").json()
    ok("暂停 incremental 生效", r["full"] and r["incremental"] and not r["global"])

    # ---- [3] 入口拦截 ----
    slugs = []
    with database.engine.connect() as conn:
        from sqlalchemy import select
        from app.models import sources
        slugs = [x[0] for x in conn.execute(select(sources.c.slug).limit(3)).all()]
    target = slugs[0] if slugs else "smoketest"
    res = syncer.sync_source(target, mode="full")
    ok("full 暂停拦截", res["status"] == "paused", str(res))
    res = syncer.sync_source(target, mode="incremental")
    ok("incremental 暂停拦截", res["status"] == "paused", str(res))

    client.post("/api/v1/pause?scope=global")
    client.post("/api/v1/resume?scope=full")
    client.post("/api/v1/resume?scope=incremental")
    r = client.get("/api/v1/pause").json()
    res = syncer.sync_source(target, mode="full")
    ok("global 暂停覆盖 full", res["status"] == "paused" and r["global"], str(res))
    res = syncer.sync_source(target, mode="incremental")
    ok("global 暂停覆盖 incremental", res["status"] == "paused")

    # ---- [4] 真实全量同步中途暂停 ----
    # 解除全部暂停，选一个数据量大的真实源站
    client.post("/api/v1/resume?scope=global")
    big = None
    with database.engine.connect() as conn:
        from sqlalchemy import select
        from app.models import sources
        # 优先 jkun（页数多），否则任一源站
        row = conn.execute(select(sources.c.slug).where(sources.c.slug == "jkun")).first()
        big = row[0] if row else (slugs[0] if slugs else None)
    assert big, "没有可用源站"
    result = {}

    def run():
        result["res"] = syncer.sync_source(big, mode="full")

    t = threading.Thread(target=run, daemon=True)
    t.start()
    time.sleep(6)                      # 让它先跑几页
    pause.set_paused("full", True)     # 中途暂停
    t.join(timeout=90)
    res = result.get("res") or {}
    with database.engine.connect() as conn:
        from sqlalchemy import select
        from app.models import sources
        src = conn.execute(select(sources).where(sources.c.slug == big)).mappings().first()
    ok(
        "真实同步中途暂停: 断点已保存",
        res.get("status") == "paused" and (src["last_page"] or 0) > 0,
        f"status={res.get('status')} last_page={src['last_page']}",
    )
    checkpoint_page = src["last_page"]

    with database.engine.connect() as conn:
        from sqlalchemy import select
        from app.models import sync_logs
        lg = conn.execute(
            select(sync_logs).where(sync_logs.c.slug == big).order_by(sync_logs.c.id.desc()).limit(1)
        ).mappings().first()
    ok("同步日志记录 paused 状态", lg is not None and lg["status"] == "paused",
       f"status={lg['status'] if lg else None}")

    # ---- [5] 继续后从断点续传 ----
    pause.set_paused("full", False)
    result2 = {}

    def run2():
        result2["res"] = syncer.sync_source(big, mode="full")

    t2 = threading.Thread(target=run2, daemon=True)
    t2.start()
    time.sleep(6)
    pause.set_paused("full", True)     # 再次暂停，观察断点前进
    t2.join(timeout=90)
    res2 = result2.get("res") or {}
    with database.engine.connect() as conn:
        from sqlalchemy import select
        from app.models import sources
        src2 = conn.execute(select(sources).where(sources.c.slug == big)).mappings().first()
    progressed = (src2["last_page"] or 0) > checkpoint_page or res2.get("status") == "success"
    ok(
        "继续后断点前进（续传生效）",
        progressed and res2.get("status") in ("paused", "success"),
        f"断点 {checkpoint_page} -> {src2['last_page']}, status={res2.get('status')}",
    )

    # ---- [6] resume API + stats.pause ----
    pause.set_paused("full", False)
    pause.set_paused("incremental", False)
    r = client.post("/api/v1/resume?scope=global").json()
    ok("resume 返回续跑源站清单", r["code"] == 1 and isinstance(r.get("resumed_sources"), list))
    r = client.get("/api/v1/stats").json()
    ok("stats 含 pause 字段", "pause" in r and r["pause"]["full"] is False, str(r.get("pause")))

    # ---- [8] 单源站暂停 / 继续 ----
    other = slugs[1] if len(slugs) > 1 else target
    r = client.post(f"/api/v1/sources/{big}/pause").json()
    ok("单源站暂停 API 受理", r["code"] == 1 and r["paused"] is True, str(r)[:80])
    lst = client.get("/api/v1/sources").json()["list"]
    row = next((x for x in lst if x["slug"] == big), None)
    ok("源站列表返回 paused 标记", row is not None and row["paused"] is True and row["paused_at"])
    res = syncer.sync_source(big, mode="full")
    ok("单源站暂停拦截 full", res["status"] == "paused", str(res))
    # 该源站暂停不影响其他源站（用判定函数断言，避免真的发起长时间同步）
    ok(
        "其他源站不受影响",
        syncer.should_stop(big, "full") is True and syncer.should_stop(other, "full") is False,
        f"{big}={syncer.should_stop(big, 'full')}, {other}={syncer.should_stop(other, 'full')}",
    )

    # 单源站暂停对进行中的全量同步生效（检查点停止）
    client.post(f"/api/v1/sources/{big}/resume")
    result3 = {}

    def run3():
        result3["res"] = syncer.sync_source(big, mode="full")

    t3 = threading.Thread(target=run3, daemon=True)
    t3.start()
    time.sleep(4)
    # 运行态断言：此刻该源站应为 running=true（这是「暂停」按钮的唯一展示依据）
    live = next((x for x in client.get("/api/v1/sources").json()["list"] if x["slug"] == big), {})
    ok(
        "运行中源站 running=true 且带模式/耗时",
        live.get("running") is True and live.get("running_mode") == "full"
        and (live.get("running_seconds") or 0) > 0,
        f"running={live.get('running')} mode={live.get('running_mode')} {live.get('running_seconds')}s",
    )
    snap = syncer.running_snapshot()
    lst = client.get("/api/v1/sources").json()["list"]
    mismatch = [x["slug"] for x in lst if bool(x["running"]) != (x["slug"] in snap)]
    ok("列表 running 字段与运行态一致", not mismatch, f"不一致: {mismatch[:3]}")
    ok(
        "存在未运行源站（不应展示暂停）",
        any(not x["running"] for x in lst),
        f"未运行 {sum(1 for x in lst if not x['running'])} / 共 {len(lst)}",
    )
    ok(
        "运行态与断点态解耦",
        live.get("syncing") is not None and "syncing" in live,
        "running=真实运行，syncing=全量未跑完",
    )
    syncer.set_source_paused(big, True)   # 只暂停这一个源站
    t3.join(timeout=90)
    res3 = result3.get("res") or {}
    left = next((x for x in client.get("/api/v1/sources").json()["list"] if x["slug"] == big), {})
    ok("结束后 running 回到 false", left.get("running") is False, f"running={left.get('running')}")
    with database.engine.connect() as conn:
        from sqlalchemy import select
        from app.models import sources as src_tbl
        src3 = conn.execute(select(src_tbl).where(src_tbl.c.slug == big)).mappings().first()
    ok(
        "运行中单源站暂停生效（断点保存）",
        res3.get("status") == "paused" and (src3["last_page"] or 0) > 0 and bool(src3["paused"]),
        f"status={res3.get('status')} last_page={src3['last_page']} paused={src3['paused']}",
    )

    # 继续单个源站
    r = client.post(f"/api/v1/sources/{big}/resume").json()
    ok("单源站继续 API", r["code"] == 1 and r["paused"] is False and r.get("blocked_by") == "", str(r)[:90])
    lst = client.get("/api/v1/sources").json()["list"]
    row = next((x for x in lst if x["slug"] == big), None)
    ok("继续后 paused 标记清除", row is not None and row["paused"] is False)

    # 全局暂停存在时，单源站 continue 应提示被上层阻断
    client.post("/api/v1/pause?scope=global")
    r = client.post(f"/api/v1/sources/{big}/resume").json()
    ok("全局暂停时单源站继续被阻断提示", r.get("blocked_by") == "full", str(r)[:90])
    client.post("/api/v1/resume?scope=global")
    syncer.set_source_paused(big, True)   # 防止测试期间后台继续抓取

    # ---- [7] 面板包含暂停控件 ----
    r = client.get("/")
    html = r.text
    ok(
        "面板含暂停控件",
        all(k in html for k in ["pauseGlobal", "pauseFull", "pauseIncr", "pauseBanner",
                                "/api/v1/pause", "/api/v1/resume",
                                'data-act="pause"', 'data-act="resume"', "toggleSourcePause"]),
        f"HTTP {r.status_code}, {len(html)} bytes",
    )
    ok(
        "面板按运行态决定是否显示暂停",
        all(k in html for k in ["s.running", "running_mode", "running_seconds"]),
        "暂停按钮仅在 running=true 时渲染",
    )

    # 清理
    for scope in ("global", "full", "incremental"):
        pause.set_paused(scope, False)
    try:
        os.remove(state_file)
    except OSError:
        pass
    print(f"\nALL_V12_PAUSE_TESTS_PASSED ({len(PASS)} 项)")


if __name__ == "__main__":
    main()
