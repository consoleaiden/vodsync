"""
定时调度：APScheduler 后台任务。
  - 源站列表刷新（默认每 12 小时）
  - 全源站增量同步（默认每 6 小时）
  - 数据维护：清理同步日志 + WAL checkpoint（默认每小时，见 housekeeping.py）
"""
import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from . import config, database, pause

log = logging.getLogger("vodsync.scheduler")
_scheduler: BackgroundScheduler = None


def _job_refresh_sources():
    from . import syncer

    if pause.is_paused("full"):
        log.info("源站刷新任务: 全量同步处于暂停状态，跳过新站全量同步")
    try:
        new_slugs = syncer.refresh_sources()
        if new_slugs and config.FULL_SYNC_ON_DISCOVER and not pause.is_paused("full"):
            for slug in new_slugs:
                syncer.sync_source(slug, mode="full")
    except Exception:
        log.exception("源站列表刷新任务失败")


def _job_sync_all():
    from . import syncer

    # 定时增量同步在暂停期间直接跳过（恢复后到下一个调度周期自动执行）
    if pause.is_paused("incremental"):
        log.info("定时增量同步已跳过: 增量同步处于暂停状态")
        return
    try:
        syncer.sync_all()
    except Exception:
        log.exception("定时同步任务失败")


def _job_housekeeping():
    from . import housekeeping

    try:
        housekeeping.run_all()
    except Exception:
        log.exception("数据维护任务失败")


def _job_related_calibration():
    """相关推荐每日校准：重算 computed_at 过期 / 覆盖率不满的源站（预算内）。"""
    from . import related, syncer

    if pause.is_paused("incremental"):
        log.info("相关推荐校准已跳过: 增量同步处于暂停状态")
        return
    try:
        related.daily_calibration()
    except Exception:
        log.exception("相关推荐每日校准失败")
    try:
        # 精确条数校准（v1.10.0 / P9）：白天 /videos 的 total 用 SUM(total_videos)
        # 估值（快），每晚把真值重算一遍（52 次 count(*)，凌晨不打扰用户）。
        syncer.refresh_counts()
    except Exception:
        log.exception("条数校准失败（估值继续沿用，不影响功能）")


def _job_field_backfill():
    """
    存量字段回填（v1.9.0）：升级前已同步的库，演员/地区/年代列是空的。
    每晚凌晨对「还有字段缺失」的源站按小站优先重跑全量（UPSERT 幂等，只补字段）。
    """
    from sqlalchemy import func, select

    from . import syncer
    from .models import build_vod_table, sources

    slugs_done = []
    with database.engine.connect() as conn:
        rows = conn.execute(
            select(sources.c.slug, sources.c.total_videos)
            .where(sources.c.enabled == 1, sources.c.total_videos > 0)
            .order_by(sources.c.total_videos)          # 小站优先，快速见成效
        ).all()
    for slug, _tv in rows:
        if len(slugs_done) >= config.FIELD_BACKFILL_SOURCES_PER_NIGHT:
            break
        table = build_vod_table(slug)
        if not database.table_exists(table.name):
            continue
        try:
            with database.engine.connect() as conn:
                missing = conn.execute(
                    select(func.count())
                    .select_from(table)
                    .where(table.c.vod_year == "")     # 有年代数据的行 = 已被回填过
                ).scalar() or 0
        except Exception:
            continue
        if missing == 0:
            continue
        log.info("[字段回填] %s 仍有 %d 行缺少推荐信号字段，开始全量回填", slug, missing)
        slugs_done.append(slug)
        try:
            syncer.sync_source(slug, mode="full")
        except Exception:
            log.exception("[字段回填] %s 全量回填失败（明晚继续）", slug)
    if slugs_done:
        log.info("[字段回填] 今晚完成: %s", ",".join(slugs_done))


def _job_star_tags_nightly():
    """
    明星 Tag 每晚重建（v1.9.1 / v1.10.0 修 C1）：每次重建 STAR_TAG_NIGHTLY_BUDGET 个源站。

    选站策略（v1.10.0 换了判据）：旧实现用 `mapped >= tv` 判断「已覆盖」，
    而 mapped 是 star_map 的**行数**、一部片常映射 2~5 个明星 → mapped 天然 ≥ tv
    → 夜间重建几乎永不执行。现在改为「按上次重建时间轮转」：
      从未映射过的站优先 → 其次最久没重建的站 → 同条件小站优先。
    这样每个源站都会被轮到，而不是永远捶同一批。
    """
    from datetime import datetime, timedelta

    from sqlalchemy import func, select

    from . import star_tags
    from .models import sources, star_rebuild_state

    if not config.RELATED_ENABLED:
        return
    try:
        with database.engine.connect() as conn:
            rows = conn.execute(
                select(sources.c.slug, sources.c.total_videos)
                .where(sources.c.enabled == 1, sources.c.total_videos > 0)
            ).all()
            # v1.16.2 / R1：只认「全量重建水位」。
            # 旧实现取 max(star_map.computed_at) 当上次重建时间，但增量同步的
            # update_ids() 也写 star_map 并刷新 computed_at —— 每 6h 有增量的
            # 大站看起来永远「刚重建过」，全量重建永远轮不到它（线上乐播
            # 33.8 万行只有 383 条映射，而真跑过全量重建的站是 10 万条量级）。
            # star_rebuild_state 只有 rebuild_source() 跑完才写，增量绝不碰。
            full_at = {
                s: last for s, last in conn.execute(
                    select(star_rebuild_state.c.slug,
                           star_rebuild_state.c.last_full_at)
                ).all()
            }
        now = datetime.now()
        min_interval = timedelta(hours=max(0, config.STAR_TAG_NIGHTLY_MIN_INTERVAL_HOURS))
        candidates = []
        for slug, tv in rows:
            last = full_at.get(slug)
            if last is not None and now - last < min_interval:
                continue
            # (从未全量重建=0, 上次全量重建时间, 视频数) 升序
            candidates.append(((0 if last is None else 1,
                                last or datetime.min, tv or 0), slug))
        candidates.sort(key=lambda x: x[0])
        done = []
        for _key, slug in candidates[: config.STAR_TAG_NIGHTLY_BUDGET]:
            star_tags.rebuild_source_safe(slug, reason="nightly")
            done.append(slug)
        if done:
            log.info("[明星Tag] 每晚重建完成: %s", ",".join(done))
    except Exception:
        log.exception("[明星Tag] 每晚重建失败")


def start_scheduler():
    global _scheduler
    if _scheduler is not None:
        return _scheduler
    _scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
    _scheduler.add_job(
        _job_refresh_sources,
        "interval",
        minutes=config.SOURCE_REFRESH_INTERVAL_MINUTES,
        id="refresh_sources",
        max_instances=1,
        coalesce=True,
    )
    _scheduler.add_job(
        _job_sync_all,
        "interval",
        minutes=config.SYNC_INTERVAL_MINUTES,
        id="sync_all",
        max_instances=1,
        coalesce=True,
    )
    if config.HOUSEKEEPING_ENABLED:
        _scheduler.add_job(
            _job_housekeeping,
            "interval",
            minutes=config.HOUSEKEEPING_INTERVAL_MINUTES,
            id="housekeeping",
            max_instances=1,
            coalesce=True,
        )
    if config.RELATED_ENABLED:
        _scheduler.add_job(
            _job_related_calibration,
            "cron",
            hour=max(0, min(23, config.RELATED_CALIBRATION_HOUR)),
            minute=max(0, min(59, config.RELATED_CALIBRATION_MINUTE)),
            id="related_calibration",
            max_instances=1,
            coalesce=True,
        )
    if config.RELATED_ENABLED and config.FIELD_BACKFILL_ENABLED:
        _scheduler.add_job(
            _job_field_backfill,
            "cron",
            hour=max(0, min(23, config.FIELD_BACKFILL_START_HOUR)),
            minute=0,
            id="field_backfill",
            max_instances=1,
            coalesce=True,
        )
    if config.RELATED_ENABLED:
        _scheduler.add_job(
            _job_star_tags_nightly,
            "cron",
            hour=max(0, min(23, config.STAR_TAG_NIGHTLY_HOUR)),
            minute=max(0, min(59, config.STAR_TAG_NIGHTLY_MINUTE)),
            id="star_tags_nightly",
            max_instances=1,
            coalesce=True,
        )
    _scheduler.start()
    log.info(
        "调度器已启动: 源站刷新每 %d 分钟, 增量同步每 %d 分钟%s%s",
        config.SOURCE_REFRESH_INTERVAL_MINUTES,
        config.SYNC_INTERVAL_MINUTES,
        f", 数据维护每 {config.HOUSEKEEPING_INTERVAL_MINUTES} 分钟"
        if config.HOUSEKEEPING_ENABLED else "",
        f", 相关推荐校准每天 {config.RELATED_CALIBRATION_HOUR:02d}:{config.RELATED_CALIBRATION_MINUTE:02d}"
        f", 字段回填每天 {config.FIELD_BACKFILL_START_HOUR:02d}:00"
        if config.RELATED_ENABLED and config.FIELD_BACKFILL_ENABLED else "",
    )
    return _scheduler


def stop_scheduler():
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None


# ---------------------------------------------------------------------------
# 调度器管理接口（v1.16.5 / 管理面板「计划任务」模块）
#
# 直接复用进程内已运行的 APScheduler（_scheduler），不改任何业务定时逻辑。
# 提供：列表（含下一次执行时间 / 暂停态）、暂停 / 恢复 / 立即执行 / 改周期。
# 注意：运行时改周期只改内存里的 job，重启后由 config 重新装配（这是预期行为，
# 避免把调度配置悄悄写进环境变量）。
# ---------------------------------------------------------------------------

# 任务 id -> 面板展示用的中文名（APScheduler 默认只给函数名，不够直观）
JOB_LABELS = {
    "refresh_sources": "源站列表刷新",
    "sync_all": "全站增量同步",
    "housekeeping": "数据维护",
    "related_calibration": "相关推荐校准",
    "field_backfill": "字段回填",
    "star_tags_nightly": "明星 Tag 重建",
}


def _trigger_text(job) -> str:
    """把 APScheduler 的 trigger 翻成人能读懂的中文周期描述。"""
    t = job.trigger
    try:
        if isinstance(t, IntervalTrigger):
            secs = int(t.interval.total_seconds())
            if secs and secs % 3600 == 0:
                return f"每 {secs // 3600} 小时"
            if secs and secs % 60 == 0:
                return f"每 {secs // 60} 分钟"
            return f"每 {secs} 秒"
        if isinstance(t, CronTrigger):
            s = str(t)  # 形如 cron[hour='4', minute='30', second='0']
            inner = s[s.find("[") + 1: s.rfind("]")]
            kv = {}
            for part in inner.split(","):
                if "=" in part:
                    k, v = part.split("=", 1)
                    kv[k.strip()] = v.strip().strip("'")
            if "hour" in kv and "minute" in kv and "day_of_week" not in kv:
                return f"每天 {kv['hour'].zfill(2)}:{kv['minute'].zfill(2)}"
            return s
        return str(t)
    except Exception:
        return str(t)


def _job_or_raise(job_id: str):
    if _scheduler is None:
        raise KeyError("调度器未启动")
    job = _scheduler.get_job(job_id)
    if job is None:
        raise KeyError(job_id)
    return job


def list_jobs() -> list:
    """返回所有定时任务的摘要：id / 名称 / 周期 / 下次执行 / 是否暂停。"""
    if _scheduler is None:
        return []
    out = []
    for j in _scheduler.get_jobs():
        next_run = j.next_run_time.isoformat() if j.next_run_time else None
        out.append({
            "id": j.id,
            "name": JOB_LABELS.get(j.id, j.name or j.id),
            "trigger": _trigger_text(j),
            "trigger_type": type(j.trigger).__name__,
            "next_run_time": next_run,
            "paused": next_run is None,
            "pending": next_run is not None,
        })
    return out


def pause_job(job_id: str) -> bool:
    _job_or_raise(job_id).pause()
    return True


def resume_job(job_id: str) -> bool:
    _job_or_raise(job_id).resume()
    return True


def trigger_job(job_id: str) -> bool:
    """
    立即跑一次。APScheduler 3.x 没有 scheduler.trigger_job（那是 4.x 的 API），
    这里直接取 job 的 func 丢到后台线程里跑 —— 与正常定时触发走的是同一函数，
    只是绕过了触发器。job 函数内部自带 try/except 与暂停判断，手动触发同样遵守
    「全局/增量暂停时跳过」的语义（用户点「立即执行」若恰在暂停期则不实际跑）。
    """
    job = _job_or_raise(job_id)
    import threading

    threading.Thread(
        target=job.func,
        args=tuple(getattr(job, "args", ()) or ()),
        kwargs=dict(getattr(job, "kwargs", {}) or {}),
        daemon=True,
        name=f"manual-{job_id}",
    ).start()
    return True


def reschedule_job(job_id: str, interval_minutes: int = None,
                   hour: int = None, minute: int = None) -> bool:
    """
    改周期：
      - interval 型：传 interval_minutes（同时写回 config 内存值，重启前的口径一致）
      - cron 型：传 hour / minute
    改完内存里的 job，下次启动由 config 重新装配。
    """
    job = _job_or_raise(job_id)
    if isinstance(job.trigger, IntervalTrigger):
        if not interval_minutes or interval_minutes < 1:
            raise ValueError("interval_minutes 必须 >= 1")
        job.reschedule("interval", minutes=int(interval_minutes))
        # 同步内存里的 config（只影响本次进程，重启后回到环境变量值）
        _sync_interval_config(job_id, int(interval_minutes))
    elif isinstance(job.trigger, CronTrigger):
        if hour is None or minute is None:
            raise ValueError("cron 任务需要 hour / minute")
        h = max(0, min(23, int(hour)))
        m = max(0, min(59, int(minute)))
        job.reschedule("cron", hour=h, minute=m)
        _sync_cron_config(job_id, h, m)
    else:
        raise ValueError("不支持的触发器类型，无法改周期")
    return True


def _sync_interval_config(job_id: str, minutes: int):
    if job_id == "refresh_sources":
        config.SOURCE_REFRESH_INTERVAL_MINUTES = minutes
    elif job_id == "sync_all":
        config.SYNC_INTERVAL_MINUTES = minutes
    elif job_id == "housekeeping":
        config.HOUSEKEEPING_INTERVAL_MINUTES = minutes


def _sync_cron_config(job_id: str, hour: int, minute: int):
    if job_id == "related_calibration":
        config.RELATED_CALIBRATION_HOUR = hour
        config.RELATED_CALIBRATION_MINUTE = minute
    elif job_id == "field_backfill":
        config.FIELD_BACKFILL_START_HOUR = hour
    elif job_id == "star_tags_nightly":
        config.STAR_TAG_NIGHTLY_HOUR = hour
        config.STAR_TAG_NIGHTLY_MINUTE = minute
