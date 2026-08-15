# NAS + Shadow SSO 部署手册（照单执行版）

> 目标机：局域网 Debian NAS，生产 PostgreSQL 在 `192.0.2.10:15432`。Health 数据仍留在 NAS；公网只通过 ECS HTTPS、Authelia 和现有 frp 隧道访问。

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
| `AUTH_PASSWORD_HASH` | 内网本地登录密码的 scrypt 哈希 |
| `SESSION_SECRET` / `INGEST_TOKEN` | 分别生成的高熵随机值 |
| `SHADOW_AUTH_MODE` | `hybrid`：公网 SSO、内网继续本地登录 |
| `SHADOW_SSO_ALLOWED_GROUPS` | `health-users,shadow-admins` |
| `SHADOW_PROXY_AUTH_SECRET_FILE` | `/etc/shadow-health/secrets/proxy-auth-secret` |
| `SHADOW_TRUSTED_PROXIES` | 原生部署为 `127.0.0.1/32,::1/128` |
| `BACKUP_PG_*` | 指向同一生产库 |

创建本地密码哈希：

```bash
python3 -m app.auth hash '<本地登录密码>'
```

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

`--no-proxy-headers` 必须保留：ECS 经 frp 到 NAS 后仍由 NAS Nginx 从回环地址访问应用，
应用只信任这个真实传输对端；若让 uvicorn 根据 `X-Forwarded-For` 改写客户端地址，SSO
代理身份校验会误判。真实访客地址仍由两端 Nginx 记录，不需要扩大
`SHADOW_TRUSTED_PROXIES`。不要覆盖 NAS 仓库里未纳入 Git 的 `deploy/` 备份和
`scripts/deploy.sh`。

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

首次生成代理身份密钥：

```bash
sudo install -d -m 0700 /etc/shadow-health/secrets
openssl rand -base64 48 | sudo tee /etc/shadow-health/secrets/proxy-auth-secret >/dev/null
sudo chmod 0600 /etc/shadow-health/secrets/proxy-auth-secret
```

同一密钥只存在于 NAS 文件和 ECS Nginx 私密 snippet，不写入 Git：

1. 将 `deploy/env/sso.env.example` 合并到 NAS `.env`。
2. 将 `deploy/nginx/nas-shealth-location.conf.example` 合并到 NAS `18080` 站点。内网 `/shealth/` 继续生成带前缀 URL，公网域名生成根路径 URL。
3. 在 ECS 安装 Shadow Platform 的 `authelia-location.conf`、`authelia-authrequest.conf`。
4. 安装本仓库 `shadow-health-upstream.conf.example` 和 `shadow-health-service-upstream.conf.example` 两个 snippet。
5. 从 `shadow-health-proxy-secret.conf.example` 创建 ECS 私密 snippet，替换占位值并设为 `root:root 0600`。
6. 安装 `health.example.com.conf.example`，配置覆盖该域名的证书。
7. 两端执行 `nginx -t` 后再 reload，不直接覆盖无备份的线上配置。

公网浏览器页面由 Authelia 的 `health-users` 或 `shadow-admins` 组保护。机器接口只精确放行以下前缀，并继续由 Health Bearer Token 鉴权：

```text
/api/ingest/
/api/agent/
/api/offline/
/api/reminders/
```

没有代理密钥时，即便伪造 `Remote-User` 也不会建立 SSO 身份。`forward-auth` 模式会直接返回 401；当前 `hybrid` 模式会回退到本地登录。

## 5. 数据迁入（可选）

- 三星历史：设置 → 导入中心 → 上传三星健康导出 zip，可幂等重跑。
- Keep 历史：同一入口，支持 `.7z`、加密 `.zip`、`.xlsx`。

## 6. ShadowApp / Android 壳

服务器地址按顺序填写：

```text
http://192.0.2.10:18080/shealth
https://health.example.com
```

第一条用于局域网直连，第二条用于外网。后台同步继续填写 `INGEST_TOKEN`；网页访问公网地址时由 WebView 跟随 Authelia 登录。APK 可从内网 `/shealth/static/shadow-health.apk` 下载。

## 7. 验收清单

- [ ] `https://health.example.com/` 跳转 Shadow Identity，登录后回到今日页
- [ ] 无 `health-users`/`shadow-admins` 组的账号无法进入
- [ ] `http://192.0.2.10:18080/shealth/` 仍可使用本地密码
- [ ] 缺少代理密钥的伪造身份头不能绕过登录
- [ ] `/healthz` 返回 200；数据库不可用时 `/readyz` 返回 503
- [ ] 手机同步、上秤、提醒和 Agent Bearer 通道不被 SSO 重定向
- [ ] 餐照上传与 AI 识别正常
- [ ] PostgreSQL 备份和 `uploads/photos/` 快照正常

## 8. 运维边界

- 禁止公开应用 `8080` 或 NAS `18080`；公网只走 ECS HTTPS + SSO。
- PostgreSQL 备份不包含餐照，`uploads/photos/` 必须进入 NAS 快照或异机备份。
- 升级前运行全量测试，部署后同时检查 `/healthz`、`/readyz` 和四类机器接口。
- SSO 故障时保留内网本地登录作为恢复入口，不临时公开应用端口。
