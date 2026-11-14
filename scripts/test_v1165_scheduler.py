"""临时回归：验证 v1.16.5 计划任务管理接口逻辑（用 dummy scheduler，不跑真实任务）。"""
import os
os.environ["DATABASE_URL"] = "sqlite:///./_test_sched.db"
os.environ.setdefault("LATEST_JSON_URL", "https://example.com/x.json")

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger

import app.scheduler as sch
import app.config as config

# 用一个只含 dummy job 的调度器替换模块内全局，避免触发真实同步任务
fake = BackgroundScheduler()
fake.add_job(lambda: None, "interval", minutes=360, id="sync_all", max_instances=1, coalesce=True)
fake.add_job(lambda: None, "cron", hour=4, minute=30, id="related_calibration", max_instances=1, coalesce=True)
fake.start()
sch._scheduler = fake

jobs = {j["id"]: j for j in sch.list_jobs()}
print("jobs:", [(j["id"], j["name"], j["trigger"], j["paused"]) for j in jobs.values()])
assert "sync_all" in jobs and "related_calibration" in jobs
assert jobs["sync_all"]["name"] == "全站增量同步", jobs["sync_all"]["name"]
assert "每" in jobs["sync_all"]["trigger"], jobs["sync_all"]["trigger"]
assert jobs["related_calibration"]["trigger"] == "每天 04:30", jobs["related_calibration"]["trigger"]

# 暂停 / 恢复
sch.pause_job("sync_all")
assert next(j for j in sch.list_jobs() if j["id"] == "sync_all")["paused"] is True
sch.resume_job("sync_all")
assert next(j for j in sch.list_jobs() if j["id"] == "sync_all")["paused"] is False

# 改 interval
sch.reschedule_job("sync_all", interval_minutes=120)
j = next(j for j in sch.list_jobs() if j["id"] == "sync_all")
assert j["trigger"] == "每 2 小时", j["trigger"]
assert config.SYNC_INTERVAL_MINUTES == 120, config.SYNC_INTERVAL_MINUTES

# 改 cron
sch.reschedule_job("related_calibration", hour=5, minute=15)
j = next(j for j in sch.list_jobs() if j["id"] == "related_calibration")
assert j["trigger"] == "每天 05:15", j["trigger"]
assert config.RELATED_CALIBRATION_HOUR == 5 and config.RELATED_CALIBRATION_MINUTE == 15

# trigger_job 不抛错（仅入队）
sch.trigger_job("sync_all")

# 不存在的 job -> KeyError
try:
    sch.pause_job("nope")
    raise SystemExit("应当 KeyError")
except KeyError:
    print("不存在 job 校验 OK")

fake.shutdown(wait=False)
print("\nALL_V1165_SCHEDULER_TESTS_PASS")
try:
    os.remove("./_test_sched.db")  # 清理临时 SQLite（该用例实际未建库，忽略缺失）
except OSError:
    pass
