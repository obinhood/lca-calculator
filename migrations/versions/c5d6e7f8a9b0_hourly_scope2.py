"""hourly Scope 2: granular certificates, hourly load, hourly intensity, deliverability

The proposed GHG Protocol Scope 2 revision would require energy attribute certificates
to be matched to consumption HOURLY and within physically deliverable boundaries. The
annual model has nowhere to put the hour dimension, so four additive tables carry it.

Purely additive: no existing table is touched and no existing figure moves. Hourly
Scope 2 is a PARALLEL method — an organisation with no rows in these tables computes
exactly as it did before.

Revision ID: c5d6e7f8a9b0
Revises: b3c4d5e6f7a8
"""
from alembic import op
import sqlalchemy as sa

revision = "c5d6e7f8a9b0"
down_revision = "b3c4d5e6f7a8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "granular_certificates",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("organisation_id", sa.Integer(),
                  sa.ForeignKey("organisations.id"), nullable=False),
        sa.Column("issuer", sa.String(), nullable=False),
        sa.Column("certificate_ref", sa.String(), nullable=False),
        sa.Column("production_start", sa.String(), nullable=False),
        sa.Column("production_end", sa.String(), nullable=False),
        sa.Column("kwh", sa.Float(), nullable=False),
        sa.Column("technology", sa.String(), nullable=True),
        sa.Column("grid_region", sa.String(), nullable=False),
        sa.Column("production_device_id", sa.String(), nullable=True),
        sa.Column("kg_co2e_per_kwh", sa.Float(), nullable=False, server_default="0"),
        sa.Column("retired_at", sa.String(), nullable=True),
        sa.Column("retired_for_period_id", sa.Integer(),
                  sa.ForeignKey("reporting_periods.id"), nullable=True),
        sa.Column("created_at", sa.String(), nullable=True),
        # Global uniqueness is the anti-double-counting guard: the same certificate
        # loaded twice under two ids would be matched twice.
        sa.UniqueConstraint("issuer", "certificate_ref", name="uq_gc_issuer_ref"),
        sa.CheckConstraint("kwh > 0", name="ck_gc_kwh_pos"),
        sa.CheckConstraint("kg_co2e_per_kwh >= 0", name="ck_gc_intensity_nonneg"),
        sa.CheckConstraint("production_end > production_start", name="ck_gc_window"),
    )

    op.create_table(
        "hourly_loads",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("organisation_id", sa.Integer(),
                  sa.ForeignKey("organisations.id"), nullable=False),
        sa.Column("entity_id", sa.Integer(),
                  sa.ForeignKey("reporting_entities.id"), nullable=True),
        sa.Column("metering_point", sa.String(), nullable=False, server_default="default"),
        sa.Column("hour_start", sa.String(), nullable=False),
        sa.Column("kwh", sa.Float(), nullable=False),
        sa.Column("grid_region", sa.String(), nullable=False),
        sa.Column("source_file", sa.String(), nullable=True),
        sa.Column("created_at", sa.String(), nullable=True),
        sa.UniqueConstraint("organisation_id", "entity_id", "metering_point", "hour_start",
                            name="uq_hourly_load_point_hour"),
        sa.CheckConstraint("kwh >= 0", name="ck_hourly_load_nonneg"),
    )

    op.create_table(
        "hourly_grid_intensities",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("grid_region", sa.String(), nullable=False),
        sa.Column("hour_start", sa.String(), nullable=False),
        sa.Column("kg_co2e_per_kwh_average", sa.Float(), nullable=False),
        sa.Column("kg_co2e_per_kwh_residual", sa.Float(), nullable=True),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("version", sa.String(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.String(), nullable=True),
        sa.UniqueConstraint("grid_region", "hour_start", "source", "version",
                            name="uq_hourly_intensity_region_hour_source"),
        sa.CheckConstraint("kg_co2e_per_kwh_average >= 0", name="ck_hgi_avg_nonneg"),
    )

    op.create_table(
        "deliverability_links",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("organisation_id", sa.Integer(),
                  sa.ForeignKey("organisations.id"), nullable=False),
        sa.Column("from_region", sa.String(), nullable=False),
        sa.Column("to_region", sa.String(), nullable=False),
        sa.Column("basis", sa.String(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("created_at", sa.String(), nullable=True),
        sa.UniqueConstraint("organisation_id", "from_region", "to_region",
                            name="uq_deliverability_pair"),
    )


def downgrade() -> None:
    op.drop_table("deliverability_links")
    op.drop_table("hourly_grid_intensities")
    op.drop_table("hourly_loads")
    op.drop_table("granular_certificates")
