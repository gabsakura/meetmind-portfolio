"""evidence to action, privacy execution and Teams receipts

Revision ID: c8f1d2e3a4b5
Revises: b7e2a4c91d30
Create Date: 2026-08-07 12:00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "c8f1d2e3a4b5"
down_revision = "b7e2a4c91d30"
branch_labels = None
depends_on = None

RLS_TABLES = (
    "users",
    "clients",
    "meetings",
    "segments",
    "analyses",
    "audit_events",
    "security_events",
    "file_verifications",
    "meeting_submissions",
    "ai_executions",
    "privacy_requests",
    "outbox_events",
    "action_items",
    "action_events",
    "analysis_feedback",
    "product_events",
    "privacy_executions",
    "teams_transcript_receipts",
)


def _index(table: str, column: str, *, unique: bool = False) -> None:
    op.create_index(f"ix_{table}_{column}", table, [column], unique=unique)


def upgrade():
    with op.batch_alter_table("organizations") as batch_op:
        batch_op.add_column(sa.Column("oidc_tenant_id", sa.String(100), nullable=True))
        batch_op.add_column(
            sa.Column(
                "legal_hold_enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch_op.create_index("ix_organizations_oidc_tenant_id", ["oidc_tenant_id"], unique=True)

    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_index("ix_users_email")
        batch_op.create_index("ix_users_email", ["email"], unique=False)
        batch_op.add_column(
            sa.Column("session_version", sa.Integer(), nullable=False, server_default="1")
        )
        batch_op.create_unique_constraint(
            "uq_users_org_email", ["organization_id", "email"]
        )
        batch_op.create_unique_constraint(
            "uq_users_org_auth_identity",
            ["organization_id", "auth_source", "auth_subject"],
        )

    with op.batch_alter_table("privacy_requests") as batch_op:
        batch_op.add_column(sa.Column("approved_by_id", sa.String(36), nullable=True))
        batch_op.add_column(sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.create_foreign_key(
            "fk_privacy_requests_approved_by_id_users",
            "users",
            ["approved_by_id"],
            ["id"],
        )

    with op.batch_alter_table("outbox_events") as batch_op:
        batch_op.add_column(sa.Column("correlation_id", sa.String(100), nullable=True))
        batch_op.add_column(sa.Column("started_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.create_index("ix_outbox_events_correlation_id", ["correlation_id"])

    op.create_table(
        "action_items",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("client_id", sa.String(36), nullable=True),
        sa.Column("meeting_id", sa.String(36), nullable=False),
        sa.Column("analysis_id", sa.String(36), nullable=False),
        sa.Column("source_index", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("urgency", sa.String(30), nullable=False),
        sa.Column("owner_id", sa.String(36), nullable=True),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("outcome", sa.Text(), nullable=True),
        sa.Column("dismissal_reason", sa.Text(), nullable=True),
        sa.Column("accepted_by_id", sa.String(36), nullable=True),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("external_system", sa.String(50), nullable=True),
        sa.Column("external_id", sa.String(200), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(["client_id"], ["clients.id"]),
        sa.ForeignKeyConstraint(["meeting_id"], ["meetings.id"]),
        sa.ForeignKeyConstraint(["analysis_id"], ["analyses.id"]),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["accepted_by_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("analysis_id", "source_index", name="uq_actions_analysis_source"),
    )
    for column in (
        "organization_id",
        "client_id",
        "meeting_id",
        "analysis_id",
        "owner_id",
        "status",
        "urgency",
        "due_at",
    ):
        _index("action_items", column)

    op.create_table(
        "action_events",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("action_item_id", sa.String(36), nullable=False),
        sa.Column("actor_id", sa.String(36), nullable=True),
        sa.Column("event_type", sa.String(50), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(["action_item_id"], ["action_items.id"]),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in ("organization_id", "action_item_id", "actor_id", "event_type"):
        _index("action_events", column)

    op.create_table(
        "analysis_feedback",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("analysis_id", sa.String(36), nullable=False),
        sa.Column("reviewer_id", sa.String(36), nullable=False),
        sa.Column("usefulness_rating", sa.Integer(), nullable=False),
        sa.Column("factual_accuracy", sa.String(30), nullable=False),
        sa.Column("action_useful", sa.Boolean(), nullable=False),
        sa.Column("correction", sa.Text(), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(["analysis_id"], ["analyses.id"]),
        sa.ForeignKeyConstraint(["reviewer_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "analysis_id", "reviewer_id", name="uq_feedback_analysis_reviewer"
        ),
    )
    for column in ("organization_id", "analysis_id", "reviewer_id", "factual_accuracy"):
        _index("analysis_feedback", column)

    op.create_table(
        "product_events",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("event_name", sa.String(50), nullable=False),
        sa.Column("target_type", sa.String(50), nullable=True),
        sa.Column("target_id", sa.String(100), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in ("organization_id", "user_id", "event_name", "target_id"):
        _index("product_events", column)

    op.create_table(
        "privacy_executions",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("privacy_request_id", sa.String(36), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("manifest", sa.JSON(), nullable=False),
        sa.Column("manifest_sha256", sa.String(64), nullable=True),
        sa.Column("error_code", sa.String(100), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(["privacy_request_id"], ["privacy_requests.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "privacy_request_id", "attempt", name="uq_privacy_execution_attempt"
        ),
    )
    for column in ("organization_id", "privacy_request_id", "status"):
        _index("privacy_executions", column)

    op.create_table(
        "teams_connections",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("tenant_id", sa.String(100), nullable=False),
        sa.Column("teams_app_id", sa.String(100), nullable=False),
        sa.Column("subscription_id", sa.String(200), nullable=True),
        sa.Column("subscription_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("last_error", sa.String(100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("organization_id"),
        sa.UniqueConstraint("subscription_id"),
    )
    _index("teams_connections", "organization_id", unique=True)
    for column in ("tenant_id", "subscription_id", "status"):
        _index("teams_connections", column, unique=column == "subscription_id")

    op.create_table(
        "teams_transcript_receipts",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("transcript_id", sa.String(200), nullable=False),
        sa.Column("meeting_id", sa.String(200), nullable=True),
        sa.Column("resource_url", sa.Text(), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("submission_id", sa.String(36), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(100), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(["submission_id"], ["meeting_submissions.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "organization_id", "transcript_id", name="uq_teams_receipt_org_transcript"
        ),
    )
    for column in (
        "organization_id",
        "transcript_id",
        "meeting_id",
        "status",
        "submission_id",
    ):
        _index("teams_transcript_receipts", column)

    if op.get_bind().dialect.name == "postgresql":
        for table in RLS_TABLES:
            op.execute(sa.text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
            op.execute(sa.text(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY"))
            op.execute(
                sa.text(f"DROP POLICY IF EXISTS meetmind_tenant_isolation ON {table}")
            )
            op.execute(
                sa.text(
                    f"CREATE POLICY meetmind_tenant_isolation ON {table} "
                    "USING (organization_id = NULLIF("
                    "current_setting('meetmind.organization_id', true), '')::varchar) "
                    "WITH CHECK (organization_id = NULLIF("
                    "current_setting('meetmind.organization_id', true), '')::varchar)"
                )
            )


def downgrade():
    if op.get_bind().dialect.name == "postgresql":
        for table in RLS_TABLES:
            op.execute(sa.text(f"DROP POLICY IF EXISTS meetmind_tenant_isolation ON {table}"))
            op.execute(sa.text(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY"))
            if table in {
                "action_items",
                "action_events",
                "analysis_feedback",
                "product_events",
                "privacy_executions",
                "teams_transcript_receipts",
            }:
                op.execute(sa.text(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY"))

    for table in (
        "teams_transcript_receipts",
        "teams_connections",
        "privacy_executions",
        "product_events",
        "analysis_feedback",
        "action_events",
        "action_items",
    ):
        op.drop_table(table)

    with op.batch_alter_table("outbox_events") as batch_op:
        batch_op.drop_index("ix_outbox_events_correlation_id")
        batch_op.drop_column("completed_at")
        batch_op.drop_column("started_at")
        batch_op.drop_column("correlation_id")
    with op.batch_alter_table("privacy_requests") as batch_op:
        batch_op.drop_constraint(
            "fk_privacy_requests_approved_by_id_users", type_="foreignkey"
        )
        batch_op.drop_column("approved_at")
        batch_op.drop_column("approved_by_id")
    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_constraint("uq_users_org_auth_identity", type_="unique")
        batch_op.drop_constraint("uq_users_org_email", type_="unique")
        batch_op.drop_column("session_version")
        batch_op.drop_index("ix_users_email")
        batch_op.create_index("ix_users_email", ["email"], unique=True)
    with op.batch_alter_table("organizations") as batch_op:
        batch_op.drop_index("ix_organizations_oidc_tenant_id")
        batch_op.drop_column("legal_hold_enabled")
        batch_op.drop_column("oidc_tenant_id")
