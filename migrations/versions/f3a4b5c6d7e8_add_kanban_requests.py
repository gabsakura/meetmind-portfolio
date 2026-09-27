"""add internal kanban requests

Revision ID: f3a4b5c6d7e8
Revises: e2f3a4b5c6d7
Create Date: 2026-09-19 12:00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "f3a4b5c6d7e8"
down_revision = "e2f3a4b5c6d7"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "kanban_requests",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("client_id", sa.String(36), nullable=True),
        sa.Column("source_action_id", sa.String(36), nullable=False),
        sa.Column("meeting_id", sa.String(36), nullable=False),
        sa.Column("analysis_id", sa.String(36), nullable=False),
        sa.Column("request_type", sa.String(50), nullable=False),
        sa.Column("department", sa.String(100), nullable=False),
        sa.Column("priority", sa.String(20), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("payload_json", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_by_id", sa.String(36), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="queued"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(["client_id"], ["clients.id"]),
        sa.ForeignKeyConstraint(["source_action_id"], ["action_items.id"]),
        sa.ForeignKeyConstraint(["meeting_id"], ["meetings.id"]),
        sa.ForeignKeyConstraint(["analysis_id"], ["analyses.id"]),
        sa.ForeignKeyConstraint(["created_by_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in (
        "organization_id", "client_id", "source_action_id", "meeting_id",
        "analysis_id", "request_type", "department", "priority", "created_by_id", "status",
    ):
        op.create_index(f"ix_kanban_requests_{column}", "kanban_requests", [column])
    if op.get_bind().dialect.name == "postgresql":
        op.execute(sa.text("ALTER TABLE kanban_requests ENABLE ROW LEVEL SECURITY"))
        op.execute(sa.text("ALTER TABLE kanban_requests FORCE ROW LEVEL SECURITY"))
        op.execute(sa.text(
            "CREATE POLICY meetmind_tenant_isolation ON kanban_requests "
            "USING (organization_id = NULLIF(current_setting('meetmind.organization_id', true), '')::varchar) "
            "WITH CHECK (organization_id = NULLIF(current_setting('meetmind.organization_id', true), '')::varchar)"
        ))


def downgrade():
    if op.get_bind().dialect.name == "postgresql":
        op.execute(sa.text("DROP POLICY IF EXISTS meetmind_tenant_isolation ON kanban_requests"))
        op.execute(sa.text("ALTER TABLE kanban_requests NO FORCE ROW LEVEL SECURITY"))
        op.execute(sa.text("ALTER TABLE kanban_requests DISABLE ROW LEVEL SECURITY"))
    op.drop_table("kanban_requests")
