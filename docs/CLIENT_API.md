# VodSync 客户端 API 对接文档

> ⚠️ 本文是**简明对接说明**。完整交付版（含字段字典、状态语义、代码骨架、排查表）见 **[`API_SPEC_FOR_AI.md`](./API_SPEC_FOR_AI.md)** —— 交给 AI 开发客户端时请使用该文档。

> 本文档面向「调用 VodSync 服务的客户端 App」（原生 App / H5 / 小程序 / 播放器 / 采集器均可）。
> 目标：一个 AI 客户端开发者（或 AI 编程助手）拿这份文档即可完成全部对接，无需阅读服务端代码。
>
> 对应服务版本：**v1.15.1** ｜ 文档版本：`2026-11-11` ｜ 机器可读规范：[`openapi.json`](./openapi.json)

---

## 1. 基础约定

| 项 | 说明 |
|---|---|
| Base URL | `http://<服务IP>:8000`（群晖部署示例端口 8181，以实际部署为准） |
| 数据格式 | 全部为 JSON（`Content-Type: application/json`） |
| 请求方法 | 数据查询用 `GET`；管理操作（触发同步等）用 `POST` |
| 编码 | UTF-8。中文参数务必 URL 编码（浏览器/fetch 自动处理；命令行用 `--data-urlencode`） |
| OpenAPI | 服务自带机器可读规范：`GET /openapi.json`（Swagger UI 在 `/docs`），AI 工具可直接据此生成客户端代码 |

### 1.1 鉴权（v1.5.0 起：API Token）

服务端默认启用访问控制：除 `GET /healthz` 与 `/api/v1/auth/*` 外，所有接口都要带 Token，三种带法任选：

```
请求头:  Authorization: Bearer vs_xxxxx      ← 推荐
请求头:  X-API-Key: vs_xxxxx                 ← 兼容旧客户端
或参数:  ?token=vs_xxxxx                     ← 无法自定义请求头的场景（<img>、播放器内核）
```

Token 来源：登录管理面板 → 右上「⚙ 设置」→ 复制 API Token；或调 `GET /api/v1/auth/token`（需登录会话）。

> **Token 有 `read` / `admin` 两档权限（v1.14.0 起）**：给 App 播放用建议签发只读
> Token（面板「⚙ 设置」→「签发只读 Token」，或 `POST /api/v1/auth/tokens`，
> 明文只返回一次）。管理接口清单 / 吊销等运维操作见 `API_SPEC_FOR_AI.md` 4.26 节。
> 注意：`TOKEN_SCOPE_ENFORCE=false`（默认）期间所有 Token 等价 admin，正式分发
> read Token 前请先在服务端开启强制校验。

鉴权失败返回 `401 {"detail":"未授权：请求头需携带 Authorization: Bearer <token>..."}`。

> 判断是否启用鉴权：调 `GET /api/v1/auth/state`，`{"enabled":true}` 即已启用。
> `/healthz` **永远返回 200**（容器健康检查用），不能用它判断鉴权状态。

### 1.2 统一响应结构

> **v1.14.0 起语义收紧（字段名一个没改）**：成功 = HTTP 2xx **且** `code == 0`；
> 业务失败 = **非 2xx** + `{code, error, detail}`。不再有「200 + code:1 表示失败」。
> 成功码可用服务端 `API_SUCCESS_CODE` 改回 1（灰度回滚开关）；
> **例外**：`/api/v1/{slug}`（苹果 CMS 兼容）恒为 `code:1`。

**成功**：HTTP 200，`code: 0`：

```json
{ "code": 0, "total": 100, "list": [ ... ] }
```

**失败**：HTTP 4xx/5xx：

```json
{ "code": 40001, "error": "bad_request", "detail": "参数错误：page 输入应大于等于 1",
  "message": "参数错误：page 输入应大于等于 1", "field": "page" }
```

| 字段 | 说明 |
|---|---|
| `code` | 程序判据。`0` 成功；`40001` 参数、`40101` 鉴权、`40301` 权限、`40401` 不存在、`50001` 内部错误 |
| `error` | 稳定英文串（`bad_request` / `invalid_token` / `insufficient_scope` / `not_found` / `internal_error`） |
| `detail` | 人可读中文（**历史上客户端读的就是它**，永远保留） |
| `message` | 与 `detail` 同值（新客户端可只读这个） |
| `field` | 参数错误时指出具体字段 |
| `required` | 403 时说明需要什么权限（`admin`） |

| HTTP 状态码 | 含义 |
|---|---|
| 200 | 成功（`code=0`） |
| 400 | 参数错误（含**校验失败**，v1.14.0 起原 422 统一为 400 并带 `field`） |
| 401 | 未授权：Token 缺失/错误/已轮换（`code=40101`） |
| 403 | 权限不足：只读 Token 调写接口（`code=40301`，`required: "admin"`） |
| 404 | 资源不存在（源站 / 视频 / 尚未同步数据 / 表），`detail` 中带补救建议 |
| 429 | 预留（限流） |
| 500 | 服务端异常（**不回显堆栈**） |
| 503 | 数据库繁忙，带 `Retry-After`，按指数退避重试 1–3 次 |

### 1.3 通用参数

| 参数 | 适用接口 | 说明 |
|---|---|---|
| `source` | videos / categories / search | 源站标识（slug），**逗号分隔可多源合并**；留空 = 全部源站。slug 从 `/source` 或 `/api/v1/sources` 获取，仅允许 `a-z 0-9 _` |
| `page` / `pg` | 列表类 | 页码，从 1 开始 |
| `page_size` | 列表类 | 每页条数，**v1.14.0 起默认 18**（与客户端固定传值一致），上限 100；超上限返回 `400 / 40001` |
| `tag` | search / videos | **v1.14.0**：标签精确检索（与 `wd` 二选一，同时传以 `tag` 为准），见 3.5 |
| `minor_mode` | 全部数据接口 | **v1.15.0**：未成年模式开关（`1`/`0`，也接受 `minor` / `teen_mode` 别名）；推荐改用请求头 `X-Minor-Mode`，见 1.5 |

### 1.4 时间格式

所有时间字段为 ISO 8601 字符串或 `"YYYY-MM-DD HH:MM:SS"`（来自源站原始数据），如 `"2026-10-30T13:00:30"`。可直接字符串比较排序。

### 1.5 未成年模式与 18+ 源站标记（v1.15.0）

服务端为每个源站持久化一个「是否包含 18+ 视频内容」的标记，管理面板「源站管理」表格
的 **18+ 内容** 列可逐站查看 / 修改（选择即保存）；勾选多行后用 **标记所选 18+** /
**取消所选 18+** 批量改（走 `POST /api/v1/sources/adult`，一次请求）。**存量源站未设置一律按「否」**。

**能力探测**：`GET /healthz`（或 `/healthz.capabilities`）的 `capabilities` 数组含
`minor_mode` 才表示服务端已支持。老版本没有这项，前端应降级为「只在本地隐藏入口」。

**开启**：客户端的**每一个** API 请求都要带上开关，二选一：

```
请求头:  X-Minor-Mode: 1                  ← 推荐（所有请求统一加）
查询参数: ?minor_mode=1                   ← 无法自定义请求头的场景（<img>、播放器内核）
```

也接受 `?minor=1` / `?teen_mode=1`；值 `1 / true / on / yes` 为开，`0 / false / off / no` 为关。
请求头优先于查询参数。

**关闭**：不带上述参数即可，也可显式传 `0`。屏蔽只作用于「读路径的可见集合」，
**从不删改任何数据**，所以关闭后原有展示与访问行为立即恢复，无需重新同步或刷新缓存。

开启后服务端的行为（前端不需要自己再过滤一遍）：

| 位置 | 行为 |
|---|---|
| `GET /source`、`GET /api/v1/sources` | 直接剔除 18+ 源站；每项新增 `is_adult` 布尔字段，响应顶层新增 `minor_mode: true` |
| `GET /api/v1/categories` / `videos` / `search` | 只查询非 18+ 源站；全部被屏蔽时返回 `200 + list=[]`（不是 404），`total=0` |
| `GET /api/v1/videos/{slug}/{vod_id}` | 18+ 源站的详情（含播放地址）直接 404 |
| `GET /api/v1/{slug}?ac=list\|detail` | 苹果 CMS 兼容接口同样 404，第三方采集器/播放器不能绕过 |
| `GET /api/v1/related` / `star/related` / `star/works` | 推荐与明星作品入口同样 404 |
| `GET /api/v1/sources/{slug}` | 单源站详情同样 404 |

即使前端显式传 `?source=<18+ 的 slug>` 或 `?ids=...` 点名访问，服务端也会先剔除，
**搜索和批量详情不是旁路**。管理面板不带该开关，所以始终能查看并编辑全部源站，
同步、词库、Token、统计、数据表浏览等原有管理操作完全不受影响。

**管理端接口**：

| 方法 | 路径 | 用途 |
|---|---|---|
| POST | `/api/v1/sources/{slug}/adult?enabled=true\|false` | 设置单源站 18+ 标记（也可 body `{"enabled": true}`）；只改这一列，立即生效 |
| GET | `/api/v1/minor-mode` | 诊断：`forced` / `requested` / `active` / `adult_sources` / `adult_count` |

**兼容性（重点）**：不传 `X-Minor-Mode` / `minor_mode` 时，服务端**一次额外查询都不做**，
所有接口的字段、分页、错误码与 v1.14.1 完全一致。新增的 `is_adult` / `minor_mode`
都是纯新增字段，老客户端按「多余字段忽略」处理即可，不需要发版。

**已知限制**：请求级开关由客户端自报，技术上可以去掉头绕过。需要服务端强制管控时，
在部署环境设 `MINOR_MODE_FORCE=true`（compose 环境变量），此时所有请求都进入未成年模式，
客户端无法通过不带头关闭。默认是 `false`。FORCE 生效期间**管理面板的源站清单也会隐藏
18+ 行**（面板走同一个 `GET /api/v1/sources`），要改标记请临时关掉 FORCE 或直接调
`POST /api/v1/sources/{slug}/adult`（该端点不受拦截）。另外
`GET /api/v1/admin/tables/{name}/rows` 是管理员会话专用的数据表浏览接口，按
「原有管理操作不受影响」的要求保持可见。

---

## 2. 接口总览

### A. 数据接口（客户端核心使用）

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/source` | 资源站清单（**v1.3 起为兼容接口**，新客户端请用 `/api/v1/sources`） |
| GET | `/api/v1/categories` | 分类清单（分类浏览入口） |
| GET | `/api/v1/videos` | 视频列表 / 批量详情 / 分类浏览（REST 风格，多源合并） |
| GET | `/api/v1/videos/{slug}/{vod_id}` | 单条视频详情（含播放地址） |
| GET | `/api/v1/search` | 跨源站聚合搜索 |
| GET | `/api/v1/related` | 相关推荐（服务端预计算，详情页「相关」栏） |
| GET | `/api/v1/star/related` | 明星维度推荐（详情页「全部/按明星」Tab） |
| GET | `/api/v1/star/works` | 按明星查作品（**v1.9.2** 明星 Tab 完整作品列表） |
| GET | `/api/v1/minor-mode` | **v1.15.0** 未成年模式状态（是否生效 / 被标记的 18+ 源站） |
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
| POST | `/api/v1/sources/{slug}/adult?enabled=` | **v1.15.0** 单源站「是否含 18+ 内容」标记（面板源站管理表格调用） |
| POST | `/api/v1/sources/adult?enabled=&slugs=` | **v1.15.1** 批量设置 18+ 标记（`slugs=all` 或逗号分隔；面板勾选多行后一次改完） |
| POST | `/api/v1/sources/auto-sync?enabled=&slugs=` | 批量自动同步开关（`slugs=all` 或逗号分隔列表） |
| GET | `/api/v1/logs?limit=50` | 最近同步日志（跨源站） |
| POST | `/api/v1/maintenance/recount` | 条数校准（异步执行） |
| GET | `/api/v1/admin/tables` | **v1.12.0** 数据表浏览：可浏览表清单（含列元信息与行数；`settings` 有意排除）。**v1.12.1** 每项新增 `label`（`vod_*` 表显示源站名，如「新极品资源站」），请求仍用 `name`（表名） |
| GET | `/api/v1/admin/tables/{name}/rows` | **v1.12.0** 数据表浏览：分页查看数据（`page` / `page_size`≤100 / `field` / `q`，页码越界自动夹回） |

> 管理接口全部为**异步受理**模式：POST 立即返回 `{"code":0,"msg":"...已受理"}`
> （v1.14.1 纠偏：v1.14.0 起成功码统一为 0，原文档示例仍是旧的 1，已过时），实际进度通过轮询 `/api/v1/sources`（看 `syncing`/`progress` 字段）与 `/api/v1/logs` 获取。
>
> 上表只列客户端最常用的管理接口。**完整清单（共 57 个，方法+路径）见
> [`API_SPEC_FOR_AI.md` 4.0 接口总表](./API_SPEC_FOR_AI.md)** —— 其中鉴权（`/api/v1/auth/*`）、
> 自定义源站（`/api/v1/sources/custom` 等）、明星词库维护（`/api/v1/star-terms*`）、
> 明星 Tag 重建（`/api/v1/maintenance/star-tags`）本文未展开。

---

## 3. 接口详情

### 3.1 GET `/source` — 资源站清单

App 初始化时调用一次，得到全部可用源站及其对接路径。

**参数**：`status=ok|down`（按上游状态过滤）、`only_ready=true`（仅返回已有本地数据的源站）；
v1.15.0 起可带 `X-Minor-Mode: 1` / `?minor_mode=1`，带上了这份清单就不含 18+ 源站（见 1.5）。

**响应**：

```json
{
  "code": 0, "minor_mode": false, "total": 52, "alive": 52, "ready": 1,
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
    "local_list_api": "/api/v1/videos?source=jkun",  // 本服务 REST 路径
    "is_adult": false                // v1.15.0：是否含 18+ 内容（纯新增，老客户端忽略）
  }]
}
```

列表按 `video_count` 倒序（数据多的站在前）。`is_adult` 与顶层 `minor_mode` 均为
v1.15.0 纯新增字段；未成年模式下 18+ 源站**不会出现在 list 里**，`total` 也随之变小，
不需要客户端再做一次本地过滤。

### 3.2 GET `/api/v1/categories` — 分类清单

> v1.10.0：响应多一个 `cached`（`true`=命中服务端缓存，默认 5 分钟）。纯诊断字段，客户端忽略即可。

**参数**：`source`（可选，多源逗号分隔；留空合并全部源站）

**响应**：

```json
{
  "code": 0, "total": 23,
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

**通用可选参数**：`wd`（名称模糊搜索）、`type_name`、`type_id`、`page`、`page_size`、
`play_raw`（v1.11.0：`1`=返回未过滤的原始播放线路，排障用）

**列表模式响应**（①④，轻量字段，无播放地址）：

```json
{
  "code": 0, "page": 1, "pagecount": 133, "total": 2640,
  "list": [{
    "source": "jkun",          // ★ 详情请求时回传的 slug
    "vod_id": 134369,          // ★ 详情请求时回传的 id
    "type_id": 6,
    "type_name": "素人搭讪",
    "vod_name": "XXX",
    "vod_pic": "https://.../cover.jpg",
    "vod_remarks": "HD",       // 通常为清晰度/更新至第N集
    "remarks": "HD",           // v1.14.0：与 vod_remarks 同值（契约名），两个都给
    "vod_area": "大陆",        // v1.14.0：地区（列表卡片展示用）
    "vod_year": "2024",        // v1.14.0：年份
    "vod_time": "2026-10-29 21:33:12"
  }]
}
```

**详情模式响应**（②③，`list` 内每条额外包含）：

```json
{
  "vod_content": "简介文本...",
  "vod_play_url": "第01集$https://.../share/e5c6...$$$第01集$https://.../index.m3u8#...",
  "vod_play_from": "vodsync",          // MacCMS 兼容字段，常量
  "play_from": "feifan$$$ffm3u8",      // v1.11.0：上游 provider 串（未回填时为空串）
  "vod_play_list": [
    {
      "name": "线路1 (ffm3u8)",        // v1.11.0：编号是过滤后的序号，括号内是 provider
      "from": "ffm3u8",                // v1.11.0
      "type": "m3u8",                  // v1.11.0：m3u8/mp4/flv/mkv/page/unknown
      "playable": true,                // v1.11.0：type != "page"
      "original_index": 1,             // v1.11.0：在上游 vod_play_url 里的下标（0 基）
      "episodes": [
        { "name": "第01集", "url": "https://.../index.m3u8",
          "from": "ffm3u8", "type": "m3u8", "playable": true },   // v1.14.0：逐集也带这三项
        { "name": "第02集", "url": "https://.../index2.m3u8",
          "from": "ffm3u8", "type": "m3u8", "playable": true }
      ]
    }
  ],
  "play_lines": [ /* v1.14.0：与 vod_play_list 同值，契约里叫 play_lines */ ],
  "tag": "悬疑,犯罪"                  // v1.14.0：与 vod_tag 同值
}
```

> **v1.14.0：`play_lines` / `remarks` / `tag` 是同值别名。** 契约文档里这三个字段叫这个名字，
> 但历史接口一直叫 `vod_play_list` / `vod_remarks` / `vod_tag`。两边都不能删（改哪个都会让
> 一半客户端白屏），所以同值双发；JSON 里重复的部分 gzip 会压掉，带宽代价接近 0。
> 新代码建议用契约名，老代码不用动。

**播放地址使用建议**：优先取 `vod_play_list[*].episodes[*].url`（已结构化且**已过滤掉不可播的分享页线路**）；
`vod_play_url` 是源站原始串仅作兜底。地址多为 m3u8 直链，可直接交给播放器内核。

> **v1.11.0：`vod_play_list` 默认已过滤。** 部分源站的第 1 条线路是 H5 分享页
> （`/share/<hash>` → 返回 `text/html`，真实地址由页面 JS 生成且带会过期的 `sign`），
> 播放器直接播必然失败。服务端现在默认把这类线路剔除，客户端「选第一条线路」就能播。
> - 真的过滤掉时，响应顶层多一个 `"play_line_filtered": true`（诊断用，可忽略）；
> - `?play_raw=1` 可拿到未过滤的原始列表（排障）；
> - `vod_play_url` **从未被改写**，自己解析原始串的逻辑不受影响；
> - 客户端若缓存线路列表，请按 `original_index` 对齐，别用 `name`（编号会随过滤变化）。

> **v1.10.0：`total` 可能是估值。** 不带 `wd` / `type_id` / `type_name` / `ids` 的全站列表，`total` 改为取 `SUM(sources.total_videos)`（后台同步一直维护的数字），并在响应里带 `"total_approximate": true`，省掉 52 张表各跑一次 `count(*)`。
> 影响：`total` 与 `pagecount` 可能有 1~2 页级别的偏差（翻到最末页才看得出来）。带筛选条件时仍是精确值。
> 建议：需要「共 N 条」这类强展示时，判断到 `total_approximate` 就把文案降级为「约 N 条」，或只展示已加载的条数。

**错误**：
- `ids` 无数字格式错误 → 400
- `ids` 均未命中 → 404（detail 中提示改用搜索接口确认 vod_id）
- 指定源站均无数据 → 404

### 3.4 GET `/api/v1/videos/{slug}/{vod_id}` — 单条详情

```bash
GET /api/v1/videos/jkun/134369
```

响应 `{"code":0, "detail": { ...详情字段，同 3.3 详情模式... }}`。不存在返回 `404 / 40401`。

> **v1.11.0：播放线路默认已过滤**（同 3.3）。`detail.vod_play_list` 里不会再出现
> `/share/` 这类 H5 分享页；每条线路多 `from` / `type` / `playable` / `original_index`，
> `detail.play_from` 是上游 provider 串；真过滤了线路时响应顶层带 `"play_line_filtered": true`。
> `?play_raw=1` 返回未过滤列表。

> **v1.10.1：详情多了 5 个字段**（纯新增，旧客户端忽略即可）：
> `vod_actor`（演员，逗号分隔）、`vod_director`（导演）、`vod_area`（地区）、
> `vod_year`（年代，4 位字符串）、`vod_tag`（**明星标签**，逗号分隔）。
> 五个键**永远存在**且一定是字符串，库里缺失/NULL 时返回 `""` —— 客户端直接显示即可，不用判 null。
> `?ids=` 批量详情与 `?ac=detail` 列表同样带这 5 个字段；普通列表模式（不带 `ac`/`ids`）**不带**，以控制体积。
> NAS 模式下从此不需要用 `star/related` 的 `stars` 去凑演员，标签也不再整行隐藏。

### 3.5 GET `/api/v1/search` — 跨源站聚合搜索

```bash
GET /api/v1/search?wd=三体                        # 全部源站
GET /api/v1/search?wd=三体&source=jkun,t1         # 限定源站（更快）
GET /api/v1/search?wd=三体&type_name=电影&page=1
```

**参数**：`wd`（1-64 字）与 `tag`（1-64 字）**至少给一个**，同时传时以 `tag` 为准；`source`、`type_name` 可选；
`page`、`page_size`（**默认 18**，≤100）；`per_source_limit`（每源站候选上限，默认 200，仅 `wd` 路径生效）。

#### `?tag=` 标签精确检索（v1.14.0）

```bash
GET /api/v1/search?tag=悬疑&source=ffzy&page=1&page_size=18
```

详情页的标签胶囊点进去用这个，**不要**用 `wd=悬疑` —— 后者是名称模糊搜索，命中的是
「片名里含悬疑」的片，和「标签是悬疑」完全不是一回事。

- 匹配规则：`vod_tag` 按 `,` `，` `/` `|` `、` 切开后**整段相等**（大小写仅 ASCII 不敏感）。
  `tag=悬疑` 命中 `"悬疑,犯罪"`，**不命中** `"悬疑喜剧"` / `"反悬疑"`。
- `total` / `pagecount` 精确，`total_approximate` 恒为 `false`。
- 空结果是 **200 + 空 list**（不是 404）。
- **能力门控**：`GET /healthz.capabilities`（v1.14.1 起是真实免鉴权端点，也可读
  `/healthz` 响应里的 `capabilities` 字段）含 `tag_search` 才用；没有就退回 `wd=`
  （索引没建完会漏数据）。
- **`tag_found`（v1.14.1）**：tag 检索是标签词精确匹配，与 `wd` 标题全文检索命中集
  天然不同 —— 片名里的词可能没人把它写成标签。响应带 `tag_found`：
  `false` = 这个词不在任何源站的标签表里（`total` 必为 0），**客户端应回退 `wd=` 再试**，
  不要把空结果直接展示给用户。

**响应**：

```json
{
  "code": 0, "wd": "三体", "page": 1, "page_size": 20, "pagecount": 1,
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

> **这一节是唯一仍返回 `code:1` 的地方**（v1.14.0 起其余接口成功码统一为 0）。
> 苹果 CMS 采集端按 `code==1` 判成功，改它会让第三方采集器/播放器直接失效，故保持原样。

### 3.7 推荐与明星接口（v1.9.0 ~ v1.10.1）

> 详情页推荐栏的三层数据来源：`related`（内容相似）→ `star/related`（全部 Tab）→ `star/works`（单个明星 Tab）。

#### GET `/api/v1/related` — 相关推荐（内容相似，预计算）

```bash
GET /api/v1/related?slug=jkun&id=134369&limit=12
```
返回 `{code, slug, vod_id, limit, fallback, computed_at, algo_version, list:[{vod_id, vod_name, vod_pic, vod_remarks, type_name, source, source_name, score, mode, reason}]}`。
列表**永远非空**（零命中走兜底链）；`fallback=true` 表示整条来自兜底（同分类→同地区/年代→最新），客户端可弱化展示。未知 `slug`/`id` → 404。

v1.10.0 追加两个字段（纯新增，老代码忽略即可）：

| 字段 | 说明 |
|---|---|
| `algo_version` | 当前算法版本（字符串，本版为 `"2"`）。做本地缓存时把它放进 key，算法升级后缓存自动失效 |
| `list[].reason` | 该条命中的原因，逗号分隔，如 `title,actor` / `director`；兜底条目为空串。可做「因为你也看过…」副标题 |

**行为变化（升级需知）**：仅「同分类+同地区+同年代」而不共享任何标题/演职员信号的配对，本版起不再进推荐位（旧版会给这类不相关影片 0.667 的高分，是误判）。零命中时兜底条数从 12 条降为 6 条，且不再与真命中混排 —— 所以推荐栏偶尔会比以前短，属正常。

#### GET `/api/v1/star/related` — 明星维度（详情页「全部」Tab，v1.9.2 增强）

```bash
GET /api/v1/star/related?slug=jkun&vod_id=134369&limit=50
GET /api/v1/star/related?slug=jkun&name=流浪地球      # 名称兜底（先精确后模糊）
```

```json
{
  "code": 0,
  "target": {"vod_id": 134369, "vod_name": "流浪地球"},
  "stars": ["吴京", "郭帆"],
  "star_counts": {"吴京": 42, "郭帆": 7},
  "truncated": true,
  "list": [{
    "vod_id": 135001, "vod_name": "战狼2", "vod_pic": "https://...",
    "vod_remarks": "HD国语", "type_name": "动作片", "source": "jkun",
    "match_stars": ["吴京"], "overlap": 1
  }]
}
```

| 字段 | 说明 |
|---|---|
| `stars` | **string[]**，本视频命中的明星（结构未变，向后兼容） |
| `star_counts` | **v1.9.2 新增**：`{明星名: 作品数}`，按作品数降序 —— Tab 角标用 |
| `truncated` | **v1.9.2 新增**：候选总数超过 `limit` 被截断时为 `true`（提示「查看更多」）。**v1.10.0 起改为精确判定**：命中不足 `limit` 时为 `false`，不会再出现「按钮常亮但点不出内容」 |
| `list[].match_stars` | 该条与目标重叠的明星名 |
| `list[].overlap` | 重叠明星数，列表按它降序、同分按更新时间降序 |

`name` 兜底已做健壮性处理：前后/中间空格差异、全半角差异、`%`/`_` 通配符转义；确实找不到 → 404（主路径请用 `vod_id`）。

#### GET `/api/v1/star/works` — 按明星查作品（v1.9.2，明星 Tab）

```bash
GET /api/v1/star/works?slug=jkun&term=吴京&limit=30&exclude_id=134369
```

```json
{
  "code": 0, "slug": "jkun", "term": "吴京", "total": 42,
  "list": [{
    "vod_id": 135001, "vod_name": "战狼2", "vod_pic": "https://...",
    "vod_remarks": "HD国语", "type_name": "动作片", "source": "jkun",
    "match_stars": ["吴京"], "overlap": 1
  }]
}
```

| 参数 | 说明 |
|---|---|
| `slug` | 必填，源站 |
| `term` | 必填，明星名（主名或别名均可，服务端归一化；响应里 `term` 返回归一后的主名） |
| `limit` | 默认 30，上限 100 |
| `exclude_id` | 可选，排除当前视频（详情页展示「其他作品」时传） |

- **排序**：`vod_time desc, vod_id desc`（最新作品靠前）
- **total**：该明星现存作品总数（**不受 limit 影响**，也不含 `exclude_id` 排除的条目）——客户端据此展示「共 N 部」并判断是否需要翻页
- `term` 无映射或作品已下架 → **200 + `total:0` + `list:[]`**（不是 404，避免客户端 Tab 报错）
- 走 `star_map` 的 `(slug, term)` 索引，P95 < 50ms
- 与 `star/related` 的区别：它**不按重叠明星数全局截断**，所以命中多位明星的目标视频，作品少的明星也不会被挤出

#### 客户端推荐栏建议流程

```text
进入详情页
  ├─ 调 /api/v1/star/related?limit=50        → 渲染「全部」Tab（用 star_counts 画明星 Tab 角标）
  └─ 用户点某个明星 Tab
        └─ 调 /api/v1/star/works?term=该明星&exclude_id=当前id → 渲染该明星完整作品（total 显示「共 N 部」）
```

### 3.8 管理接口详情

#### GET `/healthz`
`{"status":"ok","db":true,"version":"1.11.0","build":"<40 位提交 SHA>","time":"..."}`；**免鉴权**（容器健康检查用），不反映业务状态。
`version`/`build` 是服务端版本与构建提交，可在「关于」页展示，便于用户报障时定位版本。

#### GET `/api/v1/stats`
```json
{
  "code": 0,
  "sources_total": 52, "sources_enabled": 52,
  "sources_syncing": 3,        // 此刻真正在跑的任务数（不是"有断点"的站数）
  "sources_unfinished": 5,     // 全量未跑完（有断点）的站数，供"待续传"提示用
  "videos_total": 2640,
  "today_inserted": 1200, "today_updated": 30, "today_tasks": 8,
  "pause": {
    "global": false, "full": false, "incremental": false,
    "updated_at": { "full": "2026-10-30T14:08:30" }
  },
  "play_line": {                       // v1.11.0：播放线路过滤（本次进程启动以来的累计值）
    "policy": "filter",                // 全局策略
    "filtered_total": 148,             // 累计剔除的线路上数
    "fallback_total": 2,               // 触发安全阀（整部回退）的次数
    "page_lines_total": 150,           // 识别出的分享页线路总数
    "lines_total": 4200, "uptime_seconds": 3600
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
{ "code": 0, "msg": "同步暂停已生效: full（进行中的批次将在下一个检查点停止）",
  "state": { "global": false, "full": true, "incremental": false, "updated_at": {...} } }
```

**继续响应**：
```json
{ "code": 0, "msg": "同步已继续: full",
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
{ "code": 0, "msg": "源站 JKUN资源站 的同步已暂停", "slug": "jkun", "paused": true }

// resume 响应（若上层仍有暂停，blocked_by 会给出被阻断的层级）
{ "code": 0, "msg": "源站 ... 的同步已继续（但当前存在全局暂停，任务将等待）",
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
{ "code": 0, "msg": "源站 JKUN资源站 自动同步已暂停", "slug": "jkun", "auto_sync": false }
// 批量响应
{ "code": 0, "msg": "已暂停 52 个源站的自动同步", "updated": 52, "auto_sync": false }
```

- 状态持久化在 `sources.auto_sync` 列（默认 1，重启保留），`/api/v1/sources` 返回 `auto_sync`
- **全局定时调度保留不变**（源站列表刷新 + 全站增量），只是被关闭的站不再参与
- App 端建议：用 `/api/v1/sources` 拉列表渲染开关，勾选多行后调批量接口，单行开关直接调单站接口

#### GET `/api/v1/sources`
```json
{ "code": 0, "list": [{
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
    "last_sync_at": "2026-10-30T13:00:30",
    // ---- v1.11.0：播放线路策略 ----
    "play_line_policy": "inherit",         // inherit / off / filter / whitelist
    "play_line_effective": "filter",       // 实际生效值（inherit 时取全局）
    "play_line": { "lines": 120, "dropped": 118, "fallback": 0 }  // 读侧累计（进程内，重启清零）
}]}
```

> **v1.11.0 单源站线路策略**：`POST /api/v1/sources/{slug}/play-line-policy?policy=filter`
> （`inherit`/`off`/`filter`/`whitelist`，非法值 400）。只有「第 1 条线路恒为分享页」的站
> （如非凡资源站）需要设 `filter`，其余保持 `inherit`；改了立即生效，无需重新同步。

> **`running` 与 `syncing` 的区别（客户端务必分清）**：
> - `running=true` → 该源站此刻正在执行同步，**只有此时才应显示「暂停」按钮**
> - `syncing=true` → 全量同步历史上被中断、留有断点；可能已经不在跑（常见于服务重启后），此时不应提供「暂停」
> - 展示建议：`running` → 显示进度条+暂停；`syncing && !running` → 显示"断点 N/M 页 · 未在同步"；`paused` → 显示「继续」

#### GET `/api/v1/sources/{slug}`
`{"code":0,"source":{...},"recent_logs":[最近10条同步日志]}`，日志字段同 `/api/v1/logs`。

#### POST `/api/v1/sources/refresh`（可选 `?full_sync=true`）
`{"code":0,"new_sources":["新站slug",...],"full_sync_started":false}`

#### POST `/api/v1/sync/{slug}?mode=auto|full|incremental|backfill_play_from`
- `auto`：有数据走增量，无数据走全量（**推荐默认**）
- `full`：从第 1 页重新全量（支持断点续传）
- `incremental`：仅增量
- `backfill_play_from`（v1.11.0）：只回填播放线路来源（`play_from`）一列，
  不动其它字段/水位线/断点，可中断可重跑。存量库升级后按需对个别源站执行

#### POST `/api/v1/sync-all?mode=auto|full|incremental`
同上，作用于全部启用源站（后台逐站执行）。

#### GET `/api/v1/logs?limit=50`（1-200）
```json
{ "code": 0, "list": [{
    "id": 99, "slug": "jkun", "mode": "full", "status": "success"|"incomplete"|"failed"|"paused"|"interrupted",
    "fetched": 500, "inserted": 480, "updated": 20, "pages": 25,
    "error": "", "started_at": "...", "finished_at": "..."
}]}
```

#### POST `/api/v1/maintenance/recount`
异步重算各源站本地条数，`{"code":0,"msg":"条数校准任务已受理"}`。

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

*文档版本：对应服务 v1.5.0。字段如有增补，以 `/docs`（Swagger）与 `/openapi.json` 为准；完整交付版见 [`API_SPEC_FOR_AI.md`](./API_SPEC_FOR_AI.md)。*
