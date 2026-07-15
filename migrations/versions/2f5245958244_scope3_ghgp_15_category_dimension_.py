"""scope3 ghgp 15-category dimension + declarations

Revision ID: 2f5245958244
Revises: 4f04ee0a5694
Create Date: 2026-07-14 13:45:44.426262

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '2f5245958244'
down_revision: Union[str, None] = '4f04ee0a5694'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # EVERY operation here is ADDITIVE (plain ADD COLUMN / CREATE TABLE). No
    # batch_alter_table: recreating `activities` or `calculation_runs` would mean a
    # create-copy-drop-rename of an FK-target table while app.database forces
    # PRAGMA foreign_keys=ON — the riskiest thing this change could do. That is also
    # why activities.ghgp_category carries NO DB CHECK (the 1..15 range is enforced
    # in code); a constraint on the model but not here would exist in tests
    # (create_all) and not in production (alembic).
    op.add_column("activities", sa.Column("ghgp_category", sa.Integer(), nullable=True))

    # NULL ghgp_standard_version is the LEGACY-RUN sentinel.
    op.add_column("calculation_runs",
                  sa.Column("ghgp_standard_version", sa.String(), nullable=True))
    op.add_column("calculation_runs",
                  sa.Column("ghgp_map_version", sa.String(), nullable=True))
    op.add_column("calculation_runs",
                  sa.Column("scope3_declaration_fingerprint", sa.String(), nullable=True))

    op.create_table(
        "scope3_category_declarations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("organisation_id", sa.Integer(), nullable=False),
        sa.Column("reporting_period_id", sa.Integer(), nullable=False),
        sa.Column("category", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("justification", sa.Text(), nullable=True),
        sa.Column("screening_estimate_tco2e", sa.Float(), nullable=True),
        sa.Column("screening_method", sa.Text(), nullable=True),
        sa.Column("materiality_threshold_pct", sa.Float(), nullable=True),
        sa.Column("criteria", sa.Text(), nullable=True),
        sa.Column("minimum_boundary_met", sa.Boolean(), nullable=True),
        sa.Column("method_description", sa.Text(), nullable=True),
        sa.Column("calculation_tools", sa.Text(), nullable=True),
        sa.Column("primary_data_pct", sa.Float(), nullable=True),
        sa.Column("screened_at", sa.String(), nullable=False),
        sa.Column("declared_by", sa.String(), nullable=True),
        sa.Column("standard_version", sa.String(), nullable=False,
                  server_default="ghgp-scope3-2011"),
        sa.Column("created_at", sa.String(), nullable=True),
        sa.Column("updated_at", sa.String(), nullable=True),
        sa.CheckConstraint("category >= 1 AND category <= 15", name="ck_s3decl_cat"),
        sa.CheckConstraint(
            "status IN ('included','not_applicable','not_material','not_measured')",
            name="ck_s3decl_status"),
        sa.CheckConstraint("screening_estimate_tco2e IS NULL OR screening_estimate_tco2e >= 0",
                           name="ck_s3decl_est_nonneg"),
        sa.CheckConstraint("materiality_threshold_pct IS NULL OR "
                           "(materiality_threshold_pct >= 0 AND materiality_threshold_pct <= 100)",
                           name="ck_s3decl_thresh"),
        sa.ForeignKeyConstraint(["organisation_id"], ["organisations.id"]),
        sa.ForeignKeyConstraint(["reporting_period_id"], ["reporting_periods.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("organisation_id", "reporting_period_id", "category",
                            name="uq_s3decl_org_period_cat"),
    )

    op.create_table(
        "run_scope3_declarations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("category", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("declaration_id", sa.Integer(), nullable=True),
        sa.Column("justification", sa.Text(), nullable=True),
        sa.Column("screening_estimate_tco2e", sa.Float(), nullable=True),
        sa.Column("screening_method", sa.Text(), nullable=True),
        sa.Column("materiality_threshold_pct", sa.Float(), nullable=True),
        sa.Column("criteria", sa.Text(), nullable=True),
        sa.Column("minimum_boundary_met", sa.Boolean(), nullable=True),
        sa.Column("method_description", sa.Text(), nullable=True),
        sa.Column("calculation_tools", sa.Text(), nullable=True),
        sa.Column("primary_data_pct", sa.Float(), nullable=True),
        sa.Column("screened_at", sa.String(), nullable=True),
        sa.Column("ghgp_standard_version", sa.String(), nullable=False),
        sa.Column("frozen_at", sa.String(), nullable=False),
        sa.CheckConstraint("category >= 1 AND category <= 15", name="ck_run_s3decl_cat"),
        sa.CheckConstraint(
            "status IN ('included','not_applicable','not_material','not_measured','undeclared')",
            name="ck_run_s3decl_status"),
        sa.ForeignKeyConstraint(["run_id"], ["calculation_runs.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "category", name="uq_run_s3decl"),
    )


def downgrade() -> None:
    op.drop_table("run_scope3_declarations")
    op.drop_table("scope3_category_declarations")
    op.drop_column("calculation_runs", "scope3_declaration_fingerprint")
    op.drop_column("calculation_runs", "ghgp_map_version")
    op.drop_column("calculation_runs", "ghgp_standard_version")
    op.drop_column("activities", "ghgp_category")
