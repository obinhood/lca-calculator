"""scope3 boundary policy version

Records which factor-boundary ACCEPTANCE VOCABULARY (Table 5.4 token policy) produced a
run's per-line minimum-boundary verdicts. Versioned apart from GHGP_STANDARD_VERSION
because the token set is OUR interpretation of the Protocol's prose minimum, not Protocol
content (same precedent as CATEGORY_MAP_VERSION).

Additive and reversible. Nullable with NO back-fill: a run computed before the policy was
versioned keeps NULL, and boundary_policy_for_run() reports it as "s3bnd-v1 (inferred)" at
render time rather than writing an inferred value into history.

Revision ID: b7c1d9e2f3a4
Revises: a1b2c3d4e5f6
Create Date: 2026-07-21 01:20:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b7c1d9e2f3a4'
down_revision: Union[str, None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Additive only. calculation_runs is an FK target, so no batch_alter_table under
    # PRAGMA foreign_keys=ON — a plain nullable add_column is safe.
    op.add_column("calculation_runs",
                  sa.Column("ghgp_boundary_policy_version", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("calculation_runs", "ghgp_boundary_policy_version")
