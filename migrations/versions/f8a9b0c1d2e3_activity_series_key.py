"""preparer-declared series key on the activity

Unlocks period-over-period screening, which the screening register had to decline:
ActivityRecord carried no series identity, so any key INFERRED from the available
columns would merge two physically distinct sites and report their sum as one trend.

Declared, never inferred, and never written back by the engine — the same doctrine as
`scope` and `ghgp_category`. NULL means "not enrolled in period-over-period screening",
which is the default for every existing row, so no detector can fire on historical data
and the blast radius is zero by construction rather than by threshold tuning.

Purely additive: one nullable indexed column.

Revision ID: f8a9b0c1d2e3
Revises: e7f8a9b0c1d2
"""
from alembic import op
import sqlalchemy as sa

revision = "f8a9b0c1d2e3"
down_revision = "e7f8a9b0c1d2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("activities", sa.Column("series_key", sa.String(), nullable=True))
    op.create_index("ix_activities_series_key", "activities", ["series_key"])


def downgrade() -> None:
    op.drop_index("ix_activities_series_key", table_name="activities")
    op.drop_column("activities", "series_key")
