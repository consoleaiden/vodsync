"""
v1.6.0 同步可靠性测试（复盘报告里的三个 P0 + 重启恢复 + 安全项）。

覆盖项：
 A. 全量同步「批内单页失败」不再静默丢页
    A1 断点只落在连续完成的前缀（第 3 页挂 → 断点=2，不是 4）
    A2 任务标记为 incomplete，不再显示 100% 完成
    A3 重试次数用尽后中止，断点与 total_pages 保留（可续传）
    A4 源站恢复后从断点续传，最终 40 条齐全、断点清零、水位线写入
 B. 增量水位线按「实际入库」推进
    B1 详情只返回 1/2 条时，水位线不越过未入库那条（下次还能选中）
    B2 下一轮补上该条后，水位线才前进
 C. 增量 list 某页失败不再整站崩溃，且水位线冻结（不跳过漏掉的页）
 D. 重启恢复：残留 running 日志被标记为 interrupted
 E. 安全：默认不信任 X-Forwarded-For（防伪造 IP 绕过登录锁定）；TRUST_PROXY=true 取最后一跳
 F. 隐患：write_lock_only 可正常使用；pause_state.json 原子写
 G. 全量完成路径不受影响（回归基线）

运行：python scripts/test_v160_recovery.py
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="vodsync-recovery-")
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(os.environ["DATA_DIR"], "rec.db")
os.environ["AUTH_ENABLED"] = "false"
os.environ["FULL_SYNC_ON_DISCOVER"] = "false"
os.environ["PAGE_CONCURRENCY"] = "4"
os.environ["CHECKPOINT_EVERY_PAGES"] = "1"
os.environ["PAGE_MAX_ATTEMPTS"] = "3"
os.environ["AUTO_RESUME_ON_BOOT"] = "false"   # 测试里手动调用，避免线程干扰

from sqlalchemy import func, select  # noqa: E402

from app.maccms import MacCmsClient as _RealClient  # noqa: E402
from app.maccms import MacCmsError  # noqa: E402


def client_factory(fake):
    """
    替换 syncer.MacCmsClient 用的工厂：返回 fake 实例，但保留类上的 extract_row
    （sync_source 内部会用 MacCmsClient() 造客户端，直接赋 lambda 会把类的静态方法一起搞丢）。
    """

    class _Patched(_RealClient):
        def __new__(cls, *a, **kw):
            return fake

    return _Patched

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


# ---------------------------------------------------------------------------
# 假客户端
# ---------------------------------------------------------------------------
class FullClient:
    """全量同步：pagecount 页 × per_page 条，fail_pages 里的页恒定抛错。"""

    def __init__(self, pagecount=4, per_page=10, fail_pages=()):
        self.pagecount = pagecount
        self.per_page = per_page
        self.fail_pages = set(fail_pages)
        self.calls = []

    def fetch_page(self, api, ac, pg):
        self.calls.append(pg)
        if pg in self.fail_pages:
            raise MacCmsError(f"模拟第 {pg} 页 500")
        return {
            "code": 1, "page": pg, "pagecount": self.pagecount,
            "total": self.pagecount * self.per_page, "class": [],
            "list": [
                {"vod_id": pg * 1000 + i, "vod_time": "2026-02-01 00:00:00",
                 "vod_name": f"p{pg}-{i}", "type_id": 1}
                for i in range(self.per_page)
            ],
        }

    def close(self):
        pass


class IncrClient:
    """增量同步：list 一页若干条；detail 只返回 detail_ids 里的 ID。"""

    def __init__(self, items, detail_ids, list_fail_pages=(), pagecount=1):
        self.items = items
        self.detail_ids = set(detail_ids)
        self.list_fail_pages = set(list_fail_pages)
        self.pagecount = pagecount
        self.detail_calls = 0

    def fetch_page(self, api, ac, pg):
        if ac == "list" and pg in self.list_fail_pages:
            raise MacCmsError(f"模拟 list 第 {pg} 页失败")
        chunk = self.items if pg == 1 else []
        return {"code": 1, "page": pg, "pagecount": self.pagecount, "total": len(self.items),
                "class": [], "list": chunk}

    def fetch_detail_by_ids(self, api, ids):
        self.detail_calls += 1
        rows = []
        for it in self.items:
            if it["vod_id"] in set(ids) and it["vod_id"] in self.detail_ids:
                rows.append(dict(it, vod_play_url=f"http://x/{it['vod_id']}.m3u8"))
        return {"code": 1, "list": rows}

    def close(self):
        pass


def add_source(slug, watermark="", last_page=0, total_pages=0):
    from app import database
    from app.models import sources
    from app.syncer import ensure_table

    with database.write_txn() as conn:
        conn.execute(sources.insert().values(
            name=slug, slug=slug, api_url=f"http://{slug}.test/api.php",
            status="ok", enabled=1, auto_sync=1, watermark=watermark,
            last_page=last_page, total_pages=total_pages,
        ))
    ensure_table(slug)


def src_row(slug):
    from app import database
    from app.models import sources

    with database.engine.connect() as conn:
        return conn.execute(select(sources).where(sources.c.slug == slug)).mappings().first()


def row_count(slug):
    from app import database
    from app.models import build_vod_table

    with database.engine.connect() as conn:
        return conn.execute(
            select(func.count()).select_from(build_vod_table(slug))
        ).scalar() or 0


def main():
    from app import config, database, pause, syncer
    from app.syncer import incremental_sync

    database.init_db()

    # =====================================================================
    print("[A] 全量同步：批内单页失败不得静默丢页")
    # =====================================================================
    slug = "t_full"
    add_source(slug)

    client = FullClient(pagecount=4, per_page=10, fail_pages={3})
    syncer.MacCmsClient = client_factory(client)
    res = syncer.sync_source(slug, "full")

    row = src_row(slug)
    check("A1 断点只落到连续前缀（=2，不是 4）", row["last_page"] == 2, f"last_page={row['last_page']}")
    check("A1b total_pages 保留（面板仍能显示进度）", row["total_pages"] == 4, row["total_pages"])
    check("A2 状态为 incomplete，不是 success", res.get("status") == "incomplete", res.get("status"))
    check("A2b 已入库页 1/2/4 = 30 条（不是 0、也不是 40）", row_count(slug) == 30, row_count(slug))
    check("A2c 断点未被清空", row["last_page"] > 0)
    check("A2d 水位线未推进（避免增量把漏页当已完成）", not row["watermark"], row["watermark"])
    check("A2e 错误信息可读", "第 3 页" in (row and res.get("error", "")), res.get("error"))

    from app.models import sync_logs
    with database.engine.connect() as conn:
        log_row = conn.execute(
            select(sync_logs.c.status, sync_logs.c.error).order_by(sync_logs.c.id.desc())
        ).first()
    check("A2f 同步日志状态 = incomplete", log_row[0] == "incomplete", log_row[0])
    check("A2g 日志里写明了失败原因", "第 3 页" in (log_row[1] or ""), log_row[1])

    # 源站恢复后从断点续传
    fixed = FullClient(pagecount=4, per_page=10)
    syncer.MacCmsClient = client_factory(fixed)
    res2 = syncer.sync_source(slug, "full")
    row = src_row(slug)
    # 第 1 页会被再抓一次：它不是"重新入库"，而是用来取 pagecount/total 的元信息请求
    check("A4 续传不重抓已完成页（只抓 1[元信息]、3、4）",
          sorted(fixed.calls) == [1, 3, 4], fixed.calls)
    check("A4b 最终 40 条齐全", row_count(slug) == 40, row_count(slug))
    check("A4c 断点已清零", row["last_page"] == 0, row["last_page"])
    check("A4d 水位线 = 库内最大 vod_time", row["watermark"] == "2026-02-01 00:00:00", row["watermark"])
    check("A4e 状态 success", res2.get("status") == "success", res2.get("status"))

    # =====================================================================
    print("[B] 增量：水位线只按实际入库推进")
    # =====================================================================
    slug = "t_wm"
    add_source(slug, watermark="2026-01-01 00:00:00")
    items = [
        {"vod_id": 1, "vod_time": "2026-03-01 00:00:00", "vod_name": "A", "type_id": 1},
        {"vod_id": 2, "vod_time": "2026-03-02 00:00:00", "vod_name": "B", "type_id": 1},
    ]
    inc = IncrClient(items, detail_ids={1})          # 第 2 条详情始终不返回
    stats = incremental_sync(slug, inc)
    row = src_row(slug)
    check("B1 只入库 1 条", row_count(slug) == 1, row_count(slug))
    check("B1b 水位线不越过未入库记录（=2026-03-01，不是 03-02）",
          row["watermark"] == "2026-03-01 00:00:00", row["watermark"])
    check("B1c 缺失条数被记录", stats.get("missing") == 1, stats.get("missing"))
    check("B1d 详情做了二次补抓（不是静默跳过）", inc.detail_calls >= 2, inc.detail_calls)

    inc2 = IncrClient(items, detail_ids={1, 2})      # 源站恢复完整返回
    incremental_sync(slug, inc2)
    row = src_row(slug)
    check("B2 下一轮补上第 2 条", row_count(slug) == 2, row_count(slug))
    check("B2b 水位线这时才前进到 03-02", row["watermark"] == "2026-03-02 00:00:00", row["watermark"])

    # =====================================================================
    print("[C] 增量：list 某页失败不再整站崩溃，水位线冻结")
    # =====================================================================
    slug = "t_list"
    add_source(slug, watermark="2026-01-01 00:00:00")
    items = [{"vod_id": 11, "vod_time": "2026-04-01 00:00:00", "vod_name": "C", "type_id": 1}]
    inc3 = IncrClient(items, detail_ids={11}, list_fail_pages={2}, pagecount=2)
    try:
        stats3 = incremental_sync(slug, inc3)
        crashed = False
    except Exception as e:                              # noqa: BLE001
        crashed = True
        stats3 = {"error": f"{type(e).__name__}: {e}"}

    row = src_row(slug)
    check("C1 不再抛出异常（旧代码 fut.result() 会重抛）", not crashed, stats3.get("error"))
    check("C2 已拿到的第 1 页数据仍然入库", row_count(slug) == 1, row_count(slug))
    check("C3 水位线冻结在旧值", row["watermark"] == "2026-01-01 00:00:00", row["watermark"])
    check("C4 状态标记 incomplete", stats3.get("incomplete") is True, stats3)

    # =====================================================================
    print("[D] 重启恢复：残留 running 日志 → interrupted")
    # =====================================================================
    from app.models import sync_logs
    with database.write_txn() as conn:
        conn.execute(sync_logs.insert().values(slug="t_full", mode="full", status="running"))
    n = syncer.mark_interrupted_logs()
    with database.engine.connect() as conn:
        row = conn.execute(
            select(sync_logs.c.status, sync_logs.c.finished_at, sync_logs.c.error)
            .order_by(sync_logs.c.id.desc())
        ).first()
    check("D1 影响 1 行", n == 1, n)
    check("D2 状态改为 interrupted", row[0] == "interrupted", row[0])
    check("D3 finished_at 已写", row[1] is not None)
    check("D4 错误说明可读", "重启" in (row[2] or ""), row[2])

    # =====================================================================
    print("[E] 安全：X-Forwarded-For 默认不信任")
    # =====================================================================
    from starlette.requests import Request
    from app.routes.api import _client_ip

    def req(xff=None, peer="10.0.0.9"):
        headers = [(b"x-forwarded-for", xff.encode())] if xff else []
        return Request({
            "type": "http", "method": "GET", "path": "/", "query_string": b"",
            "headers": headers, "client": (peer, 1234),
        })

    old = config.TRUST_PROXY
    config.TRUST_PROXY = False
    check("E1 默认忽略伪造的 XFF", _client_ip(req("1.2.3.4")) == "10.0.0.9", _client_ip(req("1.2.3.4")))
    config.TRUST_PROXY = True
    check("E2 开启后取最后一跳（反代追加的那个）",
          _client_ip(req("1.2.3.4, 203.0.113.7")) == "203.0.113.7",
          _client_ip(req("1.2.3.4, 203.0.113.7")))
    check("E3 无 XFF 时回落到对端地址", _client_ip(req(None)) == "10.0.0.9", _client_ip(req(None)))
    config.TRUST_PROXY = old

    # =====================================================================
    print("[F] 隐患：write_lock_only / pause_state 原子写")
    # =====================================================================
    ok = True
    try:
        with database.write_lock_only():
            pass
    except Exception as e:                              # noqa: BLE001
        ok = False
        print("      write_lock_only:", repr(e))
    check("F1 write_lock_only 可正常使用（旧代码双层装饰器会 TypeError）", ok)

    pause.set_paused("full", True)
    path = os.path.join(config.DATA_DIR, "pause_state.json")
    with open(path, "r", encoding="utf-8") as f:
        saved = json.load(f)
    check("F2 暂停状态已落盘且 JSON 合法", saved.get("full") is True, saved)
    check("F3 没有残留 .tmp 文件", not os.path.exists(path + ".tmp"))
    pause.set_paused("full", False)

    # 模拟断电留下的半截文件：load_state 不应崩溃
    with open(path, "w", encoding="utf-8") as f:
        f.write('{"global": tru')
    pause.load_state()
    check("F4 损坏的暂停文件不会导致启动失败", True)

    # =====================================================================
    print("[G] 回归：全量完成路径")
    # =====================================================================
    slug = "t_ok"
    add_source(slug)
    ok_client = FullClient(pagecount=3, per_page=7)
    syncer.MacCmsClient = client_factory(ok_client)
    res = syncer.sync_source(slug, "full")
    row = src_row(slug)
    check("G1 一次跑完 21 条", row_count(slug) == 21, row_count(slug))
    check("G2 状态 success", res.get("status") == "success", res.get("status"))
    check("G3 断点清零、水位线写入", row["last_page"] == 0 and row["watermark"] == "2026-02-01 00:00:00",
          (row["last_page"], row["watermark"]))
    check("G4 不产生 incomplete 标记", not res.get("incomplete"))

    # =====================================================================
    print("[H] 自动续跑：只挑有断点且未暂停的源站")
    # =====================================================================
    import time as _time

    from app.models import sources

    add_source("t_resume", last_page=3, total_pages=10)
    add_source("t_paused", last_page=5, total_pages=9)
    with database.write_txn() as conn:
        conn.execute(sources.update().where(sources.c.slug == "t_paused").values(paused=1))

    called = []
    real_sync = syncer.sync_source
    syncer.sync_source = lambda slug, mode="auto": called.append((slug, mode)) or {"status": "success"}
    config.AUTO_RESUME_ON_BOOT = True
    config.AUTO_RESUME_DELAY_SECONDS = 0
    try:
        slugs = syncer.auto_resume_incomplete()
        _time.sleep(0.6)
    finally:
        syncer.sync_source = real_sync
        config.AUTO_RESUME_ON_BOOT = False

    check("H1 只挑有断点的源站", "t_resume" in slugs and "t_paused" not in slugs, slugs)
    check("H2 以 full 模式续跑", ("t_resume", "full") in called, called)
    check("H3 已暂停的源站不被自动拉起", all(s != "t_paused" for s, _ in called), called)

    print(f"\n结果: {passed} 通过 / {failed} 失败")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
