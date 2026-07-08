# shadow-health Android 壳

把局域网里的 shadow-health Web 应用包成一个原生 Android 应用（WebView 壳）。
不用 Gradle，纯手动流水线：`aapt2 → javac → d8 → zipalign → apksigner`。

- 包名 `com.shadowverse.health`，应用名「健康」，minSdk 26（Android 8.0+）/ targetSdk 34
- 产物：`dist/shadow-health.apk`（约 65 KB，不进 git）

## 如何安装到手机

### 方式一：局域网下载（最简单）

把 apk 丢进服务器的静态目录，手机浏览器直接下：

```powershell
Copy-Item dist\shadow-health.apk static\
```

手机浏览器访问 `http://<服务器IP>:8080/static/shadow-health.apk`，
下载后点安装（需在系统设置里允许「安装未知来源应用」）。

### 方式二：adb install

手机开 USB 调试并连接电脑（或 `adb connect <手机IP>:5555` 走无线）：

```powershell
adb install -r dist\shadow-health.apk
```

> 换过签名 key（`tools\android\debug.keystore` 被删除重建）后，
> 手机上必须先卸载旧版才能装新版。

### 方式三：微信/网盘等任意方式传文件

apk 只有几十 KB，随便传，点开安装即可。

## 修改服务器地址

默认地址 `http://192.168.1.100:8080`（改成你服务器的局域网地址）。三种方式打开地址设置框：

1. **首次启动**自动弹出；
2. 任意页面上**三根手指按住屏幕约 0.7 秒**；
3. 有实体/虚拟菜单键的设备按**菜单键**。

对话框里可「保存并连接」或「恢复默认」。地址存在 SharedPreferences 里，
重装应用后需重新设置。连接失败时错误页上也有「改地址」按钮。

其他行为：返回键 = 网页后退（无历史则退出应用）；登录 Cookie 持久化，
重启应用不掉登录态。

## 重新打包

前置：`tools\android\` 下已有工具链（gitignored，换机器需重搭，见下）。

```powershell
powershell -ExecutionPolicy Bypass -File android\build.ps1
# 可选参数：-VersionName 1.1 -VersionCode 2
```

产物写到 `dist\shadow-health.apk`，脚本自动完成签名与校验。

### 工具链搭建（新机器一次性）

全部放 `tools\android\`（已被 .gitignore 忽略）：

1. **JDK 17**：<https://adoptium.net> 下载 Windows x64 zip，解压为 `tools\android\jdk-17\`
   （任何 `jdk*` 开头的目录名都能被 build.ps1 探测到）。
2. **cmdline-tools**：<https://developer.android.com/studio#command-line-tools-only>，
   解压后摆成 `tools\android\sdk\cmdline-tools\latest\bin\sdkmanager.bat` 的结构。
3. 装 SDK 组件（先设 `$env:JAVA_HOME` 指向上面的 JDK）：

   ```powershell
   tools\android\sdk\cmdline-tools\latest\bin\sdkmanager.bat --sdk_root=tools\android\sdk `
       "platforms;android-34" "build-tools;34.0.0"
   ```

   若提示许可未接受，跑 `sdkmanager.bat --licenses --sdk_root=tools\android\sdk` 输 y，
   或直接在 `tools\android\sdk\licenses\` 写入官方许可 hash 文件。

签名用的 `tools\android\debug.keystore` 首次构建时自动生成
（alias `shadowhealth`，密码 `android`）。想在多台机器出可互相升级的包，
把这个文件拷过去即可（别提交进 git）。

## 目录结构

```
android/
├── AndroidManifest.xml        # 包名/权限/cleartext/singleTask
├── build.ps1                  # 手动打包流水线
├── src/com/shadowverse/health/
│   └── MainActivity.java      # 全屏 WebView + 地址设置 + 错误页 + cookie 持久化
├── res/mipmap-xhdpi/ic_launcher.png    # 192px 图标
└── res/mipmap-xxxhdpi/ic_launcher.png  # 512px 图标
```

图标由 Pillow 脚本按 `static/icon.svg` 构图重绘（深蓝 #0f172a 圆角方 +
翠绿 #34d399 心形描边 + 心电折线）。
