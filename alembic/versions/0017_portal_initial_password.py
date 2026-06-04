"""restore portal initial password revision marker"""

revision = '0017_portal_initial_password'
down_revision = '0014_branch_scoped_catalogs'
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Compatibility marker for deployed databases already stamped with this revision."""


def downgrade() -> None:
    """Compatibility marker only; no schema changes to revert."""
