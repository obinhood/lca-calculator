"""preparer-declared crosswalk chain on the activity

Closes the half of the crosswalk work that was missing. services/crosswalk.py could
measure a hop's uncertainty, but nothing consumed it: the calculator was standalone and
the Monte Carlo band never widened for a spend line mapped through three ambiguous hops.

The chain is DECLARED, never inferred — the same doctrine as `scope` and `series_key`.
NULL means no chain was declared and the propagation adds nothing, which is honest
rather than clean: the error is still there, it is simply not quantified, and the
payload says so.

Purely additive.

Revision ID: d2e3f4a5b6c7
Revises: c1d2e3f4a5b6
"""
from alembic import op
import sqlalchemy as sa

revision = "d2e3f4a5b6c7"
down_revision = "c1d2e3f4a5b6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("activities", sa.Column("crosswalk_chain", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("activities", "crosswalk_chain")
