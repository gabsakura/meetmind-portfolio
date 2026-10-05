from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.extensions import db
from app.models import DeliveryStatus, Role, Team, TeamDelivery, User
from app.services.calendar_oauth import CalendarOAuthError
from tests.conftest import login

PASSWORD = "correct-horse-battery-staple"


def _team_users(app):
    with app.app_context():
        admin = User.query.filter_by(email="admin@example.test").one()
        analyst = User.query.filter_by(email="analyst@example.test").one()
        manager = User(
            organization_id=admin.organization_id,
            email="delivery-manager@example.test",
            name="Gestora",
            role=Role.MANAGER,
        )
        manager.set_password(PASSWORD)
        outsider = User(
            organization_id=admin.organization_id,
            email="delivery-outsider@example.test",
            name="Analista de outra equipe",
            role=Role.ANALYST,
        )
        outsider.set_password(PASSWORD)
        db.session.add_all([manager, outsider])
        db.session.flush()
        managed = Team(
            organization_id=admin.organization_id,
            name="Equipe da gestora",
            manager_id=manager.id,
        )
        other = Team(organization_id=admin.organization_id, name="Outra equipe")
        db.session.add_all([managed, other])
        db.session.flush()
        analyst.team_id = managed.id
        outsider.team_id = other.id
        db.session.commit()
        return manager.id, analyst.id, outsider.id


def _payload(assignee_id: str, provider: str = "google") -> dict:
    return {
        "assignee_id": assignee_id,
        "title": "Preparar retorno do cliente",
        "description": "Reunir o diagnóstico e registrar o retorno.",
        "due_at": (datetime.now(UTC) + timedelta(days=2)).isoformat(),
        "priority": "high",
        "calendar_provider": provider,
    }


def test_manager_assigns_delivery_and_sees_employee_profile(app, monkeypatch):
    manager_id, analyst_id, _ = _team_users(app)
    captured = {}

    def fake_create_event(user, provider, **kwargs):
        captured.update(user_id=user.id, provider=provider, **kwargs)
        return {"id": "calendar-event-1", "url": "https://calendar.test/event/1"}

    monkeypatch.setattr("app.services.team_deliveries.create_event", fake_create_event)
    manager_client = app.test_client()
    login(manager_client, "delivery-manager@example.test")
    response = manager_client.post("/api/v1/team-deliveries", json=_payload(analyst_id))
    assert response.status_code == 201
    assert response.json["calendar_status"] == "synced"
    assert captured["user_id"] == analyst_id
    assert captured["online_meeting"] is False
    assert manager_client.get("/equipe").status_code == 200
    profile = manager_client.get(f"/equipe/{analyst_id}")
    assert profile.status_code == 200
    assert "Preparar retorno do cliente" in profile.get_data(as_text=True)

    analyst_client = app.test_client()
    login(analyst_client, "analyst@example.test")
    listed = analyst_client.get("/api/v1/team-deliveries")
    assert [item["id"] for item in listed.json["items"]] == [response.json["id"]]
    started = analyst_client.patch(
        f"/api/v1/team-deliveries/{response.json['id']}",
        json={"status": "in_progress", "expected_version": response.json["version"]},
    )
    assert started.status_code == 200
    completed = analyst_client.patch(
        f"/api/v1/team-deliveries/{response.json['id']}",
        json={"status": "completed", "expected_version": started.json["version"]},
    )
    assert completed.status_code == 200
    with app.app_context():
        assert db.session.get(TeamDelivery, response.json["id"]).assigned_by_id == manager_id


def test_manager_cannot_assign_or_view_outside_team(app, monkeypatch):
    _, analyst_id, outsider_id = _team_users(app)
    monkeypatch.setattr(
        "app.services.team_deliveries.create_event",
        lambda *_args, **_kwargs: {"id": "not-created"},
    )
    client = app.test_client()
    login(client, "delivery-manager@example.test")
    response = client.post("/api/v1/team-deliveries", json=_payload(outsider_id))
    assert response.status_code == 422
    assert client.get(f"/equipe/{outsider_id}").status_code == 404
    assert client.get(f"/equipe/{analyst_id}").status_code == 200


def test_calendar_failure_keeps_delivery_and_retry_syncs(app, monkeypatch):
    _, analyst_id, _ = _team_users(app)

    def fail_calendar(*_args, **_kwargs):
        raise CalendarOAuthError("calendar_connection_missing")

    monkeypatch.setattr("app.services.team_deliveries.create_event", fail_calendar)
    client = app.test_client()
    login(client, "delivery-manager@example.test")
    created = client.post(
        "/api/v1/team-deliveries", json=_payload(analyst_id, "microsoft")
    )
    assert created.status_code == 201
    assert created.json["calendar_status"] == "failed"
    with app.app_context():
        assert db.session.get(TeamDelivery, created.json["id"]) is not None

    analyst_client = app.test_client()
    login(analyst_client, "analyst@example.test")
    daily = analyst_client.get("/api/v1/dashboard/daily-briefing")
    assert any(
        item["id"] == f"delivery-{created.json['id']}"
        and item["kind_label"] == "Entrega"
        for item in daily.json["items"]
    )
    monkeypatch.setattr(
        "app.services.team_deliveries.create_event",
        lambda *_args, **_kwargs: {"id": "microsoft-event-1", "url": None},
    )
    retried = client.post(
        f"/api/v1/team-deliveries/{created.json['id']}/calendar/retry", json={}
    )
    assert retried.status_code == 200
    assert retried.json["calendar_status"] == "synced"


def test_analyst_cannot_assign_and_manager_can_cancel(app, monkeypatch):
    _, analyst_id, _ = _team_users(app)
    monkeypatch.setattr(
        "app.services.team_deliveries.create_event",
        lambda *_args, **_kwargs: {"id": "calendar-event-2", "url": None},
    )
    analyst_client = app.test_client()
    login(analyst_client, "analyst@example.test")
    assert analyst_client.post(
        "/api/v1/team-deliveries", json=_payload(analyst_id)
    ).status_code == 404

    manager_client = app.test_client()
    login(manager_client, "delivery-manager@example.test")
    created = manager_client.post("/api/v1/team-deliveries", json=_payload(analyst_id))
    cancelled = manager_client.patch(
        f"/api/v1/team-deliveries/{created.json['id']}",
        json={
            "status": DeliveryStatus.CANCELLED.value,
            "expected_version": created.json["version"],
        },
    )
    assert cancelled.status_code == 200
    assert cancelled.json["status"] == DeliveryStatus.CANCELLED.value
