from app.extensions import db
from app.models import CalendarConnection, Meeting, Organization, Role, User
from app.services.demo_seed import DEMO_ORGANIZATION_SLUG, reset_demo, seed_demo
from app.tenancy import tenant_scope

PASSWORD = "portfolio-demo-password"


def _login(client, email):
    return client.post(
        "/auth/login",
        data={"email": email, "password": PASSWORD},
        follow_redirects=False,
    )


def test_demo_seed_is_idempotent_and_scopes_meetings(app):
    with app.app_context():
        first = seed_demo(password=PASSWORD)
        second = seed_demo(password=PASSWORD)
        organization = Organization.query.filter_by(slug=DEMO_ORGANIZATION_SLUG).one()
        with tenant_scope(organization.id):
            ana_meeting = Meeting.query.filter_by(external_id="demo-atlas-risco").one()
            assert Meeting.query.count() == 6
        assert first["meetings_created"] == 6
        assert second["meetings_created"] == 0

    ana = app.test_client()
    assert _login(ana, "ana@meetmind.example").status_code == 302
    visible = ana.get("/api/v1/meetings").get_json()
    assert visible["total"] == 3
    assert {item["external_id"] for item in visible["items"]} == {
        "demo-atlas-risco",
        "demo-horizonte-expansao",
        "demo-nova-era-followup",
    }
    assert ana.get(f"/api/v1/meetings/{ana_meeting.id}").status_code == 200
    assert ana.get("/seguranca").status_code == 404

    bruno = app.test_client()
    assert _login(bruno, "bruno@meetmind.example").status_code == 302
    assert bruno.get(f"/api/v1/meetings/{ana_meeting.id}").status_code == 404
    assert bruno.get("/api/v1/meetings").get_json()["total"] == 2


def test_demo_seed_enables_external_ai_only_for_portfolio_chat(app):
    app.config["PORTFOLIO_CHAT_ENABLED"] = True
    with app.app_context():
        seed_demo(password=PASSWORD)
        organization = Organization.query.filter_by(slug=DEMO_ORGANIZATION_SLUG).one()
        assert organization.external_ai_enabled is True

        app.config["PORTFOLIO_CHAT_ENABLED"] = False
        seed_demo(password=PASSWORD)
        db.session.refresh(organization)
        assert organization.external_ai_enabled is False


def test_production_portfolio_login_finds_demo_tenant_and_handles_invalid_account(app):
    with app.app_context():
        seed_demo(password=PASSWORD)
    app.config.update(APP_ENV="production", PORTFOLIO_DEMO_ENABLED=True, AUTH_MODE="local")

    valid = app.test_client()
    assert _login(valid, "ana@meetmind.example").status_code == 302

    invalid = app.test_client()
    response = _login(invalid, "meetmind.demo.ana@gmail.com")
    assert response.status_code == 200
    assert "E-mail ou senha inválidos." in response.get_data(as_text=True)
    assert "ana@meetmind.example" in response.get_data(as_text=True)


def test_demo_has_no_admin_and_manager_cannot_open_security(app):
    with app.app_context():
        seed_demo(password=PASSWORD)
        organization = Organization.query.filter_by(slug=DEMO_ORGANIZATION_SLUG).one()
        assert User.query.filter_by(
            organization_id=organization.id,
            role=Role.ADMIN,
        ).count() == 0
    client = app.test_client()
    assert _login(client, "marina@meetmind.example").status_code == 302
    assert client.get("/seguranca").status_code == 404


def test_seed_deactivates_legacy_demo_admin(app):
    with app.app_context():
        seed_demo(password=PASSWORD)
        organization = Organization.query.filter_by(slug=DEMO_ORGANIZATION_SLUG).one()
        legacy = User(
            organization_id=organization.id,
            email="admin@meetmind.example",
            name="Legacy Admin",
            role=Role.ADMIN,
        )
        legacy.set_password(PASSWORD)
        db.session.add(legacy)
        db.session.commit()

        seed_demo(password=PASSWORD)
        db.session.refresh(legacy)
        assert legacy.role == Role.VIEWER
        assert legacy.is_active_record is False
        assert legacy.check_password(PASSWORD) is False


def test_demo_reset_rebuilds_only_synthetic_organization(app):
    with app.app_context():
        seed_demo(password=PASSWORD)
        default_count = User.query.filter_by(email="admin@example.test").count()
        organization = Organization.query.filter_by(slug=DEMO_ORGANIZATION_SLUG).one()
        demo_user = User.query.filter_by(
            organization_id=organization.id,
            email="ana@meetmind.example",
        ).one()
        default_user = User.query.filter_by(email="admin@example.test").one()
        db.session.add_all(
            [
                CalendarConnection(
                    organization_id=organization.id,
                    user_id=demo_user.id,
                    provider="google",
                    account_email="demo-personal@example.test",
                    token_ciphertext=b"encrypted-demo-token",
                    scopes="calendar.read",
                ),
                CalendarConnection(
                    organization_id=default_user.organization_id,
                    user_id=default_user.id,
                    provider="microsoft",
                    account_email="preserved@example.test",
                    token_ciphertext=b"encrypted-default-token",
                    scopes="calendar.read",
                ),
            ]
        )
        db.session.commit()
        report = reset_demo(password=PASSWORD)
        with tenant_scope(organization.id):
            assert Meeting.query.count() == 6
            assert CalendarConnection.query.count() == 0
        with tenant_scope(default_user.organization_id):
            assert CalendarConnection.query.count() == 1
        assert report["meetings_removed"] == 6
        assert report["meetings_created"] == 6
        assert report["calendar_connections_removed"] == 1
        assert default_count == 1
        assert User.query.filter_by(email="admin@example.test").count() == 1


def test_demo_blocks_upload_calendar_and_profile_changes(app):
    with app.app_context():
        seed_demo(password=PASSWORD)
        organization = Organization.query.filter_by(slug=DEMO_ORGANIZATION_SLUG).one()
        with tenant_scope(organization.id):
            assert all(
                meeting.approved_for_external_ai is False
                for meeting in Meeting.query.all()
            )

    client = app.test_client()
    assert _login(client, "ana@meetmind.example").status_code == 302
    upload = client.post("/api/v1/submissions")
    assert upload.status_code == 403
    assert upload.get_json()["code"] == "demo_read_only"
    assert client.get("/perfil/calendario/conectar/google").status_code == 403
    assert client.post("/perfil/foto").status_code == 403


def test_privacy_and_terms_are_public(client):
    privacy = client.get("/auth/privacidade")
    terms = client.get("/auth/termos")
    assert privacy.status_code == 200
    assert "Política de Privacidade" in privacy.get_data(as_text=True)
    assert "privacy@example.test" in privacy.get_data(as_text=True)
    assert terms.status_code == 200
    assert "demonstração acadêmica independente" in terms.get_data(as_text=True).lower()
