"""users and api tokens: a principal behind every request

Revision ID: c2d3e4f5a6b7
Revises: b1c2d3e4f5a6
Create Date: 2026-11-11 17:05:00.000000

Until now the only principal in the system was `Organisation.api_key_hash` — ONE
shared secret per tenant, with no owner, no role and no way to offboard a leaver
without rotating the key for everyone. Three consequences, all of them commercial:

  * The audit trail could record that a mapping decision was made and not WHO made
    it, which is the one question an assuror asks of a judgement.
  * There was no joiner-mover-leaver path, so the platform could not answer a
    CAIQ tenant-segregation section or an enterprise security review.
  * Letting an auditor read a filed run meant handing over the tenant's WRITE key.
    `assurance_engagements.access_token_hash` exists for that purpose and is a bare
    hash: no expiry, no revocation flag, no scope.

WHY USERS ARE SCOPED TO ONE ORGANISATION
----------------------------------------
A global `users` table joined by a membership is the better entity model in the
abstract, and the wrong one here. `services/lifecycle.py` classifies EVERY table as
tenant-scoped or global reference, and `assert_tables_classified` fails the suite for
a table in neither. A cross-tenant `users` table would be a second unscoped tenant
surface in a product whose last security finding was precisely that — `emission_factors`
served without a credential — and it would make tenant deletion conditional on a user's
other memberships, so "delete everything belonging to this tenant" would stop being a
statement about one organisation.

So `email` is unique per (organisation_id, email), not globally: one person serving
three tenants holds three accounts, which is also the segregation posture an enterprise
buyer is asking about. Parties who genuinely work ACROSS tenants — assurors,
consultancies, CI jobs — get an `api_tokens` row with `user_id` NULL instead, issued by
the tenant and revocable by it.

WHY EXPIRY IS NOT NULL
----------------------
`pact_tokens` already established the pattern: stored by hash, with an explicit
expiry, so "a leaked token has a bounded life and revocation is a delete rather than a
hope". This table keeps that and adds the scope `pact_tokens` lacks. There is
deliberately no sentinel value for "never expires": an unexpiring credential is the
defect being fixed, so the schema cannot express one.

Revocation is `revoked_at` rather than a row delete, because a filed audit trail must
still be able to say which credential acted after that credential stops working.

LIFECYCLE REGISTRATION
----------------------
Both tables are TENANT_DIRECT in `services/lifecycle.py`. `users` is in EXPORT_TABLES —
a churning customer must get back who had access, and `users.email` / `users.name` are
the only columns in the schema that identify a person by design rather than incidentally
through free text, so they are also the GDPR Art. 15 substrate alongside `activities`.
`api_tokens` is in EXPORT_EXCLUDED for the same reason `pact_tokens` is: `NEVER_SERIALISE`
would strip the hash, but an export naming every live credential with its scope and
expiry is a map of the tenant's attack surface, so the row is excluded whole.

`token_hash` reuses the column name already in `NEVER_SERIALISE`, so no read path can
serialise it by forgetting — the same doctrine as `models.factor_visible_to()`.
"""
from alembic import op
import sqlalchemy as sa


revision: str = 'c2d3e4f5a6b7'
down_revision: str = 'b1c2d3e4f5a6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'users',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('organisation_id', sa.Integer(), nullable=False),
        # Identifies a person WITHIN a tenant. See the module docstring for why this is
        # not globally unique.
        sa.Column('email', sa.String(), nullable=False),
        sa.Column('name', sa.String(), nullable=True),
        # owner  — may manage users and tokens, and delete the tenant
        # admin  — may manage users and tokens
        # member — may read and write inventory data
        # auditor— may READ only; never writes, whatever scope a token claims
        sa.Column('role', sa.String(), nullable=False, server_default='member'),
        # Suspension is the LEAVER path, and deliberately not a delete: a departed
        # preparer's name must stay attached to the decisions they made, or the audit
        # trail this platform exists to produce loses its actor.
        sa.Column('status', sa.String(), nullable=False, server_default='active'),
        sa.Column('created_at', sa.String(), nullable=True),
        sa.Column('suspended_at', sa.String(), nullable=True),
        sa.ForeignKeyConstraint(['organisation_id'], ['organisations.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('organisation_id', 'email', name='uq_user_org_email'),
        sa.CheckConstraint("role IN ('owner','admin','member','auditor')",
                           name='ck_user_role'),
        sa.CheckConstraint("status IN ('active','suspended')", name='ck_user_status'),
    )
    op.create_index(op.f('ix_users_organisation_id'), 'users',
                    ['organisation_id'], unique=False)

    op.create_table(
        'api_tokens',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('organisation_id', sa.Integer(), nullable=False),
        # NULL for a credential held by a party that is not a seat inside the tenant —
        # an assuror, a consultancy, a CI job. Exactly the cases that previously had to
        # borrow the tenant's own key.
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.Column('label', sa.String(), nullable=False),
        sa.Column('token_hash', sa.String(), nullable=False),
        sa.Column('scopes', sa.String(), nullable=False, server_default='read'),
        # NOT NULL, with no sentinel for "never" — see the module docstring.
        sa.Column('expires_at', sa.String(), nullable=False),
        sa.Column('created_at', sa.String(), nullable=True),
        sa.Column('last_used_at', sa.String(), nullable=True),
        sa.Column('revoked_at', sa.String(), nullable=True),
        sa.ForeignKeyConstraint(['organisation_id'], ['organisations.id'], ),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('token_hash', name='uq_api_token_hash'),
        sa.CheckConstraint("scopes IN ('read','write','admin')",
                           name='ck_api_token_scopes'),
    )
    op.create_index(op.f('ix_api_tokens_organisation_id'), 'api_tokens',
                    ['organisation_id'], unique=False)
    op.create_index(op.f('ix_api_tokens_user_id'), 'api_tokens',
                    ['user_id'], unique=False)
    op.create_index(op.f('ix_api_tokens_token_hash'), 'api_tokens',
                    ['token_hash'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_api_tokens_token_hash'), table_name='api_tokens')
    op.drop_index(op.f('ix_api_tokens_user_id'), table_name='api_tokens')
    op.drop_index(op.f('ix_api_tokens_organisation_id'), table_name='api_tokens')
    op.drop_table('api_tokens')
    op.drop_index(op.f('ix_users_organisation_id'), table_name='users')
    op.drop_table('users')
