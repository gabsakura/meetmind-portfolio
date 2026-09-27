"""add delegated calendar connections

Revision ID: h5c6d7e8f9a0
Revises: g4b5c6d7e8f9
Create Date: 2026-09-22 18:00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "h5c6d7e8f9a0"
down_revision = "g4b5c6d7e8f9"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "calendar_connections",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("provider", sa.String(20), nullable=False),
        sa.Column("account_email", sa.String(254), nullable=True),
        sa.Column("token_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("scopes", sa.Text(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("last_error", sa.String(100), nullable=True),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "organization_id", "user_id", "provider",
            name="uq_calendar_connection_user_provider",
        ),
    )
    op.create_index(
        "ix_calendar_connections_organization_id",
        "calendar_connections",
        ["organization_id"],
    )
    op.create_index(
        "ix_calendar_connections_user_id",
        "calendar_connections",
        ["user_id"],
    )
    op.create_index(
        "ix_calendar_connections_provider",
        "calendar_connections",
        ["provider"],
    )
    op.create_index(
        "ix_calendar_connections_status",
        "calendar_connections",
        ["status"],
    )


def downgrade():
    op.drop_index("ix_calendar_connections_status", table_name="calendar_connections")
    op.drop_index("ix_calendar_connections_provider", table_name="calendar_connections")
    op.drop_index("ix_calendar_connections_user_id", table_name="calendar_connections")
    op.drop_index(
        "ix_calendar_connections_organization_id",
        table_name="calendar_connections",
    )
    op.drop_table("calendar_connections")
