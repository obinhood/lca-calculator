"""activity coverage window (temporal straddle proration)

An ActivityRecord carries a single `date`, so a supply invoice covering 15 Dec - 15 Jan
was attributed WHOLLY to whichever fiscal year that one date fell in. Declaring the
consumption window lets the engine prorate the quantity by the overlapping share, so the
emissions land in the year they occurred.

Both columns nullable and NEVER back-filled: with no window a record is attributed wholly
by `date`, exactly as before, so every existing activity and every filed run is unchanged.
The platform cannot infer a window it was not told, and inventing one would move a
disclosed number on a guess.

Revision ID: e5b3c8d1f2a7
Revises: d4a7c1e9b2f5
Create Date: 2026-07-22 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e5b3c8d1f2a7'
down_revision: Union[str, None] = 'd4a7c1e9b2f5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # `activities` IS an FK target (emission_line_items references it), so plain
    # add_column only — never batch_alter_table under PRAGMA foreign_keys=ON.
    op.add_column("activities", sa.Column("coverage_start", sa.String(), nullable=True))
    op.add_column("activities", sa.Column("coverage_end", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("activities", "coverage_end")
    op.drop_column("activities", "coverage_start")
