# 广告指纹库客户端契约（v1.17.0 / 给前端 AI 的升级说明）

> 适用对象：实现 m3u8 广告过滤的客户端（Android/iOS/前端播放器）。
> 本文是 **v1.16.x 去广告契约的增量补充**，老接口一字未改；只描述 v1.17.0 新增的字段与接口。
> 服务端版本：`1.17.0`；对应能力声明 `/healthz` 的 `capabilities` 仍为 `adfilter`（**没有新增能力位**，
> 老逻辑「没有 adfilter 就一次都别调」继续有效）。
> 配套接口契约源文件：仓库根目录 `openapi.json` 与 `docs/openapi.json`（已同步）。

---

## 0. 一句话理解指纹库

服务端把「哪些片段是广告」从**每个客户端各自跑本地启发式**升级为**共享知识**：
客户端把每次删掉的片段的**结构特征**（时长、大小、位置）随上报交给服务端，
服务端跨用户、跨地址聚合成指纹（fingerprint），确认可靠后**随规则包下发**。
新用户 / 新站点拉到规则包后可直接按指纹命中，不必等本地算法积累。

**边界不变（务必理解，避免前端做错方向）**：
- 服务端**不算分片内容哈希、不代理任何播放流量**；过滤仍在端上、分片仍直连 CDN。
- 指纹是「结构签名」：`host + 归一化时长桶 + 大小桶`，**不是**文件内容指纹。
- 客户端拿到的指纹只用于「本地检测到的片段特征与指纹匹配 → 直接判广告」，不上传任何 ts 字节。

---

## 1. 能力探测（无变化，重申）

```
GET /healthz
```

`capabilities` 数组**仍只含 `adfilter`**，没有 `adfilter_fp` 之类的新能力位。
判定规则保持原样：

| 条件 | 行为 |
|---|---|
| `capabilities` 含 `adfilter` | 可按本文使用全部新字段/新接口 |
| 不含 `adfilter` | **一次都不要调用**本文任何接口，也不要发送 `segments` / `fp_hash` 字段 |

服务端 `ADFILTER_ENABLED=false` 时，本文所有新接口同样返回 `404`，与老接口行为一致。

---

## 2. 接口总览

| # | 方法 | 路径 | 用途 | 鉴权 | 变更 |
|---|---|---|---|---|---|
| 1 | POST | `/api/v1/adfilter/report` | 上报命中明细，**新增可选 `segments[]`** | read Token 可用 | 字段追加（向后兼容） |
| 2 | GET | `/api/v1/adfilter/rules` | 拉规则包，**rules 内可能新增 `fingerprints[]`** | read Token 可用 | 字段追加（向后兼容） |
| 3 | POST | `/api/v1/adfilter/fp-feedback` | **新增**：回报某指纹命中 / 误杀 | read Token 可用 | 全新接口 |

> 管理端接口（`/api/v1/adfilter/admin/fingerprints*`）是给运营面板用的，需 admin Token，
> **客户端不要调用**，仅在第 9 节列出供对接排查参考。

成功响应统一遵循项目约定：`code == 0` 表示业务成功（值以配置 `API_SUCCESS_CODE` 为准，默认 0）。
但注意 **#1 / #2 的历史响应不带 `code` 包装**（老契约如此，保持不变）；#3 的响应也不带 `code` 包装。

---

## 3. 接口 1：上报明细扩展 `segments`（POST /api/v1/adfilter/report）

### 3.1 请求体

在原有 `items[]` 的每一项里，**新增可选字段 `segments`**：

```json
{
  "items": [
    {
      "url": "https://x.com/2026/abc/index.m3u8",
      "rule_version": 5,
      "removed_runs": 2,
      "removed_ms": 51600,
      "total_ms": 13379900,
      "at": 1762926745,
      "segments": [
        { "index": 12, "dur_ms": 10000, "size_b": 131072 },
        { "index": 13, "dur_ms": 15000, "size_b": 131072 }
      ]
    }
  ]
}
```

### 3.2 字段定义

| 字段 | 类型 | 必填 | 约束 | 说明 |
|---|---|---|---|---|
| `segments` | array | 否 | 单条上报最多 **50** 项（`ADFILTER_FP_MAX_SEGMENTS`） | 被删片段的特征列表；不传 = 老行为 |
| `segments[].index` | int | 是 | ≥ 0 | 片段在播放列表中的序号（从 0 开始） |
| `segments[].dur_ms` | int | 是 | > 0 | 该片段时长（毫秒） |
| `segments[].size_b` | int | 否 | ≥ 0，缺省 0 | 该片段字节数；**0 表示不知道**（此时不参与大小指纹） |

- 整批 `items` 仍 ≤ 50 条（老约束不变）。
- `index` / `dur_ms` / `size_b` 接受「整数值的浮点」（如 `1.0`），其它类型（bool、字符串、负数、非整数小数）视为非法。
- `segments` 数组超长时服务端**只取前 50 项**，并在响应 `reasons` 里记 `segments_segments_too_many`，**不整批拒绝**。

### 3.3 响应

在原有响应字段基础上**新增一个字段**（老字段不动）：

```json
{
  "accepted": 1,
  "dropped": 0,
  "duplicates": 0,
  "segments_accepted": 2
}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `accepted` | int | 真正入库的 report 条数（老字段，语义不变） |
| `dropped` | int | 数据不合规被丢的 report 条数（老字段） |
| `duplicates` | int | 重复上报条数（老字段） |
| `segments_accepted` | int | **新增**：真正入库的片段特征条数（去重后） |

> 老客户端不解析 `segments_accepted`，忽略即可。**关键**：只有当对应 report 真正入库成功
> （`accepted` 增加）时，其 `segments` 才会落库；重复上报（`duplicates`）不会产生重复片段。

### 3.4 状态码 / 错误码

与老接口完全一致，未新增任何错误分支：

| HTTP | 场景 |
|---|---|
| 200 | 正常（即使部分条目被丢弃，也是 200 + `dropped` 计数） |
| 400 | `items` 不是数组 / 单批 > 50 条（整批拒绝） |
| 401 | 未鉴权 |
| 404 | `ADFILTER_ENABLED=false` |
| 429 | 超频（默认 60 请求/小时/凭据），带 `Retry-After` 秒 |

**单条 `segments` 非法不会导致 400**——只丢该条 segments、report 仍入库，符合老接口「逐条丢弃」的稳定口径。

---

## 4. 接口 2：规则包新增 `fingerprints`（GET /api/v1/adfilter/rules）

### 4.1 四态语义完全不变

`changed` / `version` / `rules` 的判定与 v1.16.x 逐字一致（首次 / 已最新 / 本地更新 / 读不到降级）。
`changed=false` 时仍**不带 `rules`**。

### 4.2 `rules` 内新增字段

当服务端指纹库有 confirmed 指纹时，`rules` 会多出一个 `fingerprints` 数组：

```json
{
  "changed": true,
  "version": 6,
  "rules": {
    "thresholds": { "relative_max_ratio": 0.5, "absolute_max_sec": 300, "safety_floor": 0.8 },
    "url_patterns": ["/ad/", "/ads/"],
    "site_overrides": [],
    "fingerprints": [
      {
        "fp_hash": "f28bc267594d74b981e7f10cd9834877a43b3ecf611035e3a98a8354d8baca76",
        "host": "ad-example.com",
        "dur_bucket": 10,
        "size_bucket": 2,
        "dur_bucket_ms": 1000,
        "size_bucket_b": 65536,
        "confidence": 0.95
      }
    ]
  }
}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `fingerprints` | array | **可能不存在**；库里没有 confirmed 指纹时该字段不出现 |
| `fingerprints[].fp_hash` | string(64) | 指纹唯一 id，十六进制 sha256；反馈接口用它 |
| `fingerprints[].host` | string | 站点 host（小写） |
| `fingerprints[].dur_bucket` | int | 归一化时长桶序号 = `round(dur_ms / dur_bucket_ms)` |
| `fingerprints[].size_bucket` | int | 归一化大小桶序号 = `round(size_b / size_bucket_b)`，`0`=未按大小分桶 |
| `fingerprints[].dur_bucket_ms` | int | 时长桶宽（毫秒），当前默认 1000 |
| `fingerprints[].size_bucket_b` | int | 大小桶宽（字节），默认 65536；`0` 表示不按大小匹配 |
| `fingerprints[].confidence` | number | 0~1，置信度 |

下发条数上限默认 128（`ADFILTER_FP_MAX_DOWNLOAD`），按置信度降序取前 N。

### 4.3 客户端如何使用指纹（**这是前端要实现的匹配逻辑**）

1. 把 `fingerprints` 按 `host` 建索引（本地 Map）。
2. 对本地检测流程识别出的每个「候选广告片段」，计算它的 `dur_bucket = round(dur_ms / dur_bucket_ms)`，
   若 `size_bucket_b > 0` 再算 `size_bucket = round(size_b / size_bucket_b)`（拿不到大小就用 0）。
3. 同 host 下若存在 `fp_hash` 对应的指纹且 `dur_bucket`、`size_bucket` 均相等 → **直接判定为广告**，
   无需再走本地阈值启发式（这是指纹库加速新站点的核心收益）。
4. 命中成功后，调用接口 3 上报 `kind="hit"`（见第 5 节）。

> **不需要**客户端自己拼 fp_hash 字符串——下发里已给出 `fp_hash` 与桶值，客户端只做桶值比较即可。
> 桶宽字段随包下发，服务端改桶宽时客户端自动适配，**不要写死 1000/65536**。

---

## 5. 接口 3：指纹反馈（POST /api/v1/adfilter/fp-feedback）【新增】

### 5.1 请求

```json
{ "fp_hash": "f28bc267594d74b981e7f10cd9834877a43b3ecf611035e3a98a8354d8baca76", "kind": "hit" }
```

| 字段 | 类型 | 必填 | 约束 | 说明 |
|---|---|---|---|---|
| `fp_hash` | string | 是 | 非空，任意字符串（通常 64 位十六进制） | 从规则包 `fingerprints[].fp_hash` 原样带回 |
| `kind` | string | 是 | 只能是 `"hit"` 或 `"miss"` | hit=成功命中；miss=认为该指纹误杀 |

### 5.2 响应（不带 `code` 包装）

```json
{ "found": true, "fp_hash": "...", "status": "confirmed", "miss_count": 0, "downloadable": true }
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `found` | bool | 服务端是否存在该指纹；**未知指纹也返回 200**（客户端缓存了已撤销指纹属正常） |
| `fp_hash` | string | 回显 |
| `status` | string | `confirmed` / `candidate` / `disabled`；`found=false` 时无此字段 |
| `miss_count` | int | 累计误杀次数；`found=false` 时无此字段 |
| `downloadable` | bool | 该指纹当前是否仍会被下发 |

`found=false` 的响应形如：`{ "found": false, "fp_hash": "..." }`。

### 5.3 行为语义

| `kind` | 服务端动作 |
|---|---|
| `hit` | `run_hits += 1`（正向证据，不改变状态） |
| `miss` | `miss_count += 1`；累计达到阈值（默认 5）→ 指纹自动 `disabled`，**下次发布规则包后不再下发** |

> 重要：`miss` 的停用不是即时的——它下次规则包发布才对全体客户端生效。
> 客户端上报 miss 后，本会话仍按本地已持有的规则包工作；不要期待服务端立刻改变本地行为。

### 5.4 状态码 / 错误码

| HTTP | 场景 |
|---|---|
| 200 | 正常（含未知指纹 `found:false`） |
| 400 | `kind` 非法 / `fp_hash` 空 |
| 401 | 未鉴权 |
| 404 | `ADFILTER_ENABLED=false` |
| 429 | 超频（与上报接口共用同一限流域，默认 60/小时/凭据），带 `Retry-After` |

---

## 6. 鉴权要求

| 接口 | 最小权限 | 说明 |
|---|---|---|
| `POST /api/v1/adfilter/report` | read Token | 老规则不变 |
| `GET /api/v1/adfilter/rules` | read Token | 老规则不变 |
| `POST /api/v1/adfilter/fp-feedback` | **read Token** | 已在服务端加入 read-scope 白名单，与 report 同级；**不要用 admin Token 也能调** |
| 管理端 `.../admin/fingerprints*` | admin Token / 面板会话 | 客户端禁止调用 |

鉴权头沿用项目现有方式（`Authorization: Bearer <token>` 或面板会话 Cookie）。
`TOKEN_SCOPE_ENFORCE=true` 时，上述三个客户端接口对 read Token 均放行。

---

## 7. 字段约束与归一化规则（服务端口径，供前端对齐理解）

| 项 | 值 | 说明 |
|---|---|---|
| host 归一 | 小写、去端口、去 userinfo、去路径、去尾部点 | 与统计口径一致 |
| 时长桶宽 `dur_bucket_ms` | 默认 1000ms，可配 `100~∞` | 桶序号 = `round(dur_ms / 桶宽)` |
| 大小桶宽 `size_bucket_b` | 默认 65536B，`0`=关闭 | `size_b=0` 时大小桶恒为 0 |
| fp_hash | `sha256("{host}\|{dur_bucket}\|{size_bucket}")` | 仅服务端计算，客户端不重算 |
| 单条上报片段上限 | 50 | 超出截断并计数 |
| 下发指纹上限 | 128（可配 0~256） | 按 confidence、hit_count 降序 |
| 晋升 confirmed 门槛 | 去重用户≥3 且 去重地址≥3 且 命中≥10（均可配） | 三项同时满足 |
| 误杀自动停用阈值 | 累计 5 次（可配） | 人工审核优先于自动 |

这些阈值随服务端配置可变；客户端**只读下发里的桶宽字段**，不要硬编码上表数值（门槛是服务端内部逻辑，不暴露给客户端）。

---

## 8. 调用时序（推荐）

```
App 启动 / 起播
  │
  ├─(1) GET /healthz → 确认 capabilities 含 "adfilter"
  │
  ├─(2) GET /api/v1/adfilter/rules?version=<本地版本>
  │       ├─ changed=false → 用本地规则/指纹，跳过
  │       └─ changed=true  → 保存 rules（含 fingerprints[]），按 host 建指纹索引
  │
  ├─(3) 播放中，本地检测广告片段
  │       ├─ 片段特征命中指纹 → 直接判广告 → 过滤
  │       └─ 未命中 → 走本地阈值启发式判定
  │
  ├─(4) 攒批上报 POST /api/v1/adfilter/report
  │       └─ items[] 每条带 segments[]（删了哪些片段的特征）
  │
  └─(5) 命中/误杀反馈 POST /api/v1/adfilter/fp-feedback
          ├─ 按指纹成功过滤 → kind="hit"
          └─ 用户点「有广告」且该片曾按指纹过滤 → kind="miss"
```

**幂等与重试**：report 与 fp-feedback 都按 `(用户, 内容, 时间)` / 计数方式处理，失败重试安全；
重复上报不会重复计数。限流 429 时按 `Retry-After` 退避。

---

## 9. 兼容性影响（重点）

### 9.1 对老客户端（未升级）零影响

- `rules` 里新增 `fingerprints` 是**追加字段**，老客户端忽略即可；
- `report` 不发送 `segments` 时，服务端走完全相同的老路径（已测试验证：无 `segments` 时 `reasons` 为空）；
- 没有新能力位，`/healthz` 响应不变。

### 9.2 对新客户端的要求

- 必须能容灾处理 `rules.fingerprints` **不存在**（老服务端 / 空库）；
- 必须按 `fingerprints[].dur_bucket_ms` / `size_bucket_b` 动态算桶，不能写死；
- 反馈接口失败要静默降级（不影响播放过滤），不要把 HTTP 错误暴露给用户。

### 9.3 服务端升级/回滚

- 新表 `adfilter_segments` / `adfilter_fingerprints` 由 `init_db()` 自动建，拉镜像重启即可，无需手工 SQL；
- 回滚到 v1.16.x：老代码不读这两张表、不认识 `fingerprints` 字段（按「未知键静默丢弃」处理），
  规则包里的 `fingerprints` 会被老服务端发布新版本时自然丢弃，数据不丢、行为不坏。

---

## 10. 前端需要配合的升级点（Checklist）

- [ ] **能力探测**：沿用 `adfilter` 能力位即可，无需新增判断。
- [ ] **上报扩展**：`items[]` 每条在能拿到片段信息时带上 `segments[]`（`index`/`dur_ms`/`size_b`）。
      拿不到大小就省略 `size_b`，不要填 0 以外的占位。
- [ ] **规则解析**：`rules` 内新增可选 `fingerprints[]`，按 `host` 建索引；兼容字段不存在。
- [ ] **指纹匹配**：本地检测片段按 `dur_bucket_ms`/`size_bucket_b` 算桶，与同 host 指纹比对命中 → 直接判广告。
- [ ] **反馈上报**：命中调 `kind="hit"`；用户举报且该片曾按指纹过滤则调 `kind="miss"`，失败静默。
- [ ] **动态桶宽**：桶宽取自下发字段，禁止硬编码。
- [ ] **限流退避**：report 与 fp-feedback 共用 60/小时限流，429 按 `Retry-After` 处理。
- [ ] **隐私**：不要上传 ts 字节或 m3u8 原文以外的播放数据；`segments` 只有三个数字字段。

---

## 11. 管理端接口（仅参考，客户端不要调用）

均需 admin。返回体带 `code` 包装。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/adfilter/admin/fingerprints` | 分页列表；query: `status`(`candidate`/`confirmed`/`disabled`)、`host`、`fp_hash`(取单条)、`limit`(1~200)、`offset` |
| GET | `/api/v1/adfilter/admin/fingerprints/overview` | 概览：各状态数量、可下发数、累计命中/误杀、当前阈值配置 |
| POST | `/api/v1/adfilter/admin/fingerprints/rebuild` | 从 segments 全量重算（幂等） |
| POST | `/api/v1/adfilter/admin/fingerprints/review` | 审核：body `{fp_hash, status, note}` |
| POST | `/api/v1/adfilter/admin/fingerprints/publish` | 把 confirmed 指纹发布为新规则包版本：body `{channel?, note?}` |

> 运营在面板审核指纹后，必须点「发布指纹到规则包」（或调 publish 接口）生成新版本，客户端才会拿到。
> 这是刻意设计：任何对全体客户端生效的改动都走「版本 +1 + 人工确认」，与 `mode:off` 止血同一套治理模型。
