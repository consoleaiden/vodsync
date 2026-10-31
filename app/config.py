"""
全局配置：所有配置项均可通过环境变量覆盖（见 .env.example）。
"""
import os

# ---------------------------------------------------------------------------
# 目录与数据库
# ---------------------------------------------------------------------------
# 数据目录（SQLite 文件、日志等）。Docker 中挂载 /app/data
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(__file__), "..", "data"))
os.makedirs(DATA_DIR, exist_ok=True)

# 数据库连接：
#   SQLite（默认）: sqlite:///data/vodsync.db
#   PostgreSQL    : postgresql+psycopg2://user:pass@host:5432/vodsync
DATABASE_URL = os.environ.get("DATABASE_URL", f"sqlite:///{os.path.join(DATA_DIR, 'vodsync.db')}")

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

# ---------------------------------------------------------------------------
# 数据库连接池
#
# 背景：SQLite 只允许一个写者，并发写会互相等锁；每个等锁的线程都占着一个池连接，
#      池被占满后所有请求（含 /healthz）都会 30s 超时报错。经验：
#      「限并发写」比「放大连接池」有效，下面的池大小只作为安全余量。
# ---------------------------------------------------------------------------
DB_POOL_SIZE = int(os.environ.get("DB_POOL_SIZE", "10"))
DB_MAX_OVERFLOW = int(os.environ.get("DB_MAX_OVERFLOW", "20"))
# 等连接的超时（秒）。小于 SQLite busy_timeout，宁可快速失败也不长时间挂住请求
DB_POOL_TIMEOUT = int(os.environ.get("DB_POOL_TIMEOUT", "10"))

# 聚合搜索的并行度（每个线程各占一个数据库连接）
SEARCH_CONCURRENCY = int(os.environ.get("SEARCH_CONCURRENCY", "4"))

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
API_PAGE_SIZE_DEFAULT = int(os.environ.get("API_PAGE_SIZE_DEFAULT", "20"))
API_PAGE_SIZE_MAX = int(os.environ.get("API_PAGE_SIZE_MAX", "100"))

# API 访问密钥（可选）。设置后，所有接口需要在 Header 携带 X-API-Key: <key> 或 ?apikey=<key>
API_KEY = os.environ.get("API_KEY", "").strip()
