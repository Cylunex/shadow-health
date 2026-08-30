# 长期健康数据底座

Shadow Health 是个人健康记录与趋势工具，不提供医疗诊断。长期数据的可信度来自可追溯的
来源、稳定的记录身份、明确的覆盖范围和可恢复性，而不是更多小数位。

## 记录身份与来源证据

所有采集通道继续先写 `health.import_raw`。迁移 `20260830u18` 为它增加统一证据字段：

- `client_record_id` 是 `external_id` 的兼容生成列；新接入应把设备/客户端的稳定记录 ID
  写入 `external_id`，不要使用传输批次 ID；
- `record_version` 是同一记录的单调版本；低版本忽略，同版本同 payload 视为幂等重发，
  同版本不同 payload 标为失败并隔离，高版本才允许替换；
- `payload_hash` 用于版本冲突检测；`provenance` 只保存通道、来源 App、设备厂商/型号和
  recording method 等值无关元数据；
- `normalized` 保存当前版本的最小归一化定位信息，用于修订时重建受影响日期。

Health Connect 优先使用 `metadata.clientRecordId` / `clientRecordVersion`，其次才使用
provider record id。没有稳定 ID 时使用规范 JSON 的确定性摘要，因此同一 payload 可幂等重放，
但无法假装识别上游未提供身份的“更新”。

步数、体重和睡眠修订不会再叠加旧版本：步数按当前全部 source record 重建日合计，体重按
当日最后测量重建，睡眠按跨源优先规则重算。手动字段仍有最高保护，不被设备回填覆盖。

## Health Connect 增量边界

`health.sync_cursors` 以 `(source, record_type)` 为主键。`steps`、`weight`、`sleep`、
`exercise`、`heart_rate` 的 cursor、历史 watermark、权限状态和来源指纹彼此独立。迁移会把旧
`sync_state('health_connect').watermark` 复制为每个类型的初始历史边界；之后 opaque cursor
不跨类型比较。

客户端可在现有 `POST /api/ingest/health_connect` payload 中附加：

```json
{
  "records": [],
  "sync": [
    {
      "record_type": "steps",
      "cursor": "OPAQUE_NEXT_CURSOR",
      "permission": "granted",
      "source_fingerprint": "INSTALLED_ORIGINS_HASH"
    },
    {
      "record_type": "sleep",
      "permission": "revoked"
    }
  ]
}
```

权限为 `denied`/`revoked` 时清除该类型 cursor 并置 `needs_resync`，不影响其他类型。来源指纹
变化时也停止推进 cursor；客户端完成该类型的受控全量重扫后发送 `reset: true`，才恢复推进。
`GET /api/ingest/health_connect/state` 使用同一 Bearer 鉴权，只返回游标/权限运行元数据，不返回
健康值。Android 端仍需真机验证 Health Connect 权限撤销、来源 App 安装/卸载和分页 cursor。

## 覆盖、来源与新鲜度

Agent/Nexus summary 和 trend 返回 `data_quality`：字段或天数覆盖、来源、新鲜度、最近有效日期，
以及是否足以形成趋势。缺失与零严格分开；少于 3 个点时 trend 不计算均值和变化。周度建议按
步数、睡眠、训练、饮食分别计算覆盖；覆盖不足时只给记录事实，不开放创建调整草稿。

月报洞察继续要求双桶各至少 8 个样本，并显示 `n/90` 覆盖、来源和最近有效日期。LLM 上下文
包含同样的证据限定，避免把稀疏观察写成确定因果。

## Platform capability lifecycle

`scripts/platform_evidence.py` 对已部署实例的 `healthz`/`readyz` 和三个只读能力做安全探测，
并输出 `shadow.conformance-evidence.v1` 的 `observed` 证据。它不把响应正文、健康值或 token 写入
证据；draft/write 能力不能用无确认探针冒充 observed，需由隔离 conformance workflow 覆盖。

```bash
python scripts/platform_evidence.py \
  --base-url https://health.example.com \
  --deployment-id health-prod \
  --build-id REPLACE_WITH_64_HEX_BUILD_ID \
  --instance-id health-main \
  --token-file REPLACE_WITH_RESTRICTED_TOKEN_FILE \
  --output build/evidence/health-observed.json
```

随后由 Platform `shadow-conformance-gate` 绑定相同 deployment/build 并推进 lifecycle。Health
不自行宣称 `contract`、`client`、`deployed` 或 `restore-tested` 阶段。

## 隔离恢复验证入口

恢复必须在非生产目标完成，并生成 `shadow.restore-drill.v1`。除了 Platform 要求的 contract、
data、health 三类 passing checks，Health wrapper 还强制以下证据名称：

- `schema-current/contract`：Alembic 已到当前 head；
- `record-identity-unique/data`：`(source, record_type, client_record_id)` 唯一且版本非负；
- `health-ready/health`：恢复实例的 `healthz`、`readyz` 通过。

清理隔离目标并在 drill 中如实写 `cleanup_completed: true` 后运行：

```bash
python scripts/verify_restore.py \
  --release-dir REPLACE_WITH_IMMUTABLE_RELEASE_DIR \
  --drill build/restore/health-drill.json \
  --output build/evidence/health-restore.json
```

wrapper 复用 Platform 的 schema、build/deployment 绑定、非生产目标、备份不可变性、时间顺序和
artifact hash 校验，不复制备份或领域事实到 Platform。

## legacy-forward 回滚窗口

`legacy-forward` 只允许显式、带时区、最长 72 小时的回滚窗口。启用时必须设置
`SHADOW_LEGACY_FORWARD_UNTIL`；到期后 Forward identity 立即失效，`readyz` 与登录入口返回
503。正常部署保持 `SHADOW_AUTH_MODE=oidc`。
