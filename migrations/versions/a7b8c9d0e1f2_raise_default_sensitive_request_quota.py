"""Raise the default sensitive request quota.

Revision ID: a7b8c9d0e1f2
Revises: f1a2b3c4d5e6
Create Date: 2026-09-11
"""

from alembic import op


revision = "a7b8c9d0e1f2"
down_revision = "f1a2b3c4d5e6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Promote existing users that still have the former default quota."""
    op.execute(
        "UPDATE users "
        "SET request_limit_per_10_minutes = 30 "
        "WHERE request_limit_per_10_minutes = 5"
    )


def downgrade() -> None:
    """Keep current user settings intact when the migration is rolled back."""
    pass
