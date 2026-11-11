"""factor gas masses: make all seven Kyoto gas families representable

Revision ID: b1c2d3e4f5a6
Revises: a7b8c9d0e1f2
Create Date: 2026-11-11 11:30:00.000000

Before this table the schema could express three of the seven Kyoto-basket gas
families. `emission_factors` had mass columns for CO2, CH4 and N2O only, so:

  * SF6 and NF3 had GWP values in services/gwp.py that NO code path could reach —
    there was nowhere to put the mass they would have been multiplied by.
  * HFCs and PFCs had neither a GWP value nor a column, so a fugitive refrigerant
    emission could not be expressed at all. That removes a material Scope 1 source
    for any preparer with cooling or refrigeration.
  * The EU/UK ETS renderer's "annual emissions of the installation" figure
    (MRR Art. 24) was therefore wrong BY CONSTRUCTION for an installation with any
    F-gas, and no gate could detect it: the gas simply had no representation.

WHY A TABLE AND NOT COLUMNS
---------------------------
A column per species was rejected. The AR6 halocarbon list runs to ~90 species and
grows with every assessment cycle, so a column per gas means a migration per cycle
and a schema that is wrong again the next time the GWP set expands. A nullable
column also cannot distinguish "this gas was never measured" from "this gas was
measured and is zero"; here NO ROW means not measured and a row of 0.0 means
measured-and-zero, which is the platform's NULL-vs-empty invariant.

A JSON column was rejected too: it cannot carry the non-negativity CHECK below, and
"does this factor carry an F-gas?" stops being a SQL predicate an ETS or F-gas
completeness check can run.

CO2, CH4 and N2O KEEP THEIR COLUMNS. They appear in essentially every combustion
factor, so they stay on the hot path with no join, and every pre-existing row, every
filed run and every test keeps working untouched. The two stores are DISJOINT: a row
here naming a column-backed gas is a data error, and gwp.gas_masses_for_factor()
raises on it rather than double counting or dropping that gas.
`gwp.gas_masses_for_factor()` is the ONE reader that unions them.

ADDITIVE AND REVERSIBLE
-----------------------
A plain CREATE TABLE. Nothing is written to it by this migration and nothing reads
it unless rows exist, so an existing deployment's numbers are byte-identical before
and after. No batch_alter_table and no change to `emission_factors`, which is an FK
target — the same reasoning recorded in 00871ee113e9 and a7b8c9d0e1f2.

`kg_per_unit` is NOT NULL because a gas row with no mass carries no information and
would read as zero; a gas that was not measured is represented by the ABSENCE of a
row, which is the distinction this table exists to preserve. The CHECK mirrors
`ck_factor_value_nonneg` on the aggregate `value`: the per-gas path bypassed that
constraint, and a negative mass would net a removal into the gross emissions total.

The downgrade drops the table, which discards any per-gas F-gas masses loaded while
it existed. That is unavoidable — there is no column to fold them into, which is the
whole defect — so a deployment that has ingested F-gas factors should export them
before downgrading.
"""
from alembic import op
import sqlalchemy as sa


revision: str = 'b1c2d3e4f5a6'
down_revision: str = 'a7b8c9d0e1f2'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'factor_gas_masses',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('factor_id', sa.Integer(), nullable=False),
        # The gas identity used by services/gwp.py: a pure species ("HFC-134a",
        # "SF6", "PFC-14") or a shipped refrigerant blend ("R-410A"), whose GWP is
        # composed from its components at calculation time.
        sa.Column('gas', sa.String(), nullable=False),
        # kg of ACTUAL GAS per activity unit — never a CO2e figure. Storing CO2e
        # would re-freeze a GWP vintage into the factor and make the AR5/AR6 switch
        # cosmetic again for exactly the gases where the two sets diverge most.
        sa.Column('kg_per_unit', sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(['factor_id'], ['emission_factors.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('factor_id', 'gas', name='uq_factor_gas'),
        sa.CheckConstraint('kg_per_unit >= 0', name='ck_factor_gas_mass_nonneg'),
    )
    op.create_index(op.f('ix_factor_gas_masses_factor_id'), 'factor_gas_masses',
                    ['factor_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_factor_gas_masses_factor_id'),
                  table_name='factor_gas_masses')
    op.drop_table('factor_gas_masses')
