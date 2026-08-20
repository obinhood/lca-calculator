"""versioned classification crosswalks

The emission-factor registry has long recorded that mapping a chart of accounts through
UNSPSC to NAICS or NACE adds error frequently larger than the factor's own. This gives
those hops an identity, a version and a measurable uncertainty.

Version-pinned deliberately: a frozen report must freeze its crosswalks exactly as it
freezes FX rates, or a concordance revision silently moves a filed figure.

Purely additive.

Revision ID: b0c1d2e3f4a5
Revises: a9b0c1d2e3f4
"""
from alembic import op
import sqlalchemy as sa

revision = "b0c1d2e3f4a5"
down_revision = "a9b0c1d2e3f4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "crosswalks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("from_scheme", sa.String(), nullable=False),
        sa.Column("to_scheme", sa.String(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("table_version", sa.String(), nullable=False),
        sa.Column("licence", sa.String(), nullable=True),
        sa.Column("url", sa.String(), nullable=True),
        sa.Column("uncitable", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.String(), nullable=True),
        sa.UniqueConstraint("from_scheme", "to_scheme", "table_version",
                            name="uq_crosswalk_scheme_version"),
    )
    op.create_table(
        "crosswalk_mappings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("crosswalk_id", sa.Integer(),
                  sa.ForeignKey("crosswalks.id"), nullable=False),
        sa.Column("from_code", sa.String(), nullable=False),
        sa.Column("to_code", sa.String(), nullable=False),
        sa.Column("partial", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("note", sa.Text(), nullable=True),
        sa.UniqueConstraint("crosswalk_id", "from_code", "to_code",
                            name="uq_crosswalk_mapping"),
    )
    op.create_index("ix_crosswalk_mappings_from_code", "crosswalk_mappings",
                    ["from_code"])


def downgrade() -> None:
    op.drop_index("ix_crosswalk_mappings_from_code", table_name="crosswalk_mappings")
    op.drop_table("crosswalk_mappings")
    op.drop_table("crosswalks")
