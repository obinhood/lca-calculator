"""scope2 residual mix

GHG Protocol Scope 2 Guidance: market-based consumption NOT covered by a contractual
instrument must be priced at the RESIDUAL MIX (the grid average with attributes other
purchasers already claimed removed), not the plain grid average. The old behaviour
double counted those attributes and UNDERSTATED the market-based figure.

Adds the published-rate reference table, the per-run frozen statement artifact, an
org-side rate provenance column, and the run policy stamp.

NO BACK-FILL, in either direction. `calculation_runs.scope2_residual_mix_version` stays
NULL on every existing run, and the gate treats NULL as "predates the requirement —
warn, never block". Back-filling it would instantly re-create the cliff this design
exists to avoid: every already-filed run would begin blocking on reference data nobody
was ever asked for. That NULL is the compatibility mechanism, not an omission.

Revision ID: d4a7c1e9b2f5
Revises: c8d2e4f6a1b3
Create Date: 2026-07-21 09:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd4a7c1e9b2f5'
down_revision: Union[str, None] = 'c8d2e4f6a1b3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Fresh tables -> CHECKs inline, no batch mode needed.
    op.create_table(
        "residual_mix_rates",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("market", sa.String(), nullable=False),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("gwp_set", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("kg_co2e_per_kwh", sa.Float(), nullable=True),
        sa.Column("gas_basis", sa.String(), nullable=False),
        sa.Column("publisher", sa.String(), nullable=False),
        sa.Column("publication", sa.Text(), nullable=True),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("published_at", sa.String(), nullable=True),
        sa.Column("recorded_at", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("status IN ('published','not_published')", name="ck_rmr_status"),
        sa.CheckConstraint(
            "(status = 'published' AND kg_co2e_per_kwh IS NOT NULL AND kg_co2e_per_kwh > 0) "
            "OR (status = 'not_published' AND kg_co2e_per_kwh IS NULL)",
            name="ck_rmr_rate_entailment"),
        sa.CheckConstraint("gas_basis IN ('co2','co2e')", name="ck_rmr_gas_basis"),
        sa.CheckConstraint("year >= 1990 AND year <= 2100", name="ck_rmr_year"),
        sa.CheckConstraint(
            "status = 'published' OR (publication IS NOT NULL "
            "AND length(trim(publication)) >= 20)",
            name="ck_rmr_absence_attested"),
    )
    # Deliberately NO unique constraint on (market, year, ...): that absence IS the
    # append-only mechanism. Migration 083915258aeb dropped uq_fx / uq_price_index for
    # exactly this reason — a correction INSERTs a new row and lookups take the newest.
    op.create_index("ix_residual_mix_market_year", "residual_mix_rates", ["market", "year"])

    op.create_table(
        "run_residual_mix_statements",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("market_key", sa.String(), nullable=False),
        sa.Column("year_key", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("rate_kg_co2e_per_kwh", sa.Float(), nullable=True),
        sa.Column("reference_rate_id", sa.Integer(), nullable=True),
        sa.Column("reference_rate_kg_co2e_per_kwh", sa.Float(), nullable=True),
        sa.Column("instrument_id", sa.Integer(), nullable=True),
        sa.Column("gwp_match", sa.String(), nullable=True),
        sa.Column("gas_basis", sa.String(), nullable=True),
        sa.Column("publisher", sa.String(), nullable=True),
        sa.Column("publication", sa.Text(), nullable=True),
        sa.Column("kwh_contractual", sa.Float(), nullable=False),
        sa.Column("kwh_priced_at_residual", sa.Float(), nullable=False),
        sa.Column("kwh_priced_at_grid", sa.Float(), nullable=False),
        sa.Column("grid_rate_avg_kg_per_kwh", sa.Float(), nullable=True),
        sa.Column("co2e_at_residual_kg", sa.Float(), nullable=False),
        sa.Column("co2e_at_grid_kg", sa.Float(), nullable=False),
        sa.Column("gap_consolidated_co2e_kg", sa.Float(), nullable=False),
        sa.Column("org_rate_kg_co2e_per_kwh", sa.Float(), nullable=True),
        sa.Column("gwp_vintage_mismatch", sa.Boolean(), nullable=False),
        sa.Column("unpriceable_lines", sa.Integer(), nullable=False),
        sa.Column("residual_mix_version", sa.String(), nullable=False),
        sa.Column("frozen_at", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["run_id"], ["calculation_runs.id"]),
        # market_key/year_key use sentinels rather than NULL: SQLite treats NULLs as
        # DISTINCT in a unique index, so a nullable pair would silently admit duplicates.
        sa.UniqueConstraint("run_id", "market_key", "year_key", name="uq_run_rm_statement"),
        sa.CheckConstraint(
            "status IN ('fully_contractual','org_instrument','reference_rate',"
            "'not_published','unresolved_no_reference_data','market_unknown',"
            "'year_unknown','unpriceable')",
            name="ck_rms_status"),
        sa.CheckConstraint(
            "(status IN ('org_instrument','reference_rate') "
            "AND rate_kg_co2e_per_kwh IS NOT NULL) "
            "OR (status NOT IN ('org_instrument','reference_rate') "
            "AND rate_kg_co2e_per_kwh IS NULL)",
            name="ck_rms_rate_entailment"),
    )

    # market_instruments is NOT an FK target -> batch mode is safe (and needed on SQLite).
    with op.batch_alter_table("market_instruments") as b:
        b.add_column(sa.Column("rate_source", sa.String(), nullable=True))

    # calculation_runs IS an FK target — plain add_column only, never batch_alter_table
    # under PRAGMA foreign_keys=ON (the note carried by b7c1d9e2f3a4 and c8d2e4f6a1b3).
    op.add_column("calculation_runs",
                  sa.Column("scope2_residual_mix_version", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("calculation_runs", "scope2_residual_mix_version")
    with op.batch_alter_table("market_instruments") as b:
        b.drop_column("rate_source")
    op.drop_table("run_residual_mix_statements")
    op.drop_index("ix_residual_mix_market_year", table_name="residual_mix_rates")
    op.drop_table("residual_mix_rates")
