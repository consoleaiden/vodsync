"""
SeleneTV 专用 API —— 数据访问层（只读 vodsync 事实表 + 读写 4 张专用表）。

复用 vodsync 现有工具，不重复造轮子：
  - models.build_vod_table(slug) 构造动态表（带缓存），utils.validate_slug 白名单防注入
  - database.engine / write_txn / dialect_insert 连接与事务
  - MODELS/MACCMS 字段换算：name→vod_name、pic→vod_pic、play_url→播放串……

严禁把用户输入拼进 SQL：slug 一律过 validate_slug（^[a-z0-9_]{1,64}$），
表名由 SQLAlchemy Table 对象承载，值全部绑定参数。
"""
import json
import logging
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import func, select

from .. import database
from ..models import build_vod_table, sources as sources_table
from ..utils import validate_slug

log = logging.getLogger("vodsync.selenetv.repo")


# ---------------------------------------------------------------------------
# 源站（对应 Helios APISite / resources）
# ---------------------------------------------------------------------------
def list_sources(enabled_only: bool = True, allow_adult: bool = True) -> List[Dict[str, Any]]:
    """
    返回 [{"slug","name","api_url","homepage","status","enabled","total_videos","is_adult"}]。

    allow_adult=False 时剔除标记为 18+ 的源站（复用 vodsync.minor_mode 的判定，
    与面板 18+ 标记/未成年模式同源），供「用户无 18+ 权限」时调用。
    默认 True 保持既有调用行为完全不变。
    """
    stmt = select(
        sources_table.c.slug,
        sources_table.c.name,
        sources_table.c.api_url,
        sources_table.c.homepage,
        sources_table.c.status,
        sources_table.c.enabled,
        sources_table.c.total_videos,
        sources_table.c.is_adult,
    )
    if enabled_only:
        stmt = stmt.where(sources_table.c.enabled == 1)
    if not allow_adult:
        # 直接按 sources.is_adult 过滤，不依赖请求上下文（函数签名显式传参）。
        stmt = stmt.where(func.coalesce(sources_table.c.is_adult, 0) == 0)
    stmt = stmt.order_by(sources_table.c.name)
    with database.engine.connect() as conn:
        return [dict(r) for r in conn.execute(stmt).mappings().all()]


def get_source(slug: str) -> Optional[Dict[str, Any]]:
    slug = validate_slug(slug)
    with database.engine.connect() as conn:
        row = conn.execute(
            select(sources_table).where(sources_table.c.slug == slug)
        ).mappings().first()
        return dict(row) if row else None


def existing_vod_tables() -> set:
    return database.list_tables("vod_")


# ---------------------------------------------------------------------------
# 搜索 / 详情（只读 vod_{slug}）
# ---------------------------------------------------------------------------
def search_in_source(
    slug: str, keyword: str, limit: int = 100, type_name: Optional[str] = None,
    known_tables: Optional[set] = None,
) -> List[Dict[str, Any]]:
    """单源 pg_trgm LIKE 检索；返回瘦行 dict（含 play_url/content 供 episodes 提取）。

    known_tables: 调用方可一次取全 vod_ 表集合传入，避免并发时每源重复查 pg_tables
                  （与 vodsync 原生聚合搜索的 list_tables 一次取全策略一致）。
    """
    slug = validate_slug(slug)
    table = build_vod_table(slug)
    if table.name not in (known_tables if known_tables is not None else existing_vod_tables()):
        return []
    pattern = "%" + keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    cols = [
        table.c.vod_id, table.c.type_id, table.c.type_name, table.c.name,
        table.c.pic, table.c.remarks, table.c.vod_year, table.c.content,
        table.c.play_url, table.c.vod_time,
    ]
    where = [table.c.name.like(pattern, escape="\\")]
    if type_name:
        where.append(table.c.type_name == type_name)
    stmt = (
        select(*cols)
        .where(*where)
        .order_by(table.c.vod_time.desc(), table.c.vod_id.desc())
        .limit(limit)
    )
    with database.engine.connect() as conn:
        return [dict(r) for r in conn.execute(stmt).mappings().all()]


def list_adult_sources(enabled_only: bool = True) -> List[Dict[str, Any]]:
    """
    [18+ Tab] 返回标记为 18+ 的源站列表（sources.is_adult = 1）。
    调用方（router）负责 Helios Cookie 鉴权 + allow_adult 守卫，这里只按标记过滤。
    """
    stmt = select(
        sources_table.c.slug,
        sources_table.c.name,
        sources_table.c.api_url,
        sources_table.c.homepage,
        sources_table.c.total_videos,
        sources_table.c.is_adult,
    ).where(func.coalesce(sources_table.c.is_adult, 0) == 1)
    if enabled_only:
        stmt = stmt.where(sources_table.c.enabled == 1)
    stmt = stmt.order_by(sources_table.c.name)
    with database.engine.connect() as conn:
        return [dict(r) for r in conn.execute(stmt).mappings().all()]


def list_categories(slug: str) -> List[Dict[str, Any]]:
    """
    [18+ Tab] 单源分类列表：SELECT DISTINCT type_id, type_name，按 type_id 升序。
    表不存在返回空列表（与 search_in_source 的容错一致）。
    """
    slug = validate_slug(slug)
    table = build_vod_table(slug)
    if table.name not in existing_vod_tables():
        return []
    with database.engine.connect() as conn:
        rows = conn.execute(
            select(
                func.max(table.c.type_id).label("type_id"),
                table.c.type_name,
            )
            .where(table.c.type_name != "")
            .group_by(table.c.type_name)
            .order_by(func.max(table.c.type_id))
            .limit(200)
        ).mappings().all()
    return [
        {"type_id": int(r["type_id"] or 0), "type_name": str(r["type_name"] or "")}
        for r in rows
    ]


def list_videos(
    slug: str,
    type_id: Optional[int] = None,
    page: int = 1,
    size: int = 25,
) -> Dict[str, Any]:
    """
    [18+ Tab] 单源分类视频分页列表（按 vod_time 倒序，与搜索排序一致）。

    返回 {"list": [瘦行...], "total": 总数, "page": page, "size": size}。
    瘦行不含 content/play_url 大字段——网格只需要封面/标题/年份/备注，
    播放信息走既有 /api/detail 端点按需获取。
    """
    slug = validate_slug(slug)
    table = build_vod_table(slug)
    if table.name not in existing_vod_tables():
        return {"list": [], "total": 0, "page": page, "size": size}

    page = max(1, int(page))
    size = min(max(1, int(size)), 100)
    offset = (page - 1) * size

    cols = [
        table.c.vod_id, table.c.type_id, table.c.type_name, table.c.name,
        table.c.pic, table.c.remarks, table.c.vod_year,
    ]
    base_where = []
    if type_id is not None:
        base_where.append(table.c.type_id == int(type_id))

    count_stmt = select(func.count()).select_from(table).where(*base_where)
    list_stmt = (
        select(*cols)
        .where(*base_where)
        .order_by(table.c.vod_time.desc(), table.c.vod_id.desc())
        .offset(offset)
        .limit(size)
    )
    with database.engine.connect() as conn:
        total = int(conn.execute(count_stmt).scalar() or 0)
        rows = [dict(r) for r in conn.execute(list_stmt).mappings().all()]
    return {"list": rows, "total": total, "page": page, "size": size}


def get_detail(slug: str, vod_id: str) -> Optional[Dict[str, Any]]:
    """精确取一条详情整行。"""
    slug = validate_slug(slug)
    table = build_vod_table(slug)
    if table.name not in existing_vod_tables():
        return None
    with database.engine.connect() as conn:
        row = conn.execute(
            select(table).where(table.c.vod_id == vod_id)
        ).mappings().first()
        return dict(row) if row else None


def suggestions(slug: str, keyword: str, limit: int = 10) -> List[str]:
    """名称前缀/包含匹配取标题（替代 Helios 首站搜索取 Title）。"""
    slug = validate_slug(slug)
    table = build_vod_table(slug)
    if table.name not in existing_vod_tables():
        return []
    pattern = keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    with database.engine.connect() as conn:
        rows = conn.execute(
            select(table.c.name)
            .where(table.c.name.like(pattern, escape="\\"))
            .limit(limit)
        ).scalars().all()
    return [str(r) for r in rows]


# ---------------------------------------------------------------------------
# 收藏 / 播放记录 / 搜索历史 / 直播源（读写 4 张专用表）
# ---------------------------------------------------------------------------
from .schema import (  # noqa: E402
    favorites, live_sources, play_records, search_history, live_playlist,
)


def _key_parts(key: str) -> Tuple[str, str]:
    """拆分 'source+source_id'；source_id 可能含 '+'，只按第一个 '+' 拆。"""
    if not key or "+" not in key:
        raise ValueError("Invalid key format. Expected format: source+source_id")
    source, source_id = key.split("+", 1)
    if not source or not source_id:
        raise ValueError("Invalid key format. Expected format: source+source_id")
    return source, source_id


def favorites_all() -> Dict[str, Dict[str, Any]]:
    with database.engine.connect() as conn:
        rows = conn.execute(select(favorites)).mappings().all()
    out = {}
    for r in rows:
        d = dict(r)
        out[f"{d['source']}+{d['source_id']}"] = d
    return out


def favorites_upsert(key: str, fav: Dict[str, Any]) -> None:
    source, source_id = _key_parts(key)
    values = {
        "source": source,
        "source_id": source_id,
        "source_name": str(fav.get("source_name") or ""),
        "total_episodes": int(fav.get("total_episodes") or 0),
        "title": str(fav.get("title") or ""),
        "year": str(fav.get("year") or ""),
        "cover": str(fav.get("cover") or ""),
        "save_time": int(fav.get("save_time") or 0),
        "search_title": str(fav.get("search_title") or ""),
    }
    stmt = database.dialect_insert(favorites).values(**values)
    stmt = stmt.on_conflict_do_update(
        index_elements=["source", "source_id"],
        set_={k: stmt.excluded[k] for k in values if k not in ("source", "source_id")},
    )
    with database.write_txn() as conn:
        conn.execute(stmt)


def favorites_delete(key: str) -> None:
    source, source_id = _key_parts(key)
    with database.write_txn() as conn:
        conn.execute(
            favorites.delete().where(
                (favorites.c.source == source) & (favorites.c.source_id == source_id)
            )
        )


def playrecords_all() -> Dict[str, Dict[str, Any]]:
    with database.engine.connect() as conn:
        rows = conn.execute(select(play_records)).mappings().all()
    out = {}
    for r in rows:
        d = dict(r)
        out[f"{d['source']}+{d['source_id']}"] = d
    return out


def playrecords_upsert(key: str, rec: Dict[str, Any]) -> None:
    source, source_id = _key_parts(key)
    values = {
        "source": source,
        "source_id": source_id,
        "source_name": str(rec.get("source_name") or ""),
        "title": str(rec.get("title") or ""),
        "cover": str(rec.get("cover") or ""),
        "year": str(rec.get("year") or ""),
        "index_number": int(rec.get("index") or rec.get("index_number") or 0),
        "total_episodes": int(rec.get("total_episodes") or 0),
        "play_time": int(rec.get("play_time") or 0),
        "total_time": int(rec.get("total_time") or 0),
        "save_time": int(rec.get("save_time") or 0),
        "search_title": str(rec.get("search_title") or ""),
    }
    stmt = database.dialect_insert(play_records).values(**values)
    stmt = stmt.on_conflict_do_update(
        index_elements=["source", "source_id"],
        set_={k: stmt.excluded[k] for k in values if k not in ("source", "source_id")},
    )
    with database.write_txn() as conn:
        conn.execute(stmt)


def playrecords_delete(key: Optional[str]) -> None:
    with database.write_txn() as conn:
        if not key:                       # 空 key = 删除全部（Helios 语义）
            conn.execute(play_records.delete())
        else:
            source, source_id = _key_parts(key)
            conn.execute(
                play_records.delete().where(
                    (play_records.c.source == source) & (play_records.c.source_id == source_id)
                )
            )


def history_get() -> List[str]:
    with database.engine.connect() as conn:
        row = conn.execute(
            select(search_history.c.record).where(search_history.c.id == 1)
        ).first()
    if not row or not row[0]:
        return []
    try:
        data = json.loads(row[0])
        return [str(x) for x in data] if isinstance(data, list) else []
    except Exception:  # noqa: BLE001
        return []


def history_update(keyword: str) -> List[str]:
    """去重并置顶，返回更新后的裸数组（Helios POST 返回更新后的数组）。"""
    current = [k for k in history_get() if k != keyword]
    current.insert(0, keyword)
    record = json.dumps(current, ensure_ascii=False)
    stmt = database.dialect_insert(search_history).values(id=1, record=record)
    stmt = stmt.on_conflict_do_update(index_elements=["id"], set_={"record": record})
    with database.write_txn() as conn:
        conn.execute(stmt)
    return current


def history_delete(keyword: Optional[str]) -> None:
    if not keyword:                       # 空 = 清空全部
        record = "[]"
    else:
        record = json.dumps([k for k in history_get() if k != keyword], ensure_ascii=False)
    stmt = database.dialect_insert(search_history).values(id=1, record=record)
    stmt = stmt.on_conflict_do_update(index_elements=["id"], set_={"record": record})
    with database.write_txn() as conn:
        conn.execute(stmt)


def live_sources_all() -> List[Dict[str, Any]]:
    with database.engine.connect() as conn:
        rows = conn.execute(
            select(live_sources).order_by(live_sources.c.sort_order)
        ).mappings().all()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# [直播源融合] iptv m3u 拉取缓存读写
# ---------------------------------------------------------------------------
def live_playlist_get(key: str) -> Optional[Dict[str, Any]]:
    with database.engine.connect() as conn:
        row = conn.execute(
            select(live_playlist).where(live_playlist.c.key == key)
        ).mappings().first()
    return dict(row) if row else None


def live_playlist_upsert(
    key: str, name: str, url: str, content: str, channel_count: int, fetched_at: int
) -> None:
    """幂等写入 m3u 缓存（ON CONFLICT DO UPDATE）。失败抛异常由调用方捕获。"""
    values = dict(
        key=key, name=name, url=url, content=content,
        channel_count=int(channel_count), fetched_at=int(fetched_at),
    )
    stmt = database.dialect_insert(live_playlist).values(**values)
    stmt = stmt.on_conflict_do_update(
        index_elements=["key"],
        set_={k: stmt.excluded[k] for k in values if k != "key"},
    )
    with database.write_txn() as conn:
        conn.execute(stmt)


def live_source_ensure(
    key: str, name: str, url: str, epg: str, ua: str, sort_order: int = 100
) -> None:
    """确保 /api/live/sources 里有这条订阅记录（存在则不覆盖人工修改）。"""
    existing = None
    with database.engine.connect() as conn:
        existing = conn.execute(
            select(live_sources).where(live_sources.c.key == key)
        ).mappings().first()
    if existing:
        return
    values = dict(
        key=key, name=name, url=url, epg=epg, ua=ua, disabled=0, sort_order=sort_order,
    )
    stmt = database.dialect_insert(live_sources).values(**values)
    stmt = stmt.on_conflict_do_nothing(index_elements=["key"])
    with database.write_txn() as conn:
        conn.execute(stmt)
