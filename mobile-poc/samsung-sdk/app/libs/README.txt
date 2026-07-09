把从三星开发者门户下载的 Samsung Health Data SDK 里的 AAR 放到本目录：

    samsung-health-data-api-<版本>.aar   （例如 samsung-health-data-api-1.1.0.aar）

build.gradle.kts 用 fileTree 收本目录所有 *.aar，版本号不限、放进来即可。

下载地址：https://developer.samsung.com/health/data  （需三星账号登录，免费）
下载页 → Download / Get Started → 解压后在 libs 或 sdk 目录里找 samsung-health-data-api-*.aar。
