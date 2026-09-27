from __future__ import annotations

import pytest

from app import create_app
from app.extensions import db
from app.models import AIExecution, Analysis, Organization, OutboxEvent, Role, User
from config import normalize_database_url


def test_render_postgres_url_uses_installed_psycopg_driver():
    assert (
        normalize_database_url("postgresql://user:secret@db.internal/app")
        == "postgresql+psycopg://user:secret@db.internal/app"
    )
    assert (
        normalize_database_url("postgres://user:secret@db.internal/app")
        == "postgresql+psycopg://user:secret@db.internal/app"
    )
    assert normalize_database_url("sqlite:///local.sqlite3") == "sqlite:///local.sqlite3"


def _login(client, email: str):
    return client.post(
        "/auth/login",
        data={"email": email, "password": "correct-horse-battery-staple"},
    )


def _meeting_payload(external_id: str = "shared-meeting") -> dict:
    return {
        "external_id": external_id,
        "title": "Reunião corporativa",
        "client_external_id": "shared-client",
        "client_name": "Cliente anonimizado",
        "segments": [
            {
                "id": f"{external_id}.0",
                "speaker": "Cliente",
                "text": "Precisamos revisar o próximo passo.",
            }
        ],
        "approved_for_external_ai": True,
        "metadata": {"source": "manual", "tags": ["enterprise-test"]},
    }


def test_health_endpoints_are_safe_and_ready_without_optional_services(client):
    live = client.get("/api/v1/health/live")
    ready = client.get("/api/v1/health/ready")

    assert live.status_code == 200
    assert live.json == {"service": "meetmind-web", "status": "ok"}
    assert ready.status_code == 200
    assert ready.json["checks"]["database"] == "ok"
    assert "X-Correlation-ID" in ready.headers


def test_production_refuses_insecure_local_configuration():
    with pytest.raises(RuntimeError, match="Configuração de produção incompleta"):
        create_app("config.TestConfig", {"APP_ENV": "production"})


def test_production_accepts_hardened_portfolio_demo_configuration():
    app = create_app(
        "config.TestConfig",
        {
            "APP_ENV": "production",
            "SERVICE_ROLE": "web",
            "AUTH_MODE": "local",
            "PORTFOLIO_DEMO_ENABLED": True,
            "DEMO_PORTFOLIO_PASSWORD": "portfolio-password-with-16-chars",
            "PRIVACY_CONTACT_EMAIL": "privacy@example.test",
            "SECRET_KEY": "production-portfolio-secret-key-32-chars",
            "SESSION_COOKIE_SECURE": True,
            "JSON_LOGS": True,
        },
    )

    assert app.config["PORTFOLIO_DEMO_ENABLED"] is True


def test_production_migration_does_not_require_application_only_services():
    app = create_app(
        "config.TestConfig",
        {
            "APP_ENV": "production",
            "SERVICE_ROLE": "migration",
            "SECRET_KEY": "production-migration-secret-key-32-chars",
            "JSON_LOGS": True,
        },
    )

    assert app.config["SERVICE_ROLE"] == "migration"


def test_tenant_guard_blocks_listing_and_direct_idor(admin_client, app):
    first = admin_client.post("/api/v1/meetings", json=_meeting_payload())
    assert first.status_code == 201

    with app.app_context():
        second_org = Organization(slug="second-org", name="Segunda organização")
        db.session.add(second_org)
        db.session.flush()
        second_admin = User(
            organization_id=second_org.id,
            email="second-admin@example.test",
            name="Second Admin",
            role=Role.ADMIN,
        )
        second_admin.set_password("correct-horse-battery-staple")
        db.session.add(second_admin)
        db.session.commit()

    second_client = app.test_client()
    _login(second_client, "second-admin@example.test")
    second = second_client.post("/api/v1/meetings", json=_meeting_payload())
    assert second.status_code == 201
    assert second.json["id"] != first.json["id"]

    first_listing = admin_client.get("/api/v1/meetings")
    second_listing = second_client.get("/api/v1/meetings")
    assert [item["id"] for item in first_listing.json["items"]] == [first.json["id"]]
    assert [item["id"] for item in second_listing.json["items"]] == [second.json["id"]]
    assert admin_client.get(f"/api/v1/meetings/{second.json['id']}").status_code == 404
    assert second_client.get(f"/api/v1/meetings/{first.json['id']}").status_code == 404


def test_organization_can_disable_external_ai(admin_client):
    created = admin_client.post("/api/v1/meetings", json=_meeting_payload("ai-policy"))
    changed = admin_client.patch(
        "/api/v1/admin/organization",
        json={"external_ai_enabled": False, "retention_days": 30},
    )
    response = admin_client.post(f"/api/v1/meetings/{created.json['id']}/analyses", json={})

    assert changed.status_code == 200
    assert changed.json["retention_days"] == 30
    assert response.status_code == 422
    assert response.json["code"] == "ai_disabled"


def test_privacy_export_and_request_are_scoped(admin_client):
    created = admin_client.post("/api/v1/meetings", json=_meeting_payload("privacy-meeting"))
    exported = admin_client.get("/api/v1/privacy/export")
    requested = admin_client.post(
        "/api/v1/privacy/requests",
        json={"type": "deletion", "reason": "Solicitação formal do titular."},
    )
    repeated = admin_client.post(
        "/api/v1/privacy/requests",
        json={"type": "deletion", "reason": "Solicitação formal do titular."},
    )

    assert exported.status_code == 200
    assert created.json["id"] in exported.json["meeting_ids"]
    assert exported.json["content_included"] is False
    assert requested.status_code == 201
    assert repeated.status_code == 200
    assert requested.json["id"] == repeated.json["id"]


def test_analysis_endpoint_exposes_immutable_execution_trace(admin_client, app):
    created = admin_client.post("/api/v1/meetings", json=_meeting_payload("trace-meeting"))
    with app.app_context():
        user = User.query.filter_by(email="admin@example.test").one()
        analysis = Analysis(
            organization_id=user.organization_id,
            meeting_id=created.json["id"],
            requested_by_id=user.id,
            provider="vertex_ai",
            model="gemini-2.5-flash",
            prompt_version="meetmind-transparent-v4",
            score_version="voc-deterministic-v2",
            transcript_hash="a" * 64,
        )
        db.session.add(analysis)
        db.session.flush()
        execution = AIExecution(
            organization_id=user.organization_id,
            analysis_id=analysis.id,
            provider="vertex_ai",
            model=analysis.model,
            prompt_version=analysis.prompt_version,
            schema_sha256="b" * 64,
            transcript_sha256="a" * 64,
            status="failed",
            error_code="provider_timeout",
        )
        db.session.add(execution)
        db.session.commit()
        analysis_id = analysis.id

    response = admin_client.get(f"/api/v1/analyses/{analysis_id}")
    assert response.status_code == 200
    assert response.json["traceability"]["provider"] == "vertex_ai"
    assert response.json["executions"][0]["schema_sha256"] == "b" * 64
    assert response.json["executions"][0]["error_code"] == "provider_timeout"


def test_transactional_outbox_is_idempotent_and_recovers_from_redis(app, monkeypatch):
    from app.services.outbox import add, dispatch

    monkeypatch.setattr(
        "app.services.outbox.redis_client",
        lambda: (_ for _ in ()).throw(ValueError("redis unavailable")),
    )
    with app.app_context():
        event = add(
            organization_id="00000000-0000-0000-0000-000000000001",
            queue_name="analyses",
            task_name="process_analysis",
            job_id="analysis-idempotency-test",
            arguments=["analysis-idempotency-test"],
            retry_intervals=[60, 180],
        )
        duplicate = add(
            organization_id=event.organization_id,
            queue_name="analyses",
            task_name="process_analysis",
            job_id=event.job_id,
        )
        db.session.commit()

        assert duplicate.id == event.id
        assert dispatch(event.id) is False
        stored = db.session.get(OutboxEvent, event.id)
        assert stored.status == "pending"
        assert stored.attempts == 1
