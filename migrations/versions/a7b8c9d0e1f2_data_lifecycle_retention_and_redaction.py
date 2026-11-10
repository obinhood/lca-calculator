"""data lifecycle: per-org retention period and activity redaction tombstone

Revision ID: a7b8c9d0e1f2
Revises: a5b6c7d8e9f0
Create Date: 2026-11-10 11:40:00.000000

Three nullable columns, all ADDITIVE (plain ADD COLUMN). No batch_alter_table:
`organisations` and `activities` are both FK TARGETS, and a create-copy-drop-rename
under PRAGMA foreign_keys=ON is the riskiest operation available in this schema — the
same reasoning recorded in 00871ee113e9.

Every column is NULLABLE with no server default, and that is load-bearing rather than
lazy:

* `organisations.retention_days` NULL means "keep indefinitely", NOT "keep zero days".
  A processor must never invent a retention period on the controller's behalf, and a
  back-filled default here would start erasing customers' inventories on the first
  retention run.

* `activities.redacted_at` / `redaction_reason` NULL means "never redacted". These must
  stay distinguishable from a row whose text was erased, which is why the redaction
  writes a self-describing tombstone into `description` rather than NULLing it.

None of the three is in `calc.activities_fingerprint()`, so applying this migration and
redacting a row leaves every previously filed calculation run reporting the same
freshness it reported before. That is what makes erasure compatible with the
reproduction contract; see app/services/lifecycle.py for the full argument.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
# Chains off a5b6c7d8e9f0 (market instrument idempotency), NOT f4a5b6c7d8e9: that
# revision and this one were written in parallel branches and both originally claimed
# f4a5b6c7d8e9 as their parent, which gives TWO Alembic heads once both are merged and
# makes `alembic upgrade head` fail outright — the container then exits before gunicorn
# starts, which is the exact failure mode the PostgreSQL work removed. The two migrations
# touch disjoint tables (organisations/activities here, market_instruments/
# calculation_runs there), so serialising them is safe and not merely expedient.
revision: str = 'a7b8c9d0e1f2'
down_revision: Union[str, None] = 'a5b6c7d8e9f0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('organisations',
                  sa.Column('retention_days', sa.Integer(), nullable=True))
    op.add_column('activities',
                  sa.Column('redacted_at', sa.String(), nullable=True))
    op.add_column('activities',
                  sa.Column('redaction_reason', sa.String(), nullable=True))


def downgrade() -> None:
    # Dropping these loses the record that an erasure happened — the rows stay redacted
    # (the tombstone is in `description`) but nothing says when or why. Acceptable only
    # because a downgrade is a schema rollback, not a data-recovery operation.
    op.drop_column('activities', 'redaction_reason')
    op.drop_column('activities', 'redacted_at')
    op.drop_column('organisations', 'retention_days')
