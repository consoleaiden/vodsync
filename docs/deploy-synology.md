# 群晖 Docker（Container Manager）部署指南

> 适用于 DSM 7.2+（Container Manager）。DSM 7.0/7.1 及以下为 Docker 套件，操作类似。

## 方式一：Container Manager 图形界面（推荐）

### 1. 准备目录

File Station 中创建两个目录（**建议都放在 SSD 卷上**）：

- `/docker/vodsync/data` —— 只放同步断点状态（`pause_state.json`）
- `/docker/vodsync/pgdata` —— **数据库本体**（v1.7.0 起固定 PostgreSQL，220 万行约 2~4GB）

```bash
mkdir -p /volume1/docker/vodsync/data /volume1/docker/vodsync/pgdata
```

### 2. 获取镜像

**方式 A（推荐）：拉取 CI 构建的镜像**

按 README 完成仓库搭建后，镜像在 GHCR：
`ghcr.io/consoleaiden/vodsync:latest`（已实测公开可拉取）

- 打开 Container Manager → 注册表，GHCR 镜像默认搜不到，需先 SSH 登录（见下方"SSH 登录 GHCR"），或在 GitHub Packages 页面把镜像设为 Public 后用"手动输入"方式拉取。

**方式 B：本地构建**

把整个项目文件夹上传到 `/docker/vodsync/`，用下面的 compose 文件（保留 `build: .` 行），Container Manager 会自动构建。

### 3. 新建项目

Container Manager → **项目** → **新增**：

- 项目名称：`vodsync`
- 路径：`/docker/vodsync`
- 来源：**创建 docker-compose.yml**，粘贴以下内容（按注释调整）：

```yaml
services:
  vodsync:
    image: ghcr.io/consoleaiden/vodsync:latest   # 方式B本地构建则改为 build: .
    container_name: vodsync
    restart: unless-stopped
    ports:
      - "8181:8000"          # 浏览器访问 http://群晖IP:8181
    volumes:
      - /volume1/docker/vodsync/data:/app/data   # 只放断点状态，必须挂载
    environment:
      # ↓ 必填：指向下面 postgres 服务；用户/密码/库名要与之保持一致
      DATABASE_URL: "postgresql+psycopg2://vodsync:vodsync@postgres:5432/vodsync"
      LATEST_JSON_URL: "https://mylazily.github.io/ziyuanzhan/data/latest.json"
      ONLY_OK_SOURCES: "true"
      FULL_SYNC_ON_DISCOVER: "true"
      SYNC_INTERVAL_MINUTES: "360"
      SOURCE_SYNC_CONCURRENCY: "2"
      PAGE_CONCURRENCY: "4"
      REQUEST_INTERVAL_SECONDS: "0.3"
      AUTO_RESUME_ON_BOOT: "true"
      AUTH_ENABLED: "true"
      ADMIN_PASSWORD: "666666"          # 首次启动的面板密码，登录后请在设置里改掉
      TZ: "Asia/Shanghai"
    depends_on:
      postgres:
        condition: service_healthy        # 等 PG 可接受连接再启动，否则会 CrashLoop

  # ---- PostgreSQL（v1.7.0 起必备，不能省）----
  postgres:
    image: postgres:16-alpine
    container_name: vodsync-pg
    restart: unless-stopped
    shm_size: "256mb"                     # 默认 64MB 不够多源归并排序用
    environment:
      POSTGRES_USER: vodsync
      POSTGRES_PASSWORD: vodsync          # 只走容器网络可不改；映射到宿主机就必须改
      POSTGRES_DB: vodsync
      TZ: Asia/Shanghai
    volumes:
      - /volume1/docker/vodsync/pgdata:/var/lib/postgresql/data   # ★ 按实际盘符改
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U vodsync -d vodsync"]
      interval: 10s
      timeout: 5s
      retries: 10
      start_period: 20s
    # 不要映射 5432 到宿主机（容器间用服务名直连即可，映射反而容易抢端口）
```

> 完整版（含全部可调项、tag 语义与回滚说明）以仓库根目录的 [`docker-compose.yml`](../docker-compose.yml) 为准 ——
> 上面的片段只是最小可用子集，**省掉 `postgres` 服务会直接起不来**。

点击 **下一步 → 完成**，容器自动启动。

### 4. 验证

- 浏览器打开 `http://群晖IP:8181/` → 跳转登录页，输入 `ADMIN_PASSWORD`（默认 `666666`）进入**管理面板**
- `curl -s http://群晖IP:8181/healthz` → 返回 `status` / `db` / `version` / `build`，**`"version"` 必须与你要部署的版本一致**

  ```json
  {"status":"ok","db":true,"version":"1.11.0","build":"<commit sha>","time":"..."}
  ```
- 打开 `http://群晖IP:8181/docs` 查看交互式 API 文档

> 管理面板说明：每 5 秒自动刷新；全量同步期间「全量进度」列会显示 `已抓取页数 / 总页数` 与百分比。
> 鉴权自 v1.5.0 起改为**面板密码登录 + API Token**（面板右上「⚙ 设置」里生成/重置 Token），
> 旧的 `API_KEY` 环境变量仅作为兼容的额外 Token，不再是主要方式。

### 5. 更新 / 回滚镜像版本

Container Manager → 项目 vodsync → **操作 → 停止 → 构建/拉取 → 启动**（数据在 `pgdata` 卷中，不会丢失）。

也可以直接改 compose 里的 tag 再 `docker compose pull vodsync && docker compose up -d`：

| tag 形式 | 可变性 | 用途 |
|---|---|---|
| `latest` | 可变 | 跟随最新（生产默认） |
| `1.11.0` | 可变 | 版本号，同一版本多次构建指向最新那次 |
| `1.11.0-<sha7>` | **不可变** | **回滚用这个** |
| `sha-<sha7>` | **不可变** | 提交级 |

回滚后 `curl http://群晖IP:8181/healthz` 核对 `version` / `build` 是否已是目标版本。
注意：**数据库不回退** —— 新版本做过的迁移不会撤销，回滚到跨迁移的旧版本前先备份 `pgdata`。

---

## 方式二：SSH 命令行（适合习惯终端的用户）

```bash
# SSH 登录群晖（用户名有管理员权限）
ssh admin@192.168.1.100

# 切换 root
sudo -i

# 创建目录（data 放断点，pgdata 放数据库）
mkdir -p /volume1/docker/vodsync/data /volume1/docker/vodsync/pgdata

# （使用 GHCR 私有镜像时先登录）
docker login ghcr.io -u consoleaiden -p <GitHub Personal Access Token>

# 进入项目目录（已上传项目文件，含 docker-compose.yml）启动
cd /volume1/docker/vodsync
docker compose up -d

# 查看日志（vodsync 与 PG 都要正常）
docker compose ps
docker logs -f vodsync
```

### SSH 登录 GHCR 拉取私有镜像

1. GitHub → Settings → Developer settings → Personal access tokens → 生成 token，勾选 `read:packages`
2. `docker login ghcr.io -u consoleaiden -p <token>`
3. `docker pull ghcr.io/consoleaiden/vodsync:latest`

---

## 运行参数说明

| 项 | 值 | 说明 |
|---|---|---|
| 端口映射 | `8181:8000` | 左侧为宿主机端口，可自改；右侧 8000 固定 |
| 数据卷（App） | `/volume1/docker/vodsync/data:/app/data` | 仅同步断点状态（`pause_state.json`）；**数据库不在这里** |
| 数据卷（PG） | `/volume1/docker/vodsync/pgdata:/var/lib/postgresql/data` | **必须**，数据库本体（220 万行约 2~4GB）；建议放 SSD |
| 重启策略 | `unless-stopped` | 开机自启、异常自动重启 |
| 内存限制 | 建议 2~4GB（含 PG） | 相关推荐整站重算时峰值较高；内存紧可把 `RELATED_RECOMPUTE_CONCURRENCY` 设为 1 |

## 常用环境变量（追加到 compose 的 environment）

```yaml
      # 数据库（必填；PG 由同一个 compose 里的 postgres 服务提供）
      DATABASE_URL: "postgresql+psycopg2://vodsync:vodsync@postgres:5432/vodsync"
      # 只同步在线源站
      ONLY_OK_SOURCES: "true"
      # 请求更保守的限速（源站风控严格时）
      REQUEST_INTERVAL_SECONDS: "0.5"
```

## 常见问题

| 现象 | 处理 |
|---|---|
| 打不开 8181 | 检查端口占用：`netstat -tlnp \| grep 8181`；防火墙/路由器端口 |
| 启动就退出 / 连不上库 | `docker logs vodsync` 看是否 `could not translate host name "postgres"`：PG 没起或不在同一 compose 项目网络。`docker ps` 确认 `vodsync-pg` 在跑，`docker compose up -d postgres` 补起 |
| 数据没保存 | 确认 `pgdata` 卷挂载了 `/var/lib/postgresql/data`；`ls /volume1/docker/vodsync/pgdata` 应能看到 `PG_VERSION` |
| 同步一直失败 | `docker logs vodsync` 看具体报错；多为源站 403 风控，调大 `REQUEST_INTERVAL_SECONDS` |
| 想重建从头同步 | 停容器 → 清空 `pgdata` 目录（或 `drop database vodsync` 后重建）→ 启动 |
| 备份 | `docker exec vodsync-pg pg_dump -U vodsync vodsync > vodsync-$(date +%F).sql`（热备安全，不用停容器） |
| 恢复 | `docker exec -i vodsync-pg psql -U vodsync -d vodsync < vodsync-2026-11-04.sql` |

## 反向代理（可选，套用域名 + HTTPS）

Control Panel → Login Portal → Advanced → Reverse Proxy：
- 来源：`https://vodsync.example.com:443`
- 目的地：`http://localhost:8181`
然后配合 DSM 的 Let's Encrypt 证书即可。
