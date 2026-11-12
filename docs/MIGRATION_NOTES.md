# 数据迁移说明（v1.14.0 ~ v1.16.0，MIGRATION_NOTES）

> 对应需求：`BACKEND_UPGRADE_REQUIREMENTS_2026-11-09.md`（B-01 / B-08 / B-09 / B-10 / B-11）、
> v1.15.0「源站 18+ 标记与未成年模式」、v1.16.0「智能去广告规则下发与命中上报」
> 影响面：**新增 5 张表 + 2 个追加列**，不改任何现有列、不重命名任何字段。
> 升级方式：拉新镜像重启即可，`init_db()` 会自动建表补列，无需手工 SQL。

---

## 1. 变更清单

| 对象 | 类型 | 是否必做 | 回滚 |
|---|---|---|---|
| `video_tags` | 新表（标签倒排） | 自动建表 | `DROP TABLE video_tags` |
| `tag_index_state` | 新表（回填进度） | 自动建表 | `DROP TABLE tag_index_state` |
| `api_tokens` | 新表（具名 Token） | 自动建表 | `DROP TABLE api_tokens`（存量单 Token 不受影响） |
| `sources.is_adult` | 追加列（18+ 标记） | 自动 `ALTER` | 保留无害（旧代码不读） |
| `adfilter_rule_packs` | 新表（v1.16.0 去广告规则包） | 自动建表 + 启动播种 v1 | `DROP TABLE adfilter_rule_packs` |
| `adfilter_reports` | 新表（v1.16.0 命中上报明细） | 自动建表 | `DROP TABLE adfilter_reports`（只影响统计，不影响播放） |
| `settings.api_token_scope` | 新增一条配置行 | 启动时自动写入 `admin` | 删行即可（等价 admin） |

**没有**改动的：`vod_{slug}` 表结构、`sources` 表、任何响应字段名、`list` / `detail` / `pagecount` /
`original_index` 的语义。

## 2. 建表 SQL（PostgreSQL）

正常情况下**不需要**手工执行 —— 服务启动时 `META.create_all()` 会建好。这里给出等价 DDL，
供「先建表再切流量」或审计用：

```sql
-- 标签倒排：一部片一个标签一行
CREATE TABLE IF NOT EXISTS video_tags (
    source   VARCHAR(64) NOT NULL,          -- 源站 slug
    vod_id   INTEGER     NOT NULL,
    tag_norm VARCHAR(64) NOT NULL,          -- 归一化：去空白 + ASCII 小写
    PRIMARY KEY (source, vod_id, tag_norm)  -- 同时支撑「某视频的全部标签」
);
-- 支撑 WHERE tag_norm = ? AND source IN (...)（点标签找影片走这条）
CREATE INDEX IF NOT EXISTS idx_video_tags_tag ON video_tags (tag_norm, source);

-- 回填进度：游标 + 计数，支持断点续跑
CREATE TABLE IF NOT EXISTS tag_index_state (
    slug        VARCHAR(64) PRIMARY KEY,
    cursor      INTEGER      DEFAULT 0,     -- 已处理到的最大 vod_id（水位，仅断点续跑用）
    processed   INTEGER      DEFAULT 0,     -- 已处理**条数**（v1.14.1；进度分子，与 total 同量纲）
    indexed     INTEGER      DEFAULT 0,     -- 已写入行数
    total       INTEGER      DEFAULT 0,     -- 该源站总条数（进度分母）
    status      VARCHAR(16)  DEFAULT 'pending',  -- pending/running/done/failed
    error       TEXT         DEFAULT '',
    started_at  TIMESTAMP,
    finished_at TIMESTAMP,
    updated_at  TIMESTAMP    DEFAULT now()
);

-- 具名 Token（只存 sha256，不存明文）
CREATE TABLE IF NOT EXISTS api_tokens (
    id           SERIAL PRIMARY KEY,
    token_hash   VARCHAR(64)  NOT NULL UNIQUE,
    scope        VARCHAR(16)  DEFAULT 'read',   -- read / admin
    note         VARCHAR(128) DEFAULT '',
    created_at   TIMESTAMP    DEFAULT now(),
    last_used_at TIMESTAMP,
    revoked      INTEGER      DEFAULT 0
);
```

## 2.1 v1.14.1 追加列（2026-11-10）

`tag_index_state` 增加 `processed`（已处理条数）。**自动迁移**，无需手工执行：

```sql
ALTER TABLE tag_index_state ADD COLUMN processed INTEGER DEFAULT 0;
```

为什么加它：v1.14.0 的面板进度是 `cursor / total`，但 `cursor` 是上游 `vod_id` 水位、
`total` 是行数，两个量纲相除，ID 稀疏的源站会显示 147%、172%。升级后：

- 已完成（`status=done`）的行：`processed` 补 0 也不影响 —— 完成态进度恒 100；
- 正在回填的行：续跑时会用 `COUNT(vod_id <= cursor)` 把 `processed` 一次性补准，
  面板不会从 0% 跳变；
- 想立刻让历史行显示正确：`docker compose exec vodsync python scripts/backfill_tags.py --status`
  查看，或直接点面板「重建索引」（幂等，已完成站也会重扫一遍，耗时按数据量）。

回滚到 v1.14.0 不需要删列（旧代码不读 `processed`，多一列无害）。

## 2.2 v1.15.0 追加列：`sources.is_adult`（2026-11-11）

给每个源站持久化「是否包含 18+ 视频内容」的标记。**自动迁移**，无需手工执行：

```sql
ALTER TABLE sources ADD COLUMN is_adult INTEGER DEFAULT 0;
```

| 值 | 含义 |
|---|---|
| `0` / `NULL` / 任意非 1 值 | 不含 18+（**未设置的存量源站一律按此处理**） |
| `1` | 含 18+，未成年模式下整站屏蔽 |

兼容要点：

- 存量行由 `DEFAULT 0` 填 0；读侧一律 `COALESCE(is_adult, 0) == 1` 判定，
  即使手工塞了 NULL 也不会误屏蔽。
- 新登记的源站（`refresh_sources()` INSERT / 面板自定义源站）不显式带这列，
  走 `server_default='0'`，同样是「否」。
- `refresh_sources()` 对已存在源站的 UPDATE 只写 `name` / `status` / `description`，
  不会把用户设的标记洗掉；`update_custom_source()` 按字段白名单更新，同理。
- 旧版本镜像连新库不会报错（多一列不影响 `select(sources)` 按 key 取值）；
  **回滚代码不需要删列**。
- 标记只影响读路径的可见集合，**不删改任何数据**：关闭未成年模式立即恢复，
  不需要重建索引或重新同步。

面板「源站管理」新增「18+ 内容」下拉列（选择即保存），等价命令：

```bash
curl -X POST -H "Authorization: Bearer <admin-token>" \
  "http://<host>:8181/api/v1/sources/<slug>/adult?enabled=true"
curl -H "Authorization: Bearer <admin-token>" \
  "http://<host>:8181/api/v1/minor-mode"
# 期望：{"code":0,"adult_sources":["<slug>"],"adult_count":1,...}
```

升级自检：

```bash
curl -s http://<host>:8181/healthz | grep -o '"version":"[^"]*"'   # 期望 1.15.1
# capabilities 里应含 minor_mode
# 不带 X-Minor-Mode 调 /source，list 长度应与升级前一致（现有调用零影响）
# 带 -H "X-Minor-Mode: 1" 调 /source，list 不应含被标记的 slug
```

服务端强制管控（可选，默认关）：compose 里设 `MINOR_MODE_FORCE: "true"`。

---

## 2.3 v1.16.0 新增两张表：去广告规则包与命中上报（2026-11-12）

**自动建表 + 启动播种**，无需手工执行。等价 DDL：

```sql
-- 规则包：每个 channel 内 version 严格 +1，历史行永不修改（回滚 = 新增一行）
CREATE TABLE IF NOT EXISTS adfilter_rule_packs (
    id         SERIAL PRIMARY KEY,
    channel    VARCHAR(16) NOT NULL DEFAULT 'stable',   -- stable / beta
    version    INTEGER     NOT NULL,
    rules_json TEXT        NOT NULL,                    -- 已规范化（键排序）的规则 JSON
    note       VARCHAR(255) DEFAULT '',
    created_at TIMESTAMP    DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_adfilter_pack_ver
    ON adfilter_rule_packs (channel, version);

-- 命中上报明细：只增不改，Phase 2「广告指纹库」的原始数据源
CREATE TABLE IF NOT EXISTS adfilter_reports (
    id           SERIAL PRIMARY KEY,
    user_id      INTEGER      NOT NULL DEFAULT 0,       -- 凭据身份（见下）
    url          VARCHAR(2048) NOT NULL,                -- 播放列表地址（不对外暴露）
    url_hash     VARCHAR(64)  NOT NULL,                 -- sha256(url)，去重键
    rule_version INTEGER      DEFAULT 0,
    removed_runs INTEGER      DEFAULT 0,
    removed_ms   BIGINT       DEFAULT 0,
    total_ms     BIGINT       DEFAULT 0,                -- 过滤前总时长
    at           BIGINT       NOT NULL,                 -- epoch 秒（用 BIGINT 避开 2038）
    created_at   TIMESTAMP    DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_adfilter_report_dedup
    ON adfilter_reports (user_id, url_hash, at);
CREATE INDEX IF NOT EXISTS idx_adfilter_report_at ON adfilter_reports (at);
```

两处刻意的设计取舍（与需求文档的「建议 DDL」不完全相同，语义等价且更安全）：

1. **主键用 `(channel, version)` 唯一索引**，不是单列 `version PK`。stable 与 beta 各自从
   v1 开始发号，客户端只比较同一渠道内的大小；单列主键会让 beta 的发版把 stable 的号顶掉。
2. **去重键用 `url_hash` 而非 `url`**。`url` 允许 2048 字符（UTF-8 最长约 6KB），直接进 PG
   唯一索引会超过 btree 单条上限（约 2704 字节）导致插入失败；sha256 定长，去重语义不变。
   原始 `url` 仍然完整保存，供 Phase 2 聚合用。

`user_id` 在本服务（单租户自托管，没有多用户表）里用**凭据身份**近似：
`0` = 存量单 Token，`>0` = `api_tokens.id`，`-1` = `API_KEY` 环境变量，
`-2` = 面板会话，`-4` = 未鉴权（`AUTH_ENABLED=false` 的开放部署）。契约要求
「按用户隔离、不得跨用户可见」，落到这里就是「按 Token 隔离」，且明细本身没有任何
对外接口暴露（`/adfilter/admin/stats` 只给聚合数字，**不含 url**）。

启动时会在 stable 渠道播种 v1（内置默认规则），因此面板「去广告规则包」首次打开
就能看到 `stable v1`，而不是 `v0`（`v0` 会让管理员误以为功能没生效）。

升级自检：

```bash
curl -s http://<host>:8181/healthz | grep -o '"version":"[^"]*"'   # 期望 1.16.0
# capabilities 里应含 adfilter
curl -s -H "Authorization: Bearer <token>" \
  "http://<host>:8181/api/v1/adfilter/rules?version=0"
# 期望：{"changed":true,"version":1,"rules":{...}}（重复带 version=1 → changed:false 且无 rules）
```

关闭控制面（保留客户端本地过滤）：compose 里设 `ADFILTER_ENABLED: "false"`，
`/healthz` 不再声明 `adfilter`，两个接口返回 404，已升级的客户端会静默停止调用。

回滚到 1.15.x 不需要删表（旧代码不读这两张表）；模型里的表定义不会影响
`select(sources)` 等既有查询。

## 3. 回填（把存量数据灌进 `video_tags`）

**正常情况什么都不用做**：`TAG_INDEX_ON_STARTUP=true`（默认）时，服务启动会检查索引，
没建完的源站在后台串行补齐，且**可断点续跑**。

手动兜底：

```bash
# 看进度（不写数据）
docker compose exec vodsync python -c "from app import tags; print(tags.status())"

# 全部启用源站（断点续跑）
docker compose exec vodsync python -c "from app import tags; print(tags.rebuild_all())"

# 只补一个站
docker compose exec vodsync python -c "from app import tags; print(tags.rebuild_all(slug='ffzy'))"

# 怀疑索引脏了，清空该站后重建
docker compose exec vodsync python -c "from app import tags; print(tags.rebuild_all(slug='ffzy', force=True))"
```

> 生产镜像只 `COPY app ./app`，不含 `scripts/`。源码目录（或把 scripts 挂载进
> 容器）里也可以用等价的 CLI：`python scripts/backfill_tags.py --status` /
> `--slug ffzy` / `--force`，输出更友好。详见 3.1 节的排障说明。

也可以走接口（面板「标签索引 → 重建索引」按钮就是它）：

```bash
curl -s -X POST -H "Authorization: Bearer $ADMIN_TOKEN" \
  "http://127.0.0.1:8181/api/v1/maintenance/tag-index?slug=ffzy&force=0"
curl -s -H "Authorization: Bearer $ADMIN_TOKEN" http://127.0.0.1:8181/api/v1/tag/status
```

### 预计耗时

按本机实测：22 万条视频、平均 2.7 个标签/条 ≈ 60 万行。
`TAG_INDEX_BATCH=2000` + `TAG_INDEX_SLEEP_MS=50` 时约 **6~12 分钟/10 万条**
（NAS 机械盘、与同步任务并行的保守值）。全库 220 万条约 **2~4 小时**。

想快可以临时调大：`TAG_INDEX_BATCH=5000 TAG_INDEX_SLEEP_MS=0`，但**别在同步高峰做**
—— 回填和同步抢的是同一块盘。

### 3.1 定点修补：明星Tag回填与倒排表不一致（v1.14.1，2026-11-11）

**症状**：`/api/v1/tag/status` 显示所有源站 `done`、进度 100%，但 `video_tags`
行数长期不涨（线上曾停在 ≈3.3% 覆盖）；明星词库补进去的名字 `?tag=` 搜不到。

**根因**：`vod_tag` 有两条写入路径 —— `syncer.upsert_batch` 在同一事务里维护
`video_tags`，而 `star_tags` 夜间明星词库回填（`_flush_tag_updates`）以前只
UPDATE 主表。补进去的标签既不进倒排、也不计入索引行数，且每晚重建持续制造
新缺口。线上实测：`s0c4e337e00` 有 77,020 条视频带标签、`video_tags` 中 0 行；
`s430327adfe` 4,270 条、同样 0 行；另有 5 个源站在倒排表里完全没有行。

**代码已修**：`_flush_tag_updates` 现在同事务（savepoint 隔离）调用
`tags.index_rows`，标签被清空时也会删掉旧倒排行。升级后新产生的回填自动一致。

**存量数据必须手动补一次** —— 全量 `--force` 重扫线上约 10 小时，不要用它：

```bash
# 只扫「有标签」的行（约占全表 3%），秒级~分钟级；clean=True 先删后插、
# 单事务原子，顺带洗掉「主表已无标签、倒排里还留着」的孤儿行
docker exec vodsync python -c "from app import tags; r=tags.sync_all_sources(clean=True); print({k:v for k,v in r.items() if k!='detail'}); print([d for d in r['detail'] if d['scanned']>0][:10])"

# 只修一个站
docker exec vodsync python -c "from app import tags; print(tags.sync_source_tags('s0c4e337e00', clean=True))"
```

`python -c` 是刻意的：生产镜像 `Dockerfile` 只 `COPY app ./app`，**不含
`scripts/`**，上面 `backfill_tags.py` 这类 CLI 只在源码目录或挂载了 scripts
的环境可用；容器内请用 `python -c` 调 `tags.rebuild_all()` /
`tags.sync_all_sources()`，或走面板 / `POST /api/v1/maintenance/tag-index` 接口。

> **「3.3% 覆盖」不是索引完成度**。`processed / total`（面板进度）才是完成度，
> 100% 就表示每条视频都扫过了。覆盖率 = `video_tags` 行数 / 视频总行数，
> 上限由上游 `vod_tag` 字段本身决定：多数采集站该字段为空（实测某站 12.8 万条
> 视频仅 15 条带标签）。想提高它只能扩大明星词库命中面
> （`STAR_TAG_NIGHTLY_BUDGET` 默认每晚只重建 2 个站）或让上游补标签，
> **重扫索引不会改变它**。

### 标签倒排与主表的一致性（v1.14.1 热修，2026-11-11）

`upsert_batch` 现在按「主表最终 `vod_tag`」维护倒排：上游 `vod_tag` 为空时
沿用主表已有的明星Tag，不再把 `video_tags` 里该视频的行先删后不插。
`star_tags` 夜间回填、字段回填、日常全量/增量同步三条路径因此不会再互相
洗掉标签。若升级前已出现覆盖率下降，执行一次 `tags.sync_all_sources(clean=True)`
即可对齐（命令见 3.1 节）。

### 回填期间的行为

- `/healthz` **不声明** `tag_search`（客户端继续用名称模糊检索，无需回滚）；
- `?tag=` 接口本身可用，但只覆盖已回填的源站（`total` 会偏小）；
- 同步/增量/详情/播放全部不受影响。

## 4. 灰度与回滚

| 想做的事 | 怎么做 | 生效方式 |
|---|---|---|
| 暂不提供标签检索 | `CAPABILITIES_DISABLE=tag_search` | 重启；客户端下次探测即退化 |
| 客户端说没就绪但其实建完了 | `TAG_SEARCH_FORCE=true` | 重启（仅排障） |
| 回填把 IO 打满 | `TAG_INDEX_SLEEP_MS=200`，或直接停容器等空闲再跑 | 重启 |
| 标签检索出问题 | `TAG_SEARCH_ENABLED=false` | 重启，接口与索引都停用，主流程不受影响 |
| 彻底回退 | `DROP TABLE video_tags; DROP TABLE tag_index_state;` | 立即，无副作用 |

## 5. Token 权限（B-08）迁移四步

| 步 | 动作 | 服务端配置 | 现状 |
|---|---|---|---|
| 1 | 上线 scope 能力，**全部 Token 默认放行** | `TOKEN_SCOPE_ENFORCE=false` | ✅ 已实现（默认值） |
| 2 | App 发版：控制台写操作优先用 admin Token | 不改 | 待客户端 |
| 3 | 观察 ≥2 个版本周期后，新建 Token 强制 scope | 不改（新建时显式选 scope） | 已完成 |
| 4 | 对存量收紧，旧版客户端按版本放行 | `TOKEN_SCOPE_ENFORCE=true` + `TOKEN_SCOPE_LEGACY_BELOW=1.6.0` | 待观察期后 |

回滚：把 `TOKEN_SCOPE_ENFORCE` 设回 `false` 重启，**不需要改数据**
（`api_tokens.scope` 列保留，只是不再校验）。

存量单 Token（`settings.api_token`）在升级后自动获得 `scope=admin`，
所以「只升级、不新建 Token」的部署行为与 v1.13 完全一致。

## 6. 升级自检

```bash
# 1) 版本
curl -s http://127.0.0.1:8181/healthz | python -c "import json,sys;d=json.load(sys.stdin);print(d['version'],d['status'])"
# 期望：1.14.0 ok

# 2) 能力清单（索引没建完时不应有 tag_search）
curl -s http://127.0.0.1:8181/healthz | python -c "import json,sys;print(json.load(sys.stdin)['capabilities'])"

# 3) 索引进度
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8181/api/v1/tag/status

# 4) 标签检索（索引就绪后）
curl -s "http://127.0.0.1:8181/api/v1/search?tag=%E6%82%AC%E7%96%91&source=ffzy&page_size=18" \
  | python -c "import json,sys;d=json.load(sys.stdin);print(d['total'],d['pagecount'],len(d['list']))"

# 5) 错误码（不带 Token）
curl -s http://127.0.0.1:8181/api/v1/sources | python -c "import json,sys;print(json.load(sys.stdin)['code'])"
# 期望：40101
```
