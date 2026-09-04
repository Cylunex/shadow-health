# Shadow Health MCP adapter v2

MCP 是 machine v1 合同的薄适配器，只读或创建草案；不持有正式写入权限，不连接数据库。
旧 `/api/agent/*` 和 `/api/ingest/agent` 已返回 410。设备上传仍使用自己的 ingest 凭据。

## 运行与迁移

```bash
uv sync --group mcp
uv run python -m mcp_server --stdio
# 或仅在回环地址运行 HTTP
uv run python -m mcp_server
```

| 配置 | 说明 |
| --- | --- |
| HEALTH_AGENT_TOKEN | 必填，独立 Health 机器主体凭据；不回退 INGEST_TOKEN |
| HEALTH_PROFILE_ID | 默认 primary，必须在主体 grants 中 |
| HEALTH_API_BASE | 默认 http://127.0.0.1:8080；远端必须由受控 HTTPS 配置提供 |
| MCP_PORT | 默认 8180，监听地址固定 127.0.0.1 |

配置由启动环境注入，不自动读取其他 Agent 或设备的密码。每个调用方单独启动实例/注入主体，
不可给模型进程发放 `health.records.write`。更新客户端工具清单和 skills 后再切换部署；旧工具名
不做隐式直写兼容。未在本机可见的生产调用方，仍须发布前逐一核对。

## 工具

- `query_today_summary(date)`：指定日期最小摘要。
- `query_metric_series(field, days)`：体重/睡眠/步数聚合趋势。
- `query_weekly_evidence(end)`：两个独立周窗口的比较与证据引用。
- `query_data_status()`：服务端接收状态，不推断蓝牙故障。
- `draft_record(record_type, effective_date, fields, idempotency_key)`：一餐/指标/运动草案。
- `draft_meal_update(row_id, effective_date, fields, idempotency_key)`：饮食修订草案。

日期始终明确；餐次是早餐/午餐/晚餐/加餐。一餐用 items 数组，不逐项生成草案。
稳定幂等键由调用方保存，同一次操作重试原样复用；用户确实再次吃一份则创建新的操作键。
不能靠进程内 60 秒缓存保证重启后的幂等。

草案回执不是落账。请引导用户到 Health 的健康助手或授权 Nexus Review 核对、确认。
无确认能力时不得调用隐藏提交路径、删除或绕用旧设备 token。旧完整上下文、习惯、
医疗原始值、任意 SQL 工具均不再提供。食物库检索目前保留在 Health 内置助手/手动页面，
MCP 不猜造 food_id；估算写明依据后交用户核对。

规范见 [插件合同](../docs/agent-plugin.md) 和 [实施说明](../docs/agent-workflows.md)。
