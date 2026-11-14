"""
v1.16.6 回归测试：标签索引面板「已写行」口径修正（方案 A）。

背景
----
面板这一列原先读 `tag_index_state.indexed`，但那个字段**只有**「重建索引」
（tags.rebuild_source）会写；真正维护倒排表的明星 Tag 夜间重建走的是
`tags.index_rows()`，直接写 `video_tags`，从不回写它。线上实测面板加总
58 万 vs 真实 170 万，甚至出现面板 4.4 万 / 真实 3.4 万这种「偏高」的站。

本测试锁定修正后的行为：
1. 面板展示的行数 = 实时 `COUNT(video_tags) WHERE source=...`（真值）；
2. `indexed` 仍保留在响应里，但只是回填进度参考，不能等于行数；
3. 顶层 `rows`（总数）与逐站 `rows` 来自同一次 GROUP BY（走主键 index-only scan）；
4. 行数走 120s 快照缓存（面板 5s 轮询不能反复扫 170 万行），`refresh=True` 强刷。

运行：PYTHONPATH=. .venv/Scripts/python.exe scripts/test_v1166_tag_rows.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DB = os.path.join(tempfile.gettempdir(), "_v1166_tag_rows.db")
if os.path.exists(DB):
    os.remove(DB)

os.environ["DATABASE_URL"] = f"sqlite:///./{DB}"

from sqlalchemy import select  # noqa: E402

from app import database, panel, tags  # noqa: E402
from app.models import sources, tag_index_state, video_tags  # noqa: E402

database.init_db()


def seed():
    """两个源站：一个「面板数」远低于真实值，一个远高于（模拟线上两种脏态）。"""
    with database.write_txn() as c:
        c.execute(sources.insert().values(
            name="测试站A", slug="abc", api_url="https://a.example/api",
            origin="custom", status="ok", enabled=1))
        c.execute(sources.insert().values(
            name="测试站B", slug="def", api_url="https://b.example/api",
            origin="custom", status="ok", enabled=1))
        # A：回填计数停在 3（陈旧），真实有 10 行
        c.execute(tag_index_state.insert().values(
            slug="abc", status="done", cursor=10, processed=10, total=10,
            indexed=3, finished_at=None))
        # B：回填计数 99（陈旧且偏高），真实只有 2 行
        c.execute(tag_index_state.insert().values(
            slug="def", status="done", cursor=2, processed=2, total=2,
            indexed=99, finished_at=None))
        for i in range(1, 11):
            c.execute(video_tags.insert().values(source="abc", vod_id=i, tag_norm=f"t{i}"))
        for i in range(1, 3):
            c.execute(video_tags.insert().values(source="def", vod_id=i, tag_norm=f"d{i}"))


def by_slug(st):
    return {s["slug"]: s for s in st["sources"]}


panel.cache_clear()
seed()

# --- 1. 首次查询：实时真值，且 age=0（现场算） ---
st = tags.status()
assert st["rows"] == 12, f"总数应为 12，实际 {st['rows']}"
assert st["cache_age_s"] == 0, f"首次应为实时值 age=0，实际 {st['cache_age_s']}"
a, b = by_slug(st)["abc"], by_slug(st)["def"]
assert a["rows"] == 10, f"A 实时行数应为 10，实际 {a['rows']}"
assert b["rows"] == 2, f"B 实时行数应为 2，实际 {b['rows']}"
# 逐站 rows 之和必须等于顶层总数（同一次 GROUP BY 出来）
assert sum(s["rows"] for s in st["sources"]) == st["rows"], "逐站与总数不一致"

# --- 2. indexed 仍是陈旧的回填计数，绝不能拿来当行数 ---
assert a["indexed"] == 3 and a["rows"] == 10, "A：indexed 与真实行数必须区分开"
assert b["indexed"] == 99 and b["rows"] == 2, "B：indexed 偏高时必须以真实行数为准"

# --- 3. 快照缓存：新增行后，普通查询仍是旧值，refresh=True 才更新 ---
with database.write_txn() as c:
    c.execute(video_tags.insert().values(source="abc", vod_id=999, tag_norm="t999"))
st_cached = tags.status()
assert st_cached["rows"] == 12, "缓存期内总数应保持旧值 12（若重算会得到 13）"
assert by_slug(st_cached)["abc"]["rows"] == 10, "缓存期内逐站行数也应保持旧值"

st_fresh = tags.status(refresh=True)
assert st_fresh["rows"] == 13, f"强刷后总数应为 13，实际 {st_fresh['rows']}"
assert st_fresh["cache_age_s"] == 0, "强刷必须现场算（age=0）"
assert by_slug(st_fresh)["abc"]["rows"] == 11, "强刷后 A 应为 11"

# --- 4. 清理 ---
panel.cache_clear()
database.engine.dispose()
os.remove(DB)
print("ALL_V1166_TAG_ROWS_TESTS_PASS")
