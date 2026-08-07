"""entity profile: size, jurisdictions and listing status

Which disclosure regimes COMPEL a filing is decided by headcount, turnover, balance-sheet
total, where the entity operates and whether it is listed — none of which the platform
previously held. All nullable: an absent figure must produce "cannot determine", never a
default that silently answers the question.

Revision ID: b3c4d5e6f7a8
Revises: a2b3c4d5e6f7
"""
from alembic import op
import sqlalchemy as sa

revision = "b3c4d5e6f7a8"
down_revision = "a2b3c4d5e6f7"
branch_labels = None
depends_on = None

_COLS = [
    ("employees", sa.Integer()),
    ("annual_turnover", sa.Float()),
    ("balance_sheet_total", sa.Float()),
    ("financials_currency", sa.String()),
    ("financials_as_of", sa.String()),
    ("jurisdictions", sa.Text()),
    ("listed_markets", sa.Text()),
]


def upgrade() -> None:
    for name, type_ in _COLS:
        op.add_column("organisations", sa.Column(name, type_, nullable=True))


def downgrade() -> None:
    for name, _type in reversed(_COLS):
        op.drop_column("organisations", name)
