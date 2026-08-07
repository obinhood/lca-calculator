"""freeze the reporting entity's sector onto each run

The sector routes the Scope 3 relevance challenge, so it has to be frozen with the run
like every other basis — otherwise editing the organisation profile silently changes what
a past run's screening was judged against. NULL on existing runs: no sector challenge ran
for them, which the completeness payload reports rather than hiding.

Revision ID: a2b3c4d5e6f7
Revises: f1a2b3c4d5e6
"""
from alembic import op
import sqlalchemy as sa

revision = "a2b3c4d5e6f7"
down_revision = "f1a2b3c4d5e6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("calculation_runs", sa.Column("organisation_sector", sa.String(),
                                                nullable=True))


def downgrade() -> None:
    op.drop_column("calculation_runs", "organisation_sector")
