# VodSync API 说明文档（AI 客户端开发交付版）

> **本文档的用途**：把 VodSync 服务作为后端，交由 AI（或 AI 编程助手）开发客户端 App。
> 文档自包含：只读本文即可完成全部对接，**不需要**阅读服务端源码。
>
> 对应服务版本：**v1.10.1** ｜ 文档版本：`2026-11-07` ｜ 机器可读规范：[`openapi.json`](./openapi.json)

**目录**

| 章节 | 内容 |
|---|---|
| [0](#0-给-ai-的硬性约定先读这一节) | 给 AI 的硬性约定（先读） |
| [1](#1-服务能提供什么) | 服务能力概览 |
| [2](#2-通用约定) | 鉴权 / 响应格式 / 错误码 / 分页 / 编码 |
| [3](#3-数据模型字段字典) | 数据模型与字段字典 |
| [4](#4-接口参考) | 44 个接口逐一说明 |
| [5](#5-状态语义与推荐-ui最容易做错的部分) | 状态语义与推荐 UI（running / syncing / paused / auto_sync） |
| [6](#6-端到端流程可直接交给-ai-实现) | 端到端流程（观影 App / 管理 App） |
| [7](#7-客户端代码骨架) | 客户端代码骨架（TS / Kotlin / Flutter / Swift） |
| [8](#8-性能与缓存建议) | 性能与缓存建议 |
| [9](#9-常见坑与排查表) | 常见坑与排查表 |
| [10](#10-附录) | 附录：OpenAPI、环境变量、版本 |

---

## 0. 给 AI 的硬性约定（先读这一节）

生成代码前请把这些规则当作不可协商的约束：

1. **Base URL 由部署方提供**，形如 `http://192.168.1.10:8181`。`docker-compose.yml` 默认把宿主机 `8181` 映射到容器内 `8000`（`"8181:8000"`），所以**对外一律用宿主机端口**；本地直接 `uvicorn` 起服务时才是 `8000`。文档中所有路径都相对于该地址，**不要**自行拼接域名或加 `/api` 前缀。
2. 所有响应为 JSON，编码 UTF-8；业务成功标记为 `code: 1`。
3. 失败是 **HTTP 状态码 + `detail`**，不是 `code: 0`。客户端必须按状态码分支，不要只判断 `code`。
4. `source` 参数是**源站标识 slug**（如 `jkun`、`s361c088099`），**不是**站点名。slug 只能匹配 `^[a-z0-9_]{1,64}$`；中文站名在服务端被哈希成 `s` + 10 位十六进制。
5. 列表接口的分页参数统一为 `page`（从 1 开始）与 `page_size`（默认 30，上限 100，可用 `API_PAGE_SIZE_DEFAULT` / `API_PAGE_SIZE_MAX` 调整；`/api/v1/search` 与列表保持一致）。**超过上限会直接 422**，不是自动截断——客户端请按上限内取值，三列宫格推荐 30（10 行 × 3 列）。
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

### 2.1 鉴权（v1.5.0 起：登录密码 + API Token）

服务端默认启用访问控制。**客户端只需要一枚 API Token**：除了 `/healthz` 和 `/api/v1/auth/*`，
所有接口都必须带上它，三种带法任选其一：

```http
Authorization: Bearer vs_xxxxxxxxxxxxxxxxxxxxxxxxxxxx      # 推荐
```
```http
X-API-Key: vs_xxxxxxxxxxxxxxxxxxxxxxxxxxxx                 # 兼容旧客户端
```
```http
?token=vs_xxxxxxxxxxxxxxxxxxxxxxxxxxxx                     # 不能自定义请求头的场景（<img>、播放器内核）
```

**Token 从哪来**（部署方一次性给出，客户端存本地配置即可）：

1. 浏览器打开管理面板 → 右上「⚙ 设置」→ 复制「API Token」；
2. 或者用接口取（需要面板登录会话）：

```bash
curl -c ck.txt -X POST http://<host>:8181/api/v1/auth/login \
     -H 'Content-Type: application/json' -d '{"password":"666666"}'
curl -b ck.txt http://<host>:8181/api/v1/auth/token
# {"token":"vs_...","created_at":"2026-10-31T16:19:14"}
```

**判断服务端当前有没有开鉴权**（`/healthz` 永远 200，不能用它判断）：

```bash
curl -s http://<host>:8181/api/v1/auth/state
# 未登录：{"enabled":true,"logged_in":false}
# 已登录：{"enabled":true,"logged_in":true,"has_token":true,"token_created_at":"..."}
```

> **v1.6.1 变更**：该接口不再返回 `token_hint`，未登录时也不再返回 `has_token` / 时间戳。
> 它是免鉴权接口（登录页要用），只回最小状态；客户端只应依赖 `enabled`。

鉴权失败：`401 {"detail":"未授权：请求头需携带 Authorization: Bearer <token>..."}`。
面板里关闭访问控制后，`enabled=false`，此时不带 Token 也能调通。

> **安全提醒**：数据接口与管理接口共用同一枚 Token，没有分权。App 面向公网时，把写接口
> （触发同步 / 暂停 / 刷新源站）放在反向代理鉴权之后；Token 不要打进公开发布的安装包——它能被提取。

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
| 401 | 未授权：Token 缺失 / 错误 / 已被重置 | 服务端启用了访问控制（`/api/v1/auth/state` 查 `enabled`） |
| 404 | 资源不存在 | 源站不存在、视频未入库、源站尚未同步数据、ids 全部未命中 |
| 422 | 参数校验失败（类型/范围） | `page=0`、`page_size=1000`、`scope` 取值不在枚举内 |
| 503 | 服务临时过载（**可重试**） | 数据库连接池排队超时 / 数据库写锁忙。响应带 `Retry-After: 5` |
| 500 | 服务端异常 | 其它未预期异常 |

**404 的 `detail` 通常带补救建议**（比如提示改用搜索接口确认 `vod_id`），可直接展示给用户或用于日志。

**503 的处理约定（v1.4.1 起）**：这不是"接口坏了"，而是服务端同步负载过高时的背压信号。
客户端应读取 `Retry-After`（秒）后指数退避重试 1–3 次，期间可展示"服务繁忙，重试中"。


### 2.3 分页

| 参数 | 类型 | 默认 | 范围 | 说明 |
|---|---|---|---|---|
| `page` | int | 1 | ≥1 | 页码，从 1 开始 |
| `page_size` | int | 30 | 1–100 | 每页条数；默认值/上限由 `API_PAGE_SIZE_DEFAULT` / `API_PAGE_SIZE_MAX` 控制，超上限 422 |

**翻页终止条件**：`page > pagecount`，或本页 `list` 为空。不要依赖 `total` 做精确翻页（搜索接口的 `total` 是近似值，见 4.5）。

### 2.4 时间与编码

- 时间字段可能是 ISO 8601（`2026-10-30T16:09:54.229931`）或上游原样字符串（`2026-10-30 10:59:06`）。
- 两者都可按**字符串字典序**比较排序；展示时统一截取前 19 位即可。
- 中文查询参数必须 URL 编码（浏览器 `fetch`/`URLSearchParams` 自动处理）。

---

## 3. 数据模型与字段字典

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
| `status` | string | `running` / `success` / `incomplete`（部分完成，断点已保留）/ `interrupted`（被重启打断）/ `failed` / `paused` |
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
| 1 | GET | `/healthz` | 基础 | 健康检查（免鉴权，恒 200） |
| 2 | GET | `/source` | 数据 | 源站清单（App 初始化） |
| 3 | GET | `/api/v1/categories` | 数据 | 分类清单（v1.10.0 起带 `cached`） |
| 4 | GET | `/api/v1/videos` | 数据 | 列表 / 分类浏览 / 批量详情（详情模式带演职员与标签） |
| 5 | GET | `/api/v1/videos/{slug}/{vod_id}` | 数据 | 单条详情（v1.10.1 起带演职员与标签） |
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
| 23 | POST | `/api/v1/sources/probe` | 管理 | 测试 API 地址连通性（不落库、不建表） |
| 24 | POST | `/api/v1/sources/custom` | 管理 | **添加自定义源站**（latest.json 之外） |
| 25 | PATCH | `/api/v1/sources/{slug}/custom` | 管理 | 修改自定义源站（改 `api_url` 会清空该站数据） |
| 26 | DELETE | `/api/v1/sources/{slug}/custom` | 管理 | 删除自定义源站（需 `confirm=<slug>`） |
| 27 | POST | `/api/v1/auth/login` | 鉴权 | 面板登录（换会话 cookie，免鉴权） |
| 28 | POST | `/api/v1/auth/logout` | 鉴权 | 退出登录 |
| 29 | GET | `/api/v1/auth/state` | 鉴权 | 鉴权状态（免鉴权，未登录只回 `enabled`/`logged_in`） |
| 30 | POST | `/api/v1/auth/password` | 鉴权 | 修改面板密码 |
| 31 | POST | `/api/v1/auth/settings` | 鉴权 | 开关访问控制（开启时自动换 Token） |
| 32 | GET | `/api/v1/auth/token` | 鉴权 | 读取当前 Token（需登录） |
| 33 | POST | `/api/v1/auth/token` | 鉴权 | 重置 Token（旧 Token 立即失效） |
| 34 | GET | `/api/v1/related` | 数据 | **相关推荐**（预计算直读，P95<50ms） |
| 35 | GET | `/api/v1/related/status` | 管理 | 各源站推荐覆盖率 |
| 36 | GET/POST | `/api/v1/star-terms` | 管理 | 明星词库查询/批量添加（面板用） |
| 37 | PATCH/DELETE | `/api/v1/star-terms/{id}` | 管理 | 词条改名/启停/删除（面板用） |
| 38 | POST | `/api/v1/star-terms/recompute` | 管理 | 词库变更后全量重算推荐（面板用） |
| 39 | POST | `/api/v1/maintenance/related-recompute` | 管理 | 手动重算指定/全部源站推荐 |
| 40 | GET | `/api/v1/star/related` | 业务 | 明星作品推荐：与目标视频共享明星的其他视频 |
| 41 | POST | `/api/v1/maintenance/star-tags` | 管理 | 手动重建明星 Tag + star_map 映射 |
| 42 | GET | `/api/v1/star/tags/status` | 管理 | 明星 Tag 覆盖统计（视频数/缺演员数/已映射数） |
| 43 | GET | `/api/v1/star/works` | 业务 | 按明星查作品（明星为中心，total 不受 limit 影响） |
| 44 | GET | `/api/v1/star/tags/untracked` | 管理 | 未入词库高频标题词 Top N（扩词库用） |

> 共 **44 个接口**。标「管理」的主要供管理面板使用，客户端 App 只需一枚 Token（见 2.1）即可调用「数据」「业务」类接口。
> 鉴权类中 `/api/v1/auth/login` 与 `/api/v1/auth/state` **免鉴权**，其余需登录会话 cookie（`POST /api/v1/auth/password` 等也可用 Token 访问）。

---

### 4.1 GET `/healthz`

```json
{ "status": "ok", "db": true, "version": "1.10.1", "build": "4f249a7299d30827c1e785173d7d5a63a22191ca", "time": "2026-11-07T10:20:31.882914" }
```

用途：服务可用性探测 + 判断是否启用 API Key（见 2.1）。

`version` / `build`（v1.9.3 起，纯新增）：服务版本号与构建所用提交的完整 SHA
（本地源码直跑时 `build` 为 `dev`）。客户端可在「关于/诊断」页展示，便于反馈问题时定位版本；
运维升级或回滚后用它确认容器里跑的到底是哪一版。

**重要（v1.4.1 起）**：即使数据库繁忙，本接口**仍返回 200**，此时 `status` 为 `"degraded"`、`db` 为 `false`。
Docker/K8s 健康检查只看状态码，若这里因数据库排队返回 5xx，编排系统会把一个还在正常干活的容器判定为不健康并重启（v1.4.0 的真实故障）。
因此：

| 场景 | HTTP | `status` | `db` |
|---|---|---|---|
| 一切正常 | 200 | `ok` | `true` |
| 数据库繁忙/暂不可用 | 200 | `degraded` | `false` |

App 端不要把 `degraded` 当"服务已死"，应降级提示并退避重试；只有网络层失败（连不上）才视为服务不可用。

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
| `page_size` | int | 30 | 1–100 |

**① ④ 响应**：

```json
{
  "code": 1,
  "page": 1,
  "pagecount": 308,
  "total": 6160,
  "query_ms": 23,
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

> **v1.7.0 新增字段**（都可忽略，不影响解析）：
> - `query_ms`：服务端本次查询耗时（毫秒），可用于客户端埋点
> - `total_is_partial` + `sources_failed`：只在个别源站查询失败时出现，表示 `total` 偏小；
>   正常情况这两个字段不出现
> 列表排序恒为 `vod_time` 倒序、同时间按 `vod_id` 倒序，跨源站合并后仍然是这个顺序。

**② ③ 响应**：结构同上，`list` 元素额外含 `vod_content`、`vod_play_url`、`vod_play_from`、`vod_play_list`：

```json
{
  "vod_actor": "吴京,屈楚萧,李光洁",
  "vod_director": "郭帆",
  "vod_area": "中国大陆",
  "vod_year": "2019",
  "vod_tag": "吴京",
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

> **v1.10.1 新增 5 个字段**（纯新增，仅详情模式 `ids=` / `ac=detail` 下出现，普通列表模式**不带**以控制体积）：
>
> | 字段 | 类型 | 说明 |
> |---|---|---|
> | `vod_actor` | string | 演员，逗号分隔（源站原文，未归一化）；缺失为 `""` |
> | `vod_director` | string | 导演，逗号分隔；缺失为 `""` |
> | `vod_area` | string | 地区（如 `中国大陆`）；缺失为 `""` |
> | `vod_year` | string | 年代（4 位字符串，如 `2019`）；缺失为 `""` |
> | `vod_tag` | string | **明星标签**，逗号分隔（词库命中的主名，由服务端回填）；缺失为 `""` |
>
> 这 5 个键**永远存在**且一定是字符串（库里为 NULL 也返回 `""`），客户端不需要判 null / 判 key 存在。
> `vod_tag` 与 `/api/v1/star/related` 的 `stars` 同源，但它是**落库的静态值**（每晚轮转重建），
> `stars` 是**实时计算**结果 —— 两者不一致时以 `stars` 为准。

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
{
  "code": 1,
  "detail": {
    "source": "jkun", "vod_id": 134369,
    "type_id": 36, "type_name": "科幻片",
    "vod_name": "流浪地球", "vod_pic": "https://.../cover.jpg",
    "vod_remarks": "HD国语", "vod_time": "2026-10-30 10:59:06",
    "vod_actor": "吴京,屈楚萧,李光洁",
    "vod_director": "郭帆",
    "vod_area": "中国大陆",
    "vod_year": "2019",
    "vod_tag": "吴京",
    "vod_content": "简介文本…",
    "vod_play_url": "高清$https://example.com/playlist.m3u8",
    "vod_play_from": "vodsync",
    "vod_play_list": [ { "name": "线路1", "episodes": [ ... ] } ],
    "related": [ ... ], "related_fallback": false
  }
}
```

> **v1.10.1**：`vod_actor` / `vod_director` / `vod_area` / `vod_year` / `vod_tag` 五个字段开始透出
> （列 v1.9.0 起就在库里，之前只是没返回）。语义与「②③」一致：恒为字符串，缺失为 `""`。
> NAS 模式下客户端不再需要拿 `star/related` 的 `stars` 去拼演员，标签也有了数据源。

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
  "db_pool": { "size": 10, "checked_out": 0, "overflow": -10, "max_overflow": 20, "pool_timeout": 10 },
  "sync_load": { "running": 0, "queued": 0, "limit": 2 },
  "server_time": "2026-10-30T16:09:54.229931"
}
```

- `last_error` 可能为 `null`。
- `pause` 结构同 `GET /api/v1/pause`（见 4.14）。
- `sync_load`（v1.4.1）：服务端同步并发闸门的运行态 —— `running` 正在跑、`queued` 已排队等待名额、`limit` 上限（`SOURCE_SYNC_CONCURRENCY`）。管理端可据此提示"还有 N 个任务在排队"。
- `db_pool`（v1.4.1）：数据库连接池水位。`checked_out` 持续接近 `size + max_overflow` 表示负载过高，此时接口会返回 503（见 2.2）。

---

### 4.9 GET `/api/v1/sources` — 源站列表 + 同步状态

```json
{
  "code": 1,
  "list": [{
    "slug": "jkun",
    "name": "示例资源站",
    "api_url": "https://example.com/api.php/provide/vod",
    "origin": "latest",
    "status": "ok",
    "enabled": true,
    "total_videos": 6160,
    "watermark": "2026-10-30 12:00:00",
    "syncing": true,
    "running": false,
    "running_mode": null,
    "running_seconds": null,
    "queued": false,
    "synced": true,
    "progress": 1.8,
    "last_page": 120,
    "total_pages": 6710,
    "last_sync_at": "2026-10-30T13:00:30",
    "paused": false,
    "paused_at": null,
    "auto_sync": true,
    "fail_streak": 0,
    "skip_until": null
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
| `queued` | 已提交但还在等并发名额（v1.4.1） | 显示「排队中」，此时不要重复提交 |

- `progress`：全量进度 0–100；**从未同步（`synced=false`）= 0**；已完成=100；刚开跑尚未取到总页数=0。
- `running_mode`：`full` / `incremental`；未运行为 `null`。
- `running_seconds`：本次已运行秒数；未运行为 `null`。
- **并发上限**：服务端同一时刻最多跑 `SOURCE_SYNC_CONCURRENCY` 个源站同步（默认 2），超出的进入排队（`queued=true`）。这是正常的背压机制，不是故障。

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
{ "code": 1, "status": "accepted", "msg": "同步任务已受理: jkun mode=auto" }
```

同一源站已在排队/运行时再次提交，**不会重复跑**，而是返回：

```json
{ "code": 1, "status": "already_running", "msg": "jkun 已有同步任务在排队或执行中，本次未重复提交" }
```

异步受理；`slug` 不存在 → 404；正在被全局/单站暂停时，任务记为 `paused` 并写同步日志。

**并发与排队**：服务端同一时刻最多执行 `SOURCE_SYNC_CONCURRENCY` 个源站同步（默认 2），超出的排队等待（不占数据库连接）。
所以连续触发 10 个源站不会把服务打挂，只会排队；管理端应展示排队状态而不是重复点击。


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

### 4.19 自定义源站（v1.8.0）

原来所有源站都来自 `latest.json`，无法登记自己的接口。这组接口用于手动添加 / 修改 / 删除，
记录存在 `sources` 表并由 `origin='custom'` 标记。

**`origin` 字段语义**

| 值 | 含义 | `refresh_sources` 的行为 |
|---|---|---|
| `latest` | 从 latest.json 自动发现 | 更新名称 / 描述 / 状态 |
| `custom` | 面板手动添加 | **只更新 status，不改名称/描述，永不删除** |

因此「自定义源持续存在」不依赖任何配置文件或内存状态：它就是一行 `origin='custom'` 的记录，
重启、刷新源站列表、重新部署都还在。

#### POST `/api/v1/sources/probe` — 测试连通性（不落库）

```json
{ "api_url": "https://api.example.com/api.php/provide/vod/" }
```

响应**永远是 200**，用 `ok` 表示结果（探测失败是正常业务结果，不该让客户端 catch 异常）：

```json
{ "code": 1, "ok": true, "total": 1200, "pagecount": 24,
  "samples": ["示例影片A", "示例影片B"], "elapsed_ms": 312 }
```

```json
{ "code": 1, "ok": false, "error": "MacCmsError: 重试 1 次仍失败: ...", "elapsed_ms": 80 }
```

只抓 `ac=list&pg=1` 一页，不建表、不写库。`api_url` 只接受 `http://` / `https://`。

#### POST `/api/v1/sources/custom` — 添加

```json
{
  "name": "我的私有源",
  "api_url": "https://api.example.com/api.php/provide/vod/",
  "slug": "myhd",
  "homepage": "",
  "description": "",
  "probe": true,
  "allow_unreachable": false,
  "sync_now": false
}
```

| 字段 | 默认 | 说明 |
|---|---|---|
| `slug` | `""` | 留空由名称推导（纯中文名 → `s` + md5 前 10 位）。**决定表名 `vod_{slug}` 与 API 路径，添加后不可改** |
| `probe` | `true` | 添加前先抓一页验证 |
| `allow_unreachable` | `false` | 探测失败仍强制添加（源站临时维护时用） |
| `sync_now` | `false` | 添加成功后立即后台全量同步（响应里 `sync_started=true`） |

成功响应：

```json
{ "code": 1, "slug": "myhd", "probe": { "ok": true, "...": "..." },
  "sync_started": false, "msg": "已添加源站 myhd" }
```

错误：`409` = 该 `api_url` 已登记（含尾斜杠差异）或 `slug` 被占用；`400` = 地址非法 / 探测失败。

#### PATCH `/api/v1/sources/{slug}/custom` — 修改

只传需要改的字段：`name` / `api_url` / `homepage` / `description` / `enabled` / `auto_sync`。

```json
{ "name": "改名后的私有源", "auto_sync": false }
```

⚠️ **改 `api_url` 会清空该站已入库的数据**（响应 `data_reset: true`）并重置水位线与断点。
换了接口就是换了数据源，保留旧行只会让列表里混进「新源已经没有」的片子，且 `vod_id` 冲突时
会被 UPSERT 覆盖成新源内容，看起来像数据错乱。

只能改 `origin='custom'` 的源；官方源返回 `403`，不存在返回 `404`。

#### DELETE `/api/v1/sources/{slug}/custom` — 删除

```
DELETE /api/v1/sources/myhd/custom?confirm=myhd&drop_table=true
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `confirm` | — | 必须等于 `slug`，否则 `400`（防止误调用） |
| `drop_table` | `true` | 是否 DROP 数据表；`false` 只删登记记录、保留已入库数据 |

- 只能删 `origin='custom'` 的源，官方源返回 `403`（删了下次 refresh 也会回来，没有意义）
- 该源正在同步时返回 `409`，需先暂停或等其结束
- 删除后可以用同一个 `slug` 重新添加（内部会清理 SQLAlchemy 的 Table 缓存）

---

### 4.20 GET `/api/v1/related` — 相关推荐（v1.9.0）★详情页推荐栏

```
GET /api/v1/related?slug=jkun&id=134369
GET /api/v1/related?slug=jkun&id=134369&limit=8
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `slug` | 必填 | 源站 slug |
| `id` | 必填 | 视频 vod_id |
| `limit` | `12` | 推荐条数（1~`RELATED_LIMIT_MAX`=20） |

响应：

```json
{
  "code": 1, "slug": "jkun", "vod_id": 134369, "limit": 12,
  "fallback": false,
  "computed_at": "2026-11-06T04:30:12",
  "algo_version": "2",
  "list": [
    { "vod_id": 135001, "vod_name": "流浪地球2", "vod_pic": "https://...",
      "vod_remarks": "HD国语", "type_name": "科幻片",
      "source": "jkun", "source_name": "优质资源", "score": 0.69, "mode": "match",
      "reason": "title,class" }
  ]
}
```

要点：

- **直读预计算表 + 一次 IN 补卡片字段**，P95 < 50ms（实测 0.3ms）；预计算缺失时自动实时兜底
- `fallback=true` 表示**整条列表**来自兜底链（同分类→同地区/年代→最新收录），客户端可弱化展示；
  列表**永远非空**（只要视频本身存在）
- `mode`: `match`=真实得分命中 / `fallback`=兜底条目；`source`/`source_name` 供跨站跳转
- `algo_version`（v1.10.0 新增）：当前算法版本。缓存时建议带上它，版本变化即失效
- `reason`（v1.10.0 新增，卡片级）：命中原因，逗号分隔，取值 `title` / `actor` / `director` /
  `writer` / `class` / `area` / `year`，按贡献排序；兜底条目为空串。
  可直接做「因为你也看过…」副标题，忽略也不影响渲染
- **v1.10.0 语义收紧**（见 11 版本记录）：仅同分类/同地区/同年代的配对**不再进推荐位**
  （旧版给这类不相关配对 0.667 的高分）；零命中时兜底从 12 条降为 6 条，且不与真命中混排
- 视频不存在返回 `404` 标准错误结构（不 500）
- REST 详情与 `?ac=detail&ids=` 响应附带 `related` / `related_fallback` 字段
  （`RELATED_IN_DETAIL=true` 默认开启），客户端可省一次请求
- 服务端在同步结束 / 每日校准 / 算法版本变更时自动重算，客户端无需关心

---

### 4.21 明星词库（v1.9.0，管理面板专用）

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/v1/star-terms` | 词库清单 + `infer_mode`/`factor`/`active` 生效数 |
| POST | `/api/v1/star-terms` | 批量添加。`{"terms": "..."}` **格式自动识别**：JSON 数组 / 带引号片段 `"名字",` / 逗号、全角逗号、顿号、竖线、换行分隔 |
| PATCH | `/api/v1/star-terms/{id}` | 改名/备注/启停：`{"term": "...", "note": "...", "enabled": false}` |
| DELETE | `/api/v1/star-terms/{id}` | 删除词条 |
| POST | `/api/v1/star-terms/recompute` | 词库变更后对全部源站重算推荐（后台） |
| POST | `/api/v1/maintenance/related-recompute?slug=all` | 手动重算指定/全部源站推荐 |

推断规则（服务端自动执行）：词库主演名会扫描**缺失演员信息**视频的标题与简介——
标题命中且字段稀疏 → **强信号不打折**；仅简介命中 → 弱信号 ×`RELATED_STAR_SIM_FACTOR`(0.7)。
所有算法参数（权重表 `RELATED_WEIGHTS`、阈值、Top N 等）均为环境变量，见 10.2。

---

### 4.22 明星作品推荐（v1.9.1）

**GET `/api/v1/star/related?slug=<源站>&vod_id=<id>` 或 `&name=<名称>`&limit=20`**

与目标视频**共享明星**（词库 tag 交集）的其他视频。目标视频可用 `vod_id` 或 `name`
（先精确后模糊）定位；返回：

```json
{
  "code": 1,
  "target": { "vod_id": 102, "vod_name": "战狼2" },
  "stars": ["吴京"],
  "star_counts": { "吴京": 42 },
  "truncated": false,
  "list": [
    { "vod_id": 101, "vod_name": "流浪地球", "vod_pic": "https://...",
      "vod_remarks": "HD国语", "type_name": "科幻片", "source": "jkun",
      "match_stars": ["吴京"], "overlap": 1 }
  ]
}
```

要点：

- 目标视频的明星集来自 `star_map` 映射表（真实演员 ∩ 词库 `src=actor` +
  标题命中推断 `src=title`）；映射缺失时自动从 `vod_tag`/`vod_actor` 现算兜底
- 排序：`overlap` 重叠明星数降序，同分按更新时间降序；已下架视频自动跳过
- 查询走 `star_map` 的 `(slug, term)` 索引，毫秒级
- **v1.9.2 新增字段（纯新增，向后兼容）**：`star_counts` = `{明星名: 本源站作品数}`
  （降序，Tab 角标用）；`truncated` = 候选总数超 `limit` 时为 true（v1.10.0 起精确判定，命中不足时为 `false`，客户端「加载更多」不会再空转）。`stars` 仍是 `string[]`
- `name` 路径健壮性（v1.9.2）：前后/中间空格、全半角差异、LIKE 通配符转义；
  确实找不到保持 404（客户端主走 `vod_id`）
- 无明星 → `stars` 与 `list` 为空数组；目标视频不存在 → `404` 标准错误
- Tag 自动维护：全量同步后整站重建、增量同步重算 touched、每晚小站优先重建；
  词库增删改后点面板「重算」（或 `POST /api/v1/star-terms/recompute`）生效
- 管理：`POST /api/v1/maintenance/star-tags?slug=all` 手动重建；
  `GET /api/v1/star/tags/status` 查看覆盖（videos / no_actor_videos / star_mapped）

---

### 4.23 按明星查作品（v1.9.2）

**GET `/api/v1/star/works?slug=<源站>&term=<明星名>&limit=30&exclude_id=0`**

| 参数 | 说明 |
|---|---|
| `slug` | 必填，源站 slug |
| `term` | 必填，明星名（主名或别名，服务端归一化；响应 `term` 返回主名） |
| `limit` | 默认 30，上限 100 |
| `exclude_id` | 可选，排除当前视频 |

```json
{ "code": 1, "slug": "jkun", "term": "吴京", "total": 42,
  "list": [{ "vod_id": 135001, "vod_name": "战狼2", "vod_pic": "https://...",
             "vod_remarks": "HD国语", "type_name": "动作片", "source": "jkun",
             "match_stars": ["吴京"], "overlap": 1 }] }
```

要点：

- **明星为中心**：不做「重叠明星数」全局排序截断，命中多位明星的目标视频里，
  作品少的明星也不会被挤出（客户端按明星切 Tab 的数据来源）
- 排序 `vod_time desc, vod_id desc`；`total` 为该明星现存作品总数
  （不受 `limit` 影响、不含 `exclude_id`），客户端据此展示「共 N 部」与翻页
- `term` 无映射 / 作品已下架 → **200 + `total:0` + `list:[]`**（不 404）
- 走 `idx_star_map_term` 索引 + join 回表（排序需 `vod_time`），P95 < 50ms

**GET `/api/v1/star/tags/untracked?slug=&limit=100&sample=20000`**（扩词库用）

抽样「缺演员」视频标题 → 用词库正则把已命中片段替换为占位符 → 剩余中文串做
2~4 元 n-gram 频次统计 → 过滤停用词/低频词、长词优先，返回
`{sample_titles, candidates:[{term, count}]}`。结果 10 分钟缓存；
**是候选不是结论**，请人工确认后加入词库（自动入库会把噪声写进词库）。

**词库别名（v1.9.2）**：`star_terms.aliases` 逗号分隔，`PATCH /api/v1/star-terms/{id}`
的 `{"aliases": "京哥,Wu Jing"}` 可维护。别名参与匹配（标题扫描 + 演员交集），
命中后统一归一到主名 —— `star_map.term` / `vod_tag` 只存主名，避免同一演员
多译名把作品拆散。别名变更后同样需 `POST /api/v1/star-terms/recompute` 生效。

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
const TOKEN = "vs_xxxxxxxxxxxxxxxxxxxxxxxxxxxx";   // 面板「⚙ 设置」里复制；关闭鉴权时可为空

type Params = Record<string, string | number | boolean | undefined>;

async function request<T>(path: string, params: Params = {}, init: RequestInit = {}): Promise<T> {
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== "") qs.set(k, String(v));
  }
  const url = `${BASE}${path}${qs.toString() ? `?${qs}` : ""}`;
  const headers: Record<string, string> = { ...(init.headers as any) };
  if (TOKEN) headers["Authorization"] = `Bearer ${TOKEN}`;
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

// OkHttp 拦截器统一加鉴权头（v1.5.0 起用 Bearer Token）
class AuthInterceptor(private val token: String) : Interceptor {
    override fun intercept(chain: Interceptor.Chain): Response =
        if (token.isEmpty()) chain.proceed(chain.request())
        else chain.proceed(
            chain.request().newBuilder().addHeader("Authorization", "Bearer $token").build()
        )
}
```

### 7.3 Flutter（Dio）

```dart
final dio = Dio(BaseOptions(
  baseUrl: "http://192.168.1.10:8000",
  headers: {"Authorization": "Bearer <你的Token>"},   // 关闭鉴权时可留空
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
    req.setValue("Bearer <你的Token>", forHTTPHeaderField: "Authorization")
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
| 17 | 接口返回 **503**（带 `Retry-After`） | 服务端连接池/写锁繁忙（同步负载高） | **不是接口坏了**，按 `Retry-After` 退避重试；`/api/v1/stats` 的 `sync_load`、`db_pool` 可看压力 |
| 18 | `/healthz` 返回 200 但 `status=degraded` | 数据库暂不可用（v1.4.1 行为，避免容器被误判不健康） | 按 `db=false` 提示"服务繁忙"，不要当服务已死 |
| 19 | 提交同步后该站显示「排队中」 | 达到全局并发上限（`SOURCE_SYNC_CONCURRENCY`） | 正常排队，等待即可；不要重复提交（会返回 `already_running`） |

---

## 10. 附录

### 10.1 机器可读规范

- OpenAPI 3.1：`GET /openapi.json`（本文档目录下也附了一份快照 `openapi.json`）
- Swagger UI：`GET /docs`
- 可直接把 `openapi.json` 喂给代码生成器（openapi-generator / Orval / Retrofit 生成器等），参数与响应模型与服务端实时一致。

### 10.2 环境变量（部署方参考，客户端不需要）

| 变量 | 默认 | 说明 |
|---|---|---|
| `AUTH_ENABLED` | true | 首次启动写入数据库的初始开关（之后以面板设置为准） |
| `ADMIN_PASSWORD` | 666666 | 首次启动的管理面板密码（登录后请改掉） |
| `SESSION_TTL_DAYS` | 7 | 面板登录会话有效期（天） |
| `LOGIN_MAX_FAILS` / `LOGIN_LOCK_MINUTES` | 5 / 5 | 连续登录失败 N 次锁 M 分钟 |
| `API_KEY` | 空 | 兼容项：设置后作为一枚额外有效的 Token |
| `API_PAGE_SIZE_DEFAULT` / `API_PAGE_SIZE_MAX` | 30 / 100 | 分页默认值与上限（客户端可从这里读取实际值） |
| `SYNC_INTERVAL_MINUTES` | 360 | 定时增量间隔（分钟） |
| `SOURCE_REFRESH_INTERVAL_MINUTES` | 720 | 源站列表刷新间隔（分钟） |
| `SOURCE_SYNC_CONCURRENCY` / `PAGE_CONCURRENCY` | 2 / 4 | 同步并发度（前者是全局并发闸门，超出排队） |
| `DB_POOL_SIZE` / `DB_MAX_OVERFLOW` / `DB_POOL_TIMEOUT` | 10 / 20 / 10 | 连接池大小与等连接超时（超时接口返回 503） |
| `SEARCH_CONCURRENCY` | 4 | 聚合搜索并行度 |
| `PROXY_ENABLED` / `PROXY_URLS` | true / 2 个内置代理 | 直连失败后自动走中转代理（`{proxy}?url=<urlencode(目标)>`） |
| `PROXY_PREF_TTL_MINUTES` | 30 | "该源站需走代理"的记忆时长 |
| `MERGE_MAX_PER_TABLE` / `MERGE_CONCURRENCY` | 3000 / 4 | 多源列表归并：每表最多取多少行、并行度 |
| `TOTAL_APPROXIMATE_ENABLED` | true | v1.10.0：无筛选列表的 `total` 用 `SUM(sources.total_videos)` 估值（省 52 次 count），响应带 `total_approximate` |
| `CATEGORIES_CACHE_SECONDS` | 300 | v1.10.0：`/api/v1/categories` 结果缓存秒数 |
| `PG_TRGM_ENABLED` | true | v1.10.0：启动后台建 `pg_trgm` GIN 索引（`CREATE INDEX CONCURRENTLY`，不锁表；无权限仅记日志） |
| `RELATED_ALGO_VERSION` | 2 | v1.10.0 起默认 2（证据先验 + 内容信号门控 + 演员余弦）；改动即触发清旧结果并全量重算 |
| `RELATED_EVIDENCE_PRIOR` / `RELATED_EVIDENCE_NEUTRAL` | 0.6 / 0.4 | v1.10.0：证据先验强度与中性分（解决稀疏字段得分通胀） |
| `RELATED_REQUIRE_CONTENT_SIGNAL` | true | v1.10.0：仅标题/演员/导演/编剧命中才算相似，纯分类+地区+年代不进推荐位 |
| `RELATED_CANDIDATE_CAP` / `RELATED_BRUTE_FORCE_MAX` | 140 / 400 | v1.10.0：倒排候选上限、小表全量比较阈值（旧值 600/3000 是性能悬崖） |
| `RELATED_FALLBACK_LIMIT` | 6 | v1.10.0：零命中时兜底条数上限（原为 12，与真命中混排） |
| `RELATED_RECOMPUTE_CONCURRENCY` | 2 | v1.10.0：全局最多几个源站同时整站重算（防内存叠加） |
| `RELATED_INCREMENT_DEBOUNCE_SECONDS` / `RELATED_INCREMENT_FULL_RATIO` | 60 / 0.3 | v1.10.0：增量重算合并窗口；窗口内累计 id 超整站比例则直接整站重算 |
| `STAR_TAG_CLEAR_STALE` | true | v1.10.0：缺演员的行按当前词库重算 tag（不命中则清空，修掉旧推断残留） |
| `DATABASE_URL` | `postgresql+psycopg2://vodsync:vodsync@postgres:5432/vodsync` | PostgreSQL（v1.7.0 起固定） |

> 中转代理是**服务端行为**，对客户端完全透明：App 调用的接口路径、字段、状态码都不变，只是原本拿不到数据的源站现在能同步成功。

### 10.3 版本与字段稳定性

| 版本 | 主要变化 |
|---|---|
| v1.10.1（当前） | **详情字段透出**：`/api/v1/videos/{slug}/{id}`、`?ids=` 批量详情、`?ac=detail` 列表新增 `vod_actor` / `vod_director` / `vod_area` / `vod_year` / `vod_tag` 五个字段（恒为字符串，缺失为 `""`；普通列表模式不带）。纯新增，旧客户端无影响 |
| v1.10.0 | **算法与性能升级（无破坏性字段变更）**：`/api/v1/related` 新增 `algo_version` 与卡片级 `reason`（命中原因）；仅同分类/同地区/同年代的配对不再进推荐位、兜底条数降为 6 条且不与真命中混排；`/api/v1/videos` 无筛选时 `total` 改为估值并带 `total_approximate`；`/api/v1/categories` 新增 `cached`；`/api/v1/star/related` 的 `truncated` 改精确判定；`/api/v1/star-terms` 的 `active` 改为进程内生效数。详见 `UPGRADE_2026-11-07_v1.10.x.md` |
| v1.9.3 | `GET /healthz` 新增 `version` / `build`（镜像构建提交 SHA），便于升级/回滚核对与报障定位；无新增/变更业务接口，**客户端无需改动** |
| v1.9.2 | **按明星查作品**：`GET /api/v1/star/works`（明星为中心，`total` 不受 limit 影响，无映射返回 200+空列表）；`star/related` 新增 `star_counts`（明星→作品数，Tab 角标）与 `truncated`（截断标志），`stars` 结构不变；`name` 匹配健壮性（空格/全半角/LIKE 转义）；词库 `aliases` 别名归一主名；`star/tags/untracked` 未入词库高频词统计 |
| v1.9.1 | **明星作品推荐**：词库明星名回填缺失演员视频的 `vod_tag`（单正则一次扫描，O(标题长) 与词库大小解耦）；新表 `star_map` 明星-视频倒排映射（`src=actor/title` 可信度标记）；`GET /api/v1/star/related` 共享明星视频（走索引毫秒级）；`maintenance/star-tags` 手动重建、`star/tags/status` 覆盖统计；UPSERT 空 `vod_tag` 不覆盖本地推断值；同步后自动维护 + 每晚小站优先重建 |
| v1.9.0 | **相关推荐（服务端预计算）**：`GET /api/v1/related`（预计算直读 P95<50ms，fallback 兜底列表非空）；REST 详情与 `ac=detail` 附带 `related` 数组（`RELATED_IN_DETAIL` 可关）；`vod_{slug}` 新增 `vod_actor/vod_director/vod_writer/vod_area/vod_year/vod_tag` 列（存量库自动补列）；详情卡片字段不变、只增；新增明星词库管理接口与 `related/status`、`maintenance/related-recompute` |
| v1.8.1 | 源站健康度退避：`/api/v1/sources` 新增 `fail_streak` / `skip_until`；大面积失败（页失败率≥50% 或整轮异常）按 60min→2h→4h→…（封顶 24h）指数退避，调度器跳过未到期的站（手动触发不受限），成功即清零；增量索引失败页在本次运行内重试（`PAGE_MAX_ATTEMPTS` 轮）；连续 2 批整批失败早停；日志行带失败原因分布（如 `timeout×1900, http_403×778`） |
| v1.8.0 | 新增自定义源站：`POST /api/v1/sources/custom`、`PATCH/DELETE /api/v1/sources/{slug}/custom`、`POST /api/v1/sources/probe`；`/api/v1/sources` 增加 `origin` 字段（`latest` / `custom`） |
| v1.4.1 | 服务端稳定性修复：同步并发闸门（超出排队）、同源站防重入、连接池可配、`/healthz` 池满时仍 200 + `degraded`、过载统一 503；新增 `queued`、`db_pool`、`sync_load` 字段 |
| v1.4.0 | 服务端新增中转代理降级（源站不可达自动走代理）；新增 `synced` 字段，修正未同步站进度误显示 100 |
| v1.3.0 | 新增 `auto_sync` 字段与单站/批量自动同步开关接口 |
| v1.2.2 | 新增 `running` / `running_mode` / `running_seconds`，修正"暂停按钮误显示" |
| v1.2.1 | 新增单源站暂停/继续接口与 `paused` / `paused_at` |
| v1.2.0 | 新增三层级暂停（global/full/incremental） |
| v1.1.3 | 新增 `/source`、`/api/v1/categories`、`/api/v1/search` |

**兼容承诺**：只增字段、不改语义；删除或重命名字段会以大版本号变更。客户端解析 JSON 时应忽略未知字段（不要开启严格模式）。

---

*文档结束。若文档与 `/openapi.json` 不一致，以线上 OpenAPI 为准。*
