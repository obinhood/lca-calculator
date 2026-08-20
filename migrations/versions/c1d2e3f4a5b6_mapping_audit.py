"""append-only mapping audit trail

Closes two of the six gaps the evidence pack declares: the override log with before
and after values, and the decision timestamp. It does NOT close reviewer identity —
authentication is an organisation-scoped API key with no concept of a person, so
there is no actor to record and inventing one would be worse than the gap.

Append-only by construction: no update path, no status column to flip.

Purely additive.

Revision ID: c1d2e3f4a5b6
Revises: b0c1d2e3f4a5
"""
from alembic import op
import sqlalchemy as sa

revision = "c1d2e3f4a5b6"
down_revision = "b0c1d2e3f4a5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mapping_audit_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("organisation_id", sa.Integer(),
                  sa.ForeignKey("organisations.id"), nullable=False),
        sa.Column("activity_id", sa.Integer(),
                  sa.ForeignKey("activities.id"), nullable=False),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("from_factor_id", sa.Integer(), nullable=True),
        sa.Column("to_factor_id", sa.Integer(), nullable=True),
        sa.Column("from_status", sa.String(), nullable=True),
        sa.Column("to_status", sa.String(), nullable=True),
        sa.Column("basis", sa.String(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("at", sa.String(), nullable=False),
        sa.CheckConstraint(
            "action IN ('auto_mapped','suggested','approved','overridden',"
            "'unmapped','pact_bound')",
            name="ck_mapping_audit_action"),
    )
    op.create_index("ix_mapping_audit_events_activity_id", "mapping_audit_events",
                    ["activity_id"])


def downgrade() -> None:
    op.drop_index("ix_mapping_audit_events_activity_id",
                  table_name="mapping_audit_events")
    op.drop_table("mapping_audit_events")
