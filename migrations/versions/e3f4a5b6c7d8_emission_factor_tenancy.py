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
    # Postgres takes the plain ALTER.
    with op.batch_alter_table("emission_factors") as batch:
        batch.add_column(sa.Column("organisation_id", sa.Integer(), nullable=True))
        batch.create_foreign_key("fk_factor_org", "organisations",
                                 ["organisation_id"], ["id"])
    op.create_index("ix_emission_factors_organisation_id", "emission_factors",
                    ["organisation_id"])

    # BACKFILL — without it this migration closes the leak only for footprints
    # materialised AFTER it runs. Leaving NULL means "platform reference data,
    # readable by every tenant" (models.factor_visible_to), which for a supplier's
    # PCF is exactly the disclosure this column exists to stop. Any customer who had
    # already imported one would keep it public across the upgrade, silently.
    #
    # services/pact_factor.materialise() stamps `source = 'PACT'` and
    # `version = <the footprint's pf_id>`, so each row's importer is recoverable
    # from product_footprints. Correlated UPDATE rather than UPDATE..FROM / JOIN,
    # because that syntax differs between SQLite and PostgreSQL and this chain has
    # to build on both.
    op.execute("""
        UPDATE emission_factors
           SET organisation_id = (
                 SELECT pf.organisation_id
                   FROM product_footprints pf
                  WHERE pf.pf_id = emission_factors.version
               )
         WHERE source = 'PACT'
           AND organisation_id IS NULL
           AND EXISTS (
                 SELECT 1
                   FROM product_footprints pf
                  WHERE pf.pf_id = emission_factors.version
               )
    """)
    # A 'PACT'-sourced row whose footprint is gone keeps organisation_id NULL and so
    # stays globally visible. That is deliberate and is NOT a silent default: it is
    # reported, because guessing an owner for a supplier's confidential figure would
    # be worse than naming the rows a human has to decide about.
    bind = op.get_bind()
    orphans = bind.execute(sa.text(
        "SELECT COUNT(*) FROM emission_factors "
        " WHERE source = 'PACT' AND organisation_id IS NULL")).scalar() or 0
    if orphans:
        print(f"[e3f4a5b6c7d8] WARNING: {orphans} PACT factor row(s) have no "
              f"matching product_footprint and remain tenant-unassigned, so they "
              f"stay readable by every tenant. Assign or delete them before "
              f"relying on factor tenancy.")


def downgrade():
    # Dropping the column discards which tenant contributed a factor; the rows
    # themselves survive and revert to platform-visible.
    op.drop_index("ix_emission_factors_organisation_id",
                  table_name="emission_factors")
    with op.batch_alter_table("emission_factors") as batch:
        batch.drop_constraint("fk_factor_org", type_="foreignkey")
        batch.drop_column("organisation_id")
