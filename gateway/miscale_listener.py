"""小米体脂秤 2（XMTZC05HM）BLE 网关：抓广播 → POST shadow-health ingest API。

秤每次测量会在 BLE 广播的 Service Data（UUID 0x181B, Body Composition）里带出
体重/阻抗/RTC 时间，无需配对连接。本脚本常驻监听，测量稳定后上报服务端，
服务端按 (RTC 时间戳 + 体重) 去重——与手机端监听器同时在线也只记一条。

协议（13 字节，参考 ESPHome xiaomi_miscale / openScale）：
  [0]     单位：0x02=kg（原始值 ×0.005）；非 kg 帧跳过（换算系数无法可靠验证）
  [1]     标志位：bit1=带阻抗；bit5=已稳定；bit7=离秤
  [2:4]   年（LE） [4]月 [5]日 [6]时 [7]分 [8]秒   —— 秤的 RTC（本地时间）
  [9:11]  阻抗 Ω（LE；0 或 >=3000 视为无效）
  [11:13] 体重原始值（LE）

环境变量：
  SHADOW_URL    服务端地址，如 http://127.0.0.1:8080
  INGEST_TOKEN  与服务端 .env 一致的 Bearer token
  MISCALE_MAC   可选，秤的 MAC 过滤（米家 App 设备信息里能查到）
  LOG_LEVEL     可选，默认 INFO

自测（不依赖蓝牙）：python miscale_listener.py --selftest
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta

log = logging.getLogger("miscale")

UUID_BODY_COMPOSITION = "0000181b-0000-1000-8000-00805f9b34fb"

# 一次测量会连播多帧：稳定后先出纯体重帧、随后出带阻抗帧。
# 缓冲 SETTLE_S 秒（期间优先保留带阻抗的帧）再上报，避免把阻抗丢掉。
SETTLE_S = 12
# 已上报测量的去重缓存保留时长
SENT_TTL_S = 600


def _round_half_up(x: float) -> int:
    """半进位舍入：与 Android 端 Math.round 一致（Python 内建 round 是银行家舍入，
    在恰好半值时与 Java 分叉，会导致双端去重键不一致）。"""
    return math.floor(x + 0.5)


@dataclass
class Measurement:
    ts: datetime          # 秤 RTC 时间（本地钟）
    weight_kg: float
    impedance: int | None

    @property
    def key(self) -> str:
        return f"{self.ts:%Y%m%dT%H%M%S}-{_round_half_up(self.weight_kg * 200)}"


def parse_adv(data: bytes) -> Measurement | None:
    """解析 0x181B Service Data；非稳定帧/离秤帧/解析失败返回 None。"""
    if len(data) != 13:
        return None
    unit, flags = data[0], data[1]
    stabilized = bool(flags & (1 << 5))
    load_removed = bool(flags & (1 << 7))
    has_impedance = bool(flags & (1 << 1))
    if not stabilized or load_removed:
        return None

    raw_weight = int.from_bytes(data[11:13], "little")
    if unit != 0x02:
        # 非 kg 模式：换算系数无法可靠验证，宁可跳过并记日志，不落错误数据
        log.warning("跳过非 kg 单位帧 unit=0x%02x raw=%s", unit, data.hex())
        return None
    weight = raw_weight * 0.005
    if not (10 <= weight <= 300):
        return None

    impedance: int | None = None
    if has_impedance:
        z = int.from_bytes(data[9:11], "little")
        if 0 < z < 3000:
            impedance = z

    # RTC 兜底取整到分钟：失效期同一测量的连播帧（乃至手机/网关双端）才能生成同一去重键
    fallback = datetime.now().replace(second=0, microsecond=0)
    try:
        ts = datetime(
            int.from_bytes(data[2:4], "little"),
            data[4], data[5], data[6], data[7], data[8],
        )
    except ValueError:
        ts = fallback
    if abs(ts - datetime.now()) > timedelta(days=3):
        ts = fallback

    # 半进位保留两位：与 Android 端 Math.round(weight*100)/100.0 一致
    return Measurement(ts=ts, weight_kg=_round_half_up(weight * 100) / 100.0, impedance=impedance)


def post_measurement(url: str, token: str, m: Measurement) -> bool:
    payload = json.dumps(
        {"measurements": [{"ts": m.ts.isoformat(), "weight_kg": m.weight_kg, "impedance": m.impedance}]}
    ).encode("utf-8")
    req = urllib.request.Request(
        url.rstrip("/") + "/api/ingest/miscale",
        data=payload,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    for attempt in range(1, 4):
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = resp.read().decode("utf-8", "replace")
                log.info("已上报 %.2fkg z=%s → %s %s", m.weight_kg, m.impedance, resp.status, body)
                return True
        except (urllib.error.URLError, OSError) as exc:
            log.warning("上报失败（第 %d 次）：%s", attempt, exc)
            time.sleep(2 * attempt)
    return False


class Gateway:
    def __init__(self, url: str, token: str, mac_filter: str | None) -> None:
        self.url = url
        self.token = token
        self.mac_filter = mac_filter.upper() if mac_filter else None
        self.pending: dict[str, tuple[float, Measurement]] = {}  # key -> (首见时刻, 最优帧)
        self.sent: dict[str, float] = {}

    def on_adv(self, device, adv) -> None:
        if self.mac_filter and device.address.upper() != self.mac_filter:
            return
        data = adv.service_data.get(UUID_BODY_COMPOSITION)
        if not data:
            return
        m = parse_adv(bytes(data))
        if m is None:
            return
        now = time.monotonic()
        self.sent = {k: t for k, t in self.sent.items() if now - t < SENT_TTL_S}
        if m.key in self.sent:
            return
        first_seen, best = self.pending.get(m.key, (now, m))
        # 同一测量的连播帧里优先保留带阻抗的
        if best.impedance is None and m.impedance is not None:
            best = m
        self.pending[m.key] = (first_seen, best)

    async def flush_loop(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            await asyncio.sleep(1)
            now = time.monotonic()
            for key, (first_seen, m) in list(self.pending.items()):
                # 已拿到阻抗，或等待窗口结束（光脚失败/穿袜子只有体重）→ 上报
                if m.impedance is not None or now - first_seen >= SETTLE_S:
                    del self.pending[key]
                    self.sent[key] = now
                    ok = await loop.run_in_executor(None, post_measurement, self.url, self.token, m)
                    if not ok:
                        self.sent.pop(key, None)  # 三次都失败：允许下次广播重试


async def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    url = os.environ.get("SHADOW_URL", "").strip()
    token = os.environ.get("INGEST_TOKEN", "").strip()
    if not url or not token:
        log.error("缺少 SHADOW_URL / INGEST_TOKEN 环境变量")
        sys.exit(2)

    from bleak import BleakScanner  # 延迟导入：--selftest 不需要蓝牙栈

    gw = Gateway(url, token, os.environ.get("MISCALE_MAC"))
    log.info("开始监听体脂秤广播（0x181B）%s", f"，MAC 过滤 {gw.mac_filter}" if gw.mac_filter else "")
    async with BleakScanner(gw.on_adv):
        await gw.flush_loop()


def selftest() -> None:
    """合成广播帧验证解析（不依赖蓝牙）。"""
    def frame(unit: int, flags: int, y: int, mo: int, d: int, h: int, mi: int, s: int,
              z: int, raw_w: int) -> bytes:
        return bytes([unit, flags]) + y.to_bytes(2, "little") + bytes([mo, d, h, mi, s]) \
            + z.to_bytes(2, "little") + raw_w.to_bytes(2, "little")

    now = datetime.now()
    y, mo, d = now.year, now.month, now.day
    # 稳定 + 阻抗：71.85kg, z=512
    m = parse_adv(frame(0x02, 0b00100010, y, mo, d, 7, 31, 22, 512, 14370))
    assert m is not None and m.weight_kg == 71.85 and m.impedance == 512, m
    # 稳定无阻抗帧
    m2 = parse_adv(frame(0x02, 0b00100000, y, mo, d, 7, 31, 22, 0, 14370))
    assert m2 is not None and m2.impedance is None, m2
    # 未稳定帧应被丢弃
    assert parse_adv(frame(0x02, 0b00000010, y, mo, d, 7, 31, 22, 512, 14370)) is None
    # 离秤帧应被丢弃
    assert parse_adv(frame(0x02, 0b10100010, y, mo, d, 7, 31, 22, 512, 14370)) is None
    # 阻抗 3000+ 视为无效
    m3 = parse_adv(frame(0x02, 0b00100010, y, mo, d, 7, 31, 22, 65534, 14370))
    assert m3 is not None and m3.impedance is None, m3
    # RTC 明显不对 → 回退系统时间
    m4 = parse_adv(frame(0x02, 0b00100010, 2000, 1, 1, 0, 0, 0, 512, 14370))
    assert m4 is not None and abs(m4.ts - datetime.now()) < timedelta(minutes=1), m4
    # 去重键一致性（同一测量不同帧）
    assert m.key == parse_adv(frame(0x02, 0b00100010, y, mo, d, 7, 31, 22, 512, 14370)).key
    # 半值舍入与 Android 端一致：raw=14425 → 72.125 kg → 半进位 72.13（银行家舍入会得 72.12）
    m5 = parse_adv(frame(0x02, 0b00100010, y, mo, d, 7, 31, 22, 512, 14425))
    assert m5.weight_kg == 72.13 and m5.key.endswith("-14426"), m5
    # 非 kg 单位帧应跳过
    assert parse_adv(frame(0x03, 0b00100010, y, mo, d, 7, 31, 22, 512, 14370)) is None
    print("selftest OK:", m)


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        asyncio.run(main())
