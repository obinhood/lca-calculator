"""append-only fx/cpi reference + recorded_at

Revision ID: 083915258aeb
Revises: 8686dd35c18a
Create Date: 2026-07-10 10:44:39.875642

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '083915258aeb'
down_revision: Union[str, None] = '8686dd35c18a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # SQLite cannot ALTER constraints in place -> batch (copy-and-move) mode.
    with op.batch_alter_table('fx_rates') as batch:
        batch.add_column(sa.Column('recorded_at', sa.String(), nullable=True))
        batch.drop_constraint('uq_fx', type_='unique')
    with op.batch_alter_table('price_indices') as batch:
        batch.add_column(sa.Column('recorded_at', sa.String(), nullable=True))
        batch.drop_constraint('uq_price_index', type_='unique')


def downgrade() -> None:
    with op.batch_alter_table('price_indices') as batch:
        batch.create_unique_constraint('uq_price_index', ['currency', 'year'])
        batch.drop_column('recorded_at')
    with op.batch_alter_table('fx_rates') as batch:
        batch.create_unique_constraint('uq_fx', ['base_currency', 'quote_currency', 'year'])
        batch.drop_column('recorded_at')
