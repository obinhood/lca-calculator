"""emission factor tenancy: a factor belongs to the platform or to one tenant

`GET /factors` served every row in `emission_factors` to anonymous callers, and the
table had no tenant column — so a supplier's PACT product footprint, materialised as
an ordinary factor by services/pact_factor.materialise, was readable by anybody with
the URL, and a licensed catalogue an operator loaded was republished to the internet.

Purely additive and NULL-safe. `organisation_id IS NULL` means platform reference data
(DEFRA, USEEIO, anything scripts/load_factors.py loads) and stays readable by every
tenant, so every existing row keeps exactly the visibility it had and no filed run
changes. Only rows written with an organisation_id — PACT materialisations from here
on — become private, which is the behaviour that was missing.

The rule itself lives in ONE place, models.factor_visible_to(); this migration only
gives it a column to read. The index matters because that predicate now sits on the
resolver's candidate query, which runs per unmapped activity row during ingestion.

Revision ID: e3f4a5b6c7d8
Revises: d2e3f4a5b6c7
"""
from alembic import op
import sqlalchemy as sa

revision = "e3f4a5b6c7d8"
down_revision = "d2e3f4a5b6c7"
branch_labels = None
depends_on = None


def upgrade():
    # batch_alter_table so SQLite gets the table rebuild it needs for a new FK;
    # Postgres takes the plain ALTER. Existing rows default to NULL = platform data.
    with op.batch_alter_table("emission_factors") as batch:
        batch.add_column(sa.Column("organisation_id", sa.Integer(), nullable=True))
        batch.create_foreign_key("fk_factor_org", "organisations",
                                 ["organisation_id"], ["id"])
    op.create_index("ix_emission_factors_organisation_id", "emission_factors",
                    ["organisation_id"])


def downgrade():
    # Dropping the column discards which tenant contributed a factor; the rows
    # themselves survive and revert to platform-visible.
    op.drop_index("ix_emission_factors_organisation_id",
                  table_name="emission_factors")
    with op.batch_alter_table("emission_factors") as batch:
        batch.drop_constraint("fk_factor_org", type_="foreignkey")
        batch.drop_column("organisation_id")
