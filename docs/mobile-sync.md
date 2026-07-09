# 移动端增量同步 — 方案与进度交接

> 最后更新：2026-07-09。本文档是**跨机器接力用的自包含交接**（另一台机器 pull 后据此继续，无需本机记忆/设计文档）。

## TL;DR

把三星健康的手表数据（步数/睡眠/心率/运动/体成分）持续同步进 shadow-health。
- **Health Connect 通道已废弃**：实测国行三星健康**不向 Health Connect 写任何数据**，HC 里拿不到手表数据。
- **采用方案：Samsung Health Data SDK 直读**（绕过 HC），**已真机验证成功**（直读到今日步数）。
- **下一步**：把直读正式集成进 `android/` 安卓壳（须先把壳从手工打包转 Gradle）。
- **兜底**：三星官方 ZIP 导出 → 现有导入器（幂等去重，全类型全保真）。

## 为什么放弃 Health Connect

- 原计划：手机装第三方 health-connect-webhook，读 HC 增量 POST 到 `/api/ingest/health_connect`。
- 该 App 修好 3 个问题后能读 HC、能 POST，但 **HC 里没有三星数据**：国行三星健康向 HC 写零条（HC 里唯一步数是手机自带计步器写的，非三星健康 App）。三星健康设置中相关授权开关国行版缺失，各种重置无效——是三星国行版的封闭，非配置问题。

## 采用方案：Samsung Health Data SDK 直读（已验证 ✅）

**解锁**：三星健康 → 设置 → 关于 → 连点版本号 10 次 → 开发者模式（Samsung Health Data SDK）。
其中 **Developer Mode for Data Read 开关 = 读自己数据，不需要 access code / 合作伙伴**（access code 仅"写入"需申请 partnership，个人拿不到；我们只读，用不上）。

**验证产物**：本仓库 [`mobile-poc/samsung-sdk/`](../mobile-poc/samsung-sdk/) —— 独立 Gradle PoC，包 `com.shadowverse.health.shpoc`，真机点开即直读到今日步数，完全绕过 HC。

### 集成技术要点（踩坑已清）

- **依赖**：`samsung-health-data-api-<版本>.aar`（三星账号登录 developer.samsung.com/health/data 下载，**不在 maven**；放 `app/libs/`，fileTree 收）+ `gson` + `kotlin-parcelize` 插件。**AAR 不入仓库**，换机器重下（见 `mobile-poc/samsung-sdk/app/libs/README.txt`）。
- **`minSdk ≥ 29`**（AAR manifest 强制）。
- `DataTypes` / `DataType` 在 `com.samsung.android.sdk.health.data.request` 包（**不是** `.type`，官方文档写错）。
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
- **运行前提**：手机三星健康开发者模式 Data Read 常开（调试性质，App 更新后可能自动关，需偶尔重开）。

### 本机构建工具链（换机器需重搭）

- Gradle wrapper 已随 PoC 入仓（含国内镜像：`settings.gradle.kts` 阿里云 + `gradle-wrapper.properties` 腾讯）。
- 需 JDK 17 + Android SDK（platform 35 由 AGP 自动补，build-tools 34 起）。
- `local.properties` 不入仓，换机器新建：`sdk.dir=<你的 Android SDK 路径>`。
- 构建：`JAVA_HOME=<jdk17> ./gradlew.bat --no-daemon assembleDebug`。

## 下一步：正式集成进 `android/` 壳

- **落点**：`android/` 壳。它已含 `ScaleScanService`（体脂秤 BLE + 带 token POST），形状与本方案一致。**须先把壳从手工流水线（aapt2/d8）转 Gradle**（PoC 已证明工具链在本机可跑）。
- **读取类型**：steps / sleep / heart_rate / exercise / body_composition。
- **上报**：复用壳的带 token POST；**服务端大概率零改动**——Android 端按 `/api/ingest/health_connect` 已有 JSON 形状发，后端归一化器直接吃（或新增 `/api/ingest/samsung_direct`，二选一）。
- **定时**：WorkManager 周期同步 + 手动触发（可参考 health-connect-webhook 的 SyncManager/WorkManager 实现思路）。

## 不在本仓库、需另行准备的东西

- **三星 SDK AAR**：专有，换机器从三星门户重下放进 `mobile-poc/samsung-sdk/app/libs/`。
- **health-connect-webhook 本地修复**：在另一 clone（upstream 无推送权限），仅本机 commit，非本方案继续所必需（HC 通道已弃）。
- **完整设计文档**：含部署凭据线索/个人数据统计，本地维护不入仓；本文档已提炼移动端同步所需的全部内容。
