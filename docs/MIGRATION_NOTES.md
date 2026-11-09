# v1.14.0 数据迁移说明（MIGRATION_NOTES）

> 对应需求：`BACKEND_UPGRADE_REQUIREMENTS_2026-11-09.md`（B-01 / B-08 / B-09 / B-10 / B-11）
> 影响面：**新增 3 张表 + 可选 1 列**，不改任何现有列、不重命名任何字段。
> 升级方式：拉新镜像重启即可，`init_db()` 会自动建表补列，无需手工 SQL。

---

## 1. 变更清单

| 对象 | 类型 | 是否必做 | 回滚 |
|---|---|---|---|
| `video_tags` | 新表（标签倒排） | 自动建表 | `DROP TABLE video_tags` |
| `tag_index_state` | 新表（回填进度） | 自动建表 | `DROP TABLE tag_index_state` |
| `api_tokens` | 新表（具名 Token） | 自动建表 | `DROP TABLE api_tokens`（存量单 Token 不受影响） |
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
    cursor      INTEGER      DEFAULT 0,     -- 已处理到的最大 vod_id（水位）
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

## 3. 回填（把存量数据灌进 `video_tags`）

**正常情况什么都不用做**：`TAG_INDEX_ON_STARTUP=true`（默认）时，服务启动会检查索引，
没建完的源站在后台串行补齐，且**可断点续跑**。

手动兜底：

```bash
# 看进度（不写数据）
docker compose exec vodsync python scripts/backfill_tags.py --status

# 全部启用源站（断点续跑）
docker compose exec vodsync python scripts/backfill_tags.py

# 只补一个站
docker compose exec vodsync python scripts/backfill_tags.py --slug ffzy

# 怀疑索引脏了，清空该站后重建
docker compose exec vodsync python scripts/backfill_tags.py --slug ffzy --force
```

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
