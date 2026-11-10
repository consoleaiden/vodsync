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
import threading

from sqlalchemy import (
    Column,
    DateTime,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    func,
)

META = MetaData()

# ---------------------------------------------------------------------------
# 全局设置表（key/value）
#
# 存访问控制相关的少量状态：是否启用鉴权、面板密码哈希、API Token、会话签名密钥。
# 用 key/value 而不是固定列，是为了以后加配置项不用改表结构（也不引入 alembic）。
# ---------------------------------------------------------------------------
settings = Table(
    "settings",
    META,
    Column("key", String(64), primary_key=True),
    Column("value", Text, default=""),
    Column("updated_at", DateTime, server_default=func.now()),
)

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
    # 来源：latest=从 latest.json 自动发现；custom=面板手动添加
    # refresh_sources 不会覆盖 custom 行的元信息（名字/描述归用户所有），
    # 也永不删除 custom 行 —— 这是「自定义源持续存在」的实现基础。
    Column("origin", String(16), default="latest", server_default="latest"),
    # ---- 健康度退避（v1.8.1）----
    # 连续「大面积失败」的次数（成功率 <50% 的 incomplete / 整轮 failed）。
    # 成功一次即清零。用于把持续限流/宕机的源站按指数退避，而不是每轮都捶一遍。
    Column("fail_streak", Integer, default=0, server_default="0"),
    # 退避截止时间：调度器跳过 skip_until > now 的源站（手动触发不受限）。
    Column("skip_until", DateTime),
    # ---- 播放线路策略覆盖（v1.11.0）----
    # inherit  跟随全局 PLAY_LINE_POLICY（默认，其他站不受影响）
    # off      该站不过滤（源站线路本来就是直链，或用户想原样下发）
    # filter   该站按黑名单剔除 page 型线路
    # whitelist 该站只保留媒体直链
    # 为什么按源站配：只有「第 1 条线路恒为 /share/ 分享页」这类站点需要过滤，
    # 一刀切的全局策略会误伤本来正常的站。
    Column("play_line_policy", String(16), default="inherit", server_default="inherit"),
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
# 明星词库（v1.9.0 可选增强）
#
# 用户维护的主演名单：这些名字常出现在缺失演员字段视频的标题/简介里。
# 预计算时扫描 标题+简介 命中词库 → 推断为演员信号（带降权系数，防「提及≠主演」误伤）。
# 面板设置页提供增删改；修改后需触发推荐重算才生效（每日校准也会兜底）。
# ---------------------------------------------------------------------------
star_terms = Table(
    "star_terms",
    META,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("term", String(64), nullable=False, unique=True),   # 演员名（去重）
    Column("note", String(255), default=""),                   # 备注（可空）
    # 别名（v1.9.2）：同一明星的多译名/写法，逗号分隔。别名参与匹配（标题扫描、
    # 演员交集），但命中后统一归一到主名 —— star_map.term / vod_tag 只存主名，
    # 避免「同演员多写法」把作品拆到多个明星名下。
    Column("aliases", String(512), default=""),
    Column("enabled", Integer, default=1, server_default="1"), # 停用后不参与推断
    Column("created_at", DateTime, server_default=func.now()),
)

# ---------------------------------------------------------------------------
# 相关推荐预计算表（v1.9.0）
#
# 每个源站独立计算；related_json 只存精简引用（vod_id + score + 兜底标记），
# 卡片字段（封面/备注/分类）读取时按 id 实时补齐 —— 好处：
#   1. JSON 最小，写入快（全量重算 220 万行也只写 id）；
#   2. 封面/备注更新后推荐卡片自动跟着新，不会出现「推荐位上是旧图」；
#   3. 推荐条目被源站下架时，读取时补齐查不到 → 自然从列表里消失，无需清理。
# algo_version 用于算法升级：版本变了启动时清空旧结果，全量重算。
# ---------------------------------------------------------------------------
vod_related = Table(
    "vod_related",
    META,
    Column("slug", String(64), primary_key=True),
    Column("vod_id", Integer, primary_key=True),
    Column("related_json", Text, default=""),
    Column("computed_at", DateTime, server_default=func.now()),
    Column("algo_version", String(16), default=""),
)

# ---------------------------------------------------------------------------
# 明星-视频倒排映射表（v1.9.1 明星作品推荐）
#
# 词库明星 → 视频的反向索引：明星相关视频 API 直接查这张表的 (slug, term)
# 索引拿候选，再回 vod_{slug} 表补卡片字段 —— 绝不对大表做 LIKE '%吴京%' 全表扫。
# src 标记信号来源（客户端可展示可信度）：
#   actor  视频自带真实演员且演员名在词库中（高可信）
#   title  缺演员视频，标题命中词库推断（「提及≠主演」，中等可信）
# ---------------------------------------------------------------------------
star_map = Table(
    "star_map",
    META,
    Column("slug", String(64), primary_key=True),
    Column("vod_id", Integer, primary_key=True),
    Column("term", String(64), primary_key=True),
    Column("src", String(8), default="title"),
    Column("computed_at", DateTime, server_default=func.now()),
)
# 主键前缀只覆盖 (slug, vod_id) 查「某视频的明星」；按明星找视频需要这把索引
Index("idx_star_map_term", star_map.c.slug, star_map.c.term)

# ---------------------------------------------------------------------------
# 标签倒排表（v1.14.0 / B-01）
#
# 详情页点标签胶囊 -> 「同一标签的影片」，必须**分词后精确匹配**：把 vod_tag
# 按 , ， / | 、 切开，逐段归一化（去空白、ASCII 小写）后存一行。
# 直接用 `vod_tag LIKE '%悬疑%'` 会把「悬疑喜剧」「反悬疑」一起搜出来，语义错。
#
# 为什么不放进 vod_{slug} 表：一个视频有多个标签，塞进主表要么存 JSON 无法走
# 索引，要么拆成多行破坏主键。独立关联表 + (source, tag_norm) 复合索引是标准解。
# 主键 (source, vod_id, tag_norm) 同时承担「某视频的全部标签」查询。
# ---------------------------------------------------------------------------
video_tags = Table(
    "video_tags",
    META,
    Column("source", String(64), primary_key=True),     # 源站 slug
    Column("vod_id", Integer, primary_key=True),
    Column("tag_norm", String(64), primary_key=True),   # 归一化后的标签
)
# 按标签找影片走这把：WHERE source IN (...) AND tag_norm = ?
# （主键是 (source, vod_id, tag_norm)，tag_norm 在第三列，无法直接支撑 tag 前缀检索）
Index("idx_video_tags_tag", video_tags.c.tag_norm, video_tags.c.source)

# ---------------------------------------------------------------------------
# 标签索引回填进度（v1.14.0）
#
# 白万级行的回填必须可断点续跑、可观测：cursor 是已处理到的最大 vod_id，
# 重启后从 cursor 继续，不会从头再扫一遍。status 用于能力声明：
# 所有启用源站都 done 才认为索引就绪（才在 /healthz 里声明 tag_search）。
# ---------------------------------------------------------------------------
tag_index_state = Table(
    "tag_index_state",
    META,
    Column("slug", String(64), primary_key=True),
    # cursor 是**上游 vod_id 水位**，供断点续跑用（WHERE vod_id > cursor）。
    # 注意：它绝对不能拿来算进度 —— 上游 vod_id 稀疏且可能从几万开始
    # （实测 109567 号只对应 7.4 万条数据），cursor/total 会算出 147%。
    Column("cursor", Integer, default=0),
    # processed 是**已处理条数**，与 total 同量纲，进度只能用这个算（v1.14.1 修）
    Column("processed", Integer, default=0, server_default="0"),
    Column("indexed", Integer, default=0),                # 已写入的 (视频,标签) 行数
    Column("total", Integer, default=0),                  # 该源站总行数（进度分母）
    Column("status", String(16), default="pending"),      # pending/running/done/failed
    Column("error", Text, default=""),
    Column("started_at", DateTime),
    Column("finished_at", DateTime),
    Column("updated_at", DateTime, server_default=func.now()),
)

# ---------------------------------------------------------------------------
# 多 Token 与权限粒度（v1.14.0 / B-08）
#
# 存量单 Token 仍在 settings 里（api_token），这里只存**新增**的具名 Token。
# 只存 sha256 摘要：数据库被读走也不等于凭据泄露（与面板密码同样处理）。
# 两者并存是刻意的：老部署只有 settings 那一枚，升级后继续可用；
# 想给 App 发一枚只读 Token 时再在面板/接口里新建，不需要轮换现有 Token。
# ---------------------------------------------------------------------------
api_tokens = Table(
    "api_tokens",
    META,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("token_hash", String(64), nullable=False, unique=True),  # sha256 hex
    Column("scope", String(16), default="read", server_default="read"),  # read / admin
    Column("note", String(128), default=""),              # 用途备注，便于轮换时不误删
    Column("created_at", DateTime, server_default=func.now()),
    Column("last_used_at", DateTime),
    Column("revoked", Integer, default=0, server_default="0"),
)

# ---------------------------------------------------------------------------
# vod_{slug} 动态表缓存：避免重复构建 Table 对象
# ---------------------------------------------------------------------------
_vod_table_cache: dict = {}
# v1.10.0 / C5：同步线程与 API 线程可能同时首次访问同一个新表，两个线程都走
# cache miss 时会对同一个名字构造两次 Table —— SQLAlchemy 的 META 里同名注册
# 直接抛 InvalidRequestError（偶发 500）。用一把进程内锁包住 cache miss 分支。
_vod_table_lock = threading.Lock()


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
    cached = _vod_table_cache.get(slug)
    if cached is not None:
        return cached
    with _vod_table_lock:
        cached = _vod_table_cache.get(slug)
        if cached is not None:
            return cached
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
            # ---- 相关推荐信号字段（v1.9.0）----
            # 抓取阶段已做占位词清洗（"内详"/"未知"/"暂无" 等存为空串）。
            # 演职员以逗号分隔存储；老库通过 database._migrate() 自动补列。
            Column("vod_actor", String(512), default=""),
            Column("vod_director", String(255), default=""),
            Column("vod_writer", String(255), default=""),
            Column("vod_area", String(64), default=""),
            Column("vod_year", String(16), default=""),
            Column("vod_tag", String(255), default=""),
            # ---- 播放线路来源（v1.11.0）----
            # 上游 vod_play_from 原样保存（"feifan$$$ffm3u8"，与 play_url 的线路顺序一一对应）。
            # 这是「哪条线路是可播直链」的唯一权威信息：旧实现入库时把它丢了，只能靠 URL 形态猜。
            # 存量行该列为空串 —— 读路径必须容忍（play_from 空 → 只按 URL 形态判断）。
            Column("play_from", Text, default=""),
            Column("vod_time", String(32), default=""),
            Column("synced_at", DateTime, server_default=func.now()),
            # v1.10.0 / C14：这里原来还有一个 UniqueConstraint("vod_id")，
            # 与主键完全重复（多一个唯一索引 = 每次写入多一次索引维护），已删除。
        )
        # 常用查询索引：按分类、按名称模糊搜索、按更新时间排序
        Index(f"idx_{slug}_type", table.c.type_id)
        Index(f"idx_{slug}_time", table.c.vod_time.desc())
        Index(f"idx_{slug}_name", table.c.name)
        _vod_table_cache[slug] = table
        return table


def forget_vod_table(slug: str):
    """
    丢弃某个 slug 的 Table 对象缓存（删除源站后调用）。

    必须同时从 META 里移除：SQLAlchemy 的 Table 构造时会把名字注册进
    `metadata.tables`，同名再构造一次直接抛 InvalidRequestError。只清
    `_vod_table_cache` 的话，删掉源站再用同一个 slug 添加新源站就会 500。
    """
    table = _vod_table_cache.pop(slug, None)
    if table is not None and table.key in META.tables:
        META.remove(table)
