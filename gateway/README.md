# 体脂秤 BLE 网关（小米体脂秤 2 / XMTZC05HM）

秤每次测量会通过 BLE 广播（Service Data UUID 0x181B）发出**体重 + 生物阻抗 + RTC 时间**，
无需配对连接。两个监听端都把解析结果 POST 到 `/api/ingest/miscale`（Bearer 鉴权，
token 同 `.env` 的 `INGEST_TOKEN`），服务端按「秤时间戳 + 体重」去重——
**手机和 NAS 同时听到同一次测量也只会记一条**。

体脂率/肌肉量/水分/内脏脂肪/基础代谢由服务端按社区公式（openScale 同源）从阻抗计算，
需要在 App 设置页填好**身高、性别、出生日期**；档案不全时只记体重。
光脚上秤才有阻抗（穿袜子只有体重）。

## NAS 端（Docker）

前置：NAS 有蓝牙适配器（板载或 USB 蓝牙棒），宿主机 BlueZ 正常
（`bluetoothctl list` 能看到 Controller，`systemctl status bluetooth` active）。

```bash
# 可选：.env 加 MISCALE_MAC=XX:XX:XX:XX:XX:XX（米家 App 设备信息里查；只有一台秤可不填）
docker compose -f docker-compose.yml -f docker-compose.miscale.yml up -d --build
docker logs -f shadow-health-miscale   # 上秤测一次，应看到「已上报 xx.xxkg z=xxx → 200」
```

注意 BLE 范围只有几米——NAS 离秤太远就靠手机端兜底。

## 手机端（Android 壳内置）

WebView 壳应用已内置监听服务（`ScaleScanService`）。启用步骤：

1. 重新打包安装 apk（Windows 机上 `powershell -File android\build.ps1`，见 android/README.md）；
2. 打开应用 → 三指长按呼出「连接设置」→ 填 INGEST_TOKEN → 勾选「后台监听体脂秤」→ 保存；
3. 授予蓝牙（Android 12+ 为「附近的设备」）与通知权限；
4. 小米/HyperOS：应用设置里允许**自启动**、电池策略改「无限制」，否则后台服务会被清理。

Android 11 及以下额外要求：授予定位权限且系统定位开关打开（系统限制，BLE 扫描必须）。

监听状态显示在常驻通知上；测量成功会更新为「已记录 xx.xx kg（含体成分）」。

## 排错

- 通知一直「等待上秤…」：确认用**米家/小米运动健康绑定过的秤**（首次使用需 App 对时，
  否则秤的 RTC 不准；监听端对偏差 >3 天的时间戳会回退为系统时间）；
- 「上报失败」：核对服务器地址、`INGEST_TOKEN` 与 `.env` 一致；
- 记录了体重但没有体成分：设置页补身高/性别/出生日期，且光脚上秤；
- 协议自测（不依赖蓝牙）：`python gateway/miscale_listener.py --selftest`。
