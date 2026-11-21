"""
SeleneTV 专用 API —— 独立数据表定义。

设计约束（用户明确要求）：
  1. 只在现有数据库里【新增】表，不删表、不删字段、不改 vodsync 任何现有表。
  2. 使用【独立的 MetaData】(selenetv_meta)，绝不与 vodsync 主 META 合并 ——
     这样 vodsync 的 init_db()/META.create_all()/_migrate() 完全感知不到本模块，
     本模块的下线（删掉 app/selenetv）也不会留下任何对现有表的引用。
  3. 表名统一 selenetv_ 前缀，避免与 vodsync 现有表（sources/settings/vod_* 等）冲突。
  4. 建表用 create_all(checkfirst=True)，幂等；已有表一律跳过，不 ALTER、不覆盖。

字段命名与 Helios 的 SQLite 表逐字对齐（见 Helios database/database.go），
便于 SeleneTV 客户端零改动切换后端：
  - favorites  : source, source_id, source_name, total_episodes, title, year,
                 cover, save_time, search_title
  - play_records: 上述 + index_number, play_time, total_time
  - search_history: 单行 id=1，record = JSON 数组字符串
  - live_sources: key, name, url, epg, ua, disabled
  - live_playlist: key, name, url, content, channel_count, fetched_at（iptv 拉取缓存，只增）
  - users       : id, username, password_hash, role, allow_adult, enabled（用户管理，只增）
"""
from sqlalchemy import (
    BigInteger,
    Column,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
)

# 独立元数据：与 vodsync.models.META 完全隔离
selenetv_meta = MetaData()

# ---------------------------------------------------------------------------
# 收藏（对应 Helios favorites 表 / GET,POST,DELETE /api/favorites）
# 主键 (source, source_id) 与 Helios 唯一索引等价；save_time 用 BIGINT 存 Unix 毫秒
# （Helios int64；SeleneTV 写入的就是毫秒，原样存原样回，不做单位转换）
# ---------------------------------------------------------------------------
favorites = Table(
    "selenetv_favorites",
    selenetv_meta,
    Column("source", String(64), nullable=False, comment="源站标识（sources.slug）"),
    Column("source_id", String(64), nullable=False, comment="影片 ID（vod_id）"),
    Column("source_name", String(128), nullable=False, default="", server_default=""),
    Column("total_episodes", Integer, nullable=False, default=0, server_default="0"),
    Column("title", String(255), nullable=False, default="", server_default=""),
    Column("year", String(16), nullable=False, default="", server_default=""),
    Column("cover", String(1024), nullable=False, default="", server_default=""),
    Column("save_time", BigInteger, nullable=False, default=0, server_default="0"),
    Column("search_title", String(255), nullable=False, default="", server_default=""),
    Index("ix_selenetv_favorites_src", "source", "source_id", unique=True),
)

# ---------------------------------------------------------------------------
# 播放记录（对应 Helios play_records 表 / GET,POST,DELETE /api/playrecords）
# 注意 index_number 是 DB 列名，对外 JSON 名必须是 "index"（见 serializers.py）
# ---------------------------------------------------------------------------
play_records = Table(
    "selenetv_play_records",
    selenetv_meta,
    Column("source", String(64), nullable=False),
    Column("source_id", String(64), nullable=False),
    Column("source_name", String(128), nullable=False, default="", server_default=""),
    Column("title", String(255), nullable=False, default="", server_default=""),
    Column("cover", String(1024), nullable=False, default="", server_default=""),
    Column("year", String(16), nullable=False, default="", server_default=""),
    Column("index_number", Integer, nullable=False, default=0, server_default="0"),
    Column("total_episodes", Integer, nullable=False, default=0, server_default="0"),
    Column("play_time", Integer, nullable=False, default=0, server_default="0"),
    Column("total_time", Integer, nullable=False, default=0, server_default="0"),
    Column("save_time", BigInteger, nullable=False, default=0, server_default="0"),
    Column("search_title", String(255), nullable=False, default="", server_default=""),
    Index("ix_selenetv_play_records_src", "source", "source_id", unique=True),
)

# ---------------------------------------------------------------------------
# 搜索历史（对应 Helios search_history 表 / GET,POST,DELETE /api/searchhistory）
# 完全复刻 Helios 的单行 JSON 方案：id=1 一行，record 存整个关键词 JSON 数组。
# ---------------------------------------------------------------------------
search_history = Table(
    "selenetv_search_history",
    selenetv_meta,
    Column("id", Integer, primary_key=True),
    Column("record", Text, nullable=False, default="[]", server_default="[]"),
)

# ---------------------------------------------------------------------------
# 直播源（对应 Helios Config.Lives / GET /api/live/sources）
# Helios 从订阅配置 JSON 读；这里落库便于远程管理，对外字段保持一致。
# ---------------------------------------------------------------------------
live_sources = Table(
    "selenetv_live_sources",
    selenetv_meta,
    Column("key", String(64), primary_key=True, comment="频道标识"),
    Column("name", String(128), nullable=False, default="", server_default=""),
    Column("url", String(1024), nullable=False, default="", server_default=""),
    Column("epg", String(512), nullable=False, default="", server_default=""),
    Column("ua", String(512), nullable=False, default="", server_default=""),
    Column("disabled", Integer, nullable=False, default=0, server_default="0"),
    Column("sort_order", Integer, nullable=False, default=0, server_default="0"),
)

# ---------------------------------------------------------------------------
# [直播源融合] iptv 拉取缓存表：存 vbskycn/iptv 的 m3u 原文，供本地端点分发。
# 只增表：不改动上面任何既有表/字段。
# ---------------------------------------------------------------------------
live_playlist = Table(
    "selenetv_live_playlist",
    selenetv_meta,
    Column("key", String(64), primary_key=True, comment="直播源标识，种子 iptv4"),
    Column("name", String(128), nullable=False, default="", server_default=""),
    Column("url", String(1024), nullable=False, default="", server_default="", comment="上游原始地址(溯源)"),
    Column("content", Text, nullable=False, default="", server_default="", comment="M3U 原文"),
    Column("channel_count", Integer, nullable=False, default=0, server_default="0", comment="频道数(#EXTINF)"),
    Column("fetched_at", BigInteger, nullable=False, default=0, server_default="0", comment="成功拉取Unix毫秒"),
)

# ---------------------------------------------------------------------------
# [用户管理] SeleneTV 专用用户表：支持面板增删改查 + 每用户 18+ 访问权限。
# 只增表：不改动上面任何既有表/字段；密码只存 PBKDF2 hash（复用 vodsync.auth 格式）。
# ---------------------------------------------------------------------------
users = Table(
    "selenetv_users",
    selenetv_meta,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("username", String(64), nullable=False, unique=True, comment="登录名"),
    Column("password_hash", String(256), nullable=False, default="", server_default="", comment="PBKDF2 hash，不存明文"),
    Column("role", String(16), nullable=False, default="user", server_default="user", comment="owner/user，仅标记"),
    Column("allow_adult", Integer, nullable=False, default=0, server_default="0", comment="1=可访问18+源，0=禁止"),
    Column("enabled", Integer, nullable=False, default=1, server_default="1", comment="1=可登录"),
    Column("created_at", BigInteger, nullable=False, default=0, server_default="0"),
    Column("updated_at", BigInteger, nullable=False, default=0, server_default="0"),
)

# 对外暴露的表名清单（供自检/文档用）
TABLE_NAMES = tuple(
    t.name for t in (
        favorites, play_records, search_history, live_sources, live_playlist, users,
    )
)
