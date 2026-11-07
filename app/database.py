"""
数据库引擎与元数据管理。

- **生产统一 PostgreSQL**（v1.7.0 起）；SQLite 分支仅保留给自动化测试用
- 使用 SQLAlchemy Core（性能优于 ORM，适合百万级行批量写入）
- 每个源站一张 vod_{slug} 动态表，见 models.build_vod_table()
"""
import contextlib
import logging
import threading

from sqlalchemy import create_engine, event, text

from . import config


def dialect_insert(table):
    """
    返回带 ON CONFLICT 能力的方言 INSERT 构造器。

    PostgreSQL 的 `ON CONFLICT DO UPDATE` 与 SQLite 的同名语法签名一致，
    所以业务代码（syncer.upsert_batch）不需要关心底层是哪个数据库。
    """
    if _is_sqlite:  # 仅测试后端
        from sqlalchemy.dialects.sqlite import insert

        return insert(table)
    from sqlalchemy.dialects.postgresql import insert

    return insert(table)

log = logging.getLogger("vodsync.db")

# SQLite 仅出现在自动化测试里（生产用 PG）。保留这个分支只是为了让 250+ 项测试
# 不需要依赖一个真 PG 实例就能跑，不构成部署选项。
_is_sqlite = config.DATABASE_URL.strip().lower().startswith("sqlite")

# 连接池参数：
#   pool_size / max_overflow 必须有上限（别把 NAS 上 PG 的连接数打满）
#   pool_timeout 比 SQLite busy_timeout(10s) 更短，宁可快速返回 503 也不把请求挂死
engine_kwargs = {
    "pool_pre_ping": True,
    "future": True,
    "pool_size": max(1, config.DB_POOL_SIZE),
    "max_overflow": max(0, config.DB_MAX_OVERFLOW),
    "pool_timeout": max(1, config.DB_POOL_TIMEOUT),
    "pool_recycle": 1800,
}
if _is_sqlite:
    # SQLite: 允许跨线程复用连接（FastAPI 线程池 + 调度器线程 + 同步线程）
    engine_kwargs["connect_args"] = {"check_same_thread": False}

engine = create_engine(config.DATABASE_URL, **engine_kwargs)


# ---------------------------------------------------------------------------
# 写事务闸门
#
# PostgreSQL 有自己的行/表锁，多个写事务可以真正并发，这里不加锁。
# SQLite（仅测试后端）同一时刻只允许一个写者：并发写事务会互相等 busy_timeout，
# 等待期间线程仍占着池连接 —— 所以测试路径上仍保留进程内串行化。
# ---------------------------------------------------------------------------
class _NoLock:
    """占位锁：PostgreSQL 下不做任何串行化。"""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


_write_lock = threading.RLock() if _is_sqlite else _NoLock()


@contextlib.contextmanager
def write_txn():
    """
    统一写事务入口：`with database.write_txn() as conn: ...`

    - PostgreSQL：直接开事务
    - SQLite（测试）：进程内串行 + 单个事务，天然避免 "database is locked"
    所有写操作都应走这里，不要在业务代码里直接 engine.begin()。
    """
    with _write_lock:
        with engine.begin() as conn:
            yield conn


@contextlib.contextmanager
def write_lock_only():
    """只拿写锁不开事务（需要自己控制事务边界时用）。"""
    with _write_lock:
        yield


def pool_status() -> dict:
    """连接池运行态（排查"连接池耗尽"用）。"""
    pool = engine.pool
    try:
        return {
            "size": pool.size(),
            "checked_out": pool.checkedout(),
            "overflow": pool.overflow(),
            "max_overflow": engine_kwargs["max_overflow"],
            "pool_timeout": engine_kwargs["pool_timeout"],
        }
    except (AttributeError, NotImplementedError):
        return {}


if _is_sqlite:
    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_conn, _record):  # noqa: ANN001
        """仅测试后端：每个新连接开启 WAL 等参数（WAL 让读写并发不互相阻塞）。"""
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA busy_timeout=10000")
        cur.execute("PRAGMA temp_store=MEMORY")
        cur.execute("PRAGMA cache_size=-32000")  # 32MB 页缓存
        cur.close()


def init_db():
    """初始化元数据表（sources / sync_logs / settings）。源站数据表在发现源站时动态创建。"""
    from .models import META

    META.create_all(engine)
    _migrate()
    log.info("数据库已就绪: %s", engine.url.render_as_string(hide_password=True))


def _migrate():
    """
    轻量迁移：为已存在的旧库补齐新增列（不引入 alembic，避免部署复杂度）。
    新增字段时在此追加一条 (表名, 列名, 列定义) 即可。
    """
    from sqlalchemy import inspect

    wanted = [
        ("sources", "total_pages", "INTEGER DEFAULT 0"),
        ("sources", "paused", "INTEGER DEFAULT 0"),
        ("sources", "paused_at", "TIMESTAMP"),
        ("sources", "auto_sync", "INTEGER DEFAULT 1"),
        # 已有 52 个源站会被填成 latest；新库由 models.py 的 server_default 保证
        ("sources", "origin", "VARCHAR(16) DEFAULT 'latest'"),
        # 健康度退避（v1.8.1）
        ("sources", "fail_streak", "INTEGER DEFAULT 0"),
        ("sources", "skip_until", "TIMESTAMP"),
        # 播放线路策略覆盖（v1.11.0）：inherit / off / filter / whitelist
        ("sources", "play_line_policy", "VARCHAR(16) DEFAULT 'inherit'"),
        # 明星词库别名（v1.9.2）：逗号分隔，命中后归一到主名
        ("star_terms", "aliases", "VARCHAR(512) DEFAULT ''"),
    ]
    insp = inspect(engine)
    existing_tables = set(insp.get_table_names())
    for table, column, ddl in wanted:
        if table not in existing_tables:
            continue
        cols = {c["name"] for c in insp.get_columns(table)}
        if column in cols:
            continue
        with write_txn() as conn:
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
        log.info("数据库迁移: %s.%s 已添加", table, column)

    # ---- v1.9.0：为所有已存在的 vod_{slug} 表补相关推荐信号字段 ----
    # 建表模板（models.build_vod_table）已含这些列，但存量库的几十张表是老模板
    # 建的，必须逐表 ALTER。新列全部带 DEFAULT ''，立即生效、不锁表（PG 走
    # 快速 ADD COLUMN）。注意：必须先跑迁移再让业务代码 build_vod_table()，
    # 否则缓存的 Table 对象和真实表结构不一致。
    vod_extra = [
        ("vod_actor", "VARCHAR(512) DEFAULT ''"),
        ("vod_director", "VARCHAR(255) DEFAULT ''"),
        ("vod_writer", "VARCHAR(255) DEFAULT ''"),
        ("vod_area", "VARCHAR(64) DEFAULT ''"),
        ("vod_year", "VARCHAR(16) DEFAULT ''"),
        ("vod_tag", "VARCHAR(255) DEFAULT ''"),
        # 播放线路来源（v1.11.0）：上游 vod_play_from 原始串。
        # 存量 657 万行补完是空串 —— 读路径按「没这个信息」处理，不需要重同步，
        # 回填走 POST /api/v1/sync/{slug}?mode=backfill_play_from（只写这一列）。
        ("play_from", "TEXT DEFAULT ''"),
    ]
    for tname in sorted(existing_tables):
        if not tname.startswith("vod_") or tname == "vod_related":
            continue
        cols = {c["name"] for c in insp.get_columns(tname)}
        missing = [(c, ddl) for c, ddl in vod_extra if c not in cols]
        if not missing:
            continue
        with write_txn() as conn:
            for column, ddl in missing:
                conn.execute(text(f"ALTER TABLE {tname} ADD COLUMN {column} {ddl}"))
        log.info(
            "数据库迁移: %s 已补推荐信号字段 %s",
            tname, ",".join(c for c, _ in missing),
        )


def table_exists(name: str) -> bool:
    if _is_sqlite:
        sql = "SELECT name FROM sqlite_master WHERE type='table' AND name=:n"
    else:
        sql = "SELECT table_name FROM information_schema.tables WHERE table_name = :n"
    with engine.connect() as conn:
        return conn.execute(text(sql), {"n": name}).first() is not None


def list_tables(prefix: str = "") -> set:
    """
    一次取回全部表名（可带前缀）。

    为什么需要：多源查询要判断 52 张 vod_{slug} 表是否存在，逐张调 table_exists()
    等于 52 次「借连接 + 一次往返」——在群晖那种 IO 上就是白白多几十毫秒。
    """
    if _is_sqlite:
        sql = "SELECT name FROM sqlite_master WHERE type='table'"
    else:
        sql = "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()"
    with engine.connect() as conn:
        names = {r[0] for r in conn.execute(text(sql)).all()}
    return {n for n in names if n.startswith(prefix)} if prefix else names


def is_sqlite() -> bool:
    return _is_sqlite


# ---------------------------------------------------------------------------
# 搜索索引（v1.10.0 / P8）
#
# `name LIKE '%关键词%'` 用不上 B-tree（左模糊），52 张表各扫一遍就是秒级。
# pg_trgm 的 GIN 索引让它走索引：长尾词从秒级降到 ms 级。
#
# 为什么放在后台线程里做：
#   - CREATE INDEX CONCURRENTLY 不锁表，但 200 万行仍要几十秒到几分钟；
#   - 启动时同步执行会把服务卡在启动阶段（调度器都起不来）。
# 幂等：IF NOT EXISTS，建过即跳过；不能建（权限/版本）只记一条日志，
# 搜索退化为顺序扫描 —— 功能不变，只是慢。
# ---------------------------------------------------------------------------
def ensure_search_indexes() -> dict:
    """为每张 vod_* 表建 name 的 pg_trgm GIN 索引（幂等、后台调用）。"""
    if _is_sqlite:
        return {"skipped": "sqlite"}
    if not config.PG_TRGM_ENABLED:
        return {"skipped": "disabled"}
    try:
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
    except Exception as e:
        log.warning("pg_trgm 不可用（搜索将走顺序扫描，功能不受影响）: %s", str(e)[:200])
        return {"error": "extension_unavailable"}

    created, failed = [], 0
    for tname in sorted(list_tables("vod_")):
        if tname == "vod_related":
            continue
        idx = f"idx_trgm_{tname}"
        try:
            with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
                conn.execute(text(
                    f'CREATE INDEX CONCURRENTLY IF NOT EXISTS "{idx}" '
                    f'ON "{tname}" USING gin (name gin_trgm_ops)'
                ))
            created.append(tname)
        except Exception as e:
            failed += 1
            log.warning("建搜索索引失败 %s: %s", tname, str(e)[:160])
    if created:
        log.info("搜索索引已就绪（pg_trgm GIN）: %d 张表%s",
                 len(created), f"，失败 {failed} 张" if failed else "")
    return {"created": len(created), "failed": failed}
