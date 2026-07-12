# shadow-health MCP server

多 Agent（Hermes / OpenClaw / 未来其他）统一记录/查询入口（V3 批次 P2，
docs/subpath-agent-plan.md §2.3）。工具内部全部调本机 REST
（`/api/ingest/agent` 等，Bearer=INGEST_TOKEN），**不直连数据库**——校验、
归一化、幂等、import_raw 审计与手机离线通道完全一致。

## 运行

```bash
uv sync --group mcp                 # 依赖组（不进主依赖）
uv run python -m mcp_server         # streamable HTTP：127.0.0.1:8180/mcp
uv run python -m mcp_server --stdio # stdio 模式（客户端 spawn 子进程）
```

环境变量（都有默认，`.env` 里通常只需已有的 `INGEST_TOKEN`）：

| 变量 | 默认 | 说明 |
|---|---|---|
| `INGEST_TOKEN` | （必填，读仓库根 .env） | 调 shadow-health API 的 Bearer |
| `SHEALTH_API_BASE` | `http://127.0.0.1:8080` | shadow-health 地址（同容器回环） |
| `SHEALTH_MCP_HOST` / `SHEALTH_MCP_PORT` | `127.0.0.1` / `8180` | HTTP 模式监听 |

鉴权：**仅回环监听是主防线**（容器内 127.0.0.1，不经 nginx 不对外），
MCP 层未再加 Bearer。不要把监听改成 0.0.0.0。

## NAS 部署（supervisor，与 shadow-health 同容器）

```ini
[program:shealth-mcp]
directory=/data/project/shadow-health
command=/data/project/shadow-health/.venv/bin/python -m mcp_server
autostart=true
autorestart=true
stopasgroup=true
```

（`uv sync --group mcp` 先在 `/data/project/shadow-health` 跑一次；
supervisor/nginx 配置在容器可写层，改完备份到 `deploy/`。）

## Agent 注册

- **Hermes（native-mcp）与 OpenClaw**：注册 `http://127.0.0.1:8180/mcp`
  （streamable HTTP，两边共用同一实例、同一套审计）。
- stdio 场景（本地 spawn）：命令
  `/data/project/shadow-health/.venv/bin/python -m mcp_server --stdio`，
  workdir 仓库根（读 .env 用）。
- Hermes 旧 skill（直写 personal_data 裸 SQL）由 skill v2 替换：数据一律经
  MCP 工具；**禁止直连 PG**；保留「先和用户确认日期」铁律。cron 迁移
  （每日提醒播报 / 每周周报）在 NAS 侧做，不在本仓库。

## 工具集（9 个）

| 工具 | 说明 |
|---|---|
| `record_diet(items, meal, date?)` | 批量饮食条目（名称/克数/热量/蛋白/碳水/脂肪） |
| `record_weight(weight_kg?, body_fat_pct?, mood_score?, 围度…, date?)` | metric 通道，字段白名单，同日=覆盖更新 |
| `record_workout(type, duration_min, date?, distance_km?, calories?, rpe?, notes?)` | 手动训练 |
| `record_habit(habit_name, date?)` | 按名称匹配 active 习惯打卡（饮水也走这个） |
| `query_today_summary(date?)` | 当日全景（含 diet/workout 行 id，删除纠错要用） |
| `query_weekly_report(week?)` | 周报数据（YYYY-Wnn，缺省=上一完整周） |
| `search_food(keyword)` | 食物库（每 100g 营养，常吃优先） |
| `list_habits()` | active 习惯清单（供 record_habit 对名） |
| `delete_record(type, row_id)` | 改口纠错删除，仅 diet/workout；外部同步来源 403 |

`record_*` 回执统一为服务器原样计数 `{received, new, skipped, results}` +
`day_totals`（当日累计 kcal/蛋白/训练分钟，复述时不用再查一次）；
`results[].row_id` 即落库行 id（diet/workout），供 `delete_record`。

## skill 确认话术规则（反假确认，写进每个 agent 的 skill）

历史伤疤：Hermes 直写旧库时代反复发生「说已记录但没写库」与日期错记。规则：

1. **确认话术必须引用服务器回执的 `new` 计数**——「已入库 new=2（午餐：牛肉面、
   凉拌黄瓜），今日累计 1830 kcal / 蛋白 92 g」。没有拿到回执 = 没写成功，
   **禁止**输出任何「已记录」措辞。
2. `skipped > 0` 时如实说明（重复补发/当日已打卡），不得把 skipped 说成新记录。
3. 补记历史必须先向用户确认日期（YYYY-MM-DD），date 参数永远显式传。
4. 删除前用 `query_today_summary` 找到 row_id，向用户复述内容确认后再
   `delete_record`，并引用返回的 `summary` 复述删了什么。
5. 工具报错（API 4xx/5xx）原样告知用户，不得掩饰成成功。

## 同参数短窗去重（防超时重调双写）

写类工具（`record_*` / `delete_record`）在**进程内**做 60 秒同参数去重：
窗口内完全相同的参数直接返回上次回执（附 `"dedup": true`），不再二次 POST。

选「短窗去重」而非「client_id 按内容派生」的理由：内容派生的幂等键会把
真实的同日同参重复记录永久吞掉（下午又吃一份同样的加餐、同日两组同参数
训练都合法），而 agent 超时重调发生在秒级——60 秒窗口精确覆盖故障模式，
不误伤故意的重复。代价与兜底：进程重启后的重调不去重（HTTP 常驻模式进程
长活，可接受）；服务器 503 时工具自动**同 client_id** 原样重试一次，
配合服务端 parse_status 门控不丢不重。
