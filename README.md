# Shadow Health

Shadow Health 是面向个人长期使用的健康记录与趋势中心。它统一饮食、体重、训练、体征、习惯
和设备数据，帮助用户理解自己的变化，而不是替代医生诊断或医疗系统。

## 理念

- 原始记录、用户确认和自动分析明确分层；
- 健康事实长期可追溯，设备或模型结果不能静默覆盖用户数据；
- 浏览器、Android 原生能力和 Agent 使用不同身份与最小权限；
- 与 Ledger、Travel 等项目联动时只交换必要的业务引用。

## 主要功能

- 饮食、体重、运动、体征、饮水和习惯记录；
- 趋势、日报、目标和提醒；
- 小米体脂秤与三星健康数据接入；
- OIDC Web 会话、机器同步接口和 MCP 能力；
- Android 离线队列及失败补偿；
- PostgreSQL 迁移、导入和审计基础。

## 本地开发

```bash
uv sync
cp .env.example .env
uv run alembic upgrade head
uv run uvicorn app.main:app --reload
```

`.env` 只用于本地实际配置并保持忽略；仓库只保留字段示例。

## 文档

领域说明、接口合同和迁移脚本位于 [docs](docs/)、[migrations](migrations/) 与
[scripts](scripts/)。Shadow Agent/DSH 接入边界见 [Health Agent Plugin](docs/agent-plugin.md)。
医疗相关输出仅供个人记录和参考。
