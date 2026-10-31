#!/usr/bin/env python
"""
SQLite -> PostgreSQL 数据迁移（vod-sync）

【为什么需要这个脚本】
  把 DATABASE_URL 换到 PostgreSQL 后，新库是空的：sources / sync_logs / vod_* 全都不存在。
  如果不搬数据，只能把所有源站重新全量同步一遍（百万级 = 十几小时）。
  本脚本把 SQLite 里的元数据与视频数据整体搬到目标库，并保留：
    - watermark（增量水位线）→ 迁移后直接接着做增量，不需要重抓
    - last_page / total_pages（全量断点）→ 没跑完的全量可以从断点续
    - paused / auto_sync / last_sync_at 等运行状态 → 面板显示与开关状态不丢

【用法】在项目根目录执行（容器内也可以：docker exec -it vodsync sh -c "cd /app && python -m scripts.migrate_sqlite_to_pg ..."）

  # 0) 迁移前请先停掉服务，避免迁移过程中源库还在写
  # 1) 看看要搬什么（不写目标库）
  python scripts/migrate_sqlite_to_pg.py --target "postgresql+psycopg2://vodsync:vodsync@192.168.1.10:5432/vodsync" --dry-run

  # 2) 正式迁移（可重复执行：已存在的行按主键跳过）
  python scripts/migrate_sqlite_to_pg.py --target "postgresql+psycopg2://vodsync:vodsync@192.168.1.10:5432/vodsync"

  # 3) 校验（行数 + 关键字段逐行比对）
  python scripts/migrate_sqlite_to_pg.py --target "..." --verify

常用选项：
  --source PATH     源 SQLite 文件（默认 $DATA_DIR/vodsync.db，退化为 data/vodsync.db）
  --only-meta       只搬 sources / sync_logs（视频数据不搬，之后各站自行全量同步）
  --tables a,b      只搬指定的 vod 表（写 slug，如 jkun,t1）
  --batch N         每批读取/写入行数（默认 2000）
  --replace         目标已有同主键时用源库覆盖（默认跳过，保证幂等）
"""
import argparse
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from sqlalchemy import DateTime, String, create_engine, func, inspect, select

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.models import build_vod_table, sources as _sources_tbl, sync_logs as _logs_tbl  # noqa: E402

# 元数据表（顺序固定：先 sources 后 sync_logs，便于阅读）
META_TABLES = ("sources", "sync_logs")
# 注意：这里不能用 META.create_all(target) —— build_vod_table() 会把 vod_* 注册进同一个 META，
# 一旦之前构建过任何 vod 表，create_all 就会把它们全建出来，让 --only-meta / --tables 失去意义。
META_DEFS = {"sources": _sources_tbl, "sync_logs": _logs_tbl}


# ---------------------------------------------------------------------------
# 值转换
# ---------------------------------------------------------------------------
def _parse_dt(value: str):
    """SQLite 里的时间是字符串，PG 的 TIMESTAMP 需要 datetime 对象。"""
    s = value.strip().replace("T", " ")
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _coerce(value, column, stats):
    """
    把一行里的值转成目标库可接受的类型：
      - 时间字符串 -> datetime
      - 去掉 NUL 字节（PostgreSQL 的 text 不接受 \x00，SQLite 允许）
      - 超长字符串按列长度截断（SQLite 不校验 varchar 长度，PG 会直接报错）
    """
    if value is None:
        return None
    if isinstance(column.type, DateTime) and isinstance(value, str):
        parsed = _parse_dt(value)
        if parsed is None:
            stats["bad_time"] += 1
        return parsed
    if isinstance(value, str):
        if "\x00" in value:
            stats["nul"] += 1
            value = value.replace("\x00", "")
        limit = getattr(column.type, "length", None)
        if limit and len(value) > limit:
            stats["truncated"] += 1
            return value[:limit]
    return value


def _insert_stmt(table, target_engine, replace: bool):
    """构造带冲突处理的 INSERT（PostgreSQL / SQLite 都支持 ON CONFLICT）。"""
    dialect = target_engine.dialect.name
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    elif dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    else:  # 其它方言（理论上用不到）退化为普通插入
        return table.insert(), False

    stmt = insert(table)
    if not replace:
        return stmt.on_conflict_do_nothing(), True
    pk_cols = [c.name for c in table.primary_key.columns]
    if not pk_cols:
        return stmt.on_conflict_do_nothing(), True
    updates = {c.name: stmt.excluded[c.name] for c in table.columns if c.name not in pk_cols}
    return stmt.on_conflict_do_update(index_elements=pk_cols, set_=updates), True


# ---------------------------------------------------------------------------
# 迁移主体
# ---------------------------------------------------------------------------
def _iter_batches(src_conn, table, batch: int, columns=None):
    """按主键翻页读取，避免一次性把整表读进内存。"""
    cols = [table.c[c] for c in (columns or [c.name for c in table.columns])]
    pk = list(table.primary_key.columns)
    if not pk:
        rows = src_conn.execute(select(*cols)).mappings().all()
        if rows:
            yield rows
        return
    pk_col = pk[0]
    last = None
    while True:
        q = select(*cols).order_by(pk_col).limit(batch)
        if last is not None:
            q = q.where(pk_col > last)
        rows = src_conn.execute(q).mappings().all()
        if not rows:
            return
        yield rows
        last = rows[-1][pk_col.name]


def copy_table(src_conn, tgt_engine, table, batch: int, replace: bool, dry_run: bool,
               src_columns=None, total_hint: int = 0):
    """
    搬一张表，返回写入行数。幂等，可重复执行。

    src_columns: 源库实际存在的列（旧库可能缺新列，例如没有 auto_sync），
                 只搬两边都有的列，缺失列交给目标库默认值。
    total_hint:  源表行数，仅用于大表打印进度（百万行级不打印会以为卡死）。
    """
    stats = {"bad_time": 0, "truncated": 0, "nul": 0}
    target_cols = [c.name for c in table.columns]
    if src_columns:
        target_cols = [c for c in target_cols if c in src_columns]
    written = 0
    stmt, _ = _insert_stmt(table, tgt_engine, replace)
    show_progress = total_hint >= 50000
    next_mark = 25

    for rows in _iter_batches(src_conn, table, batch, target_cols):
        payload = [
            {c: _coerce(row[c], table.c[c], stats) for c in target_cols}
            for row in rows
        ]
        if dry_run:
            written += len(payload)
            continue
        # 每批一个事务：中断后可直接重跑，已写入的批次会被主键冲突跳过
        with tgt_engine.begin() as conn:
            conn.execute(stmt, payload)
        written += len(payload)

        if show_progress and total_hint:
            pct = written * 100 // total_hint
            if pct >= next_mark:
                print(f"         … {next_mark}% ({written:,}/{total_hint:,} 行)")
                next_mark += 25

    if stats["truncated"]:
        print(f"      ! 有 {stats['truncated']} 个字段超出列长度，已截断")
    if stats["bad_time"]:
        print(f"      ! 有 {stats['bad_time']} 个时间字段无法解析，已置为 NULL")
    if stats["nul"]:
        print(f"      ! 有 {stats['nul']} 个字段含 NUL 字节，已剔除（PG 的 text 不接受）")
    return written


def _table_count(engine, name: str) -> int:
    from sqlalchemy import text as _text

    with engine.connect() as conn:
        return int(conn.execute(_text(f'SELECT COUNT(*) FROM "{name}"')).scalar() or 0)


def check_target(target_engine):
    """目标库连通性：连不上时给出可读原因，而不是让用户看一屏 SQLAlchemy 堆栈。"""
    from sqlalchemy import text as _text

    try:
        with target_engine.connect() as conn:
            conn.execute(_text("SELECT 1"))
            if target_engine.dialect.name != "postgresql":
                return None, f"{target_engine.dialect.name} 可连接"
            version = conn.execute(_text("SELECT version()")).scalar()
            size = conn.execute(
                _text("SELECT pg_size_pretty(pg_database_size(current_database()))")
            ).scalar()
            return None, f"{str(version).split(',')[0]}  当前库大小 {size}"
    except Exception as exc:  # noqa: BLE001
        return str(exc).strip().splitlines()[0][:300], ""


def create_table_only(engine, table) -> bool:
    """
    建表但**不建索引**：百万行先插数据再建索引，比边插边维护 B-tree 快数倍。
    返回是否真的新建了表。
    """
    from sqlalchemy.schema import CreateTable

    if inspect(engine).has_table(table.name):
        return False
    with engine.begin() as conn:
        conn.execute(CreateTable(table))
    return True


def create_indexes(engine, tables) -> int:
    """数据写完后统一建索引（幂等：已存在则跳过）。"""
    created = 0
    for table in tables:
        for idx in table.indexes:
            before = set()
            try:
                before = {i["name"] for i in inspect(engine).get_indexes(table.name)}
            except Exception:  # noqa: BLE001
                pass
            idx.create(engine, checkfirst=True)
            if idx.name not in before:
                created += 1
    return created


def _reset_pg_sequences(target_engine, tables):
    """PG 下显式插入 id 后，SERIAL 序列不会自动推进 —— 不重置会导致后续 INSERT 主键冲突。"""
    if target_engine.dialect.name != "postgresql":
        return
    for table in tables:
        if "id" not in table.c:
            continue
        sql = (
            f"SELECT setval(pg_get_serial_sequence('{table.name}', 'id'), "
            f"GREATEST(COALESCE((SELECT MAX(id) FROM {table.name}), 0), 1), true)"
        )
        with target_engine.begin() as conn:
            conn.exec_driver_sql(sql)
        print(f"  · 序列已对齐: {table.name}.id")


def _source_slug(table_name: str) -> str:
    return table_name[len("vod_"):] if table_name.startswith("vod_") else ""


def discover_vod_tables(src_engine, only=None):
    """找出源库里的 vod_* 表，返回按名字排序的表名列表。"""
    names = [n for n in inspect(src_engine).get_table_names() if n.startswith("vod_")]
    if only:
        wanted = {s.strip() for s in only if s.strip()}
        names = [n for n in names if _source_slug(n) in wanted]
    return sorted(names)


def verify(src_engine, tgt_engine, table_names, sample_fields):
    """校验：表是否都在、行数是否一致、sources 关键字段是否逐行一致。"""
    problems = []

    missing = [n for n in table_names if not inspect(tgt_engine).has_table(n)]
    for n in missing:
        problems.append(f"目标库缺表: {n}")

    for n in table_names:
        if n in missing:
            continue
        a, b = _table_count(src_engine, n), _table_count(tgt_engine, n)
        flag = "OK " if a == b else "差异"
        print(f"  [{flag}] {n:<24} 源 {a:>8} / 目标 {b:>8}")
        if a != b:
            problems.append(f"{n} 行数不一致: 源 {a} / 目标 {b}")

    # sources 关键字段逐行比对（水位线/断点/开关，决定迁移后能不能直接增量）
    from app.models import sources as _src_tbl

    with src_engine.connect() as c1, tgt_engine.connect() as c2:
        rows1 = {r["slug"]: r for r in c1.execute(select(_src_tbl)).mappings().all()}
        rows2 = {r["slug"]: r for r in c2.execute(select(_src_tbl)).mappings().all()}

    only_src = set(rows1) - set(rows2)
    if only_src:
        problems.append(f"目标库缺少源站: {sorted(only_src)[:8]}")

    for slug, r1 in rows1.items():
        r2 = rows2.get(slug)
        if not r2:
            continue
        for f in sample_fields:
            v1, v2 = r1[f], r2[f]
            if isinstance(v1, str) and isinstance(v2, str):
                same = v1.strip() == v2.strip()
            elif isinstance(v1, datetime) or isinstance(v2, datetime):
                same = str(v1)[:19] == str(v2)[:19]
            else:
                same = v1 == v2
            if not same:
                problems.append(f"{slug}.{f} 不一致: 源={v1!r} 目标={v2!r}")
    return problems


# ---------------------------------------------------------------------------
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="SQLite -> PostgreSQL 数据迁移（保留水位线与断点）")
    ap.add_argument("--source", default=None, help="源 SQLite 文件路径")
    ap.add_argument("--target", required=True, help="目标库 URL，如 postgresql+psycopg2://user:pass@host:5432/vodsync")
    ap.add_argument("--only-meta", action="store_true", help="只迁 sources / sync_logs，不搬视频数据")
    ap.add_argument("--tables", default="", help="只迁指定源站（逗号分隔的 slug）")
    ap.add_argument("--batch", type=int, default=2000, help="每批行数（默认 2000）")
    ap.add_argument("--replace", action="store_true", help="主键冲突时用源库覆盖（默认跳过）")
    ap.add_argument("--dry-run", action="store_true", help="只统计不写入")
    ap.add_argument("--verify", action="store_true", help="只校验（不迁移），用于迁移后复查")
    args = ap.parse_args(argv)

    src_path = Path(args.source or (Path(os.environ.get("DATA_DIR", "data")) / "vodsync.db"))
    if not src_path.exists():
        print(f"[错误] 源库不存在: {src_path.resolve()}")
        print("       用 --source 指定 vodsync.db 的实际路径")
        return 2

    wal = src_path.with_name(src_path.name + "-wal")
    if wal.exists() and wal.stat().st_size > 0:
        print("[提示] 检测到未合并的 -wal 文件，建议先停止服务再迁移以保证数据一致")

    src_engine = create_engine(f"sqlite:///{src_path.as_posix()}", future=True)
    tgt_engine = create_engine(args.target, future=True, pool_pre_ping=True)

    print(f"源库  : SQLite {src_path.resolve()}")
    print(f"目标库: {tgt_engine.url.render_as_string(hide_password=True)}")
    if args.dry_run:
        print("模式  : DRY-RUN（只统计，不写入）")
    elif args.verify:
        print("模式  : 仅校验（不迁移）")

    # 连通性先探一次：dry-run 只警告，正式迁移连不上就没必要往下走
    err, info = check_target(tgt_engine)
    if err:
        print(f"目标库: 连接失败 —— {err}")
        if not args.dry_run:
            print("\n[错误] 目标库不可用，迁移已中止。请检查：")
            print("  1) PG 容器是否在运行：docker ps | grep postgres")
            print("  2) 主机名/端口是否正确（compose 内网用 postgres:5432，宿主机用 IP:端口）")
            print("  3) 库与用户是否已创建（POSTGRES_DB / POSTGRES_USER）")
            return 3
        print("       （DRY-RUN 仍会统计源库规模）")
    else:
        print(f"        {info}")
    print()

    only = [s for s in args.tables.split(",") if s.strip()] if args.tables else None
    vod_tables = [] if args.only_meta else discover_vod_tables(src_engine, only)
    all_names = list(META_TABLES) + vod_tables
    write_mode = not args.dry_run and not args.verify
    plan = [META_DEFS[n] for n in META_TABLES] + [build_vod_table(_source_slug(n)) for n in vod_tables]

    # ---- DRY-RUN：只统计源库规模，不碰目标库 ----
    if args.dry_run:
        print("将要迁移的表：")
        total_rows = 0
        for idx, table in enumerate(plan, 1):
            n = _table_count(src_engine, table.name)
            total_rows += n
            if n:
                print(f"  [{idx:>2}/{len(plan)}] {table.name:<24} {n:>8} 行")
        print(f"\n合计 {len(plan)} 张表 / {total_rows:,} 行（DRY-RUN 未写入任何数据）")
        if args.only_meta:
            print("注意：--only-meta 只搬元数据，视频数据需要各站重新全量同步")
        return 0

    # ---- 建表：只建本次要迁的表（元数据表 + 过滤后的 vod_*），索引留到最后 ----
    if write_mode:
        print("建表（索引留到数据写完后统一创建）：")
        for name in META_TABLES:
            create_table_only(tgt_engine, META_DEFS[name])
        print("  · sources / sync_logs 已就绪")
        for table in plan[len(META_TABLES):]:
            create_table_only(tgt_engine, table)
        if vod_tables:
            print(f"  · {len(vod_tables)} 张 vod_* 表已就绪")
        print()
        _reset_pg_sequences(tgt_engine, [META_DEFS[n] for n in META_TABLES])

        # ---- 搬数据 ----
        print("迁移数据：")
    started = time.time()
    total = 0
    if write_mode:
        with src_engine.connect() as src_conn:
            for idx, table in enumerate(plan, 1):
                n_src = _table_count(src_engine, table.name)
                if n_src == 0:
                    print(f"  [{idx:>2}/{len(plan)}] {table.name:<24} 空表，跳过")
                    continue
                # 旧库可能缺新列（如 auto_sync），以源库实际列为准
                src_cols = {c["name"] for c in inspect(src_engine).get_columns(table.name)}
                t0 = time.time()
                n = copy_table(src_conn, tgt_engine, table, args.batch, args.replace, False,
                               src_columns=src_cols, total_hint=n_src)
                total += n
                rate = n / max(time.time() - t0, 1e-6)
                print(f"  [{idx:>2}/{len(plan)}] {table.name:<24} {n:>8} 行  "
                      f"{time.time() - t0:5.1f}s  ({rate:,.0f} 行/秒)")

        elapsed = time.time() - started
        print()
        print(f"完成：{len(plan)} 张表 / {total:,} 行，用时 {elapsed:.1f}s")

        # ---- 数据写完再建索引（比边插边维护 B-tree 快数倍）----
        print()
        print("建索引：")
        t_idx = time.time()
        n_idx = create_indexes(tgt_engine, plan)
        print(f"  · 新建 {n_idx} 个索引，用时 {time.time() - t_idx:.1f}s")
        if tgt_engine.dialect.name == "postgresql":
            try:
                with tgt_engine.begin() as conn:
                    conn.exec_driver_sql("ANALYZE")
                print("  · 已执行 ANALYZE（更新统计信息，查询计划更准）")
            except Exception as exc:  # noqa: BLE001
                print(f"  ! ANALYZE 跳过（不影响数据）：{exc}")

    # ---- 校验（迁移后总是校验一遍，几分钟的活别留隐患）----
    print()
    print("校验：")
    problems = verify(
        src_engine, tgt_engine, all_names,
        sample_fields=("watermark", "last_page", "total_pages", "paused", "auto_sync",
                       "total_videos", "last_sync_at", "enabled", "status"),
    )
    if problems:
        print(f"\n发现 {len(problems)} 个问题：")
        for p in problems[:20]:
            print("  -", p)
        return 1
    print("  全部一致 ✓")

    print()
    print("下一步：")
    print("  1) 把服务环境变量改成 DATABASE_URL=" + tgt_engine.url.render_as_string(hide_password=True))
    print("  2) 重启服务；首次启动会建索引，日志里能看到「使用 PostgreSQL」")
    print("  3) 面板点一次某站的「增量」：若水位线迁移正确，应只拉到少量新片，说明可以接着同步")
    print("  4) PostgreSQL 下写并发不再串行，可把 SOURCE_SYNC_CONCURRENCY 调到 4~6")
    return 0


if __name__ == "__main__":
    sys.exit(main())
