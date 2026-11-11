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


def _backfill_hashes(conn) -> None:
    """Give every EXISTING instrument its identity hash.

    Without this the column is NULL for everything already in the database, the unique
    index has nothing to compare, and the fix protects only rows created from now on —
    every instrument a customer already holds stays freely duplicable. That is the same
    shape as the factor-tenancy migration that shipped without a backfill.

    The blob is replicated here rather than imported, and is PINNED TO mi-v1: a
    migration must keep doing what it did when it ran, so if
    services.instruments.IDENTITY_VERSION is ever bumped this code must NOT follow it —
    a new migration backfills the new version.
    """
    import hashlib
    import json

    rows = conn.execute(sa.text(
        "SELECT id, organisation_id, instrument_type, kg_co2e_per_kwh, coverage_kwh, "
        "gwp_set, market, start_date, end_date FROM market_instruments "
        "WHERE submission_hash IS NULL")).fetchall()
    seen = set()
    for r in rows:
        market = r[6]
        key = (market.strip().upper() or None) if market is not None else None
        blob = json.dumps(
            ["mi-v1", r[1], (r[2] or "").strip().lower(), repr(float(r[3])),
             None if r[4] is None else repr(float(r[4])),
             (r[5] or "").strip().upper() or None, key, r[7] or None, r[8] or None],
            separators=(",", ":"), default=str)
        h = hashlib.sha256(blob.encode("utf-8")).hexdigest()
        # Duplicates may ALREADY exist, written before anything refused them. They
        # cannot all take the same hash under a unique index, so the first keeps the
        # hash and the rest are left NULL — visible as duplicates rather than destroyed.
        # Deleting a customer's instrument rows in a migration is not this migration's
        # decision to make.
        if (r[1], h) in seen:
            continue
        seen.add((r[1], h))
        conn.execute(sa.text("UPDATE market_instruments SET submission_hash = :h "
                             "WHERE id = :i"), {"h": h, "i": r[0]})


def upgrade() -> None:
    op.add_column("market_instruments",
                  sa.Column("submission_hash", sa.String(), nullable=True))
    _backfill_hashes(op.get_bind())
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
