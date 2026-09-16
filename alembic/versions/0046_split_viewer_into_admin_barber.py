"""rename the viewer role into the pair admin / barber

One level of access under two names, picked when the account is created so a front-desk
login is not mistaken for a chair. `barber` is the landing value for anything that was a
viewer: it is the half that does not open the manual-fact editors. Both directions rewrite
`staff.position` on the mirror rows the portal creates, because that column is where the role
label is read back as a staff category.

The downgrade is lossy on purpose. Both names collapse back into `viewer`, and re-applying
this migration maps every one of them to `barber` — which of the two an account carried is not
recorded anywhere else. Per-role money overrides for the pair are dropped rather than merged,
because any merge hands one of the two labels a metric the other was refused. A rollback
therefore costs the front-desk half its label and the tenant its money configuration for the
pair; both are re-picked by hand, and neither can widen anyone's access on the way.

Revision ID: 0046_admin_barber_roles
Revises: 0045_manual_fact_author
"""
from alembic import op
import sqlalchemy as sa

revision = '0046_admin_barber_roles'
down_revision = '0045_manual_fact_author'
branch_labels = None
depends_on = None

SYSTEM_SCHEMA = 'system'


def _has_table(inspector, name: str) -> bool:
    return name in set(inspector.get_table_names(schema=SYSTEM_SCHEMA))


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    op.execute(f"UPDATE {SYSTEM_SCHEMA}.portal_users SET role = 'barber' WHERE role = 'viewer'")
    if _has_table(inspector, 'portal_metric_visibility'):
        # Unique on (portal_account_id, role), and 'barber' cannot exist yet, so no collision.
        op.execute(
            f"UPDATE {SYSTEM_SCHEMA}.portal_metric_visibility SET role = 'barber' WHERE role = 'viewer'"
        )
    # Synthetic staff rows carry the role as their position, so they are renamed here for the
    # same reason the downgrade renames them back: left at 'viewer' the row keeps the category
    # `unknown`, while the next `sync_portal_user_staff` writes 'barber' — and two accounts of
    # one role would sit on different sides of the leaderboard split until somebody edits one.
    # Rows the portal did not create (`id == portal_user_id`) keep the position YClients owns.
    op.execute(
        "UPDATE staff SET position = 'barber' "
        "WHERE position = 'viewer' AND portal_user_id IS NOT NULL AND id <> portal_user_id"
    )
    op.alter_column(
        'portal_users',
        'role',
        server_default='barber',
        existing_type=sa.String(length=32),
        existing_nullable=False,
        schema=SYSTEM_SCHEMA,
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    # Both names collapse back into the single role they replaced.
    op.execute(
        f"UPDATE {SYSTEM_SCHEMA}.portal_users SET role = 'viewer' WHERE role IN ('admin', 'barber')"
    )
    if _has_table(inspector, 'portal_metric_visibility'):
        # Only one row per tenant can survive the (account, role) unique index, and picking a
        # survivor means picking whose codes the single role inherits — which can only ever
        # grant a label something it was refused. So the pair's overrides do not survive a
        # rollback at all: the role falls back to its default, which grants nothing.
        op.execute(
            f"DELETE FROM {SYSTEM_SCHEMA}.portal_metric_visibility WHERE role IN ('admin', 'barber')"
        )
    # Synthetic staff rows carry the role as their position. Left as 'admin' they would read as
    # administrators to `normalize_staff_category`, a state the pre-0046 code could not reach.
    # Rows the portal did not create (`id == portal_user_id`) keep the position YClients owns.
    op.execute(
        "UPDATE staff SET position = 'viewer' "
        "WHERE position IN ('admin', 'barber') AND portal_user_id IS NOT NULL AND id <> portal_user_id"
    )
    op.alter_column(
        'portal_users',
        'role',
        server_default='viewer',
        existing_type=sa.String(length=32),
        existing_nullable=False,
        schema=SYSTEM_SCHEMA,
    )
