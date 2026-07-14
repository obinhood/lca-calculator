"""audit phase0: factor value + instrument rate CHECK constraints

Revision ID: 4f04ee0a5694
Revises: 586c58942b6f
Create Date: 2026-07-14 10:44:38.396991

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '4f04ee0a5694'
down_revision: Union[str, None] = '586c58942b6f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # A negative emission factor would turn a source into a sink and understate
    # the total; a negative instrument rate would do the same to market Scope 2.
    # The instrument CHECK exists on the model but was never in a migration
    # (ORM-vs-DB drift); this reconciles it. SQLite needs batch (table recreate),
    # and recreating emission_factors — which activities/lca_items reference —
    # trips the FK enforcement app.database enables (PRAGMA foreign_keys=ON), so
    # toggle FK off OUTSIDE the migration transaction (PRAGMA is a no-op inside one).
    with op.get_context().autocommit_block():
        op.execute("PRAGMA foreign_keys=OFF")
    with op.batch_alter_table("emission_factors", schema=None) as batch_op:
        batch_op.create_check_constraint("ck_factor_value_nonneg", "value >= 0")
    with op.batch_alter_table("market_instruments", schema=None) as batch_op:
        batch_op.create_check_constraint("ck_instrument_rate_nonneg", "kg_co2e_per_kwh >= 0")
    with op.get_context().autocommit_block():
        op.execute("PRAGMA foreign_keys=ON")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("PRAGMA foreign_keys=OFF")
    with op.batch_alter_table("market_instruments", schema=None) as batch_op:
        batch_op.drop_constraint("ck_instrument_rate_nonneg", type_="check")
    with op.batch_alter_table("emission_factors", schema=None) as batch_op:
        batch_op.drop_constraint("ck_factor_value_nonneg", type_="check")
    with op.get_context().autocommit_block():
        op.execute("PRAGMA foreign_keys=ON")
