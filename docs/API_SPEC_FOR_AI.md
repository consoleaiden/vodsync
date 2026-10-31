# VodSync API 说明文档（AI 客户端开发交付版）

> **本文档的用途**：把 VodSync 服务作为后端，交由 AI（或 AI 编程助手）开发客户端 App。
> 文档自包含：只读本文即可完成全部对接，**不需要**阅读服务端源码。
>
> 对应服务版本：**v1.3.0** ｜ 文档版本：`2026-10-30` ｜ 机器可读规范：[`openapi.json`](./openapi.json)

**目录**

| 章节 | 内容 |
|---|---|
| [0](#0-给-ai-的硬性约定先读这一节) | 给 AI 的硬性约定（先读） |
| [1](#1-服务能提供什么) | 服务能力概览 |
| [2](#2-通用约定) | 鉴权 / 响应格式 / 错误码 / 分页 / 编码 |
| [3](#3-数据模型字段字典) | 数据模型与字段字典 |
| [4](#4-接口参考) | 22 个接口逐一说明 |
| [5](#5-状态语义与推荐-ui最容易做错的部分) | 状态语义与推荐 UI（running / syncing / paused / auto_sync） |
| [6](#6-端到端流程可直接交给-ai-实现) | 端到端流程（观影 App / 管理 App） |
| [7](#7-客户端代码骨架) | 客户端代码骨架（TS / Kotlin / Flutter / Swift） |
| [8](#8-性能与缓存建议) | 性能与缓存建议 |
| [9](#9-常见坑与排查表) | 常见坑与排查表 |
| [10](#10-附录) | 附录：OpenAPI、环境变量、版本 |

---

## 0. 给 AI 的硬性约定（先读这一节）

生成代码前请把这些规则当作不可协商的约束：

1. **Base URL 由部署方提供**，形如 `http://192.168.1.10:8000`（Docker 默认映射端口 `8000`；群晖示例可能用 `8181`）。文档中所有路径都相对于该地址，**不要**自行拼接域名或加 `/api` 前缀。
2. 所有响应为 JSON，编码 UTF-8；业务成功标记为 `code: 1`。
3. 失败是 **HTTP 状态码 + `detail`**，不是 `code: 0`。客户端必须按状态码分支，不要只判断 `code`。
4. `source` 参数是**源站标识 slug**（如 `jkun`、`s361c088099`），**不是**站点名。slug 只能匹配 `^[a-z0-9_]{1,64}$`；中文站名在服务端被哈希成 `s` + 10 位十六进制。
5. 列表接口的分页参数统一为 `page`（从 1 开始）与 `page_size`（默认 20，上限 100）。**超过 100 会直接 422**，不是自动截断——客户端请按 100 分页。
6. **一个 URL 只能有一个 `?`**。写成 `?a=1&?b=2` 时第二个键会变成 `?b`（服务端对数据接口做了兼容，但不要依赖）。
7. 播放地址以 `vod_play_list` 结构化字段为准，`vod_play_url` 是上游原始串，仅作兜底。
8. 管理类接口（触发同步、暂停、开关）都是**异步受理**：POST 立即返回，进度靠轮询 `GET /api/v1/sources` 得到。
9. 不要臆造字段。文档里没写的字段，服务端不会返回；需要新字段请按第 10 节的方式扩展。
10. 生成 UI 时区分四个独立状态：`running`、`syncing`、`paused`、`auto_sync`（见第 5 节，最容易做错的地方）。

---

## 1. 服务能提供什么

VodSync 聚合了上游多个「苹果 CMS（MacCMS）」资源站的影视数据，落到本地数据库（每源站一张表），对外提供统一查询接口：

| 能力 | 对应接口 |
|---|---|
| 源站清单（App 初始化） | `GET /source` |
| 分类清单 | `GET /api/v1/categories` |
| 视频列表 / 分类浏览 / 按 ID 批量详情 | `GET /api/v1/videos` |
| 单条视频详情（含播放地址） | `GET /api/v1/videos/{slug}/{vod_id}` |
| 跨源站聚合搜索 | `GET /api/v1/search` |
| 苹果 CMS 兼容接口（现成播放器直接填 URL） | `GET /api/v1/{slug}?ac=list\|detail` |
| 同步运维（状态、触发、暂停、自动同步开关、日志） | `/api/v1/stats`、`/api/v1/sources*`、`/api/v1/sync*`、`/api/v1/pause`、`/api/v1/logs` |

**数据规模参考**：单源站通常数千到数万条，全库可达百万级；`vod_time` 为更新时间，列表默认按它倒序。

---

## 2. 通用约定

### 2.1 鉴权（可选）

服务端若设置了环境变量 `API_KEY`，**所有接口**（含 `/healthz`）都需要携带密钥，二选一：

```http
X-API-Key: <你的KEY>
```
```
?apikey=<你的KEY>          # 适合不能自定义请求头的场景（<img>、播放器内核、iframe）
```

未设置 `API_KEY` 时无需鉴权。判断方式：

```bash
curl -i http://<host>:8000/healthz     # 200 → 未启用鉴权；401 → 已启用
```

鉴权失败：`401 {"detail":"无效的 API Key"}`。

> **安全提醒**：管理接口与数据接口共用同一把 Key，没有独立权限体系。App 面向公网时，管理接口应放到反代鉴权之后，不要把 Key 打进客户端安装包。

### 2.2 统一响应与错误

成功（HTTP 200）：

```json
{ "code": 1, "total": 6160, "list": [ ... ] }
```

失败（HTTP 4xx/5xx）：

```json
{ "detail": "源站 xxx 不存在" }
```

`422` 时 `detail` 是 FastAPI 标准校验数组：

```json
{ "detail": [ { "loc": ["query","wd"], "msg": "field required", "type": "value_error.missing" } ] }
```

| 状态码 | 含义 | 典型触发 |
|---|---|---|
| 200 | 成功 | — |
| 400 | 参数语义错误 | `ids` 非数字、`slug` 非法、搜索关键词为空 |
| 401 | 缺少/错误 API Key | 服务端已启用鉴权 |
| 404 | 资源不存在 | 源站不存在、视频未入库、源站尚未同步数据、ids 全部未命中 |
| 422 | 参数校验失败（类型/范围） | `page=0`、`page_size=1000`、`scope` 取值不在枚举内 |
| 500 | 服务端异常 | 数据库/上游异常 |

**404 的 `detail` 通常带补救建议**（比如提示改用搜索接口确认 `vod_id`），可直接展示给用户或用于日志。

### 2.3 分页

| 参数 | 类型 | 默认 | 范围 | 说明 |
|---|---|---|---|---|
| `page` | int | 1 | ≥1 | 页码，从 1 开始 |
| `page_size` | int | 20 | 1–100 | 每页条数，服务端可配置默认值与上限 |

**翻页终止条件**：`page > pagecount`，或本页 `list` 为空。不要依赖 `total` 做精确翻页（搜索接口的 `total` 是近似值，见 4.5）。

### 2.4 时间与编码

- 时间字段可能是 ISO 8601（`2026-10-30T16:09:54.229931`）或上游原样字符串（`2026-10-30 10:59:06`）。
- 两者都可按**字符串字典序**比较排序；展示时统一截取前 19 位即可。
- 中文查询参数必须 URL 编码（浏览器 `fetch`/`URLSearchParams` 自动处理）。

---

## 3. 数据模型（字段字典）

### 3.1 Source（源站）

| 字段 | 类型 | 说明 |
|---|---|---|
| `slug` | string | 源站标识，**所有 `source` 参数用它** |
| `name` | string | 展示名（中文） |
| `api` / `api_url` | string | 上游 MacCMS 采集接口地址（`/source` 用 `api`，`/api/v1/sources` 用 `api_url`） |
| `homepage` | string | 上游站点主页，可能为空串 |
| `status` | string | `ok` / `down`（上游健康状态） |
| `description` | string | 站点描述 |
| `enabled` | bool | 是否启用（目前恒为 true） |
| `total_videos` / `video_count` | int | 本服务已入库条数 |
| `watermark` | string | 增量同步水位线（最新 `vod_time`），客户端不需要 |
| `synced` | bool | 是否同步过（`/source` 专有） |

### 3.2 VideoSummary（列表条目）

| 字段 | 类型 | 说明 |
|---|---|---|
| `source` | string | 所属源站 slug |
| `vod_id` | int | 源站内唯一 ID，**详情请求必须带上 `source` + `vod_id`** |
| `type_id` | int | 分类 ID（**跨源站不唯一**） |
| `type_name` | string | 分类名（跨源站可用） |
| `vod_name` | string | 片名 |
| `vod_pic` | string | 封面图 URL |
| `vod_remarks` | string | 备注：清晰度 / 「更新至第 N 集」/「全 30 集」 |
| `vod_time` | string | 更新时间 |

### 3.3 VideoDetail（详情 = Summary + 以下字段）

| 字段 | 类型 | 说明 |
|---|---|---|
| `vod_content` | string | 简介（可能含 HTML 标签或为空） |
| `vod_play_url` | string | 上游原始播放串：`线路1$$$线路2`，线路内 `集名$地址#集名$地址` |
| `vod_play_from` | string | 固定为 `"vodsync"` |
| `vod_play_list` | PlayLine[] | **结构化播放列表（推荐使用）** |

```ts
type PlayLine = {
  name: string;                      // "线路1"（服务端按顺序编号，非上游原名）
  episodes: { name: string; url: string }[];  // name 如 "第01集"/"高清"
};
```

### 3.4 Category

| 字段 | 类型 | 说明 |
|---|---|---|
| `type_id` | int | 分类 ID（同名分类在不同源站可能是不同 ID） |
| `type_name` | string | 分类名 |
| `count` | int | 条数（多源合并后为合计） |
| `sources` | string[] | 该分类出现在哪些源站 |

> ⚠️ **跨源浏览请用 `type_name` 过滤**。`type_id` 只在**单源站**范围内可靠；多源合并时同一个 `type_id` 在不同表里含义不同。

### 3.5 SyncLog

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | int | 日志 ID（自增） |
| `slug` | string | 源站 |
| `mode` | string | `full` / `incremental` |
| `status` | string | `running` / `success` / `failed` / `paused` |
| `fetched` | int | 抓取条数 |
| `inserted` | int | 新增条数 |
| `updated` | int | 更新条数 |
| `pages` | int | 处理页数 |
| `error` | string | 错误信息（成功为空串） |
| `started_at` / `finished_at` | string | 开始 / 结束时间；未结束时 `finished_at` 为 `null` |

---

## 4. 接口参考

### 4.0 接口总表

| # | 方法 | 路径 | 分类 | 用途 |
|---|---|---|---|---|
| 1 | GET | `/healthz` | 基础 | 健康检查 / 探测是否需要 Key |
| 2 | GET | `/source` | 数据 | 源站清单（App 初始化） |
| 3 | GET | `/api/v1/categories` | 数据 | 分类清单 |
| 4 | GET | `/api/v1/videos` | 数据 | 列表 / 分类浏览 / 批量详情 |
| 5 | GET | `/api/v1/videos/{slug}/{vod_id}` | 数据 | 单条详情 |
| 6 | GET | `/api/v1/search` | 数据 | 跨源聚合搜索 |
| 7 | GET | `/api/v1/{slug}` | 数据 | MacCMS 兼容（`?ac=list\|detail`） |
| 8 | GET | `/api/v1/stats` | 管理 | 概览统计 |
| 9 | GET | `/api/v1/sources` | 管理 | 源站列表 + 同步状态 |
| 10 | GET | `/api/v1/sources/{slug}` | 管理 | 单源站详情 + 最近日志 |
| 11 | POST | `/api/v1/sources/refresh` | 管理 | 刷新源站列表 |
| 12 | POST | `/api/v1/sync/{slug}` | 管理 | 触发单源站同步 |
| 13 | POST | `/api/v1/sync-all` | 管理 | 触发全站同步 |
| 14 | GET | `/api/v1/pause` | 管理 | 查暂停状态 |
| 15 | POST | `/api/v1/pause` | 管理 | 暂停（三层级） |
| 16 | POST | `/api/v1/resume` | 管理 | 继续（三层级） |
| 17 | POST | `/api/v1/sources/{slug}/pause` | 管理 | 单源站暂停 |
| 18 | POST | `/api/v1/sources/{slug}/resume` | 管理 | 单源站继续 |
| 19 | POST | `/api/v1/sources/{slug}/auto-sync` | 管理 | 单源站自动同步开关 |
| 20 | POST | `/api/v1/sources/auto-sync` | 管理 | 批量自动同步开关 |
| 21 | GET | `/api/v1/logs` | 管理 | 最近同步日志 |
| 22 | POST | `/api/v1/maintenance/recount` | 管理 | 条数校准 |

---

### 4.1 GET `/healthz`

```json
{ "status": "ok", "time": "2026-10-30T16:09:53.527655" }
```

用途：服务可用性探测 + 判断是否启用 API Key（见 2.1）。

---

### 4.2 GET `/source` — 源站清单

App 启动时调用一次，作为源站选择页/初始化数据。

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `status` | string | 空 | 按上游状态过滤：`ok` / `down`；空=全部 |
| `only_ready` | bool | `false` | 仅返回本地已有数据的源站（推荐首页用） |

响应（按 `video_count` 倒序）：

```json
{
  "code": 1,
  "total": 3,
  "alive": 3,
  "ready": 3,
  "server_time": "2026-10-30T16:09:53.527655",
  "list": [{
    "name": "示例资源站",
    "slug": "jkun",
    "api": "https://example.com/api.php/provide/vod",
    "homepage": "https://example.com",
    "status": "ok",
    "description": "站点描述",
    "video_count": 6160,
    "synced": true,
    "last_sync_at": "2026-10-30T14:23:12.741188",
    "local_api": "/api/v1/jkun",
    "local_list_api": "/api/v1/videos?source=jkun"
  }]
}
```

- `total` 为返回条数；`alive` 为 `status=ok` 的数量；`ready` 为 `video_count > 0` 的数量。
- `local_api` / `local_list_api` 是相对路径，客户端拼上 Base URL 即可用。

---

### 4.3 GET `/api/v1/categories` — 分类清单

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `source` | string | 空 | 源站 slug，逗号分隔多源；空=全部源站合并 |

```json
{
  "code": 1,
  "total": 68,
  "list": [
    { "type_id": 1, "type_name": "电影", "count": 586, "sources": ["jkun","t1"] },
    { "type_id": 6, "type_name": "连续剧", "count": 872, "sources": ["jkun"] }
  ]
}
```

- 多源合并时按 `type_name` 合并计数，`type_id` 取其中一个（可能是 0）。
- 无数据时返回 `list: []`（不报错）。
- **失败**：`source` 含非法 slug → 400。

---

### 4.4 GET `/api/v1/videos` — 视频列表 / 批量详情 ★核心

四种用法：

```http
# ① 分页列表（轻量字段，无播放地址）
GET /api/v1/videos?source=jkun&page=1&page_size=20
GET /api/v1/videos?source=jkun,t1            # 多源合并
GET /api/v1/videos                           # 全部源站

# ② 按 ID 批量取详情（含播放地址，按请求顺序返回）
GET /api/v1/videos?source=jkun&ids=134369
GET /api/v1/videos?source=jkun&ids=1,2,3

# ③ 完整列表（每条都带播放地址，适合一次性渲染播放页）
GET /api/v1/videos?source=jkun&ac=detail&page=1

# ④ 分类浏览
GET /api/v1/videos?source=jkun&type_id=6&page=1
GET /api/v1/videos?source=jkun&type_name=电影
```

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `source` | string | 空 | 源站 slug，逗号分隔多源；空=全部 |
| `type_id` | int | 0 | 分类 ID（**单源**可靠） |
| `type_name` | string | 空 | 分类名（**跨源**用这个） |
| `wd` | string | 空 | 名称模糊搜索（单接口内） |
| `ac` | string | 空 | `detail` 或 `videolist` → 列表返回完整字段 |
| `ids` | string | 空 | 逗号分隔数字，批量取详情 |
| `page` | int | 1 | 页码 |
| `page_size` | int | 20 | 1–100 |

**① ④ 响应**：

```json
{
  "code": 1,
  "page": 1,
  "pagecount": 308,
  "total": 6160,
  "list": [{
    "source": "jkun",
    "vod_id": 134369,
    "type_id": 36,
    "type_name": "纪录片",
    "vod_name": "示例影片",
    "vod_pic": "https://example.com/cover.jpg",
    "vod_remarks": "高清",
    "vod_time": "2026-10-30 10:59:06"
  }]
}
```

**② ③ 响应**：结构同上，`list` 元素额外含 `vod_content`、`vod_play_url`、`vod_play_from`、`vod_play_list`：

```json
{
  "vod_content": "简介文本…",
  "vod_play_url": "高清$https://example.com/playlist.m3u8",
  "vod_play_from": "vodsync",
  "vod_play_list": [
    { "name": "线路1", "episodes": [
      { "name": "高清", "url": "https://example.com/playlist.m3u8" }
    ] }
  ]
}
```

**错误**：
- `ids` 无任何合法数字 → 400 `detail: "ids 参数格式错误，应为逗号分隔的数字，例如 ids=134369 或 ids=1,2,3"`
- `ids` 全部未命中 → 404（`detail` 会提示先用搜索接口确认 `vod_id`）
- 指定源站均无数据 → 404 `detail: "指定源站均无数据"`
- 无任何源站 → 404，提示先调用 `/api/v1/sources/refresh`

**注意**：
- `ac=detail` 会让列表每条都带完整播放表，页数固定、体积变大；首页封面墙请用普通列表模式。
- 未传 `source` 时是「全部源站 UNION 后排序分页」，源站多时会慢；客户端应尽量传 `source`。

---

### 4.5 GET `/api/v1/videos/{slug}/{vod_id}` — 单条详情

```http
GET /api/v1/videos/jkun/134369
```

```json
{ "code": 1, "detail": { "source": "jkun", "vod_id": 134369, "...": "同 3.3 全部字段" } }
```

**错误**：源站不存在 → 404；源站无数据 → 404；视频不存在 → 404 `{"detail":"视频不存在"}`。

---

### 4.6 GET `/api/v1/search` — 跨源站聚合搜索

```http
GET /api/v1/search?wd=三体
GET /api/v1/search?wd=三体&source=jkun,t1
GET /api/v1/search?wd=三体&type_name=电影&page=1&page_size=20
```

| 参数 | 类型 | 默认 | 范围 | 说明 |
|---|---|---|---|---|
| `wd` | string | — | 1–64 字 | **必填**，影视名称关键词 |
| `source` | string | 空 | — | 限定源站（更快）；空=全部 |
| `type_name` | string | 空 | — | 按分类名过滤 |
| `page` | int | 1 | ≥1 | 页码 |
| `page_size` | int | 20 | 1–100 | 每页条数 |
| `per_source_limit` | int | 200 | 10–1000 | 每个源站最多返回的候选数（一般不用动） |

```json
{
  "code": 1,
  "wd": "三体",
  "page": 1,
  "page_size": 20,
  "pagecount": 1,
  "total": 5,
  "total_is_partial": false,
  "truncated_sources": [],
  "sources_searched": 52,
  "sources_failed": [],
  "elapsed_ms": 76,
  "list": [{
    "source": "jkun",
    "source_name": "示例资源站",
    "vod_id": 123,
    "type_id": 6,
    "type_name": "连续剧",
    "vod_name": "三体",
    "vod_pic": "https://example.com/cover.jpg",
    "vod_remarks": "全30集",
    "vod_time": "2026-10-29 21:33:12"
  }]
}
```

**语义（客户端务必理解）**：
- `total` 是「各源站候选上限内的命中数」，**不是全库精确总数**；`total_is_partial=true` 表示有源站结果被截断（`truncated_sources` 列出）。
- 单个源站查询失败不影响整体：结果照常返回，失败源站列入 `sources_failed`（`[{slug, error}]`）。
- 结果按 `vod_time` 倒序。
- 翻页以 `pagecount` 为准；`elapsed_ms` 可用于性能埋点。
- **错误**：`wd` 为空 → 422（缺少参数）；无可搜索源站 → 404。

---

### 4.7 GET `/api/v1/{slug}` — 苹果 CMS 兼容接口

给现成播放器/采集软件直接填 URL 用，输出结构与 MacCMS 采集接口一致。

```http
GET /api/v1/jkun?ac=list&pg=1
GET /api/v1/jkun?ac=detail&pg=1
GET /api/v1/jkun?ac=detail&ids=1,2
GET /api/v1/jkun?ac=list&wd=三体&page=1&page_size=20
```

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `ac` | string | `detail` | `list`（轻量）/ `detail`（含播放地址） |
| `pg` | int | 1 | 页码（等价 `page`） |
| `ids` | string | 空 | 按 ID 批量取详情（仅 `ac=detail` 生效） |
| `wd` | string | 空 | 名称模糊搜索 |
| `page_size` | int | 20 | 1–100 |

```json
{
  "code": 1,
  "msg": "数据列表",
  "page": 1,
  "pagecount": 308,
  "limit": 20,
  "total": 6160,
  "class": [{ "type_id": 1, "type_name": "电影" }],
  "list": [ "…同 4.4 字段…" ]
}
```

- `class` 是该源站的分类表，可直接用于分类导航。
- `ac=detail&ids=` 时 `pagecount` 固定为 1，`total` 为命中条数。

---

### 4.8 GET `/api/v1/stats` — 概览统计

```json
{
  "code": 1,
  "sources_total": 52,
  "sources_enabled": 52,
  "sources_syncing": 0,        // 此刻真正在跑的任务数
  "sources_unfinished": 3,     // 全量同步有断点（未跑完）的站数
  "videos_total": 31920,
  "today_inserted": 4480,
  "today_updated": 880,
  "today_tasks": 14,
  "pause": { "global": false, "full": false, "incremental": false, "updated_at": {} },
  "proxy": {
    "enabled": true,
    "urls": ["https://pz.v88.qzz.io/", "https://mconfig.viptv.work/"],
    "preferred_hosts": { "bfzyapi.com": { "proxy": "https://pz.v88.qzz.io/", "age_seconds": 120.5 } },
    "direct_success": 128, "proxy_success": 46, "route_fail": 0
  },
  "last_error": { "slug": "t1", "error": "timeout", "at": "2026-10-30T14:00:00" },
  "server_time": "2026-10-30T16:09:54.229931"
}
```

- `last_error` 可能为 `null`。
- `pause` 结构同 `GET /api/v1/pause`（见 4.14）。

---

### 4.9 GET `/api/v1/sources` — 源站列表 + 同步状态

```json
{
  "code": 1,
  "list": [{
    "slug": "jkun",
    "name": "示例资源站",
    "api_url": "https://example.com/api.php/provide/vod",
    "status": "ok",
    "enabled": true,
    "total_videos": 6160,
    "watermark": "2026-10-30 12:00:00",
    "syncing": true,
    "running": false,
    "running_mode": null,
    "running_seconds": null,
    "synced": true,
    "progress": 1.8,
    "last_page": 120,
    "total_pages": 6710,
    "last_sync_at": "2026-10-30T13:00:30",
    "paused": false,
    "paused_at": null,
    "auto_sync": true
  }]
}
```

**四个状态字段的区别（决定 UI 怎么画）**：

| 字段 | 含义 | UI 建议 |
|---|---|---|
| `running` | **此刻**真的有同步任务在跑 | 显示进度条 +「暂停」按钮 |
| `syncing` | 全量同步历史上有断点、未跑完（**不代表正在跑**） | 显示「断点 N/M 页 · 未在同步」 |
| `synced` | 是否同步过（水位线 / 已入库 / 有断点，任一成立即 true） | `false` → 显示「未同步」，**不要显示「已完成」** |
| `paused` | 单源站被单独暂停（持久化） | 显示「本站暂停」徽标 +「继续」按钮 |
| `auto_sync` | 是否参与定时任务/全站同步（`false` 时不自动同步） | 显示「自动同步」开关状态 |

- `progress`：全量进度 0–100；**从未同步（`synced=false`）= 0**；已完成=100；刚开跑尚未取到总页数=0。
- `running_mode`：`full` / `incremental`；未运行为 `null`。
- `running_seconds`：本次已运行秒数；未运行为 `null`。

---

### 4.10 GET `/api/v1/sources/{slug}` — 单源站详情

```json
{
  "code": 1,
  "source": {
    "slug": "jkun", "name": "示例资源站", "api_url": "https://…",
    "status": "ok", "total_videos": 6160, "watermark": "2026-10-30 12:00:00",
    "last_page": 0, "running": false, "running_mode": null, "running_seconds": null,
    "paused": false, "paused_at": null, "auto_sync": true,
    "last_sync_at": "2026-10-30T13:00:30"
  },
  "recent_logs": [ { "id": 14, "mode": "full", "status": "success", "...": "同 3.5" } ]
}
```

`recent_logs` 为该源站最近 10 条日志。源站不存在或无数据 → 404。

---

### 4.11 POST `/api/v1/sources/refresh` — 刷新源站列表

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `full_sync` | bool | `false` | 是否对**新发现**的源站立即全量同步 |

```json
{ "code": 1, "new_sources": ["newsite1"], "full_sync_started": false }
```

同步执行（会拉取上游 `latest.json`，可能耗时数秒），异常时 500。

---

### 4.12 POST `/api/v1/sync/{slug}` — 触发单源站同步

| 参数 | 类型 | 默认 | 可选值 |
|---|---|---|---|
| `mode` | string | `auto` | `auto` / `full` / `incremental` |

- `auto`：有水位线走增量，无数据走全量（**推荐默认**）
- `full`：从第 1 页全量（支持断点续传）
- `incremental`：只拉更新

```json
{ "code": 1, "msg": "同步任务已受理: jkun mode=auto" }
```

异步受理；`slug` 不存在 → 404；正在被全局/单站暂停时任务直接返回 `status: "paused"` 并记日志。

---

### 4.13 POST `/api/v1/sync-all` — 触发全站同步

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `mode` | string | `auto` | 同上 |

```json
{ "code": 1, "msg": "全量同步任务已受理: mode=auto" }
```

只作用于**未暂停且 `auto_sync=true`** 的源站，服务端按并发上限逐站执行。

---

### 4.14 暂停 / 继续（三层级）

| 方法 | 路径 | 参数 |
|---|---|---|
| GET | `/api/v1/pause` | — |
| POST | `/api/v1/pause` | `scope`（必填，`global`/`full`/`incremental`）、`reason`（可选） |
| POST | `/api/v1/resume` | `scope`（必填，同上） |

```json
// GET /api/v1/pause
{ "code": 1, "global": false, "full": false, "incremental": false, "updated_at": {} }

// POST /api/v1/pause?scope=full
{ "code": 1, "msg": "同步暂停已生效: full（进行中的批次将在下一个检查点停止）",
  "state": { "global": false, "full": true, "incremental": false, "updated_at": {"full":"2026-10-30T16:20:00"} } }

// POST /api/v1/resume?scope=full
{ "code": 1, "msg": "同步已继续: full",
  "resumed_sources": ["jkun","t1"],
  "state": { "global": false, "full": false, "incremental": false, "updated_at": {} } }
```

**语义**：
- 暂停是**优雅停止**：当前抓取批次完成后保存断点再停，不丢数据；增量暂停时水位线不动。
- 继续会自动重新触发对应范围（`full` 只续跑有断点的站；`incremental` 对有水位线的站重跑；`global` 按 `auto` 续跑）。
- 暂停状态**持久化**，服务重启后仍生效。
- 暂停期间被触发（手动/定时）的任务直接返回 `status:"paused"`。
- 优先级：`global` 覆盖 `full`/`incremental`；单源站 `paused` 与层级暂停任一命中即停。

---

### 4.15 POST `/api/v1/sources/{slug}/pause` / `/resume` — 单源站暂停

```json
// pause
{ "code": 1, "msg": "源站 示例资源站 的同步已暂停，将在当前批次结束后停止并保留断点",
  "slug": "jkun", "paused": true, "was_running": true }

// resume（若上层仍有暂停，blocked_by 指示被阻断的层级）
{ "code": 1, "msg": "源站 示例资源站 的同步已继续", "slug": "jkun", "paused": false, "blocked_by": "" }
```

- `was_running=true` 表示确有任务在跑（此时提示"当前批次结束后停止"）；`false` 表示仅记录标记。
- `blocked_by` 的取值规则：全局暂停或「暂停全量」生效时返回 `"full"`；否则「暂停增量」生效时返回 `"incremental"`；都不生效为空串 `""`。App 可据此提示"需先解除上层暂停"。
- 单源站暂停只影响该站，不影响其他源站。

---

### 4.16 自动同步开关（`auto_sync`）

控制源站是否参与**自动同步**（定时增量任务、全站同步、增量续跑）。与「暂停」的区别：

| | 暂停 `paused` | 关闭自动同步 `auto_sync=false` |
|---|---|---|
| 定时任务 / 全站同步 | 跳过 | 跳过 |
| 手动单站同步 | **也被拦截** | **正常执行** |
| 语义 | 临时停掉这个站 | 这个站不参与批量/自动，需要时再手动跑 |

```http
POST /api/v1/sources/jkun/auto-sync?enabled=false         # 单站关闭
POST /api/v1/sources/jkun/auto-sync?enabled=true          # 单站开启
POST /api/v1/sources/auto-sync?enabled=false&slugs=all    # 全部关闭
POST /api/v1/sources/auto-sync?enabled=true&slugs=jkun,t1 # 指定多站开启
```

```json
// 单站
{ "code": 1, "msg": "源站 示例资源站 自动同步已暂停", "slug": "jkun", "auto_sync": false }
// 批量
{ "code": 1, "msg": "已暂停 52 个源站的自动同步", "updated": 52, "auto_sync": false }
```

- `enabled` **必填**（`true`/`false`，注意是布尔字面量，不是 1/0）。
- `slugs` 默认 `all`；也接受 `*`、空串（= 全部）或逗号分隔列表。
- 状态持久化在数据库，重启保留；`/api/v1/sources` 返回 `auto_sync`。
- 全局定时调度（默认每 6 小时增量、每 12 小时刷新源站列表）**始终运行**，只跳过被关闭的站。

---

### 4.17 GET `/api/v1/logs` — 最近同步日志

| 参数 | 类型 | 默认 | 范围 |
|---|---|---|---|
| `limit` | int | 50 | 1–200 |

```json
{ "code": 1, "list": [{
  "id": 14, "slug": "s361c088099", "mode": "full", "status": "paused",
  "fetched": 400, "inserted": 160, "updated": 240, "pages": 20,
  "error": "", "started_at": "2026-10-30T06:27:31", "finished_at": "2026-10-30T14:27:48.689783"
}]}
```

按 `id` 倒序（最新在前）。

---

### 4.18 POST `/api/v1/maintenance/recount` — 条数校准

```json
{ "code": 1, "msg": "条数校准任务已受理" }
```

异步重算各源站 `total_videos`（同步中断后面板条数可能滞后时使用），无请求体、无参数。

---

## 5. 状态语义与推荐 UI（最容易做错的部分）

### 5.1 单源站状态判定（按顺序）

```
1. running == true            → 正在跑：进度条（live）+「暂停」
2. paused  == true            → 已停：橙色「本站暂停」+「继续」
3. syncing == true            → 有断点未跑完：「断点 N/M 页 · 未在同步」+「全量」按钮
4. 其它                       → 「已完成」徽标
```

- **不要用 `syncing` 显示「暂停」按钮**：它只表示"全量还有断点"，服务重启后常见 `syncing=true && running=false`，此时点暂停没有意义。
- `auto_sync=false` 与上面四态**正交**：一个站可以是"已完成 + 自动同步关闭"。

### 5.2 暂停与自动同步的判定链

```
定时任务 / sync-all：
   global 暂停? → 跳过全部
   ↓ 否
   该站 paused? → 跳过该站
   ↓ 否
   该站 auto_sync=false? → 跳过该站
   ↓ 否
   执行同步
```

### 5.3 同步进度轮询

- 管理端建议 **5 秒**轮询 `GET /api/v1/sources` + `GET /api/v1/stats`；页面隐藏时暂停轮询。
- 触发同步后先等 1–2 秒再拉，避免拿到旧状态。
- 全量同步耗时可能数小时，进度看 `progress` / `last_page` / `total_pages`。

---

## 6. 端到端流程（可直接交给 AI 实现）

### 6.1 观影 App

```
启动
 ├─ GET /healthz                       → 可用性 + 是否需要 API Key（401 → 弹输入框）
 ├─ GET /source?only_ready=true        → 源站列表（缓存 10–30 分钟）
 │
首页「最近更新」
 └─ GET /api/v1/videos?page=1&page_size=20        → 封面墙
     （或指定主源：?source=jkun；封面 vod_pic、标题 vod_name、角标 vod_remarks）
 │
分类页
 ├─ GET /api/v1/categories?source=<当前源>         → 分类标签
 └─ GET /api/v1/videos?source=<当前源>&type_id=<id>&page=N
     ⚠️ 跨源浏览时改用 type_name=<分类名>；type_id 只在单源可靠
 │
搜索页
 └─ GET /api/v1/search?wd=<关键词>&page=1
     → 每条含 source + vod_id（点击后进详情）
 │
详情页
 ├─ GET /api/v1/videos/{source}/{vod_id}
 ├─ 线路选择器 = vod_play_list[i].name
 ├─ 选集列表   = vod_play_list[i].episodes
 └─ 播放        = episodes[j].url 交给播放器（多为 m3u8 直链）
```

**错误兜底**：详情 404（该条可能已下架/未入库）→ 提示"资源不可用"并可回退到搜索；`sources_failed` 非空说明部分源站超时，不影响已返回结果。

### 6.2 管理端 App（面板功能等价实现）

```
仪表盘
 ├─ GET /api/v1/stats                    → 4 张概览卡片
 └─ GET /api/v1/sources                  → 表格 + 进度条（5s 轮询）
操作
 ├─ POST /api/v1/sources/refresh         → 刷新源站
 ├─ POST /api/v1/sync/{slug}?mode=auto   → 单站增量
 ├─ POST /api/v1/sync/{slug}?mode=full   → 单站全量（需二次确认）
 ├─ POST /api/v1/sync-all?mode=auto      → 全站增量
 ├─ POST /api/v1/maintenance/recount     → 条数校准
暂停
 ├─ GET  /api/v1/pause                   → 三个开关状态
 ├─ POST /api/v1/pause?scope=global|full|incremental
 ├─ POST /api/v1/resume?scope=...
 ├─ POST /api/v1/sources/{slug}/pause     → 单站（仅 running 时展示）
 └─ POST /api/v1/sources/{slug}/resume
自动同步
 ├─ POST /api/v1/sources/{slug}/auto-sync?enabled=false   → 行内开关
 └─ POST /api/v1/sources/auto-sync?enabled=&slugs=all|列表 → 批量（多选）
日志
 └─ GET /api/v1/logs?limit=20
```

---

## 7. 客户端代码骨架

### 7.1 TypeScript / Web / React Native

```ts
const BASE = "http://192.168.1.10:8000";           // 由配置注入
const KEY = "";                                    // 服务端未启用鉴权时留空

type Params = Record<string, string | number | boolean | undefined>;

async function request<T>(path: string, params: Params = {}, init: RequestInit = {}): Promise<T> {
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== "") qs.set(k, String(v));
  }
  const url = `${BASE}${path}${qs.toString() ? `?${qs}` : ""}`;
  const headers: Record<string, string> = { ...(init.headers as any) };
  if (KEY) headers["X-API-Key"] = KEY;
  const res = await fetch(url, { ...init, headers });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(typeof body.detail === "string" ? body.detail : `HTTP ${res.status}`);
  }
  return res.json();
}

// 列表
const list = await request<{ page: number; pagecount: number; total: number; list: VideoSummary[] }>(
  "/api/v1/videos", { source: "jkun", page: 1, page_size: 20 }
);

// 批量详情（含播放地址）
const detail = await request<{ list: VideoDetail[] }>(
  "/api/v1/videos", { source: "jkun", ids: "134369,134370" }
);

// 单条详情
const one = await request<{ detail: VideoDetail }>("/api/v1/videos/jkun/134369");

// 管理：关闭两站自动同步
await request("/api/v1/sources/auto-sync", { enabled: false, slugs: "jkun,t1" }, { method: "POST" });
```

### 7.2 Kotlin / Android（Retrofit + Gson）

```kotlin
data class VideoSummary(
    val source: String, val vod_id: Long, val type_id: Int, val type_name: String,
    val vod_name: String, val vod_pic: String, val vod_remarks: String, val vod_time: String
)

data class Episode(val name: String, val url: String)
data class PlayLine(val name: String, val episodes: List<Episode>)
data class VideoDetail(
    val source: String, val vod_id: Long, val type_id: Int, val type_name: String,
    val vod_name: String, val vod_pic: String, val vod_remarks: String, val vod_time: String,
    val vod_content: String, val vod_play_url: String, val vod_play_from: String,
    val vod_play_list: List<PlayLine>
)

data class VideoListResponse(
    val code: Int, val page: Int, val pagecount: Int, val total: Int, val list: List<VideoSummary>
)
data class VideoDetailResponse(val code: Int, val detail: VideoDetail)
data class SearchResponse(
    val code: Int, val total: Int, val pagecount: Int, val list: List<SearchItem>
)

interface VodSyncApi {
    @GET("api/v1/videos")
    suspend fun videos(
        @Query("source") source: String? = null,
        @Query("type_name") typeName: String? = null,
        @Query("wd") wd: String? = null,
        @Query("page") page: Int = 1,
        @Query("page_size") pageSize: Int = 20
    ): VideoListResponse

    @GET("api/v1/videos/{slug}/{id}")
    suspend fun detail(@Path("slug") slug: String, @Path("id") id: Long): VideoDetailResponse

    @GET("api/v1/search")
    suspend fun search(@Query("wd") wd: String, @Query("page") page: Int = 1): SearchResponse
}

// OkHttp 拦截器统一加鉴权头
class AuthInterceptor(private val key: String) : Interceptor {
    override fun intercept(chain: Interceptor.Chain): Response =
        if (key.isEmpty()) chain.proceed(chain.request())
        else chain.proceed(chain.request().newBuilder().addHeader("X-API-Key", key).build())
}
```

### 7.3 Flutter（Dio）

```dart
final dio = Dio(BaseOptions(
  baseUrl: "http://192.168.1.10:8000",
  headers: {"X-API-Key": ""},   // 未启用鉴权时留空
  connectTimeout: const Duration(seconds: 10),
  receiveTimeout: const Duration(seconds: 30),
));

Future<List<dynamic>> recentVideos({String? source}) async {
  final r = await dio.get("/api/v1/videos", queryParameters: {
    if (source != null) "source": source,
    "page": 1,
    "page_size": 20,
  });
  return r.data["list"] as List<dynamic>;
}

Future<Map<String, dynamic>> videoDetail(String slug, int id) async {
  final r = await dio.get("/api/v1/videos/$slug/$id");
  return r.data["detail"] as Map<String, dynamic>;
}
```

### 7.4 Swift（URLSession）

```swift
struct VideoList: Decodable {
    let code: Int; let page: Int; let pagecount: Int; let total: Int
    let list: [Video]
}

func fetchVideos(source: String, page: Int = 1) async throws -> VideoList {
    var comps = URLComponents(string: "http://192.168.1.10:8000/api/v1/videos")!
    comps.queryItems = [
        .init(name: "source", value: source),
        .init(name: "page", value: "\(page)"),
        .init(name: "page_size", value: "20")
    ]
    var req = URLRequest(url: comps.url!)
    req.setValue("", forHTTPHeaderField: "X-API-Key")   // 未启用鉴权时不设置
    let (data, resp) = try await URLSession.shared.data(for: req)
    guard (resp as? HTTPURLResponse)?.statusCode == 200 else {
        throw URLError(.badServerResponse)
    }
    return try JSONDecoder().decode(VideoList.self, from: data)
}
```

---

## 8. 性能与缓存建议

| 数据 | 变化频率 | 建议 |
|---|---|---|
| `/source` | 低（每 12 小时刷新） | 本地缓存 10–30 分钟 |
| `/api/v1/categories` | 低 | 缓存 10–30 分钟 |
| 列表首页 / 分类页 | 中（随同步更新） | 内存缓存 1–5 分钟，下拉刷新 |
| 详情（含播放地址） | 低，但地址可能失效 | 短缓存 5 分钟；播放失败时强制刷新一次 |
| `/api/v1/stats`、`/api/v1/sources` | 高（管理端） | 不缓存，5 秒轮询 |

- 列表请求尽量带 `source`，避免全库 UNION 排序。
- 搜索先限定 `source` 或加 `type_name`，可显著降低延迟。
- 大图列表务必懒加载 + 占位图；封面域名可能有防盗链（见 9.7）。

---

## 9. 常见坑与排查表

| # | 现象 | 原因 | 处理 |
|---|---|---|---|
| 1 | 详情接口拿不到播放地址 | 用了列表模式（未加 `ids=` 或 `ac=detail`） | 用 `/api/v1/videos?source=x&ids=<id>` 或 `/api/v1/videos/{slug}/{id}` |
| 2 | 404「未找到 ids=… 的视频」 | 该 ID 未入库/源站不对 | 先 `/api/v1/search?wd=` 确认 `source`+`vod_id`；或触发该站同步 |
| 3 | 400「非法的源站标识」 | `source` 不是合法 slug | 从 `/source` 或 `/api/v1/sources` 取 `slug` 字段 |
| 4 | 400 但参数看着没错 | URL 里有第二个 `?`（`?a=1&?b=2`） | 只保留一个 `?`，其余用 `&` |
| 5 | 中文搜索返回 400/乱码 | 未 URL 编码 | 用 `URLSearchParams` / `encodeURIComponent` |
| 6 | 搜不到但库里明明有 | `wd` 是前缀无关的模糊匹配，但只匹配 `vod_name` | 换关键词；或走 `/api/v1/videos?source=x&wd=` 单源搜 |
| 7 | 图片不显示 | 上游防盗链（Referer 校验） | 请求图片时置空 Referer；H5 用 `<img referrerpolicy="no-referrer">` |
| 8 | 播放地址 403/404 | m3u8 直链有时效或需 Referer | 重新拉详情；播放器带与源站一致的 Referer/UA |
| 9 | 暂停按钮点了没反应 | 用 `syncing` 判断"正在跑" | 改用 `running`；仅 `running=true` 时展示暂停 |
| 10 | 某站不参与定时同步 | `auto_sync=false` 或 `paused=true` | 分别调用 auto-sync 开启 / resume 继续 |
| 11 | 搜索 `total` 与翻页对不上 | `total` 是候选上限内的近似值 | 以 `pagecount` 为准 |
| 12 | `page_size=500` 报 422 | 服务端上限 100，超限直接拒绝 | 改用 `page_size=100` 分页；上限可由部署方用 `API_PAGE_SIZE_MAX` 调整 |
| 13 | 422 且 `detail` 是数组 | FastAPI 参数校验失败 | 按 `detail[0].loc` 定位参数名 |
| 14 | 管理操作返回成功但没进度 | 异步受理 | 轮询 `/api/v1/sources` 的 `running` / `progress` |
| 15 | 某源站一直同步失败 | 直连被墙/被风控 | 服务端会自动切中转代理；查看 `/api/v1/stats` 的 `proxy` 段确认是否走代理 |
| 16 | 未同步的站显示「已完成」 | v1.4.0 前的进度口径 bug | 升级后按 `synced` 字段判断：`false` 一律显示「未同步」 |

---

## 10. 附录

### 10.1 机器可读规范

- OpenAPI 3.1：`GET /openapi.json`（本文档目录下也附了一份快照 `openapi.json`）
- Swagger UI：`GET /docs`
- 可直接把 `openapi.json` 喂给代码生成器（openapi-generator / Orval / Retrofit 生成器等），参数与响应模型与服务端实时一致。

### 10.2 环境变量（部署方参考，客户端不需要）

| 变量 | 默认 | 说明 |
|---|---|---|
| `API_KEY` | 空 | 设置后全站启用鉴权 |
| `API_PAGE_SIZE_DEFAULT` / `API_PAGE_SIZE_MAX` | 20 / 100 | 分页默认值与上限（客户端可从这里读取实际值） |
| `SYNC_INTERVAL_MINUTES` | 360 | 定时增量间隔（分钟） |
| `SOURCE_REFRESH_INTERVAL_MINUTES` | 720 | 源站列表刷新间隔（分钟） |
| `SOURCE_SYNC_CONCURRENCY` / `PAGE_CONCURRENCY` | 2 / 4 | 同步并发度 |
| `PROXY_ENABLED` / `PROXY_URLS` | true / 2 个内置代理 | 直连失败后自动走中转代理（`{proxy}?url=<urlencode(目标)>`） |
| `PROXY_PREF_TTL_MINUTES` | 30 | "该源站需走代理"的记忆时长 |
| `DATABASE_URL` | 本地 SQLite | 可切 PostgreSQL |

> 中转代理是**服务端行为**，对客户端完全透明：App 调用的接口路径、字段、状态码都不变，只是原本拿不到数据的源站现在能同步成功。

### 10.3 版本与字段稳定性

| 版本 | 主要变化 |
|---|---|
| v1.4.0（当前） | 服务端新增中转代理降级（源站不可达自动走代理）；新增 `synced` 字段，修正未同步站进度误显示 100 |
| v1.3.0 | 新增 `auto_sync` 字段与单站/批量自动同步开关接口 |
| v1.2.2 | 新增 `running` / `running_mode` / `running_seconds`，修正"暂停按钮误显示" |
| v1.2.1 | 新增单源站暂停/继续接口与 `paused` / `paused_at` |
| v1.2.0 | 新增三层级暂停（global/full/incremental） |
| v1.1.3 | 新增 `/source`、`/api/v1/categories`、`/api/v1/search` |

**兼容承诺**：只增字段、不改语义；删除或重命名字段会以大版本号变更。客户端解析 JSON 时应忽略未知字段（不要开启严格模式）。

---

*文档结束。若文档与 `/openapi.json` 不一致，以线上 OpenAPI 为准。*
