"""秤时间戳存量修复（V8.4）：秤 RTC 被对成 UTC，接收端此前把 RTC 当本地时间，
导致 miscale 记录时间慢 8 小时——早上 8 点前的测量被归到前一天的日期。

做什么（幂等，可重跑）：
1. import_raw source='miscale' 的每条已解析测量：raw.ts += offset（默认 +8h），
   raw 里落 "ts_fixed" 标记 → 重跑跳过；external_id（历史去重键）保持不动，
   对应的广播早已过期，不影响未来去重。
2. 按修正后的本地日期全量重算 body_metrics 的 miscale 归属：
   - 现在登记为 miscale 的字段、但修正后该日已无测量 → 清空该字段并解除登记；
   - 每个有测量的日期按「同日最后一次」重算体成分并 autofill（手动值不覆盖，
     与 /api/ingest/miscale 归一化同口径）。

用法（NAS 上跑，先备份）：
    uv run python scripts/fix_miscale_ts.py --dry-run      # 只看计划
    uv run python scripts/fix_miscale_ts.py                # 执行（默认 +8h）
    uv run python scripts/fix_miscale_ts.py --offset-hours 8 --since 2026-07-01

注意：
- 先升级壳 APK 与网关镜像（双端已带钟偏量化校正）再跑，否则新数据继续错；
- 秤钟不是一直 UTC 时用 --since 圈定范围（只移标记范围内的 raw，重算是全量的）；
- legacy 迁移的旧系统体重（source='legacy'）不在本脚本范围。
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402
from sqlalchemy.orm.attributes import flag_modified  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.models import BodyMetrics, ImportRaw  # noqa: E402
from app.routers.ingest import MISCALE_SOURCE, _miscale_profile, _miscale_ts  # noqa: E402
from app.services.autofill import autofill_fields  # noqa: E402
from app.services.miscale import compute_body_metrics  # noqa: E402
from app.timeutil import LOCAL_TZ  # noqa: E402


def shift_raw_ts(db: Session, offset: timedelta, since: date | None) -> int:
    """给未修过的 miscale raw 的 ts 加偏移并打 ts_fixed 标记；返回移动条数。"""
    rows = db.execute(
        select(ImportRaw).where(
            ImportRaw.source == MISCALE_SOURCE,
            ImportRaw.record_type == "measurement",
            ImportRaw.parse_status == "parsed",
        )
    ).scalars().all()
    moved = 0
    for r in rows:
        raw = dict(r.raw or {})
        if "ts_fixed" in raw:
            continue
        ts = _miscale_ts(raw.get("ts"))
        if ts is None:
            continue
        if since is not None and ts.astimezone(LOCAL_TZ).date() < since:
            continue
        new_ts = ts + offset
        print(f"  raw#{r.id}: {ts:%Y-%m-%d %H:%M} -> {new_ts.astimezone(LOCAL_TZ):%Y-%m-%d %H:%M}")
        raw["ts"] = new_ts.astimezone(LOCAL_TZ).replace(tzinfo=None).isoformat()
        raw["ts_fixed"] = f"+{offset}"
        r.raw = raw
        flag_modified(r, "raw")
        moved += 1
    return moved


def rebuild_metrics(db: Session) -> tuple[int, int]:
    """按（修正后的）raw 全量重算 body_metrics 的 miscale 归属；返回 (清空日, 回填日)。"""
    # 每个本地日期的最后一次测量
    by_day: dict[date, tuple[datetime, float, float | None]] = {}
    for r in db.execute(
        select(ImportRaw).where(
            ImportRaw.source == MISCALE_SOURCE,
            ImportRaw.record_type == "measurement",
            ImportRaw.parse_status == "parsed",
        )
    ).scalars():
        raw = r.raw or {}
        ts = _miscale_ts(raw.get("ts"))
        w = raw.get("weight_kg")
        if ts is None or not isinstance(w, (int, float)):
            continue
        z = raw.get("impedance")
        d = ts.astimezone(LOCAL_TZ).date()
        prev = by_day.get(d)
        if prev is None or ts >= prev[0]:
            by_day[d] = (ts, float(w), float(z) if isinstance(z, (int, float)) else None)

    # 旧归属清理：登记为 miscale 但该日已无测量的字段
    cleared = 0
    for row in db.execute(
        select(BodyMetrics).where(BodyMetrics.autofilled.is_not(None))
    ).scalars():
        miscale_fields = [f for f, s in (row.autofilled or {}).items() if s == MISCALE_SOURCE]
        if not miscale_fields or row.log_date in by_day:
            continue
        print(f"  清空 {row.log_date}: {', '.join(miscale_fields)}")
        autofilled = dict(row.autofilled)
        for f in miscale_fields:
            setattr(row, f, None)
            autofilled.pop(f, None)
        row.autofilled = autofilled
        cleared += 1

    sex, age, height_cm = _miscale_profile(db)
    filled = 0
    for d in sorted(by_day):
        _, w, z = by_day[d]
        values = compute_body_metrics(w, z, sex, age, height_cm)
        written = autofill_fields(db, d, MISCALE_SOURCE, values)
        if written:
            print(f"  回填 {d}: {', '.join(written)}")
            filled += 1
    return cleared, filled


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--offset-hours", type=float, default=8.0,
                    help="ts 加多少小时（默认 8：UTC 钟 → Asia/Shanghai）")
    ap.add_argument("--since", type=date.fromisoformat, default=None,
                    help="只移这天（含）之后的 raw（秤钟不是一直 UTC 时圈范围）")
    ap.add_argument("--dry-run", action="store_true", help="只打印计划，整体回滚")
    args = ap.parse_args()

    db = SessionLocal()
    try:
        print(f"== 移动 raw ts（+{args.offset_hours}h"
              + (f"，since {args.since}" if args.since else "") + "）")
        moved = shift_raw_ts(db, timedelta(hours=args.offset_hours), args.since)
        print(f"== 共移动 {moved} 条；重算 body_metrics 归属")
        cleared, filled = rebuild_metrics(db)
        print(f"== 清空 {cleared} 天旧归属，回填 {filled} 天")
        if args.dry_run:
            db.rollback()
            print("== dry-run：已整体回滚，未写库")
        else:
            db.commit()
            print("== 已提交")
    finally:
        db.close()


if __name__ == "__main__":
    main()
