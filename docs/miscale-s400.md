# 小米体脂秤 S400 接入

S400（`MJTZC01YM`，米家型号常见为 `yunmai.scales.ms103/ms104`）与旧的小米体脂秤 2 协议不同：

- 旧秤使用 `0x181B` 13 字节明文广播；
- S400 使用 `0xFE95` MiBeacon v5 加密广播；
- 一次 S400 测量由两帧组成：体重 + 心率 + 50kHz 低频阻抗，以及末尾的 250kHz 高频阻抗。

项目的 NAS 网关和 Android 壳均兼容两代协议。S400 数据会在双帧合并后上报；服务端使用低频阻抗继续计算现有体成分指标，并把双频阻抗、心率、用户槽位和型号完整保存在 `import_raw.raw` 中。

## 获取 BLE bindkey

S400 绑定米家后广播会加密，监听端必须配置该设备的 16 字节 BLE bindkey（32 位十六进制），这不是 Wi-Fi 密码、米家密码或设备 token。

可使用 [Xiaomi Cloud Tokens Extractor](https://github.com/PiotrMachowski/Xiaomi-cloud-tokens-extractor) 登录绑定该秤的同一小米账号，找到 S400 对应设备输出的 BLE key。bindkey 属于设备密钥，不要提交到 Git 或发到公开聊天中。

## Android 壳

1. 安装包含 S400 支持的新 APK；
2. 三指长按打开“连接设置”；
3. 在“小米 S400 BLE bindkey”填入 32 位 key；
4. 开启“后台监听体脂秤”，或在今日页点限时称重；
5. 赤脚完成一次测量，通知应显示“已记录”。

缺少或填错 bindkey 时，通知会提示“检测到 S400：请在连接设置填写正确的 BLE bindkey”。

## NAS BLE 网关

在 `.env` 中配置：

```dotenv
MISCALE_MAC=AA:BB:CC:DD:EE:FF
MISCALE_BINDKEY=0123456789abcdef0123456789abcdef
```

然后重建网关容器：

```bash
docker compose -f docker-compose.yml -f docker-compose.miscale.yml up -d --build miscale-gateway
```

`MISCALE_MAC` 可留空；家中有多个 Xiaomi MiBeacon 设备时建议填写。Android 与 NAS 同时监听不会产生两条记录：旧秤按 RTC 时间 + 体重去重，S400 按接收分钟 + 体重去重。

## 当前体成分口径

S400 的低频阻抗可继续输入项目现有的小米/华米单频公式，因此体重、体脂率、肌肉量、体水分、内脏脂肪和 BMR 会正常生成。高频阻抗目前先原样留档，尚未切换成新的双频临床估算公式，避免未经实测校准就改变历史指标口径。
