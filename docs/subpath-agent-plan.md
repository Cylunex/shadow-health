# V3 批次计划：子路径适配 + 多 Agent MCP 接入 + 旧库迁移

> 2026-07-12 定稿。**本文档是跨机器开发的执行计划**：在另一台机器 pull 后据此开发，无需本机记忆。
> 决策已由用户拍板，不要重新讨论：
> 1. 前缀用 **`/shealth`**；nginx 只允许 `/stock/` 同款配置（路径前缀 + `X-Forwarded-Prefix`），**不占根路径、不占独立端口**
> 2. **mood_score（心情分）加列**，随迁移一起进快记表单
> 3. **全部弄完再切**：生产暂维持临时态（见 §0），本批验收通过后一次性切换
> 4. **数据迁移放本批最后**（Phase 3）
> 5. 多 Agent（Hermes / OpenClaw / 未来其他）走**完善的 MCP 方案**（Phase 2）
> 6. Health Connect webhook 路线已废弃删除（国行三星健康不写 HC），文档已清理，勿再引用

## 0 ｜ 当前生产形态（开发前必读）

- **部署**：NAS 上的 agent 容器内，supervisor 管理（非 Docker Compose）。
  代码 `/data/project/shadow-health`，`uv sync` 独立 Python 3.12 venv，
  `[program:shadow-health]` 起 `uvicorn app.main:app --host 0.0.0.0 --port 8080`（容器内）。
  supervisor/nginx 配置在**容器可写层**（容器重建会重置），整份备份在 `/data/project/shadow-health/deploy/`。
- **端口约束**：容器只发布 `55080→80`、`55443→443`（闲置）、`55022→22`、（PG 另容器 `55432`）。
  8080 无宿主机映射，一切入口都走 nginx :80。
- **临时态（本批上线时回滚）**：nginx 当前让 shadow-health 占了根路径、服务面板挪到了 `/panel`。
  切换时恢复：面板回根 `location = /`，shadow-health 挂 `/shealth/`（§1.8 有完整 nginx 段）。
- **数据库**：生产库 `shadow_health`（schema `health`）已建成并灌满（三星 7 年 + Keep + seed），
  迁移版本 `20260711u11`。开发机本地 `.env` 也指向生产库——**本地预览与生产是同一个库**。
- **凭据**：一切凭据只在两端 `.env`（不入仓库）；服务器 SSH/DB 凭据不入仓库，问用户要。

## Phase 1 ｜ 子路径前缀适配（`/shealth`）✅ 已落地（2026-07-12）

**目标**：应用在 `http://<NAS_IP>:55080/shealth/` 完整可用；开发环境（无前缀）行为零变化。

> 实施与验收记录（代码为准）：
> - **中间件**（main.forwarded_prefix）：读 `X-Forwarded-Prefix` → 存
>   `scope['x_forwarded_prefix']`（**没用 root_path**：Starlette 的
>   Mount(StaticFiles) 会把 root_path 组合进子 scope，而 path 又不含前缀，
>   静态文件解析会错位 404——私有键与框架零交互，行为即 §1.1 的
>   「只有 URL 生成需要加前缀」）；脏头（非 / 开头）不生效，尾斜杠剥掉
> - **URL 生成**：模板全局 `u('/xxx')`（jinja pass_context 从 request 取前缀）
>   ≈190 处属性机械替换（href/src/action/hx-get/hx-post/hx-put/hx-delete/
>   hx-push-url + base.html/more.html 的 `u(href)` 循环变量）；Python 侧
>   deps.prefixed/redirect（登录/登出/review 303/imports 303 + HX-Redirect）
> - **防返工锁**：tests/test_template_urls.py——lint 扫描 templates/ 禁裸绝对
>   路径属性（白名单 //、http、{{）+ 前缀中间件回归 6 例（Location/HX-Redirect/
>   静态引用/脏头消毒/无头零变化）
> - **内联 JS**：sw 注册、日期跳转 location.href、Chart.js 动态加载、照片灯箱
>   均模板化；`<body data-root>` 备通用注入点；manifest.json 改相对路径、
>   sw.js 从自身 URL 反推前缀（SW v13）——PWA 仅 dev 生效，非验收项
> - **cookie**：登录 cookie path=前缀（无前缀仍 /），登出同 path 删除
> - **安卓壳**：SnapshotCache 从 serverUrl 解析 path 前缀，请求 path 剥前缀后
>   再做 cacheable/排除匹配，同域不在前缀下的请求不代理；其余拼接点
>   （MainActivity/SamsungSyncWorker/ScaleScanService/OfflineStore/Reminders/
>   gateway）核对均为 `baseUrl + "/api/..."` 形式，无需改
> - 验收：125 pytest 全绿；本地 X-Forwarded-Prefix 代理走查全页面
>   （登录/登出/今日/指标/饮食/训练/习惯/报告 tab 切换/设置/scale/更多 +
>   htmx 片段、Chart.js、hx-push-url，全部请求都在 /shealth/ 下）；
>   APK 构建通过并拷至 static/shadow-health.apk（真机回归待用户）

### 1.1 前缀感知中间件（机制层，先做）

ASGI 中间件读 `X-Forwarded-Prefix` 头 → 写入 `request.scope["root_path"]`；无该头时为空。
不用 uvicorn `--root-path`（同一份进程要同时服务有/无前缀两种形态，header 驱动更干净）。
nginx 侧 `proxy_pass http://127.0.0.1:8080/;`（带斜杠剥前缀），app 收到的 path 不含前缀，
**只有 URL 生成需要加前缀**。

### 1.2 模板 URL 全量改造（工作量大头，~50 个模板）

- Jinja 全局助手：`u(path)` = `request.scope['root_path'] + path`（或注入 `root` 变量拼接，二选一，全库统一）。
- 机械替换所有 `href="/..."` `src="/..."` `action="/..."` `hx-get="/..."` `hx-post="/..."`
  `hx-put="/..."` `hx-delete="/..."` `hx-push-url="/..."`。
- **防回归测试（必加）**：一个 pytest 扫描 templates/ 断言不再出现裸绝对路径属性
  （白名单：`//` 开头、`http` 开头、`{{` 已模板化的）。这是本阶段不返工的关键。

### 1.3 路由层重定向

grep 全部 `RedirectResponse(` 与 `HX-Redirect` / `HX-Location` 响应头，统一走带 request 的助手
（内部拼 root_path）。登录/登出/303 流程重点回归。

### 1.4 模板内 JS 与静态清单

逐个过：base.html 内联脚本、图表片段（`/fragments/metrics/chart` 等 URL 构造）、离线队列 JS、
拍照上传、`manifest.json`（start_url/icons；PWA 仅 dev-localhost 生效，改了但不作验收项）、
`sw.js`（同上）。JS 里从 `<body data-root="{{ ... }}">` 之类注入点取前缀。

### 1.5 安卓壳 URL 拼接点核对

壳约定：**base URL 含前缀**（`http://<NAS_IP>:55080/shealth`），所有拼接必须是字符串
`baseUrl + "/api/..."`，**不许用 Uri.Builder.path()（会覆盖前缀）**。逐点核对：
- `MainActivity`（WebView 加载、错误页改地址、cookie 域）
- `SamsungSyncWorker`（`/api/ingest/samsung_direct`）
- `ScaleScanService`（`/api/ingest/miscale`）
- 离线队列补发（`/api/ingest/offline`）与 `GET /api/offline/bootstrap`
- 本地启动页内链

### 1.6 会话 cookie path

登录 cookie `path` 收窄到前缀（避免漏给同域名下 `/stock/` 等），无前缀时仍为 `/`。

### 1.7 验收清单

- [x] dev 无前缀全量回归（125 测试全绿 + 无头请求行为零变化）
- [x] 模板裸 URL lint 测试通过（tests/test_template_urls.py）
- [x] 本地起一个带 `X-Forwarded-Prefix: /shealth` 的代理模拟走查全页面
- [ ] 真机：壳指向前缀地址，登录/打卡/拍照/秤/三星同步全链路（用户做）
- [x] `/shealth` 不带尾斜杠 301 到 `/shealth/`（nginx 段已含，代理模拟已验）

### 1.8 上线切换（弄完一次性做）

1. 服务器 `git pull` → `uv sync --frozen --no-dev`（如依赖有变）→ `supervisorctl restart shadow-health`
2. 替换 nginx `/etc/nginx/conf.d/default.conf`（先备份到 `/data/project/shadow-health/deploy/`）：
   - 面板恢复 `location = /`（links: /openclaw/ /stock/ **/shealth/**）
   - 删除临时的 `location / → 8080` 整站段与 `/panel`
   - 新增（与 /stock/ 同款）：
     ```nginx
     location = /shealth { return 301 /shealth/; }
     location /shealth/ {
         proxy_pass http://127.0.0.1:8080/;
         proxy_http_version 1.1;
         proxy_set_header Host $host;
         proxy_set_header X-Real-IP $remote_addr;
         proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
         proxy_set_header X-Forwarded-Prefix /shealth;
         proxy_read_timeout 300;
         proxy_send_timeout 300;
         client_max_body_size 20m;   # 餐次拍照
     }
     ```
   - 保留 `location = /health`（容器健康检查，精确匹配）
3. `nginx -t && nginx -s reload`
4. 手机端 base URL 改为 `http://<NAS_IP>:55080/shealth`
5. 验收：`/shealth/healthz`=ok、登录走查、`/stock/`、`/openclaw/`、面板均正常

## Phase 2 ｜ 多 Agent 接入（REST 通道 + MCP server）✅ 已落地（2026-07-12）

> 实施与验收记录（含用户拍板的增补，代码为准）：
> - **迁移 12**（`20260712u12`）：`ck_import_source` 加 'agent'/'legacy' +
>   `body_metrics.mood_score smallint CHECK 1~10`（downgrade 先清违规留档行）；
>   mood_score 进 metrics 页 `_FIELD_DEFS`/快记/完整表单/历史表格，离线与 agent
>   的 metric 白名单自动继承
> - **`POST /api/ingest/agent`**：offline 管线抽成 `ingest_batch(source=...)` 后的
>   薄别名（source='agent'，workout external_id='agent-{client_id}'）；响应附
>   per-record 明细 `results:[{client_id,status,row_id}]`（status ∈ new/skipped/
>   failed，failed 另带 error；diet/workout 的 new 带落库行 id），顶层保持
>   {received,new,skipped} 兼容
> - **读端点**：`GET /api/agent/summary?date=`（当日饮食汇总+明细带行 id/步数/
>   训练带行 id/体重/心情/打卡完成度，复用 diet._summary_ctx 与 report._habit_checklist；
>   date 非法 400 不静默回退）；`GET /api/agent/report/weekly?week=YYYY-Wnn`
>   （review._aggregate_week 同一查询，缺省=上一完整周，带 complete 标记）；
>   计划外补了 `GET /api/agent/foods?q=`（search_food 工具需要 Bearer 版食物搜索）
> - **删除**：`POST /api/agent/delete {type,row_id}`（仅 diet/workout；workout
>   仅 source='manual' 可删，外部同步来源 403）+ MCP 第 9 工具 delete_record
> - **mcp_server/**：FastMCP（mcp>=1.9，`[dependency-groups] mcp`），
>   streamable HTTP 127.0.0.1:8180(/mcp) + `--stdio` 双模式；9 工具全调本机 REST；
>   record_* 附当日累计 day_totals；写类工具**同参数 60s 短窗去重**（选短窗
>   而非内容派生 client_id 的理由见 mcp_server/README）+ 503 同 client_id
>   重试一次；话术规则（确认必须引用 new 计数）写进 README
> - 验收：125 pytest 全绿（新增 tests/test_agent_channel.py 22 个：幂等重放/
>   词表/mood 白名单与界值/summary 口径/delete 权限边界/去重窗）+ stdio 实调
>   全 9 工具 record→query→delete 18 项检查全过
> - §2.4（Hermes skill v2 / OpenClaw 注册 / cron 迁移）在 NAS 侧做，
>   配置说明见 mcp_server/README

**背景**：用户此前通过 Hermes agent 聊天记录饮食/体重/运动，Hermes 直接向旧库 `personal_data`
裸写 SQL（伤疤：假确认「说已记录但没写库」反复发生、日期错记）。本阶段让 Hermes/OpenClaw
等一切 agent 通过统一接口写入 shadow-health，**从协议上杜绝假确认**。

### 2.1 REST 写通道 `/api/ingest/agent`

- 薄别名复用 `app/routers/offline.py` 的归一化逻辑（habit/diet/workout/metric 四类、
  client_id 幂等、import_raw 留档、Bearer=INGEST_TOKEN），source 用新词 `'agent'`
  （迁移：import_raw 来源词表 + sync_state 加 'agent'），便于导入中心区分「手机补发」和「agent 写入」。
- 响应保持 `{received, new, skipped}`。

### 2.2 读端点（供查询工具与周报）

- `GET /api/agent/summary?date=YYYY-MM-DD`（Bearer）：当日饮食汇总/步数/训练/体重/打卡完成度，紧凑 JSON
- `GET /api/agent/report/weekly?week=YYYY-Wnn`（Bearer）：周报数据（复用报告中心的汇总查询）

### 2.3 MCP server（完善方案，多 agent 共用）

- **形态**：官方 `mcp` python SDK（FastMCP），**streamable HTTP** 常驻服务，
  supervisor `[program:shealth-mcp]`，监听 `127.0.0.1:8180`（容器内回环，不经 nginx 不对外）；
  同一入口支持 `--stdio` 模式供本地 spawn 场景。Hermes（native-mcp）与 OpenClaw 都注册
  `http://127.0.0.1:8180/mcp`。
- **鉴权**：仅回环监听为主防线；可选 Bearer（复用 INGEST_TOKEN）。
- **代码位置**：仓库 `mcp_server/`，依赖放 `[dependency-groups] mcp`（不进主依赖）。
  工具内部全部调本机 REST（`http://127.0.0.1:8080/api/...`，token 从 `.env` 读）——
  **不直连数据库**，校验/归一化/审计与其他通道完全一致。
- **工具集（8 个，宁少勿滥）**：

| 工具 | 说明 |
|---|---|
| `record_diet(items, meal, date?)` | 批量饮食条目（名称/克数/热量/蛋白/碳水/脂肪） |
| `record_weight(weight_kg?, body_fat_pct?, mood_score?, 围度…, date?)` | 走 metric 类型，字段白名单 |
| `record_workout(type, duration_min, date?, distance_km?, calories?, notes?)` | 手动训练 |
| `record_habit(habit_name, date?)` | 按名称匹配 active 习惯打卡（饮水也走这个） |
| `query_today_summary(date?)` | 当日全景（记录前查重/记录后复述都用它） |
| `query_weekly_report(week?)` | 周报数据 |
| `search_food(keyword)` | 查食物库（热量估算辅助） |
| `list_habits()` | active 习惯清单（供 record_habit 对名） |

- **幂等与真实性**：工具内部生成 client_id（UUID）；返回原样透传 `{new, skipped}`。
  **skill 规则：agent 确认话术必须引用返回计数**（"已入库 new=3"），没有计数=没写成功，
  从机制上消灭假确认。

### 2.4 Hermes skill v2 + OpenClaw 注册

- 重写 `personal-health-tracking`：数据一律经 MCP 工具（或兜底 curl `/api/ingest/agent`）；
  保留「先 `date` 确认日期」铁律；**禁止直连 PG**；确认必须引用 new 计数。
- OpenClaw 注册同一 MCP server；两边共用同一套工具与审计。
- Hermes 两个 cron 迁移：每日记录提醒（改为提醒+query_today_summary 播报）；
  每周健康周报（query_weekly_report 生成，替换旧 weekly-report-generator.py 直读 personal_data）。

## Phase 3 ｜ personal_data 旧库迁移（本批最后做）

### 3.1 旧库实测盘点（2026-07-12，public schema，10 表）

| 表 | 行数 | 范围 | 关键列 | 迁移决策 |
|---|---|---|---|---|
| diet_records | 301 | 2026-05-25 ~ 07-09 | meal_type, food_name, calories, protein_g, carbs_g, fat_g, portion_desc, notes | **迁** → diet_logs：free_text=food_name(+portion_desc)，kcal/protein/carbs/fat 直映，meal_type 对 CHECK 词表 |
| weight_records | 83 | 2020-06-15 ~ 2026-07-11 | weight_kg NOT NULL, body_fat_pct, bmi, notes | **迁** → body_metrics 按 log_date upsert，手动优先（可覆盖三星 autofill 同日值）；bmi 不迁（app 自算） |
| activity_records | 35 | 2026-05-25 ~ 07-09 | activity_type, duration_minutes, calories_burned, intensity, distance_km, steps, avg_hr, max_hr, pace_kmh, notes | **迁** → workout_logs source='manual'、external_id='legacy-{id}'（天然幂等）；steps/intensity/pace 并入 notes 或 detail |
| daily_summary | 21 | 2026-05-27 ~ 07-09 | total_* 宏量汇总, target_calories, **mood_score**, notes | mood_score → **新列 body_metrics.mood_score**（见 3.2）；target_calories→app_settings.target_kcal（仅当现值为空）；宏量汇总不迁（app 自算） |
| daily_drinks | 34 | 2026-05-25 ~ 07-10 | water_ml, notes | **迁打卡**：water_ml>0 的日期 → 「饮水」习惯 habit_logs（按名称匹配现有 active 习惯；无匹配则跳过并在报告列出） |
| body_measurements | 1 | 2026-05-27 | waist/hip/chest/arm/thigh/neck_cm | **迁** → body_metrics 围度列（chest/arm/thigh/hip 已有；waist/neck 若无对应列→并入当日 notes，开发时核对列集再定） |
| personal_info | 1 | — | gender, birth_date, height_cm | **迁** → app_settings 体成分档案（sex/birth_date；height 已由三星导入回填则跳过） |
| exercise_summary | 3 | 2026-05-25 | 年度汇总 | 不迁（派生数据，app 自算） |
| monthly_activity | 28 | 2024-02 ~ 2026-05 | 月度总时长 | 默认不迁（无原始明细支撑；三星 7 年历史已覆盖同期。如在意可 import_raw 留档） |
| life_memories | 8 | 2026-05-27 ~ 05-28 | 生活片段 | 不迁（非健康数据，留 personal_data） |

### 3.2 前置：mood_score 加列（用户已拍板）

- 迁移：`body_metrics` 加 `mood_score smallint`（CHECK 1~10）
- metrics 页快记表单 + `_FIELD_DEFS` 白名单 + 图表可选序列；agent 的 record_weight 工具带上

### 3.3 迁移脚本

- `scripts/migrate_personal_data.py`：一次性、幂等可重跑
  （postgres 读 personal_data.public → health_app 写 shadow_health.health；
  全部先落 import_raw source='legacy' 留档，diet 查重口径=同日+同文本+同 kcal 跳过）
- 跑完输出迁移报告（各表 迁入/跳过/无匹配 计数）

### 3.4 收尾冻结

- personal_data 各健康表 REVOKE INSERT/UPDATE/DELETE（或改名 `*_frozen`）——防旧链路复活双写
- 删除 Hermes 旧 skill 中直写 personal_data 的全部脚本与说明（skill v2 已替代）

## 里程碑与顺序

1. ~~**P1 子路径适配**~~ ✅ 已落地（见 §1 落地记录）；真机回归待用户
2. ~~**P2 REST+MCP+skill**~~ ✅ 服务端+MCP server 已落地（见 §2 落地记录）；
   Hermes/OpenClaw 注册与试记在 NAS 侧做
3. **切换上线**（§1.8：nginx 切 /shealth/、面板回根、手机 base URL 更新）
4. **P3 迁移+冻结** → 迁移报告核对、personal_data 冻结
5. 全程结束后：更新 README 部署段、docs/mobile-sync.md 的 base URL 说明

## 明确不做 / 已废弃

- Health Connect webhook 路线（含第三方 App 修复版）：已废弃，文档已清理，本地 clone 与 apk 可手动删除
- personal_data 继续写入：Phase 3 后冻结
- nginx 根路径/独立端口方案：临时态，P1 上线时回滚
