"""ghgp ch3 organisational boundary

Revision ID: 00871ee113e9
Revises: 9e73e8b7d6ea
Create Date: 2026-07-17 11:03:22.028271

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '00871ee113e9'
down_revision: Union[str, None] = '9e73e8b7d6ea'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # All ADDITIVE (plain ADD COLUMN / CREATE TABLE). No batch_alter_table: activities,
    # organisations and calculation_runs are FK targets, and a create-copy-drop-rename
    # under PRAGMA foreign_keys=ON is the riskiest operation available. activities.entity_id
    # deliberately carries NO FK (an FK can't enforce the tenant match, which is the check
    # that matters) and NO CHECK, so create_all (tests) and alembic (prod) agree exactly.
    op.add_column("activities", sa.Column("entity_id", sa.Integer(), nullable=True))
    op.create_index("ix_activities_entity_id", "activities", ["entity_id"])
    op.add_column("organisations",
                  sa.Column("consolidation_approach_reason", sa.Text(), nullable=True))
    for col in (
        sa.Column("boundary_version", sa.String(), nullable=True),
        sa.Column("consolidation_approach", sa.String(), nullable=True),
        sa.Column("consolidation_reason", sa.Text(), nullable=True),
        sa.Column("consolidation_fingerprint", sa.String(), nullable=True),
        sa.Column("total_co2e_non_consolidated", sa.Float(), nullable=True),
    ):
        op.add_column("calculation_runs", col)

    op.create_table(
        "reporting_entities",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("organisation_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("entity_ref", sa.String(), nullable=True),
        sa.Column("accounting_category", sa.String(), nullable=False),
        sa.Column("equity_share_pct", sa.Float(), nullable=True),
        sa.Column("equity_share_basis", sa.Text(), nullable=True),
        sa.Column("financial_control", sa.Boolean(), nullable=True),
        sa.Column("joint_financial_control", sa.Boolean(), nullable=True),
        sa.Column("operational_control", sa.Boolean(), nullable=True),
        sa.Column("control_rationale", sa.Text(), nullable=True),
        sa.Column("in_consolidated_accounting_group", sa.Boolean(), nullable=True),
        sa.Column("effective_from", sa.String(), nullable=True),
        sa.Column("effective_to", sa.String(), nullable=True),
        sa.Column("created_at", sa.String(), nullable=True),
        sa.CheckConstraint("equity_share_pct IS NULL OR "
                           "(equity_share_pct >= 0 AND equity_share_pct <= 100)",
                           name="ck_entity_equity_pct_range"),
        sa.CheckConstraint("accounting_category IN ('subsidiary','joint_venture_incorporated',"
                           "'joint_operation','associate','fixed_asset_investment',"
                           "'franchise','lease_finance','lease_operating')",
                           name="ck_entity_acct_category"),
        sa.CheckConstraint("NOT (financial_control = 1 AND joint_financial_control = 1)",
                           name="ck_entity_joint_vs_sole_fc"),
        sa.ForeignKeyConstraint(["organisation_id"], ["organisations.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("organisation_id", "name", name="uq_entity_org_name"),
    )

    op.create_table(
        "run_entity_boundary",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("entity_key", sa.String(), nullable=False),
        sa.Column("entity_id", sa.Integer(), nullable=True),
        sa.Column("entity_name", sa.String(), nullable=False),
        sa.Column("entity_ref", sa.String(), nullable=True),
        sa.Column("accounting_category", sa.String(), nullable=False),
        sa.Column("equity_share_pct", sa.Float(), nullable=True),
        sa.Column("equity_share_basis", sa.Text(), nullable=True),
        sa.Column("financial_control", sa.Boolean(), nullable=True),
        sa.Column("joint_financial_control", sa.Boolean(), nullable=True),
        sa.Column("operational_control", sa.Boolean(), nullable=True),
        sa.Column("control_rationale", sa.Text(), nullable=True),
        sa.Column("in_consolidated_accounting_group", sa.Boolean(), nullable=True),
        sa.Column("effective_from", sa.String(), nullable=True),
        sa.Column("effective_to", sa.String(), nullable=True),
        sa.Column("approach", sa.String(), nullable=False),
        sa.Column("share_factor", sa.Float(), nullable=False),
        sa.Column("share_basis", sa.String(), nullable=False),
        sa.Column("resolved", sa.Boolean(), nullable=False),
        sa.Column("group_class", sa.String(), nullable=False),
        sa.Column("gross_co2e", sa.Float(), nullable=False),
        sa.Column("consolidated_co2e", sa.Float(), nullable=False),
        sa.Column("line_count", sa.Integer(), nullable=False),
        sa.Column("boundary_version", sa.String(), nullable=False),
        sa.Column("frozen_at", sa.String(), nullable=False),
        sa.CheckConstraint("share_factor >= 0 AND share_factor <= 1",
                           name="ck_reb_share_range"),
        sa.CheckConstraint("group_class IN ('consolidated_accounting_group','other_investee',"
                           "'unclassified')", name="ck_reb_group_class"),
        sa.ForeignKeyConstraint(["run_id"], ["calculation_runs.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "entity_key", name="uq_run_entity_boundary"),
    )


def downgrade() -> None:
    op.drop_table("run_entity_boundary")
    op.drop_table("reporting_entities")
    for c in ("total_co2e_non_consolidated", "consolidation_fingerprint",
              "consolidation_reason", "consolidation_approach", "boundary_version"):
        op.drop_column("calculation_runs", c)
    op.drop_column("organisations", "consolidation_approach_reason")
    op.drop_index("ix_activities_entity_id", table_name="activities")
    op.drop_column("activities", "entity_id")
