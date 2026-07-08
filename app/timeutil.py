"""时间口径（设计文档 §3.0）。

- 事件类记录：UTC 时间 + 行级 time_offset（'UTC+0800'）；落 date 用 time_offset
  折算本地时间，缺失回退 Asia/Shanghai。
- 天汇总类 day_time：本地日 00:00，可能是格式化字符串或毫秒 epoch，
  毫秒值按 UTC 解日期，禁止再套时区转换。
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

LOCAL_TZ = ZoneInfo("Asia/Shanghai")

_OFFSET_RE = re.compile(r"UTC([+-])(\d{2})(\d{2})")


def parse_time_offset(value: str | None) -> timezone:
    """'UTC+0800' → timezone(+8h)；解析失败回退 Asia/Shanghai 当前偏移。"""
    if value:
        m = _OFFSET_RE.fullmatch(value.strip())
        if m:
            sign = 1 if m.group(1) == "+" else -1
            delta = timedelta(hours=int(m.group(2)), minutes=int(m.group(3)))
            return timezone(sign * delta)
    return timezone(datetime.now(LOCAL_TZ).utcoffset() or timedelta(hours=8))


def parse_event_time(value: str | int | float, time_offset: str | None = None) -> datetime:
    """事件时间：'2019-10-18 03:54:02.000'（UTC）或毫秒 epoch → aware UTC datetime。"""
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000.0, tz=timezone.utc)
    s = value.strip()
    if s.isdigit():
        return datetime.fromtimestamp(int(s) / 1000.0, tz=timezone.utc)
    dt = datetime.fromisoformat(s)
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def event_local_date(value: str | int | float, time_offset: str | None) -> date:
    """事件时间按行级 time_offset 折算本地日期。"""
    return parse_event_time(value).astimezone(parse_time_offset(time_offset)).date()


def parse_day_time(value: str | int | float) -> date:
    """天汇总 day_time：字符串直接取日期部分；毫秒按 UTC 解，不再套时区。"""
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000.0, tz=timezone.utc).date()
    s = str(value).strip()
    if s.isdigit():
        return datetime.fromtimestamp(int(s) / 1000.0, tz=timezone.utc).date()
    return datetime.fromisoformat(s).date()


def today_local() -> date:
    return datetime.now(LOCAL_TZ).date()


def now_local() -> datetime:
    return datetime.now(LOCAL_TZ)
