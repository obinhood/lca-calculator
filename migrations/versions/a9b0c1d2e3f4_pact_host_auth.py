"""PACT host side: OAuth2 client credentials and issued bearer tokens

Serving the PACT API means authenticating a DATA RECIPIENT, which is a different
identity from the owning organisation's own X-API-Key. A partner must be revocable
without touching the owner's access, so partners and their tokens get their own tables.

Purely additive.

Revision ID: a9b0c1d2e3f4
Revises: f8a9b0c1d2e3
"""
from alembic import op
import sqlalchemy as sa

revision = "a9b0c1d2e3f4"
down_revision = "f8a9b0c1d2e3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "pact_clients",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("organisation_id", sa.Integer(),
                  sa.ForeignKey("organisations.id"), nullable=False),
        sa.Column("client_id", sa.String(), nullable=False),
        sa.Column("client_secret_hash", sa.String(), nullable=False),
        sa.Column("partner_name", sa.String(), nullable=True),
        sa.Column("revoked", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.String(), nullable=True),
        sa.UniqueConstraint("client_id", name="uq_pact_client_id"),
    )
    op.create_table(
        "pact_tokens",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("client_id", sa.String(), nullable=False),
        sa.Column("organisation_id", sa.Integer(),
                  sa.ForeignKey("organisations.id"), nullable=False),
        sa.Column("token_hash", sa.String(), nullable=False),
        sa.Column("expires_at", sa.String(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=True),
        sa.UniqueConstraint("token_hash", name="uq_pact_token_hash"),
    )


def downgrade() -> None:
    op.drop_table("pact_tokens")
    op.drop_table("pact_clients")
