"""
全局配置：所有配置项均可通过环境变量覆盖（见 .env.example）。
"""
import os

# ---------------------------------------------------------------------------
# 目录与数据库
# ---------------------------------------------------------------------------
# 数据目录（断点状态 pause_state.json、日志等）。Docker 中挂载 /app/data
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(__file__), "..", "data"))
os.makedirs(DATA_DIR, exist_ok=True)

# **生产统一使用 PostgreSQL**（v1.7.0 起，SQLite 已不再作为部署选项）。
# 默认值对应 docker-compose.yml 里的 postgres 服务；部署时用环境变量覆盖即可：
#   DATABASE_URL=postgresql+psycopg2://user:pass@host:5432/dbname
#
# SQLite 驱动代码保留，但**只服务自动化测试**（scripts/test_*.py 显式指向临时文件），
# 生产路径上的所有优化（连接池、并行查询、VACUUM 等）都按 PG 设计。
DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql+psycopg2://vodsync:vodsync@postgres:5432/vodsync"
)

# ---------------------------------------------------------------------------
# 数据源
# ---------------------------------------------------------------------------
# 资源站列表（ziyuanzu 监测数据）
LATEST_JSON_URL = os.environ.get(
    "LATEST_JSON_URL", "https://mylazily.github.io/ziyuanzhan/data/latest.json"
)
# 仅同步 status == "ok" 的源站
ONLY_OK_SOURCES = os.environ.get("ONLY_OK_SOURCES", "true").lower() == "true"
# 首次发现新源站时是否立即执行全量同步（否则等待下一个定时周期）
FULL_SYNC_ON_DISCOVER = os.environ.get("FULL_SYNC_ON_DISCOVER", "true").lower() == "true"

# ---------------------------------------------------------------------------
# 同步调度
# ---------------------------------------------------------------------------
# 源站列表刷新间隔（分钟），默认 12 小时
SOURCE_REFRESH_INTERVAL_MINUTES = int(os.environ.get("SOURCE_REFRESH_INTERVAL_MINUTES", "720"))
# 增量同步间隔（分钟），默认 6 小时
SYNC_INTERVAL_MINUTES = int(os.environ.get("SYNC_INTERVAL_MINUTES", "360"))
# 同时同步的源站数量（并行源站数）
SOURCE_SYNC_CONCURRENCY = int(os.environ.get("SOURCE_SYNC_CONCURRENCY", "2"))
# 单个源站内并行抓取的页数/请求数
PAGE_CONCURRENCY = int(os.environ.get("PAGE_CONCURRENCY", "4"))
# 每次请求之间的最小间隔（秒），对源站限速防封
REQUEST_INTERVAL_SECONDS = float(os.environ.get("REQUEST_INTERVAL_SECONDS", "0.2"))
# 请求超时（秒）
REQUEST_TIMEOUT = int(os.environ.get("REQUEST_TIMEOUT", "20"))
# 请求失败重试次数
REQUEST_RETRIES = int(os.environ.get("REQUEST_RETRIES", "3"))
# 请求 UA
USER_AGENT = os.environ.get(
    "USER_AGENT", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) VodSync/1.0"
)
# 增量同步时是否额外检测"漏抓"的 ID（更慢但更稳，源站数据被重置时可打开）
INCR_CHECK_MISSING = os.environ.get("INCR_CHECK_MISSING", "false").lower() == "true"
# 全量同步断点保存间隔（每 N 页保存一次进度）
CHECKPOINT_EVERY_PAGES = int(os.environ.get("CHECKPOINT_EVERY_PAGES", "5"))
# 单页抓取的最大尝试次数。超过即中止本次全量（保留断点），避免对已挂的源站空转。
# 注意：这是「同一页在本次运行内」的计数，重启后重新计。
PAGE_MAX_ATTEMPTS = int(os.environ.get("PAGE_MAX_ATTEMPTS", "3"))

# ---------------------------------------------------------------------------
# 重启恢复（断电 / 崩溃 / docker restart 之后）
#
# 数据安全本来就有保障（每页一个事务 + 断点存库 + UPSERT 幂等），但「自动接着跑」
# 需要显式开启：启动时扫描 last_page > 0 的源站并延迟续跑。
#   - 延迟是为了让服务先完成建库/校准条数/启动调度，别在启动瞬间抢连接
#   - 关掉它就回到旧行为：等下一个调度周期（默认 6h）或手动点「继续」
# ---------------------------------------------------------------------------
AUTO_RESUME_ON_BOOT = os.environ.get("AUTO_RESUME_ON_BOOT", "true").lower() == "true"
AUTO_RESUME_DELAY_SECONDS = int(os.environ.get("AUTO_RESUME_DELAY_SECONDS", "30"))

# ---------------------------------------------------------------------------
# 数据库连接池（PostgreSQL）
#
# 池大小要盖住「API 并发 + 同步线程 + 多源归并查询」的总和：默认 10+20 富余。
# 历史上 SQLite 时代这里是为了躲写锁（池放大只会让等锁的线程更多），
# 迁移到 PG 后限制的目的变成「别把 NAS 的 PG 连接数打满」，思路一致。
# ---------------------------------------------------------------------------
DB_POOL_SIZE = int(os.environ.get("DB_POOL_SIZE", "10"))
DB_MAX_OVERFLOW = int(os.environ.get("DB_MAX_OVERFLOW", "20"))
# 等连接的超时（秒）。宁可快速返回 503，也不让请求长时间挂住
DB_POOL_TIMEOUT = int(os.environ.get("DB_POOL_TIMEOUT", "10"))

# 聚合搜索的并行度（每个线程各占一个数据库连接）
SEARCH_CONCURRENCY = int(os.environ.get("SEARCH_CONCURRENCY", "4"))

# ---------------------------------------------------------------------------
# 多源列表查询（/api/v1/videos 不带 source 时）
#
# 旧做法：52 张表 UNION ALL 成子查询，外面套 count(*) + ORDER BY ... OFFSET。
#         220 万行每次请求全量物化排序，首页 4~8s、深页 10s+，并发两个就打死 IO。
# 新做法：每张表各取自己的 top-K（走 idx_*_time 索引倒序扫描），进程内多路归并。
#         全局第 N 行必然落在它所属表的前 N 行内，所以结果是精确的，不是近似。
# ---------------------------------------------------------------------------
# 每个表最多取多少行参与归并。= 能精确支持的「最深一页」× 每页条数，
# 超过即回退 UNION 全量路径（慢但正确，正常用户翻不到那么深）。
MERGE_MAX_PER_TABLE = int(os.environ.get("MERGE_MAX_PER_TABLE", "3000"))
# 归并查询的线程数，每个线程占一个数据库连接（别超过 DB_POOL_SIZE）
MERGE_CONCURRENCY = int(os.environ.get("MERGE_CONCURRENCY", "4"))
# 无筛选条件时 total 的缓存秒数。total 只用于算 pagecount/展示，
# 后台同步写入会造成最多这么多秒的偏差，属可接受（每次请求都全表 count 才是问题）。
TOTAL_CACHE_SECONDS = int(os.environ.get("TOTAL_CACHE_SECONDS", "60"))

# ---------------------------------------------------------------------------
# 数据维护（sync_logs 清理 / 测试后端的 WAL checkpoint）
#
# sync_logs 每个源站每次同步写一行（52 源站 × 每天多次），不清就一直涨；
# 面板只读最近几十条，历史全留没有意义。
# ---------------------------------------------------------------------------
HOUSEKEEPING_ENABLED = os.environ.get("HOUSEKEEPING_ENABLED", "true").lower() == "true"
HOUSEKEEPING_INTERVAL_MINUTES = int(os.environ.get("HOUSEKEEPING_INTERVAL_MINUTES", "60"))
# 保留最近多少天的同步日志（running 状态的行永不清理）—— 主策略
SYNC_LOG_RETENTION_DAYS = int(os.environ.get("SYNC_LOG_RETENTION_DAYS", "30"))
# 每个源站最多保留多少条（兜底：每 6 小时同步一次的站，30 天也能堆 120 条）
SYNC_LOG_KEEP_PER_SOURCE = int(os.environ.get("SYNC_LOG_KEEP_PER_SOURCE", "50"))
# 全表行数硬上限（防止保留策略失效时无限膨胀）
SYNC_LOG_MAX_ROWS = int(os.environ.get("SYNC_LOG_MAX_ROWS", "20000"))
# 仅测试后端（SQLite）有意义：WAL 超过这个大小做一次 TRUNCATE checkpoint（MB）。
# PostgreSQL 下该配置无效，checkpoint 会自动跳过。
WAL_CHECKPOINT_MB = int(os.environ.get("WAL_CHECKPOINT_MB", "128"))

# ---------------------------------------------------------------------------
# 中转代理：上游源站不可直连时的降级通道
#
# 代理约定：{proxy}?url=<urlencode(目标地址)>
#   例：https://pz.v88.qzz.io/?url=https%3A%2F%2Fapi.example.com%2Fapi.php%3Fac%3Dlist
# 策略：直连优先 → 失败自动按顺序切代理；某源站走代理成功后记忆 30 分钟，
#      期间直接走该代理（避免每次先等直连超时），到期后重新尝试直连。
# ---------------------------------------------------------------------------
PROXY_ENABLED = os.environ.get("PROXY_ENABLED", "true").lower() == "true"
PROXY_URLS = [
    u.strip()
    for u in os.environ.get(
        "PROXY_URLS",
        "https://pz.v88.qzz.io/,https://mconfig.viptv.work/",
    ).split(",")
    if u.strip()
]
# 走代理时的单请求超时（秒）
PROXY_TIMEOUT = int(os.environ.get("PROXY_TIMEOUT", "20"))
# 每个代理的最大并发请求数（避免把中转服务压垮）
PROXY_CONCURRENCY = int(os.environ.get("PROXY_CONCURRENCY", "4"))
# "该源站需走代理"的记忆有效期（分钟），到期重新尝试直连
PROXY_PREF_TTL_MINUTES = int(os.environ.get("PROXY_PREF_TTL_MINUTES", "30"))

# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
# 每页条数默认值：30 是「三列宫格 × 10 行」的整数倍，App 列表用它一页正好整行，
# 不会出现最后一页只剩 1~2 格 + 空白的观感问题。上限 100 是为了避免单次响应过大。
API_PAGE_SIZE_DEFAULT = int(os.environ.get("API_PAGE_SIZE_DEFAULT", "30"))
API_PAGE_SIZE_MAX = int(os.environ.get("API_PAGE_SIZE_MAX", "100"))

# API 访问密钥（可选）。设置后，所有接口需要在 Header 携带 X-API-Key: <key> 或 ?apikey=<key>
# v1.5.0 起改由「访问控制」统一管理（面板里可生成/重置 Token），此项仍兼容：
# 设置了就作为一枚额外有效的 Token，适合不想改客户端的环境变量部署。
API_KEY = os.environ.get("API_KEY", "").strip()

# ---------------------------------------------------------------------------
# 访问控制（v1.5.0）
#
# - 面板：需要登录密码才能进入，登录后可在「设置」改密码、开关鉴权、生成 Token
# - API ：启用后必须带 Authorization: Bearer <token>（或 X-API-Key / ?token=）
# 上面的值只是**首次初始化时的种子**，之后以数据库 settings 表为准（面板可改）。
# ---------------------------------------------------------------------------
AUTH_ENABLED = os.environ.get("AUTH_ENABLED", "true").lower() == "true"
# 首次启动写入的管理面板密码（务必登录后改掉）
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "666666").strip() or "666666"
# 登录会话有效期（天）
SESSION_TTL_DAYS = int(os.environ.get("SESSION_TTL_DAYS", "7"))
# 登录失败锁定：连续失败 N 次锁 M 分钟（防六位默认密码被爆破）
LOGIN_MAX_FAILS = int(os.environ.get("LOGIN_MAX_FAILS", "5"))
LOGIN_LOCK_MINUTES = int(os.environ.get("LOGIN_LOCK_MINUTES", "5"))
# 是否信任反向代理写入的 X-Forwarded-For。
#
# 默认 false：只看 TCP 连接的对端地址。为什么默认关——XFF 是客户端可以自己伪造的头，
# 无条件相信它，任何人都能用「每次换一个假 IP」绕过登录失败锁定（六位默认密码等于裸奔）。
# 只有确实部署在 Nginx / Traefik / 群晖反代之后（且反代会覆盖 XFF）才打开。
# 打开后取 XFF 的**最后一跳**（信任的代理追加的那一段），而不是第一段。
TRUST_PROXY = os.environ.get("TRUST_PROXY", "false").lower() == "true"
