from __future__ import annotations

import json

from app.extensions import db
from app.models import (
    ActionEvent,
    ActionItem,
    Analysis,
    AnalysisAutopsyRun,
    AnalysisStatus,
    Meeting,
    MeetingParticipant,
    Role,
    User,
)
from app.services.sector_context import build_context_snapshot, context_for_ai


def _successful_analysis(admin_client, app, external_id: str = "feedback-flow") -> str:
    created = admin_client.post(
        "/api/v1/meetings",
        json={
            "external_id": external_id,
            "title": "Acompanhamento fabril",
            "client_external_id": f"client-{external_id}",
            "client_name": "Cliente industrial anonimizado",
            "approved_for_external_ai": True,
            "segments": [
                {
                    "id": f"{external_id}.0",
                    "speaker": "Cliente",
                    "text": "Precisamos validar o apontamento antes do próximo ciclo.",
                }
            ],
            "metadata": {"source": "manual"},
        },
    )
    assert created.status_code == 201
    with app.app_context():
        meeting = db.session.get(Meeting, created.json["id"])
        meeting.metadata_json = {
            "source": "manual",
            "nome_segmento": "MANUFATURA",
            "cnae": "10.91-1/02",
            "pais": "Brasil",
            "regiao_brasil": "Sul",
            "uf": "RS",
        }
        context = build_context_snapshot(meeting)
        segment_id = meeting.segments[0].id
        analysis = Analysis(
            organization_id=meeting.organization_id,
            meeting_id=meeting.id,
            requested_by_id=meeting.created_by_id,
            status=AnalysisStatus.SUCCEEDED,
            model="gemini-test",
            provider="gemini_api",
            prompt_version="meetmind-transparent-v4",
            score_version="voc-deterministic-v2",
            transcript_hash=meeting.transcript_hash,
            context_snapshot=context,
            context_version=context["context_version"],
            context_sha256=context["context_sha256"],
            result={
                "meeting_classification": {
                    "type": "relationship_success",
                    "evidence_segment_ids": [segment_id],
                },
                "relationship_health": {
                    "status": "attention",
                    "evidence_segment_ids": [segment_id],
                },
                "action_items": [
                    {
                        "action": "Validar o apontamento com o responsável",
                        "rationale": "A validação foi solicitada na reunião.",
                        "urgency": "next_7_days",
                        "evidence_segment_ids": [segment_id],
                    }
                ],
            },
        )
        db.session.add(analysis)
        db.session.commit()
        return analysis.id


def test_regionality_stays_in_snapshot_but_not_in_gemini_context(admin_client, app):
    analysis_id = _successful_analysis(admin_client, app, "regional-payload")
    with app.app_context():
        analysis = db.session.get(Analysis, analysis_id)
        assert analysis.context_snapshot["account"]["region"] == "Sul"
        payload = context_for_ai(analysis.context_snapshot)
        serialized = json.dumps(payload, ensure_ascii=False)

    assert "country" not in payload["account"]
    assert "region" not in payload["account"]
    assert "state" not in payload["account"]
    assert '"Sul"' not in serialized
    assert '"RS"' not in serialized


def test_backfill_actions_dry_run_apply_and_comments(admin_client, app):
    _successful_analysis(admin_client, app, "backfill-actions")
    runner = app.test_cli_runner()
    dry_run = runner.invoke(
        args=["backfill-analysis-actions", "--organization-slug", "meetmind-portfolio"]
    )
    with app.app_context():
        assert ActionItem.query.count() == 0
    applied = runner.invoke(
        args=[
            "backfill-analysis-actions",
            "--apply",
            "--organization-slug",
            "meetmind-portfolio",
        ]
    )
    action = admin_client.get("/api/v1/actions").json["items"][0]
    comment = admin_client.post(
        f"/api/v1/actions/{action['id']}/comments",
        json={"comment": "Confirmado com a equipe fabril.", "expected_version": action["version"]},
    )

    assert dry_run.exit_code == 0
    assert "Dry-run" in dry_run.output
    assert applied.exit_code == 0
    assert "Ações criadas: 1" in applied.output
    assert comment.status_code == 201
    with app.app_context():
        assert ActionEvent.query.filter_by(event_type="commented").count() == 1


def test_feedback_is_saved_without_evaluation_workflow(admin_client, app):
    analysis_id = _successful_analysis(admin_client, app, "simple-feedback")
    missing_correction = admin_client.post(
        f"/api/v1/analyses/{analysis_id}/feedback",
        json={
            "usefulness_rating": 3,
            "factual_accuracy": "minor_issues",
            "action_useful": False,
        },
    )
    saved = admin_client.post(
        f"/api/v1/analyses/{analysis_id}/feedback",
        json={
            "usefulness_rating": 4,
            "factual_accuracy": "minor_issues",
            "action_useful": True,
            "correction": "Manter a saúde como atenção até confirmar o responsável.",
        },
    )
    current = admin_client.get(f"/api/v1/analyses/{analysis_id}/feedback")

    assert missing_correction.status_code == 422
    assert saved.status_code == 200
    assert current.status_code == 200
    assert current.json["feedback"]["correction"].startswith("Manter")
    assert "status" not in current.json["feedback"]


def test_evaluation_center_route_was_removed(analyst_client):
    assert analyst_client.get("/avaliacoes-ia").status_code == 404


def test_autopsy_comparison_remains_restricted(admin_client, analyst_client, viewer_client, app):
    analysis_id = _successful_analysis(admin_client, app, "autopsy-comparison")
    with app.app_context():
        analysis = db.session.get(Analysis, analysis_id)
        analyst = User.query.filter_by(role=Role.ANALYST).one()
        db.session.add(
            MeetingParticipant(
                organization_id=analysis.organization_id,
                meeting_id=analysis.meeting_id,
                user_id=analyst.id,
                participation_role="participant",
            )
        )
        run = AnalysisAutopsyRun(
            organization_id=analysis.organization_id,
            analysis_id=analysis.id,
            mode="shadow",
            status="succeeded",
            bundle_version="meetmind-autopsy-manufacturing-v1",
            bundle_sha256="a" * 64,
            source_references=[],
            path_id="objective_unclear",
            questions=[{"id": "objective", "text": "Qual era o objetivo?"}],
            quality_flags=[],
            baseline_result=analysis.result,
            guided_review={
                "objective": {"status": "unclear"},
                "follow_up": {"status": "proposed"},
                "conflict": {"status": "none_detected"},
                "customization": {"status": "absent"},
                "traceability": {"evidence_count": 0},
            },
            assisted_result=analysis.result,
            disagreements=[],
        )
        db.session.add(run)
        db.session.commit()

    forbidden = viewer_client.get(f"/api/v1/analyses/{analysis_id}/autopsy-comparison")
    comparison = analyst_client.get(f"/api/v1/analyses/{analysis_id}/autopsy-comparison")

    assert forbidden.status_code == 404
    assert comparison.status_code == 200
    assert comparison.json["autopsy"]["mode"] == "shadow"


def test_autopsy_second_stage_failure_preserves_baseline(admin_client, app, monkeypatch):
    from app.jobs import _run_autopsy_assistant

    analysis_id = _successful_analysis(admin_client, app, "autopsy-fallback")
    with app.app_context():
        analysis = db.session.get(Analysis, analysis_id)
        baseline = json.loads(json.dumps(analysis.result))
        app.config["AUTOPSY_ASSISTANT_MODE"] = "shadow"
        monkeypatch.setattr(
            "app.jobs.review_transcript_with_autopsy",
            lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("provider down")),
        )

        run = _run_autopsy_assistant(
            analysis,
            "[SEGMENT_ID=test] Cliente: reunião sem conclusão.",
            analysis.result,
        )
        db.session.commit()

        assert run is not None
        assert run.status == "failed"
        assert run.error_code.startswith("RuntimeError")
        assert analysis.result == baseline
