"""market instrument grid market attribute

Revision ID: 8e1c907fc8ee
Revises: b97b13bf3174
Create Date: 2026-07-16 00:29:45.044343

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '8e1c907fc8ee'
down_revision: Union[str, None] = 'b97b13bf3174'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("market_instruments", sa.Column("market", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("market_instruments", "market")
