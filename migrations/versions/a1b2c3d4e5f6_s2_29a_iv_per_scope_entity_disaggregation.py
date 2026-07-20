"""s2 29(a)(iv) per-scope entity disaggregation

Freeze Scope 1 and Scope 2 (location-based) consolidated emissions PER ENTITY on
run_entity_boundary so the IFRS S2 ¶29(a)(iv) disaggregation between the
consolidated accounting group and other investees can be reported per scope,
rather than the all-scope figure it reported before.

Additive and reversible. Both columns are nullable: runs frozen before this
dimension keep NULL, and the summary renderer falls back to the all-scope figure
with disaggregation_scope_split_available=False (reproduction contract — a legacy
run renders exactly what it froze, never a back-filled per-scope claim).

Revision ID: a1b2c3d4e5f6
Revises: 3fefecad7c9f
Create Date: 2026-07-20 12:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, None] = '3fefecad7c9f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Additive only. run_entity_boundary is not an FK target, so a plain add_column
    # is safe under PRAGMA foreign_keys=ON (no batch recreate needed).
    op.add_column("run_entity_boundary",
                  sa.Column("scope1_consolidated_co2e", sa.Float(), nullable=True))
    op.add_column("run_entity_boundary",
                  sa.Column("scope2_consolidated_co2e", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("run_entity_boundary", "scope2_consolidated_co2e")
    op.drop_column("run_entity_boundary", "scope1_consolidated_co2e")
