from __future__ import annotations

from app.extensions import db
from app.models import (
    Analysis,
    AnalysisStatus,
    AuditEvent,
    KanbanRequest,
    Meeting,
    utcnow,
)
from app.services.actions import create_drafts


def _dashboard_action(admin_client, app) -> str:
    response = admin_client.post(
        "/api/v1/meetings",
        json={
            "external_id": "dashboard-command-meeting",
            "title": "Revisão de desempenho",
            "client_external_id": "dashboard-client",
            "client_name": "Cliente exemplo",
            "approved_for_external_ai": True,
            "segments": [{
                "id": "dashboard-command-meeting.0",
                "speaker": "Cliente",
                "text": "A lentidão continua e precisamos de um diagnóstico.",
            }],
            "metadata": {"source": "manual"},
        },
    )
    assert response.status_code == 201
    with app.app_context():
        meeting = db.session.get(Meeting, response.json["id"])
        analysis = Analysis(
            organization_id=meeting.organization_id,
            meeting_id=meeting.id,
            requested_by_id=meeting.created_by_id,
            status=AnalysisStatus.SUCCEEDED,
            model="gemini-test",
            provider="gemini_api",
            prompt_version="dashboard-test",
            score_version="dashboard-test",
            transcript_hash=meeting.transcript_hash,
            finished_at=utcnow(),
            result={
                "executive_summary": {
                    "what_happened": "O cliente relatou lentidão.",
                    "why_it_matters": "A operação está sendo afetada.",
                    "next_step": "Solicitar diagnóstico técnico.",
                },
                "risk_level": "high",
                "action_items": [{
                    "action": "Investigar a lentidão relatada",
                    "rationale": "O cliente confirmou impacto operacional.",
                    "urgency": "next_7_days",
                    "evidence_segment_ids": [meeting.segments[0].id],
                }],
            },
        )
        db.session.add(analysis)
        db.session.flush()
        action = create_drafts(analysis)[0]
        db.session.commit()
        return action.id


def test_dashboard_renders_command_shell_without_technical_chart(admin_client):
    response = admin_client.get("/")

    assert response.status_code == 200
    assert b"Seu dia" in response.data
    assert b'data-dashboard-region="briefing"' in response.data
    assert b'data-dashboard-region="daily-briefing"' in response.data
    assert b"Resumo do dia" in response.data
    assert b'data-dashboard-region="timeline"' in response.data
    assert b'data-dashboard-region="focus"' in response.data
    for stage in (b"priorizar", b"entender", b"agir", b"acompanhar"):
        assert b'data-command-stage="' + stage + b'"' in response.data
        assert b'data-jump-target="' + stage + b'"' in response.data
    assert b'role="tablist"' in response.data
    assert b"1 de 4" not in response.data
    assert b'data-command-stage="entender" hidden' in response.data
    assert b"coverage-chart" not in response.data
    assert b"dashboard-chart.js" not in response.data


def test_dashboard_regions_load_independently(admin_client, monkeypatch):
    monkeypatch.setattr(
        "app.blueprints.api.routes.events_for_user",
        lambda _user: [{
            "id": "microsoft-event-1",
            "type": "meeting",
            "title": "Reunião de acompanhamento",
            "starts_at": "2026-09-22T14:00:00+00:00",
            "ends_at": "2026-09-22T15:00:00+00:00",
            "detail": "Microsoft Teams",
            "critical": False,
            "source": "microsoft",
        }],
    )
    briefing = admin_client.get("/api/v1/dashboard/briefing")
    daily = admin_client.get("/api/v1/dashboard/daily-briefing")
    timeline = admin_client.get("/api/v1/dashboard/timeline")
    focus = admin_client.get("/api/v1/dashboard/focus")

    assert briefing.status_code == 200
    assert [item["tone"] for item in briefing.json["metrics"]] == [
        "risk", "attention", "success"
    ]
    assert daily.status_code == 200
    assert {item["type"] for item in daily.json["items"]} == {"meeting"}
    assert daily.json["items"][0]["source"] == "microsoft"
    assert timeline.status_code == 200
    assert timeline.json == {"items": []}
    assert focus.status_code == 200
    assert focus.json == {"items": []}


def test_focus_timeline_and_kanban_share_audited_source(admin_client, app):
    action_id = _dashboard_action(admin_client, app)

    focus = admin_client.get("/api/v1/dashboard/focus")
    timeline = admin_client.get("/api/v1/dashboard/timeline")
    sent = admin_client.post(
        "/api/v1/kanban/requests",
        json={
            "request_type": "performance_analysis",
            "source_action_id": action_id,
            "note": "Validar horários de pico.",
            "department": "Departamento adulterado",
        },
    )

    assert focus.status_code == 200
    assert focus.json["items"][0]["id"] == action_id
    assert focus.json["items"][0]["approach"] == "A operação está sendo afetada."
    assert any(item["title"] == "Sinal de risco encontrado" for item in timeline.json["items"])
    assert sent.status_code == 201
    assert sent.json["department"] == "Infraestrutura"
    with app.app_context():
        request_item = KanbanRequest.query.one()
        assert request_item.source_action_id == action_id
        assert request_item.department == "Infraestrutura"
        assert request_item.payload_json["department"] == "Infraestrutura"
        assert AuditEvent.query.filter_by(
            action="KANBAN_REQUEST_CREATED", target_id=request_item.id
        ).count() == 1


def test_kanban_rejects_invalid_type_and_viewer(viewer_client, admin_client, app):
    action_id = _dashboard_action(admin_client, app)
    invalid = admin_client.post(
        "/api/v1/kanban/requests",
        json={"request_type": "unknown", "source_action_id": action_id},
    )
    forbidden = viewer_client.post(
        "/api/v1/kanban/requests",
        json={"request_type": "product_bug", "source_action_id": action_id},
    )

    assert invalid.status_code == 422
    assert invalid.json["code"] == "invalid_request_type"
    assert forbidden.status_code == 404


def test_frontend_has_explicit_routing_dictionary():
    source = open("app/static/js/dashboard/index.js", encoding="utf-8").read()

    assert "const kanbanRouting = Object.freeze" in source
    for request_type in (
        "performance_analysis", "product_bug", "commercial_opportunity", "enablement_request"
    ):
        assert request_type in source


def test_daily_briefing_uses_backend_endpoint_without_frontend_framework():
    data_source = open("app/static/js/dashboard/data.js", encoding="utf-8").read()
    view_source = open("app/static/js/dashboard/views.js", encoding="utf-8").read()

    assert 'request("/api/v1/dashboard/daily-briefing")' in data_source
    assert "Dia inteiro" in view_source
    assert "Manhã" in view_source
    assert "Tarde" in view_source
    assert "Próximo" in view_source
    assert "renderDailyBriefing" in view_source
    assert "angular" not in data_source.lower()
