# NAS 部署手册（照单执行版）

> 目标机：局域网 Debian NAS（生产 PG 已在 `172.22.169.180:55432` 运行）。
> 本手册自包含，部署当天从上到下照做即可；背景见根 README「部署」节。

## 0. 前置确认

- [ ] NAS 已装 Docker + compose 插件（`docker compose version`）
- [ ] 生产 PG 可从 NAS 访问（`psql -h 172.22.169.180 -p 55432 -U postgres -d postgres`）
- [ ] 手机与 NAS 同一局域网

## 1. 生产库初始化（一次性，psql 以超级用户执行）

```sql
CREATE ROLE health_app LOGIN PASSWORD '<给应用角色起一个独立密码>';
CREATE DATABASE shadow_health OWNER health_app;
-- schema health 由 alembic 首次迁移创建（owner 即 health_app，无需额外授权）
```

> 最小权限口径：health_app 只拥有 shadow_health 库；不要给 SUPERUSER/CREATEDB。

## 2. 拉代码与配置

```bash
git clone https://github.com/Cylunex/shadow-health.git && cd shadow-health
cp .env.example .env && chmod 600 .env
```

`.env` 必填（生成命令见文件内注释）：

| 键 | 值 |
|---|---|
| `DATABASE_URL` | `postgresql+psycopg://health_app:<角色密码>@172.22.169.180:55432/shadow_health` |
| `AUTH_PASSWORD_HASH` | `docker run --rm -v $PWD:/a -w /a python:3.12-slim python -m app.auth hash <登录密码>` |
| `SESSION_SECRET` / `INGEST_TOKEN` | `python3 -c "import secrets; print(secrets.token_urlsafe(32))"` 各生成一个 |
| `BACKUP_PG_HOST/PORT/DB/USER/PASSWORD` | 指向同一生产库（backup 容器用） |
| `ANTHROPIC_API_KEY` | 可选；填了 AI 分析和拍照识别才可用 |
| `MISCALE_MAC` | 可选；体脂秤 MAC（米家 App 设备信息里查） |

## 3. 目录与启动

```bash
sudo mkdir -p /srv/health-backups && sudo chown $USER /srv/health-backups
mkdir -p uploads   # 照片/导入文件卷
docker compose up -d --build            # app + backup（迁移在容器启动时自动执行）
docker compose exec app python -m app.seed   # 食物库/动作库/习惯/内置计划
curl http://127.0.0.1:8080/healthz     # 应返回 ok
```

体脂秤 BLE 网关（NAS 有蓝牙适配器时叠加，详见 gateway/README.md）：

```bash
docker compose -f docker-compose.yml -f docker-compose.miscale.yml up -d --build
docker logs -f shadow-health-miscale   # 上秤应看到「已上报 …→200」
```

## 4. 数据迁入（可选）

- 三星历史：设置 → 导入中心 → 上传三星健康导出 zip（幂等可重跑；
  会同时给 HC 与三星直读通道设防重水位线）
- Keep 历史：同上，选 Keep 文件（支持 .7z/.zip 加密/.xlsx）

## 5. 手机切换

1. 浏览器下载 `http://<NAS_IP>:8080/static/shadow-health.apk` 安装
   （apk 需先从构建机拷进 NAS 的 `static/`；签名变了要先卸载旧版）
2. 三指长按 → 服务器地址填 `http://<NAS_IP>:8080`，`INGEST_TOKEN` 填 .env 里那个
3. 勾选：体脂秤监听 / 三星健康同步（先开三星健康开发者模式 Data Read）/ 每日提醒
4. 国产 ROM：给应用开自启动 + 电池无限制

## 6. 验收清单

- [ ] 登录 → 今日页三环渲染
- [ ] 上秤一次 → 指标页出现带「体脂秤」徽标的体重/体成分
- [ ] 手机同步一次 → 设置页「同步状态」出现「三星直读」，导入中心「数据源明细」零失败
- [ ] 拍一张餐照 → AI 识别生成饮食记录（需 API key）
- [ ] 次日看 `/srv/health-backups` 出现 PG 备份文件

## 7. 运维要点

- **备份**：backup 容器只备 PG（日 7/周 4/月 6 滚动）；`uploads/photos/`（餐照）
  不在其中——纳入 NAS 快照/RAID，或另加 cron rsync；应用内
  「设置 → 一键全量备份」可随手拉全部表 CSV + 照片的 zip（唯一带照片的出口）
- **升级**：`git pull && docker compose up -d --build`（迁移自动跑；
  静态资源变更已通过 SW_VERSION 机制让客户端刷新）
- **测试**：改动后跑 `uv run pytest`（关键数据逻辑的回归锁）
- **硬性约定**：禁止公网端口转发；外网访问走 WireGuard/Tailscale
- 故障排查：手机同步失败先查三星健康 Data Read 开关和 token 一致性；
  秤不上报看 `docker logs shadow-health-miscale`；导入异常看导入中心「数据源明细」失败列
