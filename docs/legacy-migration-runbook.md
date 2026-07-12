# personal_data 旧库迁移 · NAS 执行手册（V3 P3）

> 一页流程：**备份 → dry-run 看报告 → 正式跑 → 核对 → 冻结**。
> 脚本 `scripts/migrate_personal_data.py` 幂等可重跑，中途失败/中断直接再跑即可。
> 凭据全部用占位符表示，执行时问用户要。

## 0. 前置

```bash
cd /path/to/shadow-health          # NAS 上的部署目录（或容器内 /app）
uv run alembic current             # 必须已到 20260712u12（迁移 12），否则先 alembic upgrade head
pg_dump -h <NAS_PG_HOST> -p <PORT> -U <USER> -Fc personal_data  > backup_personal_data.dump
pg_dump -h <NAS_PG_HOST> -p <PORT> -U <USER> -Fc shadow_health  > backup_shadow_health.dump
```

- 目标库连接串默认取 `.env` 的 `DATABASE_URL`（应用同款，写 `shadow_health.health`）。
- 若希望 daily_drinks 一次迁完：先在 app 习惯页确认有一个 **active、名字含「饮水」或「喝水」** 的习惯
  （seed 的「饮水1.5-2L」默认停用，启用即可）。没有也不阻塞——先跑，建好习惯后重跑补上。

## 1. Dry-run（完整试跑，最后整体回滚，不落任何数据）

```bash
export LEGACY_SOURCE_DSN='postgresql://<USER>:<PASS>@<NAS_PG_HOST>:<PORT>/personal_data'
uv run python scripts/migrate_personal_data.py --dry-run
```

报告核对要点（源行数应约为：diet 301 / weight 83 / activity 35 / summary 21 /
drinks 34 / measurements 1 / personal_info 1，以实际为准）：

- `迁入 + 跳过 + 失败 = 源行数`，跳过原因逐条列出，确认口径符合预期
  （diet 查重、weight 同日手动值保留、water_ml=0、无 mood 等）；
- `meal_type 未识别（已兜底加餐）`：确认这些值兜底到加餐可接受；
- `被现有手动值挡下的字段`：旧库值放弃、新库值保留，逐条过目；
- `饮水习惯匹配`：显示匹配到哪个习惯；无匹配时列出受影响日期；
- `失败` 非 0：`SELECT external_id, parse_error FROM health.import_raw WHERE source='legacy' AND parse_status='failed'`（dry-run 已回滚查不到，看报告即可），修数据后重跑。

## 2. 正式迁移

```bash
uv run python scripts/migrate_personal_data.py        # 同一 LEGACY_SOURCE_DSN
```

退出码 0=全部成功；1=有失败行（已迁成功的不受影响，修复后**直接重跑**，
parsed 门控保证不双写）。

## 3. 核对

```sql
-- 目标库 shadow_health
SELECT record_type, parse_status, count(*) FROM health.import_raw
WHERE source='legacy' GROUP BY 1,2 ORDER BY 1,2;        -- 对照报告计数
SELECT count(*) FROM health.workout_logs WHERE external_id LIKE 'legacy-%';
SELECT log_date, mood_score FROM health.body_metrics WHERE mood_score IS NOT NULL ORDER BY 1 LIMIT 5;
```

App 页面抽查：饮食页看 5/25~7/9 间任一天旧记录；体重曲线看 2020 年段；
训练列表看「跑步/徒步」手动条目；设置页看 target_kcal / 出生日期。

## 4. 冻结旧库（核对通过后）

```bash
psql -h <NAS_PG_HOST> -p <PORT> -U <SUPERUSER> -d personal_data \
     -f scripts/freeze_personal_data.sql
```

- 默认方案 A（REVOKE 写权限）；若旧 Hermes 链路用的是表 owner 角色，
  REVOKE 管不住 owner，改用 SQL 文件末尾注释的方案 B（改名 `*_frozen`）。
- life_memories 非健康数据，不冻结。
- 冻结后：删除 Hermes 旧 skill 里直写 personal_data 的脚本与说明（skill v2 已替代）。

## 常见问题

| 情况 | 处理 |
|---|---|
| 报「目标库缺迁移 12」 | 目标环境 `uv run alembic upgrade head` 后重跑 |
| 报「源表 xxx 日期列无法确定」 | 按提示确认实际列名，补进脚本 `_DATE_CANDIDATES` |
| 饮水日期列了「无匹配习惯」 | 启用/新建含「饮水」的 active 习惯后重跑，只会补上缺的打卡 |
| 迁完发现映射不对想重来 | 恢复两库备份重放；或人工修正目标数据（import_raw source='legacy' 有全量原始留档可查） |
