"""
数据库引擎与元数据管理。

- 使用 SQLAlchemy Core（性能优于 ORM，适合百万级行批量写入）
- SQLite 时自动开启 WAL、busy_timeout、NORMAL 同步模式
- 每个源站一张 vod_{slug} 动态表，见 models.build_vod_table()
"""
import contextlib
import logging
import threading

from sqlalchemy import create_engine, event, text

from . import config


def dialect_insert(table):
    """返回带 ON CONFLICT 能力的方言 INSERT 构造器（SQLite 与 PostgreSQL 同签名）。"""
    if _is_sqlite:
        from sqlalchemy.dialects.sqlite import insert

        return insert(table)
    from sqlalchemy.dialects.postgresql import insert

    return insert(table)

log = logging.getLogger("vodsync.db")

_is_sqlite = config.DATABASE_URL.strip().lower().startswith("sqlite")

# 连接池参数：
#   pool_size / max_overflow 必须有上限（无上限=打死 SQLite 的写锁）
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
# SQLite（WAL）同一时刻只允许一个写者：并发写事务会互相等 busy_timeout，
# 等待期间线程仍占着池连接 —— 这正是 v1.4.0 在 NAS 上「连接池耗尽 → 全接口 500」的根因。
# 这里在进程内把所有写事务串行化，写者排队时不占连接，也不触发 busy_timeout。
# PostgreSQL 有自己的行/表锁机制，无需串行化。
# ---------------------------------------------------------------------------
class _NoLock:
    """占位锁：非 SQLite 时不做任何串行化。"""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


_write_lock = threading.RLock() if _is_sqlite else _NoLock()


@contextlib.contextmanager
def write_txn():
    """
    统一写事务入口：`with database.write_txn() as conn: ...`

    - SQLite：进程内串行 + 单个事务，天然避免 "database is locked"
    - PostgreSQL：直接开事务
    所有写操作都应走这里，不要在业务代码里直接 engine.begin()。
    """
    with _write_lock:
        with engine.begin() as conn:
            yield conn


@contextlib.contextmanager
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
        """每个新连接开启 WAL 等性能参数（WAL 大幅提升读写并发）。"""
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA busy_timeout=10000")
        cur.execute("PRAGMA temp_store=MEMORY")
        cur.execute("PRAGMA cache_size=-32000")  # 32MB 页缓存
        cur.close()


def init_db():
    """初始化元数据表（sources / sync_logs）。源站数据表在发现源站时动态创建。"""
    from .models import META

    META.create_all(engine)
    _migrate()
    if not _is_sqlite:
        log.info("使用 PostgreSQL: %s", engine.url.render_as_string(hide_password=True))


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


def table_exists(name: str) -> bool:
    if _is_sqlite:
        sql = "SELECT name FROM sqlite_master WHERE type='table' AND name=:n"
    else:
        sql = "SELECT table_name FROM information_schema.tables WHERE table_name = :n"
    with engine.connect() as conn:
        return conn.execute(text(sql), {"n": name}).first() is not None
