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
