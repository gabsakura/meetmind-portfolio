"""add delivery consent, change requests and notifications

Revision ID: m0b1c2d3e4f5
Revises: l9a0b1c2d3e4
"""

import sqlalchemy as sa
from alembic import op

revision = "m0b1c2d3e4f5"
down_revision = "l9a0b1c2d3e4"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "users",
        sa.Column(
            "email_delivery_notifications",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "calendar_connections",
        sa.Column("allow_team_deliveries", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "team_deliveries", sa.Column("seen_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("team_deliveries", sa.Column("change_request", sa.Text(), nullable=True))
    op.add_column(
        "team_deliveries",
        sa.Column("change_requested_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "user_notifications",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("kind", sa.String(50), nullable=False),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("target_type", sa.String(50), nullable=True),
        sa.Column("target_id", sa.String(100), nullable=True),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
    )
    for column in ("organization_id", "user_id", "kind", "target_id", "read_at"):
        op.create_index(f"ix_user_notifications_{column}", "user_notifications", [column])


def downgrade():
    op.drop_column("users", "email_delivery_notifications")
    op.drop_table("user_notifications")
    op.drop_column("team_deliveries", "change_requested_at")
    op.drop_column("team_deliveries", "change_request")
    op.drop_column("team_deliveries", "seen_at")
    op.drop_column("calendar_connections", "allow_team_deliveries")
