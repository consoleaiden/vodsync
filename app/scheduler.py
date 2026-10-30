"""
定时调度：APScheduler 后台任务。
  - 源站列表刷新（默认每 12 小时）
  - 全源站增量同步（默认每 6 小时）
"""
import logging

from apscheduler.schedulers.background import BackgroundScheduler

from . import config, pause

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
    _scheduler.start()
    log.info(
        "调度器已启动: 源站刷新每 %d 分钟, 增量同步每 %d 分钟",
        config.SOURCE_REFRESH_INTERVAL_MINUTES,
        config.SYNC_INTERVAL_MINUTES,
    )
    return _scheduler


def stop_scheduler():
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
