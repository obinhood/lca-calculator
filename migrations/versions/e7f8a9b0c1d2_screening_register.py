"""pre-calculation screening: the assurance exception register

An assurance-grade misstatement ledger rather than an anomaly detector. Each finding
carries a stated expectation, the threshold in force, the observation, a quantified
effect and an auditable disposition — the shape ISAE 3410 (50-56) and ISSA 5000
(153-161) require a practitioner to assemble by hand.

Purely additive: two new tables plus one nullable column on calculation_runs. That
column is an anti-cliff sentinel — NULL means the run predates screening and is never
retroactively blocked by it — so every existing run and every filed figure is unchanged.

Revision ID: e7f8a9b0c1d2
Revises: d6e7f8a9b0c1
"""
from alembic import op
import sqlalchemy as sa

revision = "e7f8a9b0c1d2"
down_revision = "d6e7f8a9b0c1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "activity_findings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("organisation_id", sa.Integer(),
                  sa.ForeignKey("organisations.id"), nullable=False),
        sa.Column("activity_id", sa.Integer(),
                  sa.ForeignKey("activities.id"), nullable=True),
        sa.Column("related_activity_ids", sa.Text(), nullable=True),
        sa.Column("finding_key", sa.String(), nullable=False),
        sa.Column("check_code", sa.String(), nullable=False),
        sa.Column("severity", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="open"),
        sa.Column("expectation", sa.Text(), nullable=False),
        sa.Column("threshold", sa.Text(), nullable=False),
        sa.Column("observed", sa.Text(), nullable=False),
        sa.Column("estimated_effect_kg", sa.Float(), nullable=True),
        sa.Column("effect_quantifiable", sa.Boolean(), nullable=False,
                  server_default=sa.text("1")),
        sa.Column("disposition_reason_code", sa.String(), nullable=True),
        sa.Column("disposition_note", sa.Text(), nullable=True),
        sa.Column("dispositioned_at", sa.String(), nullable=True),
        sa.Column("screening_version", sa.String(), nullable=False),
        sa.Column("detected_at", sa.String(), nullable=True),
        sa.Column("created_at", sa.String(), nullable=True),
        sa.UniqueConstraint("organisation_id", "finding_key", name="uq_finding_org_key"),
        sa.CheckConstraint("severity IN ('blocking','high','medium','informational')",
                           name="ck_finding_severity"),
        sa.CheckConstraint("status IN ('open','corrected','accepted','superseded')",
                           name="ck_finding_status"),
        sa.CheckConstraint(
            "disposition_reason_code IS NULL OR disposition_reason_code IN ("
            "'genuine_operational_change','corrected_at_source','restated_prior_period',"
            "'unit_error_fixed','boundary_change','benchmark_not_applicable',"
            "'accepted_immaterial')",
            name="ck_finding_reason_code"),
        sa.CheckConstraint(
            "status IN ('open','superseded') OR "
            "(disposition_reason_code IS NOT NULL AND disposition_note IS NOT NULL)",
            name="ck_finding_disposition_complete"),
    )

    op.create_table(
        "run_screening_statements",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("run_id", sa.Integer(),
                  sa.ForeignKey("calculation_runs.id"), nullable=False),
        sa.Column("screening_version", sa.String(), nullable=False),
        sa.Column("screened_at", sa.String(), nullable=True),
        sa.Column("findings_total", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("findings_open", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("findings_corrected", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("findings_accepted", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("open_blocking", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("accumulated_uncorrected_effect_kg", sa.Float(), nullable=False,
                  server_default="0"),
        sa.Column("uncorrected_unquantifiable", sa.Integer(), nullable=False,
                  server_default="0"),
        sa.Column("materiality_kg", sa.Float(), nullable=False, server_default="0"),
        sa.Column("materiality_pct", sa.Float(), nullable=False, server_default="5"),
        sa.Column("exceeds_materiality", sa.Boolean(), nullable=False,
                  server_default=sa.text("0")),
        sa.Column("frozen_at", sa.String(), nullable=False),
        sa.UniqueConstraint("run_id", name="uq_run_screening_statement"),
        sa.CheckConstraint("findings_total >= 0", name="ck_rss_total_nonneg"),
        sa.CheckConstraint("materiality_pct > 0 AND materiality_pct <= 100",
                           name="ck_rss_materiality_pct"),
    )

    # NULL = the run predates screening. Never back-filled.
    op.add_column("calculation_runs",
                  sa.Column("screening_version", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("calculation_runs", "screening_version")
    op.drop_table("run_screening_statements")
    op.drop_table("activity_findings")
