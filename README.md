# shadow-health

单用户数据模型、局域网优先的健康、饮食和运动管理 Web 应用，浏览器统一使用 Shadow Platform SSO。

四大模块：**饮食记录+营养分析 · 运动训练管理 · 身体指标追踪 · 养生任务打卡**，外加三星健康历史数据一次性导入、Health Connect 增量同步、小米体脂秤 2 / S400 蓝牙直连（上秤即记录，见 [S400 适配说明](docs/miscale-s400.md)）。

亮点：餐次拍照 + **AI 识别热量**（可独立配置视觉模型）· 自定义食物库 · 身体围度追踪 · 图表目标参考线 · 睡眠分期堆叠图 · 习惯/训练打卡热力图 · 训练周负荷（sRPE）· HIIT/组间计时器 · 累计成就 · 每日提醒 · 报告中心（日报/周报/月报 + 手写复盘）· AI 分析与**能动手记录的 AI 问答**（Claude/OpenAI 双通道 tool use）· PWA 离线可用。

**多 Agent 通道**：外部 AI Agent（Hermes/OpenClaw 等）经 [MCP server](mcp_server/README.md)（16 工具）或 REST（`/api/ingest/agent` + `/api/agent/*`，Bearer）读写数据——写入全部幂等留档（`import_raw`），`/agent-log` 页可核对/撤销，写入按 `agent_name` 归属展示；内置 AI 问答走同一通道同一审计。

> `app/seed/data/*.md` 训练计划内容素材为私人整理，不入仓库；缺失时 seed 自动跳过对应计划，说明见 [app/seed/data/README.md](app/seed/data/README.md)。
> 另有 **5 套内置通用计划**（徒手入门/减脂 HIIT/久坐修复/核心强化/我的·常用全身循环，`app/seed/plans_builtin.py`）与 **68 个动作的动作库**（`/workout/exercises`，含要领与进阶链），不依赖私人素材、开箱即用。

## 技术栈

Python 3.12（uv 锁定）· FastAPI · SQLAlchemy 2.x · Alembic · PostgreSQL（schema `health`）· Jinja2 + HTMX + Alpine.js + Chart.js + Tailwind（standalone CLI，静态资源全本地化，断外网可用）

## 本地开发

```bash
# 1. 依赖（uv 自动使用 .python-version 指定的 3.12）
python -m uv sync

# 2. 开发数据库
docker compose -f docker-compose.dev.yml up -d

# 3. 配置：复制 .env.example 为 .env，填写 Platform SSO、数据库和机器接口密钥
# 本地页面调试也需用代理注入 Remote-* 与 X-Shadow-Proxy-Secret；
# SHADOW_PROXY_AUTH_SECRET 可供本地测试，生产只能使用受限密钥文件。

# 4. 迁移 + seed
python -m uv run alembic upgrade head
python -m uv run python -m app.seed

# 5. 三星历史数据导入（可选，一次性；zip 为三星健康 App 官方导出）
python -m uv run python -m app.importers.samsung_zip <路径>\SamsungHealth.zip --dry-run
python -m uv run python -m app.importers.samsung_zip <路径>\SamsungHealth.zip

# 6. 起服务
python -m uv run uvicorn app.main:app --reload --port 8801
```

前端样式改动后在 Git Bash 重建 CSS：`./tools/tailwindcss.exe -c tailwind.config.js -i static/src/input.css -o static/app.css --minify`
（`tools/` 不进 git，CLI 从 [tailwindcss releases](https://github.com/tailwindlabs/tailwindcss/releases/tag/v3.4.17) 下载；
macOS/Linux 可直接 `npx -y tailwindcss@3.4.17 -c tailwind.config.js -i static/src/input.css -o static/app.css --minify`）。
注意：`static/*` 变更后要同步升级 `static/sw.js` 的 `SW_VERSION`，否则老客户端 Service Worker cache-first 会一直用旧资源。

## 部署（局域网 Debian 主机）

生产配置、网络地址和凭据不进入仓库。部署时从示例生成本地配置，并按
[部署手册](docs/deploy.md) 完成数据库、反向代理、备份和验收。

## 外部数据

| 通道 | 状态 | 说明 |
|---|---|---|
| 三星 zip 历史导入 | ✅ CLI + Web 上传 | `app/importers/samsung_zip.py`，幂等可重跑 |
| 三星健康直读（手表） | ✅ 双端 | `POST /api/ingest/samsung_direct` + Android 壳内置 Data SDK 直读（每小时增量，见 [docs/mobile-sync.md](docs/mobile-sync.md)）；国行三星健康不写 HC，直读绕过 |
| Health Connect webhook | ⚠️ 接收端保留 | `POST /api/ingest/health_connect`；实测国行三星健康不向 HC 写数据，通道已被上行直读取代 |
| Keep 文件导入 | ✅ CLI + Web 上传 | `app/importers/keep_file.py`，支持 .7z / .zip（AES 密码）/ .xlsx，跨源去重；.fit 为占位 stub 只清点不导入 |
| Keep API 同步 | ⏳ 暂缓 | 看过 Keep xlsx 内容后再决定是否值得做 |
| 小米体脂秤 2 / S400 BLE | ✅ 双监听端 | `POST /api/ingest/miscale`；NAS 网关 + Android 壳双监听，S400 支持 MiBeacon v5 加密、双频阻抗与心率原始值留档，见 [docs/miscale-s400.md](docs/miscale-s400.md) |
