from __future__ import annotations

import pytest

from app import create_app
from app.extensions import db
from app.models import DEFAULT_ORGANIZATION_ID, Organization, Role, User


@pytest.fixture()
def app(tmp_path):
    app = create_app(
        "config.TestConfig",
        {"SUBMISSION_STORAGE_DIR": str(tmp_path / "submissions")},
    )
    with app.app_context():
        db.create_all()
        db.session.add(
            Organization(
                id=DEFAULT_ORGANIZATION_ID,
                slug="meetmind-portfolio",
                name="MeetMind Portfolio",
            )
        )
        db.session.flush()
        admin = User(
            email="admin@example.test",
            name="Admin",
            role=Role.ADMIN,
        )
        admin.set_password("correct-horse-battery-staple")
        analyst = User(email="analyst@example.test", name="Analyst", role=Role.ANALYST)
        analyst.set_password("correct-horse-battery-staple")
        viewer = User(email="viewer@example.test", name="Viewer", role=Role.VIEWER)
        viewer.set_password("correct-horse-battery-staple")
        db.session.add_all([admin, analyst, viewer])
        db.session.commit()
    yield app
    with app.app_context():
        db.session.remove()
        db.drop_all()


@pytest.fixture()
def client(app):
    return app.test_client()


def login(client, email="admin@example.test"):
    return client.post(
        "/auth/login",
        data={"email": email, "password": "correct-horse-battery-staple"},
        follow_redirects=False,
    )


@pytest.fixture()
def admin_client(app):
    client = app.test_client()
    login(client)
    return client


@pytest.fixture()
def analyst_client(app):
    client = app.test_client()
    login(client, "analyst@example.test")
    return client


@pytest.fixture()
def viewer_client(app):
    client = app.test_client()
    login(client, "viewer@example.test")
    return client
