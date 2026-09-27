"""add meeting participants

Revision ID: j7e8f9a0b1c2
Revises: i6d7e8f9a0b1
Create Date: 2026-09-23 15:00:00
"""

import uuid

import sqlalchemy as sa
from alembic import op

revision = "j7e8f9a0b1c2"
down_revision = "i6d7e8f9a0b1"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "meeting_participants",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("meeting_id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("participation_role", sa.String(30), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.ForeignKeyConstraint(["meeting_id"], ["meetings.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("meeting_id", "user_id", name="uq_meeting_participants_meeting_user"),
    )
    op.create_index("ix_meeting_participants_meeting_id", "meeting_participants", ["meeting_id"])
    op.create_index("ix_meeting_participants_user_id", "meeting_participants", ["user_id"])
    op.create_index(
        "ix_meeting_participants_organization_id",
        "meeting_participants",
        ["organization_id"],
    )

    connection = op.get_bind()
    meetings = connection.execute(
        sa.text("SELECT id, created_by_id, created_at, organization_id FROM meetings")
    ).mappings()
    table = sa.table(
        "meeting_participants",
        sa.column("id", sa.String),
        sa.column("meeting_id", sa.String),
        sa.column("user_id", sa.String),
        sa.column("participation_role", sa.String),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("organization_id", sa.String),
    )
    rows = [
        {
            "id": str(uuid.uuid4()),
            "meeting_id": item["id"],
            "user_id": item["created_by_id"],
            "participation_role": "organizer",
            "created_at": item["created_at"],
            "organization_id": item["organization_id"],
        }
        for item in meetings
    ]
    if rows:
        op.bulk_insert(table, rows)
    if connection.dialect.name == "postgresql":
        op.execute(sa.text("ALTER TABLE meeting_participants ENABLE ROW LEVEL SECURITY"))
        op.execute(sa.text("ALTER TABLE meeting_participants FORCE ROW LEVEL SECURITY"))
        op.execute(
            sa.text(
                "CREATE POLICY meetmind_tenant_isolation ON meeting_participants "
                "USING (organization_id = NULLIF(current_setting("
                "'meetmind.organization_id', true), '')::varchar) "
                "WITH CHECK (organization_id = NULLIF(current_setting("
                "'meetmind.organization_id', true), '')::varchar)"
            )
        )


def downgrade():
    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        op.execute(
            sa.text("DROP POLICY IF EXISTS meetmind_tenant_isolation ON meeting_participants")
        )
        op.execute(sa.text("ALTER TABLE meeting_participants NO FORCE ROW LEVEL SECURITY"))
        op.execute(sa.text("ALTER TABLE meeting_participants DISABLE ROW LEVEL SECURITY"))
    op.drop_index("ix_meeting_participants_organization_id", table_name="meeting_participants")
    op.drop_index("ix_meeting_participants_user_id", table_name="meeting_participants")
    op.drop_index("ix_meeting_participants_meeting_id", table_name="meeting_participants")
    op.drop_table("meeting_participants")
