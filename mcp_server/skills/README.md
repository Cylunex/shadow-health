# Agent skills（配合 shadow-health MCP 16 工具使用）

给接入 MCP 的外部 Agent（Hermes / OpenClaw / 其他）用的技能文档——即
mcp_server/README「skill v2」的实体。每个文件自包含：触发场景、工具调用
顺序、话术铁律、失败处理，可直接作为 agent 的 skill/system prompt 片段挂载。

| 文件 | 技能 | 触发 |
|---|---|---|
| recorder.md | 健康记录员 | 「记一下/帮我记/吃了/练了/体重…」以及改口纠错 |
| morning-briefing.md | 晨间简报 | 每日晨间 cron 或「今天状态怎么样/该练什么」 |
| weekly-review.md | 周/月复盘播报 | 每周一 cron 或「上周/上个月怎么样」 |
| analyst.md | 数据分析问答 | 趋势类提问「体重最近怎么变/睡眠有没有改善」与深度分析 |

## 挂载方式

- **Hermes / OpenClaw**：注册 MCP `http://127.0.0.1:8180/mcp` 后，把对应
  skill 文件内容加入该 agent 的技能库/系统提示。四个可以全挂（触发词不冲突）。
- **cron 场景**（晨间简报/周报播报）：cron 只发触发语（如「播报晨间简报」），
  技能文档负责其余流程。

## 所有技能共用的铁律（历史伤疤，每个 skill 里也各自重申）

1. 写入确认必须引用服务器回执的 `new` 计数；没有回执 = 没写成功，禁止说「已记录」。
2. `skipped > 0` 如实说明，不得说成新记录。
3. 补记历史必须先向用户确认日期（YYYY-MM-DD），date 参数永远显式传。
4. 删除/修改前先复述内容并得到确认；工具报错原样转述，不掩饰成成功。
5. **禁止直连 PG**：数据一律经 MCP 工具（旧库 personal_data 已冻结）。
