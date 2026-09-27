"""customer success evidence plan action workflow

Revision ID: e2f3a4b5c6d7
Revises: d1e2f3a4b5c6
Create Date: 2026-09-18 12:00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "e2f3a4b5c6d7"
down_revision = "d1e2f3a4b5c6"
branch_labels = None
depends_on = None


def _index(table: str, column: str, *, unique: bool = False) -> None:
    op.create_index(f"ix_{table}_{column}", table, [column], unique=unique)


def _enable_rls(table: str) -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(sa.text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
    op.execute(sa.text(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY"))
    op.execute(sa.text(f"DROP POLICY IF EXISTS meetmind_tenant_isolation ON {table}"))
    op.execute(
        sa.text(
            f"CREATE POLICY meetmind_tenant_isolation ON {table} "
            "USING (organization_id = NULLIF("
            "current_setting('meetmind.organization_id', true), '')::varchar) "
            "WITH CHECK (organization_id = NULLIF("
            "current_setting('meetmind.organization_id', true), '')::varchar)"
        )
    )


def upgrade():
    with op.batch_alter_table("clients") as batch_op:
        batch_op.add_column(sa.Column("owner_id", sa.String(36), nullable=True))
        batch_op.create_foreign_key(
            "fk_clients_owner_id_users", "users", ["owner_id"], ["id"]
        )
        batch_op.create_index("ix_clients_owner_id", ["owner_id"])

    op.create_table(
        "meeting_plans",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("client_id", sa.String(36), nullable=True),
        sa.Column("meeting_id", sa.String(36), nullable=False),
        sa.Column("analysis_id", sa.String(36), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("objective", sa.Text(), nullable=False),
        sa.Column("situation", sa.Text(), nullable=False),
        sa.Column("recommendation", sa.Text(), nullable=False),
        sa.Column("evidence_segment_ids", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column(
            "status",
            sa.String(30),
            nullable=False,
            server_default="awaiting_review",
        ),
        sa.Column("reviewed_by_id", sa.String(36), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(["client_id"], ["clients.id"]),
        sa.ForeignKeyConstraint(["meeting_id"], ["meetings.id"]),
        sa.ForeignKeyConstraint(["analysis_id"], ["analyses.id"]),
        sa.ForeignKeyConstraint(["reviewed_by_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("analysis_id", name="uq_meeting_plans_analysis"),
    )
    for column in ("organization_id", "client_id", "meeting_id", "analysis_id", "status"):
        _index("meeting_plans", column)

    with op.batch_alter_table("action_items") as batch_op:
        batch_op.add_column(sa.Column("plan_id", sa.String(36), nullable=True))
        batch_op.add_column(
            sa.Column("priority", sa.String(20), nullable=False, server_default="low")
        )
        batch_op.add_column(
            sa.Column(
                "evidence_segment_ids",
                sa.JSON(),
                nullable=False,
                server_default="[]",
            )
        )
        batch_op.create_foreign_key(
            "fk_action_items_plan_id_meeting_plans",
            "meeting_plans",
            ["plan_id"],
            ["id"],
        )
        batch_op.create_index("ix_action_items_plan_id", ["plan_id"])
        batch_op.create_index("ix_action_items_priority", ["priority"])

    op.create_table(
        "evidence_issues",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("analysis_id", sa.String(36), nullable=False),
        sa.Column("segment_id", sa.String(100), nullable=False),
        sa.Column("reporter_id", sa.String(36), nullable=False),
        sa.Column("category", sa.String(40), nullable=False),
        sa.Column("comment", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(["analysis_id"], ["analyses.id"]),
        sa.ForeignKeyConstraint(["segment_id"], ["segments.id"]),
        sa.ForeignKeyConstraint(["reporter_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in ("organization_id", "analysis_id", "segment_id", "reporter_id", "category"):
        _index("evidence_issues", column)

    _enable_rls("meeting_plans")
    _enable_rls("evidence_issues")


def downgrade():
    if op.get_bind().dialect.name == "postgresql":
        for table in ("evidence_issues", "meeting_plans"):
            op.execute(sa.text(f"DROP POLICY IF EXISTS meetmind_tenant_isolation ON {table}"))
            op.execute(sa.text(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY"))
            op.execute(sa.text(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY"))

    op.drop_table("evidence_issues")

    with op.batch_alter_table("action_items") as batch_op:
        batch_op.drop_index("ix_action_items_priority")
        batch_op.drop_index("ix_action_items_plan_id")
        batch_op.drop_constraint(
            "fk_action_items_plan_id_meeting_plans", type_="foreignkey"
        )
        batch_op.drop_column("evidence_segment_ids")
        batch_op.drop_column("priority")
        batch_op.drop_column("plan_id")

    op.drop_table("meeting_plans")

    with op.batch_alter_table("clients") as batch_op:
        batch_op.drop_index("ix_clients_owner_id")
        batch_op.drop_constraint("fk_clients_owner_id_users", type_="foreignkey")
        batch_op.drop_column("owner_id")
