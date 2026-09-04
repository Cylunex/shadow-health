# Shadow Health

Shadow Health 是面向个人长期使用的健康记录与趋势中心。它统一饮食、体重、训练、体征、习惯
和设备数据，帮助用户理解自己的变化，而不是替代医生诊断或医疗系统。

## 理念

- 原始记录、用户确认和自动分析明确分层；
- 健康事实长期可追溯，设备或模型结果不能静默覆盖用户数据；
- 浏览器、Android 原生能力和 Agent 使用不同身份与最小权限；
- 与 Ledger、Travel 等项目联动时只交换必要的业务引用。

## 主要功能

- 饮食、体重、运动、体征、饮水和习惯记录；饮食名称与人类可读备注独立保存；
- 活动消耗统一取设备日汇总与当日训练热量的较大值，缺失时自动回退且不重复相加；
- 本地餐照与 Platform Asset 餐图统一展示，跨应用私有图片通过显式委派和独立 Health 凭据访问；
- 趋势、日报、目标和提醒；
- 小米体脂秤与三星健康数据接入；
- OIDC Web 会话、机器同步接口和 MCP 能力；
- Android 离线队列及失败补偿；
- PostgreSQL 迁移、导入和审计基础。
- 健康助手：一餐草案核对、可信回执、来源证据复盘、持久任务、小行动与显式偏好；
- 默认关闭的站内主动整理，先影子观察再启用；首页开秤显示本次接收与重复状态。

## Nexus 快捷操作

Health 通过声明式 Surface 向 Nexus 提供体重、睡眠、步数和运动时长摘要，并上浮称重、睡眠、
心情三个高频动作。Nexus 只收集字段并调用现有 Health Review 接口；Health 仍负责范围校验、
幂等、最终写入和回执，完整健康页面与设备管理继续留在本项目。

## 本地开发

```bash
uv sync
cp .env.example .env
uv run alembic upgrade head
uv run uvicorn app.main:app --reload
# 另一个终端启动领域任务执行器
uv run python -m scripts.health_worker
```

`.env` 只用于本地实际配置并保持忽略；仓库只保留字段示例。

## 文档

领域说明、接口合同和迁移脚本位于 [docs](docs/)、[migrations](migrations/) 与
[scripts](scripts/)。Shadow Agent/DSH 接入边界见 [Health Agent Plugin](docs/agent-plugin.md)。
长期记录身份、Health Connect 游标、覆盖/来源证据与 Platform 恢复验证见
[长期健康数据底座](docs/health-data-foundation.md)。
医疗相关输出仅供个人记录和参考。

本轮 Agent 改造、迁移、MCP 凭据切换和 APK 兼容性见 [工作流实施说明](docs/agent-workflows.md)。
