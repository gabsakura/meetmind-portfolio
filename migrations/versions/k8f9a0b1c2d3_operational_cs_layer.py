"""operational customer success layer

Revision ID: k8f9a0b1c2d3
Revises: j7e8f9a0b1c2
Create Date: 2026-09-23 18:00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "k8f9a0b1c2d3"
down_revision = "j7e8f9a0b1c2"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("ALTER TYPE role ADD VALUE IF NOT EXISTS 'MANAGER'")

    op.create_table(
        "teams",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("manager_id", sa.String(36), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.ForeignKeyConstraint(["manager_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.UniqueConstraint("organization_id", "name", name="uq_teams_org_name"),
    )
    op.create_index("ix_teams_name", "teams", ["name"])
    op.create_index("ix_teams_manager_id", "teams", ["manager_id"])
    op.create_index("ix_teams_organization_id", "teams", ["organization_id"])
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("team_id", sa.String(36), nullable=True))
        batch.create_foreign_key(
            "fk_users_team_id", "teams", ["team_id"], ["id"]
        )
        batch.create_index("ix_users_team_id", ["team_id"])

    op.create_table(
        "client_contacts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("client_id", sa.String(36), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("email", sa.String(254), nullable=False),
        sa.Column("job_title", sa.String(160), nullable=True),
        sa.Column("phone", sa.String(40), nullable=True),
        sa.Column("is_primary", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.ForeignKeyConstraint(["client_id"], ["clients.id"]),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.UniqueConstraint(
            "organization_id", "client_id", "email", name="uq_client_contacts_email"
        ),
    )
    for column in ("client_id", "email", "is_primary", "is_active", "organization_id"):
        op.create_index(f"ix_client_contacts_{column}", "client_contacts", [column])

    with op.batch_alter_table("kanban_requests") as batch:
        batch.add_column(
            sa.Column(
                "customer_returned_at", sa.DateTime(timezone=True), nullable=True
            )
        )
        batch.add_column(
            sa.Column("customer_returned_by_id", sa.String(36), nullable=True)
        )
        batch.add_column(
            sa.Column(
                "customer_return_not_required_reason", sa.Text(), nullable=True
            )
        )
        batch.create_foreign_key(
            "fk_kanban_customer_returned_by",
            "users",
            ["customer_returned_by_id"],
            ["id"],
        )

    op.create_table(
        "communication_drafts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("client_id", sa.String(36), nullable=False),
        sa.Column("meeting_id", sa.String(36), nullable=True),
        sa.Column("action_id", sa.String(36), nullable=True),
        sa.Column("kanban_request_id", sa.String(36), nullable=True),
        sa.Column("draft_type", sa.String(40), nullable=False),
        sa.Column("channel", sa.String(30), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("recipient_contact_ids", sa.JSON(), nullable=False),
        sa.Column("current_revision", sa.Integer(), nullable=False),
        sa.Column("approved_revision", sa.Integer(), nullable=True),
        sa.Column("created_by_id", sa.String(36), nullable=False),
        sa.Column("approved_by_id", sa.String(36), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_by_id", sa.String(36), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("external_provider", sa.String(30), nullable=True),
        sa.Column("external_id", sa.String(200), nullable=True),
        sa.Column("external_url", sa.Text(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.ForeignKeyConstraint(["client_id"], ["clients.id"]),
        sa.ForeignKeyConstraint(["meeting_id"], ["meetings.id"]),
        sa.ForeignKeyConstraint(["action_id"], ["action_items.id"]),
        sa.ForeignKeyConstraint(["kanban_request_id"], ["kanban_requests.id"]),
        sa.ForeignKeyConstraint(["created_by_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["approved_by_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["sent_by_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
    )
    for column in (
        "client_id", "meeting_id", "action_id", "kanban_request_id", "draft_type",
        "channel", "status", "created_by_id", "organization_id",
    ):
        op.create_index(
            f"ix_communication_drafts_{column}", "communication_drafts", [column]
        )

    op.create_table(
        "communication_revisions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("communication_draft_id", sa.String(36), nullable=False),
        sa.Column("revision_number", sa.Integer(), nullable=False),
        sa.Column("subject", sa.String(500), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("evidence_segment_ids", sa.JSON(), nullable=False),
        sa.Column("generated_by_ai", sa.Boolean(), nullable=False),
        sa.Column("generation_metadata", sa.JSON(), nullable=False),
        sa.Column("created_by_id", sa.String(36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.ForeignKeyConstraint(["communication_draft_id"], ["communication_drafts.id"]),
        sa.ForeignKeyConstraint(["created_by_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.UniqueConstraint(
            "communication_draft_id",
            "revision_number",
            name="uq_communication_revision_number",
        ),
    )
    for column in ("communication_draft_id", "created_by_id", "organization_id"):
        op.create_index(
            f"ix_communication_revisions_{column}",
            "communication_revisions",
            [column],
        )


def downgrade():
    for column in ("communication_draft_id", "created_by_id", "organization_id"):
        op.drop_index(
            f"ix_communication_revisions_{column}", table_name="communication_revisions"
        )
    op.drop_table("communication_revisions")
    for column in (
        "client_id", "meeting_id", "action_id", "kanban_request_id", "draft_type",
        "channel", "status", "created_by_id", "organization_id",
    ):
        op.drop_index(
            f"ix_communication_drafts_{column}", table_name="communication_drafts"
        )
    op.drop_table("communication_drafts")
    with op.batch_alter_table("kanban_requests") as batch:
        batch.drop_constraint(
            "fk_kanban_customer_returned_by", type_="foreignkey"
        )
        batch.drop_column("customer_return_not_required_reason")
        batch.drop_column("customer_returned_by_id")
        batch.drop_column("customer_returned_at")
    for column in ("client_id", "email", "is_primary", "is_active", "organization_id"):
        op.drop_index(f"ix_client_contacts_{column}", table_name="client_contacts")
    op.drop_table("client_contacts")
    with op.batch_alter_table("users") as batch:
        batch.drop_index("ix_users_team_id")
        batch.drop_constraint("fk_users_team_id", type_="foreignkey")
        batch.drop_column("team_id")
    op.drop_index("ix_teams_organization_id", table_name="teams")
    op.drop_index("ix_teams_manager_id", table_name="teams")
    op.drop_index("ix_teams_name", table_name="teams")
    op.drop_table("teams")
