"""add manager-assigned team deliveries

Revision ID: l9a0b1c2d3e4
Revises: k8f9a0b1c2d3
Create Date: 2026-10-04 22:00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "l9a0b1c2d3e4"
down_revision = "k8f9a0b1c2d3"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "team_deliveries",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("priority", sa.String(20), nullable=False, server_default="medium"),
        sa.Column("status", sa.String(30), nullable=False, server_default="pending"),
        sa.Column("assigned_by_id", sa.String(36), nullable=False),
        sa.Column("assignee_id", sa.String(36), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("calendar_provider", sa.String(20), nullable=False),
        sa.Column("calendar_status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("calendar_event_id", sa.String(300), nullable=True),
        sa.Column("calendar_event_url", sa.Text(), nullable=True),
        sa.Column("calendar_error", sa.String(120), nullable=True),
        sa.Column("calendar_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.ForeignKeyConstraint(["assigned_by_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["assignee_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
    )
    for column in (
        "organization_id", "assigned_by_id", "assignee_id", "due_at", "priority",
        "status", "calendar_provider", "calendar_status",
    ):
        op.create_index(f"ix_team_deliveries_{column}", "team_deliveries", [column])


def downgrade():
    op.drop_table("team_deliveries")
