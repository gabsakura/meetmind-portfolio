from __future__ import annotations

from app import create_app
from app.extensions import db
from app.models import Role, SecurityEvent, User


def test_thirty_first_sensitive_operation_returns_429(monkeypatch):
    class FakeRedis:
        count = 0

        def incr(self, _key):
            self.count += 1
            return self.count

        def expire(self, _key, _seconds):
            return True

        def setex(self, _key, _seconds, _value):
            return True

    monkeypatch.setattr("app.security.redis_client", lambda: FakeRedis())
    app = create_app(
        "config.TestConfig",
        {
            "RATELIMIT_ENABLED": True,
            "RATELIMIT_STORAGE_URI": "memory://",
        },
    )
    with app.app_context():
        db.create_all()
        user = User(email="rate@example.test", name="Rate Admin", role=Role.ADMIN)
        user.set_password("correct-horse-battery-staple")
        db.session.add(user)
        db.session.commit()
    client = app.test_client()
    client.post(
        "/auth/login",
        data={"email": "rate@example.test", "password": "correct-horse-battery-staple"},
    )
    responses = []
    for index in range(31):
        responses.append(
            client.post(
                "/api/v1/meetings",
                json={
                    "external_id": f"rate-{index}",
                    "title": "Teste de limite",
                    "transcript": "Conteúdo anonimizado.",
                    "metadata": {"source": "manual"},
                },
            )
        )
    assert [response.status_code for response in responses[:30]] == [201] * 30
    assert responses[30].status_code == 429
    assert responses[30].headers["Retry-After"]
    assert responses[30].json["code"] == "rate_limit_exceeded"
    with app.app_context():
        assert SecurityEvent.query.filter_by(event_type="RATE_LIMIT_EXCEEDED").count() == 1
        db.session.remove()
        db.drop_all()


def test_user_with_elevated_limit_can_exceed_default_limit():
    app = create_app(
        "config.TestConfig",
        {
            "RATELIMIT_ENABLED": True,
            "RATELIMIT_STORAGE_URI": "memory://",
        },
    )
    with app.app_context():
        db.create_all()
        user = User(
            email="elevated@example.test",
            name="Elevated Admin",
            role=Role.ADMIN,
            request_limit_per_10_minutes=100,
        )
        user.set_password("correct-horse-battery-staple")
        db.session.add(user)
        db.session.commit()
    client = app.test_client()
    client.post(
        "/auth/login",
        data={
            "email": "elevated@example.test",
            "password": "correct-horse-battery-staple",
        },
    )

    responses = [
        client.post(
            "/api/v1/meetings",
            json={
                "external_id": f"elevated-{index}",
                "title": "Teste de cota elevada",
                "transcript": "Conteúdo anonimizado.",
                "metadata": {"source": "manual"},
            },
        )
        for index in range(6)
    ]

    assert [response.status_code for response in responses] == [201] * 6
    with app.app_context():
        db.session.remove()
        db.drop_all()
