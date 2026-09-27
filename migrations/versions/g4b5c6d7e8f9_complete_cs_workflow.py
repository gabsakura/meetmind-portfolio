"""complete customer success workflow

Revision ID: g4b5c6d7e8f9
Revises: f3a4b5c6d7e8
Create Date: 2026-09-19 16:30:00
"""

import sqlalchemy as sa
from alembic import op

revision = "g4b5c6d7e8f9"
down_revision = "f3a4b5c6d7e8"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("users") as batch:
        batch.add_column(
            sa.Column(
                "last_briefing_seen_at", sa.DateTime(timezone=True), nullable=True
            )
        )
    with op.batch_alter_table("action_items") as batch:
        batch.add_column(sa.Column("parent_action_id", sa.String(36), nullable=True))
        batch.add_column(
            sa.Column(
                "customer_notified",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch.create_foreign_key(
            "fk_action_items_parent_action_id",
            "action_items",
            ["parent_action_id"],
            ["id"],
        )
        batch.create_index("ix_action_items_parent_action_id", ["parent_action_id"])

    with op.batch_alter_table("kanban_requests") as batch:
        batch.add_column(sa.Column("assigned_to_id", sa.String(36), nullable=True))
        batch.add_column(
            sa.Column("due_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch.add_column(sa.Column("response", sa.Text(), nullable=True))
        batch.add_column(
            sa.Column("version", sa.Integer(), nullable=False, server_default="1")
        )
        batch.add_column(
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch.add_column(
            sa.Column("responded_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch.create_foreign_key(
            "fk_kanban_requests_assigned_to_id",
            "users",
            ["assigned_to_id"],
            ["id"],
        )
        for column in ("assigned_to_id", "due_at"):
            batch.create_index(f"ix_kanban_requests_{column}", [column])
    op.execute(
        sa.text("UPDATE kanban_requests SET updated_at = created_at WHERE updated_at IS NULL")
    )


def downgrade():
    with op.batch_alter_table("kanban_requests") as batch:
        for column in ("due_at", "assigned_to_id"):
            batch.drop_index(f"ix_kanban_requests_{column}")
        batch.drop_constraint(
            "fk_kanban_requests_assigned_to_id", type_="foreignkey"
        )
        for column in (
            "responded_at",
            "updated_at",
            "version",
            "response",
            "due_at",
            "assigned_to_id",
        ):
            batch.drop_column(column)

    with op.batch_alter_table("action_items") as batch:
        batch.drop_index("ix_action_items_parent_action_id")
        batch.drop_constraint(
            "fk_action_items_parent_action_id", type_="foreignkey"
        )
        batch.drop_column("customer_notified")
        batch.drop_column("parent_action_id")
    with op.batch_alter_table("users") as batch:
        batch.drop_column("last_briefing_seen_at")
