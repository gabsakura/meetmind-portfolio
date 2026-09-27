"""Add isolated executions for the autopsy reading assistant.

Revision ID: b8c9d0e1f2a3
Revises: a7b8c9d0e1f2
Create Date: 2026-09-11
"""

import sqlalchemy as sa
from alembic import op

revision = "b8c9d0e1f2a3"
down_revision = "a7b8c9d0e1f2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "analysis_autopsy_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=False),
        sa.Column("analysis_id", sa.String(length=36), nullable=False),
        sa.Column("mode", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("bundle_version", sa.String(length=80), nullable=False),
        sa.Column("bundle_sha256", sa.String(length=64), nullable=False),
        sa.Column("source_references", sa.JSON(), nullable=False),
        sa.Column("path_id", sa.String(length=100), nullable=True),
        sa.Column("questions", sa.JSON(), nullable=False),
        sa.Column("quality_flags", sa.JSON(), nullable=False),
        sa.Column("baseline_result", sa.JSON(), nullable=False),
        sa.Column("guided_review", sa.JSON(), nullable=True),
        sa.Column("assisted_result", sa.JSON(), nullable=True),
        sa.Column("disagreements", sa.JSON(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("format_repair_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["analysis_id"], ["analyses.id"]),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("analysis_id", "bundle_version", name="uq_autopsy_analysis_bundle"),
    )
    for column in (
        "organization_id",
        "analysis_id",
        "mode",
        "status",
        "bundle_version",
        "bundle_sha256",
        "path_id",
    ):
        op.create_index(
            f"ix_analysis_autopsy_runs_{column}",
            "analysis_autopsy_runs",
            [column],
            unique=False,
        )
    if op.get_bind().dialect.name == "postgresql":
        op.execute(sa.text("ALTER TABLE analysis_autopsy_runs ENABLE ROW LEVEL SECURITY"))
        op.execute(sa.text("ALTER TABLE analysis_autopsy_runs FORCE ROW LEVEL SECURITY"))
        op.execute(
            sa.text(
                "CREATE POLICY meetmind_tenant_isolation ON analysis_autopsy_runs "
                "USING (organization_id = NULLIF(current_setting('meetmind.organization_id', "
                "true), '')) WITH CHECK (organization_id = NULLIF(current_setting("
                "'meetmind.organization_id', true), ''))"
            )
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            sa.text("DROP POLICY IF EXISTS meetmind_tenant_isolation ON analysis_autopsy_runs")
        )
        op.execute(sa.text("ALTER TABLE analysis_autopsy_runs NO FORCE ROW LEVEL SECURITY"))
    op.drop_table("analysis_autopsy_runs")
