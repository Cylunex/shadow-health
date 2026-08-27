# Health Agent Plugin

Shadow Health 是独立领域应用，拥有健康记录、设备数据、授权、草案和审计。仓库内的 Shadow
Plugin 只声明运行时无关的远程合同；项目不发布领域 DSH npm 包，也不把业务代码移入 Platform。

首期普通 `shadow-health` Profile 提供最小日期摘要、7–90 天聚合趋势、可解释周度建议和
`pending` 健康记录草案。血压、血氧、化验原始值、完整导出、正式批量修正及不可逆执行
保持隐藏；没有 L4 能力。模型输出只能作为个人记录和趋势参考，不能描述为诊断或治疗结论。

Platform 校验 `shadow-plugin.yaml`、Manifest 和 OpenAPI 后，为独立 Health Profile 生成通用
DSH Bundle。DSH 使用 Health 专属 Bearer 直接访问本应用，Platform 不代理健康数据流量。

## 机器身份

新机器 API 只接受 `Authorization: Bearer`，不会回退到旧 `INGEST_TOKEN` 或
`X-Ingest-Token`。部署侧通过以下环境变量指向受限文件：

- `SHADOW_HEALTH_AGENT_REGISTRY_FILE`：JSON 注册表；
- `SHADOW_HEALTH_AGENT_SECRETS_DIR`：只读凭据摘要目录。

注册表为每个 Agent 分别声明 `audiences`、`scopes`、`profile_grants` 和相对摘要文件位置。
Profile grant 使用 `summary:read`、`trends:read`、`suggestions:read`、`drafts:create`；Nexus Review 的专用主体另需
`health.records.write` scope 与 `records:write` grant。正式写入能力在 Agent 目录中保持 hidden，
只由用户在 Nexus 明确确认后通过草稿 commit 端点调用。同一隐藏边界还允许 Nexus 列出同一
Agent、同一 Profile 的 pending 草稿，并将用户退回的草稿标记为 rejected。统一 Nexus Profile
只向模型提供 Health 读取能力；模型不能直接创建草稿。摘要文件只保存 Bearer Token
的 SHA-256 十六进制摘要；真实 Token、地址和生产注册表不进入仓库。

## 审计与幂等

授权后的读取、拒绝、草案创建和重放写入 `health.agent_machine_audit`，只记录请求 ID、Agent、
capability、Profile、结果和资源引用，不记录请求正文或健康数值。草案要求 `Idempotency-Key`；
同键同内容返回原草案，同键不同内容返回稳定的 `idempotency_conflict`。

Nexus 只缓存审核所需的字段快照和 `shadow://health/drafts/{id}` 引用，不迁移健康草稿或正式记录
的所有权。确认时提交原草稿，退回操作可安全重放；浏览器用户自己创建、且不属于该 Agent 的草稿
不会进入 Nexus 队列。

旧 MCP 可继续服务现有本机工作流，但不是插件合同真相，也不被 DSH Builder 编译。

## 周度建议边界

`health.suggestions.read` 只聚合最近 7 天已确认数据，输出稳定周键、理由、证据 URI、缺失率、
置信度和有效期；不返回逐日原始序列。数据不足时建议明确降级为“先补齐记录”，不会用缺失值
推导健康结论。Nexus 可查看证据、生成待审核调整草稿、稍后提醒、忽略或静音，但建议本身不
写入 Health，也不构成诊断或治疗意见。
