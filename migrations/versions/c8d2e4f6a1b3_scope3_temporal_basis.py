"""scope3 temporal basis (Cats 2/11/12)

The engine computes activity x factor FOR THE PERIOD, but Cat 2 (capital goods acquired),
Cat 11 (use of sold products) and Cat 12 (end-of-life of sold products) require an
acquisition-year / sale-year-lifetime basis. The platform cannot compute that, so it
demands a structured, frozen ASSERTION of what the figure denominates.

NO BACK-FILL. `calculation_runs.scope3_temporal_basis_version` stays NULL on every
existing run, and the gate treats NULL as "this run predates the requirement" — warn,
never block. Back-filling that column would instantly re-create the cliff this design
exists to avoid: every already-filed Cat 2/11/12 declaration would begin blocking on a
basis nobody was ever asked for. The NULL is the compatibility mechanism, not an omission.

Revision ID: c8d2e4f6a1b3
Revises: b7c1d9e2f3a4
Create Date: 2026-07-21 02:40:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c8d2e4f6a1b3'
down_revision: Union[str, None] = 'b7c1d9e2f3a4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# NULL-SAFE: `temporal_basis = 'x'` evaluates to NULL when the column IS NULL, and SQLite
# treats a NULL CHECK result as PASS — so the naive form does not enforce what it documents.
_ENTAILMENT = ("(temporal_basis IS NOT NULL AND temporal_basis = 'sold_units_full_lifetime') "
               "OR (basis_units_sold IS NULL "
               "AND basis_lifetime_years IS NULL AND basis_per_unit_annual_co2e_kg IS NULL)")
_POSITIVE = ("(basis_units_sold IS NULL OR basis_units_sold > 0) AND "
             "(basis_lifetime_years IS NULL OR basis_lifetime_years > 0) AND "
             "(basis_per_unit_annual_co2e_kg IS NULL OR basis_per_unit_annual_co2e_kg > 0)")

_TABLES = (
    ("scope3_category_declarations", "s3decl"),
    ("run_scope3_declarations", "runs3decl"),
)


def upgrade() -> None:
    # Both declaration tables are safe to batch-recreate: neither is an FK TARGET
    # (RunScope3Declaration.declaration_id is a plain Integer with no ForeignKey, by the
    # same doctrine as activities.ghgp_category). SQLite needs the recreate to attach a
    # CHECK. Existing rows are all-NULL in the new columns and satisfy both constraints.
    for table, prefix in _TABLES:
        with op.batch_alter_table(table) as b:
            b.add_column(sa.Column("temporal_basis", sa.String(), nullable=True))
            b.add_column(sa.Column("basis_units_sold", sa.Float(), nullable=True))
            b.add_column(sa.Column("basis_lifetime_years", sa.Float(), nullable=True))
            b.add_column(sa.Column("basis_per_unit_annual_co2e_kg", sa.Float(), nullable=True))
            b.create_check_constraint(f"ck_{prefix}_basis_entailment", _ENTAILMENT)
            b.create_check_constraint(f"ck_{prefix}_basis_positive", _POSITIVE)

    # calculation_runs IS an FK target — plain add_column only, never batch_alter_table
    # under PRAGMA foreign_keys=ON (same note as b7c1d9e2f3a4).
    op.add_column("calculation_runs",
                  sa.Column("scope3_temporal_basis_version", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("calculation_runs", "scope3_temporal_basis_version")
    for table, prefix in _TABLES:
        with op.batch_alter_table(table) as b:
            b.drop_constraint(f"ck_{prefix}_basis_positive", type_="check")
            b.drop_constraint(f"ck_{prefix}_basis_entailment", type_="check")
            b.drop_column("basis_per_unit_annual_co2e_kg")
            b.drop_column("basis_lifetime_years")
            b.drop_column("basis_units_sold")
            b.drop_column("temporal_basis")
