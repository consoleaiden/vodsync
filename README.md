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
| `SOURCE_SYNC_CONCURRENCY` | 2 | 并行同步的源站数（NAS 建议 1-2） |
| `PAGE_CONCURRENCY` | 4 | 单源站内并行抓取页数 |
| `REQUEST_INTERVAL_SECONDS` | 0.2 | 请求间隔，防源站封禁 |
| `DATABASE_URL` | SQLite | 切 PostgreSQL：`postgresql+psycopg2://user:pass@host:5432/db` |
| `API_KEY` | 空 | 设置后接口需带 `X-API-Key` 头 |
| `ONLY_OK_SOURCES` | true | 只同步在线源站 |

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
git remote add origin https://github.com/<你的用户名>/vodsync.git
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
└── docs/deploy-synology.md
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
