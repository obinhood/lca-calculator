"""market instrument idempotency + frozen Scope 2 coverage statement

Two columns, both serving the same defect: a market instrument could be submitted
twice and the second copy silently doubled the declared contractual coverage,
driving published market-based Scope 2 down — to zero where the claim was 100%
renewable — while the readiness gate still passed.

  market_instruments.submission_hash   SHA-256 of the submitted fields, UNIQUE per
                                       organisation, so a replay is refused (409).
  calculation_runs.scope2_coverage_statement
                                       JSON: declared contractual volume against the
                                       electricity load it was offered, frozen at run
                                       time so a filed run's verdict cannot drift when
                                       a certificate is added or withdrawn later.

Both are nullable: existing rows keep NULL, which is what "written before this
check existed" means and is distinguishable from an evaluated verdict.

Revision ID: a5b6c7d8e9f0
Revises: f4a5b6c7d8e9
"""
import sqlalchemy as sa
from alembic import op

revision = "a5b6c7d8e9f0"
down_revision = "f4a5b6c7d8e9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("market_instruments",
                  sa.Column("submission_hash", sa.String(), nullable=True))
    # Named explicitly so the downgrade can drop it on both engines; SQLite reaches a
    # constraint only through batch_alter_table, which rebuilds the table.
    with op.batch_alter_table("market_instruments") as batch:
        batch.create_unique_constraint("uq_instrument_org_submission",
                                       ["organisation_id", "submission_hash"])
    op.add_column("calculation_runs",
                  sa.Column("scope2_coverage_statement", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("calculation_runs", "scope2_coverage_statement")
    with op.batch_alter_table("market_instruments") as batch:
        batch.drop_constraint("uq_instrument_org_submission", type_="unique")
    op.drop_column("market_instruments", "submission_hash")
