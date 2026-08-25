# Nexus 联邦审核边界

状态：已接受，2026-08-25。

统一 Shadow Nexus Profile 不再选择 `health.records.draft`。模型只读取健康摘要与趋势，并向 Nexus
返回结构化 Proposal。Nexus Host 使用隐藏的 `health.records.write` 边界创建/提交自身 Proposal，
也可以列出同一机器主体此前产生的 pending 草稿，再以引用方式放入全局 Review。

领域草稿仍存放在 Health。Nexus 缓存的只是审核快照、来源和
`shadow://health/drafts/{draft_id}`；确认提交原草稿，退回把原草稿标记为 rejected。所有接口同时
验证机器 scope、Profile grant、Agent ID 与 Profile ID，模型和其他 Agent 无法调用或枚举这些草稿。
