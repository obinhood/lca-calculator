"""inventory removals lsrg dimension

Revision ID: 3fefecad7c9f
Revises: 00871ee113e9
Create Date: 2026-07-20 11:06:30.499258

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '3fefecad7c9f'
down_revision: Union[str, None] = '00871ee113e9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Additive only. No batch_alter_table on calculation_runs (FK target under
    # PRAGMA foreign_keys=ON). removal_records.entity_id is a plain indexed Integer
    # (no FK — matches ActivityRecord.entity_id, preserves the fail-open path).
    for col in (
        sa.Column("total_removals_co2e", sa.Float(), nullable=True),
        sa.Column("removals_reversed_co2e", sa.Float(), nullable=True),
        sa.Column("removals_as_of", sa.String(), nullable=True),
        sa.Column("removals_fingerprint", sa.String(), nullable=True),
        sa.Column("removals_lsrg_version", sa.String(), nullable=True),
    ):
        op.add_column("calculation_runs", col)

    op.create_table(
        "removal_records",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("organisation_id", sa.Integer(), nullable=False),
        sa.Column("entity_id", sa.Integer(), nullable=True),
        sa.Column("reporting_period_id", sa.Integer(), nullable=True),
        sa.Column("record_kind", sa.String(), nullable=False),
        sa.Column("reverses_record_id", sa.Integer(), nullable=True),
        sa.Column("removal_category", sa.String(), nullable=False),
        sa.Column("method", sa.String(), nullable=False),
        sa.Column("scope", sa.String(), nullable=False),
        sa.Column("quantity_tco2e", sa.Float(), nullable=False),
        sa.Column("quantification_method", sa.String(), nullable=False),
        sa.Column("storage_medium", sa.String(), nullable=True),
        sa.Column("expected_durability_years", sa.Integer(), nullable=True),
        sa.Column("monitoring_method", sa.Text(), nullable=True),
        sa.Column("monitoring_period_years", sa.Integer(), nullable=True),
        sa.Column("reversal_accounting", sa.Text(), nullable=True),
        sa.Column("attribute_retained", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("credit_registry", sa.String(), nullable=True),
        sa.Column("credit_serial_if_sold", sa.String(), nullable=True),
        sa.Column("uncertainty_pct", sa.Float(), nullable=True),
        sa.Column("buffer_pct", sa.Float(), nullable=True),
        sa.Column("vintage_year", sa.Integer(), nullable=True),
        sa.Column("as_of_date", sa.String(), nullable=True),
        sa.Column("created_at", sa.String(), nullable=True),
        sa.CheckConstraint("quantity_tco2e > 0", name="ck_removal_qty_pos"),
        sa.CheckConstraint("removal_category IN ('technological','land_based')",
                           name="ck_removal_category"),
        sa.CheckConstraint("record_kind IN ('removal','reversal')", name="ck_removal_record_kind"),
        sa.CheckConstraint("scope IN ('1','3')", name="ck_removal_scope"),
        sa.ForeignKeyConstraint(["organisation_id"], ["organisations.id"]),
        sa.ForeignKeyConstraint(["reporting_period_id"], ["reporting_periods.id"]),
        sa.ForeignKeyConstraint(["reverses_record_id"], ["removal_records.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_removal_records_entity_id", "removal_records", ["entity_id"])

    op.create_table(
        "run_removal_lines",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("removal_record_id", sa.Integer(), nullable=False),
        sa.Column("removal_category", sa.String(), nullable=False),
        sa.Column("scope", sa.String(), nullable=False),
        sa.Column("record_kind", sa.String(), nullable=False),
        sa.Column("co2e", sa.Float(), nullable=False),
        sa.Column("details", sa.Text(), nullable=False),
        sa.CheckConstraint("co2e >= 0", name="ck_rrl_co2e_nonneg"),
        sa.CheckConstraint("record_kind IN ('removal','reversal')", name="ck_rrl_record_kind"),
        sa.ForeignKeyConstraint(["run_id"], ["calculation_runs.id"]),
        sa.ForeignKeyConstraint(["removal_record_id"], ["removal_records.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "removal_record_id", name="uq_run_removal_line"),
    )


def downgrade() -> None:
    op.drop_table("run_removal_lines")
    op.drop_index("ix_removal_records_entity_id", table_name="removal_records")
    op.drop_table("removal_records")
    for c in ("removals_lsrg_version", "removals_fingerprint", "removals_as_of",
              "removals_reversed_co2e", "total_removals_co2e"):
        op.drop_column("calculation_runs", c)
