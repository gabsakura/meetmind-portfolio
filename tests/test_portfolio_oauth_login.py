from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from app.extensions import db
from app.models import CalendarConnection, Organization, Role, User


def _configure(app):
    app.config.update(
        PORTFOLIO_DEMO_ENABLED=True,
        PORTFOLIO_CALENDAR_OAUTH_ENABLED=True,
        GOOGLE_CALENDAR_CLIENT_ID="google-client",
        GOOGLE_CALENDAR_CLIENT_SECRET="google-secret",
        MICROSOFT_CALENDAR_CLIENT_ID="microsoft-client",
        MICROSOFT_CALENDAR_CLIENT_SECRET="microsoft-secret",
        MICROSOFT_CALENDAR_TENANT="common",
        PUBLIC_BASE_URL="https://meetmind.example",
    )
    with app.app_context():
        db.session.add(Organization(slug="meetmind-demo", name="MeetMind Demo"))
        db.session.commit()


def test_login_offers_configured_personal_providers(client, app):
    _configure(app)

    response = client.get("/auth/login")

    assert response.status_code == 200
    text = response.get_data(as_text=True)
    assert "Entrar com Google" in text
    assert "Entrar com Microsoft" in text
    assert "vinculados somente ao seu perfil" in text


def test_google_login_creates_personal_user_and_encrypted_connection(
    client, app, monkeypatch
):
    _configure(app)
    started = client.get("/auth/oauth/start/google")
    assert started.status_code == 302
    query = parse_qs(urlparse(started.headers["Location"]).query)
    assert query["redirect_uri"] == [
        "https://meetmind.example/auth/oauth/callback/google"
    ]
    state = query["state"][0]
    monkeypatch.setattr(
        "app.blueprints.auth.routes.exchange_code",
        lambda *_args: {
            "access_token": "private-access-token",
            "refresh_token": "private-refresh-token",
            "expires_at": 4_102_444_800,
        },
    )
    monkeypatch.setattr(
        "app.blueprints.auth.routes.authorized_account_profile",
        lambda *_args: {
            "email": "person@gmail.com",
            "name": "Pessoa CS",
            "subject": "google-subject",
        },
    )

    response = client.get(
        f"/auth/oauth/callback/google?code=valid-code&state={state}",
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/")
    with app.app_context():
        organization = Organization.query.filter_by(slug="meetmind-demo").one()
        user = User.query.filter_by(
            organization_id=organization.id,
            email="person@gmail.com",
        ).one()
        assert user.role == Role.ANALYST
        assert user.auth_source == "google"
        connection = CalendarConnection.query.filter_by(user_id=user.id).one()
        assert connection.account_email == "person@gmail.com"
        assert b"private-access-token" not in connection.token_ciphertext

    profile = client.get("/perfil")
    assert profile.status_code == 200
    assert "person@gmail.com" in profile.get_data(as_text=True)


def test_personal_login_rejects_invalid_state(client, app):
    _configure(app)
    client.get("/auth/oauth/start/microsoft")

    response = client.get(
        "/auth/oauth/callback/microsoft?code=valid-code&state=wrong",
        follow_redirects=True,
    )

    assert "Não foi possível validar a autorização" in response.get_data(as_text=True)
    with app.app_context():
        assert CalendarConnection.query.count() == 0
