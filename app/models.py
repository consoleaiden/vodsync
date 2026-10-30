"""
数据模型（SQLAlchemy Core）。

表结构设计：
- sources     : 源站元数据（来自 latest.json 的站点 + 同步进度水位线）
- sync_logs   : 每次同步任务的执行记录
- vod_{slug}  : 每个源站一张独立数据表，只保留关键 Cast 字段

vod_{slug} 关键字段映射（苹果 CMS -> 本库）：
  vod_id       -> vod_id        (主键，源站唯一)
  type_id/name -> type_id, type_name (分类)
  vod_name     -> name          (影视名称)
  vod_pic      -> pic           (封面图)
  vod_remarks  -> remarks       (时长/备注，如 "HD国语" "更新至第10集")
  vod_content  -> content       (简介)
  vod_play_url -> play_url      (播放地址，原始格式: 线路$$$集$地址#集$地址)
  vod_time     -> vod_time      (更新时间字符串 "YYYY-MM-DD HH:MM:SS"，用于增量水位线)
"""
import hashlib
import re

from sqlalchemy import (
    Column,
    DateTime,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    func,
)

META = MetaData()

# ---------------------------------------------------------------------------
# 源站元数据表
# ---------------------------------------------------------------------------
sources = Table(
    "sources",
    META,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("name", String(128), nullable=False),          # 源站名称（latest.json name）
    Column("slug", String(64), nullable=False, unique=True),  # 表名后缀 / API 路径
    Column("api_url", String(512), nullable=False),       # 采集 API 地址
    Column("homepage", String(512), default=""),
    Column("description", String(512), default=""),
    Column("status", String(16), default="ok"),           # ok / down
    Column("enabled", Integer, default=1),                # 是否参与同步
    Column("total_videos", Integer, default=0),           # 本地已入库条数（定期刷新）
    # 增量水位线：vod_time 字符串（"YYYY-MM-DD HH:MM:SS" 按字典序可比）
    Column("watermark", String(32), default=""),
    # 全量同步断点：最后已完成的页码，0 表示无断点
    Column("last_page", Integer, default=0),
    # 全量同步总页数（用于管理面板显示进度百分比）
    Column("total_pages", Integer, default=0),
    # 单源站暂停标记：1=该源站的同步被单独暂停（不影响其他源站），0=正常
    Column("paused", Integer, default=0),
    Column("paused_at", DateTime),                        # 单源站暂停时间（便于审计/展示）
    # 自动同步开关：1=参与定时任务与全站同步，0=排除在外（手动单站同步不受影响）
    Column("auto_sync", Integer, default=1, server_default="1"),
    Column("last_sync_at", DateTime),                     # 最近一次成功同步时间
    Column("created_at", DateTime, server_default=func.now()),
    Column("updated_at", DateTime, server_default=func.now(), onupdate=func.now()),
)

# ---------------------------------------------------------------------------
# 同步日志表
# ---------------------------------------------------------------------------
sync_logs = Table(
    "sync_logs",
    META,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("slug", String(64), nullable=False),
    Column("mode", String(16), nullable=False),           # full / incremental / refresh
    Column("status", String(16), default="running"),      # running / success / failed
    Column("fetched", Integer, default=0),                # 抓取条数
    Column("inserted", Integer, default=0),               # 新增条数
    Column("updated", Integer, default=0),                # 更新条数
    Column("pages", Integer, default=0),
    Column("error", Text, default=""),
    Column("started_at", DateTime, server_default=func.now()),
    Column("finished_at", DateTime),
)

# ---------------------------------------------------------------------------
# vod_{slug} 动态表缓存：避免重复构建 Table 对象
# ---------------------------------------------------------------------------
_vod_table_cache: dict = {}


def slugify(name: str) -> str:
    """
    由源站名称生成安全的表名后缀 / URL slug。
    规则：保留 ASCII 字母数字，非 ASCII（中文等）转成短哈希，保证稳定且合法。
    示例："暴风资源" -> "s3f2a91c0d4"；"Bfzy Tv" -> "bfzytv"
    """
    ascii_part = re.sub(r"[^a-zA-Z0-9]+", "", name or "").lower()
    if ascii_part:
        return ascii_part[:48]
    digest = hashlib.md5((name or "").encode("utf-8")).hexdigest()[:10]
    return f"s{digest}"


def build_vod_table(slug: str) -> Table:
    """
    构建并缓存某个源站的数据表定义（vod_{slug}）。
    建表语句由 syncer.ensure_table() 通过 CREATE TABLE IF NOT EXISTS 执行。
    """
    if slug in _vod_table_cache:
        return _vod_table_cache[slug]
    table = Table(
        f"vod_{slug}",
        META,
        Column("vod_id", Integer, primary_key=True, autoincrement=False),
        Column("type_id", Integer, default=0),
        Column("type_name", String(64), default=""),
        Column("name", String(255), nullable=False),
        Column("pic", String(1024), default=""),
        Column("remarks", String(128), default=""),
        Column("content", Text, default=""),
        Column("play_url", Text, default=""),
        Column("vod_time", String(32), default=""),
        Column("synced_at", DateTime, server_default=func.now()),
        UniqueConstraint("vod_id", name=f"uk_vod_{slug}"),
    )
    # 常用查询索引：按分类、按名称模糊搜索、按更新时间排序
    Index(f"idx_{slug}_type", table.c.type_id)
    Index(f"idx_{slug}_time", table.c.vod_time.desc())
    Index(f"idx_{slug}_name", table.c.name)
    _vod_table_cache[slug] = table
    return table
