from __future__ import annotations

from datetime import timedelta

import pytest

from app.extensions import db
from app.models import Organization, TeamsConnection, utcnow
from app.services.teams_graph import (
    TeamsGraphError,
    create_or_renew_subscription,
    download_transcript,
    probe_credentials,
)


class FakeResponse:
    def __init__(self, status_code=200, payload=None, content=b"WEBVTT\n"):
        self.status_code = status_code
        self._payload = payload or {}
        self.content = content

    def json(self):
        return self._payload


def _connection(app) -> TeamsConnection:
    organization = Organization.query.one()
    connection = TeamsConnection(
        organization_id=organization.id,
        tenant_id="tenant-rsc",
        teams_app_id="teams-app-rsc",
        status="pending",
    )
    db.session.add(connection)
    db.session.commit()
    app.config.update(
        TEAMS_GRAPH_ENABLED=True,
        TEAMS_GRAPH_CLIENT_ID="client-id",
        TEAMS_GRAPH_CLIENT_SECRET="client-secret",
        TEAMS_GRAPH_CLIENT_STATE="client-state",
        PUBLIC_BASE_URL="https://meetmind.example",
    )
    return connection


def test_graph_probe_subscription_creation_and_download(app, monkeypatch):
    with app.app_context():
        connection = _connection(app)

        def fake_post(url, **_kwargs):
            if "oauth2" in url:
                return FakeResponse(payload={"access_token": "token"})
            return FakeResponse(
                payload={
                    "id": "subscription-id",
                    "expirationDateTime": (utcnow() + timedelta(hours=1)).isoformat(),
                }
            )

        monkeypatch.setattr("app.services.teams_graph.httpx.post", fake_post)
        monkeypatch.setattr(
            "app.services.teams_graph.httpx.get",
            lambda *_args, **_kwargs: FakeResponse(content=b"WEBVTT\n\nCliente: teste"),
        )

        assert probe_credentials(connection) is True
        assert create_or_renew_subscription(connection)["id"] == "subscription-id"
        assert download_transcript(
            connection, "communications/onlineMeetings/m1/transcripts/t1"
        ).startswith(b"WEBVTT")


def test_graph_renews_existing_subscription(app, monkeypatch):
    with app.app_context():
        connection = _connection(app)
        connection.subscription_id = "existing"
        monkeypatch.setattr(
            "app.services.teams_graph.httpx.post",
            lambda *_args, **_kwargs: FakeResponse(payload={"access_token": "token"}),
        )
        monkeypatch.setattr(
            "app.services.teams_graph.httpx.patch",
            lambda *_args, **_kwargs: FakeResponse(
                payload={"id": "existing", "expirationDateTime": utcnow().isoformat()}
            ),
        )
        assert create_or_renew_subscription(connection)["id"] == "existing"


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (403, "teams_graph_auth_rejected"),
        (500, "teams_graph_token_failed"),
    ],
)
def test_graph_token_errors_are_normalized(app, monkeypatch, status, expected):
    with app.app_context():
        connection = _connection(app)
        monkeypatch.setattr(
            "app.services.teams_graph.httpx.post",
            lambda *_args, **_kwargs: FakeResponse(status_code=status),
        )
        with pytest.raises(TeamsGraphError, match=expected):
            probe_credentials(connection)


def test_graph_rejects_untrusted_resource_and_empty_token(app, monkeypatch):
    with app.app_context():
        connection = _connection(app)
        monkeypatch.setattr(
            "app.services.teams_graph.httpx.post",
            lambda *_args, **_kwargs: FakeResponse(payload={}),
        )
        with pytest.raises(TeamsGraphError, match="teams_graph_token_missing"):
            probe_credentials(connection)
        with pytest.raises(TeamsGraphError, match="teams_graph_resource_rejected"):
            download_transcript(connection, "https://attacker.example/transcript")


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (403, "teams_graph_permission_denied"),
        (404, "teams_transcript_not_available"),
        (429, "teams_graph_rate_limited"),
        (500, "teams_graph_download_failed"),
        (200, "teams_transcript_empty"),
    ],
)
def test_graph_download_errors_are_operational(app, monkeypatch, status, expected):
    with app.app_context():
        connection = _connection(app)
        monkeypatch.setattr(
            "app.services.teams_graph.httpx.post",
            lambda *_args, **_kwargs: FakeResponse(payload={"access_token": "token"}),
        )
        monkeypatch.setattr(
            "app.services.teams_graph.httpx.get",
            lambda *_args, **_kwargs: FakeResponse(
                status_code=status,
                content=b"" if status == 200 else b"error",
            ),
        )
        with pytest.raises(TeamsGraphError, match=expected):
            download_transcript(connection, "users/u/onlineMeetings/m/transcripts/t")


def test_graph_requires_worker_configuration(app):
    with app.app_context():
        connection = _connection(app)
        app.config["TEAMS_GRAPH_ENABLED"] = False
        with pytest.raises(TeamsGraphError, match="teams_graph_not_configured"):
            probe_credentials(connection)
