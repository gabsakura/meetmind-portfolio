"""Comprova o isolamento PostgreSQL usando a credencial limitada da aplicação."""

from __future__ import annotations

import os

import psycopg

ORG_A = "10000000-0000-0000-0000-000000000001"
ORG_B = "20000000-0000-0000-0000-000000000002"


def main() -> None:
    owner_url = os.environ["MIGRATION_DATABASE_URL"].replace(
        "postgresql+psycopg://", "postgresql://"
    )
    app_url = os.environ["DATABASE_URL"].replace(
        "postgresql+psycopg://", "postgresql://"
    )
    with psycopg.connect(owner_url, autocommit=True) as owner:
        owner.execute(
            """
            INSERT INTO organizations
                (id, slug, name, retention_days, external_ai_enabled,
                 legal_hold_enabled, is_active, created_at)
            VALUES
                (%s, 'rls-a', 'RLS A', 7, true, false, true, CURRENT_TIMESTAMP),
                (%s, 'rls-b', 'RLS B', 7, true, false, true, CURRENT_TIMESTAMP)
            ON CONFLICT (id) DO NOTHING
            """,
            (ORG_A, ORG_B),
        )
        owner.execute(
            """
            INSERT INTO clients (id, external_id, name, created_at, organization_id)
            VALUES
                ('10000000-0000-0000-0000-000000000011', 'client-a', 'A',
                 CURRENT_TIMESTAMP, %s),
                ('20000000-0000-0000-0000-000000000022', 'client-b', 'B',
                 CURRENT_TIMESTAMP, %s)
            ON CONFLICT (id) DO NOTHING
            """,
            (ORG_A, ORG_B),
        )
        rls_rows = owner.execute(
            """
            SELECT relname, relrowsecurity, relforcerowsecurity
            FROM pg_class
            WHERE relname IN ('users', 'clients', 'action_items', 'privacy_executions')
            """
        ).fetchall()
        if len(rls_rows) != 4 or any(not row[1] or not row[2] for row in rls_rows):
            raise RuntimeError("force_rls_not_enabled")

    with psycopg.connect(app_url) as application:
        if application.execute("SELECT count(*) FROM clients").fetchone()[0] != 0:
            raise RuntimeError("select_without_tenant_was_not_blocked")
        application.execute("SELECT set_config('meetmind.organization_id', %s, true)", (ORG_A,))
        visible = application.execute("SELECT external_id FROM clients").fetchall()
        if visible != [("client-a",)]:
            raise RuntimeError("cross_tenant_select_detected")
        updated = application.execute(
            "UPDATE clients SET name = 'cross' WHERE organization_id = %s", (ORG_B,)
        )
        if updated.rowcount != 0:
            raise RuntimeError("cross_tenant_update_detected")
        try:
            application.execute(
                """
                INSERT INTO clients (id, external_id, name, created_at, organization_id)
                VALUES ('30000000-0000-0000-0000-000000000033', 'forbidden', 'X',
                        CURRENT_TIMESTAMP, %s)
                """,
                (ORG_B,),
            )
        except psycopg.errors.InsufficientPrivilege:
            application.rollback()
        else:
            raise RuntimeError("cross_tenant_insert_was_not_blocked")
    print("PostgreSQL FORCE RLS: SELECT, INSERT e UPDATE isolados.")


if __name__ == "__main__":
    main()
