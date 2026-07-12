"""Open Food Facts 中国区子集导入（V7 D3）——扫码离线查询的数据底座。

数据来源（任选其一，NAS 上执行）：
1. 全量 CSV（约 10GB，推荐在 NAS 上跑）：
   https://static.openfoodfacts.org/data/en.openfoodfacts.org.products.csv.gz
   gunzip 后传入 --csv 路径；本脚本流式逐行读，内存占用恒定。
2. 任何同列名的裁剪文件（可先用 zcat + awk 预筛 69 开头条码减小体积）。

筛选口径：条码 69 开头（GS1 中国前缀）**或** countries_tags 含 china；
需有产品名与至少一项营养值。幂等：off_products 按条码 upsert 可重跑。

用法（仓库根）：
    uv run python scripts/import_off_products.py --csv /path/products.csv [--dry-run]

执行时长参考：全量 CSV 约 350 万行，NAS 上 10-20 分钟；--dry-run 只统计不写库。
"""
from __future__ import annotations

import argparse
import csv
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# OFF CSV 是制表符分隔；字段名以官方 data dictionary 为准
_FIELDS = {
    "code": "code",
    "name": "product_name",
    "brand": "brands",
    "kcal": "energy-kcal_100g",
    "protein": "proteins_100g",
    "fat": "fat_100g",
    "carb": "carbohydrates_100g",
    "countries": "countries_tags",
}
BATCH = 2000


def _dec(raw: str, hi: float) -> Decimal | None:
    s = (raw or "").strip()
    if not s:
        return None
    try:
        v = Decimal(s)
    except InvalidOperation:
        return None
    if not v.is_finite() or not (Decimal(0) <= v <= Decimal(str(hi))):
        return None
    return v.quantize(Decimal("0.1"))


def _is_cn(code: str, countries: str) -> bool:
    return code.startswith("69") or "china" in (countries or "").lower()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--csv", required=True, help="OFF products.csv 路径（tab 分隔）")
    ap.add_argument("--dry-run", action="store_true", help="只统计，不写库")
    args = ap.parse_args()

    csv.field_size_limit(10_000_000)  # OFF 部分字段极长，默认 128KB 会炸
    path = Path(args.csv)
    if not path.exists():
        print(f"文件不存在：{path}", file=sys.stderr)
        return 1

    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from app.db import SessionLocal
    from app.models import OffProduct

    db = None if args.dry_run else SessionLocal()
    seen = kept = 0
    batch: list[dict] = []

    def _flush() -> None:
        if db is None or not batch:
            return
        ins = pg_insert(OffProduct).values(batch)
        db.execute(ins.on_conflict_do_update(
            index_elements=["barcode"],
            set_={k: getattr(ins.excluded, k)
                  for k in ("name", "brand", "kcal_per_100g", "protein_g", "fat_g", "carb_g")},
        ))
        db.commit()
        batch.clear()

    with path.open(encoding="utf-8", errors="replace", newline="") as f:
        reader = csv.DictReader(f, delimiter="\t")
        missing = [v for v in _FIELDS.values() if v not in (reader.fieldnames or [])]
        if missing:
            print(f"CSV 缺列：{missing}（确认是官方全量 CSV？）", file=sys.stderr)
            return 1
        for row in reader:
            seen += 1
            if seen % 200_000 == 0:
                print(f"…已扫 {seen:,} 行，命中 {kept:,}")
            code = (row.get(_FIELDS["code"]) or "").strip()
            if not code.isdigit() or not (8 <= len(code) <= 14):
                continue
            if not _is_cn(code, row.get(_FIELDS["countries"]) or ""):
                continue
            name = (row.get(_FIELDS["name"]) or "").strip()[:100]
            kcal = _dec(row.get(_FIELDS["kcal"]) or "", 900)
            protein = _dec(row.get(_FIELDS["protein"]) or "", 100)
            fat = _dec(row.get(_FIELDS["fat"]) or "", 100)
            carb = _dec(row.get(_FIELDS["carb"]) or "", 100)
            if not name or all(v is None for v in (kcal, protein, fat, carb)):
                continue
            kept += 1
            batch.append({
                "barcode": code,
                "name": name,
                "brand": (row.get(_FIELDS["brand"]) or "").strip()[:60] or None,
                "kcal_per_100g": kcal,
                "protein_g": protein,
                "fat_g": fat,
                "carb_g": carb,
            })
            if len(batch) >= BATCH:
                _flush()
    _flush()
    if db is not None:
        db.close()
    print(f"完成：扫描 {seen:,} 行，{'将' if args.dry_run else '已'}入库 {kept:,} 条中国区产品")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
