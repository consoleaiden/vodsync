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
- **自定义源站**（v1.8.0）：latest.json 之外的接口可在面板手动登记，带连通性测试；持久化保存，刷新源站列表不会覆盖或删除
- **相关推荐预计算**（v1.9.0）：同步时离线算好每个视频的 Top-12 相关推荐，客户端详情页直接拉取零计算成本；权重重分配 + 标题相似度核心 + 三级兜底，数据稀疏也能命中；面板可维护「明星词库」增强信号
- **明星作品推荐**（v1.9.1）：词库明星名自动回填进缺失演员视频的 `vod_tag`（单正则一次扫描全部命中，性能与词库大小解耦）；`star_map` 倒排映射让「同明星的其他作品」API 走索引毫秒返回
- **按明星查作品**（v1.9.2）：`/api/v1/star/works` 以明星为中心返回完整作品（`total` 不受 limit 影响），配合 `/star/related` 的 `star_counts` 角标与 `truncated` 标志，客户端可直接做「全部 / 按明星」Tab；词库支持别名（多译名归一主名）
- **推荐算法 v2**（v1.10.0）：证据先验 + 内容信号门控，修掉「仅同分类的不相关影片拿高分」；演员改余弦相似度抑制客串；整站重算 **3.9×**；无筛选列表 `total` 走估值
- **详情字段透出**（v1.10.1）：详情与批量详情返回 `vod_actor` / `vod_director` / `vod_area` / `vod_year` / `vod_tag`，NAS 模式下演员与标签不再无数据
- **播放线路过滤**（v1.11.0）：入库保留上游 `vod_play_from`，读接口识别线路类型（m3u8/mp4/flv/mkv/page/unknown）并剔除 `/share/` 这类「H5 分享页」——这类线路返回 text/html、真实地址由页面 JS 生成且带过期 sign，客户端按顺序选第一条必然播不出来；可按源站覆盖策略，`vod_play_url` 原始串不改写，`?play_raw=1` 可排障
- **数据表浏览**（v1.12.0）：管理面板新增只读数据浏览器——下拉切表、按指定字段或全表搜关键词、每页 10/20/50/100 条、页码直跳；后端 `GET /api/v1/admin/tables` 与 `/api/v1/admin/tables/{name}/rows`（表名/字段名走服务端白名单，`settings` 密码哈希表有意排除）

> 文档入口：[`docs/README.md`](docs/README.md)（按用途索引全部文档）

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

## 自定义源站（v1.8.0）

默认源站来自 `latest.json` 自动发现。你自己有的接口（私有站、内网源、朋友给的）可以在面板上
「+ 添加源站」直接登记，**重启、刷新源站列表、重新部署都不会丢**。

### 面板操作

源站列表右上角「+ 添加源站」：

1. 填名称与 API 地址（苹果 CMS 采集接口，如 `https://api.example.com/api.php/provide/vod/`）
2. 点「测试连接」——抓第 1 页，显示总条数、页数与一条示例片名
3. 可选填「源站标识」（决定表名 `vod_{slug}` 和 API 路径 `?source={slug}`）。留空自动生成；
   纯中文名会得到 `s` + 10 位哈希这种不好读的形式，想要 `myhd` 这种可读标识就自己填
4. 「添加」或「添加后立即全量同步」

添加后表格该行出现「自定义」徽标，并多出「编辑」「删除」两个按钮。

### 行为约定

| 场景 | 行为 |
|---|---|
| 刷新源站列表（latest.json） | 自定义源的名称/描述**不会被覆盖**，`status` 按上游更新 |
| latest.json 里没有它 | 不会被删除（refresh 只增不减） |
| 改 API 地址 | **清空该站已入库数据**并重置进度（换接口 = 换数据源） |
| 改源站标识 slug | 不允许——它是表名与 API 路径的一部分，改了等于把已有数据和客户端一起废掉 |
| 删除 | 需 `confirm=<slug>`；默认连数据表一起删；正在同步时拒绝（先暂停） |
| 官方源站 | 不能编辑/删除，只能用「自动同步」开关控制是否参与定时任务 |

实现上没有任何额外存储：自定义源就是 `sources` 表里一行 `origin='custom'` 的记录，
`refresh_sources()` 遇到它只更新 `status`，不改名字也不删行。

### 内网源站与 HTTP_PROXY

如果服务器设了全局 `HTTP_PROXY`（群晖上不少镜像/脚本会设），httpx 默认会把它也用在
`127.0.0.1` 和内网地址上，表现为「测试连接」报 `ConnectError … 仍失败`。加一个环境变量：

```yaml
environment:
  NO_PROXY: "127.0.0.1,localhost,192.168.0.0/16,10.0.0.0/8"
```

### 命令行等价操作

```bash
# 测试连通性（不落库、不建表）
curl -s -X POST http://<host>:8181/api/v1/sources/probe \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"api_url":"https://api.example.com/api.php/provide/vod/"}'

# 添加（sync_now=true 则添加后立即全量同步）
curl -s -X POST http://<host>:8181/api/v1/sources/custom \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"name":"我的私有源","api_url":"https://api.example.com/api.php/provide/vod/","slug":"myhd","sync_now":true}'

# 删除（必须带 confirm）
curl -s -X DELETE "http://<host>:8181/api/v1/sources/myhd/custom?confirm=myhd" \
  -H "Authorization: Bearer $TOKEN"
```

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
| `BACKOFF_BASE_MINUTES` | 60 | 源站大面积失败后的退避基数（60min → 2h → 4h → …） |
| `BACKOFF_MAX_HOURS` | 24 | 退避封顶时长 |
| `FAIL_STATUS_DOWN_STREAK` | 2 | 连续失败达到该次数后面板标「离线」 |
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
| `TOTAL_APPROXIMATE_ENABLED` | true | 无筛选列表的 total 用 `SUM(sources.total_videos)` 估值（响应带 `total_approximate`） |
| `PLAY_LINE_POLICY` | filter | 播放线路策略 `off` / `filter` / `whitelist`（源站可单独覆盖，面板下拉） |
| `PLAY_LINE_DROP_PATTERNS` | `/share/,/play/,.html,.htm` | filter 模式判为「非播放地址」的路径特征（`re:` 前缀按正则） |
| `PLAY_LINE_KEEP_EXT` | m3u8,mp4,flv,mkv | whitelist 模式保留的扩展名 |
| `PLAY_LINE_PREFER_DIRECT` | true | 直链优先排序（同类型内保持上游顺序） |
| `PLAY_LINE_NAME_WITH_FROM` | true | 线路名追加 provider：「线路2 (ffm3u8)」 |
| `PLAY_LINE_MIN_KEEP` | 1 | 安全阀：过滤后低于该条数则整部回退未过滤结果 |
| `PLAY_LINE_PROBE_HTML` | false | 对 unknown 地址做 HEAD 探测（读路径发请求，默认关；超时 `PLAY_LINE_PROBE_TIMEOUT`=1.5s） |
| `PLAY_LINE_POLICY_CACHE_SECONDS` | 30 | 源站级策略缓存（面板改下拉立即失效，此项只兜底多进程/直接改库） |
| `CATEGORIES_CACHE_SECONDS` | 300 | 分类清单缓存秒数（0 = 不缓存） |
| `PG_TRGM_ENABLED` | true | 启动后台建 `pg_trgm` GIN 搜索索引（不锁表；无权限只记日志） |
| `RELATED_ALGO_VERSION` | 2 | 算法版本，改动即清空旧结果并重算（回滚到 1.9.x 请设回 `1`） |
| `RELATED_REQUIRE_CONTENT_SIGNAL` | true | 只有标题/演职员命中才算相似；关掉则退回纯权重打分（不推荐） |
| `RELATED_CANDIDATE_CAP` | 140 | 倒排候选上限（越大越慢；1 万行实测 140 是性价比拐点） |
| `RELATED_RECOMPUTE_CONCURRENCY` | 2 | 全局同时整站重算的源站数（内存紧设 1） |
| `RELATED_FALLBACK_LIMIT` | 6 | 零命中时的兜底条数 |
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

## 镜像版本与回滚

**每次构建的镜像都带版本号**，不再是「只有一个 latest」—— 回滚就是把 compose 里的 tag 改回旧版本。

### tag 语义

| tag | 示例 | 是否可变 | 用途 |
|---|---|---|---|
| `latest` | `…/vodsync:latest` | 可变 | 生产默认跟随最新（只在 main 分支构建时更新） |
| 版本号 | `…/vodsync:1.11.0` | 可变 | 同一版本多次构建时指向最新一次 |
| 次版本号 | `…/vodsync:1.11` | 可变 | 小版本内快速定位 |
| 版本 + 提交 | `…/vodsync:1.11.0-f0f40c8` | **不可变** | **回滚用这个** —— 一个提交一个镜像，永不被覆盖 |
| 提交短 SHA | `…/vodsync:sha-f0f40c8` | **不可变** | 按提交回滚 / 溯源 |

版本号唯一来源是 `app/__init__.py` 里的 `__version__`：CI 从这里读出来生成 tag，
`/docs` 与 `/healthz` 也读同一个值。推 `vX.Y.Z` git tag 时 CI 会校验 tag 与代码里的版本号一致，
不一致直接构建失败（防止「镜像说 1.11.0、代码其实是 1.10.1」这种回滚事故）。
每次构建的日志开头会打印「构建版本：…」，Actions 页面即可查到某一版打了哪些 tag。

### 升级（跟随最新）

```bash
cd /volume2/docker/vodsync
sudo docker compose pull vodsync && sudo docker compose up -d
```

### 回滚到指定版本

> 下例以「回到 v1.11.0 这次构建」为样本，把 tag 换成你要回到的那一版即可。

```bash
# 1) 看有哪些版本：GitHub → 仓库右侧 Packages → vodsync（或 Actions 里某次构建的 tag 列表）
#    也可以直接试拉，404 就是没有这个版本
sudo docker pull ghcr.io/consoleaiden/vodsync:1.11.0-f0f40c8

# 2) 改 compose 里的镜像 tag（推荐用不可变的「版本 + 提交」tag）
#    image: ghcr.io/consoleaiden/vodsync:1.11.0-f0f40c8
sudo docker compose up -d

# 3) 核对真的回滚成功了（version/build 对不上说明 tag 或缓存不对）
curl -s http://127.0.0.1:8181/healthz
# {"status":"ok","db":true,"version":"1.11.0","build":"f0f40c85bf9567f29bad7afb6fedf5e0dc901c97",...}
```

要点：

- **回滚只回滚代码，数据库不会跟着回退**。新版本做过的迁移（补列、加表）在旧版本上依然存在，
  旧版本忽略它们即可 —— 本项目迁移均为「加列/加表」，不删不改，因此前后兼容。
  唯一的例外是算法类升级（如相关推荐算法版本）：旧镜像启动后会把 `algo_version` 视为不匹配并重算，
  属预期行为。
- **生产环境建议把 `latest` 钉成具体版本**（`1.11.0-f0f40c8`），否则下次 `docker compose pull`
  又会回到最新；想「只升指定版本」就永远别用 `latest`。
- `docker compose pull` 发现有同名 tag 被覆盖时才会更新，所以**用不可变 tag 时拉取是幂等的**。
- `build` 字段取自镜像构建时的提交 SHA（本地 `build: .` 时为 `unknown`），是区分「同名版本不同构建」的唯一凭据。

## 群晖部署

详见 [docs/deploy-synology.md](docs/deploy-synology.md)。

**客户端 App 对接**：完整的接口文档（含参数表、响应示例、App 实现流程、常见坑）见 **[docs/CLIENT_API.md](docs/CLIENT_API.md)**；给已有 App **加 API Token** 的施工单见 **[docs/APP_TOKEN_UPGRADE.md](docs/APP_TOKEN_UPGRADE.md)**（整篇丢给 AI 即可）；机器可读规范直接导入 `http://<host>:8181/openapi.json`（宿主机端口按 `docker-compose.yml` 映射，默认 8181→容器 8000）。

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
    ├── README.md                     # 文档索引（每份文档的用途 / 时效 / 读者）
    ├── API_SPEC_FOR_AI.md            # ★ 交付给 AI 的完整 API 文档（字段字典/状态语义/代码骨架/排查表）
    ├── CLIENT_API.md                 # 简明对接说明（人类速查）
    ├── openapi.json                  # OpenAPI 3.1 快照（可导入代码生成器）
    ├── deploy-synology.md            # 群晖部署
    ├── APP_TOKEN_UPGRADE.md          # 给已有 App 加 API Token 的施工单（交给 AI 执行）
    ├── UPGRADE_2026-11-07_v1.10.x.md # v1.9.3 → v1.10.x 升级报告（前端对接清单）
    ├── UPGRADE_2026-11-07_v1.11.0.md # v1.10.x → v1.11.0 升级报告（播放线路过滤）
    ├── REVIEW_2026-11-07.md          # 代码审查报告（v1.9.3 基线，问题已全部落地）
    ├── REVIEW_2026-11-03.md          # 复盘报告（v1.5.2 基线，已在 v1.6.0 修复）
    └── RESPONSE_ENCRYPTION_PLAN.md   # 响应加密/请求签名方案（规划稿，未实施）
```

## 变更记录

> 按版本号**从新到旧**排列，最新版本在最前。历史版本只做归档记录，细节以对应版本的代码与文档为准。

### v1.12.0
管理面板「数据表浏览」：只读查看数据库内容，排障时不用再登 psql。

- **面板新区块**：下拉切换数据表（内置 5 张系统表 + 全部 `vod_{slug}`）；
  字段下拉自动取自所选表的列（选「全部字段」则所有列参与匹配）；
  关键词部分匹配、大小写不敏感；翻页 + 每页 10/20/50/100 条 + 页码直跳（越界自动夹回）；
  长文本单元格（简介/播放地址）点击展开
- **两个只读接口**：`GET /api/v1/admin/tables`（表清单 + 列元信息 + 行数）、
  `GET /api/v1/admin/tables/{name}/rows`（分页/搜索）。结果恒按主键排序，翻页不重不漏
- **安全边界**：表名/字段名只接受服务端元数据白名单（坏表名 404、坏字段 400），
  关键词走 bind param，LIKE 通配符已转义；`settings` 表（密码哈希/会话密钥）有意不可浏览
- **行数口径**：`vod_*` 大表用 `sources.total_videos` 缓存值（「条数校准」后更新），
  不做实时 count；系统表实时统计
- 无新增环境变量、无数据库变更、客户端 App 无需改动
- 测试：`scripts/test_v1120_tablebrowse.py`（32 项：清单/分页/搜索/转义/注入拒绝/参数校验）

**升级步骤（v1.11.0 → v1.12.0）**：`docker compose pull vodsync && docker compose up -d`。
`/healthz` 应返回 `"version":"1.12.0"`。

### v1.11.0
播放线路识别 / 过滤 / 排序（需求源自线上：非凡资源站 100% 记录的线路 1 是 H5 分享页，
客户端自动选源必然播放失败）。

- **入库保留线路来源**：`vod_*` 表新增 `play_from`（上游 `vod_play_from` 原样保存，
  `$$$` 分隔与 `play_url` 对齐）；存量 657 万行留空不影响功能（按 URL 形态判断），
  需要时用 `POST /api/v1/sync/{slug}?mode=backfill_play_from` 只回填这一列
- **线路类型识别**：`classify_play_line()` 纯函数按 URL path 判 `m3u8`/`mp4`/`flv`/`mkv`/
  `page`/`unknown`（`.m3u8` 结尾优先于路径黑名单 —— 避免 `/play/xxx/index.m3u8` 被误杀）
- **读时过滤 + 排序**：`parse_play_url()` 保持旧签名，新增 `from`/`type`/`playable`/
  `original_index`；默认剔除分享页线路、直链优先排序，**安全阀**保证绝不过滤成「无线路」
- **源站级覆盖**：`sources.play_line_policy`（inherit/off/filter/whitelist）+ 面板下拉，
  只有需要的站开 `filter`，其它站不受影响
- **接口**：3 个详情类接口支持 `?play_raw=1`（原始列表）；真过滤时顶层带
  `play_line_filtered: true`；`/api/v1/stats` 新增 `play_line` 段（读侧累计）
- `vod_play_url` **永不改写**，老客户端零改动即受益（线路从 2 条变 1 条且可播）
- 测试：`scripts/test_v1110_playline.py`（77 项，覆盖需求第 5 节全部验收标准）

完整对接说明（前端要改什么、怎么排障、回填怎么做）见
[`docs/UPGRADE_2026-11-07_v1.11.0.md`](docs/UPGRADE_2026-11-07_v1.11.0.md)。

**升级步骤（v1.10.x → v1.11.0）**：`docker compose pull vodsync && docker compose up -d`。
补列在启动时自动完成，无需手工 SQL、无需重新同步；`/healthz` 应返回 `"version":"1.11.0"`。

### v1.10.1
详情接口透出演职员与明星标签：NAS 模式下「演员/标签」不再无数据。

- **`_detail_item` 新增 5 个字段**（纯新增，旧客户端无影响）：`vod_actor`、`vod_director`、
  `vod_area`、`vod_year`、`vod_tag`。这些列 v1.9.0 起就在库里，只是一直没返回 ——
  NAS 模式下客户端只能拿 `star/related` 的 `stars` 兜底填演员，标签则完全没有数据源、整行隐藏
- **取值走 `_pick`（容错）**：`_detail_item` 有 3 个调用点，其中「多源归并列表」的行是
  multiquery 组装的 dict，没有这些列 —— 直接 `row["vod_actor"]` 会 KeyError 打挂列表接口。
  现在缺列/为 NULL 统一返回 `""`，字段永远存在且恒为字符串
- **三条路径对齐**：`/videos/{slug}/{id}`、`?ids=` 批量详情、`?ac=detail` 列表都带这 5 个字段
  （`ac=detail` 与 `content/play_url` 同一条 SQL 取回，不增加往返；深翻页的 UNION 路径同样带上）；
  普通列表模式**不带**（体积敏感）
- 测试：新增 `scripts/test_v1101_detail.py`（26 项：3 条路径 / 缺列容错 / NULL / 回归）；
  其余 14 个套件全绿（13/19/17/54/14/42/42/66/36/58/31/39/15/62）
- 版本号 1.10.0 → 1.10.1

**升级步骤（v1.10.0 → v1.10.1）**：`docker compose pull vodsync && docker compose up -d`。
无数据库变更、无新增配置项、不触发重算。升级后 `curl http://<IP>:8181/healthz` 应看到
`"version":"1.10.1"`。

### v1.10.0
按 `docs/REVIEW_2026-11-07.md` 第 8 节把 W1~W4 四个批次一次落地：4 个数据级 Bug、
6 项算法修正、13 项性能优化、7 项次要 Bug。**接口只加字段、不改结构**，
完整对接说明见 [`docs/UPGRADE_2026-11-07_v1.10.x.md`](docs/UPGRADE_2026-11-07_v1.10.x.md)。

- **数据级修复**
  - B1 批量加词条改 `ON CONFLICT DO NOTHING`：PG 上"含重复名字 → 整批 500、一个字没进"修掉
  - B2 `reset_algo_version` 补事务：旧实现 DELETE 不 commit（日志说清空了、实际没删），改算法版本完全不生效
  - B3 缺演员行重建时按当前词库重算 tag（不命中则清空）：词库删人后旧 `vod_tag` 不再永久残留
  - B4 源站响应编码兜底（utf-8-sig → charset → gb18030 → big5）：GBK 站点不再把片名写成 `����` 入库
- **算法（`RELATED_ALGO_VERSION` 1→2）**
  - 证据先验：缺失字段按缺失权重注入中性分，修掉"稀疏数据得分通胀"
    （旧版「仅同分类+同地区+同年代」得 0.714，高于「同导演同演员」的 0.368 —— 推荐位退化成同分类列表）
  - 内容信号门控：只有 标题/演员/导演/编剧 命中才算相似；权重改为 title 0.35 / class 0.15
  - 演员集合余弦相似度、标题相似度分母改 `max(len)`、明星否定词（访谈/花絮/盘点…）
  - 兜底从 12 条降到 6 条，且不再与真命中混排
- **性能**
  - `RELATED_CANDIDATE_CAP` 600→140、`RELATED_BRUTE_FORCE_MAX` 3000→400：1 万行整站重算 **8.3s（0.83 ms/条），较旧默认快 3.9×**
  - `_load_feats` 改 keyset 分页（内存恒定 27MB/万行）；全局重算闸门 + 增量重算窗口合并（60s）
  - 52 处固定开销：`list_tables` 一次取全、`COUNT(*) FILTER` 合并计数、分类清单缓存、启动只校准空条数
  - `pg_trgm` GIN 索引（后台 `CREATE INDEX CONCURRENTLY`，不锁表）：`name LIKE '%词%'` 走索引
  - 无筛选列表 `total` 改用 `SUM(sources.total_videos)` 估值（响应带 `total_approximate`），夜间校准精确值
- **接口（纯新增 + 两处语义修正）**
  - `/api/v1/related` 新增 `algo_version`、卡片 `reason`（命中原因）
  - `/api/v1/videos` 新增 `total_approximate`；`/api/v1/categories` 新增 `cached`
  - `/api/v1/star/related` 的 `truncated` 改精确判定（不再恒 true）
  - `/api/v1/star-terms` 的 `active` 改为进程内生效数（不再每次重编译词库）
- **测试**：新增 `scripts/test_v194_integrity.py`（62 项）；`test_v190_related` 按新契约调整为 58 项；
  全量回归 13/19/17/54/14/42/42/66/36/58/31/39/15/62 全绿
- 版本号 1.9.3 → 1.10.0

**升级步骤（v1.9.3 → v1.10.0）**：`docker compose pull vodsync && docker compose up -d`。
无数据库结构变更，但**会清空并重算全部推荐结果**（算法版本 1→2）：启动日志出现
`算法版本已变更（当前 2），清空 N 条旧推荐结果`，随后按源站串行补齐（单站 20 万行约 3~7 分钟，
52 站预计数小时）。期间 `/api/v1/related` 走实时兜底（`fallback=true`，仍非空），功能不降级。
`pg_trgm` 扩展若建不出来只会记 warning，搜索退化为顺序扫描。回滚见「镜像版本与回滚」。

### v1.9.3
镜像版本化 + 版本探针：每次构建都打版本号 tag，回滚有据可依。

- **CI 改为「每构建必带版本号」**：`app/__init__.py` 的 `__version__` 是唯一来源，
  每次构建同时推 `1.9.3`、`1.9`、`1.9.3-<sha7>`（不可变）、`sha-<sha7>`（不可变），
  main 分支额外更新 `latest`；推 `vX.Y.Z` tag 时校验与代码版本号一致，不一致直接失败
- **镜像内写入构建信息**：`org.opencontainers.image.version / revision` label +
  `VODSYNC_BUILD_SHA` 环境变量（`docker inspect` 可见）
- **`GET /healthz` 新增 `version` / `build`**：升级或回滚后一条 curl 就能确认容器里跑的是哪个版本、
  哪个提交构建的（`build` 为完整提交 SHA，源码直跑时为 `dev`）
- **版本号集中**：`app/main.py` 的 FastAPI `version` 改为引用 `app/__init__.py`，不再两处写死
- 文档：README 新增「镜像版本与回滚」章节（tag 语义表 / 回滚命令 / 数据库不回退的说明）；
  `docker-compose.yml` 补 tag 说明与回滚示例
- 测试：v1.9.2(39)、v1.9.1(31)、v1.9.0(56) 回归零失败
- 版本号 1.9.2 → 1.9.3

**升级步骤（v1.9.2 → v1.9.3）**：`docker compose pull vodsync && docker compose up -d`。
无数据库变更、无新增配置项。升级后 `curl http://<IP>:8181/healthz` 应看到
`"version":"1.9.3"`；从这版起每次构建都有不可变 tag，回滚按上文「镜像版本与回滚」操作。

### v1.9.2
明星作品接口补强：新增「按明星查作品」、`star/related` 增加 Tab 角标数据、
词库支持别名、未入词库高频词统计。

- **新接口 `GET /api/v1/star/works`**（明星为中心）：`slug` + `term`（主名或别名）
  + `limit`（默认 30/上限 100）+ `exclude_id`；按 `vod_time desc, vod_id desc` 排序，
  `total` 为该明星现存作品总数（不受 limit 影响）；`term` 无映射/已下架 →
  200 + `total:0` + `list:[]`（不 404）。解决「目标视频命中多明星时，作品少的明星
  被 overlap 全局截断挤出」的问题
- **`/api/v1/star/related` 增强（纯新增，向后兼容）**：
  - `star_counts`：`{明星名: 本源站作品数}`（降序）—— 客户端画明星 Tab 角标
  - `truncated`：候选总数超 `limit` 被截断时为 true
  - `stars` 仍为 `string[]`，客户端解析逻辑不用改
- **name 匹配健壮性**：`resolve_target` 依次尝试 精确 → LIKE（**通配符已转义**）→
  去空白 → 全半角归一 → 库里名字去空格再比；确实找不到仍返回 404
- **词库别名 `aliases`**（v1.9.2）：面板词库行新增「别名」按钮（逗号分隔，可多个），
  别名参与标题匹配与演员交集，但命中后**统一归一到主名**（`star_map.term` /
  `vod_tag` 只存主名）—— 同一演员多译名/写法不再把作品拆散。别名变更后点
  「重算推荐」生效
- **未入词库高频词统计 `GET /api/v1/star/tags/untracked`**：抽样缺演员视频标题 →
  屏蔽已命中词库片段 → 剩余文本 2~4 元 n-gram 频次统计 → 过滤停用词/低频词、
  长词优先，返回 Top N 候选供扩词库（启发式候选，需人工确认）
- 测试：`scripts/test_v192_star_works.py` 39 项全绿；v1.9.0(56)/v1.9.1(31) 与
  历史 10 个脚本回归零失败
- 版本号 1.9.1 → 1.9.2

**升级步骤（v1.9.1 → v1.9.2）**：`docker compose pull vodsync && docker compose up -d`。
`star_terms.aliases` 列自动补；不需要重建数据 —— 别名是可选增强，填了之后点一次
「重算推荐」即可把历史别名命中并入主名。新配置见 `.env.example`「明星 Tag」段
（`STAR_WORKS_LIMIT_*` / `STAR_UNTRACKED_*`），均有默认值。

### v1.9.1
明星作品推荐：明星 Tag 回填 + 同明星视频查询 API。基于 v1.9.0 的明星词库，
把词库价值落到数据层（`vod_tag`）和查询层（`star_map` 倒排映射）。

- **Tag 回填（词库 → vod_tag）**：对没有演员信息的视频，用标题匹配明星词库，
  命中的明星名写入 `vod_tag`（逗号分隔，可多个，上限 `STAR_TAG_MAX_PER_VIDEO`）；
  真实演员行把演员名（繁体/全角归一化后）与词库求交集也进映射，`vod_tag` 保留源站原值
- **高性能方案**：
  - 词库编译成**单个正则**（按长度降序排列，「吴京安」优先于「吴京」），一个标题
    一次扫描出全部命中，O(标题长) 且与词库大小解耦（万级词库扫描 µs 级/标题；
    词库到十万级可换 Aho-Corasick，当前无需）
  - keyset 分页流式扫描（`vod_id > last`，不做 OFFSET 深分页），内存恒定
  - 真实演员行不跑正则，只做集合交集
  - 批量写库（executemany UPDATE + 批量 INSERT），每批 2000 行
  - API 查询走 `star_map` 的 `(slug, term)` 索引，绝不对大表 LIKE 全表扫
- **新表 `star_map`**（slug, vod_id, term, src）：明星-视频倒排映射；
  `src=actor`（真实演员 ∩ 词库，高可信）/ `src=title`（标题推断，中等可信）
- **UPSERT 保护**：增量同步时源站的空 `vod_tag` 不覆盖本地推断值
  （`COALESCE(NULLIF(excluded,''), 本地)`），源站真给了非空 tag 才更新
- **新 API**：
  - `GET /api/v1/star/related?slug=<源站>&vod_id=<id>|name=<名称>&limit=20`
    返回与目标视频共享明星的其他视频（卡片字段 + `match_stars` 命中的明星 +
    `overlap` 重叠数；重叠多者靠前，同分按更新时间降序）
  - `POST /api/v1/maintenance/star-tags?slug=` 手动重建（后台串行）
  - `GET /api/v1/star/tags/status` 覆盖统计（视频数/缺演员数/已映射数）
  - `POST /api/v1/star-terms/recompute` 扩展：词库变更后先重建 Tag 映射再重算推荐
- **自动触发**：全量同步后整站重建；增量同步只重算本轮 touched 的视频
  （改名后不再命中的行旧映射会被清除）；每晚 03:40 小站优先重建
  `STAR_TAG_NIGHTLY_BUDGET` 个站（字段回填补上的演员会进入映射）
- 测试：`scripts/test_v191_star_tags.py` 31 项全绿（词库正则/回填/增量/UPSERT
  保护/API 端到端/排序）；历史 12 个脚本回归零失败
- 版本号 1.9.0 → 1.9.1

**升级步骤（v1.9.0 → v1.9.1）**：`docker compose pull vodsync && docker compose up -d`
即可 —— `star_map` 表自动创建；已录入词库的话启动后首次重建在夜间任务（或手动
`POST /api/v1/maintenance/star-tags?slug=all` 立即执行）；新增配置见 `.env.example`
的「明星 Tag 回填」段，全部有默认值。

### v1.9.0
相关推荐（服务端预计算）+ 存量字段补齐 + 明星词库。客户端详情页新增「相关推荐」栏，
接口直读预计算表（P95 < 50ms，实测 0.3ms），任何详情页的推荐栏都不为空。

**关键背景**：升级前 `vod_{slug}` 表只存 9 个字段，演员/导演/编剧/地区/年代在抓取
阶段就被丢弃了 —— 本版从抓取链路到算法到存储全链路打通，并给出存量数据补齐方案。

- **抓取链路扩容**：`extract_row` 新增 vod_actor/vod_director/vod_writer/vod_area/
  vod_year/vod_tag，占位值（内详/未知/暂无…，词表可配）在入库前清洗为空串；
  存量 vod 表启动时自动补列（PG 快速 ADD COLUMN）
- **预计算引擎 `app/related.py`**：
  - 权重重分配：字段缺失（含占位值）时权重按比例摊给其余项——数据越稀疏，
    标题占比自动越高，正好匹配数据现状
  - 标题相似度 = 0.6×LCS公共子串占比 + 0.4×bigram Jaccard；双阈值门控
    （LCS≥3字 或 Jaccard≥0.45）防两字短标题误伤；归一化含全角/繁转简（opencc 可选）/
    噪声后缀剥离（正则表可配）；序号值（季1/季2/终章）摘出降权
  - 倒排索引剪枝 + 稀有 token 优先候选 + 打分提前放弃：1 万视频内存峰值 31MB
    （验收 <256MB），实测约 90s/万条（后台任务）
  - 同片去重：同 (归一化标题, 年代, 序号) 只留最新一条进候选池
  - 翻拍惩罚：年份差 >6 年标题分 ×0.6
  - 三级兜底链：同分类最近更新 → 同地区/年代 → 全站最新，零命中也有内容
- **存储**：新表 `vod_related`（slug+vod_id 主键，related_json 只存 id+score 精简引用，
  卡片字段读取时实时补齐——封面永远最新，下架条目自然消失，算法升级只需改小 JSON）
- **API**：`GET /api/v1/related?slug=&id=&limit=`（未知 slug/视频返回 404 标准错误）；
  详情接口附带 `related` 数组（`RELATED_IN_DETAIL` 可关）；`/api/v1/related/status`
  覆盖率；`POST /api/v1/maintenance/related-recompute` 手动重算
- **重算触发**：增量同步结束只重算本轮 touched 的视频（`RELATED_TOUCHED_CAP`）；
  全量同步整站重算；每日 04:30 校准（`RELATED_CALIBRATION_*`）；`RELATED_ALGO_VERSION`
  变更时启动清空重算；启动时对覆盖率不满的源站自动补齐（存量推荐当天即可用——
  标题是唯一全量信号）
- **存量字段回填**：升级前已同步的库演员/地区/年代列是空的。每晚凌晨 2 点对缺字段
  源站按小站优先重跑全量（UPSERT 幂等只补字段），每晚 N 个站（`FIELD_BACKFILL_*`），
  几个晚上自然补完；也可以手动 `POST /api/v1/sync/{slug}?mode=full`
- **明星词库（面板设置页维护）**：可批量粘贴主演名单（自动识别 JSON 数组、带引号
  片段 `"名字",`、逗号/顿号/竖线/换行分隔），支持增删改/启停。推断规则：
  标题命中且字段稀疏（area/year/type 非空 ≤`RELATED_STAR_STRONG_MAX_FIELDS`）→
  **强信号不打折**（用户规则：相关性非常高）；仅简介命中 → 弱信号乘
  `RELATED_STAR_SIM_FACTOR`（0.7）。词库变更后点「重算推荐」或等每日校准生效
- **可配置性**：权重表、全部阈值、占位词表、噪声正则、Top N、兜底/候选/校准/回填
  参数全部环境变量化（`RELATED_*` / `FIELD_BACKFILL_*`），改配置即调优不改代码
- 测试：`scripts/test_v190_related.py` 56 项（含 4 类验收样本 + API 端到端 + 明星词库），
  性能基准 `scripts/bench_v190_related.py`
- 版本号 1.8.1 → 1.9.0

**升级步骤（v1.8.x → v1.9.0）**：

1. 拉取新镜像并重启：
   ```bash
   cd /volume2/docker/vodsync
   sudo docker compose pull vodsync && sudo docker compose up -d
   ```
2. 启动时自动完成迁移，无需手动改表：存量 `vod_*` 表自动补 6 个新列（PG `ADD COLUMN`），自动建 `vod_related` / `star_terms` 表
3. 观察两条日志确认存量补齐在推进：
   - 启动后数分钟内：`相关推荐: 启动补齐 N 个源站`（存量推荐当天可用，标题是全量信号）
   - 每晚凌晨 2 点起：`[字段回填]`（每晚默认补 2 个站的演员/地区/年代，几个晚上补完；想加快改 `FIELD_BACKFILL_SOURCES_PER_NIGHT`）
4. （可选）想立刻补某个站的字段：`POST /api/v1/sync/{slug}?mode=full`
5. （可选）到面板 **⚙ 设置 → 明星词库** 批量粘贴主演名单增强缺失字段的匹配；改完点「重算推荐」生效
6. 客户端接入：`GET /api/v1/related?slug=<源站>&id=<vod_id>`，或直接读详情响应里新增的 `related` 数组（无需改请求）

**新增配置**：见 `.env.example` 的「相关推荐（v1.9.0）」与「存量字段回填」两段，全部有默认值，不配也能正常跑。

### v1.8.1
源站健康度退避：源站持续限流/宕机时不再「每 6 小时从第 1 页捶到最后一页」。

背景：某 9.3 万条的源站出现单轮 2,678 个索引页失败的日志。旧逻辑水位线冻结是对的
（不丢数据），但下一轮照样全量重扫——源站还在宕机时就是每轮 2,700 次空转请求，
既烧流量又持续触发对方风控，面板上也看不出哪个站在装死。

- **M1 页级重试**：增量索引阶段的失败页在本次运行内重试（复用 `PAGE_MAX_ATTEMPTS`
  轮数 + `PAGE_RETRY_SLEEP_SECONDS` 间隔），零进展提前退出。瞬时抖动当场恢复，
  不必等下一轮重扫全索引
- **M2 早停**：连续 2 个批次整批失败（≥8 次请求含内部重试全部失败）判定源站当前
  不可达，剩余页直接跳过。2,700 页的死站现在只花约 20 次请求就确认结论
- **M3 指数退避**：`sources` 新增 `fail_streak` / `skip_until`。大面积失败
  （页失败率 ≥50% 或整轮异常）→ streak+1，调度器跳过 `skip_until` 未到期的站，
  退避 60min → 2h → 4h → … 封顶 24h（`BACKOFF_*` 可配）；到期自然恢复参与
  （第一页就是探针，早停保证探针成本极低）；成功一次立即清零
- **M4 状态联动**：连续失败达 `FAIL_STATUS_DOWN_STREAK`（默认 2）次后状态标
  `down`（面板「离线」徽标），退避中的站显示「退避 Xh」徽标；恢复自动转回
- **M5 错误聚合**：日志行从「索引页 2678 个失败」升级为
  「索引页 2678 个失败（timeout×1900, http_403×778），水位线保持不变」，
  面板上直接看到原因分布，不必钻 docker logs
- **安全边界不变**：水位线「失败冻结 / 按实际写库推进」的语义一字未动；
  手动点「增量/全量」不受退避限制
- 测试：新增 `scripts/test_v181_backoff.py`（36 项：迁移、页级重试、早停请求计数、
  streak 进退、sync_all 跳过与到期恢复、错误聚合、水位线冻结回归），全仓 303 项通过
- 版本号 1.8.0 → 1.8.1

### v1.8.0
自定义源站：latest.json 之外的接口可以在面板直接登记。

- **数据模型**：`sources` 表新增 `origin`（`latest` / `custom`），`_migrate()` 自动为老库补列。
  自定义源就是一行 `origin='custom'` 的记录，不依赖任何额外配置文件或内存状态
- **持久化语义**：`refresh_sources()` 遇到 `origin='custom'` 只更新 `status`，不改名称/描述，
  也永不删除（refresh 本来就只增不减）。所以「持续存在」是结构保证，不是约定
- **新接口**：`POST /api/v1/sources/custom`（添加）、`PATCH|DELETE /api/v1/sources/{slug}/custom`
  （修改/删除）、`POST /api/v1/sources/probe`（连通性测试，不落库不建表）
- **身份与标识**：按 `api_url` 判重（含尾斜杠差异）；`slug` 留空自动生成、冲突自动加 `_2`；
  显式指定的 `slug` 冲突直接 409。`slug` 决定表名与 API 路径，登记后不可修改
- **改地址清数据**：`PATCH api_url` 会清空该站已入库数据并重置水位线/断点 —— 换接口就是换数据源，
  保留旧行只会让列表混进「新源已经没有」的片子，且 `vod_id` 冲突时会被覆盖成新源内容
- **删除保护**：必须 `confirm=<slug>`；只能删自定义源（官方源删了下次 refresh 也回来）；
  正在同步时返回 409；`drop_table=false` 可只删登记、保留数据
- **SSRF 收口**：`api_url` 只放行 `http://` / `https://`，挡掉 `file://`/`gopher://` 这类能读本地文件的 scheme
- **顺带修的两个 bug**：
  - `MacCmsClient._request` 的 `range(1, retries+1)` 在 `retries=0` 时循环体一次都不执行，
    直接抛「重试 0 次仍失败 (None)」——一个请求都没发。连通性探测正好传了 0，
    本来想「只试一次」，实际变成了「绝不请求」。改为 `max(1, retries)`
  - `forget_vod_table()` 只清 `_vod_table_cache` 不清 `META.tables`，导致「删掉自定义源
    再用同一个 slug 重建」会抛 `InvalidRequestError: Table ... is already defined`
- **面板**：源站列表右上「+ 添加源站」，弹窗内可测试连接、可选立即全量同步；
  表格新增「来源」列（官方/自定义），自定义行提供编辑与删除入口
- 测试：新增 `scripts/test_v180_custom_source.py`（66 项：地址校验、探测成功/失败/强制添加、
  去重含尾斜杠、slug 生成与冲突、refresh 不覆盖不删除、改地址清数据、删除权限与表 DROP、
  同 slug 重建，以及一段真实 HTTP 的端到端链路），全仓 267 项通过
- 版本号 1.7.0 → 1.8.0

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

### v1.5.2
- 分页：`API_PAGE_SIZE_DEFAULT` 默认由 20 调到 **30**（三列宫格 10 行 × 3 列，每页正好整行）
- 修复：`/api/v1/search` 的 `page_size` 之前写死 `20 / 100`，不跟随配置；现在与列表接口一致读 `API_PAGE_SIZE_DEFAULT` / `API_PAGE_SIZE_MAX`
- 新增 `scripts/test_v151_paging.py`（14 项：默认值、显式值、最后一页不足整页、越界 422、search 与 MacCMS 兼容接口一致性）
- 文档：`docs/APP_TOKEN_UPGRADE.md` 新增「9. 分页约定：三列宫格每页 30 条」（含取数策略与三个常见坑）
- 版本号 1.5.1 → 1.5.2

### v1.5.1
- 修复：`/source`（源站清单 JSON）不带 `/api/` 前缀，此前只带 Token 的客户端会被拦成
  `401 请先登录管理面板`，现在与其它接口一致认 Token
- 新增 **`docs/APP_TOKEN_UPGRADE.md`** —— 交给 AI 给 App 加 Token 的施工单（含各平台拦截器代码、401 处理策略、自测清单）
- 测试：鉴权用例补 4 项（`/source` 认 Token、双空格 Bearer、无空格 Bearer 拒绝），共 44 项
- 版本号 1.5.0 → 1.5.1

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

### v1.4.2
- 新增 `scripts/migrate_sqlite_to_pg.py`：SQLite → PostgreSQL 数据迁移，**切库不需要重新同步**
  - 迁移 `sources` / `sync_logs` / 全部 `vod_*` 表，保留 `watermark`（增量水位线）与 `last_page`（全量断点），迁完直接续增量
  - 幂等（重复执行按主键跳过）、每批一个事务（中断可续跑）、PG 下自动对齐 SERIAL 序列
  - `--dry-run` 预估规模、`--only-meta` 只搬元数据、`--tables` 指定源站、`--replace` 覆盖、`--verify` 只校验
  - 迁移后自动校验：表齐全性、行数一致、关键字段逐行比对
- 镜像内包含迁移脚本，可直接 `docker run --entrypoint python .../migrate_sqlite_to_pg.py`
- 测试：`scripts/test_migration.py`（42 项：跨批翻页、字段保真、幂等、替换、过滤、dry-run、错误提示）

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

### v1.3.0
- 新增**按源站的自动同步开关**（全局定时调度保留不变：每 6 小时全站增量、每 12 小时刷新源站列表）：
  - `sources` 表新增 `auto_sync` 列（默认 1=参与），启动自动迁移旧库，状态持久化重启不丢
  - 关闭后该源站**不参与**定时增量任务与全站同步（`sync-all` / 定时 / 增量续跑），手动单站同步不受影响
  - 新增 API：`POST /api/v1/sources/{slug}/auto-sync?enabled=`（单站）、`POST /api/v1/sources/auto-sync?enabled=&slugs=`（批量，`slugs=all` 为全部）
  - `/api/v1/sources`、`/api/v1/sources/{slug}` 返回 `auto_sync` 字段
  - 面板源站列表：每行「自动」开关 + 复选框多选（全选框跨刷新保留）+ 「全部开启 / 全部暂停 / 开启所选 / 暂停所选自动同步」批量按钮
  - 测试 `scripts/test_v130_autosync.py`（13 项：默认值/单站/批量/sync_all 过滤/续跑过滤/路由注册）

### v1.2.3
- 项目已发布到 GitHub：https://github.com/consoleaiden/vodsync
- GitHub Actions 首次构建成功（amd64 + arm64 双架构），镜像公开可拉取：`ghcr.io/consoleaiden/vodsync:latest`、`ghcr.io/consoleaiden/vodsync:1.2.2`
- `docker-compose.yml` 与群晖部署文档的镜像地址已替换为实际发布地址

### v1.2.2
- 修复：源站列表的「暂停」按钮在**未同步的源站**上也显示（误把"有断点未跑完"当成"正在跑"）
- 新增运行时状态 `syncer._running`：`sync_source` 进入即登记、结束即注销，`/api/v1/sources` 与详情返回 `running` / `running_mode` / `running_seconds`
- 面板交互改为按运行态渲染：**只有 `running=true` 才显示「暂停」**；已暂停显示「继续」；空闲站不显示这两个按钮
- 进度列区分三态：正在跑（流动进度条 + 已运行时长）/ 有断点但未跑（"断点 N/M 页 · 未在同步"）/ 已完成
- 修正：全新全量同步在取到首页总页数前 `progress` 误算为 100，现为 0；`stats.sources_syncing` 改为"真正在跑的任务数"，另加 `sources_unfinished` 表示有断点的站数
- 单源站暂停接口返回 `was_running`，并区分提示"将在当前批次结束后停止"与"当前未在同步，暂停标记已记录"
- 新增 `scripts/test_panel_render.js`：Node 里用 DOM 桩执行面板真实 JS，逐行断言「暂停」按钮只出现在运行中的行

### v1.2.1
- 新增**单源站级暂停/继续**：源站列表每行增加「暂停 / 继续」按钮，暂停中的站显示橙色「本站暂停」徽标
- 新增 API `POST /api/v1/sources/{slug}/pause` 与 `POST /api/v1/sources/{slug}/resume`；`/api/v1/sources`、`/api/v1/sources/{slug}` 返回 `paused` / `paused_at`
- 判定顺序：全局/分层暂停 → 单源站暂停，任一命中即停；单源站暂停不影响其他源站，`sync-all` 与定时任务自动跳过被暂停的站
- `sources` 表新增 `paused` / `paused_at` 列，启动自动迁移旧库
- 测试 `scripts/test_v12_pause.py` 扩至 23 项（含单源站暂停隔离、运行中单源站暂停断点保存、全局暂停下的阻断提示）

### v1.2.0
- 新增**同步暂停/继续**，三层级独立控制：全局（全量+增量+定时任务一起停）、仅全量、仅增量
- 暂停语义为**检查点式优雅停止**：当前抓取批次完成后保存断点再停，不丢数据；继续时自动从断点续传（增量则水位线不动、重跑增量）
- 暂停状态持久化到 `data/pause_state.json`，服务重启后仍生效；启动时自动恢复
- 新增 API：`GET /api/v1/pause`、`POST /api/v1/pause?scope=`、`POST /api/v1/resume?scope=`（resume 会自动续跑受影响的源站）；`/api/v1/stats` 新增 `pause` 字段；同步日志新增 `paused` 状态
- 管理面板新增「同步暂停控制」区：三个开关 + 页面顶部暂停横幅 + 源站行"已暂停"徽标
- 定时调度在暂停期间自动跳过对应任务（恢复后下一周期自动执行）
- 新增测试 `scripts/test_v12_pause.py`（15 项，含真实网络中途暂停→断点 8 页→继续→16 页的端到端验证）

### v1.1.3
- 面向客户端 App 对接的整理：
  - 新增 `GET /api/v1/categories`：分类清单（type_id/type_name/count，支持多源合并），分类浏览入口
  - `/api/v1/videos` 新增 `type_id` 参数（可与 `ac=detail`、`wd` 组合）
  - 新增 CORS 支持（H5/Web App 可跨域直调，`CORS_ORIGINS` 环境变量可限制来源）
  - 新增 **docs/CLIENT_API.md**：面向 AI 客户端开发的完整 API 文档（约定/鉴权/全部接口/响应示例/App 实现流程/常见坑）

### v1.1.2
- `/api/v1/videos` 增强：新增 `ids=1,2,3`（批量取详情，按请求顺序返回，含播放地址）与 `ac=detail`（列表带播放地址）
- 查询参数归一化：兼容 `&?ac=detail` 这类多打问号的写法（原会被静默忽略）
- 详情字段统一：列表/详情/兼容接口共用 `_detail_item()`，补齐 `vod_play_from`
- `ids` 查不到时返回 404 并提示如何搜索确认 `vod_id`（原来只返回空列表）

### v1.1.1
- 新增 `GET /source`：资源站 API 清单（上游 `api` 地址 + 本地 `local_api`/`local_list_api` + 已入库条数），支持 `?status=ok|down`、`?only_ready=true` 过滤
- 管理面板新增「资源站 API」按钮，一键查看清单
- 断点保存时同步更新 `last_sync_at`，面板与 `/source` 的"最后同步时间"不再为空

### v1.1.0
- 新增 **Web 管理面板**（`/`）：概览统计、全量同步进度条、单站/全站手动触发、条数校准、源站日志、跨源搜索
- 新增 **聚合搜索接口** `GET /api/v1/search`：线程池并行查询各源站表 + 内存归并分页，单源失败隔离，LIKE 通配符转义
- 新增 `GET /api/v1/stats`（面板概览）、`POST /api/v1/maintenance/recount`（条数校准）
- `sources` 表新增 `total_pages` 列，启动时自动迁移旧库（无需手动改表）
- 修复：全量同步中断时 `total_videos` 长期为 0（现在每次断点保存同时刷新条数；启动时也会校准）
- 版本号 1.0.0 → 1.1.0

### v1.0.0
- 首个版本：源站发现、全量断点续传、增量水位线同步、每源站一表、REST + MacCMS 兼容 API、CI 多架构镜像、群晖部署文档
