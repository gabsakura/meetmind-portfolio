"""enterprise tenancy and AI governance

Revision ID: b7e2a4c91d30
Revises: a1b2c3d4e5f6
Create Date: 2026-08-05 12:00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "b7e2a4c91d30"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None

DEFAULT_ORGANIZATION_ID = "00000000-0000-0000-0000-000000000001"
TENANT_TABLES = (
    "users",
    "clients",
    "meetings",
    "segments",
    "analyses",
    "audit_events",
    "security_events",
    "file_verifications",
    "meeting_submissions",
)


def upgrade():
    op.create_table(
        "organizations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("slug", sa.String(length=80), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("retention_days", sa.Integer(), nullable=False),
        sa.Column("external_ai_enabled", sa.Boolean(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug"),
    )
    op.create_index("ix_organizations_slug", "organizations", ["slug"], unique=True)
    op.execute(
        sa.text(
            "INSERT INTO organizations "
            "(id, slug, name, retention_days, external_ai_enabled, is_active, created_at) "
            "VALUES (:id, :slug, :name, 7, :enabled, :active, CURRENT_TIMESTAMP)"
        ).bindparams(
            id=DEFAULT_ORGANIZATION_ID,
            slug="totvs-meetmind",
            name="TOTVS MeetMind",
            enabled=True,
            active=True,
        )
    )

    for table in TENANT_TABLES:
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.add_column(
                sa.Column(
                    "organization_id",
                    sa.String(length=36),
                    nullable=False,
                    server_default=DEFAULT_ORGANIZATION_ID,
                )
            )
            batch_op.create_index(
                f"ix_{table}_organization_id", ["organization_id"], unique=False
            )
            batch_op.create_foreign_key(
                f"fk_{table}_organization_id_organizations",
                "organizations",
                ["organization_id"],
                ["id"],
            )

    with op.batch_alter_table("clients", schema=None) as batch_op:
        batch_op.drop_index("ix_clients_external_id")
        batch_op.create_index("ix_clients_external_id", ["external_id"], unique=False)
        batch_op.create_unique_constraint(
            "uq_clients_org_external", ["organization_id", "external_id"]
        )
    with op.batch_alter_table("meetings", schema=None) as batch_op:
        batch_op.drop_index("ix_meetings_external_id")
        batch_op.create_index("ix_meetings_external_id", ["external_id"], unique=False)
        batch_op.create_unique_constraint(
            "uq_meetings_org_external", ["organization_id", "external_id"]
        )

    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("auth_source", sa.String(length=20), nullable=False, server_default="local")
        )
        batch_op.add_column(sa.Column("auth_subject", sa.String(length=200), nullable=True))
        batch_op.add_column(
            sa.Column("is_break_glass", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch_op.create_index("ix_users_auth_source", ["auth_source"], unique=False)
        batch_op.create_index("ix_users_auth_subject", ["auth_subject"], unique=False)

    with op.batch_alter_table("analyses", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("provider", sa.String(length=30), nullable=False, server_default="gemini_api")
        )

    with op.batch_alter_table("file_verifications", schema=None) as batch_op:
        batch_op.drop_index("ix_file_verifications_cache_key")
        batch_op.create_index(
            "ix_file_verifications_cache_key",
            ["organization_id", "content_hash", "prompt_version", "model"],
            unique=True,
        )

    op.create_table(
        "ai_executions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=False),
        sa.Column("analysis_id", sa.String(length=36), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=30), nullable=False),
        sa.Column("model", sa.String(length=100), nullable=False),
        sa.Column("prompt_version", sa.String(length=50), nullable=False),
        sa.Column("schema_sha256", sa.String(length=64), nullable=False),
        sa.Column("transcript_sha256", sa.String(length=64), nullable=False),
        sa.Column("context_sha256", sa.String(length=64), nullable=True),
        sa.Column("parameters", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("estimated_cost_microunits", sa.Integer(), nullable=True),
        sa.Column("cost_currency", sa.String(length=3), nullable=False),
        sa.Column("result_sha256", sa.String(length=64), nullable=True),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["analysis_id"], ["analyses.id"]),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("analysis_id", "attempt", name="uq_ai_executions_analysis_attempt"),
    )
    op.create_index("ix_ai_executions_analysis_id", "ai_executions", ["analysis_id"])
    op.create_index("ix_ai_executions_organization_id", "ai_executions", ["organization_id"])
    op.create_index("ix_ai_executions_status", "ai_executions", ["status"])

    op.create_table(
        "privacy_requests",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=False),
        sa.Column("requested_by_id", sa.String(length=36), nullable=False),
        sa.Column("request_type", sa.String(length=30), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("resolution", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(["requested_by_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in ("organization_id", "requested_by_id", "request_type", "status"):
        op.create_index(f"ix_privacy_requests_{column}", "privacy_requests", [column])

    op.create_table(
        "outbox_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=False),
        sa.Column("queue_name", sa.String(length=50), nullable=False),
        sa.Column("task_name", sa.String(length=100), nullable=False),
        sa.Column("job_id", sa.String(length=200), nullable=False),
        sa.Column("arguments", sa.JSON(), nullable=False),
        sa.Column("retry_intervals", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(length=100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id"),
    )
    for column in ("organization_id", "queue_name", "job_id", "status", "scheduled_for"):
        op.create_index(f"ix_outbox_events_{column}", "outbox_events", [column])

    if op.get_bind().dialect.name == "postgresql":
        protected_tables = (
            "clients",
            "meetings",
            "segments",
            "analyses",
            "file_verifications",
            "meeting_submissions",
            "ai_executions",
            "privacy_requests",
            "outbox_events",
        )
        for table in protected_tables:
            op.execute(sa.text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
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
        for table in (
            "clients",
            "meetings",
            "segments",
            "analyses",
            "file_verifications",
            "meeting_submissions",
            "ai_executions",
            "privacy_requests",
            "outbox_events",
        ):
            op.execute(sa.text(f"DROP POLICY IF EXISTS meetmind_tenant_isolation ON {table}"))
            op.execute(sa.text(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY"))
    op.drop_table("outbox_events")
    op.drop_table("privacy_requests")
    op.drop_table("ai_executions")
    with op.batch_alter_table("file_verifications", schema=None) as batch_op:
        batch_op.drop_index("ix_file_verifications_cache_key")
        batch_op.create_index(
            "ix_file_verifications_cache_key",
            ["content_hash", "prompt_version", "model"],
            unique=True,
        )
    with op.batch_alter_table("analyses", schema=None) as batch_op:
        batch_op.drop_column("provider")
    with op.batch_alter_table("meetings", schema=None) as batch_op:
        batch_op.drop_constraint("uq_meetings_org_external", type_="unique")
        batch_op.drop_index("ix_meetings_external_id")
        batch_op.create_index("ix_meetings_external_id", ["external_id"], unique=True)
    with op.batch_alter_table("clients", schema=None) as batch_op:
        batch_op.drop_constraint("uq_clients_org_external", type_="unique")
        batch_op.drop_index("ix_clients_external_id")
        batch_op.create_index("ix_clients_external_id", ["external_id"], unique=True)
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.drop_index("ix_users_auth_subject")
        batch_op.drop_index("ix_users_auth_source")
        batch_op.drop_column("is_break_glass")
        batch_op.drop_column("auth_subject")
        batch_op.drop_column("auth_source")
    for table in reversed(TENANT_TABLES):
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.drop_index(f"ix_{table}_organization_id")
            batch_op.drop_constraint(
                f"fk_{table}_organization_id_organizations", type_="foreignkey"
            )
            batch_op.drop_column("organization_id")
    op.drop_index("ix_organizations_slug", table_name="organizations")
    op.drop_table("organizations")
