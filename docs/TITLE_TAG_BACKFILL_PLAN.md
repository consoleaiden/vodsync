# 标题词库回填修复：根因、实现与上线方案

> 日期：2026-11-13
> 版本：**v1.16.2**（tag `v1.16.2`）
> 基线：线上与仓库原为 `v1.16.1` / `deecfd0`
>
> **版本号说明**：本次修复开发期间曾用 `v1.17.0` 作内部代号，但 `v1.17.0`
> 此前已被用于「`jingjian.txt` base58 源站自动接入」，且该版本已决定放弃并
> 完整回滚。为避开混淆，正式发版统一采用补丁版本号 `v1.16.2`；代码注释、
> 配置项说明与回归脚本命名均已同步。

---

## 1. 需求口径（按你的澄清收敛）

| 模块 | 数据来源 | 本次是否改动 |
|---|---|---|
| **相关推荐** | 现有 `vod_related` 多维度相关度 + 兜底算法 | **不动** |
| **猜你喜欢 / 标签推荐** | `vod_tag` → `video_tags` 倒排 → `/api/v1/videos?tag=`、`/search?tag=` | 不新增端点，只把数据补对 |
| **数据补全** | 视频**标题**命中明星词库 → 把命中词写入 `vod_{slug}.vod_tag` | 本次核心 |

- 18+ 源站的 `vod_tag` / `vod_actor` 几乎全空，标签推荐没有数据基础。
- 标题里包含词库中一个或多个词时，`vod_tag` 应写入**多个**词。
- 实测反例：乐播资源站标题含「香草少女」，`vod_tag` 全空，但词库里确有该词。

**明确不做**：不新增 `/api/v1/guess` 关键词推荐端点；不做用户画像；不在详情页实时按词库扫全库；不新建全库通用关键词倒排表。

---

## 2. 线上实测证据（群晖 Docker PG，只读查询）

连接方式：`plink` + `sudo docker exec -i vodsync-pg psql ...`（PG 未映射宿主端口，容器内访问）。

```
vodsync / vodsync-pg 均 healthy；启用源站 65 个
s35192781f1 | 乐播资源站 | 337986 行 | enabled=1
  vod_tag   非空:   383 行
  vod_actor 非空:     0 行
  标题含「香草少女」: 118 行，其中 vod_tag 非空 0 行

star_terms: 3443 条全部 enabled；容器内 reload_star_terms() 读到 3407 个主名，
            正则长度 21959
词库确实有: 262「香草少女」/ 1563「香草少女m」/ 2280「香草少女M」
正则对真实标题命中:
  56556  -> ['爆乳', '香草少女m']
  85666  -> ['爆乳', '香草少女']
  250277 -> ['香草少女', 'jk', '九尾狐狸']

全局: star_map 533869 / video_tags 925410 / vod_related 6921944
历史耗时: /search?wd 全库 ≈10.7s，/videos?tag ≈3.3~3.9s，tag=巨乳 ≈6.7s
/healthz.capabilities 缺少 tag_search
```

结论：**词库和正则都没问题，是「回填链路」没把命中结果落到 `vod_tag`。**

---

## 3. 根因链条

| # | 根因 | 证据 / 后果 |
|---|---|---|
| **R1** | 夜间全量重建的「上次重建时间」误取 `max(star_map.computed_at)`，而增量同步 `update_ids()` 每 6h 也刷新它 | 大站看起来「刚重建过」，**全量重建从未轮到乐播**：该站 `star_map` 仅 461 行，真跑过全量的站是 `s6541a42029=100149`、`s0c4e337e00=96715` 等 10 万条量级 |
| **R2** | `_hits_for_row()` 是「有演员就只看演员，否则才扫标题」的二选一 | 标题正则一次都不跑；这是「标题有多个词却一个都不写」的直接原因 |
| **R3** | 旧 `_scan_and_collect()` 只有 `src=="title"` 才写 `vod_tag`，且用推断值**整体覆盖** | 演员行不写；无命中却把源站原生标签清空；有命中就把原生标签冲掉；重复重建留下无法清理的旧推断段 |
| **R4** | 词库新增/停用后旧视频不会被重算，只能等 touched ids 或夜间全量重建 | 夜间轮转又被 R1 污染 → 词库改动对存量几乎不生效 |
| **R5** | 面板手动新增的 8 个源站缺 `tag_index_state` 行，`ready()` 的 `all(s in done)` 恒 false | `/healthz` 不声明 `tag_search`，App 标签检索整体回退成标题全文搜索。缺行站：`xinlang`、`https://p2100.net`、`dytt`、`ukys`、`mdzy`、`https://caiji.moduapi.cc`、`ruyi`、`lzcaiji`；它们其实各有 205~169456 行倒排 |
| 附带 | 词库里 `er/ss/jk` 等 2 字母 ASCII 词会污染标签 | 线上 `star_map` top term 出现 `er`、`ss` |

---

## 4. 修复实现（8 个已改文件 + 1 个新增测试）

| 文件 | 改动 |
|---|---|
| `app/star_tags.py` | 重写 `_hits_for_row()` 为 `(tag_hits, map_hits, actor_hits)`：标题∪演员都算，但**只有标题命中写 `vod_tag`**；新增 `merge_vod_tag()` 合并式回写（保留原生段、只替换上次推断段）、`_old_inferred_map()`、`_delete_map_ids()`、`_filter_hits()`；重写 `_scan_and_collect()` 为「每批读旧推断段 → 删该批 `star_map` → 写 `vod_tag`/`video_tags` → 插新 `star_map`」；`rebuild_source()` 完成后写 `star_rebuild_state`；`update_ids()` 共用合并回写且**不推进全量水位**；新增 `infer_on_ingest()` 写时推断；`status_all()` 暴露全量水位 |
| `app/models.py` | 新增 `star_rebuild_state(slug, last_full_at, rows_scanned, rows_tagged, rows_mapped, elapsed_s)`，由 `META.create_all()` 自动建表 |
| `app/scheduler.py` | `_job_star_tags_nightly()` 改为只认 `star_rebuild_state.last_full_at`；排序：从未重建优先 → 时间升序 → 小站优先 |
| `app/syncer.py` | `upsert_batch()` 在构造 INSERT 前调 `infer_on_ingest()`，同事务写主表 + 倒排；失败只 warning 不影响同步。**撤回了**曾加进 `ensure_table()` 的「建表即补状态行」（它会破坏 A7 契约） |
| `app/tags.py` | 新增 `ensure_state_rows()`（启动自愈：有倒排补 `done`、无倒排补 `pending`，只做 count）；`ready()` 保持严格判据 |
| `app/main.py` | 启动引导在 `TAG_SEARCH_ENABLED` 下无条件调 `ensure_state_rows()`，**与 `TAG_INDEX_ON_STARTUP` 解耦**；再按开关决定是否后台回填 |
| `app/config.py` | 新增 `STAR_TAG_INFER_ON_SYNC` / `UNION_HITS` / `MERGE_TAG` / `REBUILD_SLEEP_MS` / `MIN_ASCII_LEN`；校正 `STAR_TAG_CLEAR_STALE` 注释（现仅回滚分支生效） |
| `.env.example` | 增加「标题词库回填」配置段，与默认值一致 |
| `scripts/test_v1162_title_tags.py` | 新增 26 条回归用例 |

### 一个关键设计取舍（R5）

一开始试图让 `ready()` 兼容「缺状态行但有倒排 → 判就绪」，结果直接打挂
`test_v1140_contract.py` 的 **A7「索引未就绪时不声明 tag_search」**：
「面板手动新增站（倒排齐、缺状态行）」和「索引还没回填完」在数据形态上
**完全一样**，`ready()` 无法区分，放宽就等于废掉 v1.14.0 的灰度契约。

最终方案：**状态行是唯一真源**。`ready()` 保持纯读；缺行由
`main.py` 启动引导调 `ensure_state_rows()` 一次性补齐元数据（不扫大表、
不启动回填）。这样 A7 通过，线上 8 个缺行站也在下次重启后自愈。

---

## 5. 性能取舍：为什么是这套而不是另外两条

| 方案 | 判定 |
|---|---|
| 详情页实时用词库扫全库 / 每次切关键词全库 `LIKE` | ❌ 历史实测全库 `wd` 10.7s，群晖扛不住并发详情页 |
| 新建「标题关键词 ↔ 视频」通用倒排表 | ❌ 会在 658 万行级数据上再膨胀一张表，且多一条写时维护链路；词库一改就要全量重刷 |
| **本次：写时推断 + 存量一次性回填 + 独立低频校准** | ✅ |

具体理由：

1. **单正则抽词**：3400+ 词编译成 1 个 alternation，按长度降序（长词优先）、别名归一到主名；编译约 10ms，每个标题 µs 级，与词库规模解耦。到十万级再考虑 Aho-Corasick。
2. **写时推断**：同步每页几十条，成本可忽略；**新片从入库那一刻就有标签**，全量重建从「常规路径」降为「一次性存量回填 + 低频校准」。
3. **只写变化行**：`cur_tag != new_tag` 才落库，重复重建零写入（`tagged=0`）。
4. **keyset 分页 + 批间休眠**：`vod_id > last_id`、`STAR_TAG_BATCH_SIZE=2000`、`STAR_TAG_REBUILD_SLEEP_MS=20`，内存恒定、不与同步抢满 IO。
5. **读侧仍走倒排**：`video_tags` 精确匹配、`star_map` 按 `(slug, term)` 索引，不对大表 `LIKE`。
6. **不碰相关推荐**：`vod_related` 预计算链路零改动。

---

## 6. 数据一致性保障

- `vod_tag` 与 `video_tags` **同事务**：`_flush_tag_updates()` 复用同一 `conn` 调 `index_rows()`；写时推断直接改内存 `rows`，由 `upsert_batch()` 一次 UPSERT 同时落主表和倒排。
- `upsert_batch()` 的 `coalesce(nullif(excluded.vod_tag,''), table.c.vod_tag)` 保护，上游推空 tag 不覆盖本地推断值（C2 用例覆盖）。
- `merge_vod_tag()`：按「本服务上次写进去的推断段」（来自 `star_map.src='title'`）精确移除旧段，保住源站原生段；词库停用/标题改名后旧段自动消失（B2 用例）。
- `VARCHAR(255)` 按**整段**累加，绝不切出半个词；`STAR_TAG_MAX_PER_VIDEO=5` 限制词数（B3 用例）。
- `STAR_TAG_MIN_ASCII_LEN=3` 过滤 `er/ss/jk` 噪声；中文词仍按既有 2 字起规则。
- `star_map` 按批先删后插，不整站清空，避免中途失败丢光映射。

---

## 7. 验证结果

```
python -m compileall -q app                     OK
scripts/test_v1162_title_tags.py               26 passed / 0 failed
scripts/test_v1140_contract.py                 通过 123 / 失败 0（A7 PASS）
scripts/test_v1141_startag_sync.py             通过 40 / 失败 0
全量 31 个脚本：28 个 rc=0；3 个 rc=1 为历史固定失败，与本次无关：
  test_v11_features    UNIQUE constraint failed: sources.slug（历史脚本重复插数据）
  test_v12_pause       401 invalid_token（历史鉴权环境变量问题）
  test_v191_star_tags  could not translate host name "postgres"（未设 DATABASE_URL）
复现脚本 _repro_startag.py：修复前 201/204/205/206 标签缺失或被清空，
                            修复后标题多词写入、原生标签保留。
线上仅做只读 SQL / 容器内只读 Python 探针，**未对线上数据做任何写操作**。
```

`test_v1162_title_tags.py` 覆盖：标题多词、演员行也写标题命中、原生标签合并、
无命中不清空、演员命中只进 `star_map`、`src` 标记、短 ASCII 过滤、幂等、
停用词过期段清理、255 上限、主表/倒排一致、写时推断、全量水位、
`tag_search` 状态自愈与「不放宽灰度」的语义边界。

---

## 8. 上线与存量回填方案

### 8.0 前置

- 当前线上 `v1.16.1`，镜像 healthy；本次改动**全部还在本地工作区**。
- 备份：`docker exec vodsync-pg pg_dump -U postgres vodsync > vodsync_$(date +%F).sql`
  （或群晖 Container Manager 的卷快照），确认备份文件非空后再继续。
- 确认前端 App **零改动**：本次未新增/修改任何 API 契约，`related` 行为不变。

### 8.1 部署

沿用 `docs/deploy-synology.md`：上传项目文件 → `docker compose up -d --build`。
启动后 `init_db()` 的 `META.create_all()` 会自动建 `star_rebuild_state`（**无需手工 SQL**），
启动引导会调 `ensure_state_rows()` 补齐 8 个缺行站的状态。

**先只部署、不要立刻点全量回填**，按 8.2 验收启动结果。

### 8.2 部署后即时验收

```bash
curl -s http://100.84.176.119:8181/healthz
```
期望 `capabilities` 里**出现 `tag_search`**（这是 R5 自愈生效的标志）。
若仍没有，查 `docker logs vodsync | grep 补齐` 与
`SELECT slug,status FROM tag_index_state ORDER BY slug;`。

```sql
-- 8 个手动新增站应已有状态行
SELECT count(*) FROM tag_index_state;   -- 期望 65（原 57 + 8）
```

### 8.3 存量回填（一次性，建议夜间低峰）

**不要用 `slug=all`**：它把 65 个站一次性排进同一个后台线程，串行跑到底，
中途失败难定位、无法按站控速。按站逐个调用：

```bash
# 单站触发（需要 Token，沿用面板现有调用方式）
POST /api/v1/maintenance/star-tags?slug=<slug>
```

**顺序建议**：

1. **先拿乐播做验收站**：`s35192781f1`（33.8 万行，正是反例所在），单独在夜间跑，
   跑完按第 9 节 SQL 核对「香草少女」118 行是否全部有标签，再决定是否铺开。
2. 然后按 `total_videos` 升序，小站 → 大站，每跑完一站核对一站。
3. 大站（10 万行以上）之间留间隔，`STAR_TAG_REBUILD_SLEEP_MS` 可临时调到 `50`。
4. 全部跑完后，夜间调度器会按 `star_rebuild_state` 独立水位自动低频校准
   （`STAR_TAG_NIGHTLY_BUDGET=2`、`MIN_INTERVAL_HOURS=20`，**保持默认即可**，
   不需要为了回填调大预算）。

**观测**：

```bash
GET /api/v1/star/tags/status
```
本次新增返回字段：`last_full_rebuild` / `last_full_scanned` /
`last_full_tagged` / `last_full_mapped` / `last_full_elapsed_s`。
`last_full_rebuild=null` 即该站**从未真正全量重建过**，优先安排。

**新片无需等回填**：`STAR_TAG_INFER_ON_SYNC=true` 下，同步入库那一刻就有标签。

### 8.4 建议配置（`.env`，默认值已够用）

```dotenv
STAR_TAG_INFER_ON_SYNC=true        # 写时推断，务必保持开
STAR_TAG_UNION_HITS=true           # 标题∪演员，别回到二选一
STAR_TAG_MERGE_TAG=true            # 合并式回写，保住源站原生标签
STAR_TAG_REBUILD_SLEEP_MS=20       # 群晖 IO 紧张时提到 50
STAR_TAG_MIN_ASCII_LEN=3           # 过滤 er/ss/jk
STAR_TAG_BATCH_SIZE=2000           # 既有项，不必改
```

### 8.5 回滚

| 层级 | 做法 | 影响 |
|---|---|---|
| 代码 | 回退到 `v1.16.1` 镜像 / `deecfd0` | `star_rebuild_state` 表残留但旧代码不读，**无害** |
| 功能开关 | `STAR_TAG_INFER_ON_SYNC=false` 关写时推断 | 新片不再自动加标签 |
| 回写语义 | `STAR_TAG_MERGE_TAG=false` | 回到旧覆盖语义（**仅供对照，不建议**） |
| 数据 | 已写入的 `vod_tag` 不会自动回滚 | 合并式回写只动了推断段，源站原生标签保留，业务上可直接使用；如确需精确到旧值，只能从备份恢复 |

由于改动全部是「给空标签补值 + 幂等合并」，即使不回滚也不会破坏播放、
相关推荐或既有检索，风险主要集中在大站回填时的磁盘 IO。

---

## 9. 验收 SQL（乐播站，回填后执行）

```sql
-- 1) 反例复验：修复前 tagged=0，修复后应 tagged=118
SELECT count(*) AS total,
       count(*) FILTER (WHERE coalesce(vod_tag,'') <> '') AS tagged
FROM vod_s35192781f1
WHERE name LIKE '%香草少女%';

-- 2) 抽查标签内容是否多词、无半个词、无 er/ss/jk
SELECT vod_id, name, vod_tag
FROM vod_s35192781f1
WHERE name LIKE '%香草少女%'
ORDER BY vod_id LIMIT 10;

-- 3) 倒排与主表一致
SELECT count(*) FROM video_tags
WHERE source = 's35192781f1' AND tag_norm = '香草少女';

-- 4) 哪些站真的跑过全量重建
SELECT slug, last_full_at, rows_scanned, rows_tagged, rows_mapped, elapsed_s
FROM star_rebuild_state
ORDER BY last_full_at DESC NULLS LAST;

-- 5) 标签推荐可用（走 tag 精确检索，而不是 wd 全文）
-- GET /api/v1/videos?tag=香草少女&page=1
```

---

## 10. 已定事项与后续维护

1. **版本号已定 `v1.16.2`**：代码注释、`config.py`、`.env.example`、
   `scripts/test_v1162_title_tags.py`、`app/__init__.py`、`openapi.json`、
   `docker-compose.yml` 注释均已同步；提交 / 推送 / 打 tag / 部署按第 8 节执行。
2. **迁移文档已同步**：`docs/MIGRATION_NOTES.md` 第 7 节记录了
   `star_rebuild_state` 新表与升级行为。`README.md` /
   `docs/API_SPEC_FOR_AI.md` 若需补配置项说明可后续追加。
3. **openapi.json**：本次无接口契约变更，只同步了 `info.version`；
   `/api/v1/star/tags/status` 的响应 schema 本就是泛化 `{}`，仅新增可选字段，
   不需要重新导出。
4. **前端协同**：无需改 App；「猜你喜欢」继续用 `?tag=` 精确检索
   （依赖 `/healthz` 声明 `tag_search`），相关推荐保持 `/related` 不变。
5. **群晖临时诊断文件**：`/tmp/_q.sql` ~ `/tmp/_q5.sql`、`/tmp/_probe.py`
   是只读探针留下的，可手动删除，无副作用。
6. **已知限制**：升级本身不会改写历史 `vod_tag`，存量需按第 8.3 节按站回填；
   新片由写时推断自动覆盖。全量重建是幂等的，中断后重跑不会重复写入。
