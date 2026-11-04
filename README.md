# VodSync 影视数据同步服务

遍历 [ziyuanzhan 源站监测列表](https://mylazily.github.io/ziyuanzhan/data/latest.json)，定时增量同步各苹果 CMS（MacCMS）采集站的影视数据到本地数据库，并对外提供**统一格式的视频列表/详情 API**（通过 `source` 参数区分源站）。

## 功能特性

- **源站自动发现**：定时拉取 latest.json，新源站自动登记并建表（每源站一张独立表 `vod_{slug}`）
- **高性能写入**：SQLAlchemy Core 批量 UPSERT，百万级数据无压力
- **增量同步**：以 `vod_time` 为水位线，先用轻量 `ac=list` 对比，仅对新增/更新的 ID 拉详情，存量数据零重复传输
- **断点续传**：全量同步每 5 页保存进度，中断后自动续传
- **双接口风格**：REST API（`?source=` 区分源站）+ 苹果 CMS 兼容 API（`?ac=list|detail`），现有播放器/采集器可直接对接
- **Web 管理面板**：浏览器看同步进度、手动触发同步、跨源聚合搜索，单文件零依赖
- **聚合搜索**：跨全部源站按名称并行搜索（线程池 + 内存归并），单源失败隔离
- **中转代理降级**：源站直连不可达/被风控时，自动按顺序切到 API 中转代理取数（可配多个，成功后记忆 30 分钟）
- **访问控制**：面板密码登录（默认 `666666`）+ API Token，可一键开关；Token 可在面板生成/重置
- **断电可恢复**：每页一个事务 + 断点/水位线入库，重启后自动续跑；单页失败不会静默丢数据
- **PostgreSQL 存储**：v1.7.0 起固定使用 PG，百万级行下列表查询走并行 top-K 归并
- **自带维护任务**：定时清理同步日志、回收 WAL，长时间跑不会把库/磁盘撑爆

## 技术选型说明

| 项 | 选型 | 理由 |
|---|---|---|
| 语言/框架 | Python 3.12 + FastAPI | IO 密集型抓取，多线程 + httpx 连接池足够；代码易读易改 |
| 数据库 | PostgreSQL 16 | 220 万行起步、52 张分表、列表要按时间倒序翻页 —— 这些是 SQLite 的弱项（无并行查询、写串行、UNION 排序慢）；PG 还能让 API 与同步真正并发 |
| 调度 | APScheduler | 轻量、随应用进程运行，无需额外 cron 容器 |
| 列表查询 | 每表 top-K + 进程内归并 | 替代 52 表 UNION ALL + 全量排序，首页从秒级降到几十毫秒（见 `app/multiquery.py`） |

> SQLite 驱动代码仍然保留，但**只用于自动化测试**（`python scripts/test_*.py` 直接跑，不需要起 PG）。
> 生产环境请始终配置 `DATABASE_URL` 指向 PostgreSQL。

## 快速开始（本地开发）

```bash
# 0. 先有一个 PostgreSQL（本地装、或用 compose 里那个都行）
#    最省事：docker compose up -d postgres

# 1. 安装依赖（建议 Python 3.11+）
pip install -r requirements.txt

# 2. 配置连接串（必须指向 PG）
cp .env.example .env
# DATABASE_URL=postgresql+psycopg2://vodsync:vodsync@127.0.0.1:5432/vodsync

# 3. 启动
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

# 4. 打开管理面板 / 交互式文档
# 管理面板: http://127.0.0.1:8000/
# API 文档: http://127.0.0.1:8000/docs
```

首次启动会自动：建表 → 拉取源站列表 → 后台开始全量同步（数据量大，需数小时）。

## 管理面板

浏览器打开 `http://<主机IP>:8181/`（Docker）即进入管理面板，单文件零依赖、不联网可用：

| 区域 | 能力 |
|---|---|
| 概览卡片 | 源站总数/启用数、已入库总量、正在全量同步的源站数、今日新增与更新 |
| 操作区 | 刷新源站列表、全站增量同步、全站全量同步（二次确认）、条数校准 |
| 聚合搜索 | 跨全部源站按名称搜索，直接看封面/分类/备注，点「详情」查看简介与分集播放地址 |
| 源站列表 | 状态、已入库条数、**全量同步进度条（第 N/M 页 + 百分比）**、增量水位线、最后同步时间；每行可单独触发增量/全量同步、查看该站日志 |
| 同步日志 | 最近 20 条任务：模式、结果、抓取/新增/更新条数、页数、失败原因 |

面板每 5 秒自动刷新（可关闭），页面切换后台时自动暂停，避免占资源。

> 面板默认需要密码登录（默认 `666666`）。登录后右上「⚙ 设置」可改密码、复制/重置 API Token、开关访问控制。

## API 一览

### 管理

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | 管理面板（单页 HTML） |
| GET | `/source` | **资源站 API 清单**（含上游采集接口地址与本地接口路径） |
| GET | `/healthz` | 健康检查 |
| GET | `/api/v1/stats` | 概览统计（面板用） |
| GET | `/api/v1/sources` | 所有源站及同步状态（含进度百分比、页数） |
| GET | `/api/v1/sources/{slug}` | 单源站详情 + 最近 10 条同步日志 |
| POST | `/api/v1/sources/refresh` | 立即刷新源站列表（`?full_sync=true` 同步新站） |
| POST | `/api/v1/sync/{slug}?mode=auto` | 手动同步单源站（auto/full/incremental） |
| POST | `/api/v1/sync-all?mode=auto` | 手动同步全部源站 |
| GET | `/api/v1/pause` | 同步暂停状态（global/full/incremental 三层级） |
| POST | `/api/v1/pause?scope=global\|full\|incremental` | 暂停同步（优雅停止，保存断点，重启不丢） |
| POST | `/api/v1/resume?scope=global\|full\|incremental` | 继续同步（清标记并自动从断点续跑） |
| POST | `/api/v1/sources/{slug}/pause` | **暂停单个源站**（不影响其他源站，断点保留） |
| POST | `/api/v1/sources/{slug}/resume` | **继续单个源站**（按 auto 续跑：有断点续全量/有水位线走增量） |
| POST | `/api/v1/maintenance/recount` | 重算各源站本地条数（同步中断后校准） |
| GET | `/api/v1/logs?limit=50` | 最近同步日志 |

### 资源站 API 清单

```bash
GET /source                          # 全部启用的资源站
GET /source?only_ready=true          # 仅返回本地已有数据的源站
GET /source?status=ok                # 只返回在线源站（ok / down）
```

响应（按本地已入库条数倒序）：

```json
{
  "code": 1, "total": 52, "alive": 52, "ready": 1,
  "list": [
    {
      "name": "JKUN资源站",
      "slug": "jkun",
      "api": "https://jkunzyapi.com/api.php/provide/vod",   // 上游苹果CMS采集接口
      "homepage": "https://jkunzyapi.com",
      "status": "ok",
      "description": "JKUN资源采集站，提供影视资源API接口",
      "video_count": 2640,                                   // 本地已入库条数
      "synced": true,
      "last_sync_at": "2026-10-30T13:00:30",
      "local_api": "/api/v1/jkun",                           // 本服务的 MacCMS 兼容接口
      "local_list_api": "/api/v1/videos?source=jkun"         // 本服务的 REST 列表接口
    }
  ]
}
```

用途：给下游客户端/前端做源站选择器；`api` 可直接拼 `?ac=list` / `?ac=detail` 调上游，`local_api` / `local_list_api` 则是走本服务的统一输出。

### 分类清单（分类浏览入口）

```bash
GET /api/v1/categories?source=jkun     # 单源站分类；留空=全部源站合并，逗号分隔=多源合并
```

```json
{ "code": 1, "total": 15, "list": [
  { "type_id": 4, "type_name": "AV解说", "count": 160, "sources": ["jkun"] }
]}
```

拿到 `type_id` 后用 `GET /api/v1/videos?source=jkun&type_id=4` 按分类翻页（支持与 `ac=detail` 组合）。

### 聚合搜索（跨全部源站）

```bash
# 基础搜索：跨所有源站按名称模糊匹配，结果按更新时间倒序
GET /api/v1/search?wd=三体

# 常用参数
GET /api/v1/search?wd=三体&page=1&page_size=20      # 分页
GET /api/v1/search?wd=三体&source=jkun,t1           # 只搜指定源站（更快）
GET /api/v1/search?wd=三体&type_name=电影            # 按分类过滤
GET /api/v1/search?wd=三体&per_source_limit=500     # 每源站候选上限（默认 200）
```

响应：

```json
{
  "code": 1, "wd": "三体", "page": 1, "total": 4,
  "total_is_partial": false,          // true 表示有源站结果被 per_source_limit 截断
  "truncated_sources": [],
  "sources_searched": 52,             // 实际搜索成功的源站数
  "sources_failed": [],               // 查询失败/超时的源站（不影响其他源站结果）
  "elapsed_ms": 76,
  "list": [
    { "source": "jkun", "source_name": "JKUN资源站", "vod_id": 132412,
      "type_name": "动作片", "vod_name": "示例片名", "vod_pic": "https://...",
      "vod_remarks": "高清", "vod_time": "2026-09-28 09:33:38" }
  ]
}
```

**实现与性能说明**：

- 名称模糊匹配无法走索引，采用「**每源站各查一次 + 线程池并行 + 内存归并分页**」策略，避免一次性 UNION 全部表造成单条 SQL 扫描上千万行而超时
- 单源站查询失败/超时被隔离，返回 `partial` 信息，不影响整体可用性
- `total` 是各源站候选上限内的命中数，**不是全库精确总数**（精确计数需全表扫描，代价过高）；需要精确分页时用 `source=` 限定单源站
- 实测：52 个源站、单站 2640 条，跨源搜索 76ms；数据量增长后建议限定 `source` 或缩短关键词

### 数据（REST 风格，`source` 参数区分源站）

```bash
# 视频列表（source 支持逗号分隔多源合并；留空=全部源站）
GET /api/v1/videos?source=jipinvip1com&page=1&page_size=20
GET /api/v1/videos?wd=三体                       # 按名称搜索
GET /api/v1/videos?type_name=电影                # 按分类过滤

# 批量取详情（含 vod_play_url 原始串 + vod_play_list 结构化播放列表）
GET /api/v1/videos?source=jkun&ids=134369
GET /api/v1/videos?source=jkun&ids=1,2,3

# 列表直接带播放地址（MacCMS 兼容参数 ac=detail）
GET /api/v1/videos?source=jkun&ac=detail

# 单条详情（REST 风格）
GET /api/v1/videos/jkun/134369
```

> **URL 写法注意**：一个 URL 只能有一个 `?`，参数之间用 `&` 连接。
> 正确：`/api/v1/videos?source=jkun&ac=detail&ids=134369`
> 错误：`/api/v1/videos?source=jkun&?ac=detail&ids=134369`（多了一个 `?`）
> 服务端已对多余的 `?` 做兼容（会把 `?ac` 归一化为 `ac`），但仍建议按标准写法调用。

### 数据（苹果 CMS 兼容风格）

```bash
GET /api/v1/{slug}?ac=list&pg=1        # 轻量列表，输出结构与 MacCMS 采集接口一致
GET /api/v1/{slug}?ac=detail&pg=1      # 全字段列表（含 vod_play_url）
GET /api/v1/{slug}?ac=detail&ids=1,2,3 # 按 ID 批量取详情
```

响应示例（`/api/v1/videos`）：

```json
{
  "code": 1, "page": 1, "pagecount": 2808, "total": 56144,
  "list": [
    {
      "source": "jipinvip1com",
      "vod_id": 12345,
      "type_name": "国产剧",
      "vod_name": "示例剧名",
      "vod_pic": "https://...",
      "vod_remarks": "更新至第10集",
      "vod_time": "2026-10-29 20:15:22"
    }
  ]
}
```

## 访问控制（v1.5.0）

服务默认开启访问控制，两层：

| 层 | 保护对象 | 凭证 |
|---|---|---|
| 面板登录 | `/` 面板页面、`/docs` | 管理密码（默认 `666666`，存在 `settings` 表里，PBKDF2 哈希） |
| API Token | 全部 `/api/v1/*`（`/healthz` 与 `/api/v1/auth/*` 除外） | `vs_` 开头的 Token，请求头 `Authorization: Bearer <token>` |

三处入口：

```bash
# 1) 面板：浏览器打开 http://<IP>:8181/ → 输入密码 → 右上「⚙ 设置」
#    可改密码、复制 Token、重新生成 Token、开关访问控制

# 2) 命令行拿 Token
curl -c ck.txt -X POST http://<IP>:8181/api/v1/auth/login \
     -H 'Content-Type: application/json' -d '{"password":"666666"}'
curl -b ck.txt http://<IP>:8181/api/v1/auth/token

# 3) 用 Token 调接口
curl -H "Authorization: Bearer vs_xxx..." "http://<IP>:8181/api/v1/videos?source=jkun&page=1"
curl "http://<IP>:8181/api/v1/videos?source=jkun&token=vs_xxx..."      # 不方便加请求头时
```

要点：

- **开关**：面板「设置」里关闭后，`/api/v1/*` 不再校验 Token（面板也免登录）；重新开启时**自动更换 Token**
  —— 关闭期间谁都能读到旧 Token，不换等于没关。
- **改密码**：所有设备上的旧登录会话立即失效（会话签名里带密码指纹），当前页面会自动续期。
- **防爆破**：连续输错 5 次锁定 5 分钟（按 IP 计），六位默认密码不至于被脚本刷开。
- **忘记密码**：删掉数据库里的那行密码哈希再重启，即恢复成 `ADMIN_PASSWORD`（默认 `666666`）：
  ```bash
  docker exec vodsync-pg psql -U vodsync -d vodsync -c "delete from settings where key='admin_password_hash'"
  docker compose restart vodsync
  ```
- 兼容：`API_KEY` 环境变量仍可作为一枚额外 Token 使用（老客户端不必改配置就能继续跑，只要它带的 Key 与之一致）。
- **状态接口不回显凭据**：`/api/v1/auth/state` 在免鉴权白名单里（登录页要靠它判断），因此未登录只返回
  `enabled` / `logged_in`；Token 是否存在、Token/密码的变更时间都要登录后才给。**任何接口和日志都不输出 Token 片段**
  —— 要拿 Token 走面板或 `GET /api/v1/auth/token`（需登录）。

## 断电 / 重启恢复（v1.6.0）

| 中断时机 | 数据 | 进度 | 重启后 |
|---|---|---|---|
| 全量同步中（断电 / `docker restart`） | 每页一个事务，已提交的页都在 | `sources.last_page` 持久化 | 自动续跑（默认延迟 30s），从断点的下一页继续 |
| 全量某页持续抓取失败 | 该页不写入，其他页照常 | 断点停在失败页**之前** | 本次内重试 `PAGE_MAX_ATTEMPTS` 次；仍失败则中止，断点保留，下次接着来 |
| 增量同步中 | 水位线最后才推进，重跑整批候选 | 水位线 | 下一轮调度自动重来（UPSERT 幂等，不会重复行） |
| 写入瞬间断电 | PG 回滚未提交事务（每页一个事务，最坏丢当前页），已提交的页都在 | — | 库不会损坏；断点还在，续跑该页即可补齐 |
| 服务被 kill | 内存态清空 | — | 残留的 `running` 日志自动改 `interrupted`，不会永远显示"进行中" |

设计要点（为什么能这么恢复）：

- **断点只落在「连续完成的前缀」上**。一个批次里第 3 页失败、第 4 页成功时，断点存 2 而不是 4 ——
  否则重启后会从 5 开始，第 3 页那几百条永久丢失，而任务还显示 100%。
- **只有全部页写完才清断点、推进水位线**；否则状态记 `incomplete`（面板显示「部分完成」），
  断点和 `total_pages` 都保留，随时可续。
- **增量水位线只按「真的写进库」的 `vod_time` 推进**。详情拉取失败的记录会把水位线封顶在它之前，
  下一轮筛选条件 `vod_time > watermark` 仍然能选中它。
- **增量 list 索引页失败 → 水位线冻结**。漏掉的索引页里可能有新片，宁可不推进也不跳过。
- **幂等**：`INSERT ... ON CONFLICT(vod_id) DO UPDATE`，重复抓同一页、重跑同一批候选都不会产生脏数据。

配置：

```yaml
      AUTO_RESUME_ON_BOOT: "true"          # 不想自动续跑就改 false，等下一轮调度或手动点「继续」
      AUTO_RESUME_DELAY_SECONDS: "30"
      PAGE_MAX_ATTEMPTS: "3"
```

手动排查：`GET /api/v1/logs` 里 `status` 为 `incomplete`（部分完成）/ `interrupted`（被重启打断）的
记录，`error` 字段会写明卡在第几页或失败了多少条。

## 配置项（环境变量）

完整列表见 [.env.example](.env.example)，关键项：

| 变量 | 默认 | 说明 |
|---|---|---|
| `SYNC_INTERVAL_MINUTES` | 360 | 增量同步间隔（分钟） |
| `SOURCE_REFRESH_INTERVAL_MINUTES` | 720 | 源站列表刷新间隔 |
| `SOURCE_SYNC_CONCURRENCY` | 2 | **同时真正在跑的源站同步数上限**（全局闸门，排队不占资源；NAS 建议 1-2） |
| `PAGE_CONCURRENCY` | 4 | 单源站内并行抓取页数 |
| `PAGE_MAX_ATTEMPTS` | 3 | 同一页最多重试次数，超过则中止并保留断点 |
| `AUTO_RESUME_ON_BOOT` | true | 启动时自动续跑有断点的全量同步 |
| `AUTO_RESUME_DELAY_SECONDS` | 30 | 延迟多少秒开始续跑（让服务先起来） |
| `REQUEST_INTERVAL_SECONDS` | 0.2 | 请求间隔，防源站封禁 |
| `PROXY_ENABLED` | true | 直连失败后是否自动走中转代理 |
| `PROXY_URLS` | 2 个内置地址 | 中转代理列表（逗号分隔），格式 `{proxy}?url=<urlencode(目标地址)>` |
| `PROXY_PREF_TTL_MINUTES` | 30 | "该源站需走代理"的记忆时长，到期重试直连 |
| `PROXY_CONCURRENCY` | 4 | 每个代理的最大并发请求数 |
| `DB_POOL_SIZE` | 10 | 数据库连接池常驻连接数 |
| `DB_MAX_OVERFLOW` | 20 | 连接池峰值溢出连接数 |
| `DB_POOL_TIMEOUT` | 10 | 等连接的秒数，超时返回 503（不是 500） |
| `SEARCH_CONCURRENCY` | 4 | 聚合搜索的并行度（每线程占一个连接） |
| `MERGE_MAX_PER_TABLE` | 3000 | **列表归并每表最多取多少行**（决定能精确翻到第几页，超出回退慢路径） |
| `MERGE_CONCURRENCY` | 4 | 列表归并的并行度（每线程占一个连接，别超过 `DB_POOL_SIZE`） |
| `TOTAL_CACHE_SECONDS` | 60 | 列表 total 的缓存秒数（0 = 每次实时 count） |
| `HOUSEKEEPING_ENABLED` | true | 是否定时清理同步日志 / 回收 WAL |
| `HOUSEKEEPING_INTERVAL_MINUTES` | 60 | 维护任务间隔 |
| `SYNC_LOG_RETENTION_DAYS` | 30 | 同步日志保留天数（`running` 行不删） |
| `SYNC_LOG_KEEP_PER_SOURCE` | 50 | 每个源站最多保留条数（兜底） |
| `SYNC_LOG_MAX_ROWS` | 20000 | `sync_logs` 全表行数上限（兜底） |
| `DATABASE_URL` | `postgresql+psycopg2://vodsync:vodsync@postgres:5432/vodsync` | **必填**，指向 PostgreSQL |
| `AUTH_ENABLED` | true | 访问控制初始开关（首次启动写入数据库，之后以面板设置为准） |
| `ADMIN_PASSWORD` | 666666 | 首次启动的管理面板密码，登录后请改掉 |
| `SESSION_TTL_DAYS` | 7 | 面板登录会话有效期（天） |
| `LOGIN_MAX_FAILS` / `LOGIN_LOCK_MINUTES` | 5 / 5 | 连续登录失败锁定策略 |
| `TRUST_PROXY` | false | 是否信任反代的 `X-Forwarded-For`（反代之后才开，否则锁定可被伪造 IP 绕过） |
| `API_KEY` | 空 | 兼容项：设置后作为一枚额外有效的 Token |
| `ONLY_OK_SOURCES` | true | 只同步在线源站 |

## 群晖部署

详见 [docs/deploy-synology.md](docs/deploy-synology.md)。

**客户端 App 对接**：完整的接口文档（含参数表、响应示例、App 实现流程、常见坑）见 **[docs/CLIENT_API.md](docs/CLIENT_API.md)**；给已有 App **加 API Token** 的施工单见 **[docs/APP_TOKEN_UPGRADE.md](docs/APP_TOKEN_UPGRADE.md)**（整篇丢给 AI 即可）；机器可读规范直接导入 `http://<host>:8000/openapi.json`。

## 常见坑

1. **pgdata 必须持久化**：数据库在 postgres 容器里，`/var/lib/postgresql/data` 不挂载 = 重建容器数据全丢；
   `/app/data` 也建议挂载（里面是同步断点 `pause_state.json`）
2. **pgdata 别放机械盘**：220 万行 + 索引约 2~4GB，机械盘上全量同步和列表查询都会明显变慢（放 SSD）
3. **PG 大版本不能直接换镜像**：`postgres:16-alpine` 升到 17 前必须 `pg_dump` 导出再导入，直接改 tag 会启动失败
4. **全量同步耗时**：百万级数据首次同步约需数小时（受源站限速影响），期间断点可续传；`GET /api/v1/sources` 中 `syncing=true` 表示还在同步
5. **部分源站 403**：源站有 UA/频率风控，可调大 `REQUEST_INTERVAL_SECONDS`；日志中会记录失败原因并自动跳过
6. **群晖 ARM 机型**：CI 已构建 `linux/arm64` 镜像，直接可用
7. **磁盘占用**：数据 + 索引约 1.5 倍原始 CSV 大小，PG 的 WAL 在 `pg_wal/` 下自行回收，不用手工清
8. **接口变慢 / 列表首页要好几秒**：先看 `/api/v1/videos` 响应里的 `query_ms` 和 `pagecount`
   - `query_ms` 大且翻页很深（超过 `MERGE_MAX_PER_TABLE / page_size`）→ 走了 UNION 回退路径，属预期；
     想支持更深翻页就调大 `MERGE_MAX_PER_TABLE`（代价是内存）
   - 一直慢则看 PG 侧：`docker exec vodsync-pg psql -U vodsync -d vodsync -c "select count(*) from pg_stat_activity"`
9. **服务「假死」/接口全 500**：多数是同时跑了太多同步任务把连接池占满。排查顺序：
   - `GET /api/v1/stats` 看 `sync_load`（running/queued/limit）与 `db_pool.checked_out`；`checked_out` 长期贴近 `size+max_overflow` 说明压力过大
   - 面板上同时点多个「全量」时，超出的会进入「排队中」，属正常排队，不要重复点击
   - 想更快完成多个源站全量，调大 `SOURCE_SYNC_CONCURRENCY`（NAS 上建议 ≤3，抓取本身就是瓶颈）
   - 若日志仍有 `QueuePool limit ... reached`，把 `DB_POOL_TIMEOUT` 调小（如 5）让请求快速失败，并检查是否有外部脚本在狂刷列表接口

## 项目结构

```
vodsync/
├── app/
│   ├── main.py          # 应用入口（启动初始化 + 调度器 + 鉴权中间件）
│   ├── config.py        # 全部配置项（环境变量）
│   ├── database.py      # 引擎与连接池（生产 PG；SQLite 仅测试）
│   ├── models.py        # sources / sync_logs / settings / 动态 vod_{slug} 表
│   ├── maccms.py        # 苹果 CMS 接口客户端（重试/限速/代理降级）
│   ├── syncer.py        # 同步引擎（全量断点续传 + 增量水位线 + 重启恢复）
│   ├── multiquery.py    # 多源列表查询（每表 top-K + 进程内归并）
│   ├── housekeeping.py  # 数据维护（sync_logs 清理 / WAL checkpoint）
│   ├── auth.py          # 访问控制（面板密码 / 会话 / API Token）
│   ├── pause.py         # 暂停状态持久化（原子写）
│   ├── scheduler.py     # APScheduler 定时任务
│   ├── utils.py         # 播放地址解析 / 分页参数
│   ├── routes/api.py    # 全部 API 路由（含 /search 聚合搜索、/stats 统计）
│   └── web/             # 管理面板 index.html + 登录页 login.html（单文件内联，无需构建）
├── .github/workflows/docker-image.yml   # CI：多架构镜像构建推送
├── Dockerfile / docker-compose.yml      # compose 内含必备的 postgres 服务
├── .env.example
├── scripts/                             # 测试脚本（用 SQLite 临时库，不需要 PG）
└── docs/
    ├── API_SPEC_FOR_AI.md   # ★ 交付给 AI 的完整 API 文档（字段字典/状态语义/代码骨架/排查表）
    ├── CLIENT_API.md        # 简明对接说明（人类速查）
    ├── APP_TOKEN_UPGRADE.md # ★ 给 App 加 API Token 的升级施工单（交给 AI 执行）
    ├── openapi.json         # OpenAPI 3.1 快照（可导入代码生成器）
    └── deploy-synology.md   # 群晖部署
```

## 变更记录

### v1.1.0
- 新增 **Web 管理面板**（`/`）：概览统计、全量同步进度条、单站/全站手动触发、条数校准、源站日志、跨源搜索
- 新增 **聚合搜索接口** `GET /api/v1/search`：线程池并行查询各源站表 + 内存归并分页，单源失败隔离，LIKE 通配符转义
- 新增 `GET /api/v1/stats`（面板概览）、`POST /api/v1/maintenance/recount`（条数校准）
- `sources` 表新增 `total_pages` 列，启动时自动迁移旧库（无需手动改表）
- 修复：全量同步中断时 `total_videos` 长期为 0（现在每次断点保存同时刷新条数；启动时也会校准）
- 版本号 1.0.0 → 1.1.0

### v1.0.0
- 首个版本：源站发现、全量断点续传、增量水位线同步、每源站一表、REST + MacCMS 兼容 API、CI 多架构镜像、群晖部署文档

### v1.1.1
- 新增 `GET /source`：资源站 API 清单（上游 `api` 地址 + 本地 `local_api`/`local_list_api` + 已入库条数），支持 `?status=ok|down`、`?only_ready=true` 过滤
- 管理面板新增「资源站 API」按钮，一键查看清单
- 断点保存时同步更新 `last_sync_at`，面板与 `/source` 的"最后同步时间"不再为空

### v1.1.2
- `/api/v1/videos` 增强：新增 `ids=1,2,3`（批量取详情，按请求顺序返回，含播放地址）与 `ac=detail`（列表带播放地址）
- 查询参数归一化：兼容 `&?ac=detail` 这类多打问号的写法（原会被静默忽略）
- 详情字段统一：列表/详情/兼容接口共用 `_detail_item()`，补齐 `vod_play_from`
- `ids` 查不到时返回 404 并提示如何搜索确认 `vod_id`（原来只返回空列表）

### v1.1.3
- 面向客户端 App 对接的整理：
  - 新增 `GET /api/v1/categories`：分类清单（type_id/type_name/count，支持多源合并），分类浏览入口
  - `/api/v1/videos` 新增 `type_id` 参数（可与 `ac=detail`、`wd` 组合）
  - 新增 CORS 支持（H5/Web App 可跨域直调，`CORS_ORIGINS` 环境变量可限制来源）
  - 新增 **docs/CLIENT_API.md**：面向 AI 客户端开发的完整 API 文档（约定/鉴权/全部接口/响应示例/App 实现流程/常见坑）

### v1.2.0
- 新增**同步暂停/继续**，三层级独立控制：全局（全量+增量+定时任务一起停）、仅全量、仅增量
- 暂停语义为**检查点式优雅停止**：当前抓取批次完成后保存断点再停，不丢数据；继续时自动从断点续传（增量则水位线不动、重跑增量）
- 暂停状态持久化到 `data/pause_state.json`，服务重启后仍生效；启动时自动恢复
- 新增 API：`GET /api/v1/pause`、`POST /api/v1/pause?scope=`、`POST /api/v1/resume?scope=`（resume 会自动续跑受影响的源站）；`/api/v1/stats` 新增 `pause` 字段；同步日志新增 `paused` 状态
- 管理面板新增「同步暂停控制」区：三个开关 + 页面顶部暂停横幅 + 源站行"已暂停"徽标
- 定时调度在暂停期间自动跳过对应任务（恢复后下一周期自动执行）
- 新增测试 `scripts/test_v12_pause.py`（15 项，含真实网络中途暂停→断点 8 页→继续→16 页的端到端验证）

### v1.2.1
- 新增**单源站级暂停/继续**：源站列表每行增加「暂停 / 继续」按钮，暂停中的站显示橙色「本站暂停」徽标
- 新增 API `POST /api/v1/sources/{slug}/pause` 与 `POST /api/v1/sources/{slug}/resume`；`/api/v1/sources`、`/api/v1/sources/{slug}` 返回 `paused` / `paused_at`
- 判定顺序：全局/分层暂停 → 单源站暂停，任一命中即停；单源站暂停不影响其他源站，`sync-all` 与定时任务自动跳过被暂停的站
- `sources` 表新增 `paused` / `paused_at` 列，启动自动迁移旧库
- 测试 `scripts/test_v12_pause.py` 扩至 23 项（含单源站暂停隔离、运行中单源站暂停断点保存、全局暂停下的阻断提示）

### v1.2.2
- 修复：源站列表的「暂停」按钮在**未同步的源站**上也显示（误把"有断点未跑完"当成"正在跑"）
- 新增运行时状态 `syncer._running`：`sync_source` 进入即登记、结束即注销，`/api/v1/sources` 与详情返回 `running` / `running_mode` / `running_seconds`
- 面板交互改为按运行态渲染：**只有 `running=true` 才显示「暂停」**；已暂停显示「继续」；空闲站不显示这两个按钮
- 进度列区分三态：正在跑（流动进度条 + 已运行时长）/ 有断点但未跑（"断点 N/M 页 · 未在同步"）/ 已完成
- 修正：全新全量同步在取到首页总页数前 `progress` 误算为 100，现为 0；`stats.sources_syncing` 改为"真正在跑的任务数"，另加 `sources_unfinished` 表示有断点的站数
- 单源站暂停接口返回 `was_running`，并区分提示"将在当前批次结束后停止"与"当前未在同步，暂停标记已记录"
- 新增 `scripts/test_panel_render.js`：Node 里用 DOM 桩执行面板真实 JS，逐行断言「暂停」按钮只出现在运行中的行

### v1.2.3
- 项目已发布到 GitHub：https://github.com/consoleaiden/vodsync
- GitHub Actions 首次构建成功（amd64 + arm64 双架构），镜像公开可拉取：`ghcr.io/consoleaiden/vodsync:latest`、`ghcr.io/consoleaiden/vodsync:1.2.2`
- `docker-compose.yml` 与群晖部署文档的镜像地址已替换为实际发布地址

### v1.3.0
- 新增**按源站的自动同步开关**（全局定时调度保留不变：每 6 小时全站增量、每 12 小时刷新源站列表）：
  - `sources` 表新增 `auto_sync` 列（默认 1=参与），启动自动迁移旧库，状态持久化重启不丢
  - 关闭后该源站**不参与**定时增量任务与全站同步（`sync-all` / 定时 / 增量续跑），手动单站同步不受影响
  - 新增 API：`POST /api/v1/sources/{slug}/auto-sync?enabled=`（单站）、`POST /api/v1/sources/auto-sync?enabled=&slugs=`（批量，`slugs=all` 为全部）
  - `/api/v1/sources`、`/api/v1/sources/{slug}` 返回 `auto_sync` 字段
  - 面板源站列表：每行「自动」开关 + 复选框多选（全选框跨刷新保留）+ 「全部开启 / 全部暂停 / 开启所选 / 暂停所选自动同步」批量按钮
  - 测试 `scripts/test_v130_autosync.py`（13 项：默认值/单站/批量/sync_all 过滤/续跑过滤/路由注册）

### v1.4.0
- 新增**中转代理降级通道**：源站直连不可达/被风控时，自动按顺序切到 API 中转代理拉取数据
  - 请求格式 `{proxy}?url=<urlencode(目标地址)>`，默认内置 `https://pz.v88.qzz.io/`、`https://mconfig.viptv.work/`，可用 `PROXY_URLS` 覆盖
  - 路由策略：直连优先 → 失败自动切代理；连接失败/超时/403/429/451 立即切换（不做无意义重试），5xx 与 JSON 解析失败在当前路由重试后再切换
  - **代理偏好记忆**：某源站走代理成功后记住 30 分钟（`PROXY_PREF_TTL_MINUTES`），期间直接走代理避免每次先等直连超时；直连恢复后自动清除记忆
  - 每个代理由信号量限制并发（`PROXY_CONCURRENCY`，默认 4），避免打挂中转服务
  - `/api/v1/stats` 新增 `proxy` 段（是否启用、代理列表、当前走代理的源站、成功/失败计数）；面板操作区显示代理状态
- 修复：**未同步过的源站进度误显示「已完成」**
  - 根因：进度计算把"没有断点且不在跑"一律判为 100，未区分"从未同步"
  - 修复：新增 `synced` 判定（水位线 / 已入库 / 有断点 任一成立才算同步过），从未同步时进度恒为 0
  - `/api/v1/sources`、`/api/v1/sources/{slug}` 新增 `synced` 字段；面板对未同步站显示 `0%` +「从未同步 · 可点「增量」开始」
- 测试：新增 `scripts/test_v140_proxy_progress.py`（19 项：URL 编码、直连不可达/403 自动切代理、记忆与 TTL、代理关闭即报错、进度口径）；`scripts/test_panel_render.js` 增加"未同步不得显示已完成"断言

### v1.4.1
- 修复：**并发同步过多导致数据库连接池耗尽，整个服务「假死」**（NAS 上表现为容器不健康 / 面板全 500）
  - 根因链：面板连点十几个「全量」→ 每个请求各起一个线程、无任何上限 → SQLite 只允许一个写者，十几个写线程互相等写锁 → 等锁期间线程仍占着连接池连接（默认 5+10）→ 池满后连 `/healthz` 都 30s 超时 → Docker 判容器不健康
  - **全局并发闸门**：`sync_source` 内加信号量，无论谁触发（面板 / 定时 / 续跑 / 批量），同时真正在跑的源站不超过 `SOURCE_SYNC_CONCURRENCY`（默认 2），其余排队且**不占数据库连接**；`/stats.sync_load`、`/sources[].queued` 可看排队情况，面板显示「排队中」
  - **同源站防重入**：同一源站已在排队/运行时重复触发直接返回 `status=already_running`，不再双写同一张表
  - **写事务串行化**：新增 `database.write_txn()`，SQLite 下进程内串行所有写事务，消除 `database is locked` 与写锁抢占；所有业务写操作统一走它
  - **count(*) 移出写事务**：断点保存 / 水位线更新不再"持着写锁做全表扫描"，改用独立读连接先统计
  - **修掉一处连接泄漏**：`GET /api/v1/videos` 原来是 `engine.connect().execute(...)` 直接丢弃对象（靠 GC 归还），改为 `with` 显式归还
  - **连接池可配 + 更容易排障**：`DB_POOL_SIZE`(10) / `DB_MAX_OVERFLOW`(20) / `DB_POOL_TIMEOUT`(10)，`/api/v1/stats` 新增 `db_pool`、`sync_load`
  - **`/healthz` 不再因数据库繁忙返回 5xx**：始终 200，用 `status=ok|degraded` + `db:true|false` 表达，避免 Docker 把还在干活的容器反复重启（健康检查只看状态码）
  - **503 语义**：连接池排队超时 / `database is locked` 统一返回 `503` + `Retry-After: 5`，不再是 500 堆栈
  - 聚合搜索并行度收敛为 `SEARCH_CONCURRENCY`（默认 4，原来固定 8，每线程各占一个连接）
- 测试：新增 `scripts/test_v141_pool.py`（17 项：池参数、无泄漏、并发闸门峰值、防重入、并发写、池满时 healthz、断点保存）与 `scripts/loadtest_concurrency.py`（端到端：12 源站同时全量，实测峰值并发 2、探活 0 失败、连接池归零、720 条数据全部入库）

### v1.4.2
- 新增 `scripts/migrate_sqlite_to_pg.py`：SQLite → PostgreSQL 数据迁移，**切库不需要重新同步**
  - 迁移 `sources` / `sync_logs` / 全部 `vod_*` 表，保留 `watermark`（增量水位线）与 `last_page`（全量断点），迁完直接续增量
  - 幂等（重复执行按主键跳过）、每批一个事务（中断可续跑）、PG 下自动对齐 SERIAL 序列
  - `--dry-run` 预估规模、`--only-meta` 只搬元数据、`--tables` 指定源站、`--replace` 覆盖、`--verify` 只校验
  - 迁移后自动校验：表齐全性、行数一致、关键字段逐行比对
- 镜像内包含迁移脚本，可直接 `docker run --entrypoint python .../migrate_sqlite_to_pg.py`
- 测试：`scripts/test_migration.py`（42 项：跨批翻页、字段保真、幂等、替换、过滤、dry-run、错误提示）

### v1.5.0
- 新增**访问控制**：面板密码登录 + API Token，防止接口被公网滥用
  - `settings` 表（key/value，启动自动建）：`auth_enabled` / `admin_password_hash` / `api_token` / `session_secret`
  - 面板登录：默认密码 `666666`，PBKDF2-SHA256（20 万轮 + 随机盐），HttpOnly 签名会话 cookie（默认 7 天）
  - 改密码后所有旧会话立即失效（签名携带密码指纹）；连续失败 5 次锁 5 分钟
  - API Token：`Authorization: Bearer` / `X-API-Key` / `?token=` 三种带法；面板可复制、重置
  - 开关：可随时关闭（`enabled=false` 时接口不校验），重新开启时自动更换 Token
  - `/healthz` 与 `/api/v1/auth/*` 永远免鉴权；`/` 未登录时 303 跳 `/login`
  - 兼容旧的 `API_KEY` 环境变量（作为额外 Token）
- 新增接口：`POST /api/v1/auth/{login,logout,password,settings,token}`、`GET /api/v1/auth/{state,token}`
- 新增页面：`/login`（登录页），面板右上「⚙ 设置」抽屉（改密码 / Token / 开关 / 退出登录）
- 测试：`scripts/test_v150_auth.py`（40 项：默认密码、三种 Token 带法、登录跳转、锁定、改密失效、开关换 Token、开放重定向防护）
- 版本号 1.4.2 → 1.5.0

### v1.5.1
- 修复：`/source`（源站清单 JSON）不带 `/api/` 前缀，此前只带 Token 的客户端会被拦成
  `401 请先登录管理面板`，现在与其它接口一致认 Token
- 新增 **`docs/APP_TOKEN_UPGRADE.md`** —— 交给 AI 给 App 加 Token 的施工单（含各平台拦截器代码、401 处理策略、自测清单）
- 测试：鉴权用例补 4 项（`/source` 认 Token、双空格 Bearer、无空格 Bearer 拒绝），共 44 项
- 版本号 1.5.0 → 1.5.1

### v1.5.2
- 分页：`API_PAGE_SIZE_DEFAULT` 默认由 20 调到 **30**（三列宫格 10 行 × 3 列，每页正好整行）
- 修复：`/api/v1/search` 的 `page_size` 之前写死 `20 / 100`，不跟随配置；现在与列表接口一致读 `API_PAGE_SIZE_DEFAULT` / `API_PAGE_SIZE_MAX`
- 新增 `scripts/test_v151_paging.py`（14 项：默认值、显式值、最后一页不足整页、越界 422、search 与 MacCMS 兼容接口一致性）
- 文档：`docs/APP_TOKEN_UPGRADE.md` 新增「9. 分页约定：三列宫格每页 30 条」（含取数策略与三个常见坑）
- 版本号 1.5.1 → 1.5.2

### v1.6.0
修复三个「静默丢数据」的同步缺陷（复盘报告 `docs/REVIEW_2026-11-03.md`），围绕断电/重启恢复补齐冗余：

- **P0-1 全量同步丢页**：批内单页失败时旧代码用 `max(成功页)` 存断点 → 跳过失败页且清断点标记完成。
  现在断点只落在「连续完成的前缀」，失败页在本次内重试（`PAGE_MAX_ATTEMPTS`），仍失败则中止并保留断点，
  状态记 `incomplete`（新增，面板显示「部分完成」）
- **P0-2 增量水位线越过未入库数据**：水位线改为按「实际写库的最大 `vod_time`」推进；有失败批次时
  封顶在失败记录之前；详情缺失的 ID 追加一次补抓，仍未返回才按「源站已下架」跳过并记 `missing`
- **P0-3 增量 list 单页失败导致整站崩溃**：`except` 分支里重复调用 `fut.result()` 会重抛异常；
  已改为按页记录，并且索引不完整时**冻结水位线**
- **重启恢复**：启动时把残留 `running` 日志标记为 `interrupted`；新增 `AUTO_RESUME_ON_BOOT`
  （默认 true，延迟 30s）自动续跑有断点的源站；`pause_state.json` 改原子写（tmp + `os.replace`）
- **安全**：`_client_ip` 不再无条件信任 `X-Forwarded-For`（默认只看对端地址，`TRUST_PROXY=true`
  才取最后一跳），修复「伪造 IP 绕过登录失败锁定」
- **隐患**：修复 `database.write_lock_only()` 被 `@contextmanager` 装饰两层导致调用即 `TypeError`
- 测试：新增 `scripts/test_v160_recovery.py`（42 项，含三个缺陷的复现转回归 + 自动续跑），全仓 207 项通过
- 版本号 1.5.2 → 1.6.0

### v1.6.1
收紧 `/api/v1/auth/state` 的信息暴露面（该接口在免鉴权白名单里，登录页需要它）：

- **删除 `token_hint`**：原来会回 `vs_HYgS…u51c` 这种前 7 位 + 后 4 位片段。Token 是唯一凭据，
  暴露片段等于把搜索空间从 43 字符砍到十几个字符，而这个接口**任何人不登录都能调**
- **未登录只回 `enabled` / `logged_in`**：`has_token`、`token_created_at`、`password_changed_at`
  收敛到「已登录」才返回 —— 它们能用来推断服务器是否配过 Token、什么时候改的密码
- 响应加 `Cache-Control: no-store`，避免代理/浏览器缓存状态
- 启动日志里的 Token 也改为只报「已生成/未生成」，不再打印片段
- 文档同步：`docs/APP_TOKEN_UPGRADE.md` 的 state 示例与「给 AI 的提示词」第 6 条改为
  「任何日志都不出现 Token，最多记布尔位」；`docs/API_SPEC_FOR_AI.md` 同步
- 测试：`scripts/test_v150_auth.py` 增加 10 项断言（未登录字段白名单、响应体不含 Token 前后缀、缓存头）
- 版本号 1.6.0 → 1.6.1

### v1.7.0
Wave 2：多源查询重写 + 数据维护 + 源站身份修复 + **生产统一 PostgreSQL**。

- **多源列表查询**（`app/multiquery.py`，新）：不带 `source` 查全部源站时，旧实现把 52 张表
  `UNION ALL` 成子查询再套 `count(*)` + `ORDER BY ... OFFSET`，等于每次请求把 220 万行物化排序
  （首页 4~8s，第 20 页 10s+）。改为每表各取 top-K（走 `idx_*_time` 倒序）+ 进程内多路归并：
  全局第 N 行必然在所属表的前 N 行内，**结果精确**而非近似；首页降到几十毫秒
  - `ac=detail` 改两段式：先瘦列排序分页，再只对当前页那几十行补 `content`/`play_url`
  - `total` 并行 count + 60s 缓存；单表失败降级为 `total_is_partial` 而不是整页 500
  - 翻页超过 `MERGE_MAX_PER_TABLE / page_size`（默认第 100 页）自动回退 UNION，宁可慢不快错
  - 响应新增 `query_ms` / `total_is_partial` / `sources_failed`（App 可忽略）
- **数据维护**（`app/housekeeping.py`，新）：`sync_logs` 原来只增不减。现在每小时三级清理
  （超期 / 每源站上限 / 全表上限，`running` 行永不删）+ SQLite WAL checkpoint（PG 下 no-op）
- **源站 slug 碰撞**（`syncer.refresh_sources`）：只按 `slugify(name)` 判重时，两个纯 ASCII 站名
  撞车（`"HD 资源"` 与 `"HD-资源"` 都得 `hd`），第二个站被当成已存在 → 它的 `api_url` 永远写不进去，
  表现为**长期同步的是别人家的接口且无任何报错**。改为按 `api_url` 认源站身份，slug 冲突时追加
  `_2`/`_3`；已登记源站的 slug 永不变（它是表名与 API 路径的一部分）
- **生产统一 PG**：默认 `DATABASE_URL` 指向 `postgresql+psycopg2://vodsync:vodsync@postgres:5432/vodsync`；
  compose 里 `postgres` 从注释段升为必备服务（`depends_on: service_healthy` + `shm_size`）；
  删除 `scripts/migrate_sqlite_to_pg.py` 与其 58 项测试（数据已迁完，git 历史可查）；
  SQLite 驱动仅保留给自动化测试
- **安全**：启动日志不再打印带密码的 `DATABASE_URL`（`init_db` 内已打脱敏版本）
- 测试：新增 `scripts/test_v170_wave2.py`（42 项：归并与 UNION 逐条比对、分页边界、detail 两段式、
  LIKE 转义、缓存命中、深页回退、日志三级清理、slug 碰撞），全仓 201 项通过
- 版本号 1.6.1 → 1.7.0
