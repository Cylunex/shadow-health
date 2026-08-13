"""网关 BLE 广播解析（gateway/miscale_listener.parse_adv）：
双端去重键一致性（半进位舍入、RTC 钟偏量化校正、kg-only）是审查修复的回归锁。

V8.4：秤 RTC 钟偏校正——用户的秤 RTC 被对成 UTC（慢 8h），偏差 ≤10min 原样用、
更大偏差按 15min 粒度量化校正（Android ScaleScanService 同款逻辑，两边同步改）。
parse_adv 的 now 参数为测试注入口，生产缺省当前时间。
"""
import importlib.util
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

_GATEWAY = Path(__file__).resolve().parent.parent / "gateway" / "miscale_listener.py"
spec = importlib.util.spec_from_file_location("miscale_listener", _GATEWAY)
listener = importlib.util.module_from_spec(spec)
sys.modules["miscale_listener"] = listener
spec.loader.exec_module(listener)

NOW = datetime(2020, 5, 1, 7, 31, 22)  # 固定注入时刻，测试全确定性


def frame(unit, flags, y, mo, d, h, mi, s, z, raw_w) -> bytes:
    return bytes([unit, flags]) + y.to_bytes(2, "little") + bytes([mo, d, h, mi, s]) \
        + z.to_bytes(2, "little") + raw_w.to_bytes(2, "little")


def at(dt: datetime, unit=0x02, flags=0b00100010, z=512, raw_w=14370) -> bytes:
    return frame(unit, flags, dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second, z, raw_w)


def test_stable_frame_with_impedance():
    m = listener.parse_adv(at(NOW), now=NOW)
    assert m.weight_kg == 71.85 and m.impedance == 512
    assert m.ts == NOW  # 钟准：RTC 原样用
    assert m.key.endswith("-14370")


def test_half_value_rounds_up_matching_android():
    # raw=14425 → 72.125 kg：银行家舍入会得 72.12，半进位（与 Java Math.round 一致）须得 72.13
    m = listener.parse_adv(at(NOW, raw_w=14425), now=NOW)
    assert m.weight_kg == 72.13
    assert m.key.endswith("-14426")


def test_unstable_and_removed_frames_dropped():
    assert listener.parse_adv(at(NOW, flags=0b00000010), now=NOW) is None
    assert listener.parse_adv(at(NOW, flags=0b10100010), now=NOW) is None


def test_non_kg_unit_skipped():
    assert listener.parse_adv(at(NOW, unit=0x03), now=NOW) is None


def test_invalid_impedance_dropped_weight_kept():
    m = listener.parse_adv(at(NOW, z=65534), now=NOW)
    assert m is not None and m.impedance is None


def test_small_drift_kept_as_is():
    # 钟慢 6 分钟（≤10min）：原样用——日期归属无碍，且双端天然一致
    rtc = NOW - timedelta(minutes=6)
    m = listener.parse_adv(at(rtc), now=NOW)
    assert m.ts == rtc


def test_utc_clock_corrected_to_local():
    # 用户实测：秤 RTC 是 UTC（慢 8h）→ 量化校正 +8h 回本地时间
    rtc = NOW - timedelta(hours=8)
    m = listener.parse_adv(at(rtc), now=NOW)
    assert m.ts == NOW  # delta 恰为 8h 整 → 校正后正好等于接收时刻


def test_utc_clock_dual_listener_same_key():
    # 手机与网关听到同一广播、接收时刻差 5 秒 → 量化偏移一致 → 去重键一致
    rtc = NOW - timedelta(hours=8)
    m_phone = listener.parse_adv(at(rtc), now=NOW)
    m_nas = listener.parse_adv(at(rtc), now=NOW + timedelta(seconds=5))
    assert m_phone.key == m_nas.key
    assert m_phone.ts == m_nas.ts == NOW


def test_dead_rtc_corrected_near_now():
    # RTC 停在 2000 年：巨大偏移同样量化校正 → ≈接收时刻（量化粒度 ±7.5min 内）
    m = listener.parse_adv(frame(0x02, 0b00100010, 2000, 1, 1, 0, 0, 0, 512, 14370), now=NOW)
    assert m is not None and abs(m.ts - NOW) <= timedelta(minutes=8)


def test_invalid_rtc_bytes_fall_back_to_minute_floor():
    # 月=13 非法 → datetime 构造失败 → 接收时刻取整到分钟兜底
    bad = frame(0x02, 0b00100010, 2020, 13, 1, 0, 0, 0, 512, 14370)
    m = listener.parse_adv(bad, now=NOW)
    assert m.ts == NOW.replace(second=0, microsecond=0)


def test_scale_clock_ahead_corrected():
    # 钟快 1 小时（负偏移方向）：同样量化校正回来
    rtc = NOW + timedelta(hours=1)
    m = listener.parse_adv(at(rtc), now=NOW)
    assert m.ts == NOW


# xiaomi-ble 上游 MJTZC01YM 实机加密广播向量（MiBeacon v5 / object 0x6E16）。
S400_ADDRESS = "8C:D0:B2:F6:BE:EF"
S400_BINDKEY = "0728974d657a4b60964c1b1677f35f7c"


def test_s400_encrypted_weight_low_impedance_and_heart_rate():
    raw = bytes.fromhex("4859d53b0abc078ff2348c844138e930220000009e538599")
    f = listener.parse_s400_adv(raw, S400_ADDRESS, S400_BINDKEY)
    assert f is not None
    assert f.profile_id == 1
    assert f.weight_kg == 69.9
    assert f.impedance_low == 543.2
    assert f.heart_rate == 92
    assert f.impedance_high is None and not f.reset


def test_s400_encrypted_final_high_impedance_packet():
    raw = bytes.fromhex("4859d53b0bd6ef0b25db72785e7e2f46d6000000d8642df6")
    f = listener.parse_s400_adv(raw, S400_ADDRESS, S400_BINDKEY)
    assert f is not None
    assert f.profile_id == 1
    assert f.weight_kg is None and f.impedance_low is None
    assert f.impedance_high == 497.6


def test_s400_rejects_missing_or_wrong_bindkey():
    raw = bytes.fromhex("4859d53b0abc078ff2348c844138e930220000009e538599")
    assert listener.parse_s400_adv(raw, S400_ADDRESS, None) is None
    assert listener.parse_s400_adv(raw, S400_ADDRESS, "00" * 16) is None


def test_s400_gateway_merges_two_packets_before_flush():
    first = bytes.fromhex("4859d53b0abc078ff2348c844138e930220000009e538599")
    final = bytes.fromhex("4859d53b0bd6ef0b25db72785e7e2f46d6000000d8642df6")
    gw = listener.Gateway("http://example.invalid", "token", None, bindkey=S400_BINDKEY)
    device = SimpleNamespace(address=S400_ADDRESS)
    gw.on_adv(device, SimpleNamespace(service_data={listener.UUID_MIBEACON: first}))
    assert len(gw.s400_pending) == 1 and not gw.pending
    m = gw.s400_pending[S400_ADDRESS][1]
    assert m.weight_kg == 69.9 and m.impedance == 543.2
    assert m.impedance_high is None and m.heart_rate == 92

    gw.on_adv(device, SimpleNamespace(service_data={listener.UUID_MIBEACON: final}))
    m = gw.s400_pending[S400_ADDRESS][1]
    assert m.impedance_high == 497.6


def test_s400_reset_preserves_captured_weight_for_flush():
    first = bytes.fromhex("4859d53b0abc078ff2348c844138e930220000009e538599")
    # 未加密 MiBeacon v2：object 0x6E16 全零表示离秤/复位。
    reset = bytes.fromhex("4020d93001166e09010000000000000000")
    gw = listener.Gateway("http://example.invalid", "token", None, bindkey=S400_BINDKEY)
    device = SimpleNamespace(address=S400_ADDRESS)
    gw.on_adv(device, SimpleNamespace(service_data={listener.UUID_MIBEACON: first}))
    assert len(gw.s400_pending) == 1

    parsed = listener.parse_s400_adv(reset, S400_ADDRESS, S400_BINDKEY)
    assert parsed is not None and parsed.reset
    gw.on_adv(device, SimpleNamespace(service_data={listener.UUID_MIBEACON: reset}))

    assert not gw.s400_pending
    assert len(gw.pending) == 1
    m = next(iter(gw.pending.values()))[1]
    assert m.weight_kg == 69.9 and m.impedance == 543.2
