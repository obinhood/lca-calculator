"""freeze the pedigree compilation onto each run

The engine summed ecoinvent's pedigree variance contributions without the factor
of a half that SDg95 = sigma_g^2 implies, so sigma was twice the true lognormal
shape parameter and every published 95% interval was twice as wide in log space.
The repository already stated the correct convention in the other module using
the same table — services/crosswalk.py pins the worst technological cell at
variance 0.12, reachable only as (ln 2.00 / 2)^2 = 0.120113 — so the two modules
disagreed and crosswalk.py was right.

Correcting dq.py moves every new interval by a factor of two. Runs already filed
keep their own wider band, which is the reproduction contract working as
intended; the hazard is COMPARING one with the other, where a pure methodology
change would read as a halving of uncertainty.

So this column records which compilation produced a run, and
comparability.pedigree_comparable() blocks subtracting across the change. NULL is
not "unknown": nothing wrote this field before the correction, so an absent value
IS the superseded compilation, and the gate says so in those words.

Purely additive and NULL-safe: no existing run changes, and no filed figure moves.

Revision ID: f4a5b6c7d8e9
Revises: e3f4a5b6c7d8
"""
from alembic import op
import sqlalchemy as sa

revision = "f4a5b6c7d8e9"
down_revision = "e3f4a5b6c7d8"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("calculation_runs") as batch:
        batch.add_column(sa.Column("pedigree_method", sa.String(), nullable=True))


def downgrade():
    # Dropping the column loses the ability to tell the two compilations apart;
    # the runs and their frozen intervals are untouched either way.
    with op.batch_alter_table("calculation_runs") as batch:
        batch.drop_column("pedigree_method")
