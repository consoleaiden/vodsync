"""
v1.8.1 源站健康度退避 + 增量索引页级重试 测试。

覆盖项：
 A. 迁移：sources 新增 fail_streak / skip_until（老库自动补列，默认 0 / NULL）
 B. M1 页级重试：瞬时抖动的失败页在本次运行内重试成功 → 索引完整、水位线正常推进
 C. M2 早停：连续 2 批整批失败 → 剩余页跳过（请求次数远小于总页数）
 D. M3/M4 健康度：
    D1 大面积失败（失败率>=50%）→ streak=1，skip_until 写入，status 仍 ok
    D2 再失败一次 → streak=2，status=down，退避时长按指数增长
    D3 成功 → streak 清零、skip_until 清空、status 恢复 ok
    D4 整轮异常（failed）→ 也计入 streak
    D5 失败率 < 50% 的 incomplete → 不计 streak（瞬时抖动不该触发退避）
 E. 调度：sync_all 跳过 skip_until 未到的站；到期的站正常参与
 F. M5 错误聚合：日志行带「http_403×N, timeout×M」原因分布
 G. 水位线安全边界回归：索引失败时水位线仍冻结（不因重试逻辑改变语义）

运行：python scripts/test_v181_backoff.py
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="vodsync-backoff-")
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(os.environ["DATA_DIR"], "bo.db")
os.environ["AUTH_ENABLED"] = "false"
os.environ["FULL_SYNC_ON_DISCOVER"] = "false"
os.environ["PAGE_CONCURRENCY"] = "4"
os.environ["PAGE_MAX_ATTEMPTS"] = "3"
os.environ["PAGE_RETRY_SLEEP_SECONDS"] = "0"   # 测试不等 2 秒
os.environ["BACKOFF_BASE_MINUTES"] = "60"
os.environ["AUTO_RESUME_ON_BOOT"] = "false"

from sqlalchemy import select  # noqa: E402

from app.maccms import MacCmsClient as _RealClient  # noqa: E402
from app.maccms import MacCmsError  # noqa: E402


def client_factory(fake):
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
class IncrClient:
    """
    增量：pagecount 页 list，每页 per_page 条。
    fail_pages: 恒定失败的页
    flaky_pages: 只在「该页第一次被请求」时失败（模拟瞬时抖动，重试即恢复）
    detail_ids: 详情接口返回哪些 ID（空 set = 详情全部返回）
    """

    def __init__(self, pagecount, per_page=10, fail_pages=(), flaky_pages=(),
                 detail_ids=None, watermark_time="2026-02-01 00:00:00"):
        self.pagecount = pagecount
        self.per_page = per_page
        self.fail_pages = set(fail_pages)
        self.flaky_pages = set(flaky_pages)
        self._flaky_done = set()
        self.detail_ids = set() if detail_ids is None else set(detail_ids)
        self.wm_time = watermark_time
        self.calls = []

    def fetch_page(self, api, ac, pg):
        self.calls.append(pg)
        if pg in self.fail_pages:
            raise MacCmsError(f"模拟 list 第 {pg} 页失败: 403 Forbidden")
        if pg in self.flaky_pages and pg not in self._flaky_done:
            self._flaky_done.add(pg)
            raise MacCmsError(f"模拟 list 第 {pg} 页失败: request timed out")
        chunk = [
            {"vod_id": pg * 1000 + i, "vod_time": self.wm_time,
             "vod_name": f"p{pg}-{i}", "type_id": 1}
            for i in range(self.per_page)
        ]
        return {"code": 1, "page": pg, "pagecount": self.pagecount,
                "total": self.pagecount * self.per_page, "class": [], "list": chunk}

    def fetch_detail_by_ids(self, api, ids):
        rows = []
        for it_ids in [set(ids)]:
            for vid in sorted(it_ids):
                if vid in self.detail_ids or not self.detail_ids:
                    rows.append({"vod_id": vid, "vod_time": self.wm_time,
                                 "vod_name": f"v{vid}", "type_id": 1,
                                 "vod_play_url": f"http://x/{vid}.m3u8"})
        return {"code": 1, "list": rows}

    def close(self):
        pass


class DeadFirstClient:
    """首页就挂 → 整轮异常（failed 路径）。"""

    def __init__(self):
        self.calls = []

    def fetch_page(self, api, ac, pg):
        self.calls.append(pg)
        raise MacCmsError("connect error: connection refused")

    def close(self):
        pass


def add_source(slug, watermark=""):
    from app import database
    from app.models import sources
    from app.syncer import ensure_table

    with database.write_txn() as conn:
        conn.execute(sources.insert().values(
            name=slug, slug=slug, api_url=f"http://{slug}.test/api.php",
            status="ok", enabled=1, auto_sync=1, watermark=watermark,
        ))
    ensure_table(slug)


def src_row(slug):
    from app import database
    from app.models import sources

    with database.engine.connect() as conn:
        return conn.execute(select(sources).where(sources.c.slug == slug)).mappings().first()


def main():
    from app import config, database, syncer

    database.init_db()

    # =====================================================================
    print("[A] 迁移：fail_streak / skip_until 列存在且默认值正确")
    # =====================================================================
    add_source("t_mig")
    row = src_row("t_mig")
    check("fail_streak 默认 0", (row["fail_streak"] or 0) == 0, row["fail_streak"])
    check("skip_until 默认 NULL", row["skip_until"] is None, row["skip_until"])

    # =====================================================================
    print("[B] M1 页级重试：瞬时抖动当场恢复，水位线正常推进")
    # =====================================================================
    slug = "t_flaky"
    add_source(slug, watermark="2026-01-01 00:00:00")
    # 水位线 1/1，条目时间 2/1 → 全部是候选；第 2 页第一次请求失败
    client = IncrClient(pagecount=10, flaky_pages={2})
    syncer.MacCmsClient = client_factory(client)
    res = syncer.sync_source(slug, "incremental")
    check("结果 success（无 incomplete）", res["status"] == "success", res.get("status"))
    check("第 2 页被重试过", client.calls.count(2) == 2, sorted(client.calls))
    check("索引完整（100 条全部入索引）", res.get("fetched") == 100, res.get("fetched"))
    check("水位线已推进", src_row(slug)["watermark"] == "2026-02-01 00:00:00",
          src_row(slug)["watermark"])
    check("健康度未受影响", (src_row(slug)["fail_streak"] or 0) == 0)

    # =====================================================================
    print("[C] M2 早停：连续 2 批整批失败 → 剩余页跳过")
    # =====================================================================
    slug = "t_early"
    add_source(slug, watermark="2026-01-01 00:00:00")
    client = IncrClient(pagecount=100, fail_pages=set(range(2, 101)))  # 第 1 页成功，其余全挂
    syncer.MacCmsClient = client_factory(client)
    res = syncer.sync_source(slug, "incremental")
    check("结果 incomplete", res["status"] == "incomplete", res.get("status"))
    # 100 页、并发 4：首页 1 次 + 第 2~9 页（2 批）各 1 次 → 早停后重试轮同理
    check(f"请求次数远小于总页数（实际 {len(client.calls)} 次）",
          len(client.calls) <= 40, sorted(client.calls)[:20])
    check("日志行带失败统计", "个失败" in (res.get("error") or ""), res.get("error"))
    row = src_row(slug)
    check("水位线保持不变（安全边界）", row["watermark"] == "2026-01-01 00:00:00",
          row["watermark"])

    # =====================================================================
    print("[D] 健康度退避")
    # =====================================================================
    print("[D1] 首次大面积失败 → streak=1，退避写入，status 仍 ok")
    row = src_row("t_early")
    check("fail_streak == 1", (row["fail_streak"] or 0) == 1, row["fail_streak"])
    check("skip_until 已写入（约 60 分钟后）",
          row["skip_until"] is not None
          and 55 <= (row["skip_until"] - datetime.now()).total_seconds() / 60 <= 65,
          row["skip_until"])
    check("status 仍为 ok（未达 down 阈值）", row["status"] == "ok", row["status"])

    print("[D2] 再失败一次 → streak=2，status=down，退避翻倍")
    res = syncer.sync_source("t_early", "incremental")   # 手动触发，不受 skip_until 限制
    row = src_row("t_early")
    check("fail_streak == 2", (row["fail_streak"] or 0) == 2, row["fail_streak"])
    check("status == down", row["status"] == "down", row["status"])
    check("退避时长翻倍（约 120 分钟）",
          110 <= (row["skip_until"] - datetime.now()).total_seconds() / 60 <= 130,
          row["skip_until"])

    print("[D3] 成功 → 清零恢复")
    ok_client = IncrClient(pagecount=10)   # 全部成功
    syncer.MacCmsClient = client_factory(ok_client)
    res = syncer.sync_source("t_early", "incremental")
    row = src_row("t_early")
    check("结果 success", res["status"] == "success", res.get("status"))
    check("fail_streak 清零", (row["fail_streak"] or 0) == 0, row["fail_streak"])
    check("skip_until 清空", row["skip_until"] is None, row["skip_until"])
    check("status 恢复 ok", row["status"] == "ok", row["status"])

    print("[D4] 整轮异常（failed）也计入 streak")
    add_source("t_dead", watermark="2026-01-01 00:00:00")
    syncer.MacCmsClient = client_factory(DeadFirstClient())
    res = syncer.sync_source("t_dead", "incremental")
    row = src_row("t_dead")
    check("结果 failed", res["status"] == "failed", res.get("status"))
    check("fail_streak == 1", (row["fail_streak"] or 0) == 1, row["fail_streak"])
    check("skip_until 已写入", row["skip_until"] is not None)

    print("[D5] 失败率 < 50% 的 incomplete 不触发退避")
    slug = "t_minor"
    add_source(slug, watermark="2026-01-01 00:00:00")
    # 100 页只有第 50~56 页挂（7%）→ 早停条件是「连续 2 批」，会波及 2~9 页吗？
    # fail_pages 从 50 开始：批 [2..5] ok、…、批 [50..53] 挂（empty=1）、批 [54..57] 挂
    # （empty=2）→ 触发早停。剩余页计入失败后失败率可能 >= 50%…… 改为 98 页里挂 3 页
    # 且分散：50、60、70 → 不会出现连续 2 批整批失败。
    client = IncrClient(pagecount=100, fail_pages={50, 60, 70})
    syncer.MacCmsClient = client_factory(client)
    res = syncer.sync_source(slug, "incremental")
    row = src_row(slug)
    check("结果 incomplete", res["status"] == "incomplete", res.get("status"))
    check("失败率 3%（远小于 50%）", res.get("failed_list_pages") == 3,
          res.get("failed_list_pages"))
    check("fail_streak 仍为 0", (row["fail_streak"] or 0) == 0, row["fail_streak"])
    check("skip_until 仍为空", row["skip_until"] is None, row["skip_until"])

    # =====================================================================
    print("[E] sync_all 跳过退避中的源站")
    # =====================================================================
    from app.models import sources as sources_t

    called = []
    _orig_sync_source = syncer.sync_source

    def spy(slug, mode="auto"):
        called.append(slug)
        return {"slug": slug, "status": "success"}

    syncer.sync_source = spy
    try:
        # t_dead 的 skip_until 在未来（D4 刚写入）；t_mig 无退避
        results = syncer.sync_all("auto")
        check("退避中的站被跳过", "t_dead" not in called, called)
        check("到期/无退避的站正常参与", "t_mig" in called, called)
        # 退避到期 → 恢复参与
        with database.write_txn() as conn:
            conn.execute(sources_t.update().where(sources_t.c.slug == "t_dead")
                         .values(skip_until=datetime.now() - timedelta(minutes=1)))
        called.clear()
        syncer.sync_all("auto")
        check("退避到期后恢复参与", "t_dead" in called, called)
    finally:
        syncer.sync_source = _orig_sync_source

    # =====================================================================
    print("[F] 错误聚合：日志行带原因分布")
    # =====================================================================
    res_err = res  # t_minor 的结果：3 页失败（403 消息）
    check("日志行包含 http_403×3", "http_403×3" in (res_err.get("error") or ""),
          res_err.get("error"))
    # 混合错误类型
    slug = "t_mixed"
    add_source(slug, watermark="2026-01-01 00:00:00")

    class MixedClient(IncrClient):
        def fetch_page(self, api, ac, pg):
            if pg in self.fail_pages:
                raise MacCmsError(
                    "timed out" if pg % 2 == 0 else "403 Forbidden"
                )
            return super().fetch_page(api, ac, pg)

    client = MixedClient(pagecount=100, fail_pages=set(range(50, 100)))
    syncer.MacCmsClient = client_factory(client)
    res2 = syncer.sync_source(slug, "incremental")
    err = res2.get("error") or ""
    check("timeout 计数正确", "timeout×" in err, err)
    check("http_403 计数正确", "http_403×" in err, err)
    row = src_row(slug)
    check("混合失败计入 streak", (row["fail_streak"] or 0) == 1, row["fail_streak"])

    # =====================================================================
    print("[G] 水位线安全边界回归：索引不完整 → 冻结（语义不变）")
    # =====================================================================
    row = src_row("t_mixed")
    # t_mixed 失败率 50%（索引不完整）→ 水位线不得推进
    check("t_mixed 水位线冻结在 1/1", row["watermark"] == "2026-01-01 00:00:00",
          row["watermark"])

    print(f"\n结果: 通过 {passed} / {passed + failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
