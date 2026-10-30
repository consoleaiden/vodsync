# VodSync 客户端 API 对接文档

> ⚠️ 本文是**简明对接说明**。完整交付版（含字段字典、状态语义、代码骨架、排查表）见 **[`API_SPEC_FOR_AI.md`](./API_SPEC_FOR_AI.md)** —— 交给 AI 开发客户端时请使用该文档。

> 本文档面向「调用 VodSync 服务的客户端 App」（原生 App / H5 / 小程序 / 播放器 / 采集器均可）。
> 目标：一个 AI 客户端开发者（或 AI 编程助手）拿这份文档即可完成全部对接，无需阅读服务端代码。

---

## 1. 基础约定

| 项 | 说明 |
|---|---|
| Base URL | `http://<服务IP>:8000`（群晖部署示例端口 8181，以实际部署为准） |
| 数据格式 | 全部为 JSON（`Content-Type: application/json`） |
| 请求方法 | 数据查询用 `GET`；管理操作（触发同步等）用 `POST` |
| 编码 | UTF-8。中文参数务必 URL 编码（浏览器/fetch 自动处理；命令行用 `--data-urlencode`） |
| OpenAPI | 服务自带机器可读规范：`GET /openapi.json`（Swagger UI 在 `/docs`），AI 工具可直接据此生成客户端代码 |

### 1.1 鉴权（可选）

服务端设置环境变量 `API_KEY` 后，**所有接口**都需要鉴权，两种传递方式任选：

```
请求头:  X-API-Key: <你的KEY>
或参数:  ?apikey=<你的KEY>      （适合无法自定义请求头的场景，如 <img>、播放器内核）
```

未设置 `API_KEY` 时无需鉴权。鉴权失败返回 `401 {"detail":"无效的 API Key"}`。

> 判断是否启用鉴权：调 `GET /healthz`，返回 200 即当前无需 Key；返回 401 说明已启用。

### 1.2 统一响应结构

**成功**：HTTP 200，业务字段 `code: 1`：

```json
{ "code": 1, "total": 100, "list": [ ... ] }
```

**失败**：HTTP 状态码 + `detail` 文本：

```json
{ "detail": "源站 xxx 不存在" }
```

| HTTP 状态码 | 含义 |
|---|---|
| 200 | 成功 |
| 400 | 参数错误（如 ids 格式、非法 slug、空关键词） |
| 401 | API Key 缺失/错误 |
| 404 | 资源不存在（源站 / 视频 / 尚未同步数据），detail 中带补救建议 |
| 422 | 参数校验失败（FastAPI 标准格式，`detail` 为数组） |
| 500 | 服务端异常 |

### 1.3 通用参数

| 参数 | 适用接口 | 说明 |
|---|---|---|
| `source` | videos / categories / search | 源站标识（slug），**逗号分隔可多源合并**；留空 = 全部源站。slug 从 `/source` 或 `/api/v1/sources` 获取，仅允许 `a-z 0-9 _` |
| `page` / `pg` | 列表类 | 页码，从 1 开始 |
| `page_size` | 列表类 | 每页条数，默认 20，上限见 `/docs`（默认 ≤100） |

### 1.4 时间格式

所有时间字段为 ISO 8601 字符串或 `"YYYY-MM-DD HH:MM:SS"`（来自源站原始数据），如 `"2026-10-30T13:00:30"`。可直接字符串比较排序。

---

## 2. 接口总览

### A. 数据接口（客户端核心使用）

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/source` | 资源站清单（含上游/本地 API 地址，App 初始化用） |
| GET | `/api/v1/categories` | 分类清单（分类浏览入口） |
| GET | `/api/v1/videos` | 视频列表 / 批量详情 / 分类浏览（REST 风格，多源合并） |
| GET | `/api/v1/videos/{slug}/{vod_id}` | 单条视频详情（含播放地址） |
| GET | `/api/v1/search` | 跨源站聚合搜索 |
| GET | `/api/v1/{slug}?ac=list\|detail` | 苹果 CMS(MacCMS) 兼容接口，现有播放器可直接对接 |

### B. 管理接口（管理端 App / 面板功能）

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/healthz` | 健康检查（同时用于探测是否需要 API Key） |
| GET | `/api/v1/stats` | 概览统计（源站数/总条数/同步中/今日入库/最近错误） |
| GET | `/api/v1/sources` | 源站列表 + 同步状态 + **全量同步进度百分比** |
| GET | `/api/v1/sources/{slug}` | 单源站详情 + 最近 10 条同步日志 |
| POST | `/api/v1/sources/refresh` | 立即刷新源站列表（`?full_sync=true` 可选） |
| POST | `/api/v1/sync/{slug}?mode=auto\|full\|incremental` | 触发单源站同步 |
| POST | `/api/v1/sync-all?mode=auto\|full\|incremental` | 触发全站同步 |
| GET | `/api/v1/pause` | 查询同步暂停状态（global/full/incremental 三层级） |
| POST | `/api/v1/pause?scope=global\|full\|incremental` | 暂停同步（优雅停止，保存断点） |
| POST | `/api/v1/resume?scope=global\|full\|incremental` | 继续同步（自动从断点续跑） |
| POST | `/api/v1/sources/{slug}/pause` | 暂停单个源站（独立于全局暂停） |
| POST | `/api/v1/sources/{slug}/resume` | 继续单个源站（按 auto 续跑） |
| POST | `/api/v1/sources/{slug}/auto-sync?enabled=` | 单源站自动同步开关（是否参与定时任务/全站同步） |
| POST | `/api/v1/sources/auto-sync?enabled=&slugs=` | 批量自动同步开关（`slugs=all` 或逗号分隔列表） |
| GET | `/api/v1/logs?limit=50` | 最近同步日志（跨源站） |
| POST | `/api/v1/maintenance/recount` | 条数校准（异步执行） |

> 管理接口全部为**异步受理**模式：POST 立即返回 `{"code":1,"msg":"...已受理"}`，实际进度通过轮询 `/api/v1/sources`（看 `syncing`/`progress` 字段）与 `/api/v1/logs` 获取。

---

## 3. 接口详情

### 3.1 GET `/source` — 资源站清单

App 初始化时调用一次，得到全部可用源站及其对接路径。

**参数**：`status=ok|down`（按上游状态过滤）、`only_ready=true`（仅返回已有本地数据的源站）

**响应**：

```json
{
  "code": 1, "total": 52, "alive": 52, "ready": 1,
  "server_time": "2026-10-30T13:00:00",
  "list": [{
    "name": "JKUN资源站",            // 展示名
    "slug": "jkun",                  // ★ 后续所有接口的 source 参数都用它
    "api": "https://jkunzyapi.com/api.php/provide/vod",  // 上游原始采集地址
    "homepage": "https://jkunzyapi.com",
    "status": "ok",
    "description": "JKUN资源采集站...",
    "video_count": 2640,             // 本服务已入库条数
    "synced": true,                  // 是否已同步过
    "last_sync_at": "2026-10-30T13:00:30",
    "local_api": "/api/v1/jkun",     // 本服务 MacCMS 兼容路径
    "local_list_api": "/api/v1/videos?source=jkun"  // 本服务 REST 路径
  }]
}
```

列表按 `video_count` 倒序（数据多的站在前）。

### 3.2 GET `/api/v1/categories` — 分类清单

**参数**：`source`（可选，多源逗号分隔；留空合并全部源站）

**响应**：

```json
{
  "code": 1, "total": 23,
  "list": [
    { "type_id": 1, "type_name": "电影", "count": 1203, "sources": ["jkun","t1"] },
    { "type_id": 6, "type_name": "连续剧", "count": 890, "sources": ["jkun"] }
  ]
}
```

多源合并时同名分类合并计数；`sources` 数组表示该分类出现在哪些源站。

### 3.3 GET `/api/v1/videos` — 视频列表 / 批量详情（REST 风格核心接口）

四种用法：

```bash
# ① 分页列表（按更新时间倒序，多源合并）
GET /api/v1/videos?source=jkun&page=1&page_size=20
GET /api/v1/videos?source=jkun,t1          # 多源
GET /api/v1/videos                          # 全部源站

# ② 批量详情（含播放地址，按传入顺序返回）
GET /api/v1/videos?source=jkun&ids=134369
GET /api/v1/videos?source=jkun&ids=1,2,3

# ③ 完整列表（每条都带播放地址，适合一次性构建播放页）
GET /api/v1/videos?source=jkun&ac=detail&page=1

# ④ 分类浏览
GET /api/v1/videos?source=jkun&type_id=6&page=1
GET /api/v1/videos?source=jkun&type_name=电影
```

**通用可选参数**：`wd`（名称模糊搜索）、`type_name`、`type_id`、`page`、`page_size`

**列表模式响应**（①④，轻量字段，无播放地址）：

```json
{
  "code": 1, "page": 1, "pagecount": 133, "total": 2640,
  "list": [{
    "source": "jkun",          // ★ 详情请求时回传的 slug
    "vod_id": 134369,          // ★ 详情请求时回传的 id
    "type_id": 6,
    "type_name": "素人搭讪",
    "vod_name": "XXX",
    "vod_pic": "https://.../cover.jpg",
    "vod_remarks": "HD",       // 通常为清晰度/更新至第N集
    "vod_time": "2026-10-29 21:33:12"
  }]
}
```

**详情模式响应**（②③，`list` 内每条额外包含）：

```json
{
  "vod_content": "简介文本...",
  "vod_play_url": "线路1$第01集$https://.../e1.m3u8#第02集$https://.../e2.m3u8",
  "vod_play_from": "vodsync",
  "vod_play_list": [
    {
      "name": "线路1",
      "episodes": [
        { "name": "第01集", "url": "https://.../e1.m3u8" },
        { "name": "第02集", "url": "https://.../e2.m3u8" }
      ]
    }
  ]
}
```

**播放地址使用建议**：优先取 `vod_play_list[*].episodes[*].url`（已结构化）；`vod_play_url` 是源站原始串仅作兜底。地址多为 m3u8 直链，可直接交给播放器内核。

**错误**：
- `ids` 无数字格式错误 → 400
- `ids` 均未命中 → 404（detail 中提示改用搜索接口确认 vod_id）
- 指定源站均无数据 → 404

### 3.4 GET `/api/v1/videos/{slug}/{vod_id}` — 单条详情

```bash
GET /api/v1/videos/jkun/134369
```

响应 `{"code":1, "detail": { ...详情字段，同 3.3 详情模式... }}`。不存在返回 404。

### 3.5 GET `/api/v1/search` — 跨源站聚合搜索

```bash
GET /api/v1/search?wd=三体                        # 全部源站
GET /api/v1/search?wd=三体&source=jkun,t1         # 限定源站（更快）
GET /api/v1/search?wd=三体&type_name=电影&page=1
```

**参数**：`wd` 必填（1-64 字）；`source`、`type_name` 可选；`page`、`page_size`(≤100)；`per_source_limit`（每源站候选上限，默认 200，一般不用动）。

**响应**：

```json
{
  "code": 1, "wd": "三体", "page": 1, "page_size": 20, "pagecount": 1,
  "total": 5,
  "total_is_partial": false,       // true 表示部分源站候选被截断，total 非精确全库数
  "truncated_sources": [],
  "sources_searched": 52,
  "sources_failed": [],            // 单源失败隔离，不影响整体结果
  "elapsed_ms": 76,
  "list": [{
    "source": "jkun", "source_name": "JKUN资源站",
    "vod_id": 123, "type_id": 6, "type_name": "连续剧",
    "vod_name": "三体", "vod_pic": "https://...",
    "vod_remarks": "全30集", "vod_time": "2026-10-29 21:33:12"
  }]
}
```

结果按 `vod_time` 倒序。`total` 是"候选上限内的命中数"（模糊搜索不做全库精确计数以保证速度），App 端搜索建议展示 `total` 并在翻页超出 `pagecount` 时停止。

### 3.6 GET `/api/v1/{slug}` — 苹果 CMS 兼容接口

与 MacCMS 采集接口结构一致，供现成播放器/采集软件直接填 URL 使用：

```bash
GET /api/v1/jkun?ac=list&pg=1        # 列表（轻量字段 + class 分类表）
GET /api/v1/jkun?ac=detail&pg=1      # 全字段列表（含播放地址）
GET /api/v1/jkun?ac=detail&ids=1,2   # 按 ID 批量详情
GET /api/v1/jkun?ac=list&wd=三体     # 搜索
```

响应结构：`{code, msg, page, pagecount, limit, total, class:[{type_id,type_name}], list:[...]}`，`ac=detail` 时 list 元素含 `vod_content / vod_play_url / vod_play_from / vod_play_list`。

### 3.7 管理接口详情

#### GET `/healthz`
`{"status":"ok","time":"..."}`；已启用 API Key 时同样需带 Key（可用它探测鉴权状态）。

#### GET `/api/v1/stats`
```json
{
  "code": 1,
  "sources_total": 52, "sources_enabled": 52,
  "sources_syncing": 3,        // 此刻真正在跑的任务数（不是"有断点"的站数）
  "sources_unfinished": 5,     // 全量未跑完（有断点）的站数，供"待续传"提示用
  "videos_total": 2640,
  "today_inserted": 1200, "today_updated": 30, "today_tasks": 8,
  "pause": {
    "global": false, "full": false, "incremental": false,
    "updated_at": { "full": "2026-10-30T14:08:30" }
  },
  "last_error": { "slug": "t1", "error": "timeout...", "at": "..." } | null,
  "server_time": "..."
}
```

#### GET `/api/v1/pause` / POST `/api/v1/pause` / POST `/api/v1/resume` — 同步暂停控制

三层级：`global`（全量+增量+定时任务全部停止）、`full`（仅全量）、`incremental`（仅增量）。

```bash
GET  /api/v1/pause                        # 查询状态
POST /api/v1/pause?scope=full&reason=xx   # 暂停（reason 可选）
POST /api/v1/resume?scope=full            # 继续（自动续跑受影响的源站）
```

**暂停响应**：
```json
{ "code": 1, "msg": "同步暂停已生效: full（进行中的批次将在下一个检查点停止）",
  "state": { "global": false, "full": true, "incremental": false, "updated_at": {...} } }
```

**继续响应**：
```json
{ "code": 1, "msg": "同步已继续: full",
  "resumed_sources": ["jkun", "t1"],      // 已自动续跑的源站
  "state": { ... } }
```

**语义说明**：
- 暂停为**优雅停止**：正在抓取的页面批次会完成并保存断点（`sources.last_page`）后才停，不丢数据；增量暂停时水位线不动
- 继续时服务端**自动重新触发**对应范围的同步（full 只续跑有断点的源站；incremental 对有水位线的源站重跑；global 按 auto 模式续跑全部）
- 暂停状态**持久化**，服务重启后仍生效，需再次 resume 才会恢复同步
- 暂停期间被触发（手动/定时）的同步任务直接返回 `status: "paused"`，日志状态为 `paused`

#### POST `/api/v1/sources/{slug}/pause` / `/api/v1/sources/{slug}/resume` — 单源站暂停

与上面的层级暂停**相互独立**，任一命中即停止该源站的同步（判定顺序：全局/分层 → 单源站）。

```bash
POST /api/v1/sources/jkun/pause      # 暂停该源站
POST /api/v1/sources/jkun/resume     # 继续该源站（自动按 auto 续跑）
```

```json
// pause 响应
{ "code": 1, "msg": "源站 JKUN资源站 的同步已暂停", "slug": "jkun", "paused": true }

// resume 响应（若上层仍有暂停，blocked_by 会给出被阻断的层级）
{ "code": 1, "msg": "源站 ... 的同步已继续（但当前存在全局暂停，任务将等待）",
  "slug": "jkun", "paused": false, "blocked_by": "full" }
```

- 暂停标记持久化在 `sources.paused` 列（重启保留），`/api/v1/sources` 返回 `paused` 与 `paused_at`
- 单源站暂停后：`sync-all` 与定时任务自动跳过该源站；进行中的全量同步在下一检查点停止并保存断点
- `resume` 会对该源站按 auto 模式续跑：有断点则续全量、有水位线则走增量
- 管理面板源站列表每行提供「暂停 / 继续」按钮：**「暂停」只在 `running=true`（真的有任务在跑）时出现**；已暂停的站显示「继续」；空闲站不显示暂停/继续

#### POST `/api/v1/sources/{slug}/auto-sync` / `/api/v1/sources/auto-sync` — 自动同步开关

控制源站是否参与**自动同步**（定时增量任务、全站同步 `sync-all`、增量续跑）。与「暂停」的区别：暂停会阻断该源站的**一切**同步（含手动）；自动同步开关只影响**自动/批量**触发，手动单站同步始终可用。

```bash
POST /api/v1/sources/jkun/auto-sync?enabled=false        # 关闭单站自动同步
POST /api/v1/sources/auto-sync?enabled=false&slugs=all   # 全部关闭
POST /api/v1/sources/auto-sync?enabled=true&slugs=jkun,t1  # 只开启指定站
```

```json
// 单站响应
{ "code": 1, "msg": "源站 JKUN资源站 自动同步已暂停", "slug": "jkun", "auto_sync": false }
// 批量响应
{ "code": 1, "msg": "已暂停 52 个源站的自动同步", "updated": 52, "auto_sync": false }
```

- 状态持久化在 `sources.auto_sync` 列（默认 1，重启保留），`/api/v1/sources` 返回 `auto_sync`
- **全局定时调度保留不变**（源站列表刷新 + 全站增量），只是被关闭的站不再参与
- App 端建议：用 `/api/v1/sources` 拉列表渲染开关，勾选多行后调批量接口，单行开关直接调单站接口

#### GET `/api/v1/sources`
```json
{ "code": 1, "list": [{
    "slug": "jkun", "name": "JKUN资源站", "api_url": "https://...", 
    "status": "ok", "enabled": true,
    "total_videos": 2640,
    "watermark": "2026-10-30 12:00:00",   // 增量同步水位线
    "running": true,                       // ✅ 此刻真的有任务在跑（唯一决定"能否暂停"的字段）
    "running_mode": "full",                // full / incremental；未运行为 null
    "running_seconds": 42.5,               // 本次已运行秒数；未运行为 null
    "syncing": true,                       // ⚠️ 全量同步"未跑完"（有断点），不代表正在跑
    "progress": 1.8,                       // 全量进度 0-100（已完成=100；刚开跑未取到总页数=0）
    "last_page": 120, "total_pages": 6710, // 断点位置
    "paused": false,                       // 单源站暂停标记（持久化）
    "paused_at": null,
    "last_sync_at": "2026-10-30T13:00:30"
}]}
```

> **`running` 与 `syncing` 的区别（客户端务必分清）**：
> - `running=true` → 该源站此刻正在执行同步，**只有此时才应显示「暂停」按钮**
> - `syncing=true` → 全量同步历史上被中断、留有断点；可能已经不在跑（常见于服务重启后），此时不应提供「暂停」
> - 展示建议：`running` → 显示进度条+暂停；`syncing && !running` → 显示"断点 N/M 页 · 未在同步"；`paused` → 显示「继续」

#### GET `/api/v1/sources/{slug}`
`{"code":1,"source":{...},"recent_logs":[最近10条同步日志]}`，日志字段同 `/api/v1/logs`。

#### POST `/api/v1/sources/refresh`（可选 `?full_sync=true`）
`{"code":1,"new_sources":["新站slug",...],"full_sync_started":false}`

#### POST `/api/v1/sync/{slug}?mode=auto|full|incremental`
- `auto`：有数据走增量，无数据走全量（**推荐默认**）
- `full`：从第 1 页重新全量（支持断点续传）
- `incremental`：仅增量

#### POST `/api/v1/sync-all?mode=auto|full|incremental`
同上，作用于全部启用源站（后台逐站执行）。

#### GET `/api/v1/logs?limit=50`（1-200）
```json
{ "code": 1, "list": [{
    "id": 99, "slug": "jkun", "mode": "full", "status": "success"|"failed"|"partial",
    "fetched": 500, "inserted": 480, "updated": 20, "pages": 25,
    "error": "", "started_at": "...", "finished_at": "..."
}]}
```

#### POST `/api/v1/maintenance/recount`
异步重算各源站本地条数，`{"code":1,"msg":"条数校准任务已受理"}`。

---

## 4. 客户端典型流程（可直接给 AI 的实现指引）

### 4.1 观影 App（数据端）

```
启动
 ├─ GET /healthz                    → 判断服务可用 & 是否需要 API Key（401→弹输入框）
 ├─ GET /source?only_ready=true     → 源站选择页数据源（slug/name/video_count/status）
 ├─ GET /api/v1/stats               → 首页概览（视频总量等）
 │
首页「最近更新」
 └─ GET /api/v1/videos?page=1       → 封面墙（vod_pic + vod_name + vod_remarks + source）
 │
分类浏览
 ├─ GET /api/v1/categories?source=  → 分类标签栏
 └─ GET /api/v1/videos?source=&type_id=&page=   → 翻页加载
 │
搜索
 └─ GET /api/v1/search?wd=          → 结果列表；点条目拿 source+vod_id 查详情
 │
详情页
 ├─ GET /api/v1/videos/{source}/{vod_id}
 └─ 渲染 vod_play_list → 线路选择器 + 选集列表 → 播放器加载 episodes[i].url
```

**缓存建议**：`/source`、`/api/v1/categories` 变化少可缓存 10-30 分钟；列表按 `page` 缓存；详情不缓存或短缓存（`vod_time` 变化代表有更新）。

### 4.2 管理端 App

```
仪表盘:  GET /api/v1/stats  +  GET /api/v1/sources（进度条用 progress/syncing 字段，建议 5s 轮询）
操作:    POST /api/v1/sync/{slug}   POST /api/v1/sync-all   POST /api/v1/sources/refresh
         （均为异步受理 → 操作后刷新 sources 轮询进度）
暂停控制: GET /api/v1/pause + POST /api/v1/pause / /api/v1/resume（三个开关对应三个 scope，
         开关状态轮询 /api/v1/pause；暂停时页面横幅提示，源站行加"已暂停"徽标）
运维:    GET /api/v1/logs           GET /api/v1/sources/{slug}
         POST /api/v1/maintenance/recount（条数显示异常时调用）
```

---

## 5. 注意事项与坑

1. **中文参数必须 URL 编码**。原始 TCP 请求行不允许非 ASCII，未编码会得到 400。浏览器/前端 fetch 自动处理；命令行示例：`curl -G ".../api/v1/search" --data-urlencode "wd=三体"`。
2. **一个 URL 只能有一个 `?`**。写成 `?a=1&?b=2` 时第二个键是 `?b`。数据接口做了兼容，但请勿依赖。
3. **`source` 只接受 `a-z0-9_`**，否则 400。
4. **搜索 `total` 是近似值**（见 3.5），翻页逻辑以 `pagecount` 为准。
5. **ids 详情返回按传入顺序**，不做排序；混入非法数字会被静默忽略。
6. **多源列表请求会逐表扫描**，源站多时建议明确传 `source=` 限定，或分源请求。
7. **图片防盗链**：部分 `vod_pic` 外链有 Referer 限制，App 端建议 `Referer` 置空或不发送；H5 端可走 `<img referrerpolicy="no-referrer">`。
8. **播放地址时效性**：m3u8 直链可能失效，详情数据以 `vod_time` 判断新旧，必要时重新请求详情。
9. **写操作幂等性**：sync 类接口重复触发会在后台排队执行，无需客户端去重，但也不建议高频连点。
10. **机器可读规范**：对接工具支持 OpenAPI 时直接导入 `http://<host>:8000/openapi.json`，参数与响应模型与服务端实现实时同步。
11. **别用 `syncing` 判断"正在跑"**：它只表示"全量还有断点未跑完"。判断某站此刻是否在执行同步必须用 `running`；否则会出现"站已停但界面还显示暂停按钮"的错误交互。

---

*文档版本：对应服务 v1.3.0。字段如有增补，以 `/docs`（Swagger）与 `/openapi.json` 为准；完整交付版见 [`API_SPEC_FOR_AI.md`](./API_SPEC_FOR_AI.md)。*
