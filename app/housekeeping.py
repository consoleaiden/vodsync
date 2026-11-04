"""
数据维护（定时跑，默认每小时一次）：

1. `prune_sync_logs()` —— 清理同步日志
   sync_logs 每个源站每次同步写一行。52 个源站、增量每 6 小时一次、加上手动触发，
   一年就是一万多行；面板只读最近几十条，历史全留着只会让 `/api/v1/logs` 越来越慢。
   三级策略（都是「只删已结束的行」，`running` 永不删）：
     ① 超过 SYNC_LOG_RETENTION_DAYS 天的
     ② 每个源站超过 SYNC_LOG_KEEP_PER_SOURCE 条的（兜底，防止高频同步堆太多）
     ③ 全表超过 SYNC_LOG_MAX_ROWS 条的（兜底，防止策略失效）

2. `checkpoint_wal()` —— SQLite 的 WAL 回收
   SQLite 默认每 1000 页自动 checkpoint，正常不会涨；但**长读事务**（比如一条跑十几秒
   的大查询）会把 checkpoint 顶回去，WAL 文件于是持续变大，直到把 NAS 磁盘吃满。
   这里定期做一次 PASSIVE checkpoint，超过阈值再做 TRUNCATE，并把 WAL 大小写进日志。
   PostgreSQL 下是 no-op（PG 有自己的 WAL 归档/清理机制，不需要应用层干预）。
"""
import logging
import os
from datetime import datetime, timedelta

from sqlalchemy import and_, delete, func, select, text

from . import config, database
from .models import sync_logs

log = logging.getLogger("vodsync.housekeeping")

_RUNNING = "running"


def _db_file() -> str:
    """当前 SQLite 数据库文件路径（PG 返回空串）。"""
    if not database.is_sqlite():
        return ""
    return (database.engine.url.database or "") if hasattr(database.engine.url, "database") else ""


def prune_sync_logs(
    retention_days: int = None,
    keep_per_source: int = None,
    max_rows: int = None,
) -> dict:
    """按三级策略清理 sync_logs，返回各级删除行数。"""
    retention_days = config.SYNC_LOG_RETENTION_DAYS if retention_days is None else retention_days
    keep_per_source = config.SYNC_LOG_KEEP_PER_SOURCE if keep_per_source is None else keep_per_source
    max_rows = config.SYNC_LOG_MAX_ROWS if max_rows is None else max_rows

    cutoff = datetime.now() - timedelta(days=max(1, retention_days))
    out = {"expired": 0, "per_source": 0, "over_limit": 0, "deleted": 0}

    with database.write_txn() as conn:
        # ① 超期
        res = conn.execute(
            delete(sync_logs).where(
                and_(sync_logs.c.status != _RUNNING, sync_logs.c.started_at < cutoff)
            )
        )
        out["expired"] = max(0, res.rowcount or 0)

        # ② 每源站只留最近 keep_per_source 条
        #    用窗口函数按 slug 分组编号，比「相关子查询 + LIMIT」快得多（后者会退化成 N×N）
        rn = func.row_number().over(
            partition_by=sync_logs.c.slug, order_by=sync_logs.c.id.desc()
        ).label("rn")
        ranked = (
            select(sync_logs.c.id, rn)
            .where(sync_logs.c.status != _RUNNING)
            .subquery()
        )
        res = conn.execute(
            delete(sync_logs).where(
                sync_logs.c.id.in_(select(ranked.c.id).where(ranked.c.rn > keep_per_source))
            )
        )
        out["per_source"] = max(0, res.rowcount or 0)

        # ③ 全表硬上限：把「第 max_rows 新的那条」之前的全部删掉
        total = conn.execute(select(func.count()).select_from(sync_logs)).scalar() or 0
        if max_rows > 0 and total > max_rows:
            edge = conn.execute(
                select(sync_logs.c.id).order_by(sync_logs.c.id.desc()).offset(max_rows - 1).limit(1)
            ).scalar()
            if edge is not None:
                res = conn.execute(
                    delete(sync_logs).where(
                        and_(sync_logs.c.id < edge, sync_logs.c.status != _RUNNING)
                    )
                )
                out["over_limit"] = max(0, res.rowcount or 0)

    out["deleted"] = out["expired"] + out["per_source"] + out["over_limit"]
    if out["deleted"]:
        log.info(
            "清理同步日志 %d 条（超期 %d / 超每源站上限 %d / 超总量上限 %d），"
            "保留策略：%d 天、每源站 %d 条、全表 %d 条",
            out["deleted"], out["expired"], out["per_source"], out["over_limit"],
            retention_days, keep_per_source, max_rows,
        )
    return out


def checkpoint_wal() -> dict:
    """SQLite: 回收 WAL 文件；PostgreSQL: 直接跳过。"""
    if not database.is_sqlite():
        return {"engine": "postgres", "skipped": True}

    db_file = _db_file()
    wal_file = f"{db_file}-wal"

    def _size_mb(path: str) -> float:
        try:
            return round(os.path.getsize(path) / 1048576, 2)
        except OSError:
            return 0.0

    before = _size_mb(wal_file)
    # 超过阈值才用 TRUNCATE（会把 WAL 文件截断到 0）；平时 PASSIVE 足够，
    # 且 PASSIVE 不会阻塞正在读的请求 —— 用 TRUNCATE 太频繁反而会打断并发读。
    mode = "TRUNCATE" if before >= config.WAL_CHECKPOINT_MB else "PASSIVE"
    try:
        with database.engine.connect() as conn:
            row = conn.execute(text(f"PRAGMA wal_checkpoint({mode})")).first()
        # 返回 (busy, log_pages, checkpointed_pages)：busy=1 表示有读者挡住了部分页
        busy = int(row[0]) if row else 0
    except Exception as e:  # noqa: BLE001 —— 维护任务失败不该影响服务
        log.warning("WAL checkpoint 失败: %s", e)
        return {"engine": "sqlite", "mode": mode, "error": str(e), "wal_mb": _size_mb(wal_file)}

    after = _size_mb(wal_file)
    result = {"engine": "sqlite", "mode": mode, "busy": busy, "before_mb": before, "wal_mb": after}
    if busy:
        log.info("WAL checkpoint(%s) 部分完成：有读事务占用，WAL %.1fMB → %.1fMB", mode, before, after)
    elif before >= config.WAL_CHECKPOINT_MB:
        log.info("WAL checkpoint(%s)：%.1fMB → %.1fMB", mode, before, after)
    return result


def run_all() -> dict:
    """维护任务总入口（调度器 / 启动引导调用）。"""
    started = datetime.now()
    result = {"logs": None, "wal": None}
    try:
        result["logs"] = prune_sync_logs()
    except Exception:
        log.exception("清理同步日志失败（下个周期重试）")
    try:
        result["wal"] = checkpoint_wal()
    except Exception:
        log.exception("WAL checkpoint 失败（下个周期重试）")
    result["elapsed_ms"] = int((datetime.now() - started).total_seconds() * 1000)
    return result
