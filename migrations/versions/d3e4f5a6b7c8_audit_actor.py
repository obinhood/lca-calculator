"""mapping audit events: record WHO decided

Revision ID: d3e4f5a6b7c8
Revises: c2d3e4f5a6b7
Create Date: 2026-11-11 18:10:00.000000

`mapping_audit_events` closed two of the six gaps the evidence pack declares about
itself — the override log with before/after values, and the decision timestamp. It
could not close the third, reviewer identity, and said so in its own docstring:
"authentication is an organisation-scoped API key with no concept of a person, so
there is no actor to record. Recording the organisation as though it were a reviewer
would turn an honest gap into a misleading answer."

Revision c2d3e4f5a6b7 created the actor: `users` and `api_tokens`, resolved into a
`Principal` on every request. This records it.

NULL IS NOT A MISSING VALUE HERE, IT IS AN ANSWER
-------------------------------------------------
Every row already in this table was written while there was provably no actor to
record. Those rows keep `actor IS NULL`, and NULL means RECORDED BEFORE IDENTITY
EXISTED. That is a different fact from each of:

  * 'org_key' — the tenant's own shared credential acted, so the organisation is
    accountable but no person is named;
  * 'token:<id>' — a credential with no user behind it (an assuror, a consultancy,
    a CI job);
  * 'user:<email>' — a named person;
  * 'engine' — a machine binding, which the `auto_mapped` action already implies.

So this migration back-fills NOTHING. Choosing any default would assert of a filed
run that somebody reviewed a binding when nobody can know who, which is exactly the
misleading answer the gap was left open to avoid. `summary()` consequently reports
attribution as a count of attributed against unattributed events rather than
flipping one claim for the whole journal, and the evidence pack reports the gap as
closed FROM a date rather than closed outright.

WHY A STRING AND AN ID, NOT AN ID ALONE
---------------------------------------
`actor` is the evidence and must stay readable after the `users` row is gone — a
journal that becomes unreadable when a leaver is removed is not an audit trail.
`actor_user_id` is the joinable convenience for reporting, nullable for every actor
kind that is not a named user of this tenant, and deliberately not constrained to
agree with the string.

The table stays APPEND-ONLY: these are two new columns on insert, and no update path
is added.
"""
from alembic import op
import sqlalchemy as sa


revision: str = 'd3e4f5a6b7c8'
down_revision: str = 'c2d3e4f5a6b7'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Nullable with NO server_default, on purpose. A default would back-fill every
    # historical row with a claim about who reviewed it.
    op.add_column('mapping_audit_events',
                  sa.Column('actor', sa.String(), nullable=True))
    op.add_column('mapping_audit_events',
                  sa.Column('actor_user_id', sa.Integer(), nullable=True))
    # Named explicitly: an unnamed FK cannot be dropped by name on PostgreSQL, and
    # SQLite needs batch mode to alter a table at all — which is why the downgrade
    # below uses it.
    with op.batch_alter_table('mapping_audit_events') as batch:
        batch.create_foreign_key('fk_mapping_audit_actor_user', 'users',
                                 ['actor_user_id'], ['id'])


def downgrade() -> None:
    with op.batch_alter_table('mapping_audit_events') as batch:
        batch.drop_constraint('fk_mapping_audit_actor_user', type_='foreignkey')
    op.drop_column('mapping_audit_events', 'actor_user_id')
    op.drop_column('mapping_audit_events', 'actor')
