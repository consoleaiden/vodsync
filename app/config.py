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
# 增量索引阶段失败页重试之间的等待（秒）。给限流的源站一点喘息，重试成功率明显更高。
PAGE_RETRY_SLEEP_SECONDS = float(os.environ.get("PAGE_RETRY_SLEEP_SECONDS", "2"))

# ---------------------------------------------------------------------------
# 源站健康度退避（v1.8.1）
#
# 场景：某源站被限流/宕机，旧逻辑每 6 小时照样从第 1 页捶到最后一页（2,700 页
# 全失败也照捶），既烧自己的流量又持续触发对方风控，还每轮写一行 incomplete 日志。
#
# 规则：一轮同步「大面积失败」（索引失败率 >= 50%，或整轮异常）→ fail_streak +1，
#       调度器在 skip_until 之前跳过该站；退避时长 60min → 2h → 4h → ... 封顶 24h。
#       到期后自然参与下一轮（第一页就是探针，早停保证探针成本只有几页请求）。
#       任何一次成功 → streak 清零、解除退避。手动点「增量/全量」不受退避限制。
# ---------------------------------------------------------------------------
BACKOFF_BASE_MINUTES = int(os.environ.get("BACKOFF_BASE_MINUTES", "60"))
BACKOFF_MAX_HOURS = int(os.environ.get("BACKOFF_MAX_HOURS", "24"))
# 连续失败达到该次数后，面板状态标为 down（离线徽标）
FAIL_STATUS_DOWN_STREAK = int(os.environ.get("FAIL_STATUS_DOWN_STREAK", "2"))

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
# total 近似（v1.10.0 / P7）：不带筛选条件的「全站列表」直接用
# SUM(sources.total_videos)（后台同步一直在维护的数字，几十字节）代替
# 52 张表各跑一次 count(*)（PG 上是 52 次全表扫，220 万行首次 1~3s）。
# 响应里会带 total_approximate=true 让客户端知道它是个估值；带筛选条件时仍走精确统计。
TOTAL_APPROXIMATE_ENABLED = (
    os.environ.get("TOTAL_APPROXIMATE_ENABLED", "true").lower() == "true"
)

# 分类清单缓存秒数（v1.10.0 / P10）：/api/v1/categories 不带参数时要对 52 张表做
# GROUP BY type_id/type_name，几百 ms 起步，而分类几乎不变 —— 缓存 5 分钟。
CATEGORIES_CACHE_SECONDS = int(os.environ.get("CATEGORIES_CACHE_SECONDS", "300"))

# 搜索索引（v1.10.0 / P8）：PG 的 pg_trgm GIN 索引能让 `name LIKE '%关键词%'`
# 走索引（默认 B-tree 对左模糊完全无效，长尾词只能全表扫）。
# 启动时后台建（CREATE INDEX CONCURRENTLY，不锁表、不阻塞服务），建过即跳过。
# 需要数据库允许 CREATE EXTENSION；没权限时只记一条日志，搜索退化为顺序扫描（功能不变）。
PG_TRGM_ENABLED = os.environ.get("PG_TRGM_ENABLED", "true").lower() == "true"

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
# 每页条数默认值（v1.14.0 / B-04）：契约固定为 18 —— 客户端固定传 18，
# 缺省值必须与之一致，否则「不传 page_size」的调用会拿到与客户端不同的
# pagecount，翻页到底判断会错位。上限 100 是为了避免单次响应过大。
API_PAGE_SIZE_DEFAULT = int(os.environ.get("API_PAGE_SIZE_DEFAULT", "18"))
API_PAGE_SIZE_MAX = int(os.environ.get("API_PAGE_SIZE_MAX", "100"))

# API 访问密钥（可选）。设置后，所有接口需要在 Header 携带 X-API-Key: <key> 或 ?apikey=<key>
# v1.5.0 起改由「访问控制」统一管理（面板里可生成/重置 Token），此项仍兼容：
# 设置了就作为一枚额外有效的 Token，适合不想改客户端的环境变量部署。
API_KEY = os.environ.get("API_KEY", "").strip()

# ---------------------------------------------------------------------------
# 响应包络与错误码（v1.14.0 / B-11）
#
# 契约：成功 = HTTP 2xx + code==0；业务失败 = HTTP 4xx/5xx + {code, error, detail}。
#
# 为什么给 code 做成可配置：v1.13 及更早的成功响应是 MacCMS 风格的 code=1，
# 而客户端契约要求 code=0。改常量是唯一改动点，灰度期真出问题只要把
# API_SUCCESS_CODE 设回 1 并重启即可回滚，不需要改代码或回退镜像。
# 注意：/api/v1/{slug}（MacCMS 兼容接口）**不受此开关影响**，恒为 1 —— 那是
# 苹果 CMS 采集端的约定，改它会让第三方采集器/播放器直接失效。
# ---------------------------------------------------------------------------
API_SUCCESS_CODE = int(os.environ.get("API_SUCCESS_CODE", "0"))

# ---------------------------------------------------------------------------
# 未成年模式（v1.15.0）
#
# 请求级开关由客户端自报（X-Minor-Mode 头 / ?minor_mode=1），默认关 —— 现有
# 前端一个字都不用改，行为与升级前完全一致。仅靠客户端自报是可以绕过的（去掉
# 头即可），所以留一个部署级强制开关：设为 true 后**所有**请求都进入未成年模式，
# 客户端无法通过不带头绕过，适合家长在服务端统一管控。
# 两者是 OR 关系；标记编辑端点本身永不拦截。但 FORCE=true 时**面板的源站清单
# 也会被过滤**（面板同样走 GET /api/v1/sources），此时看不到 18+ 行，需临时
# 关掉 FORCE 或直接调 POST /api/v1/sources/{slug}/adult 修改标记。
# ---------------------------------------------------------------------------
MINOR_MODE_FORCE = os.environ.get("MINOR_MODE_FORCE", "false").lower() == "true"

# 能力清单灰度开关（v1.14.0 / B-10）
#
# `/healthz.capabilities` 是客户端「能不能用新参数」的唯一判据。灰度期需要
# 单独关掉某一项（例如 tag 索引只建了一半）而不用改代码：
#   CAPABILITIES_DISABLE=tag_search        强制不声明
#   CAPABILITIES_EXTRA=demo_thing          强制声明（仅排障用）
# 两者都只影响声明，不影响接口本身能不能调。
# ---------------------------------------------------------------------------
CAPABILITIES_DISABLE = {
    x.strip() for x in os.environ.get("CAPABILITIES_DISABLE", "").split(",") if x.strip()
}
CAPABILITIES_EXTRA = [
    x.strip() for x in os.environ.get("CAPABILITIES_EXTRA", "").split(",") if x.strip()
]

# ---------------------------------------------------------------------------
# 智能去广告（adfilter，v1.16.0）
#
# 后端只做控制面：下发规则包 + 接收命中上报，**不做任何播放流量代理**
# （m3u8 改写、ts 反代都不在本服务范围内，过滤在客户端完成，零带宽成本）。
#
# ADFILTER_ENABLED 只影响**能力声明**与两个接口是否可用，不影响已有任何接口：
# 设成 false 后 /healthz 不再声明 adfilter，客户端据此完全不调这两个接口。
# ---------------------------------------------------------------------------
ADFILTER_ENABLED = os.environ.get("ADFILTER_ENABLED", "true").lower() == "true"

# 上报限流：每个 Token 每小时允许的请求数（App 正常 ≈ 每会话 1~2 次）。
# 计数在进程内存里，多副本部署时按副本各自计数（自托管单容器场景够用）。
ADFILTER_REPORT_RATE_PER_HOUR = int(os.environ.get("ADFILTER_REPORT_RATE_PER_HOUR", "60"))
# 单批上报条数上限：客户端自己就按 ≤50 攒批，这里只是兜住异常客户端
ADFILTER_REPORT_MAX_ITEMS = int(os.environ.get("ADFILTER_REPORT_MAX_ITEMS", "50"))
# 上报时间戳与服务端时钟的最大允许偏差（秒）。超过即丢弃该条 ——
# 手机时钟错乱会产生「未来数据」，直接污染按时间聚合的统计。
ADFILTER_AT_SKEW_SEC = int(os.environ.get("ADFILTER_AT_SKEW_SEC", str(7 * 86400)))
# 管理端历史版本保留条数（面板只展示最近这么多，更老的仍留在库里供回滚）
ADFILTER_PACK_HISTORY = int(os.environ.get("ADFILTER_PACK_HISTORY", "20"))

# v1.16.1 按站点分布 / 误杀判定口径（前端给的运营阈值，做成可配置而不是写死）。
# 判定必须同时满足「样本量够」，否则一条异常上报就能把一个正常站标成过激。
ADFILTER_HOST_SUSPECT_RATIO = float(os.environ.get("ADFILTER_HOST_SUSPECT_RATIO", "0.15"))
ADFILTER_HOST_SUSPECT_RUNS = float(os.environ.get("ADFILTER_HOST_SUSPECT_RUNS", "4"))
ADFILTER_HOST_MIN_REPORTS = int(os.environ.get("ADFILTER_HOST_MIN_REPORTS", "5"))
# stats.hosts 默认/最大返回条数（只是列表长度，不等于 SQL 扫描量）
ADFILTER_HOST_STATS_LIMIT = int(os.environ.get("ADFILTER_HOST_STATS_LIMIT", "20"))
ADFILTER_HOST_STATS_MAX_LIMIT = 200

# ---------------------------------------------------------------------------
# 标签精确检索（v1.14.0 / B-01）
#
# 详情页的标签胶囊点击后，按「分词后精确匹配 vod_tag」检索，而不是把标签
# 当全文关键词去 LIKE '%悬疑%'（后者会把「悬疑喜剧」也搜出来，语义错误）。
# 数据落在 video_tags 关联表，(source, tag_norm) 复合索引支撑检索。
# ---------------------------------------------------------------------------
TAG_SEARCH_ENABLED = os.environ.get("TAG_SEARCH_ENABLED", "true").lower() == "true"
# 同步入库时同步维护 video_tags（关掉后只能靠全量回填，一般不需要关）
TAG_INDEX_ON_SYNC = os.environ.get("TAG_INDEX_ON_SYNC", "true").lower() == "true"
# 启动时检查索引是否建完，没建完就后台补（可断点续跑；建完的站不会重扫）
TAG_INDEX_ON_STARTUP = os.environ.get("TAG_INDEX_ON_STARTUP", "true").lower() == "true"
# 回填批大小与批间休眠：串行写、主动让路给同步任务，避免把 NAS 的 IO 打满
TAG_INDEX_BATCH = int(os.environ.get("TAG_INDEX_BATCH", "2000"))
TAG_INDEX_SLEEP_MS = int(os.environ.get("TAG_INDEX_SLEEP_MS", "50"))
# 能力声明的额外闸门：TAG_SEARCH_FORCE=true 时忽略「索引是否建完」直接声明
# （排障用）；false 且索引没建完则不声明，客户端会继续用 wd 全文检索。
TAG_SEARCH_FORCE = os.environ.get("TAG_SEARCH_FORCE", "false").lower() == "true"
# 单次 tag 检索每个源站最多取多少条候选（与 multiquery 的 top-K 归并同思路）
TAG_SEARCH_PER_SOURCE_LIMIT = int(os.environ.get("TAG_SEARCH_PER_SOURCE_LIMIT", "500"))

# ---------------------------------------------------------------------------
# Token 权限粒度（v1.14.0 / B-08，分阶段上线）
#
# 迁移铁律：先上线 scope 能力但**默认全放行**（等价全部 admin），客户端发版
# 切到 read Token 之后，再打开强制校验。打开后：
#   read  -> 只读接口（列表/详情/搜索/分类/统计/healthz）
#   admin -> 一切写操作、/auth/*、/admin/*、/maintenance/*、词库 CRUD
# 回滚：把 TOKEN_SCOPE_ENFORCE 设回 false 重启即可，不需要动数据。
# ---------------------------------------------------------------------------
TOKEN_SCOPE_ENFORCE = os.environ.get("TOKEN_SCOPE_ENFORCE", "false").lower() == "true"
# 强制校验时，低于该版本的客户端一律按 admin 放行（老版本 App 不会因为
# 拿不到 admin Token 而整站 401）。客户端可用 X-Client-Version 头或
# client_version 查询参数自报版本；不自报也按 admin 放行（宽进严出）。
TOKEN_SCOPE_LEGACY_BELOW = os.environ.get("TOKEN_SCOPE_LEGACY_BELOW", "").strip()
# API_KEY 环境变量那枚 Token 的权限（默认 admin，保持既有部署行为）
API_KEY_SCOPE = os.environ.get("API_KEY_SCOPE", "admin").strip().lower() or "admin"

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

# ---------------------------------------------------------------------------
# 相关推荐（v1.9.0，服务端预计算）—— 全部可调参数集中在这里，改环境变量即可调优
#
# 数据现状：vod_tag/vod_class 基本缺失，演职员/地区/年代大量缺失，标题是唯一
# 全量信号。算法按「缺失即权重重分配」设计：某视频只有 标题+演员+年代 可用时，
# 权重自动归一为 标题/演员/年代 按原始占比摊分，字段越稀疏标题占比越高。
# ---------------------------------------------------------------------------
RELATED_ENABLED = os.environ.get("RELATED_ENABLED", "true").lower() == "true"
# 算法版本：改了这个值，启动时会清空 vod_related 旧版本行并触发全量重算。
#   1 → v1.9.x 的「纯权重重分配」
#   2 → v1.10.0 起：证据先验 + 内容信号门控 + 演员余弦相似度（读路径按版本过滤）
RELATED_ALGO_VERSION = os.environ.get("RELATED_ALGO_VERSION", "2")
# 推荐条数：默认与上限（客户端请求 limit 超过上限按上限截断）
RELATED_LIMIT_DEFAULT = int(os.environ.get("RELATED_LIMIT_DEFAULT", "12"))
RELATED_LIMIT_MAX = int(os.environ.get("RELATED_LIMIT_MAX", "20"))
# 命中阈值：候选得分低于该值不进推荐列表；零命中时走兜底链（保证列表非空）。
# 注意：v1.10.0 起分数带「证据先验」地板（约 0.2），阈值再往下调已无意义，
# 想放宽/收紧请用下面 RELATED_REQUIRE_CONTENT_SIGNAL 与各项权重。
RELATED_MIN_SCORE = float(os.environ.get("RELATED_MIN_SCORE", "0.15"))
# ---- 证据先验（v1.10.0 / A1）----
# 旧公式 score = Σ(w·sim)/Σ(w) 只对「双方都有值」的字段求和，字段越少单个弱信号
# 越接近 1.0 —— 实测「同分类+同年代、标题毫不相干」得 0.667，反而高过
# 「流浪地球 vs 流浪地球2」的 0.383（排序反转）。
# 新公式：缺失字段按「缺失权重占比」注入一份中性先验，稀疏数据不再自动封顶：
#   prior = LAMBDA × (缺失权重 / 总权重)
#   score = (Σ w·sim + prior × NEUTRAL) / (Σ w + prior)
RELATED_EVIDENCE_PRIOR = float(os.environ.get("RELATED_EVIDENCE_PRIOR", "0.6"))
RELATED_EVIDENCE_NEUTRAL = float(os.environ.get("RELATED_EVIDENCE_NEUTRAL", "0.4"))
# ---- 内容信号门控（v1.10.0 / A1-3）----
# 只有「标题/演员/导演/编剧」命中才算相似；仅同分类+同地区+同年代的配对
# 一律不进推荐位（走兜底链，带 m=fb），避免「推荐位里全是同分类随机片」。
RELATED_REQUIRE_CONTENT_SIGNAL = (
    os.environ.get("RELATED_REQUIRE_CONTENT_SIGNAL", "true").lower() == "true"
)
# 标题相似度达到该值才计入「内容信号」（低于它的标题命中只能当加分项）
RELATED_TITLE_CONTENT_MIN = float(os.environ.get("RELATED_TITLE_CONTENT_MIN", "0.15"))
# 可用权重下限：低于该值直接判无关（兜底链接手）。仅用于关掉内容门控时的兜底防线
RELATED_MIN_EVIDENCE_WEIGHT = float(os.environ.get("RELATED_MIN_EVIDENCE_WEIGHT", "0.35"))
# 标题相似度双阈值（防两字短标题误伤）：LCS 公共子串 < 该字数 且 bigram Jaccard
# < JACCARD 阈值时，标题项直接计 0
RELATED_TITLE_LCS_MIN = int(os.environ.get("RELATED_TITLE_LCS_MIN", "3"))
RELATED_TITLE_JACCARD_MIN = float(os.environ.get("RELATED_TITLE_JACCARD_MIN", "0.45"))
# 倒排索引候选集上限：只对「与目标共享信号 token 最多的前 N 个候选」打分。
# v1.10.0 实测：cap 600→140 让 1 万行的整站重算 27.2s→6.0s（4.5×），
# Top12 结果基本不变（Top 只要 12 条，给 140 个候选已远超所需）。
RELATED_CANDIDATE_CAP = int(os.environ.get("RELATED_CANDIDATE_CAP", "140"))
# 表行数 <= 该值时跳过倒排索引直接全量两两比较。
# v1.10.0：3000→400。旧阈值是个性能悬崖 —— 3000 行走 O(n²) 要比 1 万行走倒排还慢
# （实测 18.2s vs 27s/万行，单条 6.08ms vs 2.72ms）。
RELATED_BRUTE_FORCE_MAX = int(os.environ.get("RELATED_BRUTE_FORCE_MAX", "400"))
# 超大表的降级阈值：行数超过后只用 标题/分类/年代 计算并丢弃演职员存储（内存保护）
RELATED_MAX_ROWS_PER_SOURCE = int(os.environ.get("RELATED_MAX_ROWS_PER_SOURCE", "200000"))
# 重算时一次写入 vod_related 的批大小
RELATED_WRITE_BATCH = int(os.environ.get("RELATED_WRITE_BATCH", "500"))
# 每日校准：把 computed_at 超过 N 天的源站重算一遍（兜底/下架条目刷新）
RELATED_CALIBRATION_DAYS = int(os.environ.get("RELATED_CALIBRATION_DAYS", "1"))
RELATED_CALIBRATION_HOUR = int(os.environ.get("RELATED_CALIBRATION_HOUR", "4"))
RELATED_CALIBRATION_MINUTE = int(os.environ.get("RELATED_CALIBRATION_MINUTE", "30"))
# 每次校准最多重算多少个源站（夜里跑，别抢白天同步的 IO）
RELATED_CALIBRATION_BUDGET = int(os.environ.get("RELATED_CALIBRATION_BUDGET", "3"))
# 详情响应是否附带 related 数组（ac=detail 与 REST 详情接口，纯新增字段，老客户端无感）
RELATED_IN_DETAIL = os.environ.get("RELATED_IN_DETAIL", "true").lower() == "true"
# 增量同步收集的「受影响 ID」上限（超过截断，全量同步会走整站重算不依赖它）
RELATED_TOUCHED_CAP = int(os.environ.get("RELATED_TOUCHED_CAP", "20000"))
# 兜底链条数上限（v1.10.0 / A4）：零命中时最多补几条「猜你想看」。
# 旧实现一次补满 12 条同分类最新，和真命中混在一栏，用户分不清。
RELATED_FALLBACK_LIMIT = int(os.environ.get("RELATED_FALLBACK_LIMIT", "6"))

# ---- 重算并发闸门与增量合并（v1.10.0 / P5）----
# 52 个源站同步完各自起线程整站重算，叠加时内存可达 GB 级（单源 20 万行 ≈ 300MB）。
# 全局最多几个源站同时算（超出的排队，按源站加锁串行）。
RELATED_RECOMPUTE_CONCURRENCY = int(os.environ.get("RELATED_RECOMPUTE_CONCURRENCY", "2"))
# 增量重算合并窗口（秒）：窗口内的多次 touched 合并成一次重算。
# 增量同步一轮会分批触发很多次，每次都全量加载特征纯属浪费；合并后每个源站
# 每个窗口最多加载一次（0 = 关闭合并，退回旧行为）。
RELATED_INCREMENT_DEBOUNCE_SECONDS = int(
    os.environ.get("RELATED_INCREMENT_DEBOUNCE_SECONDS", "60")
)
# 窗口内累计的 id 超过「该源站行数 × 该比例」时，直接整站重算（比逐个小批更划算）
RELATED_INCREMENT_FULL_RATIO = float(os.environ.get("RELATED_INCREMENT_FULL_RATIO", "0.3"))

# 基础权重表（逗号分隔 key:value）。
# v1.10.0 调整（A1-2）：标题是唯一全量信号 → 0.20→0.35；分类（52 站共用十几个大类，
# 几乎不含区分信息）0.30→0.15；地区/年代降为语境信号 0.05。
# 可用 key: class / actor / director / writer / area / year / title
RELATED_WEIGHTS = os.environ.get(
    "RELATED_WEIGHTS",
    "class:0.15,actor:0.20,director:0.05,writer:0.05,area:0.05,year:0.05,title:0.35",
)
# 占位值表（逗号分隔，比较时忽略大小写）。演职员/地区/年代命中即视为缺失。
RELATED_PLACEHOLDERS = os.environ.get(
    "RELATED_PLACEHOLDERS",
    "内详,未知,暂无,暂缺,不详,无,待定,待补,更多,点击查看,角色介绍,n/a,na,tba,none,null,-,—,/,0",
)
# 标题噪声剥离（逗号分隔的正则，逐条依次 sub）。别把「上/下/终章」这类序号写进来，
# 它们由序号逻辑单独处理（保留但降权）。
RELATED_NOISE_PATTERNS = os.environ.get(
    "RELATED_NOISE_PATTERNS",
    "[（(【\\[][^）)】\\]]{0,12}[）)】\\]],完整版|国语版|粤语版|国语|粤语|中字|双语| Thai |泰语|韩语|日语版,"
    "抢先版|修正版|修复版|枪版|蓝光|超清|高清|标清|4k|1080p|720p|hd|bd|tc|ts|cam|"
    "第\\s*\\d+\\s*季|第\\s*[一二三四五六七八九十]+\\s*季|season\\s*\\d+|s\\d{1,2}$|"
    "第\\s*\\d+\\s*部|第\\s*[一二三四五六七八九十]+\\s*部|终章|完结篇|大结局|先行版|加长版",
)
# 年份差惩罚：候选与目标年份都已知且相差超过 N 年 → 标题分 ×0.6（翻拍/同名不同片）
RELATED_YEAR_GAP_PENALTY_YEARS = int(os.environ.get("RELATED_YEAR_GAP_PENALTY_YEARS", "6"))
RELATED_YEAR_GAP_FACTOR = float(os.environ.get("RELATED_YEAR_GAP_FACTOR", "0.6"))
# 年代相邻得分：年份相等=1.0，相差 1 年=0.5（同系列续作），相差 >=2 计 0
RELATED_YEAR_ADJACENT_SCORE = float(os.environ.get("RELATED_YEAR_ADJACENT_SCORE", "0.5"))
# 序号（季/部/上下集）不一致时标题分乘的惩罚系数
RELATED_ORDINAL_MISMATCH_FACTOR = float(os.environ.get("RELATED_ORDINAL_MISMATCH_FACTOR", "0.5"))

# 明星词库（面板设置页可维护）：词库中的主演名会扫描缺失演员信息视频的
# 标题与简介，命中即推断为演员信号参与匹配。
#   off          关闭推断
#   when_missing 只对 vod_actor 为空的视频推断（默认，不覆盖源站真实数据）
#   always       真实演员与推断演员合并（谨慎使用，推断分带降权）
# 推断强度分级（用户规则：标题提到主演 + 其它字段缺失/稀少 → 相关性非常高）：
#   强推断 strong：命中出现在【标题】且该视频稀疏（area/year/type 非空数
#                  <= RELATED_STAR_STRONG_MAX_FIELDS）→ 打分不打折（等价真实演员）
#   弱推断 inf   ：仅简介命中，或标题命中但字段较齐全 → 打分乘 STAR_SIM_FACTOR
RELATED_STAR_INFER_MODE = os.environ.get("RELATED_STAR_INFER_MODE", "when_missing")
RELATED_STAR_STRONG_MAX_FIELDS = int(os.environ.get("RELATED_STAR_STRONG_MAX_FIELDS", "1"))
# 推断演员参与的匹配，相似度乘以该系数（<1 表示弱信号；强推断不受此系数影响）
RELATED_STAR_SIM_FACTOR = float(os.environ.get("RELATED_STAR_SIM_FACTOR", "0.7"))
# 标题命中词库的「否定词」（v1.10.0 / A5）：正则，逗号分隔。
# 「吴京的访谈」「盘点吴京十大名场面」「吴京同款」这类标题虽然提到明星，
# 但显然不是他主演，命中后强制降为弱信号（inf），不享受 strong 的不打折待遇。
RELATED_STAR_NEGATIVE_PATTERNS = os.environ.get(
    "RELATED_STAR_NEGATIVE_PATTERNS",
    "访谈,专访,花絮,幕后,解说,盘点,合集,混剪,预告,同款,致敬,恶搞,吐槽,影评,"
    "reaction,采访,纪录片",
)

# ---------------------------------------------------------------------------
# 播放线路识别 / 过滤 / 排序（v1.11.0）
#
# 问题：上游 `vod_play_from` 从未入库，`parse_play_url()` 只按序号命名线路，
# 也不做任何形态判断。于是「第 1 条线路是 H5 分享页（/share/<hash>，返回 text/html，
# 真实流地址由页面 JS 生成且带会过期的 sign）」的源站（如非凡资源站，100% 记录如此），
# 客户端按顺序选第一条 → 必然播放失败。线路与 provider 的对应关系在入库那一刻就丢了。
#
# 策略：入库保留 play_from（原始串），读时按 URL 形态分类并过滤/排序。
# `vod_play_url` 原始串**永不改写**（客户端仍可自行解析），只影响结构化的 vod_play_list。
# ---------------------------------------------------------------------------
# off 不过滤（等价旧行为）/ filter 剔除 page 型 / whitelist 只保留媒体直链
PLAY_LINE_POLICY = os.environ.get("PLAY_LINE_POLICY", "filter").strip().lower()
# filter 模式下判定「不是播放地址」的路径特征（逗号分隔，大小写不敏感子串；
# 以 `re:` 开头的按正则处理）。只匹配 URL 的 path（不含 query）。
# 注意：以 .m3u8/.mp4/.flv/.mkv 结尾的地址永远视为媒体直链，不会被这里误杀。
PLAY_LINE_DROP_PATTERNS = os.environ.get(
    "PLAY_LINE_DROP_PATTERNS", "/share/,/play/,.html,.htm"
)
# whitelist 模式下保留的扩展名
PLAY_LINE_KEEP_EXT = os.environ.get("PLAY_LINE_KEEP_EXT", "m3u8,mp4,flv,mkv")
# 直链优先排序：m3u8 > mp4 > flv > mkv > unknown > page，同类型内保持上游原顺序（稳定排序）
PLAY_LINE_PREFER_DIRECT = (
    os.environ.get("PLAY_LINE_PREFER_DIRECT", "true").strip().lower() == "true"
)
# 线路名是否追加 provider，如「线路2 (ffm3u8)」
PLAY_LINE_NAME_WITH_FROM = (
    os.environ.get("PLAY_LINE_NAME_WITH_FROM", "true").strip().lower() == "true"
)
# 安全阀：过滤后线路数低于该值则整条影片回退到未过滤结果（绝不允许把片子过滤成「无线路」）
PLAY_LINE_MIN_KEEP = int(os.environ.get("PLAY_LINE_MIN_KEEP", "1"))
# 是否对 unknown 型地址做一次 HEAD 探测（默认关：读路径上发网络请求会拖慢详情接口）
PLAY_LINE_PROBE_HTML = (
    os.environ.get("PLAY_LINE_PROBE_HTML", "false").strip().lower() == "true"
)
PLAY_LINE_PROBE_TIMEOUT = float(os.environ.get("PLAY_LINE_PROBE_TIMEOUT", "1.5"))
# HEAD 结果缓存秒数（结果不落库，仅进程内）
PLAY_LINE_PROBE_CACHE_SECONDS = int(os.environ.get("PLAY_LINE_PROBE_CACHE_SECONDS", "300"))
# 源站级策略（sources.play_line_policy）的进程内缓存秒数。
# 面板改下拉后立即失效（reset_policy_cache），这个 TTL 只兜底多进程/直接改库的场景。
PLAY_LINE_POLICY_CACHE_SECONDS = int(os.environ.get("PLAY_LINE_POLICY_CACHE_SECONDS", "30"))

# ---------------------------------------------------------------------------
# 存量字段回填（v1.9.0）
#
# 场景：升级前已全量同步的库，演员/地区/年代列全是空的（旧抓取逻辑没存）。
# 方案：每晚凌晨对「还有字段缺失」的源站重跑一遍全量同步 —— UPSERT 幂等，
# 只是把新字段补进去；按小站优先，每晚 N 个站，几个晚上自然补完。
# 不着急的话也可以手动 POST /api/v1/sync/{slug}?mode=full 逐站补。
# ---------------------------------------------------------------------------
FIELD_BACKFILL_ENABLED = os.environ.get("FIELD_BACKFILL_ENABLED", "true").lower() == "true"
FIELD_BACKFILL_SOURCES_PER_NIGHT = int(os.environ.get("FIELD_BACKFILL_SOURCES_PER_NIGHT", "2"))
FIELD_BACKFILL_START_HOUR = int(os.environ.get("FIELD_BACKFILL_START_HOUR", "2"))

# ---------------------------------------------------------------------------
# 明星 Tag 回填 + 明星作品推荐（v1.9.1）
#
# 数据流：词库编译成单个正则 → 缺演员的行用标题扫描命中词库 →
#   命中名写入 vod_tag（逗号分隔，可多个）+ star_map 倒排映射表；
#   真实演员行把演员名与词库做交集也进 star_map（高可信）。
# API 查询走 star_map 的 (slug, term) 索引，不打大表。
# ---------------------------------------------------------------------------
# 同步完成后自动回填：全量同步 → 整站重建；增量同步 → 只重算本轮 touched 的 id
STAR_TAG_AUTO_AFTER_SYNC = os.environ.get("STAR_TAG_AUTO_AFTER_SYNC", "true").lower() == "true"
# 每晚定时重建的源站预算（重建是全量幂等的，词库增删后历史行会被重新评估）
STAR_TAG_NIGHTLY_BUDGET = int(os.environ.get("STAR_TAG_NIGHTLY_BUDGET", "2"))
STAR_TAG_NIGHTLY_HOUR = int(os.environ.get("STAR_TAG_NIGHTLY_HOUR", "3"))
STAR_TAG_NIGHTLY_MINUTE = int(os.environ.get("STAR_TAG_NIGHTLY_MINUTE", "40"))
# 同一源站两次夜间重建的最小间隔（小时）。配合「按上次重建时间轮转」，
# 保证每个源站都能轮到，而不是每晚捶同一批小站（v1.10.0 / C1）。
STAR_TAG_NIGHTLY_MIN_INTERVAL_HOURS = int(
    os.environ.get("STAR_TAG_NIGHTLY_MIN_INTERVAL_HOURS", "20")
)
# 单视频最多写入的明星数（防超长溢出 vod_tag VARCHAR(255)）
STAR_TAG_MAX_PER_VIDEO = int(os.environ.get("STAR_TAG_MAX_PER_VIDEO", "5"))
# 重建时清掉「旧推断残留」tag（v1.10.0 / B3）：
# 旧实现只在命中词库时才回写 vod_tag，词库删改后旧值永久残留，而 upsert 的
# COALESCE(NULLIF()) 保护又让它连源站重同步都洗不掉。开启后，缺演员的行
# 每次重建都会按当前词库重算 tag（命中则写入，不命中则清空）。
STAR_TAG_CLEAR_STALE = os.environ.get("STAR_TAG_CLEAR_STALE", "true").lower() == "true"
# 增量回填单次处理的 id 上限（v1.10.0 / C11：与推荐用的 RELATED_TOUCHED_CAP 解耦）
STAR_TAG_TOUCHED_CAP = int(os.environ.get("STAR_TAG_TOUCHED_CAP", "50000"))
# 扫描批大小（keyset 分页步长，内存恒定）
STAR_TAG_BATCH_SIZE = int(os.environ.get("STAR_TAG_BATCH_SIZE", "2000"))
# 明星相关视频 API 默认/上限条数
STAR_RELATED_LIMIT_DEFAULT = int(os.environ.get("STAR_RELATED_LIMIT_DEFAULT", "20"))
STAR_RELATED_LIMIT_MAX = int(os.environ.get("STAR_RELATED_LIMIT_MAX", "50"))
# 按明星查作品 API（v1.9.2）默认/上限条数
STAR_WORKS_LIMIT_DEFAULT = int(os.environ.get("STAR_WORKS_LIMIT_DEFAULT", "30"))
STAR_WORKS_LIMIT_MAX = int(os.environ.get("STAR_WORKS_LIMIT_MAX", "100"))
# 未入词库高频词统计（v1.9.2，扩词库用）：抽样标题数 / 最低频次 / 停用词
STAR_UNTRACKED_SAMPLE = int(os.environ.get("STAR_UNTRACKED_SAMPLE", "20000"))
STAR_UNTRACKED_MIN_COUNT = int(os.environ.get("STAR_UNTRACKED_MIN_COUNT", "3"))
STAR_UNTRACKED_STOPWORDS = os.environ.get(
    "STAR_UNTRACKED_STOPWORDS",
    "电影,电视剧,全集,完整版,高清,超清,国语,粤语,英语,中字,字幕,更新,在线,免费,"
    "第,季,集,部,期,版,篇,之,的,和,与,合集,解说,预告,花絮,正片,加长",
)
