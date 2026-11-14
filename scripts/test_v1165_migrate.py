"""临时回归：验证 v1.16.5 的非法 slug 迁移与登记收敛（SQLite 后端）。"""
import os, sys, tempfile
# 必须在导入 app 之前设置 DATABASE_URL
db = tempfile.mktemp(suffix=".db")
os.environ["DATABASE_URL"] = f"sqlite:///{db}"
os.environ.setdefault("LATEST_JSON_URL", "https://example.com/x.json")

from sqlalchemy import select, text, func
from app import database, models
from app import syncer
from app.models import (
    sources, sync_logs, vod_related, star_map, star_rebuild_state,
    video_tags, tag_index_state,
)

# 1) 建表
database.init_db()

# 2) 插入两个非法 slug 源站（模拟历史上把 API 地址当 slug 入库）
with database.write_txn() as conn:
    conn.execute(sources.insert().values(
        name="靡都动漫", slug="https://caiji.moduapi.cc", api_url="https://caiji.moduapi.cc/api.php/provide/vod",
        origin="custom", status="ok", enabled=1, auto_sync=1, total_videos=10))
    conn.execute(sources.insert().values(
        name="飘零资源", slug="https://p2100.net", api_url="https://p2100.net/api.php/provide/vod",
        origin="custom", status="ok", enabled=1, auto_sync=1, total_videos=5))
    conn.execute(sources.insert().values(
        name="正常站", slug="okslug", api_url="https://ok.example/api",
        origin="latest", status="ok", enabled=1, auto_sync=1, total_videos=3))

bad = "https://caiji.moduapi.cc"
# 3) 给第一个坏 slug 建数据表 + 引用行
t = syncer.build_vod_table(bad)
t.create(database.engine, checkfirst=True)
with database.write_txn() as conn:
    conn.execute(text(f'INSERT INTO "{t.name}" (vod_id, name) VALUES (1, "x")'))
    conn.execute(sync_logs.insert().values(slug=bad, mode="full", status="success"))
    conn.execute(vod_related.insert().values(slug=bad, vod_id=1, related_json="[]"))
    conn.execute(star_map.insert().values(slug=bad, vod_id=1, term="a"))
    conn.execute(star_rebuild_state.insert().values(slug=bad))
    conn.execute(video_tags.insert().values(source=bad, vod_id=1, tag_norm="t"))
    conn.execute(tag_index_state.insert().values(slug=bad, status="done", total=10, processed=10, indexed=1))

# 4) 运行迁移
syncer.migrate_unsafe_slugs()

# 5) 断言
with database.engine.connect() as conn:
    rows = {r["slug"]: r["name"] for r in conn.execute(select(sources)).mappings().all()}
    print("sources slugs:", sorted(rows))

assert "https://caiji.moduapi.cc" not in rows, "坏 slug 仍残留"
assert "https://p2100.net" not in rows, "坏 slug 仍残留"
assert "okslug" in rows, "正常 slug 被误改"

# 坏 slug 改成的安全标识（s+md5 形式）
new_slug = [s for s in rows if rows[s] == "靡都动漫"][0]
assert syncer.SAFE_SLUG.match(new_slug), f"新 slug 仍不安全: {new_slug}"
print("靡都动漫 ->", new_slug)

# 数据表改名
assert database.table_exists(f"vod_{new_slug}"), "vod 表未改名"
assert not database.table_exists(f"vod_{bad}"), "旧 vod 表未消失"

# 引用行全部迁移
with database.engine.connect() as conn:
    n_sl = conn.execute(select(func.count()).select_from(sync_logs).where(sync_logs.c.slug == new_slug)).scalar()
    n_vr = conn.execute(select(func.count()).select_from(vod_related).where(vod_related.c.slug == new_slug)).scalar()
    n_sm = conn.execute(select(func.count()).select_from(star_map).where(star_map.c.slug == new_slug)).scalar()
    n_sr = conn.execute(select(func.count()).select_from(star_rebuild_state).where(star_rebuild_state.c.slug == new_slug)).scalar()
    n_vt = conn.execute(select(func.count()).select_from(video_tags).where(video_tags.c.source == new_slug)).scalar()
    n_ti = conn.execute(select(func.count()).select_from(tag_index_state).where(tag_index_state.c.slug == new_slug)).scalar()
print("引用行迁移:", n_sl, n_vr, n_sm, n_sr, n_vt, n_ti)
assert (n_sl, n_vr, n_sm, n_sr, n_vt, n_ti) == (1, 1, 1, 1, 1, 1), "引用行未全部迁移"

# 6) 再跑一次迁移应幂等（无变化、无报错）
syncer.migrate_unsafe_slugs()
with database.engine.connect() as conn:
    rows2 = {r["slug"]: r["name"] for r in conn.execute(select(sources)).mappings().all()}
assert rows2 == rows, "二次迁移改变了已合法的 slug"

# 7) 登记收敛：用户把 URL 当 slug 提交应被收敛，且占用时给出 409 语义
with database.write_txn() as conn:
    res = syncer.register_custom_source(name="我的站", api_url="https://new.example/api", slug="https://evil.url/x")
    print("register with bad slug ->", res["slug"])
    assert syncer.SAFE_SLUG.match(res["slug"]), "登记未收敛非法 slug"

# 占用提示
try:
    syncer.register_custom_source(name="占用", api_url="https://dup.example/api", slug="okslug")
    raise SystemExit("应当抛出 ValueError(占用)")
except ValueError as e:
    assert "已被占用" in str(e), str(e)
    print("占用校验 OK:", e)

print("\nALL_V1165_MIGRATE_TESTS_PASS")
try:
    database.engine.dispose()  # 释放连接，避免 Windows 下文件被占用无法删除
except Exception:
    pass
try:
    os.remove(db)  # 清理临时 SQLite
except OSError:
    pass
