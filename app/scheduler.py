"""
定时调度：APScheduler 后台任务。
  - 源站列表刷新（默认每 12 小时）
  - 全源站增量同步（默认每 6 小时）
  - 数据维护：清理同步日志 + WAL checkpoint（默认每小时，见 housekeeping.py）
"""
import logging

from apscheduler.schedulers.background import BackgroundScheduler

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
    from . import related

    if pause.is_paused("incremental"):
        log.info("相关推荐校准已跳过: 增量同步处于暂停状态")
        return
    try:
        related.daily_calibration()
    except Exception:
        log.exception("相关推荐每日校准失败")


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
    明星 Tag 每晚重建（v1.9.1）：小站优先重建 STAR_TAG_NIGHTLY_BUDGET 个源站。
    全量幂等 —— 字段回填补上的真实演员会进入映射；词库新增的名字也会
    重新评估历史未命中标题。全部源站都映射过则跳过（白天同步已增量维护）。
    """
    from sqlalchemy import func, select

    from . import star_tags
    from .models import sources, star_map

    if not config.RELATED_ENABLED:
        return
    try:
        with database.engine.connect() as conn:
            rows = conn.execute(
                select(sources.c.slug, sources.c.total_videos)
                .where(sources.c.enabled == 1, sources.c.total_videos > 0)
                .order_by(sources.c.total_videos)
            ).all()
            mapped = dict(conn.execute(
                select(star_map.c.slug, func.count()).group_by(star_map.c.slug)
            ).all())
        done = []
        for slug, tv in rows:
            if len(done) >= config.STAR_TAG_NIGHTLY_BUDGET:
                break
            if mapped.get(slug, 0) >= tv:      # 已覆盖（映射条数 >= 视频数），跳过
                continue
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
