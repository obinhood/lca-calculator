"""auth api keys + mapping review gate

Revision ID: c490ab48eb23
Revises: 85fd4ab1e9cb
Create Date: 2026-07-09 21:31:01.287166

SQLite cannot ALTER constraints in place, so the activities changes use batch
mode (copy-and-move) to add the suggested_factor_id FK properly.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c490ab48eb23'
down_revision: Union[str, None] = '85fd4ab1e9cb'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('activities') as batch:
        batch.add_column(sa.Column('suggested_factor_id', sa.Integer(), nullable=True))
        batch.add_column(sa.Column('mapping_status', sa.String(), nullable=True))
        batch.add_column(sa.Column('mapping_basis', sa.String(), nullable=True))
        batch.create_foreign_key('fk_activities_suggested_factor', 'emission_factors',
                                 ['suggested_factor_id'], ['id'])
    op.add_column('organisations', sa.Column('api_key_hash', sa.String(), nullable=True))
    op.create_index(op.f('ix_organisations_api_key_hash'), 'organisations',
                    ['api_key_hash'], unique=True)


def downgrade() -> None:
    op.drop_index(op.f('ix_organisations_api_key_hash'), table_name='organisations')
    op.drop_column('organisations', 'api_key_hash')
    with op.batch_alter_table('activities') as batch:
        batch.drop_constraint('fk_activities_suggested_factor', type_='foreignkey')
        batch.drop_column('mapping_basis')
        batch.drop_column('mapping_status')
        batch.drop_column('suggested_factor_id')
