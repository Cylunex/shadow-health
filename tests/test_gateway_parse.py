"""网关 BLE 广播解析（gateway/miscale_listener.parse_adv）：
双端去重键一致性（半进位舍入、RTC 兜底分钟取整、kg-only）是审查修复的回归锁。"""
import importlib.util
import sys
from datetime import datetime, timedelta
from pathlib import Path

_GATEWAY = Path(__file__).resolve().parent.parent / "gateway" / "miscale_listener.py"
spec = importlib.util.spec_from_file_location("miscale_listener", _GATEWAY)
listener = importlib.util.module_from_spec(spec)
sys.modules["miscale_listener"] = listener
spec.loader.exec_module(listener)


def frame(unit, flags, y, mo, d, h, mi, s, z, raw_w) -> bytes:
    return bytes([unit, flags]) + y.to_bytes(2, "little") + bytes([mo, d, h, mi, s]) \
        + z.to_bytes(2, "little") + raw_w.to_bytes(2, "little")


def _now_ymd():
    n = datetime.now()
    return n.year, n.month, n.day


def test_stable_frame_with_impedance():
    y, mo, d = _now_ymd()
    m = listener.parse_adv(frame(0x02, 0b00100010, y, mo, d, 7, 31, 22, 512, 14370))
    assert m.weight_kg == 71.85 and m.impedance == 512
    assert m.key.endswith("-14370")


def test_half_value_rounds_up_matching_android():
    # raw=14425 → 72.125 kg：银行家舍入会得 72.12，半进位（与 Java Math.round 一致）须得 72.13
    y, mo, d = _now_ymd()
    m = listener.parse_adv(frame(0x02, 0b00100010, y, mo, d, 7, 31, 22, 512, 14425))
    assert m.weight_kg == 72.13
    assert m.key.endswith("-14426")


def test_unstable_and_removed_frames_dropped():
    y, mo, d = _now_ymd()
    assert listener.parse_adv(frame(0x02, 0b00000010, y, mo, d, 7, 0, 0, 512, 14370)) is None
    assert listener.parse_adv(frame(0x02, 0b10100010, y, mo, d, 7, 0, 0, 512, 14370)) is None


def test_non_kg_unit_skipped():
    y, mo, d = _now_ymd()
    assert listener.parse_adv(frame(0x03, 0b00100010, y, mo, d, 7, 0, 0, 512, 14370)) is None


def test_invalid_impedance_dropped_weight_kept():
    y, mo, d = _now_ymd()
    m = listener.parse_adv(frame(0x02, 0b00100010, y, mo, d, 7, 0, 0, 65534, 14370))
    assert m is not None and m.impedance is None


def test_rtc_fallback_floors_to_minute():
    # RTC 停在 2000 年 → 回退系统时间且秒位归零（双端/连播帧去重键才能对齐）
    m = listener.parse_adv(frame(0x02, 0b00100010, 2000, 1, 1, 0, 0, 0, 512, 14370))
    assert m.ts.second == 0 and m.ts.microsecond == 0
    assert abs(m.ts - datetime.now()) < timedelta(minutes=2)
