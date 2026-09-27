from __future__ import annotations

from app.extensions import db
from app.models import Analysis, AnalysisStatus, Meeting
from app.services.sector_context import build_context_snapshot


def payload(external_id="meeting-1"):
    return {
        "external_id": external_id,
        "title": "Reunião segura",
        "client_external_id": "client-1",
        "client_name": "Cliente anonimizado",
        "approved_for_external_ai": False,
        "segments": [
            {"id": f"{external_id}.0", "speaker": "Cliente", "text": "O sistema está ótimo."}
        ],
        "metadata": {"source": "manual", "tags": ["piloto"]},
    }


def test_authentication_required(client):
    assert client.get("/api/v1/meetings").status_code == 401


def test_admin_creates_and_lists_meeting(admin_client):
    response = admin_client.post("/api/v1/meetings", json=payload())
    assert response.status_code == 201
    listing = admin_client.get("/api/v1/meetings")
    assert listing.status_code == 200
    assert listing.json["total"] == 1


def test_viewer_cannot_create_meeting(viewer_client):
    response = viewer_client.post("/api/v1/meetings", json=payload())
    assert response.status_code == 404


def test_unknown_fields_are_rejected(admin_client):
    body = payload()
    body["unexpected"] = "blocked"
    response = admin_client.post("/api/v1/meetings", json=body)
    assert response.status_code == 422


def test_content_type_is_required(admin_client):
    response = admin_client.post("/api/v1/meetings", data="{}")
    assert response.status_code == 415


def test_malicious_html_remains_text(admin_client):
    body = payload()
    body["segments"][0]["text"] = "<img src=x onerror=alert(1)>"
    created = admin_client.post("/api/v1/meetings", json=body)
    detail = admin_client.get(f"/api/v1/meetings/{created.json['id']}")
    assert detail.json["segments"][0]["text"] == "<img src=x onerror=alert(1)>"


def test_analysis_does_not_require_individual_approval(admin_client):
    created = admin_client.post("/api/v1/meetings", json=payload())
    response = admin_client.post(f"/api/v1/meetings/{created.json['id']}/analyses", json={})
    assert response.status_code == 503
    assert response.json["code"] == "queue_disabled"


def test_new_analysis_requires_worker_when_queue_is_disabled(admin_client):
    body = payload()
    body["approved_for_external_ai"] = True
    created = admin_client.post("/api/v1/meetings", json=body)
    response = admin_client.post(f"/api/v1/meetings/{created.json['id']}/analyses", json={})
    assert response.status_code == 503
    assert response.json["code"] == "queue_disabled"


def test_cached_analysis_is_reused(admin_client, app):
    body = payload()
    body["approved_for_external_ai"] = True
    created = admin_client.post("/api/v1/meetings", json=body)
    with app.app_context():
        meeting = db.session.get(Meeting, created.json["id"])
        context = build_context_snapshot(meeting)
        analysis = Analysis(
            meeting_id=meeting.id,
            requested_by_id=meeting.created_by_id,
            status=AnalysisStatus.SUCCEEDED,
            model=app.config["GEMINI_MODEL"],
            prompt_version=app.config["PROMPT_VERSION"],
            score_version=app.config["SCORE_VERSION"],
            transcript_hash=meeting.transcript_hash,
            context_snapshot=context,
            context_version=context["context_version"],
            context_sha256=context["context_sha256"],
            mode_strategy="auto",
            requested_modes=[],
            mode_bundle_version=app.config["PROMPT_VERSION"],
            analysis_profile_hash="legacy",
            result={"summary": "cached"},
        )
        db.session.add(analysis)
        db.session.commit()
        analysis_id = analysis.id
        transcript_hash = meeting.transcript_hash
    response = admin_client.post(f"/api/v1/meetings/{created.json['id']}/analyses", json={})
    assert response.status_code == 200
    assert response.json == {
        "analysis_id": analysis_id,
        "cached": True,
        "status": "succeeded",
        "status_url": f"/api/v1/analyses/{analysis_id}",
    }
    detail = admin_client.get(response.json["status_url"])
    assert detail.status_code == 200
    assert detail.json["traceability"]["prompt_version"] == app.config["PROMPT_VERSION"]
    assert detail.json["traceability"]["transcript_hash"] == transcript_hash
    assert detail.json["traceability"]["context_sha256"] == context["context_sha256"]


def test_analysis_context_preview_is_reproducible(admin_client):
    created = admin_client.post("/api/v1/meetings", json=payload())
    first = admin_client.get(
        f"/api/v1/meetings/{created.json['id']}/analysis-context"
    )
    second = admin_client.get(
        f"/api/v1/meetings/{created.json['id']}/analysis-context"
    )
    assert first.status_code == 200
    assert first.json["context"]["playbook"]["id"] == "generico"
    assert first.json["context"]["context_sha256"] == second.json["context"]["context_sha256"]
    assert "meeting_type_rules" not in first.json["context"]
    assert "prohibited_inferences" not in first.json["context"]


def test_meeting_analysis_history_is_available_after_reload(admin_client, app):
    created = admin_client.post("/api/v1/meetings", json=payload())
    with app.app_context():
        meeting = db.session.get(Meeting, created.json["id"])
        analysis = Analysis(
            meeting_id=meeting.id,
            requested_by_id=meeting.created_by_id,
            status=AnalysisStatus.SUCCEEDED,
            model=app.config["GEMINI_MODEL"],
            prompt_version=app.config["PROMPT_VERSION"],
            score_version=app.config["SCORE_VERSION"],
            transcript_hash=meeting.transcript_hash,
            result={"summary": "persisted"},
        )
        db.session.add(analysis)
        db.session.commit()
        analysis_id = analysis.id

    response = admin_client.get(
        f"/api/v1/meetings/{created.json['id']}/analyses"
    )

    assert response.status_code == 200
    assert response.json["items"][0]["id"] == analysis_id
    assert response.json["items"][0]["status"] == "succeeded"
    assert response.json["items"][0]["status_url"] == (
        f"/api/v1/analyses/{analysis_id}"
    )


def test_auditability_keeps_instruction_only_when_there_are_no_meetings(admin_client):
    response = admin_client.get("/auditabilidade-ia")

    assert response.status_code == 200
    assert "Escolha uma reunião para abrir a trilha".encode() in response.data


def test_auditability_lists_only_successfully_analyzed_meetings(admin_client, app):
    analyzed = admin_client.post("/api/v1/meetings", json=payload("already-read"))
    admin_client.post("/api/v1/meetings", json=payload("not-read-yet"))
    with app.app_context():
        meeting = db.session.get(Meeting, analyzed.json["id"])
        db.session.add(
            Analysis(
                meeting_id=meeting.id,
                requested_by_id=meeting.created_by_id,
                status=AnalysisStatus.SUCCEEDED,
                model=app.config["GEMINI_MODEL"],
                prompt_version=app.config["PROMPT_VERSION"],
                score_version=app.config["SCORE_VERSION"],
                transcript_hash=meeting.transcript_hash,
                result={"summary": "persisted"},
            )
        )
        db.session.commit()

    response = admin_client.get("/auditabilidade-ia")

    assert response.status_code == 200
    assert b"already-read" in response.data
    assert "Escolha uma reunião para abrir a trilha".encode() not in response.data
    assert b"not-read-yet" not in response.data
    assert b"analysis-findings" in response.data
    assert b"local-trigger-events" in response.data
    assert b"analysis-traceability" in response.data
    assert b"audit-transcript" in response.data


def test_ai_auditability_is_restricted_and_legacy_route_redirects(
    admin_client, viewer_client
):
    denied = viewer_client.get("/auditabilidade-ia")
    legacy = admin_client.get("/analise", query_string={"meeting": "example"})
    assert denied.status_code == 404
    assert legacy.status_code == 302
    assert "/inteligencia-artificial?meeting=example" in legacy.headers["Location"]


def test_analysis_page_preselects_meeting_from_history_link(admin_client, app):
    created = admin_client.post("/api/v1/meetings", json=payload())
    with app.app_context():
        meeting = db.session.get(Meeting, created.json["id"])
        db.session.add(
            Analysis(
                meeting_id=meeting.id,
                requested_by_id=meeting.created_by_id,
                status=AnalysisStatus.SUCCEEDED,
                model=app.config["GEMINI_MODEL"],
                prompt_version=app.config["PROMPT_VERSION"],
                score_version=app.config["SCORE_VERSION"],
                transcript_hash=meeting.transcript_hash,
                result={"summary": "persisted"},
            )
        )
        db.session.commit()

    response = admin_client.get(
        "/auditabilidade-ia", query_string={"meeting": created.json["id"]}
    )

    assert response.status_code == 200
    selected = f'value="{created.json["id"]}"'.encode()
    assert selected in response.data
    assert b"selected" in response.data


def test_analysis_page_keeps_manufacturing_history(admin_client, app):
    manufacturing = admin_client.post(
        "/api/v1/meetings", json=payload("manufacturing-meeting")
    )
    other = admin_client.post("/api/v1/meetings", json=payload("health-meeting"))
    with app.app_context():
        manufacturing_meeting = db.session.get(Meeting, manufacturing.json["id"])
        other_meeting = db.session.get(Meeting, other.json["id"])
        manufacturing_meeting.metadata_json = {
            **(manufacturing_meeting.metadata_json or {}),
            "nome_segmento": "MANUFATURA",
        }
        other_meeting.metadata_json = {
            **(other_meeting.metadata_json or {}),
            "nome_segmento": "SAUDE",
        }
        db.session.commit()

    response = admin_client.get("/inteligencia-artificial")

    assert response.status_code == 200
    assert b"manufacturing-meeting" in response.data
    assert b"health-meeting" not in response.data
    assert "Selecione uma reunião".encode() in response.data


def test_empty_analysis_request_uses_single_call_legacy_profile(
    admin_client, app, monkeypatch
):
    body = payload("legacy-analysis")
    body["approved_for_external_ai"] = True
    created = admin_client.post("/api/v1/meetings", json=body)
    app.config["QUEUE_ENABLED"] = True
    monkeypatch.setattr(
        "app.blueprints.api.routes.dispatch_outbox_event", lambda _event_id: False
    )

    response = admin_client.post(
        f"/api/v1/meetings/{created.json['id']}/analyses", json={}
    )

    assert response.status_code == 202
    with app.app_context():
        analysis = db.session.get(Analysis, response.json["analysis_id"])
        assert analysis.prompt_version == "meetmind-manufacturing-v2"
        assert analysis.mode_bundle_version == "meetmind-manufacturing-v2"
        assert analysis.analysis_profile_hash == "legacy"
        assert analysis.requested_modes == []


def test_repeated_manual_request_reuses_analysis_in_progress(
    admin_client, app, monkeypatch
):
    body = payload("manual-analysis")
    body["approved_for_external_ai"] = True
    created = admin_client.post("/api/v1/meetings", json=body)
    app.config["QUEUE_ENABLED"] = True
    monkeypatch.setattr(
        "app.blueprints.api.routes.dispatch_outbox_event", lambda _event_id: False
    )
    endpoint = f"/api/v1/meetings/{created.json['id']}/analyses"

    first = admin_client.post(endpoint, json={})
    second = admin_client.post(endpoint, json={})

    assert first.status_code == 202
    assert second.status_code == 202
    assert second.json["analysis_id"] == first.json["analysis_id"]
    assert second.json["in_progress"] is True
    assert second.json["delivery"] == "existing"
    with app.app_context():
        analyses = Analysis.query.filter_by(meeting_id=created.json["id"]).all()
        assert len(analyses) == 1


def test_manufacturing_backfill_is_batched_and_idempotent(
    admin_client, app, monkeypatch
):
    body = payload("manufacturing-backfill")
    body["approved_for_external_ai"] = True
    created = admin_client.post("/api/v1/meetings", json=body)
    with app.app_context():
        meeting = db.session.get(Meeting, created.json["id"])
        meeting.metadata_json = {
            **(meeting.metadata_json or {}),
            "nome_segmento": "MANUFATURA",
        }
        db.session.commit()
    app.config["QUEUE_ENABLED"] = True
    monkeypatch.setattr("app.cli.dispatch_outbox_event", lambda _event_id: True)
    runner = app.test_cli_runner()

    first = runner.invoke(
        args=["backfill-manufacturing-ai", "--apply", "--batch-size", "1"]
    )
    second = runner.invoke(
        args=["backfill-manufacturing-ai", "--apply", "--batch-size", "1"]
    )

    assert first.exit_code == 0
    assert "Análises criadas: 1" in first.output
    assert second.exit_code == 0
    assert "Análises criadas: 0" in second.output
    with app.app_context():
        analyses = Analysis.query.filter_by(
            meeting_id=created.json["id"],
            prompt_version=app.config["PROMPT_VERSION"],
        ).all()
        assert len(analyses) == 1


def test_analyzed_meetings_page_shows_latest_traceable_result(admin_client, app):
    created = admin_client.post("/api/v1/meetings", json=payload())
    with app.app_context():
        meeting = db.session.get(Meeting, created.json["id"])
        analysis = Analysis(
            meeting_id=meeting.id,
            requested_by_id=meeting.created_by_id,
            status=AnalysisStatus.SUCCEEDED,
            model="gemini-test",
            prompt_version="meetmind-transparent-v3",
            score_version="voc-deterministic-v2",
            transcript_hash=meeting.transcript_hash,
            result={
                "executive_summary": {
                    "what_happened": "O cliente relatou uma falha.",
                    "why_it_matters": "A operação foi interrompida.",
                    "next_step": "Validar os logs.",
                },
                "overall_sentiment": "negative",
                "risk_level": "high",
                "products_mentioned": ["Protheus"],
                "findings": [
                    {
                        "category": "technical",
                        "title": "Falha operacional",
                        "observation": "O sistema travou.",
                        "interpretation": "Há impacto operacional.",
                        "business_impact": "A atividade parou.",
                        "recommendation": "Validar a causa.",
                        "confidence": 0.92,
                        "trigger": {
                            "trigger_type": "opportunity",
                            "label": "Acompanhamento técnico",
                            "reason": "O cliente pediu uma solução.",
                        },
                        "evidence": [
                            {
                                "segment_id": "meeting-1.0",
                                "quote": "O sistema está ótimo.",
                                "relevance": "Trecho verificado.",
                            }
                        ],
                    }
                ],
                "action_items": [
                    {
                        "action": "Validar logs",
                        "rationale": "Confirmar a causa.",
                        "urgency": "immediate",
                    }
                ],
                "uncertainties": ["A causa ainda não foi confirmada."],
                "traceability": {"evidence_count": 1, "format_repair_count": 0},
            },
        )
        db.session.add(analysis)
        db.session.commit()

    response = admin_client.get("/historico")

    assert response.status_code == 200
    assert "Reuniões analisadas".encode() in response.data
    assert b"O que aconteceu" in response.data
    assert "Acompanhamento técnico".encode() in response.data
    assert b'data-groups="all risk opportunity review"' in response.data
    assert b"data-history-details-button" in response.data
    assert b"meetmind-transparent-v3" in response.data


def test_payload_over_two_megabytes(admin_client):
    response = admin_client.post(
        "/api/v1/meetings",
        data=b"x" * (2 * 1024 * 1024 + 1),
        content_type="application/json",
    )
    assert response.status_code == 413


def test_transcript_character_limit(admin_client):
    body = payload()
    body["segments"] = None
    body["transcript"] = "a" * 250_001
    response = admin_client.post("/api/v1/meetings", json=body)
    assert response.status_code == 422


def test_segment_count_limit(admin_client):
    body = payload()
    body["segments"] = [
        {"id": f"segment-{index}", "text": "a"}
        for index in range(5_001)
    ]
    response = admin_client.post("/api/v1/meetings", json=body)
    assert response.status_code == 422


def test_search_uses_parameterized_query(admin_client):
    admin_client.post("/api/v1/meetings", json=payload())
    response = admin_client.get("/api/v1/meetings", query_string={"q": "%' OR 1=1 --"})
    assert response.status_code == 200
    assert response.json["total"] == 0
