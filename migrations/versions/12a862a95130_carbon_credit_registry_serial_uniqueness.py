"""carbon credit registry+serial uniqueness

Revision ID: 12a862a95130
Revises: 342420eea1c1
Create Date: 2026-07-11 10:21:24.237760

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '12a862a95130'
down_revision: Union[str, None] = '342420eea1c1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # SQLite cannot ALTER constraints in place -> batch (copy-and-move) mode.
    with op.batch_alter_table('carbon_credits') as batch:
        batch.create_unique_constraint('uq_credit_registry_serial',
                                       ['registry', 'serial_number'])


def downgrade() -> None:
    with op.batch_alter_table('carbon_credits') as batch:
        batch.drop_constraint('uq_credit_registry_serial', type_='unique')
