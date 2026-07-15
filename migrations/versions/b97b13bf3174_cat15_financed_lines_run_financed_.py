"""cat15 financed lines + run financed columns

Revision ID: b97b13bf3174
Revises: 2f5245958244
Create Date: 2026-07-15 14:10:14.213647

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b97b13bf3174'
down_revision: Union[str, None] = '2f5245958244'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # All additive (plain ADD COLUMN / CREATE TABLE) — no batch on FK-target tables.
    op.add_column("calculation_runs", sa.Column("financed_co2e", sa.Float(), nullable=True))
    op.add_column("calculation_runs", sa.Column("financed_as_of", sa.String(), nullable=True))
    op.add_column("calculation_runs",
                  sa.Column("financed_include_scope3", sa.Boolean(), nullable=True))
    op.add_column("calculation_runs", sa.Column("financed_fingerprint", sa.String(), nullable=True))
    op.create_table(
        "run_financed_lines",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("position_id", sa.Integer(), nullable=False),
        sa.Column("ghgp_category", sa.Integer(), nullable=False),
        sa.Column("co2e", sa.Float(), nullable=False),
        sa.Column("details", sa.Text(), nullable=False),
        sa.CheckConstraint("ghgp_category = 15", name="ck_rfl_cat15"),
        sa.CheckConstraint("co2e >= 0", name="ck_rfl_co2e_nonneg"),
        sa.ForeignKeyConstraint(["run_id"], ["calculation_runs.id"]),
        sa.ForeignKeyConstraint(["position_id"], ["financed_positions.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "position_id", name="uq_run_financed_line"),
    )


def downgrade() -> None:
    op.drop_table("run_financed_lines")
    op.drop_column("calculation_runs", "financed_fingerprint")
    op.drop_column("calculation_runs", "financed_include_scope3")
    op.drop_column("calculation_runs", "financed_as_of")
    op.drop_column("calculation_runs", "financed_co2e")
