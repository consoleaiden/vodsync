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
- **可选鉴权**：设置 `API_KEY` 后所有接口需携带 `X-API-Key`
- **多数据库**：默认 SQLite（WAL），一行环境变量切换 PostgreSQL

## 技术选型说明

| 项 | 选型 | 理由 |
|---|---|---|
| 语言/框架 | Python 3.12 + FastAPI | IO 密集型抓取，多线程 + httpx 连接池足够；代码易读易改 |
| 数据库（推荐） | SQLite WAL 模式 | 群晖单机部署零依赖；写集中在同步任务，读多写少场景 WAL 并发完全够用；备份=复制单文件 |
| 数据库（可选） | PostgreSQL | 需要多实例共享/超大并发读时切换，仅改 `DATABASE_URL` |
| 调度 | APScheduler | 轻量、随应用进程运行，无需额外 cron 容器 |

## 快速开始（本地开发）

```bash
# 1. 安装依赖（建议 Python 3.10+）
pip install -r requirements.txt

# 2. （可选）复制并修改环境变量
cp .env.example .env

# 3. 启动
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

# 4. 打开管理面板 / 交互式文档
# 管理面板: http://127.0.0.1:8000/
# API 文档: http://127.0.0.1:8000/docs
```

首次启动会自动：建库 → 拉取源站列表 → 后台开始全量同步（数据量大，需数小时）。

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

> 若设置了 `API_KEY`，面板首次请求会弹窗要求输入密钥，输入后保存在浏览器 localStorage。

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

## 配置项（环境变量）

完整列表见 [.env.example](.env.example)，关键项：

| 变量 | 默认 | 说明 |
|---|---|---|
| `SYNC_INTERVAL_MINUTES` | 360 | 增量同步间隔（分钟） |
| `SOURCE_REFRESH_INTERVAL_MINUTES` | 720 | 源站列表刷新间隔 |
| `SOURCE_SYNC_CONCURRENCY` | 2 | **同时真正在跑的源站同步数上限**（全局闸门，排队不占资源；NAS 建议 1-2） |
| `PAGE_CONCURRENCY` | 4 | 单源站内并行抓取页数 |
| `REQUEST_INTERVAL_SECONDS` | 0.2 | 请求间隔，防源站封禁 |
| `PROXY_ENABLED` | true | 直连失败后是否自动走中转代理 |
| `PROXY_URLS` | 2 个内置地址 | 中转代理列表（逗号分隔），格式 `{proxy}?url=<urlencode(目标地址)>` |
| `PROXY_PREF_TTL_MINUTES` | 30 | "该源站需走代理"的记忆时长，到期重试直连 |
| `PROXY_CONCURRENCY` | 4 | 每个代理的最大并发请求数 |
| `DB_POOL_SIZE` | 10 | 数据库连接池常驻连接数 |
| `DB_MAX_OVERFLOW` | 20 | 连接池峰值溢出连接数 |
| `DB_POOL_TIMEOUT` | 10 | 等连接的秒数，超时返回 503（不是 500） |
| `SEARCH_CONCURRENCY` | 4 | 聚合搜索的并行度（每线程占一个连接） |
| `DATABASE_URL` | SQLite | 切 PostgreSQL：`postgresql+psycopg2://user:pass@host:5432/db` |
| `API_KEY` | 空 | 设置后接口需带 `X-API-Key` 头 |
| `ONLY_OK_SOURCES` | true | 只同步在线源站 |

## 从 SQLite 迁移到 PostgreSQL（不需要重新同步）

换成 PostgreSQL 后新库是空的。**不用把所有源站重新抓一遍** —— 用迁移脚本把 SQLite 里的数据整体搬过去即可，
连 `watermark`（增量水位线）和 `last_page`（全量断点）都会保留，迁完直接接着做增量。

### 0) 先把 PG 加进 compose（群晖 Container Manager 图形界面）

`docker-compose.yml` 里的 `postgres` 段是**注释掉**的，所以 `docker ps | grep postgres` 什么都没有、迁移时报
`could not translate host name "postgres"`。启用方式二选一：

- **Container Manager** → 项目 → 选中 `vodsync` → 编辑 → 把末尾 `# postgres:` 起的整段注释去掉（或按下面整段粘贴，**2 空格缩进，与 `vodsync:` 同级**）→ 保存 → 构建/启动。
- 项目是 SSH 里 `docker compose up` 起的、UI 里看不到时，用 **File Station** 右键 `docker-compose.yml` → 打开方式 → 文本编辑器 改。

```yaml
  postgres:
    image: postgres:16-alpine
    container_name: vodsync-pg
    restart: unless-stopped
    environment:
      POSTGRES_USER: vodsync
      POSTGRES_PASSWORD: vodsync
      POSTGRES_DB: vodsync
      TZ: Asia/Shanghai
    volumes:
      - /volume2/docker/vodsync/pgdata:/var/lib/postgresql/data   # 与 data 卷同一前缀
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U vodsync -d vodsync"]
      interval: 10s
      timeout: 5s
      retries: 10
      start_period: 20s
```

> 两个不要：**不要映射 5432 到宿主机**（容器间用服务名直连，映射容易和已有 PG 抢端口）；
> **不要在这一步就解开 `DATABASE_URL`** —— 空库会让 vodsync 以空库启动、白抓一遍，先迁移再切。
> 想让 vodsync 等 PG 就绪再起，可在 vodsync 服务下加 `depends_on: {postgres: {condition: service_healthy}}`。

```bash
# 0) 先停服务，保证源库不再写入（PG 容器要先起好，两种起法见下方"一条命令起 PG"）
sudo docker compose stop vodsync

# 1) 预演：看看要搬多少（不写目标库）
#    compose run 会复用同一网络和同一 data 卷，所以 --source 默认就指向 /app/data/vodsync.db
sudo docker compose run --rm vodsync python /app/scripts/migrate_sqlite_to_pg.py \
  --target "postgresql+psycopg2://vodsync:vodsync@postgres:5432/vodsync" --dry-run

# 2) 正式迁移（可重复执行，已存在的行按主键跳过）；同上命令去掉 --dry-run

# 3) 改 DATABASE_URL 并启动
sudo docker compose up -d
```

> 如果 PostgreSQL 装在别的机器（不在 compose 内网），把 target 换成那台机器的地址即可，例如
> `postgresql+psycopg2://vodsync:vodsync@192.168.1.10:5432/vodsync`；此时用
> `docker run --rm -v /volume2/docker/vodsync/data:/data --entrypoint python ghcr.io/consoleaiden/vodsync:latest /app/scripts/migrate_sqlite_to_pg.py ...` 也行，注意 `--source /data/vodsync.db`。

**不想改 compose 的话，一条命令就能起 PG**（挂到本项目网络并带 `postgres` 别名，迁移命令和 `DATABASE_URL` 都不用改）：

```bash
# 先确认真没有 PG：docker ps | grep -i postgres 无输出、docker inspect postgres 报 No such object
mkdir -p /volume2/docker/vodsync/pgdata        # 与 data 卷同级，按实际路径改
docker run -d --name vodsync-pg --restart unless-stopped \
  --network vodsync_default --network-alias postgres \
  -e POSTGRES_USER=vodsync -e POSTGRES_PASSWORD=vodsync -e POSTGRES_DB=vodsync \
  -e TZ=Asia/Shanghai \
  -v /volume2/docker/vodsync/pgdata:/var/lib/postgresql/data \
  postgres:16-alpine

# 等日志出现 "database system is ready to accept connections" 再迁移
docker logs -f vodsync-pg
docker exec vodsync-pg pg_isready -U vodsync
```

> `vodsync_default` 是 compose 项目默认网络名（`<项目名>_default`），用 `docker network ls | grep vodsync` 核对。
> 若改用 compose 起 PG，记住两者 `container_name` 都是 `vodsync-pg`，需要先 `docker rm -f vodsync-pg` 再 `up -d`。

### 报错 `could not translate host name "postgres"` 怎么办

**这是 Docker 网络问题，不是 PG 没启动。** `docker compose run` 起的临时容器只认识「同一个 compose 项目网络」里的服务名；
如果 `postgres` 这个容器是 `docker run --name postgres` 或另一个 compose 项目起的，名字虽然叫 postgres，但在本项目网络里没有这个 DNS 记录。

```bash
# 看 PG 挂在哪个网络（以及是否真的在跑）
docker ps | grep -i postgres
docker inspect -f '{{json .NetworkSettings.Networks}}' postgres
docker network ls | grep vodsync       # 本项目默认网络名，通常 vodsync_default

# 解法①（推荐，PG 不用重建、数据不动）：把 PG 接进本项目网络，并给它 postgres 这个别名
docker network connect --alias postgres vodsync_default postgres

# 自测：能打印出 IP 就说明通了
docker compose run --rm vodsync python -c "import socket;print(socket.gethostbyname('postgres'))"
```

解法②（绕过 DNS）：PG 的 5432 已映射到宿主机时，直接用宿主机地址 ——
`--target "postgresql+psycopg2://vodsync:vodsync@<NAS局域网IP 或 172.17.0.1>:5432/vodsync"`。

解法③（最干净，适合 PG 还是空的/新建的）：解开 compose 里的 `postgres` 段，让两个服务同属一个项目网络，
`docker compose up -d postgres` 后 `postgres:5432` 自然可解析。注意先停掉旧的同名 PG 容器（否则 5432 端口冲突）。

- 迁移内容：`sources`（源站元数据 + 水位线 + 断点 + 暂停/自动同步开关）、`sync_logs`、全部 `vod_*` 数据表
- 脚本结束会自动校验：表是否齐全、行数是否一致、`watermark`/`last_page`/`paused`/`auto_sync` 等关键字段逐行比对
- 只想搬元数据不搬视频数据：`--only-meta`；只搬部分源站：`--tables jkun,t1`
- 想要源库覆盖目标库（重跑迁移修正脏数据）：`--replace`；只校验不迁移：`--verify`
- **索引在数据全部写完后统一创建**（先插数据再建 B-tree，比边插边维护快数倍），所以日志里会先看到“迁移数据”，跑完才看到“建索引”
- PostgreSQL 下迁移结束会自动执行 `ANALYZE`（更新统计信息，让后续查询走对索引）
- 迁移前会先探测目标库连通性，连不上直接列出排查清单（容器是否在跑 / 主机名端口 / 库和用户是否已建）
- 速度实测：30 万行 5.7 秒（本地 SQLite 目标，约 5.2 万行/秒）；220 万行在 NAS 上连 PG 预计 **2~5 分钟**，加建索引 10~30 秒
- 磁盘预留：PG 侧约为 SQLite 库大小的 1.5 倍（数据 + 索引 + WAL）
- 大数据量想再快一点：`--batch 5000`（默认 2000）
- 迁移完成后 PG 下写事务不再需要进程内串行，可把 `SOURCE_SYNC_CONCURRENCY` 从 2 调到 4~6

> 兜底方案：如果 SQLite 里本来就没多少数据（比如只有几个站跑过），直接换 `DATABASE_URL`
> 重新全量同步更省事 —— 迁移的收益在于**省掉百万级的重新抓取**。

### 把 pgdata 搬到 SSD（可选，提升 PG 随机 IO）

**不能边跑边复制，也不能用 File Station 拖拽**（会丢 uid/gid，PG 启动直接报 data directory has wrong ownership）。
正确姿势是「干净停库 → 保权限复制 → 改挂载路径 → 重建容器 → 逐表校验」：

```bash
cd /volume2/docker/vodsync
sudo docker compose stop vodsync postgres          # 必须停干净，不要 docker kill
docker ps | grep -E "vodsync|postgres"              # 应为空

sudo mkdir -p /volume3/ssd/vodsync/pgdata           # SSD 上的真实路径，必须在本地存储空间上
sudo cp -a /volume2/docker/vodsync/pgdata/. /volume3/ssd/vodsync/pgdata/   # -a 保留属主/权限，或
# sudo rsync -aHAX --numeric-ids /volume2/docker/vodsync/pgdata/ /volume3/ssd/vodsync/pgdata/
sudo du -sh /volume2/docker/vodsync/pgdata /volume3/ssd/vodsync/pgdata     # 两份大小应一致

# 改 compose 里的挂载路径，然后重建容器（改了 volume 必须 recreate 才生效）
#   - /volume3/ssd/vodsync/pgdata:/var/lib/postgresql/data
sudo docker compose up -d --force-recreate postgres
docker exec vodsync-pg pg_isready -U vodsync
```

- `cp -a` 之后**不要 chown**：PG 数据目录属主必须保持原样（alpine 版是 uid 70）。若真丢了权限又没备份，只能重新 initdb。
- **绝不能放在 SMB / NFS / iSCSI 等网络挂载上**，PG 依赖文件锁与 fsync，网络盘会随机报错甚至损坏数据。
- SSD 若在加密共享文件夹里，务必确认**开机自动挂载**：路径不存在时 Docker 会自建空目录，PG 见到空目录会 initdb 一个新库，看起来就是「数据全没了」。
- 旧 pgdata **先别删**，跑一周确认无误再清理（它是唯一的快速回滚点）。
- 搬完用迁移脚本复查一遍（SQLite 与 PG 逐表比行数，最可靠的验证）：
  `sudo docker compose run --rm vodsync python /app/scripts/migrate_sqlite_to_pg.py --target "postgresql+psycopg2://vodsync:vodsync@postgres:5432/vodsync" --verify`

## GitHub 仓库搭建流程（完整步骤）

```bash
# 1. 在 GitHub 上创建空仓库（建议名 vodsync，不要勾选初始化 README）

# 2. 本地初始化并推送
cd vodsync
git init
git add .
git commit -m "feat: vodsync 影视数据同步服务 v1.0"

# 3. 关联远程并推送（替换 <你的用户名>）
git branch -M main
git remote add origin https://github.com/consoleaiden/vodsync.git   # 本项目已推送: https://github.com/consoleaiden/vodsync
git push -u origin main

# 4.（可选）推送版本标签触发多架构正式镜像构建
git tag v1.0.0
git push origin v1.0.0
```

**启用 CI 自动构建镜像：**

1. 仓库已在 `.github/workflows/docker-image.yml` 配好：推 `main` → 构建 `latest` 镜像；推 `v*` 标签 → 构建 `1.x.x` 版本镜像
2. GHCR 无需额外配置（用内置 `GITHUB_TOKEN`），构建完成后镜像在仓库 Packages 页可见
3. 首次使用 GHCR 镜像需将其设为 Public（Packages → vodsync → Package settings → Change visibility），或在群晖登录 GHCR
4. （可选）同步推 Docker Hub：仓库 Settings → Secrets and variables → Actions → 添加 `DOCKERHUB_USERNAME` 和 `DOCKERHUB_TOKEN`
5. 修改代码后只需 `git push`，约 3-5 分钟后群晖即可拉到新镜像

## 群晖部署

详见 [docs/deploy-synology.md](docs/deploy-synology.md)。

**客户端 App 对接**：完整的接口文档（含参数表、响应示例、App 实现流程、常见坑）见 **[docs/CLIENT_API.md](docs/CLIENT_API.md)** —— 专为 AI 客户端开发准备，一份文档即可完成对接；机器可读规范直接导入 `http://<host>:8000/openapi.json`。

## 常见坑

1. **必须挂载数据卷**：SQLite 数据库在容器内 `/app/data`，不挂载则重建容器数据全丢
2. **全量同步耗时**：百万级数据首次同步约需数小时（受源站限速影响），期间断点可续传；`GET /api/v1/sources` 中 `syncing=true` 表示还在同步
3. **部分源站 403**：源站有 UA/频率风控，可调大 `REQUEST_INTERVAL_SECONDS`；日志中会记录失败原因并自动跳过
4. **群晖 ARM 机型**：CI 已构建 `linux/arm64` 镜像，直接可用
5. **切换 PostgreSQL**：修改 `DATABASE_URL` 即可，表结构自动创建；SQLite 文件中的历史数据需手动迁移
6. **磁盘占用**：百万条记录 + 简介文本约 1-3GB，预留空间；SQLite 启用 WAL 后同目录会出现 `-wal`/`-shm` 文件属正常现象
7. **服务「假死」/接口全 500 / 容器不健康**：基本都是同时跑了太多同步任务导致 SQLite 写锁争抢、连接池被占满（v1.4.1 已加全局并发闸门，正常不会再发生）。排查顺序：
   - `GET /api/v1/stats` 看 `sync_load`（running/queued/limit）与 `db_pool.checked_out`；`checked_out` 长期贴近 `size+max_overflow` 说明压力过大
   - 面板上同时点多个「全量」时，超出的会进入「排队中」，属正常排队，不要重复点击
   - 想更快完成多个源站全量，调大 `SOURCE_SYNC_CONCURRENCY`（NAS 上建议 ≤2，SQLite 写是串行的，调太大只会互相等锁）
   - 若日志仍有 `QueuePool limit ... reached`，把 `DB_POOL_TIMEOUT` 调小（如 5）让请求快速失败，并检查是否有外部脚本在狂刷列表接口

## 项目结构

```
vodsync/
├── app/
│   ├── main.py          # 应用入口（启动初始化 + 调度器）
│   ├── config.py        # 全部配置项（环境变量）
│   ├── database.py      # 引擎（SQLite WAL 优化 / PG 支持）
│   ├── models.py        # sources / sync_logs / 动态 vod_{slug} 表
│   ├── maccms.py        # 苹果 CMS 接口客户端（重试/限速/字段提取）
│   ├── syncer.py        # 同步引擎（全量断点续传 + 增量水位线）
│   ├── scheduler.py     # APScheduler 定时任务
│   ├── utils.py         # 播放地址解析 / 鉴权 / 分页
│   ├── routes/api.py    # 全部 API 路由（含 /search 聚合搜索、/stats 统计）
│   └── web/index.html   # Web 管理面板（单文件，内联 CSS/JS，无需构建）
├── .github/workflows/docker-image.yml   # CI：多架构镜像构建推送
├── Dockerfile / docker-compose.yml
├── .env.example
└── docs/
    ├── API_SPEC_FOR_AI.md   # ★ 交付给 AI 的完整 API 文档（字段字典/状态语义/代码骨架/排查表）
    ├── CLIENT_API.md        # 简明对接说明（人类速查）
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
