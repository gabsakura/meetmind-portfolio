from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.extensions import db
from app.models import (
    ActionEvent,
    ActionItem,
    Analysis,
    AnalysisStatus,
    Client,
    ClientContact,
    CommunicationDraft,
    CommunicationRevision,
    KanbanRequest,
    Meeting,
    Role,
    User,
    utcnow,
)
from app.services.actions import create_drafts


def _action(admin_client, app, suffix: str = "cs-workflow") -> tuple[str, str]:
    response = admin_client.post(
        "/api/v1/meetings",
        json={
            "external_id": f"{suffix}-meeting",
            "title": "Revisão do cliente",
            "client_external_id": f"{suffix}-client",
            "client_name": "Cliente Fluxo CS",
            "approved_for_external_ai": True,
            "segments": [{
                "id": f"{suffix}-meeting.0",
                "speaker": "Cliente",
                "text": "Precisamos corrigir a lentidão e acompanhar na próxima semana.",
            }],
            "metadata": {"source": "manual"},
        },
    )
    assert response.status_code == 201
    with app.app_context():
        meeting = db.session.get(Meeting, response.json["id"])
        owner = User.query.filter_by(role=Role.ADMIN).first()
        meeting.client.owner_id = owner.id
        analysis = Analysis(
            organization_id=meeting.organization_id,
            meeting_id=meeting.id,
            requested_by_id=owner.id,
            status=AnalysisStatus.SUCCEEDED,
            model="gemini-test",
            provider="gemini_api",
            prompt_version="cs-workflow-test",
            score_version="cs-workflow-test",
            transcript_hash=meeting.transcript_hash,
            finished_at=utcnow(),
            result={
                "executive_summary": {
                    "what_happened": "O cliente relatou lentidão.",
                    "why_it_matters": "Existe impacto operacional.",
                    "next_step": "Investigar e acompanhar.",
                },
                "relationship_health": {"status": "at_risk"},
                "risk_level": "high",
                "action_items": [{
                    "action": "Investigar lentidão",
                    "rationale": "O cliente confirmou impacto.",
                    "urgency": "next_7_days",
                    "evidence_segment_ids": [meeting.segments[0].id],
                }],
            },
        )
        db.session.add(analysis)
        db.session.flush()
        action = create_drafts(analysis)[0]
        db.session.commit()
        return action.id, meeting.client_id


def test_client_portfolio_and_360_use_existing_records(admin_client, app):
    _, client_id = _action(admin_client, app, "client-360")

    portfolio = admin_client.get("/clientes")
    detail = admin_client.get(f"/clientes/{client_id}")

    assert portfolio.status_code == 200
    assert b"Cliente Fluxo CS" in portfolio.data
    assert detail.status_code == 200
    assert b"Pend\xc3\xaancias abertas" in detail.data
    assert b"O cliente relatou lentid\xc3\xa3o" in detail.data


def test_analyst_actions_page_renders_without_duplicate_client_join(
    admin_client, analyst_client, app
):
    with app.app_context():
        analyst = User.query.filter_by(email="analyst@example.test").one()
        analyst_id = analyst.id
    _, client_id = _action(admin_client, app, "analyst-actions-page")
    with app.app_context():
        client = db.session.get(Client, client_id)
        client.owner_id = analyst_id
        db.session.commit()

    response = analyst_client.get("/acoes")

    assert response.status_code == 200
    assert b"Investigar lentid\xc3\xa3o" in response.data


def test_completion_can_create_linked_follow_up(admin_client, app):
    action_id, _ = _action(admin_client, app, "completion")
    with app.app_context():
        owner = User.query.filter_by(role=Role.ADMIN).first()
        owner_id = owner.id
        action = db.session.get(ActionItem, action_id)
        version = action.version
    accepted = admin_client.post(
        f"/api/v1/actions/{action_id}/decision",
        json={
            "decision": "accept",
            "owner_id": owner_id,
            "due_at": (datetime.now(UTC) + timedelta(days=2)).isoformat(),
            "expected_version": version,
        },
    )
    assert accepted.status_code == 200

    completed = admin_client.patch(
        f"/api/v1/actions/{action_id}",
        json={
            "status": "completed",
            "outcome": "Correção validada com o cliente.",
            "customer_notified": True,
            "expected_version": accepted.json["version"],
            "follow_up": {
                "title": "Confirmar estabilidade",
                "owner_id": owner_id,
                "due_at": (datetime.now(UTC) + timedelta(days=7)).isoformat(),
            },
        },
    )

    assert completed.status_code == 200
    assert completed.json["customer_notified"] is True
    assert completed.json["follow_up"]["parent_action_id"] == action_id
    assert completed.json["follow_up"]["status"] == "accepted"


def test_routing_response_reaches_personal_briefing_and_can_be_seen(admin_client, app):
    action_id, _ = _action(admin_client, app, "routing")
    with app.app_context():
        owner_id = User.query.filter_by(role=Role.ADMIN).first().id
    created = admin_client.post(
        "/api/v1/kanban/requests",
        json={
            "request_type": "performance_analysis",
            "source_action_id": action_id,
            "note": "Validar os horários de pico.",
        },
    )
    assert created.status_code == 201
    started = admin_client.patch(
        f"/api/v1/kanban/requests/{created.json['id']}",
        json={
            "expected_version": created.json["version"],
            "status": "in_progress",
            "assigned_to_id": owner_id,
            "due_at": (datetime.now(UTC) + timedelta(days=2)).isoformat(),
        },
    )
    assert started.status_code == 200
    answered = admin_client.patch(
        f"/api/v1/kanban/requests/{created.json['id']}",
        json={
            "expected_version": started.json["version"],
            "status": "answered",
            "assigned_to_id": owner_id,
            "due_at": started.json["due_at"],
            "response": "Índice revisado e consumo estabilizado.",
        },
    )
    assert answered.status_code == 200
    assert answered.json["status"] == "answered"
    routing_page = admin_client.get("/encaminhamentos")
    assert routing_page.status_code == 200
    assert b"\xc3\x8dndice revisado e consumo estabilizado" in routing_page.data
    listed = admin_client.get("/api/v1/kanban/requests")
    assert listed.status_code == 200
    assert listed.json["items"][0]["id"] == created.json["id"]

    briefing = admin_client.get("/api/v1/dashboard/briefing")
    assert any(item["id"] == f"kanban-{created.json['id']}" for item in briefing.json["updates"])
    seen = admin_client.post("/api/v1/dashboard/seen")
    assert seen.status_code == 200
    assert admin_client.get("/api/v1/dashboard/briefing").json["updates"] == []
    with app.app_context():
        item = db.session.get(KanbanRequest, created.json["id"])
        assert item.response == "Índice revisado e consumo estabilizado."
        assert ActionEvent.query.filter_by(event_type="routing_answered").count() == 1


def test_customer_communication_is_versioned_approved_and_auditable(admin_client, app):
    action_id, client_id = _action(admin_client, app, "communication")
    with app.app_context():
        owner_id = User.query.filter_by(role=Role.ADMIN).first().id
        contact = ClientContact(
            organization_id=User.query.filter_by(role=Role.ADMIN).first().organization_id,
            client_id=client_id,
            name="Contato Cliente",
            email="contato@cliente.example",
            is_primary=True,
        )
        db.session.add(contact)
        db.session.commit()
        contact_id = contact.id
    routed = admin_client.post(
        "/api/v1/kanban/requests",
        json={"request_type": "product_bug", "source_action_id": action_id},
    ).json
    started = admin_client.patch(
        f"/api/v1/kanban/requests/{routed['id']}",
        json={
            "expected_version": routed["version"],
            "status": "in_progress",
            "assigned_to_id": owner_id,
            "due_at": (datetime.now(UTC) + timedelta(days=2)).isoformat(),
        },
    ).json
    answered = admin_client.patch(
        f"/api/v1/kanban/requests/{routed['id']}",
        json={
            "expected_version": started["version"],
            "status": "answered",
            "assigned_to_id": owner_id,
            "due_at": started["due_at"],
            "response": "Correção aplicada e validada pelo setor.",
        },
    ).json
    created = admin_client.post(
        "/api/v1/communication-drafts",
        json={
            "client_id": client_id,
            "action_id": action_id,
            "request_id": routed["id"],
            "draft_type": "customer_message",
            "channel": "email",
            "recipient_contact_ids": [contact_id],
            "subject": "Atualização do atendimento",
            "body": "A correção foi aplicada. Podemos confirmar a estabilidade?",
        },
    )
    assert created.status_code == 201
    revised = admin_client.patch(
        f"/api/v1/communication-drafts/{created.json['id']}",
        json={
            "version": created.json["version"],
            "recipient_contact_ids": [contact_id],
            "subject": "Atualização do atendimento",
            "body": "A correção foi aplicada. Por favor, confirme a estabilidade.",
        },
    )
    assert revised.status_code == 200
    assert revised.json["current_revision"] == 2
    approved = admin_client.post(
        f"/api/v1/communication-drafts/{created.json['id']}/approve",
        json={"version": revised.json["version"]},
    )
    assert approved.status_code == 200
    sent = admin_client.post(
        f"/api/v1/communication-drafts/{created.json['id']}/confirm-send",
        json={"version": approved.json["version"], "channel": "email"},
    )
    assert sent.status_code == 200
    assert sent.json["status"] == "sent_confirmed"
    returned = admin_client.post(
        f"/api/v1/kanban/requests/{routed['id']}/customer-return",
        json={"version": answered["version"], "returned": True},
    )
    assert returned.status_code == 200
    closed = admin_client.patch(
        f"/api/v1/kanban/requests/{routed['id']}",
        json={
            "expected_version": returned.json["version"],
            "status": "closed",
            "assigned_to_id": owner_id,
            "due_at": started["due_at"],
            "response": answered["response"],
        },
    )
    assert closed.status_code == 200
    with app.app_context():
        draft = db.session.get(CommunicationDraft, created.json["id"])
        assert draft.sent_at is not None
        assert CommunicationRevision.query.filter_by(
            communication_draft_id=draft.id
        ).count() == 2


def test_external_customer_request_stays_outside_the_meeting_analysis(
    admin_client, app
):
    _, client_id = _action(admin_client, app, "external-request")
    created = admin_client.post(
        "/api/v1/kanban/requests",
        json={
            "request_type": "product_bug",
            "client_id": client_id,
            "source_channel": "teams",
            "note": "Cliente informou erro ao emitir a ordem fora da reunião.",
        },
    )

    assert created.status_code == 201
    assert created.json["source_kind"] == "external"
    assert created.json["source_channel"] == "teams"
    assert created.json["source_action_id"] is None
    assert created.json["meeting_id"] is None
    assert created.json["analysis_id"] is None
    assert created.json["due_at"] is not None
    assert "Cliente: Cliente Fluxo CS" in created.json["message"]
    assert "Prazo esperado:" in created.json["message"]
    page = admin_client.get("/encaminhamentos")
    assert b"Registrar pedido recebido fora de reuni" in page.data
    with app.app_context():
        item = db.session.get(KanbanRequest, created.json["id"])
        assert item.payload_json["source_kind"] == "external"

def test_routing_requires_assignment_and_response(admin_client, app):
    action_id, _ = _action(admin_client, app, "routing-validation")
    created = admin_client.post(
        "/api/v1/kanban/requests",
        json={"request_type": "product_bug", "source_action_id": action_id},
    )
    missing_assignment = admin_client.patch(
        f"/api/v1/kanban/requests/{created.json['id']}",
        json={"expected_version": created.json["version"], "status": "in_progress"},
    )

    assert missing_assignment.status_code == 422
    assert missing_assignment.json["code"] == "assignment_required"
