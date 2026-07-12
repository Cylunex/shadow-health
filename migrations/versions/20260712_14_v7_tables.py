"""V7 批次表：foods.barcode + off_products（条码离线库）+ fitness_tests（体测）
+ lab_results（体检化验档案）

- foods.barcode：自建食物关联条码（扫码直达）；唯一、可空
- off_products：Open Food Facts 中国区子集本地缓存（scripts/import_off_products.py
  导入，完全离线查询；扫到未建档条码时一键从这里建 foods）
- fitness_tests：周期体测协议（俯卧撑/平板/坐位体前屈/心率恢复），(test_date,item) 唯一
- lab_results：体检化验结构化档案（(report_date,item_key) 唯一，多年趋势）

Revision ID: 20260712u14
Revises: 20260712u13
Create Date: 2026-07-12

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260712u14"
down_revision: Union[str, None] = "20260712u13"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("foods", sa.Column("barcode", sa.Text(), nullable=True), schema="health")
    op.create_unique_constraint("ux_foods_barcode", "foods", ["barcode"], schema="health")

    op.create_table(
        "off_products",
        sa.Column("barcode", sa.Text(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("brand", sa.Text(), nullable=True),
        sa.Column("kcal_per_100g", sa.Numeric(7, 1), nullable=True),
        sa.Column("protein_g", sa.Numeric(6, 1), nullable=True),
        sa.Column("fat_g", sa.Numeric(6, 1), nullable=True),
        sa.Column("carb_g", sa.Numeric(6, 1), nullable=True),
        sa.Column(
            "updated_at", sa.TIMESTAMP(timezone=True),
            nullable=False, server_default=sa.text("now()"),
        ),
        schema="health",
    )

    op.create_table(
        "fitness_tests",
        sa.Column("id", sa.Integer(), sa.Identity(always=True), primary_key=True),
        sa.Column("test_date", sa.Date(), nullable=False),
        sa.Column("item", sa.Text(), nullable=False),
        sa.Column("value", sa.Numeric(7, 1), nullable=False),
        sa.UniqueConstraint("test_date", "item", name="ux_fitness_tests_day_item"),
        schema="health",
    )

    op.create_table(
        "lab_results",
        sa.Column("id", sa.Integer(), sa.Identity(always=True), primary_key=True),
        sa.Column("report_date", sa.Date(), nullable=False),
        sa.Column("item_key", sa.Text(), nullable=False),
        sa.Column("item_label", sa.Text(), nullable=False),
        sa.Column("value", sa.Numeric(10, 3), nullable=False),
        sa.Column("unit", sa.Text(), nullable=True),
        sa.Column("ref_low", sa.Numeric(10, 3), nullable=True),
        sa.Column("ref_high", sa.Numeric(10, 3), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.TIMESTAMP(timezone=True),
            nullable=False, server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("report_date", "item_key", name="ux_lab_results_day_item"),
        schema="health",
    )


def downgrade() -> None:
    op.drop_table("lab_results", schema="health")
    op.drop_table("fitness_tests", schema="health")
    op.drop_table("off_products", schema="health")
    op.drop_constraint("ux_foods_barcode", "foods", schema="health", type_="unique")
    op.drop_column("foods", "barcode", schema="health")
