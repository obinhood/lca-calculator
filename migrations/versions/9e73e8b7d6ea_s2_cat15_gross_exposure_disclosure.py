"""s2 cat15 gross exposure disclosure

Revision ID: 9e73e8b7d6ea
Revises: 8e1c907fc8ee
Create Date: 2026-07-17 10:11:51.558587

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '9e73e8b7d6ea'
down_revision: Union[str, None] = '8e1c907fc8ee'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Additive only. IFRS S2 B58-B63 gross exposure, on the live Cat 15 declaration
    # and its frozen per-run copy.
    for t in ("scope3_category_declarations", "run_scope3_declarations"):
        op.add_column(t, sa.Column("gross_exposure_total", sa.Float(), nullable=True))
        op.add_column(t, sa.Column("gross_exposure_currency", sa.String(), nullable=True))


def downgrade() -> None:
    for t in ("run_scope3_declarations", "scope3_category_declarations"):
        op.drop_column(t, "gross_exposure_currency")
        op.drop_column(t, "gross_exposure_total")
