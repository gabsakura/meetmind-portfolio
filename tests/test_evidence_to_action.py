from __future__ import annotations

from datetime import timedelta

import pytest

from app.extensions import db
from app.models import (
    ActionEvent,
    ActionItem,
    ActionStatus,
    Analysis,
    AnalysisStatus,
    EvidenceIssue,
    Meeting,
    MeetingPlan,
    MeetingSubmission,
    Organization,
    OutboxEvent,
    PrivacyExecution,
    PrivacyRequest,
    TeamsConnection,
    TeamsTranscriptReceipt,
    User,
    utcnow,
)
from app.services.actions import create_drafts


def _meeting(admin_client, external_id: str = "evidence-action") -> str:
    response = admin_client.post(
        "/api/v1/meetings",
        json={
            "external_id": external_id,
            "title": "Reunião segura",
            "client_external_id": "client-evidence",
            "client_name": "Cliente anonimizado",
            "approved_for_external_ai": True,
            "segments": [
                {
                    "id": f"{external_id}.0",
                    "speaker": "Cliente",
                    "text": "O treinamento precisa de acompanhamento.",
                }
            ],
            "metadata": {"source": "manual", "tags": ["piloto"]},
        },
    )
    assert response.status_code == 201
    return response.json["id"]


def _analysis_with_drafts(app, meeting_id: str) -> tuple[str, list[str]]:
    with app.app_context():
        meeting = db.session.get(Meeting, meeting_id)
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
            result={
                "action_items": [
                    {
                        "action": "Confirmar responsáveis pelo treinamento",
                        "rationale": "A reunião definiu treinamento como próximo passo.",
                        "urgency": "next_7_days",
                        "evidence_segment_ids": [meeting.segments[0].id],
                    },
                    {
                        "action": "Registrar o critério de aceite",
                        "rationale": "O aceite ainda precisa ser documentado.",
                        "urgency": "next_30_days",
                        "evidence_segment_ids": [meeting.segments[0].id],
                    },
                ]
            },
        )
        db.session.add(analysis)
        db.session.flush()
        first = create_drafts(analysis)
        second = create_drafts(analysis)
        db.session.commit()
        return analysis.id, [item.id for item in first + second]


def test_analysis_actions_are_idempotent_and_versioned(admin_client, app):
    analysis_id, ids = _analysis_with_drafts(app, _meeting(admin_client))

    assert ids[:2] == ids[2:]
    listing = admin_client.get(f"/api/v1/actions?client_id={listing_client_id(app)}")
    assert listing.status_code == 200
    assert len(listing.json["items"]) == 2
    action = listing.json["items"][0]
    accepted = admin_client.post(
        f"/api/v1/actions/{action['id']}/decision",
        json={
            "decision": "accept",
            "owner_id": admin_id(app),
            "due_at": "2026-09-01",
            "expected_version": action["version"],
        },
    )
    stale = admin_client.patch(
        f"/api/v1/actions/{action['id']}",
        json={"status": "in_progress", "expected_version": action["version"]},
    )

    assert analysis_id
    assert accepted.status_code == 200
    assert accepted.json["status"] == ActionStatus.ACCEPTED.value
    assert accepted.json["due_at"].startswith("2026-09-01")
    assert stale.status_code == 409
    assert stale.json["code"] == "version_conflict"


def test_single_suggestion_is_added_to_kanban_idempotently(admin_client, app):
    meeting_id = _meeting(admin_client, "single-kanban-action")
    with app.app_context():
        meeting = db.session.get(Meeting, meeting_id)
        analysis = Analysis(
            organization_id=meeting.organization_id,
            meeting_id=meeting.id,
            requested_by_id=meeting.created_by_id,
            status=AnalysisStatus.SUCCEEDED,
            model="gemini-test",
            provider="gemini_api",
            prompt_version="meetmind-manufacturing-v1",
            score_version="voc-deterministic-v2",
            transcript_hash=meeting.transcript_hash,
            result={
                "action_items": [
                    {
                        "action": "Confirmar responsáveis",
                        "rationale": "O cliente pediu acompanhamento.",
                        "urgency": "next_7_days",
                        "evidence_segment_ids": [meeting.segments[0].id],
                    },
                    {
                        "action": "Documentar o aceite",
                        "rationale": "O critério ainda está aberto.",
                        "urgency": "next_30_days",
                        "evidence_segment_ids": [meeting.segments[0].id],
                    },
                ]
            },
        )
        db.session.add(analysis)
        db.session.commit()
        analysis_id = analysis.id

    first = admin_client.post(f"/api/v1/analyses/{analysis_id}/actions/1")
    second = admin_client.post(f"/api/v1/analyses/{analysis_id}/actions/1")

    assert first.status_code == 201
    assert first.json["created"] is True
    assert second.status_code == 200
    assert second.json["created"] is False
    assert first.json["source_index"] == 1
    assert first.json["id"] == second.json["id"]
    with app.app_context():
        assert ActionItem.query.filter_by(analysis_id=analysis_id).count() == 2
        assert MeetingPlan.query.filter_by(analysis_id=analysis_id).count() == 1


def test_plan_is_not_created_without_valid_evidence(admin_client, app):
    meeting_id = _meeting(admin_client, "no-actionable-evidence")
    with app.app_context():
        meeting = db.session.get(Meeting, meeting_id)
        analysis = Analysis(
            organization_id=meeting.organization_id,
            meeting_id=meeting.id,
            requested_by_id=meeting.created_by_id,
            status=AnalysisStatus.SUCCEEDED,
            model="gemini-test",
            provider="gemini_api",
            prompt_version="meetmind-manufacturing-v1",
            score_version="voc-deterministic-v2",
            transcript_hash=meeting.transcript_hash,
            result={
                "executive_summary": {
                    "what_happened": "Sem compromisso confirmado.",
                    "why_it_matters": "Não há ação segura.",
                    "next_step": "Acompanhar.",
                },
                "action_items": [
                    {
                        "action": "Inventar um acompanhamento",
                        "rationale": "Sem origem literal.",
                        "urgency": "monitor",
                        "evidence_segment_ids": ["segmento-inventado"],
                    }
                ],
            },
        )
        db.session.add(analysis)
        db.session.flush()
        assert create_drafts(analysis) == []
        assert MeetingPlan.query.filter_by(analysis_id=analysis.id).first() is None


def test_bulk_confirmation_requires_owner_and_deadline(admin_client, app):
    analysis_id, _ = _analysis_with_drafts(
        app, _meeting(admin_client, "bulk-action-review")
    )
    detail = admin_client.get(f"/api/v1/analyses/{analysis_id}").json
    missing = admin_client.post(
        f"/api/v1/analyses/{analysis_id}/actions/confirm-all",
        json={
            "actions": [
                {
                    "id": action["id"],
                    "expected_version": action["version"],
                    "title": action["title"],
                }
                for action in detail["actions"]
            ]
        },
    )
    confirmed = admin_client.post(
        f"/api/v1/analyses/{analysis_id}/actions/confirm-all",
        json={
            "actions": [
                {
                    "id": action["id"],
                    "expected_version": action["version"],
                    "title": action["title"],
                    "owner_id": admin_id(app),
                    "due_at": "2026-09-30",
                }
                for action in detail["actions"]
            ]
        },
    )

    assert missing.status_code == 422
    assert confirmed.status_code == 200
    assert {item["status"] for item in confirmed.json["actions"]} == {"accepted"}
    assert confirmed.json["plan"]["status"] == "active"


def test_evidence_issue_is_linked_to_analysis_segment(admin_client, app):
    analysis_id, _ = _analysis_with_drafts(
        app, _meeting(admin_client, "evidence-issue")
    )
    detail = admin_client.get(f"/api/v1/analyses/{analysis_id}").json
    segment_id = detail["actions"][0]["evidence_segment_ids"][0]
    response = admin_client.post(
        f"/api/v1/analyses/{analysis_id}/evidence-issues",
        json={
            "segment_id": segment_id,
            "category": "out_of_context",
            "comment": "O trecho está correto, mas não sustenta esta interpretação.",
        },
    )

    assert response.status_code == 201
    with app.app_context():
        issue = EvidenceIssue.query.one()
        assert issue.analysis_id == analysis_id
        assert issue.segment_id == segment_id


def test_reprocessing_does_not_change_reviewed_actions(admin_client, app):
    analysis_id, _ = _analysis_with_drafts(
        app, _meeting(admin_client, "preserve-reviewed-action")
    )
    action = admin_client.get("/api/v1/actions").json["items"][0]
    accepted = admin_client.post(
        f"/api/v1/actions/{action['id']}/decision",
        json={
            "decision": "accept",
            "owner_id": admin_id(app),
            "due_at": "2026-10-01",
            "expected_version": action["version"],
        },
    )
    assert accepted.status_code == 200
    original_title = accepted.json["title"]

    with app.app_context():
        analysis = db.session.get(Analysis, analysis_id)
        updated_result = dict(analysis.result)
        updated_actions = [dict(item) for item in updated_result["action_items"]]
        updated_actions[0]["action"] = "Título alterado no reprocessamento"
        updated_result["action_items"] = updated_actions
        analysis.result = updated_result
        create_drafts(analysis)
        db.session.commit()
        preserved = db.session.get(ActionItem, action["id"])
        assert preserved.title == original_title
        assert preserved.status == ActionStatus.ACCEPTED.value
        assert ActionItem.query.filter_by(analysis_id=analysis_id).count() == 2


def listing_client_id(app) -> str:
    with app.app_context():
        return ActionItem.query.first().client_id


def admin_id(app) -> str:
    with app.app_context():
        return User.query.filter_by(email="admin@example.test").one().id


def test_action_dismissal_and_completion_require_explanations(admin_client, app):
    _, _ = _analysis_with_drafts(app, _meeting(admin_client, "action-rules"))
    action = admin_client.get("/api/v1/actions").json["items"][0]

    rejected = admin_client.post(
        f"/api/v1/actions/{action['id']}/decision",
        json={"decision": "dismiss", "reason": "não", "expected_version": action["version"]},
    )
    accepted = admin_client.post(
        f"/api/v1/actions/{action['id']}/decision",
        json={
            "decision": "accept",
            "owner_id": admin_id(app),
            "due_at": "2026-09-01",
            "expected_version": action["version"],
        },
    )
    incomplete = admin_client.patch(
        f"/api/v1/actions/{action['id']}",
        json={"status": "completed", "expected_version": accepted.json["version"]},
    )

    assert rejected.status_code == 422
    assert incomplete.status_code == 422
    assert incomplete.json["code"] == "outcome_required"


def test_completion_can_schedule_an_authorized_follow_up(
    admin_client, app, monkeypatch
):
    _analysis_with_drafts(app, _meeting(admin_client, "calendar-follow-up"))
    action = admin_client.get("/api/v1/actions").json["items"][0]
    accepted = admin_client.post(
        f"/api/v1/actions/{action['id']}/decision",
        json={
            "decision": "accept",
            "owner_id": admin_id(app),
            "due_at": "2026-09-30",
            "expected_version": action["version"],
        },
    )
    monkeypatch.setattr(
        "app.blueprints.api.routes.create_event",
        lambda *_args, **_kwargs: {
            "provider": "google",
            "id": "calendar-event-1",
            "url": "https://calendar.google/event",
        },
    )

    completed = admin_client.patch(
        f"/api/v1/actions/{action['id']}",
        json={
            "status": "completed",
            "outcome": "Pendência resolvida e retorno confirmado.",
            "customer_notified": True,
            "expected_version": accepted.json["version"],
            "follow_up": {
                "title": "Confirmar estabilidade com o cliente",
                "owner_id": admin_id(app),
                "due_at": "2026-10-02T14:00:00",
                "calendar_provider": "google",
            },
        },
    )

    assert completed.status_code == 200
    assert completed.json["follow_up"]["parent_action_id"] == action["id"]
    assert completed.json["calendar_event"]["id"] == "calendar-event-1"
    with app.app_context():
        assert ActionEvent.query.filter_by(event_type="calendar_scheduled").count() == 1

def test_feedback_is_structured_and_boolean_is_strict(admin_client, app):
    analysis_id, _ = _analysis_with_drafts(app, _meeting(admin_client, "feedback"))
    invalid = admin_client.post(
        f"/api/v1/analyses/{analysis_id}/feedback",
        json={
            "usefulness_rating": 5,
            "factual_accuracy": "correct",
            "action_useful": "false",
        },
    )
    valid = admin_client.post(
        f"/api/v1/analyses/{analysis_id}/feedback",
        json={
            "usefulness_rating": 4,
            "factual_accuracy": "minor_issues",
            "action_useful": True,
            "correction": "Confirmar o prazo com o cliente.",
        },
    )

    assert invalid.status_code == 422
    assert valid.status_code == 200


def test_privacy_execution_anonymizes_and_generates_pii_free_manifest(admin_client, app):
    requested = admin_client.post(
        "/api/v1/privacy/requests",
        json={"type": "deletion", "reason": "Solicitação confirmada pelo titular."},
    )
    approved = admin_client.post(
        f"/api/v1/admin/privacy-requests/{requested.json['id']}/approve"
    )
    executed = admin_client.post(
        f"/api/v1/admin/privacy-requests/{requested.json['id']}/execute"
    )

    assert approved.status_code == 200
    assert executed.status_code == 200
    assert executed.json["status"] == "completed"
    with app.app_context():
        execution = PrivacyExecution.query.one()
        user = User.query.filter(User.email.like("deleted-%@invalid.local")).one()
        assert execution.status == "completed"
        assert "email" not in execution.manifest
        assert "name" not in execution.manifest
        assert user.is_active_record is False


def test_legal_hold_blocks_privacy_approval(admin_client, app):
    requested = admin_client.post(
        "/api/v1/privacy/requests",
        json={"type": "deletion", "reason": "Solicitação confirmada pelo titular."},
    )
    with app.app_context():
        organization = Organization.query.one()
        organization.legal_hold_enabled = True
        db.session.commit()

    response = admin_client.post(
        f"/api/v1/admin/privacy-requests/{requested.json['id']}/approve"
    )
    assert response.status_code == 409
    assert response.json["code"] == "legal_hold"


def test_teams_webhook_validates_state_and_deduplicates(app):
    app.config["TEAMS_GRAPH_CLIENT_STATE"] = "state-secret"
    with app.app_context():
        organization = Organization.query.one()
        connection = TeamsConnection(
            organization_id=organization.id,
            tenant_id="tenant-test",
            teams_app_id="teams-app-test",
            subscription_id="subscription-test",
            status="active",
        )
        db.session.add(connection)
        db.session.commit()

    client = app.test_client()
    handshake = client.post(
        "/api/v1/integrations/teams/notifications?validationToken=validated"
    )
    invalid = client.post(
        "/api/v1/integrations/teams/notifications",
        json={
            "value": [
                {
                    "clientState": "wrong",
                    "subscriptionId": "subscription-test",
                }
            ]
        },
    )
    notification = {
        "value": [
            {
                "clientState": "state-secret",
                "subscriptionId": "subscription-test",
                "resource": "communications/onlineMeetings/m1/transcripts/t1",
                "resourceData": {"id": "t1", "meetingId": "m1"},
            }
        ]
    }
    first = client.post("/api/v1/integrations/teams/notifications", json=notification)
    duplicate = client.post("/api/v1/integrations/teams/notifications", json=notification)

    assert handshake.status_code == 200
    assert handshake.text == "validated"
    assert invalid.status_code == 401
    assert first.json["accepted"] == 1
    assert duplicate.json["accepted"] == 0
    with app.app_context():
        assert TeamsTranscriptReceipt.query.count() == 1


def test_action_pages_timeline_metrics_and_history(admin_client, app):
    analysis_id, _ = _analysis_with_drafts(app, _meeting(admin_client, "action-pages"))
    listing = admin_client.get("/api/v1/actions")
    action_id = listing.json["items"][0]["id"]
    detail = admin_client.get(f"/api/v1/actions/{action_id}")
    with app.app_context():
        action = db.session.get(ActionItem, action_id)
        client_id = action.client_id

    assert admin_client.get("/acoes").status_code == 200
    assert admin_client.get(f"/clientes/{client_id}").status_code == 200
    assert admin_client.get(f"/api/v1/clients/{client_id}/timeline").status_code == 200
    metrics = admin_client.get("/api/v1/pov/metrics")
    assert metrics.status_code == 200
    assert "average_review_minutes" in metrics.json["actions"]
    assert "decision_rate" in metrics.json["actions"]
    assert "follow_ups_created" in metrics.json["actions"]
    assert "average_response_hours" in metrics.json["routing"]
    assert detail.status_code == 200
    assert detail.json["analysis_id"] == analysis_id
    assert detail.json["events"][0]["type"] == "proposed"


def test_admin_configures_and_reads_teams_connection(admin_client, app):
    configured = admin_client.post(
        "/api/v1/admin/integrations/teams/subscription",
        json={"tenant_id": "tenant-admin", "teams_app_id": "app-admin"},
    )
    status = admin_client.get("/api/v1/admin/integrations/teams/status")

    assert configured.status_code == 202
    assert configured.json["queued"] is False
    assert status.status_code == 200
    assert status.json["configured"] is True
    assert status.json["connection"]["tenant_id"] == "tenant-admin"
    with app.app_context():
        assert OutboxEvent.query.filter_by(task_name="renew_teams_subscription").count() == 1


def test_outbox_replay_is_audited(admin_client, app, monkeypatch):
    with app.app_context():
        organization = Organization.query.one()
        event = OutboxEvent(
            organization_id=organization.id,
            queue_name="maintenance",
            task_name="renew_teams_subscription",
            job_id="dead-letter-test",
            arguments=["connection"],
            status="dead_letter",
            attempts=3,
        )
        db.session.add(event)
        db.session.commit()
        event_id = event.id
    monkeypatch.setattr("app.blueprints.admin.routes.dispatch_outbox_event", lambda _id: True)

    replayed = admin_client.post(f"/api/v1/admin/outbox/{event_id}/replay")
    repeated = admin_client.post(f"/api/v1/admin/outbox/{event_id}/replay")

    assert replayed.status_code == 200
    assert replayed.json["delivered"] is True
    assert repeated.status_code == 409


def test_organization_identity_policy_is_editable(admin_client):
    changed = admin_client.patch(
        "/api/v1/admin/organization",
        json={"oidc_tenant_id": "entra-tenant", "legal_hold_enabled": True},
    )
    detail = admin_client.get("/api/v1/admin/organization")

    assert changed.status_code == 200
    assert detail.json["oidc_tenant_id"] == "entra-tenant"
    assert detail.json["legal_hold_enabled"] is True


def test_viewer_cannot_be_assigned_or_complete_action(
    admin_client, viewer_client, app
):
    _, _ = _analysis_with_drafts(app, _meeting(admin_client, "assigned-action"))
    action = admin_client.get("/api/v1/actions").json["items"][0]
    with app.app_context():
        viewer = User.query.filter_by(email="viewer@example.test").one()
        viewer_id = viewer.id
    accepted = admin_client.post(
        f"/api/v1/actions/{action['id']}/decision",
        json={
            "decision": "accept",
            "owner_id": viewer_id,
            "due_at": "2026-09-01",
            "expected_version": action["version"],
        },
    )
    completed = viewer_client.patch(
        f"/api/v1/actions/{action['id']}",
        json={
            "status": "completed",
            "outcome": "Treinamento confirmado com os responsáveis.",
            "expected_version": action["version"],
        },
    )

    assert accepted.status_code == 422
    assert accepted.json["code"] == "owner_required"
    assert completed.status_code == 404


def test_action_decision_rejects_cross_tenant_owner_and_redecision(admin_client, app):
    _, _ = _analysis_with_drafts(app, _meeting(admin_client, "owner-guard"))
    action = admin_client.get("/api/v1/actions").json["items"][0]
    with app.app_context():
        other = Organization(slug="owner-other", name="Outra")
        db.session.add(other)
        db.session.flush()
        outsider = User(
            organization_id=other.id,
            email="outsider@example.test",
            name="Outsider",
        )
        outsider.set_password("correct-horse-battery-staple")
        db.session.add(outsider)
        db.session.commit()
        outsider_id = outsider.id
    invalid_owner = admin_client.post(
        f"/api/v1/actions/{action['id']}/decision",
        json={
            "decision": "accept",
            "owner_id": outsider_id,
            "expected_version": action["version"],
        },
    )
    accepted = admin_client.post(
        f"/api/v1/actions/{action['id']}/decision",
        json={
            "decision": "accept",
            "owner_id": admin_id(app),
            "due_at": "2026-09-01",
            "expected_version": action["version"],
        },
    )
    repeated = admin_client.post(
        f"/api/v1/actions/{action['id']}/decision",
        json={"decision": "dismiss", "expected_version": accepted.json["version"]},
    )

    assert invalid_owner.status_code == 422
    assert accepted.status_code == 200
    assert repeated.status_code == 409
    assert repeated.json["code"] == "invalid_state"


def test_outbox_successful_dispatch_preserves_correlation(app, monkeypatch):
    from app.services.outbox import add, dispatch

    calls = []

    class FakeQueue:
        def __init__(self, *_args, **_kwargs):
            pass

        def enqueue(self, *args, **kwargs):
            calls.append((args, kwargs))

    monkeypatch.setattr("app.services.outbox.Queue", FakeQueue)
    monkeypatch.setattr("app.services.outbox.redis_client", lambda: object())
    with app.app_context():
        organization = Organization.query.one()
        event = add(
            organization_id=organization.id,
            queue_name="analyses",
            task_name="process_analysis",
            job_id="dispatch-success",
            arguments=["analysis"],
            correlation_id="correlation-test",
        )
        db.session.commit()
        assert dispatch(event.id) is True
        stored = db.session.get(OutboxEvent, event.id)
        assert stored.status == "dispatched"
        assert stored.correlation_id == "correlation-test"
        assert calls


def test_tenant_aware_cli_commands(app):
    runner = app.test_cli_runner()
    created = runner.invoke(
        args=[
            "create-admin",
            "--email",
            "new-admin@example.test",
            "--name",
            "New Admin",
            "--password",
            "correct-horse-battery-staple",
            "--organization-slug",
            "meetmind-portfolio",
        ]
    )
    break_glass = runner.invoke(
        args=[
            "set-break-glass",
            "--email",
            "new-admin@example.test",
            "--enabled",
            "true",
            "--organization-slug",
            "meetmind-portfolio",
        ]
    )
    request_limit = runner.invoke(
        args=[
            "set-user-request-limit",
            "--email",
            "viewer@example.test",
            "--requests",
            "12",
            "--organization-slug",
            "meetmind-portfolio",
        ]
    )

    assert created.exit_code == 0, created.output
    assert break_glass.exit_code == 0, break_glass.output
    assert request_limit.exit_code == 0, request_limit.output
    with app.app_context():
        admin = User.query.filter_by(email="new-admin@example.test").one()
        viewer = User.query.filter_by(email="viewer@example.test").one()
        assert admin.role.value == "admin"
        assert admin.is_break_glass is True
        assert viewer.request_limit_per_10_minutes == 12


def test_privacy_execution_removes_private_objects(app, monkeypatch):
    from app.services.privacy_execution import execute

    monkeypatch.setattr("app.services.privacy_execution.delete", lambda _key: True)
    with app.app_context():
        user = User.query.filter_by(email="viewer@example.test").one()
        submission = MeetingSubmission(
            organization_id=user.organization_id,
            status="completed",
            created_by_id=user.id,
            original_filename="private.json",
            file_format="json",
            content_type="application/json",
            byte_size=2,
            file_sha256="f" * 64,
            stored_object_key="private-object",
            retained_until=utcnow() + timedelta(days=7),
            consent_at=utcnow(),
        )
        request_item = PrivacyRequest(
            organization_id=user.organization_id,
            requested_by_id=user.id,
            request_type="deletion",
            status="approved",
            reason="Solicitação confirmada.",
        )
        db.session.add_all([submission, request_item])
        db.session.commit()

        execution = execute(request_item)

        assert execution.manifest["deleted_objects"] == 1
        assert submission.stored_object_key is None
        assert submission.original_deleted_at is not None


def test_privacy_execution_records_partial_failure(app, monkeypatch):
    from app.services.privacy_execution import execute

    def fail_delete(_key):
        raise OSError("storage unavailable")

    monkeypatch.setattr("app.services.privacy_execution.delete", fail_delete)
    with app.app_context():
        user = User.query.filter_by(email="viewer@example.test").one()
        db.session.add(
            MeetingSubmission(
                organization_id=user.organization_id,
                status="completed",
                created_by_id=user.id,
                original_filename="private.json",
                file_format="json",
                content_type="application/json",
                byte_size=2,
                file_sha256="e" * 64,
                stored_object_key="failing-object",
                retained_until=utcnow() + timedelta(days=7),
                consent_at=utcnow(),
            )
        )
        request_item = PrivacyRequest(
            organization_id=user.organization_id,
            requested_by_id=user.id,
            request_type="deletion",
            status="approved",
            reason="Solicitação confirmada.",
        )
        db.session.add(request_item)
        db.session.commit()

        with pytest.raises(OSError, match="storage unavailable"):
            execute(request_item)

        failed = PrivacyExecution.query.one()
        assert failed.status == "failed"
        assert request_item.status == "failed"
