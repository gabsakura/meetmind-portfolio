from __future__ import annotations

from app.models import User
from tests.conftest import login


def test_profile_shows_email_and_calendar_readiness(app):
    client = app.test_client()
    login(client, "analyst@example.test")

    response = client.get("/perfil")

    assert response.status_code == 200
    page = response.get_data(as_text=True)
    assert "Configuração pendente" in page
    assert "O envio por e-mail ainda não foi configurado" in page


def test_user_can_send_email_test_after_opt_in(app, monkeypatch):
    sent = []
    monkeypatch.setattr(
        "app.blueprints.painel.routes.send_delivery_email",
        lambda user, subject, body: sent.append((user.email, subject, body)) or True,
    )
    client = app.test_client()
    login(client, "analyst@example.test")
    client.post(
        "/perfil/notificacoes",
        data={"email_delivery_notifications": "true"},
    )

    response = client.post(
        "/perfil/notificacoes/testar",
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "E-mail de teste enviado" in response.get_data(as_text=True)
    assert sent[0][0] == "analyst@example.test"
    with app.app_context():
        user = User.query.filter_by(email="analyst@example.test").one()
        assert user.email_delivery_notifications
