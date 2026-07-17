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
  QUEUE_FILE    可选，上报失败的本地持久队列（默认 ./miscale_queue.json；
                compose 里挂 /data 卷）——服务端重启窗口内的测量不再丢失
  LOG_LEVEL     可选，默认 INFO

健壮性：
- 看门狗：周期性重建 BLE 扫描会话——bluetoothd 重启/适配器掉线后 BlueZ 的
  discovery 会话静默丢失，bleak 既不报错也不重建；重建失败则退出进程，
  交给 Docker restart 策略拉起
- 本地队列：上报三连失败（如 app 容器在部署重启）先落盘，服务恢复后补发；
  服务端按 (秤时间戳+体重) 去重，重放幂等

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
# 本地队列补发间隔 / 扫描会话重建周期（看门狗）
RETRY_QUEUE_S = 60
SCAN_REBUILD_S = 600


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


def parse_adv(data: bytes, now: datetime | None = None) -> Measurement | None:
    """解析 0x181B Service Data；非稳定帧/离秤帧/解析失败返回 None。
    now 仅测试注入用（缺省取当前时间）。"""
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

    # 秤 RTC 与接收时刻的偏差 = 秤钟偏移（广播是实时的，不存在"旧测量"）：
    # ≤10 分钟视为钟准原样用（含小漂移，双端天然一致）；更大偏差按 15 分钟粒度
    # 量化后校正——用户的秤 RTC 被对成了 UTC（偏移 +8h），量化保证手机/网关双端
    # 各自计算也得到同一校正量 → 去重键仍一致；RTC 字节非法才用接收时刻取整兜底。
    # 校正必须在接收端做：服务端看不出"补发的旧测量"与"钟偏"的区别。
    if now is None:
        now = datetime.now()
    try:
        rtc = datetime(
            int.from_bytes(data[2:4], "little"),
            data[4], data[5], data[6], data[7], data[8],
        )
    except ValueError:
        rtc = None
    if rtc is None:
        ts = now.replace(second=0, microsecond=0)
    else:
        delta = now - rtc
        if abs(delta) <= timedelta(minutes=10):
            ts = rtc
        else:
            quarter = timedelta(minutes=15)
            # 半进位与 Android Math.round 一致（负偏移时银行家舍入会分叉）
            ts = rtc + quarter * _round_half_up(delta / quarter)

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
    def __init__(self, url: str, token: str, mac_filter: str | None,
                 queue_path: str | None = None) -> None:
        self.url = url
        self.token = token
        self.mac_filter = mac_filter.upper() if mac_filter else None
        self.pending: dict[str, tuple[float, Measurement]] = {}  # key -> (首见时刻, 最优帧)
        self.sent: dict[str, float] = {}
        self.queue_path = queue_path
        self._last_drain = 0.0

    # ---- 失败测量的本地持久队列（服务端按秤时间戳+体重去重，重放幂等） ----
    def _load_queue(self) -> list[dict]:
        if not self.queue_path or not os.path.exists(self.queue_path):
            return []
        try:
            with open(self.queue_path, encoding="utf-8") as f:
                items = json.load(f)
            return items if isinstance(items, list) else []
        except (ValueError, OSError):
            return []

    def _save_queue(self, items: list[dict]) -> None:
        if not self.queue_path:
            return
        try:
            tmp = self.queue_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(items[-200:], f, ensure_ascii=False)  # 上限护栏，防无限膨胀
            os.replace(tmp, self.queue_path)
        except OSError as exc:
            log.warning("写本地队列失败：%s", exc)

    def enqueue(self, m: Measurement) -> None:
        items = self._load_queue()
        items.append({"ts": m.ts.isoformat(), "weight_kg": m.weight_kg, "impedance": m.impedance})
        self._save_queue(items)
        log.warning("上报失败已入本地队列（%d 条待补发）", len(items))

    def drain_queue(self) -> None:
        items = self._load_queue()
        if not items:
            return
        remaining: list[dict] = []
        for it in items:
            try:
                m = Measurement(
                    ts=datetime.fromisoformat(str(it["ts"])),
                    weight_kg=float(it["weight_kg"]),
                    impedance=it.get("impedance"),
                )
            except (KeyError, TypeError, ValueError):
                continue  # 坏条目直接丢弃
            # 保序补发：一旦失败，其余留到下一轮
            if remaining or not post_measurement(self.url, self.token, m):
                remaining.append(it)
        self._save_queue(remaining)
        if len(remaining) < len(items):
            log.info("本地队列补发：%d → %d 条", len(items), len(remaining))

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
                        # 三连失败（如 app 容器在部署重启）：落本地持久队列稍后补发；
                        # 保留 sent 键，同一测量的后续广播不会重复入队
                        await loop.run_in_executor(None, self.enqueue, m)
            # 有积压时定期补发（服务恢复后 1 分钟内追平）
            if now - self._last_drain >= RETRY_QUEUE_S:
                self._last_drain = now
                if self._load_queue():
                    await loop.run_in_executor(None, self.drain_queue)


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

    gw = Gateway(
        url, token, os.environ.get("MISCALE_MAC"),
        queue_path=os.environ.get("QUEUE_FILE", "miscale_queue.json").strip() or None,
    )
    log.info("开始监听体脂秤广播（0x181B）%s", f"，MAC 过滤 {gw.mac_filter}" if gw.mac_filter else "")

    flusher = asyncio.create_task(gw.flush_loop())
    try:
        # 看门狗：周期性重建扫描会话。bluetoothd 重启/适配器掉线后 discovery 会话
        # 静默丢失且 bleak 不报错——重建让监听自动恢复；重建失败说明蓝牙栈不可用，
        # 退出进程交给 Docker restart 拉起（比带病假活强）。
        while True:
            try:
                async with BleakScanner(gw.on_adv):
                    await asyncio.sleep(SCAN_REBUILD_S)
                log.info("按看门狗周期重建 BLE 扫描会话")
            except Exception as exc:
                log.error("BLE 扫描会话建立/维持失败：%s——退出交给容器重启", exc)
                sys.exit(1)
    finally:
        flusher.cancel()


def selftest() -> None:
    """合成广播帧验证解析（不依赖蓝牙）。"""
    def frame(unit: int, flags: int, y: int, mo: int, d: int, h: int, mi: int, s: int,
              z: int, raw_w: int) -> bytes:
        return bytes([unit, flags]) + y.to_bytes(2, "little") + bytes([mo, d, h, mi, s]) \
            + z.to_bytes(2, "little") + raw_w.to_bytes(2, "little")

    now = datetime.now().replace(microsecond=0)
    y, mo, d = now.year, now.month, now.day
    hh, mi, ss = now.hour, now.minute, now.second
    # 稳定 + 阻抗：71.85kg, z=512（RTC=now，钟准原样用）
    m = parse_adv(frame(0x02, 0b00100010, y, mo, d, hh, mi, ss, 512, 14370), now=now)
    assert m is not None and m.weight_kg == 71.85 and m.impedance == 512 and m.ts == now, m
    # 稳定无阻抗帧
    m2 = parse_adv(frame(0x02, 0b00100000, y, mo, d, hh, mi, ss, 0, 14370), now=now)
    assert m2 is not None and m2.impedance is None, m2
    # 未稳定帧应被丢弃
    assert parse_adv(frame(0x02, 0b00000010, y, mo, d, hh, mi, ss, 512, 14370), now=now) is None
    # 离秤帧应被丢弃
    assert parse_adv(frame(0x02, 0b10100010, y, mo, d, hh, mi, ss, 512, 14370), now=now) is None
    # 阻抗 3000+ 视为无效
    m3 = parse_adv(frame(0x02, 0b00100010, y, mo, d, hh, mi, ss, 65534, 14370), now=now)
    assert m3 is not None and m3.impedance is None, m3
    # RTC 明显不对（掉电回 2000 年）→ 量化校正后 ≈ 接收时刻
    m4 = parse_adv(frame(0x02, 0b00100010, 2000, 1, 1, 0, 0, 0, 512, 14370), now=now)
    assert m4 is not None and abs(m4.ts - now) <= timedelta(minutes=8), m4
    # UTC 钟（用户实测：RTC 慢 8h）→ 校正回本地时间
    utc8 = now - timedelta(hours=8)
    m6 = parse_adv(frame(0x02, 0b00100010, utc8.year, utc8.month, utc8.day,
                         utc8.hour, utc8.minute, utc8.second, 512, 14370), now=now)
    assert m6 is not None and abs(m6.ts - now) <= timedelta(minutes=8), m6
    # 双端一致性：同一广播、接收时刻差 3 秒（手机 vs 网关）→ 同一去重键
    m6b = parse_adv(frame(0x02, 0b00100010, utc8.year, utc8.month, utc8.day,
                          utc8.hour, utc8.minute, utc8.second, 512, 14370),
                    now=now + timedelta(seconds=3))
    assert m6b is not None and m6.key == m6b.key, (m6.key, m6b.key)
    # 去重键一致性（同一测量不同帧）
    assert m.key == parse_adv(
        frame(0x02, 0b00100010, y, mo, d, hh, mi, ss, 512, 14370), now=now).key
    # 半值舍入与 Android 端一致：raw=14425 → 72.125 kg → 半进位 72.13（银行家舍入会得 72.12）
    m5 = parse_adv(frame(0x02, 0b00100010, y, mo, d, hh, mi, ss, 512, 14425), now=now)
    assert m5.weight_kg == 72.13 and m5.key.endswith("-14426"), m5
    # 非 kg 单位帧应跳过
    assert parse_adv(frame(0x03, 0b00100010, y, mo, d, hh, mi, ss, 512, 14370), now=now) is None
    print("selftest OK:", m)


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        asyncio.run(main())
