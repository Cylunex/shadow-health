# shadow-health Android 壳

把局域网里的 shadow-health Web 应用包成原生 Android 应用（WebView 壳），并内置两条数据采集通道：

- **体脂秤 BLE 监听**（`ScaleScanService`）：后台抓小米体脂秤 2 的广播 → `/api/ingest/miscale`
- **三星健康直读同步**（`SamsungSyncWorker`）：Samsung Health Data SDK 读手表数据（步数/睡眠/心率/运动/体成分）→ `/api/ingest/samsung_direct`，WorkManager 每小时一次

2.0 起为标准 Gradle 工程（1.x 的 aapt2/d8 手工流水线已废弃）。
包名 `com.shadowverse.health`，应用名「健康」，**minSdk 29**（Samsung SDK 强制，Android 10+）/ targetSdk 34。

## 构建

前置（一次性）：

1. **JDK 17**（macOS：`brew install openjdk@17`；Windows：Adoptium zip）
2. **Android SDK**（macOS：`brew install android-commandlinetools` 后
   `sdkmanager "platforms;android-35" "build-tools;35.0.0"`；或任一已有 SDK）
3. `android/local.properties`（不入仓）：`sdk.dir=<你的 Android SDK 路径>`
4. **Samsung Health Data SDK AAR**（专有，不入仓）：三星账号登录
   [developer.samsung.com/health/data](https://developer.samsung.com/health/data) 下载，
   把 `samsung-health-data-api-*.aar` 放进 `android/app/libs/`

构建（Gradle wrapper 已入仓，含国内镜像）：

```bash
cd android
JAVA_HOME=/opt/homebrew/opt/openjdk@17 ./gradlew --no-daemon assembleDebug   # macOS
# Windows: 设好 JAVA_HOME 后 .\gradlew.bat --no-daemon assembleDebug
```

产物：`app/build/outputs/apk/debug/app-debug.apk`。

> 签名：debug keystore（`~/.gradle` / `~/.android` 自动生成）。换构建机后签名不同，
> 手机上需先卸载旧版再安装。

## 安装到手机

- **局域网下载**：apk 拷进服务器 `static/` 目录，手机浏览器访问
  `http://<服务器IP>:8080/static/app-debug.apk` 下载安装（需允许安装未知来源应用）；
- **adb**：`adb install -r app/build/outputs/apk/debug/app-debug.apk`；
- 或微信/网盘任意方式传文件安装。

## 连接设置

默认地址 `http://192.168.1.100:8080`。三种方式打开设置框：**首次启动**自动弹出；
任意页面**三指按住约 0.7 秒**；实体/虚拟**菜单键**。

设置项：服务器地址 · `INGEST_TOKEN`（同服务器 .env，各通道共用）·
体脂秤监听开关 · 三星健康同步开关 · 每日提醒开关（20:30 拉服务端
`/api/reminders/digest`，有打卡/蛋白/步数/周有氧缺口才弹通知，全达成不打扰）。
地址等存 SharedPreferences，重装后需重设。

其他行为：返回键 = 网页后退（无历史则退出）；登录 Cookie 持久化，重启不掉登录态。

## 体脂秤后台监听（小米体脂秤 2）

低功耗扫描秤的 BLE 广播（Service Data 0x181B），测量稳定后 POST 服务端；
与 NAS 网关同时在线不会重复记录（服务端按秤时间戳去重）。协议与去抖逻辑同
`gateway/miscale_listener.py`，整体说明见 [gateway/README.md](../gateway/README.md)。

- 开启后需授予蓝牙（Android 12+ 为「附近的设备」）与通知权限；
- 国产 ROM（小米/HyperOS 等）需允许**自启动**、电池策略「无限制」；
- 监听状态显示在常驻通知上。

## 三星健康同步（手表数据）

前置：手机装三星健康且**开发者模式 Data Read 已开**——三星健康 → 设置 → 关于 →
连点版本号 10 次 → Developer Mode for Data Read（读自己数据不需要 partner 资格；
三星健康更新后开关可能自动关闭，同步失败时先检查这里）。

勾选开关后会弹三星健康自己的授权页（步数/睡眠/心率/运动/体成分五项只读）。
之后 WorkManager 每小时同步一次增量（首次回溯 7 天），保存设置也会立即触发一次。
去重与幂等由服务端保证（`/api/ingest/samsung_direct`，详见 docs/mobile-sync.md）。

## 目录结构

```
android/
├── settings.gradle.kts / build.gradle.kts / gradle.properties
├── gradle/wrapper/ · gradlew(.bat)     # wrapper 入仓（腾讯镜像）
├── app/
│   ├── build.gradle.kts                # minSdk 29 / target 34，AAR fileTree
│   ├── libs/                           # Samsung SDK AAR（不入仓，见 libs/README.txt）
│   └── src/main/
│       ├── AndroidManifest.xml
│       ├── java/com/shadowverse/health/
│       │   ├── MainActivity.java       # WebView 壳 + 连接设置 + 权限
│       │   └── ScaleScanService.java   # 体脂秤 BLE 前台服务
│       ├── kotlin/com/shadowverse/health/
│       │   ├── SamsungSync.kt          # 同步开关/调度/授权
│       │   └── SamsungSyncWorker.kt    # Data SDK 直读 → POST
│       └── res/mipmap-*/ic_launcher.png
└── local.properties                    # 本机 SDK 路径（不入仓）
```
