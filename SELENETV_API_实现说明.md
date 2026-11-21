# vodsync 之上新增 SeleneTV 专用 API —— 实现说明

> 目标：在 **vodsync 现有功能完全不变** 的前提下，额外挂载一套 Helios 风格 API 供 SeleneTV 客户端直连。
> 数据库只 **增表、不删表、不删字段、不改现有表**。
> 实现基于 `Helios_API分析与SeleneTV_API实现方案.md`，并已用 Helios 与 vodsync v1.20.0 真实源码逐字核对契约。

---

## 一、任务清单与完成状态

| # | 任务 | 状态 | 关键产物 |
|---|---|---|---|
| 13 | 克隆并分析 vodsync 源码结构 | ✅ | 本地 `vodsync/`（v1.20.0），确认接入点/可复用工具 |
| 14 | 设计只增不改的接入方案 | ✅ | 独立包 + 独立 MetaData + 开关 + 精确鉴权放行 |
| 15 | 实现新增表迁移脚本 | ✅ | `app/selenetv/schema.py`、`migrate.py`（幂等） |
| 16 | 实现 Helios 风格 API 模块 | ✅ | `security.py`/`serializers.py`/`repository.py`/`router.py` |
| 17 | 接线挂载与本地验证 | ✅ | `main.py` 三处增量；`scripts/test_selenetv_api.py` 64/64 通过 |

另：克隆 Helios 源码逐字核对了 10 条 API 的 json tag、错误文本、SSE 驼峰字段与 Cookie 编码。

---

## 二、对现有功能的影响（核心承诺）

### 2.1 改动的文件（仅 2 个现有文件，均为纯增量）

| 文件 | 改动 | 行数 |
|---|---|---|
| `app/main.py` | ① 挂载 router（开关控制）② `_auth_guard` 精确放行 10 条新路径 ③ startup 幂等建表钩子 | +30 |
| `.env.example` | 追加 `SELENETV_API_ENABLED` 说明 | +11 |

**未改动**任何现有 handler、模型、表定义、鉴权逻辑、调度器、面板。

### 2.2 新增的文件（整包可删，删除即完全回退）

```
app/selenetv/
├── __init__.py       # 包入口 + mount(app)，幂等挂载
├── schema.py         # 独立 MetaData + 4 张 selenetv_* 表（只增）
├── migrate.py        # 幂等建表入口，失败不影响主服务
├── security.py       # Helios Cookie 鉴权（明文口令双编码 + vodsync PBKDF2 校验）
├── serializers.py    # Helios 响应序列化（json tag/omitempty/SSE 驼峰/m3u8 提取）
├── repository.py     # 数据访问（只读 vod_{slug}，读写 4 张新表，slug 白名单防注入）
└── router.py         # 10 条路由
scripts/test_selenetv_api.py   # 契约 + 零影响验证（64 项）
```

### 2.3 数据库：只增 4 张表，现有表零触碰

| 新表 | 对应 Helios 能力 | 说明 |
|---|---|---|
| `selenetv_favorites` | `/api/favorites` | 唯一索引 (source, source_id) |
| `selenetv_play_records` | `/api/playrecords` | DB 列 `index_number` → JSON 名 `index` |
| `selenetv_search_history` | `/api/searchhistory` | 单行 id=1，record 存 JSON 数组 |
| `selenetv_live_sources` | `/api/live/sources` | 直播频道配置 |

- 使用**独立 MetaData**（`selenetv_meta`），不与 vodsync 主 `META` 合并；`META.create_all()`/`_migrate()` 感知不到本模块。
- `create_all(checkfirst=True)` 幂等：已有表跳过，不 ALTER、不 DROP、不 UPDATE。
- 验证结果：建表前后现有表集合、`vod_{slug}` 列定义完全一致；二次建表 `created=[]`。

---

## 三、10 条 API 与数据源映射

| # | 端点 | 方法 | 数据源 | 备注 |
|---|---|---|---|---|
| 1 | `/api/login` | POST | vodsync `settings.admin_password_hash`（PBKDF2） | 免鉴权；`{"ok":true}` + auth Cookie |
| 2 | `/api/search` | GET | `vod_{slug}` name LIKE（pg_trgm） | `{"results":[SearchResult]}`；过滤无 m3u8 结果 |
| 3 | `/api/search/ws` | GET | 同上 | SSE，逐源推事件，驼峰键 + 毫秒时间戳 |
| 4 | `/api/search/resources` | GET | `sources` 表 | 裸数组 `[{key,api,name,detail}]` |
| 5 | `/api/search/suggestions` | GET | 首源 `name LIKE 'q%'` | `{"suggestions":[{"text"}]}` |
| 6 | `/api/detail` | GET | `vod_{slug}` 整行 | 裸 SearchResult；`id` 正则 `^[\w-]+$` |
| 7 | `/api/favorites` | GET/POST/DELETE | `selenetv_favorites` | 裸 map / `{"success":true}` |
| 8 | `/api/searchhistory` | GET/POST/DELETE | `selenetv_search_history` | 裸 `[]string` |
| 9 | `/api/playrecords` | GET/POST/DELETE | `selenetv_play_records` | 裸 map；空 key 全删 |
| 10 | `/api/live/sources` | GET | `selenetv_live_sources` | `{"success":true,"data":[...]}` |

**关键契约复刻点**：
- `SearchResult` 字段逐字对齐 Helios：`id,title,poster,episodes,episodes_titles,source,source_name,year`，`class/desc/type_name/douban_id` 为 omitempty（`class` 因 PG 无列恒缺失）。
- `episodes[]` 是 m3u8 URL、`episodes_titles[]` 是集名，从 `play_url` 的 `$$$/#/$` 提取（取集数最多线路），与 Helios `ExtractEpisodes` 同构。
- 用户数据 GET 返回**以 `source+source_id` 为 key 的裸 map**，写操作返回 `{"success":true}`。
- 所有错误是 **text/plain**（如 `Authentication required`、`Invalid credentials`、`Method not allowed`），非 vodsync 的 JSON 包络。
- Cookie：`auth=<双重urlencode的 {role,username,password}>`，7 天、Path=/、SameSite=Lax、非 HttpOnly/Secure——与 Helios 完全一致。

---

## 四、部署与对接步骤

### 4.1 升级已有 vodsync（NAS Docker）

1. 用本目录作为新镜像构建源（或直接把 `app/selenetv/` 与改动的 `app/main.py`、`.env.example` 覆盖到你现有的 vodsync 部署目录）。
2. 环境变量：新增 `SELENETV_API_ENABLED=true`（默认即 true，可不显式设）。**其余 vodsync 配置一律不变**。
3. 重启容器。启动时日志会出现：`SeleneTV 专用表就绪：新建=[...]`（仅首次；再次重启显示"已存在跳过"）。
4. 验证：`curl http://<NAS>:8181/api/login -X POST -H 'Content-Type: application/json' -d '{"username":"any","password":"<你的vodsync面板密码>"}'` 应返回 `{"ok":true}` 并带 `Set-Cookie: auth=...`。
   > 说明：登录口令用的是 **vodsync 面板密码**（PBKDF2 校验）；username 任意非空即可（单租户，与 Helios 一致）。

### 4.2 SeleneTV 客户端设置

- 服务器地址填 `http://<NAS>:8181`（与 vodsync 同址同端口，无需额外端口）。
- 登录用面板密码；用户名随意（如 `aiden`）。
- 搜索建议切服务端聚合（`search_origin=server`）时走 `/api/search/ws` SSE。

### 4.3 回滚

任选其一，现有功能立即恢复原状：
- 设 `SELENETV_API_ENABLED=false` 重启（路由不挂载、表不建、闸门不放行）；
- 或删除整个 `app/selenetv/` 并还原 `app/main.py` 三处 `[selenetv]` 标记块。

`selenetv_*` 表留在库里对 vodsync 无任何影响（无代码引用）。

---

## 五、验证记录

| 验证项 | 结果 |
|---|---|
| 新模块语法编译（7 文件 + main.py） | ✅ 全部通过 |
| `scripts/test_selenetv_api.py`（契约 + 零影响，64 项） | ✅ **64 PASS / 0 FAIL** |
| vodsync 现有回归 `test_v1141_hotfix.py` | ✅ **28/28 通过**（现有功能零回归） |
| 建表幂等性（二次 created=[]） | ✅ |
| 现有表/字段建表前后一致 | ✅ |
| 关闭开关 `SELENETV_API_ENABLED=false` | ✅ 路由 0、新表 0、现有路由全保留 |
| slug 白名单注入防护 | ✅ 复用 `validate_slug`（`^[a-z0-9_]{1,64}$`） |

---

## 六、已知差异与能力边界（沿用分析报告）

1. **登录凭据**：Helios 是明文口令 Cookie + 环境变量；本实现 Cookie 格式相同，但口令走 vodsync PBKDF2 校验（更强），单租户无多用户。
2. **`class`/`douban_id`**：PG 无对应列，恒省略；SeleneTV 豆瓣联动降级为标题匹配。
3. **直播源**：Helios 从订阅配置读；本实现从 `selenetv_live_sources` 表读，需自行灌入频道数据（否则 `data: []`）。
4. **图片代理**：Helios 与本实现都不提供（SeleneTV 用 Coil 直连海报 URL）。
5. **分页**：Helios 无显式分页；新服务内部统一 limit，不涉及 `pg/page` 之争。

---

*实现完成。所有验证在本地 SQLite 测试后端进行（vodsync 原生支持的测试模式），未触碰 NAS 上的生产 PG 数据。*
