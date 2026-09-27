from __future__ import annotations

from app.extensions import db
from app.models import Role, SecurityEvent, User
from app.services.privacy import detect_pii


def test_security_headers(client):
    response = client.get("/api/v1/ping")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert "connect-src 'self'" in response.headers["Content-Security-Policy"]
    assert response.headers["X-Frame-Options"] == "DENY"


def test_x_forwarded_for_is_not_trusted_by_default(client, app):
    with app.test_request_context(
        "/api/v1/ping",
        headers={"X-Forwarded-For": "203.0.113.77"},
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    ):
        from flask import request

        assert request.remote_addr == "127.0.0.1"


def test_pii_detection():
    assert detect_pii("Meu e-mail é pessoa@example.com") == ["email"]
    assert "cpf" in detect_pii("CPF 123.456.789-10")
    assert detect_pii("[PESSOA] sem identificadores") == []


def test_no_gemini_key_in_frontend_files():
    from pathlib import Path

    root = Path(__file__).parents[1] / "app"
    served = list((root / "static").rglob("*")) + list((root / "templates").rglob("*"))
    for path in served:
        if path.is_file():
            assert "GEMINI_API_KEY" not in path.read_text(encoding="utf-8", errors="ignore")


def test_frontend_has_no_external_connections():
    from pathlib import Path

    root = Path(__file__).parents[1] / "app" / "static" / "js"
    javascript = "\n".join(path.read_text(encoding="utf-8") for path in root.glob("*.js"))
    assert "https://" not in javascript
    assert "http://" not in javascript


def test_web_container_does_not_receive_gemini_key():
    from pathlib import Path

    compose = (Path(__file__).parents[1] / "docker-compose.yml").read_text(encoding="utf-8")
    web_section = compose.split("  web:", 1)[1].split("  worker:", 1)[0]
    worker_section = compose.split("  worker:", 1)[1]
    assert "GEMINI_API_KEY" not in web_section
    assert "GEMINI_API_KEY" in worker_section


def test_docker_build_excludes_local_artifacts_and_node_toolchain():
    from pathlib import Path

    root = Path(__file__).parents[1]
    dockerignore = (root / ".dockerignore").read_text(encoding="utf-8").splitlines()
    dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
    assert {".pip-audit-cache", ".pytest-tmp", "instance", ".venv"}.issubset(
        set(dockerignore)
    )
    assert "FROM node:" not in dockerfile
    assert "package.json" not in dockerfile
    assert "COPY app ./app" in dockerfile
    assert (root / "app" / "static" / "vendor" / "chart.umd.js").is_file()


def test_local_mode_does_not_parse_memory_rate_limit_as_redis(app):
    app.config.update(
        TESTING=False,
        REDIS_URL="memory://",
        RATELIMIT_STORAGE_URI="memory://",
        SECURITY_REDIS_ENABLED=False,
    )
    response = app.test_client().get("/auth/login")
    assert response.status_code == 200


def test_login_preserves_only_local_next_destination(client):
    accepted = client.post(
        "/auth/login",
        data={
            "email": "admin@example.test",
            "password": "correct-horse-battery-staple",
            "next": "/verificacao",
        },
        follow_redirects=False,
    )
    assert accepted.status_code == 302
    assert accepted.headers["Location"].endswith("/verificacao")

    client.post("/auth/logout")
    rejected = client.post(
        "/auth/login",
        data={
            "email": "admin@example.test",
            "password": "correct-horse-battery-staple",
            "next": "//malicious.example/path",
        },
        follow_redirects=False,
    )
    assert rejected.status_code == 302
    assert rejected.headers["Location"].endswith("/")


def test_third_rate_violation_blocks_ip(app, monkeypatch):
    from flask import request

    from app.models import SecurityEvent
    from app.security import record_rate_limit

    app.config["SECURITY_REDIS_ENABLED"] = True
    app.config["RATE_LIMIT_BLOCK_THRESHOLD"] = 3

    class FakeRedis:
        def __init__(self):
            self.count = 0
            self.blocked = {}

        def incr(self, _key):
            self.count += 1
            return self.count

        def expire(self, _key, _seconds):
            return True

        def setex(self, key, seconds, value):
            self.blocked[key] = (seconds, value)

    fake = FakeRedis()
    monkeypatch.setattr("app.security.redis_client", lambda: fake)
    with app.test_request_context(
        "/api/v1/meetings",
        method="POST",
        data="secret transcript must never be logged",
        environ_base={"REMOTE_ADDR": "192.0.2.44"},
    ):
        for _ in range(3):
            record_rate_limit(request)
    assert len(fake.blocked) == 1
    assert next(iter(fake.blocked.values())) == (3600, "1")
    with app.app_context():
        event = SecurityEvent.query.filter_by(severity="high").first()
        assert event is not None
        serialized = str(event.details).lower()
        assert "secret" not in serialized
        assert "transcript" not in serialized


def test_security_events_are_grouped_by_ip_and_type(admin_client, app):
    with app.app_context():
        admin = User.query.filter_by(email="admin@example.test").one()
        common = {
            "severity": "medium",
            "user_id": admin.id,
            "masked_ip": "192.0.x.x",
            "ip_hash": "same-ip-hash",
            "user_agent_hash": "agent-hash",
            "details": {},
        }
        db.session.add_all(
            [
                SecurityEvent(
                    event_type="RATE_LIMIT_EXCEEDED",
                    route="/api/v1/meetings",
                    **common,
                ),
                SecurityEvent(
                    event_type="RATE_LIMIT_EXCEEDED",
                    route="/api/v1/meetings/1/analyses",
                    **common,
                ),
                SecurityEvent(
                    event_type="LOGIN_FAILED",
                    route="/auth/login",
                    **common,
                ),
            ]
        )
        db.session.commit()

    response = admin_client.get("/api/v1/admin/security-events")

    assert response.status_code == 200
    assert response.json["total"] == 2
    rate_group = next(
        item for item in response.json["items"] if item["type"] == "RATE_LIMIT_EXCEEDED"
    )
    assert rate_group["count"] == 2

    acknowledged = admin_client.post(
        f"/api/v1/admin/security-events/{rate_group['id']}/acknowledge",
        json={},
    )

    assert acknowledged.status_code == 200
    assert acknowledged.json["count"] == 2
    with app.app_context():
        rate_events = SecurityEvent.query.filter_by(
            ip_hash="same-ip-hash",
            event_type="RATE_LIMIT_EXCEEDED",
        ).all()
        assert all(event.acknowledged_at is not None for event in rate_events)


def test_cli_unblocks_latest_rate_limited_ip(app, monkeypatch):
    class FakeRedis:
        deleted = ()

        def delete(self, *keys):
            self.deleted = keys
            return len(keys)

    fake = FakeRedis()
    monkeypatch.setattr("app.cli.redis_client", lambda: fake)
    with app.app_context():
        event = SecurityEvent(
            event_type="RATE_LIMIT_EXCEEDED",
            severity="high",
            user_id=None,
            route="/api/v1/meetings",
            masked_ip="192.0.x.x",
            ip_hash="blocked-ip-hash",
            user_agent_hash="agent-hash",
            details={"blocked_for_seconds": 3600},
        )
        db.session.add(event)
        db.session.commit()

    result = app.test_cli_runner().invoke(args=["unblock-ip", "--latest"])

    assert result.exit_code == 0
    assert fake.deleted == (
        "meetmind:blocked:blocked-ip-hash",
        "meetmind:violations:blocked-ip-hash",
    )


def test_cli_sets_per_user_request_limit(app):
    result = app.test_cli_runner().invoke(
        args=[
            "set-user-request-limit",
            "--email",
            "analyst@example.test",
            "--requests",
            "100",
        ]
    )

    assert result.exit_code == 0
    with app.app_context():
        analyst = User.query.filter_by(
            email="analyst@example.test",
            role=Role.ANALYST,
        ).one()
        assert analyst.request_limit_per_10_minutes == 100
