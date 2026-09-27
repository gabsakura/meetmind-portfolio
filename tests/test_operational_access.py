from __future__ import annotations

import hashlib

from app.extensions import db
from app.models import AuditEvent, Client, Meeting, MeetingParticipant, Role, Team, User

PASSWORD = "correct-horse-battery-staple"


def _login(app, email: str):
    client = app.test_client()
    response = client.post(
        "/auth/login", data={"email": email, "password": PASSWORD}
    )
    assert response.status_code == 302
    return client


def _meeting(organization_id: str, owner: User, creator: User, suffix: str) -> Meeting:
    client = Client(
        organization_id=organization_id,
        external_id=f"access-client-{suffix}",
        name=f"Cliente {suffix}",
        owner_id=owner.id,
    )
    db.session.add(client)
    db.session.flush()
    item = Meeting(
        organization_id=organization_id,
        external_id=f"access-meeting-{suffix}",
        title=f"Reunião {suffix}",
        client_id=client.id,
        transcript_hash=hashlib.sha256(suffix.encode()).hexdigest(),
        metadata_json={"nome_segmento": "MANUFATURA"},
        created_by_id=creator.id,
    )
    db.session.add(item)
    db.session.flush()
    return item


def test_manager_is_limited_to_managed_team_and_viewer_is_read_only(app):
    with app.app_context():
        admin = User.query.filter_by(email="admin@example.test").one()
        analyst = User.query.filter_by(email="analyst@example.test").one()
        viewer = User.query.filter_by(email="viewer@example.test").one()
        manager = User(
            organization_id=admin.organization_id,
            email="manager@example.test",
            name="Manager",
            role=Role.MANAGER,
        )
        manager.set_password(PASSWORD)
        outsider = User(
            organization_id=admin.organization_id,
            email="other-analyst@example.test",
            name="Other Analyst",
            role=Role.ANALYST,
        )
        outsider.set_password(PASSWORD)
        db.session.add_all([manager, outsider])
        db.session.flush()
        managed_team = Team(
            organization_id=admin.organization_id,
            name="Equipe gerenciada",
            manager_id=manager.id,
        )
        other_team = Team(
            organization_id=admin.organization_id,
            name="Outra equipe",
        )
        db.session.add_all([managed_team, other_team])
        db.session.flush()
        analyst.team_id = managed_team.id
        viewer.team_id = managed_team.id
        outsider.team_id = other_team.id
        visible = _meeting(admin.organization_id, analyst, analyst, "visível")
        hidden = _meeting(admin.organization_id, outsider, outsider, "restrita")
        db.session.add(
            MeetingParticipant(
                organization_id=admin.organization_id,
                meeting_id=visible.id,
                user_id=viewer.id,
                participation_role="viewer",
            )
        )
        db.session.commit()
        visible_id, hidden_id = visible.id, hidden.id

    manager_client = _login(app, "manager@example.test")
    listed = manager_client.get("/api/v1/meetings").json["items"]
    assert {item["id"] for item in listed} == {visible_id}
    assert manager_client.get(f"/api/v1/meetings/{hidden_id}").status_code == 404
    assert manager_client.get("/seguranca").status_code == 404

    viewer_client = _login(app, "viewer@example.test")
    assert viewer_client.get(f"/api/v1/meetings/{visible_id}").status_code == 200
    assert viewer_client.get(f"/api/v1/meetings/{hidden_id}").status_code == 404
    assert viewer_client.post(
        f"/api/v1/meetings/{visible_id}/participants",
        json={"user_id": "irrelevant"},
    ).status_code == 404


def test_admin_team_changes_are_audited(admin_client, app):
    with app.app_context():
        admin = User.query.filter_by(email="admin@example.test").one()
        manager = User(
            organization_id=admin.organization_id,
            email="team-manager@example.test",
            name="Team Manager",
            role=Role.MANAGER,
        )
        manager.set_password(PASSWORD)
        db.session.add(manager)
        db.session.commit()
        manager_id = manager.id
    response = admin_client.post(
        "/api/v1/admin/teams",
        json={"name": "Customer Success Manufatura", "manager_id": manager_id},
    )
    assert response.status_code == 201
    assert response.json["manager_id"] == manager_id
    with app.app_context():
        assert AuditEvent.query.filter_by(
            action="TEAM_CREATED", target_id=response.json["id"]
        ).count() == 1
