# shadow-health

单用户、局域网自用的健康/饮食/运动管理 Web 应用。（详细设计文档在本地维护，不随仓库分发。）

四大模块：**饮食记录+营养分析 · 运动训练管理 · 身体指标追踪 · 养生任务打卡**，外加三星健康历史数据一次性导入与 Health Connect 增量同步。

> `app/seed/data/*.md` 训练计划内容素材为私人整理，不入仓库；缺失时 seed 自动跳过对应计划，说明见 [app/seed/data/README.md](app/seed/data/README.md)。

## 技术栈

Python 3.12（uv 锁定）· FastAPI · SQLAlchemy 2.x · Alembic · PostgreSQL（schema `health`）· Jinja2 + HTMX + Alpine.js + Chart.js + Tailwind（standalone CLI，静态资源全本地化，断外网可用）

## 本地开发

```powershell
# 1. 依赖（uv 自动使用 .python-version 指定的 3.12）
python -m uv sync

# 2. 开发数据库
docker compose -f docker-compose.dev.yml up -d

# 3. 配置：复制 .env.example 为 .env，生成密码哈希与密钥
python -m uv run python -m app.auth hash <你的密码>   # 填 AUTH_PASSWORD_HASH

# 4. 迁移 + seed
python -m uv run alembic upgrade head
python -m uv run python -m app.seed

# 5. 三星历史数据导入（可选，一次性；zip 为三星健康 App 官方导出）
python -m uv run python -m app.importers.samsung_zip <路径>\SamsungHealth.zip --dry-run
python -m uv run python -m app.importers.samsung_zip <路径>\SamsungHealth.zip

# 6. 起服务
python -m uv run uvicorn app.main:app --reload --port 8801
```

前端样式改动后重建 CSS：`.\tools\tailwindcss.exe -c tailwind.config.js -i static\src\input.css -o static\app.css --minify`
（`tools/` 不进 git，CLI 从 [tailwindcss releases](https://github.com/tailwindlabs/tailwindcss/releases/tag/v3.4.17) 下载）

## 部署（局域网 Debian 主机）

镜像基于 `python:3.12-slim`（Debian bookworm），全部依赖走 `uv.lock` 锁定的 pip 包（含 `tzdata`，
slim 镜像无系统 zoneinfo，`ZoneInfo("Asia/Shanghai")` 依赖它），无平台特定二进制，Debian x86_64 直接可用。

1. 装 Docker 与 compose 插件（Debian 12：`sudo apt install docker.io docker-compose-v2`，或按官方源装 `docker-ce` + `docker-compose-plugin`）；
2. 在 PG 里建 schema 与最小权限角色（设计文档附录 A）；
3. 服务器上放好 `.env`（`DATABASE_URL` 指向 `health_app` 角色、`BACKUP_PG_PASSWORD` 等，chmod 600）。
   注意 Linux 容器内没有 `host.docker.internal`：宿主机 PG 的地址直接写局域网 IP（如 `192.168.1.100:55432`），
   并确认 PG 的 `listen_addresses`/`pg_hba.conf` 放行 Docker 网段（详见 docker-compose.yml 顶部注释）；
4. 备份目录先建好：`sudo mkdir -p /srv/health-backups`；
5. `docker compose up -d --build`；
6. 部署前检查端口：`ss -tlnp | grep :8080`，冲突改 `.env` 的 `APP_PORT`；
7. 验证：`curl http://127.0.0.1:8080/healthz` 返回 `ok`。

**硬性约定**：禁止对本应用做公网端口转发（设计文档 §7.6）；外网访问走 WireGuard/Tailscale。

## 外部数据

| 通道 | 状态 | 说明 |
|---|---|---|
| 三星 zip 历史导入 | ✅ CLI + Web 上传 | `app/importers/samsung_zip.py`，幂等可重跑 |
| Health Connect webhook | ✅ 接收端 | `POST /api/ingest/health_connect`，Bearer token 鉴权，水位线去重 |
| Keep 文件导入 | ⏳ 待 zip 密码 | keep.zip 为 AES 加密，拿到密码确认结构后实现映射 |
| Keep API 同步 | ⏳ 暂缓 | 看过 Keep xlsx 内容后再决定是否值得做 |
