# 移动端增量同步 — 现行方案（Samsung Health Data SDK 直读）

> 最后更新：2026-07-12。历史背景一句话：曾计划走 Health Connect webhook（第三方 App 中转），
> 实测国行三星健康不向 HC 写任何数据，该路线已废弃删除；现行方案为壳内直读，本文档只保留有效信息。

## 现行架构

```
三星健康 ──Data SDK 直读──▶ android/ 壳 SamsungSyncWorker ──Bearer POST──▶ /api/ingest/samsung_direct
```

- 五类数据：steps（分日聚合）/ sleep（含分期）/ heart_rate（逐日 MIN/MAX）/ exercise / body_composition
- WorkManager 每小时 + 保存设置即触发；读取窗口首次 7 天、此后按上次成功 +1 天重叠，幂等 upsert 兜底
- 连接设置对话框有开关，勾选即弹三星授权页

## 服务端契约（`app/routers/ingest.py`）

`POST /api/ingest/samsung_direct`（Bearer=INGEST_TOKEN）接收已归一化的紧凑 JSON：
`daily`（date + steps/hr_min/hr_max…，**SET 语义**、ext_id 带内容哈希，同日数值变化会重新归一化）、
`sleep_sessions` / `exercises`（uid 作 external_id，upsert）、`body`（体成分 → body_metrics
字段级回填，同日取最后一次）。水位线：`sync_state('samsung_direct').watermark`，与 zip 历史一刀切。

## 手机端前提（一次性设置）

- 三星健康 → 设置 → 关于 → 连点版本号 10 次 → **开发者模式（Samsung Health Data SDK）**，
  开 **Developer Mode for Data Read**（读自己数据不需要 access code；access code 仅"写入"需要）。
  注意：开发者模式属调试性质，三星健康更新后可能自动关，需偶尔重开。
- 壳内连接设置：服务器地址 + INGEST_TOKEN（服务器 `.env`）。

## 集成技术要点（踩坑记录，改壳代码前必读）

- 依赖：`samsung-health-data-api-<版本>.aar`（三星账号登录 developer.samsung.com/health/data 下载，
  **不在 maven、不入仓库**；放 `android/app/libs/`，fileTree 收）+ `gson` + `kotlin-parcelize` 插件。
- **`minSdk ≥ 29`**（AAR manifest 强制）。
- `DataTypes` / `DataType` 在 `com.samsung.android.sdk.health.data.request` 包（**不是** `.type`，官方文档有误）。
- 读 API（均为 suspend）：
  ```kotlin
  val store = HealthDataService.getStore(context)
  val perm = Permission.of(DataTypes.STEPS, AccessType.READ)
  if (!store.getGrantedPermissions(setOf(perm)).contains(perm))
      store.requestPermissions(setOf(perm), activity)   // 弹三星健康自己的授权页
  val req = DataType.StepsType.TOTAL.requestBuilder
      .setLocalTimeFilter(LocalTimeFilter.of(startLocalDateTime, endLocalDateTime))
      .build()
  val steps = store.aggregateData(req).dataList.firstOrNull()?.value ?: 0L
  ```

## 构建工具链（换机器需重搭）

- Gradle wrapper 已入仓（国内镜像：`settings.gradle.kts` 阿里云 + `gradle-wrapper.properties` 腾讯）。
- 需 JDK 17 + Android SDK（platform 35 由 AGP 自动补）。
- `local.properties` 不入仓，换机器新建：`sdk.dir=<你的 Android SDK 路径>`。
- 构建：`JAVA_HOME=<jdk17> ./gradlew.bat --no-daemon assembleDebug`。

## 待办关联

子路径前缀适配（/shealth）会影响壳的 base URL 与各 URL 拼接点，见
[docs/subpath-agent-plan.md](subpath-agent-plan.md) Phase 1.5。
