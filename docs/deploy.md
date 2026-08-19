# NAS + Shadow SSO 部署手册（照单执行版）

> 目标机：局域网 Debian NAS，生产 PostgreSQL 在 `192.0.2.10:15432`。Health 数据仍留在 NAS；公网只通过 ECS HTTPS、Authelia 和现有 frp 隧道访问。
>
> 设计、安全边界、验收原理和回滚说明见 `docs/sso-migration.md`。本方案仅维护 Health
> Health 浏览器链路使用原生 OIDC Authorization Code + PKCE；Nginx 不注入身份头。

## 0. 前置确认

- [ ] NAS 已装 Docker + Compose，或已按现网方式配置 Supervisor
- [ ] `psql -h 192.0.2.10 -p 15432 -U postgres -d postgres` 可连接
- [ ] `health.example.com`、`auth.example.com` 已解析到 ECS
- [ ] ECS 上 Shadow Identity 可用，NAS 上 `127.0.0.1:8080` 为 Health
- [ ] 现有 frp `ECS 127.0.0.1:18081 → NAS 18080` 正常

## 1. 生产库初始化（仅首次）

```sql
CREATE ROLE health_app LOGIN PASSWORD '<独立强密码>';
CREATE DATABASE shadow_health OWNER health_app;
-- schema health 由 Alembic 首次迁移创建。
```

`health_app` 只拥有 `shadow_health`，不要授予 `SUPERUSER` 或 `CREATEDB`。

## 2. 应用配置

```bash
git clone https://github.com/Example/shadow-health.git
cd shadow-health
cp .env.example .env
chmod 600 .env
```

关键配置：

| 键 | 生产值 |
|---|---|
| `DATABASE_URL` | `postgresql+psycopg://health_app:<密码>@192.0.2.10:15432/shadow_health` |
| `INGEST_TOKEN` | Android、体脂秤与机器接口使用的高熵随机值 |
| `SHADOW_AUTH_MODE` | `oidc` |
| `SHADOW_OIDC_ISSUER` | Shadow Identity 的唯一 HTTPS issuer |
| `SHADOW_OIDC_CLIENT_ID` | `shadow-health` |
| `SHADOW_OIDC_CLIENT_SECRET_FILE` | `/etc/shadow-health/secrets/oidc-client-secret` |
| `SHADOW_OIDC_REDIRECT_URI` | Health 规范入口的精确 `/auth/callback` |
| `SHADOW_OIDC_REQUIRED_GROUP` | `health-users` |
| `SHADOW_OIDC_SESSION_DB` | NAS 持久目录下的本地浏览器 Session 库 |
| `BACKUP_PG_*` | 指向同一生产库 |

## 3. 启动与探活

Docker 部署：

```bash
sudo mkdir -p /srv/health-backups
sudo chown "$USER" /srv/health-backups
mkdir -p uploads
docker compose up -d --build
docker compose exec app python -m app.seed
```

现网 Supervisor 原生部署继续使用原有服务，并把 uvicorn 启动命令固定为：

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8080 --no-proxy-headers
```

`--no-proxy-headers` 继续保留，外部 Origin、Host 和前缀只由受控 Nginx 配置传入。
不要覆盖 NAS 仓库里未纳入 Git 的 `deploy/` 备份和 `scripts/deploy.sh`。

```bash
curl -fsS http://127.0.0.1:8080/healthz   # ok：仅进程存活
curl -fsS http://127.0.0.1:8080/readyz    # ready：包含数据库检查
```

体脂秤 BLE 网关按需叠加：

```bash
docker compose -f docker-compose.yml -f docker-compose.miscale.yml up -d --build
docker logs -f shadow-health-miscale
```

## 4. Shadow SSO 与独立域名

首次生成 OIDC client secret：

```bash
sudo install -d -m 0700 /etc/shadow-health/secrets
openssl rand -base64 48 | sudo tee /etc/shadow-health/secrets/oidc-client-secret >/dev/null
sudo chmod 0640 /etc/shadow-health/secrets/oidc-client-secret
```

原始 secret 只存在于 NAS 文件；Authelia 只登记其密码哈希，不写入 Git：

1. 将 `deploy/env/sso.env.example` 合并到 NAS `.env`。
2. 将 `deploy/nginx/nas-shealth-location.conf.example` 合并到 NAS `18080` 站点。内网 `/shealth/` 继续生成带前缀 URL，公网域名生成根路径 URL。
3. 在 Authelia 登记 `shadow-health`、精确 callback、PKCE S256 和 `health-users` 准入策略。
4. 安装本仓库两个 upstream snippet；浏览器 location 不使用 `auth_request`。
5. 安装 `health.example.com.conf.example`，配置覆盖该域名的证书。
6. 两端执行 `nginx -t` 后再 reload，不直接覆盖无备份的线上配置。

Health 应用在 OIDC callback 中校验 issuer、audience、签名、state、nonce、PKCE 和
`health-users`。机器接口继续由 Health Bearer Token 鉴权：

```text
/api/ingest/
/api/agent/
/api/offline/
/api/reminders/
```

Health 不接受客户端提交的 `Remote-*` 身份头、本地密码或旧 `sh_session` Cookie。
NAS 内网页面访问会经 `/login` 进入同一个云端 issuer；机器接口仍只认 Bearer。

## 5. 数据迁入（可选）

- 三星历史：设置 → 导入中心 → 上传三星健康导出 zip，可幂等重跑。
- Keep 历史：同一入口，支持 `.7z`、加密 `.zip`、`.xlsx`。

## 6. ShadowApp / Android 壳

服务器地址按顺序填写：

```text
http://192.0.2.10:18080/shealth
https://health.example.com
```

第一条仍供后台同步和体脂秤优先走局域网；WebView 页面访问到内网入口时会自动跳转
第二条并跟随 Platform 登录。后台同步继续填写 `INGEST_TOKEN`。APK 可从内网
`/shealth/static/shadow-health.apk` 下载。

## 7. 验收清单

- [ ] `https://health.example.com/` 跳转 Shadow Identity，登录后回到今日页
- [ ] 无 `health-users`/`shadow-admins` 组的账号无法进入
- [ ] `http://192.0.2.10:18080/shealth/` 不再显示密码页，并跳转公网 SSO
- [ ] 伪造 `Remote-*` 身份头不能绕过登录
- [ ] `/healthz` 返回 200；数据库不可用时 `/readyz` 返回 503
- [ ] 手机同步、上秤、提醒和 Agent Bearer 通道不被 SSO 重定向
- [ ] 餐照上传与 AI 识别正常
- [ ] PostgreSQL 备份和 `uploads/photos/` 快照正常

## 8. 运维边界

- 禁止公开应用 `8080` 或 NAS `18080`；公网只走 ECS HTTPS + SSO。
- PostgreSQL 备份不包含餐照，`uploads/photos/` 必须进入 NAS 快照或异机备份。
- 升级前运行全量测试，部署后同时检查 `/healthz`、`/readyz` 和四类机器接口。
- SSO 故障时修复 Identity/代理或整体回滚上一发布，不恢复旧密码入口，也不临时公开应用端口。
