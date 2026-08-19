"""PACT Pathfinder v3 product footprints

The WBCSD Partnership for Carbon Transparency defines a data model and REST API for
exchanging product carbon footprints. This holds them: a supplier's PCF we consumed
(direction 'received') or one of ours offered to customers ('published').

`document` is the received bytes, verbatim; every other column is derived from it for
querying. v3 versioning is IMMUTABLE — a correction arrives as a new pf_id listing the
old one in preceding_pf_ids — so there is deliberately no `version` column.

Purely additive.

Revision ID: d6e7f8a9b0c1
Revises: c5d6e7f8a9b0
"""
from alembic import op
import sqlalchemy as sa

revision = "d6e7f8a9b0c1"
down_revision = "c5d6e7f8a9b0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "product_footprints",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("organisation_id", sa.Integer(),
                  sa.ForeignKey("organisations.id"), nullable=False),
        sa.Column("direction", sa.String(), nullable=False),
        sa.Column("pf_id", sa.String(), nullable=False),
        sa.Column("spec_version", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="Active"),
        sa.Column("created", sa.String(), nullable=True),
        sa.Column("preceding_pf_ids", sa.Text(), nullable=True),
        sa.Column("company_name", sa.String(), nullable=True),
        sa.Column("company_ids", sa.Text(), nullable=True),
        sa.Column("product_name", sa.String(), nullable=True),
        sa.Column("product_description", sa.Text(), nullable=True),
        sa.Column("product_ids", sa.Text(), nullable=True),
        sa.Column("declared_unit", sa.String(), nullable=True),
        sa.Column("declared_unit_amount", sa.Float(), nullable=True),
        sa.Column("kg_co2e_per_unit_excl_biogenic", sa.Float(), nullable=True),
        sa.Column("kg_co2e_per_unit_incl_biogenic", sa.Float(), nullable=True),
        sa.Column("reference_period_start", sa.String(), nullable=True),
        sa.Column("reference_period_end", sa.String(), nullable=True),
        sa.Column("validity_period_start", sa.String(), nullable=True),
        sa.Column("validity_period_end", sa.String(), nullable=True),
        sa.Column("primary_data_share", sa.Float(), nullable=True),
        sa.Column("geography_level", sa.String(), nullable=True),
        sa.Column("geography_value", sa.String(), nullable=True),
        sa.Column("dqi_technological", sa.Float(), nullable=True),
        sa.Column("dqi_geographical", sa.Float(), nullable=True),
        sa.Column("dqi_temporal", sa.Float(), nullable=True),
        sa.Column("document", sa.Text(), nullable=False),
        sa.Column("source_url", sa.String(), nullable=True),
        sa.Column("validation_warnings", sa.Text(), nullable=True),
        sa.Column("received_at", sa.String(), nullable=True),
        sa.Column("created_at", sa.String(), nullable=True),
        sa.UniqueConstraint("organisation_id", "pf_id", name="uq_pf_org_pfid"),
        sa.CheckConstraint("direction IN ('received','published')", name="ck_pf_direction"),
        sa.CheckConstraint("status IN ('Active','Deprecated')", name="ck_pf_status"),
        sa.CheckConstraint("declared_unit_amount > 0", name="ck_pf_declared_amount_pos"),
    )


def downgrade() -> None:
    op.drop_table("product_footprints")
