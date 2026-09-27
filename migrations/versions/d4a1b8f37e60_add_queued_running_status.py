"""add queued and running to verification status

Revision ID: d4a1b8f37e60
Revises: c7d3e91a45f2
Create Date: 2026-07-28 12:00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "d4a1b8f37e60"
down_revision = "c7d3e91a45f2"
branch_labels = None
depends_on = None

ANTIGOS = ("BLOCKED", "REJECTED", "NEEDS_REVIEW", "APPROVED", "FAILED")
NOVOS = ("QUEUED", "RUNNING", *ANTIGOS)


def upgrade():
    # A classificação por IA passou a rodar no worker, então o registro nasce
    # QUEUED e vira RUNNING antes de concluir.
    with op.batch_alter_table("file_verifications", schema=None) as batch_op:
        batch_op.alter_column(
            "status",
            existing_type=sa.Enum(*ANTIGOS, name="verificationstatus"),
            type_=sa.Enum(*NOVOS, name="verificationstatus"),
            existing_nullable=False,
        )


def downgrade():
    # Registros em trânsito passam a NEEDS_REVIEW para não violar o enum antigo.
    op.execute(
        "UPDATE file_verifications SET status = 'NEEDS_REVIEW' "
        "WHERE status IN ('QUEUED', 'RUNNING')"
    )
    with op.batch_alter_table("file_verifications", schema=None) as batch_op:
        batch_op.alter_column(
            "status",
            existing_type=sa.Enum(*NOVOS, name="verificationstatus"),
            type_=sa.Enum(*ANTIGOS, name="verificationstatus"),
            existing_nullable=False,
        )
