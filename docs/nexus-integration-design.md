# Health 接入 Nexus 的详细设计

设计版本：2026-09-07 / UA-1。状态：目标设计，尚未实现。公共身份、鉴权、Agent、模型、命令与回执以 [Platform 统一规范](https://github.com/Cylunex/shadow-platform/blob/main/docs/nexus-unified-access-design.md) 为准；本文仅定义本领域差异。旧接口安全限制在对应能力通过迁移验收前继续生效。

## 1. 当前基线与保留能力

基线 `3e59a9f`。`app/machine_auth.py` 维护 JSON registry/profile_grants；`app/oidc.py`、`app/auth.py` 维护登录兼容。`MachineHealthService`、`app/services/agent_drafts.py` 已有锁、目标快照、来源保护和实际结果；legacy Agent REST 已退役。持久复盘任务、证据、小行动、偏好、监测、设备 attempt 保留。

健康用户/Agent 身份、Session、profile 委托与凭据撤销迁入 Platform；Health 保留 profile 与中央 user_id 的映射、来源授权/可见性、记录范围及业务版本。单用户 profile 不能因中央支持多用户就推断已完成多租户隔离。

## 2. 迁出与适配

| 当前模块 | 目标 |
| --- | --- |
| `app/machine_auth.py` registry/parser/hash | 共享 Access middleware，适配旧 MachinePrincipal 的过渡只读结构 |
| `app/oidc.py` / `app/auth.py` | SDK 路由 + 中央 Session；旧代理/LAN 模式只在明确旧路由存活 |
| `mcp_server/` 独立凭据和工具循环 | 中央 MCP/Runtime，保留 handler/Schema 和领域说明 |
| `app/services/llm.py` transport/通用工具迭代 | 中央 Model/Agent Runtime；Health 保留营养提取模板和结果校验 |
| 健康助手审核页 | 普通记录直接完成，历史草案可读，高影响确认使用中央组件 |

profile_grants 转中央 delegation 的 exact profile ref + capabilities；过期/禁用记录不恢复为 active。设备只注册 device principal，不借 Agent record-write 权限复用同步；旧 INGEST_TOKEN 不成为通用认证回退。

## 3. 首批命令与读取合同

以下是拟议业务操作名，由 Manifest 映射具体 capability/operation_id，未实现前不加入在线工具目录。

| 操作 | 必要参数/版本 | 结果与约束 |
| --- | --- | --- |
| `metric.record` | profile_ref、date/timezone、metric、value/unit | committed metric ref + 实际值；只允许手工可写指标 |
| `meal.record` | profile_ref、日期、餐次、items(name/quantity/nutrients/source) | committed 单餐/批次 ref + item IDs；未知营养不填 0 |
| `meal.correct` | target_ref、expected_revision、修改字段 | before 引用和新版本；保护照片/原始来源 |
| `workout.record` | 日期、活动类型、时长/用户提供数值 | direct；不虚构消耗或距离 |
| `goal.set/update` | 指标/期限/用户目标、expected_revision | direct 普通目标；目标不替代医疗处方 |
| `review.run` | 7/30/90 天窗口、时区 | accepted task_ref，完成后返回 evidence_ref |
| `summary/trends/evidence.read` | profile、范围与指标允许列表 | covered/missing/source/observed_at，不推断诊断 |
| `data.status` | profile、可选 attempt_ref | observed/parsed/duplicate 等真实服务端状态 |
| `suggestion.feedback` | suggestion/episode、ignore/snooze/mute | ignore 本轮、mute 规则；不覆盖全局偏好 |

metric/meal 的 execute、普通 Web 保存、内置助手和兼容 MCP 统一进入应用服务；调用前 Access 检查、事务中 profile/source/target 检查。餐图仅存 Asset 固定版本引用；不能由 arbitrary URI 读图。

用户“大约 600 千卡”属于已给出的估算事实；不再要求确认，notes/structured provenance 保留不确定性。克数确实未知时 Schema 支持 unknown，不猜数通过校验；一个字段缺失不阻塞其他完整记录。

## 4. 结果与持久化

复用 `_target`、payload_hash、锁定写入和 `_result_ids/_result_uri`，提取 transaction-local command service。普通路径内部可保存不可变快照，但成功立即终结，不产生人工 pending。DietLog、BodyMetrics 等仍是事实真相，执行记录关联到实际资源/版本及 command_id。

对 Health 同日指标是更新聚合而非可无限 append：同 command 返回同结果；新 command 要按来源与预期版本正常更新，不把“新 ID”解释成重复插入同日指标。饮食多项在领域事务中原子保存；跨 Health/Ledger 不做原子事务。

状态查询既支持 command_id，也支持 task_ref；accepted 不称“周报已完成”。来源撤销后 Receipt 元数据仍可审计，但正文/证据读取重新鉴权。误填更正保留修订，不删除重建历史来源。

## 5. 数据与任务边界

确定性周复盘、来源指纹、等长窗口、缺失和过期判断仍在 Health worker；不为每个日汇总调用模型。目标/偏好/监测规则继续是 Health 业务事实，中央只统一其 Agent 授权，不迁移到另一套个人记忆库。

worker 的任务主键关联 central user/delegation/command；Agent 相关新副作用阶段检查撤销。已有设备原始消息接收与确定性标准化是独立 device/workload 能力，不与模型 loop 绑定。开秤 started、收到消息、parsed、duplicate 分开；measurement_time、received_time、attempt_id 不混用。

健康数据进入中央模型处理必须遵循 purpose/disclosure；原始医疗/敏感数据不因拥有 summary 权限全部开放。不建立第二套 provider fallback。

## 6. 迁移与体验

1. 盘点 profile/旧主体映射，先中央影子鉴权；不改健康事实主键。
2. 统一 Session + profile access，迁入 registry 授权后按能力切 central；已迁移路由拒绝旧 token。
3. 体重命令先试点，再单餐/睡眠/心情/目标；Nexus 首页仍由 Surface 渲染。
4. 助手入口使用统一 Runtime，删除通用聊天循环与永久审核要求；legacy REST 继续 410。
5. 原草案凭证/历史审核可读，旧 pending 不自动落账；已过期/已拒绝不复活。逐步移除本地 registry 配置与 Grant 编辑。

## 7. 必须验证

跨 profile/owner、撤销后读图/正文、设备字段覆盖、饮食修正版本冲突；同键异内容/并发/丢响应；单餐未知值；中央故障；实际读回；旧 Agent token 无法当 device credential；旧秤回调不覆盖当前 attempt。使用现有 `tests/test_agent_v5.py`、`test_agent_channel.py`、`test_shadow_plugin_contract.py` 的领域 fixture 扩展，不仅测试“能进入审核页”。
