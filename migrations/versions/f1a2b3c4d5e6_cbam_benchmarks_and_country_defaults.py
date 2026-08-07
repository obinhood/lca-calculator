"""cbam benchmarks + country-specific default values

The certificate obligation must be embedded emissions MINUS a benchmark-based free-allocation
adjustment, so EU production benchmarks need a home. And the Commission's default tables are
published per CN code AND country of origin, so the defaults table needs the country dimension
it was missing (a Norwegian and a Chinese line with the same CN code are not the same default).

Both are additive: a new table plus a nullable column. NULL origin_country keeps every existing
row valid as a country-agnostic fallback.

Revision ID: f1a2b3c4d5e6
Revises: e5b3c8d1f2a7
"""
from alembic import op
import sqlalchemy as sa

revision = "f1a2b3c4d5e6"
down_revision = "e5b3c8d1f2a7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "cbam_benchmarks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("cn_code_prefix", sa.String(), nullable=False),
        sa.Column("good_category", sa.String(), nullable=False),
        sa.Column("benchmark_t_co2e_per_t", sa.Float(), nullable=False),
        sa.Column("basis", sa.String(), nullable=True),
        sa.Column("valid_year", sa.Integer(), nullable=False),
        sa.Column("recorded_at", sa.String(), nullable=True),
        sa.CheckConstraint("benchmark_t_co2e_per_t >= 0", name="ck_cbam_benchmark_nonneg"),
    )
    # cbam_default_values is an FK target for nothing, but a plain nullable add_column is the
    # safe operation under PRAGMA foreign_keys=ON regardless (no table recreate).
    op.add_column("cbam_default_values", sa.Column("origin_country", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("cbam_default_values", "origin_country")
    op.drop_table("cbam_benchmarks")
