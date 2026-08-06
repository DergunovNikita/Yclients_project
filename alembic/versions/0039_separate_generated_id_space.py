"""move generated ids clear of the yclients id space

Rows mirrored from YClients take their primary key from the upstream id when it is
free (external_pk_kwargs). When it is not — the same id already belongs to another
branch — the key falls back to the table's sequence, which lags far behind because
the explicit ids never advance it. comments_id_seq sat at 375898 against a max id of
50803920, so the fallback collided with an existing row and aborted the load.

Sequences now start above any plausible YClients id, which the bigint widening in
0038 makes room for. Generated and mirrored ids can no longer meet.

Revision ID: 0039_separate_generated_ids
Revises: 0038_widen_yclients_ids
Create Date: 2026-08-06 00:00:00.000000
"""

import sqlalchemy as sa
from alembic import op


revision = '0039_separate_generated_ids'
down_revision = '0038_widen_yclients_ids'
branch_labels = None
depends_on = None


# Upstream ids are around 2e9 and climbing; 1e12 leaves three orders of magnitude of
# headroom while staying far below the bigint ceiling.
GENERATED_ID_FLOOR = 1_000_000_000_000

# Only tables whose primary key is sometimes the upstream id. transactions and
# staff_schedules are always sequence-generated, so their sequences never collide.
MIRRORED_ID_SEQUENCES = (
    'appointments_id_seq',
    'financial_transactions_id_seq',
    'goods_transactions_id_seq',
    'comments_id_seq',
)


def upgrade() -> None:
    conn = op.get_bind()
    for sequence in MIRRORED_ID_SEQUENCES:
        current = conn.execute(sa.text(f"SELECT last_value FROM {sequence}")).scalar()
        if current is not None and current < GENERATED_ID_FLOOR:
            conn.execute(sa.text(f"SELECT setval('{sequence}', {GENERATED_ID_FLOOR}, false)"))


def downgrade() -> None:
    # Rewinding would hand out ids that are already taken, so the floor is left in
    # place. Dropping back to int4 in 0038's downgrade fails first anyway.
    pass
