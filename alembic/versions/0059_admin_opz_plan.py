"""OPZ plan for administrators

Plan/fact now plans an administrator's `opz_qty` as their planned clients times the branch `opz_pct` of the same
month (`_staff_metric_values_from_input`), and `opz_pct` follows from it. Months already saved by the plan editor
have no such rows, so they are filled here with the arithmetic the editor would have used: the quantity rounds
half up, the percent is 100 * quantity / rounded clients, both in float8 like the Python code.

Revision ID: 0059_admin_opz_plan
Revises: 0058_portal_branch_sort_order
"""

from alembic import op


revision = '0059_admin_opz_plan'
down_revision = '0058_portal_branch_sort_order'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        WITH planned AS (
            SELECT
                pi.period_start,
                pi.period_end,
                pi.company_id,
                pi.staff_id,
                pi.updated_at,
                floor(pi.clients * pbs.opz_pct + 0.5::float8) AS opz_qty,
                floor(pi.clients + 0.5::float8) AS clients
            FROM plan_staff_inputs pi
            JOIN plan_branch_settings pbs
                ON pbs.period_start = pi.period_start
                AND pbs.period_end = pi.period_end
                AND pbs.company_id = pi.company_id
            WHERE pi.staff_category = 'administrator'
                AND pi.clients > 0
                AND pbs.opz_pct IS NOT NULL
                AND EXISTS (
                    SELECT 1 FROM plan_metrics pm
                    WHERE pm.period_start = pi.period_start
                        AND pm.period_end = pi.period_end
                        AND pm.company_id = pi.company_id
                        AND pm.staff_id = pi.staff_id
                        AND pm.source = 'dashboard_plan_settings'
                )
                AND NOT EXISTS (
                    SELECT 1 FROM plan_metrics pm
                    WHERE pm.period_start = pi.period_start
                        AND pm.period_end = pi.period_end
                        AND pm.company_id = pi.company_id
                        AND pm.staff_id = pi.staff_id
                        AND pm.metric_code = 'opz_qty'
                )
        )
        INSERT INTO plan_metrics (
            period_start, period_end, company_id, staff_id, staff_category, metric_code, value, source, updated_at
        )
        SELECT period_start, period_end, company_id, staff_id, 'administrator', metric_code, value,
            'dashboard_plan_settings', updated_at
        FROM planned,
            LATERAL (VALUES
                ('opz_qty', opz_qty),
                ('opz_pct', CASE WHEN clients = 0 THEN 0.0::float8 ELSE 100.0::float8 * opz_qty / clients END)
            ) AS metric(metric_code, value)
        ON CONFLICT (period_start, period_end, company_id, staff_id, metric_code) WHERE staff_id IS NOT NULL
        DO NOTHING
        """
    )


def downgrade() -> None:
    # Before this revision the plan editor never produced OPZ rows for administrators, so every such
    # editor-sourced row is one this revision (or the code that came with it) wrote.
    op.execute(
        """
        DELETE FROM plan_metrics
        WHERE staff_category = 'administrator'
            AND metric_code IN ('opz_qty', 'opz_pct')
            AND source = 'dashboard_plan_settings'
        """
    )
