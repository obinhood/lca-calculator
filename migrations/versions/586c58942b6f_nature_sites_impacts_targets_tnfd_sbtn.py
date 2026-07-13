"""nature sites impacts targets (TNFD/SBTN)

Revision ID: 586c58942b6f
Revises: 59a92aabfff1
Create Date: 2026-07-13 11:43:50.500681

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '586c58942b6f'
down_revision: Union[str, None] = '59a92aabfff1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'nature_sites',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('organisation_id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('country', sa.String(), nullable=True),
        sa.Column('biome', sa.String(), nullable=True),
        sa.Column('latitude', sa.Float(), nullable=True),
        sa.Column('longitude', sa.Float(), nullable=True),
        sa.Column('area_hectares', sa.Float(), nullable=False, server_default='0'),
        sa.Column('in_protected_area', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('in_kba', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('water_stress', sa.String(), nullable=False, server_default='unknown'),
        sa.Column('created_at', sa.String(), nullable=True),
        sa.CheckConstraint('area_hectares >= 0', name='ck_nature_area_nonneg'),
        sa.ForeignKeyConstraint(['organisation_id'], ['organisations.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_table(
        'nature_impacts_dependencies',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('site_id', sa.Integer(), nullable=False),
        sa.Column('kind', sa.String(), nullable=False),
        sa.Column('driver', sa.String(), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('materiality', sa.String(), nullable=False, server_default='low'),
        sa.Column('metric_value', sa.Float(), nullable=True),
        sa.Column('metric_unit', sa.String(), nullable=True),
        sa.ForeignKeyConstraint(['site_id'], ['nature_sites.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_table(
        'nature_targets',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('organisation_id', sa.Integer(), nullable=False),
        sa.Column('realm', sa.String(), nullable=False),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('baseline_value', sa.Float(), nullable=False, server_default='0'),
        sa.Column('baseline_unit', sa.String(), nullable=False),
        sa.Column('baseline_year', sa.Integer(), nullable=True),
        sa.Column('target_value', sa.Float(), nullable=False, server_default='0'),
        sa.Column('target_year', sa.Integer(), nullable=False),
        sa.Column('validated', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('created_at', sa.String(), nullable=True),
        sa.CheckConstraint('target_year >= 2000 AND target_year <= 2100',
                           name='ck_nature_target_year'),
        sa.ForeignKeyConstraint(['organisation_id'], ['organisations.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade() -> None:
    op.drop_table('nature_targets')
    op.drop_table('nature_impacts_dependencies')
    op.drop_table('nature_sites')
