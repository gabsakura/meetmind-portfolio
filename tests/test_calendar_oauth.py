from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse

import httpx

from app.extensions import db
from app.models import CalendarConnection, User
from app.services import calendar_oauth
from tests.conftest import login


def _configure(app):
    app.config.update(
        MICROSOFT_CALENDAR_CLIENT_ID="microsoft-client",
        MICROSOFT_CALENDAR_CLIENT_SECRET="microsoft-secret",
        GOOGLE_CALENDAR_CLIENT_ID="google-client",
        GOOGLE_CALENDAR_CLIENT_SECRET="google-secret",
        PUBLIC_BASE_URL="http://localhost",
    )


def test_oauth_urls_use_pkce_and_calendar_write_scopes(client, app):
    _configure(app)
    login(client)

    microsoft = client.get("/perfil/calendario/conectar/microsoft")
    google = client.get("/perfil/calendario/conectar/google")

    assert microsoft.status_code == 302
    assert google.status_code == 302
    microsoft_query = parse_qs(urlparse(microsoft.headers["Location"]).query)
    google_query = parse_qs(urlparse(google.headers["Location"]).query)
    assert microsoft_query["code_challenge_method"] == ["S256"]
    assert "Calendars.ReadWrite" in microsoft_query["scope"][0]
    assert "Tasks.ReadWrite" in microsoft_query["scope"][0]
    assert "User.Read" in microsoft_query["scope"][0]
    assert google_query["code_challenge_method"] == ["S256"]
    assert "calendar.events" in google_query["scope"][0]
    assert "https://www.googleapis.com/auth/tasks" in google_query["scope"][0]
    assert "calendar.events.readonly" not in google_query["scope"][0]
    assert google_query["access_type"] == ["offline"]


def test_callback_rejects_invalid_state(client, app):
    _configure(app)
    login(client)
    client.get("/perfil/calendario/conectar/microsoft")

    response = client.get(
        "/perfil/calendario/callback/microsoft?code=code&state=invalid",
        follow_redirects=True,
    )

    assert "Não foi possível validar" in response.get_data(as_text=True)
    with app.app_context():
        assert CalendarConnection.query.count() == 0


def test_callback_encrypts_token_and_disconnects(client, app, monkeypatch):
    _configure(app)
    login(client)
    started = client.get("/perfil/calendario/conectar/microsoft")
    state = parse_qs(urlparse(started.headers["Location"]).query)["state"][0]
    monkeypatch.setattr(
        "app.blueprints.painel.routes.exchange_code",
        lambda *_args: {
            "access_token": "access-secret",
            "refresh_token": "refresh-secret",
            "token_type": "Bearer",
            "expires_at": 4_102_444_800,
        },
    )
    monkeypatch.setattr(
        "app.blueprints.painel.routes.authorized_account_email",
        lambda *_args: "microsoft.owner@example.test",
    )

    response = client.get(
        f"/perfil/calendario/callback/microsoft?code=valid-code&state={state}",
        follow_redirects=True,
    )

    assert "Calendário conectado" in response.get_data(as_text=True)
    with app.app_context():
        connection = CalendarConnection.query.one()
        assert connection.provider == "microsoft"
        assert b"access-secret" not in connection.token_ciphertext
        assert b"refresh-secret" not in connection.token_ciphertext

    disconnected = client.post(
        "/perfil/calendario/microsoft/desconectar", follow_redirects=True
    )
    assert "Calendário desconectado" in disconnected.get_data(as_text=True)
    with app.app_context():
        assert CalendarConnection.query.count() == 0


class _Response:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


def test_microsoft_identity_meetings_and_tasks_are_normalized(app, monkeypatch):
    def fake_get(url, **_kwargs):
        if url.endswith("/me?$select=mail,userPrincipalName"):
            return _Response({
                "mail": "CS.Real@Example.com",
                "userPrincipalName": "cs.real@example.com",
            })
        if url.endswith("/me/calendarView"):
            return _Response({"value": [{
                "id": "teams-meeting",
                "subject": "Revisão do cliente",
                "start": {"dateTime": "2026-09-25T12:00:00", "timeZone": "UTC"},
                "end": {"dateTime": "2026-09-25T13:00:00", "timeZone": "UTC"},
                "isAllDay": False,
                "isCancelled": False,
                "onlineMeetingProvider": "teamsForBusiness",
                "onlineMeeting": {"joinUrl": "https://teams.microsoft.com/l/meetup-join/1"},
                "webLink": "https://outlook.office/event",
            }]})
        if url.endswith("/me/todo/lists"):
            return _Response({"value": [{"id": "list-1", "displayName": "Tarefas"}]})
        if "/me/todo/lists/list-1/tasks" in url:
            return _Response({"value": [
                {
                    "id": "todo-1",
                    "title": "Retornar ao cliente",
                    "status": "notStarted",
                    "importance": "high",
                    "dueDateTime": {
                        "dateTime": "2026-09-26T15:00:00",
                        "timeZone": "UTC",
                    },
                },
                {
                    "id": "todo-done",
                    "title": "Concluída",
                    "status": "completed",
                    "dueDateTime": {
                        "dateTime": "2026-09-26T16:00:00",
                        "timeZone": "UTC",
                    },
                },
            ]})
        if url.endswith("/me/planner/tasks"):
            return _Response({"value": [{
                "id": "planner-1",
                "title": "Validar correção",
                "percentComplete": 25,
                "priority": 2,
                "dueDateTime": "2026-09-27T18:00:00Z",
            }]})
        raise AssertionError(url)

    monkeypatch.setattr(calendar_oauth.httpx, "get", fake_get)
    with app.app_context():
        email = calendar_oauth.authorized_account_email(
            "microsoft", {"access_token": "token"}, "fallback@example.test"
        )
        start = datetime(2026, 9, 24, tzinfo=UTC)
        end = datetime(2026, 10, 24, tzinfo=UTC)
        meetings = calendar_oauth._microsoft_events("token", start, end)
        tasks = calendar_oauth._microsoft_tasks("token", start, end)

    assert email == "cs.real@example.com"
    assert meetings[0]["kind_label"] == "Teams"
    assert meetings[0]["url"].startswith("https://teams.microsoft.com/")
    assert [item["id"] for item in tasks] == [
        "microsoft_todo-todo-1",
        "microsoft_planner-planner-1",
    ]
    assert tasks[0]["critical"] is True
    assert tasks[1]["kind_label"] == "Planner"


def test_google_identity_and_events_are_normalized(app, monkeypatch):
    captured = {}

    def fake_get(url, **kwargs):
        if url.endswith("/userinfo"):
            return _Response({"email": "Conta.Real@Gmail.com"})
        captured.update(kwargs.get("params") or {})
        return _Response({
            "items": [
                {
                    "id": "timed",
                    "summary": "Reunião da manhã",
                    "status": "confirmed",
                    "start": {"dateTime": "2026-09-23T09:00:00-03:00"},
                    "end": {"dateTime": "2026-09-23T10:00:00-03:00"},
                },
                {
                    "id": "all-day",
                    "summary": "Renovação",
                    "status": "confirmed",
                    "start": {"date": "2026-09-24"},
                    "end": {"date": "2026-09-25"},
                },
                {
                    "id": "recurring-instance",
                    "recurringEventId": "series",
                    "summary": "Check-in recorrente",
                    "status": "confirmed",
                    "start": {"dateTime": "2026-09-25T15:00:00-03:00"},
                    "end": {"dateTime": "2026-09-25T15:30:00-03:00"},
                },
                {
                    "id": "cancelled",
                    "status": "cancelled",
                    "start": {"dateTime": "2026-09-26T09:00:00-03:00"},
                    "end": {"dateTime": "2026-09-26T10:00:00-03:00"},
                },
            ]
        })

    monkeypatch.setattr(calendar_oauth.httpx, "get", fake_get)
    with app.app_context():
        email = calendar_oauth.authorized_account_email(
            "google", {"access_token": "token"}, "fallback@example.test"
        )
        events = calendar_oauth._google_events(
            "token",
            datetime(2026, 9, 22, tzinfo=UTC),
            datetime(2026, 10, 22, tzinfo=UTC),
        )

    assert email == "conta.real@gmail.com"
    assert [event["id"] for event in events] == [
        "google-timed", "google-all-day", "google-recurring-instance"
    ]
    assert events[0]["starts_at"] == "2026-09-23T09:00:00-03:00"
    assert events[0]["all_day"] is False
    assert events[1]["starts_at"] == "2026-09-24"
    assert events[1]["all_day"] is True
    assert captured["singleEvents"] == "true"
    assert captured["orderBy"] == "startTime"


def test_google_callback_saves_authorized_account(client, app, monkeypatch):
    _configure(app)
    login(client)
    started = client.get("/perfil/calendario/conectar/google")
    state = parse_qs(urlparse(started.headers["Location"]).query)["state"][0]
    monkeypatch.setattr(
        "app.blueprints.painel.routes.exchange_code",
        lambda *_args: {
            "access_token": "google-access",
            "refresh_token": "google-refresh",
            "token_type": "Bearer",
            "expires_at": 4_102_444_800,
        },
    )
    monkeypatch.setattr(
        "app.blueprints.painel.routes.authorized_account_email",
        lambda *_args: "calendar.owner@gmail.com",
    )

    response = client.get(
        f"/perfil/calendario/callback/google?code=valid&state={state}",
        follow_redirects=True,
    )

    assert response.status_code == 200
    with app.app_context():
        connection = CalendarConnection.query.one()
        assert connection.provider == "google"
        assert connection.account_email == "calendar.owner@gmail.com"


def test_expired_google_token_refreshes_and_api_failure_is_isolated(
    client, app, monkeypatch
):
    _configure(app)
    with app.app_context():
        user = User.query.filter_by(email="admin@example.test").one()
        connection = calendar_oauth.save_connection(
            user,
            "google",
            {
                "access_token": "expired-access",
                "refresh_token": "refresh-secret",
                "token_type": "Bearer",
                "expires_at": 1,
            },
            account_email="calendar.owner@gmail.com",
        )
        db.session.commit()
        connection_id = connection.id

    monkeypatch.setattr(
        calendar_oauth.httpx,
        "post",
        lambda *_args, **_kwargs: _Response({
            "access_token": "renewed-access",
            "expires_in": 3600,
            "token_type": "Bearer",
        }),
    )
    used_tokens = []

    def successful_events(token, _start, _end):
        used_tokens.append(token)
        return []

    monkeypatch.setattr(calendar_oauth, "_google_events", successful_events)
    login(client)
    assert client.get("/api/v1/dashboard/daily-briefing").status_code == 200
    assert used_tokens == ["renewed-access"]

    def unavailable(*_args):
        raise httpx.ConnectError("calendar unavailable")

    monkeypatch.setattr(calendar_oauth, "_google_events", unavailable)
    response = client.get("/api/v1/dashboard/daily-briefing")
    assert response.status_code == 200
    with app.app_context():
        connection = db.session.get(CalendarConnection, connection_id)
        assert connection.status == "error"
        assert connection.last_error == "calendar_events_unavailable"
        assert b"renewed-access" not in connection.token_ciphertext

def test_connected_calendars_can_create_explicit_follow_up_events(app, monkeypatch):
    _configure(app)
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs["json"]))
        payload = (
            {"id": "google-event", "htmlLink": "https://calendar.google/event"}
            if "googleapis" in url
            else {"id": "microsoft-event", "webLink": "https://outlook.office/event"}
        )
        return httpx.Response(201, json=payload, request=httpx.Request("POST", url))

    monkeypatch.setattr(calendar_oauth.httpx, "post", fake_post)
    with app.app_context():
        user = User.query.filter_by(email="admin@example.test").one()
        for provider in ("google", "microsoft"):
            calendar_oauth.save_connection(
                user,
                provider,
                {
                    "access_token": f"{provider}-token",
                    "refresh_token": "refresh",
                    "expires_at": int(datetime.now(UTC).timestamp()) + 3600,
                },
            )
        db.session.commit()
        google = calendar_oauth.create_event(
            user,
            "google",
            title="Retorno ao cliente",
            starts_at=datetime(2026, 9, 30, 14, 0, tzinfo=UTC),
            description="Confirmar estabilidade.",
            attendee_emails=["cliente@example.com"],
            idempotency_key="draft-123",
        )
        microsoft = calendar_oauth.create_event(
            user,
            "microsoft",
            title="Retorno ao cliente",
            starts_at=datetime(2026, 9, 30, 14, 0, tzinfo=UTC),
            description="Confirmar estabilidade.",
            attendee_emails=["cliente@example.com"],
            idempotency_key="draft-123",
        )

    assert google["id"] == "google-event"
    assert microsoft["id"] == "microsoft-event"
    assert calls[0][1]["summary"] == "Retorno ao cliente"
    assert calls[0][1]["attendees"] == [{"email": "cliente@example.com"}]
    assert calls[0][1]["extendedProperties"]["private"]["meetmindId"] == "draft-123"
    assert calls[1][1]["subject"] == "Retorno ao cliente"
    assert calls[1][1]["attendees"][0]["emailAddress"]["address"] == "cliente@example.com"
    assert calls[1][1]["transactionId"] == "draft-123"
    assert calls[1][1]["isOnlineMeeting"] is True
    assert calls[1][1]["onlineMeetingProvider"] == "teamsForBusiness"
