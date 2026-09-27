from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime

from flask import Blueprint, Response, current_app, jsonify, request
from flask_login import current_user, login_required
from pydantic import ValidationError
from redis.exceptions import RedisError
from sqlalchemy import or_, text

from app.extensions import csrf, db, limiter, sensitive_request_limit
from app.models import (
    ActionEvent,
    ActionItem,
    ActionStatus,
    AIExecution,
    Analysis,
    AnalysisAutopsyRun,
    AnalysisFeedback,
    AnalysisStatus,
    Client,
    EvidenceIssue,
    KanbanRequest,
    KanbanRequestStatus,
    Meeting,
    MeetingParticipant,
    MeetingPlan,
    MeetingSubmission,
    Organization,
    PrivacyRequest,
    ProductEvent,
    Role,
    Segment,
    TeamsConnection,
    TeamsTranscriptReceipt,
    User,
    utcnow,
)
from app.schemas import AnalysisRequestInput, MeetingInput
from app.security import audit, redis_client, role_required, sensitive_operation
from app.services import antivirus
from app.services.access_policy import (
    actions_for,
    can_access_client,
    can_manage_request,
    can_manage_user,
)
from app.services.actions import (
    action_json,
    can_manage,
    create_draft,
    create_follow_up,
    plan_json,
    record_event,
    sync_plan_status,
)
from app.services.autopsy import profile_hash as autopsy_profile_hash
from app.services.calendar_oauth import CalendarOAuthError, create_event, events_for_user
from app.services.dashboard import briefing as dashboard_briefing_data
from app.services.dashboard import focus as dashboard_focus_data
from app.services.dashboard import timeline as dashboard_timeline_data
from app.services.feedback import autopsy_run_json, feedback_json
from app.services.gemini_modes import (
    MODE_BUNDLE_VERSION,
    analysis_profile_hash,
)
from app.services.kanban import (
    create_request as create_kanban_request,
)
from app.services.kanban import (
    list_requests as list_kanban_requests,
)
from app.services.kanban import (
    request_json as kanban_request_json,
)
from app.services.kanban import (
    update_request as update_kanban_request,
)
from app.services.meeting_access import (
    can_access_analysis,
    can_access_meeting,
    meetings_for,
)
from app.services.metrics import calculate_metrics
from app.services.outbox import add as add_outbox_event
from app.services.outbox import dispatch as dispatch_outbox_event
from app.services.sector_context import build_context_snapshot, public_context_snapshot
from app.tenancy import tenant_scope, tenant_segment_id

api_bp = Blueprint("api", __name__, url_prefix="/api/v1")


def meeting_json(meeting: Meeting, include_segments: bool = False) -> dict:
    data = {
        "id": meeting.id,
        "external_id": meeting.external_id,
        "title": meeting.title,
        "client": (
            {
                "id": meeting.client.id,
                "external_id": meeting.client.external_id,
                "name": meeting.client.name,
            }
            if meeting.client
            else None
        ),
        "occurred_at": meeting.occurred_at.isoformat() if meeting.occurred_at else None,
        "duration_seconds": meeting.duration_seconds,
        "approved_for_external_ai": meeting.approved_for_external_ai,
        "metadata": meeting.metadata_json,
        "created_at": meeting.created_at.isoformat(),
    }
    if include_segments:
        data["segments"] = [
            {
                "id": segment.id,
                "position": segment.position,
                "speaker": segment.speaker,
                "is_totvs": segment.is_totvs,
                "text": segment.text,
            }
            for segment in meeting.segments
        ]
        data["analyses"] = [
            {"id": analysis.id, "status": analysis.status.value}
            for analysis in sorted(
                meeting.analyses, key=lambda item: item.requested_at, reverse=True
            )
        ]
    return data


@api_bp.get("/ping")
def ping():
    return jsonify(status="ok", service="TOTVS MeetMind API v1")


@api_bp.post("/integrations/teams/notifications")
@csrf.exempt
@limiter.limit("600 per minute")
def teams_notifications():
    validation_token = request.args.get("validationToken")
    if validation_token:
        return Response(validation_token[:2_000], content_type="text/plain; charset=utf-8")
    payload = request.get_json(silent=True) or {}
    notifications = payload.get("value") if isinstance(payload.get("value"), list) else []
    configured_state = str(current_app.config.get("TEAMS_GRAPH_CLIENT_STATE") or "")
    if not configured_state:
        return jsonify(error="Integração indisponível.", code="not_configured"), 503
    accepted = 0
    for notification in notifications[:100]:
        if not isinstance(notification, dict):
            continue
        supplied_state = str(notification.get("clientState") or "")
        if not hmac.compare_digest(supplied_state, configured_state):
            return jsonify(error="Notificação inválida.", code="invalid_client_state"), 401
        subscription_id = str(notification.get("subscriptionId") or "")
        connection = TeamsConnection.query.filter_by(subscription_id=subscription_id).first()
        if connection is None or connection.status not in {"active", "pending"}:
            continue
        resource_data = (
            notification.get("resourceData")
            if isinstance(notification.get("resourceData"), dict)
            else {}
        )
        resource_url = str(notification.get("resource") or "")[:2_000]
        transcript_id = str(resource_data.get("id") or resource_url.rsplit("/", 1)[-1])[:200]
        meeting_id = str(resource_data.get("meetingId") or "")[:200] or None
        if not transcript_id or not resource_url:
            continue
        with tenant_scope(connection.organization_id):
            receipt = TeamsTranscriptReceipt.query.filter_by(
                organization_id=connection.organization_id,
                transcript_id=transcript_id,
            ).first()
            if receipt is None:
                receipt = TeamsTranscriptReceipt(
                    organization_id=connection.organization_id,
                    transcript_id=transcript_id,
                    meeting_id=meeting_id,
                    resource_url=resource_url,
                )
                db.session.add(receipt)
                db.session.flush()
                event = add_outbox_event(
                    organization_id=connection.organization_id,
                    queue_name="screening",
                    task_name="process_teams_transcript",
                    job_id=f"teams-transcript-{receipt.id}",
                    arguments=[receipt.id],
                    retry_intervals=[60, 300, 900],
                )
                db.session.commit()
                if current_app.config["QUEUE_ENABLED"]:
                    dispatch_outbox_event(event.id)
                accepted += 1
    return jsonify(accepted=accepted), 202


@api_bp.get("/health/live")
def health_live():
    return jsonify(status="ok", service="meetmind-web")


@api_bp.get("/health")
def health():
    """Compatibility health endpoint for clients predating live/ready probes."""
    checks = {"database": "unavailable", "redis": "disabled"}
    try:
        db.session.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception:
        db.session.rollback()
    if current_app.config["QUEUE_ENABLED"]:
        try:
            checks["redis"] = "ok" if redis_client().ping() else "unavailable"
        except (RedisError, ValueError):
            checks["redis"] = "unavailable"
    healthy = checks["database"] == "ok" and checks["redis"] != "unavailable"
    return (
        jsonify(status="ok" if healthy else "unavailable", checks=checks),
        200 if healthy else 503,
    )


@api_bp.get("/health/ready")
def health_ready():
    checks = {"database": "unavailable", "redis": "disabled", "antivirus": "disabled"}
    try:
        db.session.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception:
        db.session.rollback()
    if current_app.config["QUEUE_ENABLED"]:
        try:
            checks["redis"] = "ok" if redis_client().ping() else "unavailable"
        except (RedisError, ValueError):
            checks["redis"] = "unavailable"
    if current_app.config["ANTIVIRUS_ENABLED"]:
        checks["antivirus"] = antivirus.scan(b"meetmind-healthcheck")["status"]
    ready = checks["database"] == "ok" and checks["redis"] != "unavailable"
    if current_app.config.get("SUBMISSION_REQUIRE_ANTIVIRUS"):
        ready = ready and checks["antivirus"] == "clean"
    return jsonify(status="ready" if ready else "not_ready", checks=checks), 200 if ready else 503


@api_bp.post("/meetings")
@role_required(Role.ADMIN)
@sensitive_operation
@limiter.limit(sensitive_request_limit)
def create_meeting():
    if not request.is_json:
        return (
            jsonify(
                error="Content-Type application/json é obrigatório.",
                code="invalid_media_type",
            ),
            415,
        )
    try:
        payload = MeetingInput.model_validate(request.get_json())
    except ValidationError as error:
        return jsonify(error="Dados inválidos.", fields=error.errors(include_url=False)), 422
    if Meeting.query.filter_by(
        external_id=payload.external_id,
        organization_id=current_user.organization_id,
    ).first():
        return jsonify(error="Reunião já cadastrada.", code="conflict"), 409

    client = None
    if payload.client_external_id:
        client = Client.query.filter_by(
            external_id=payload.client_external_id,
            organization_id=current_user.organization_id,
        ).first()
        if not client:
            client = Client(
                external_id=payload.client_external_id,
                name=payload.client_name,
                organization_id=current_user.organization_id,
                owner_id=current_user.id,
            )
            db.session.add(client)
            db.session.flush()
    segments = payload.segments or []
    if payload.transcript:
        from app.schemas import SegmentInput

        segments = [SegmentInput(id=f"{payload.external_id}.0", text=payload.transcript)]
    digest = hashlib.sha256("\n".join(segment.text for segment in segments).encode()).hexdigest()
    meeting = Meeting(
        organization_id=current_user.organization_id,
        external_id=payload.external_id,
        title=payload.title,
        client=client,
        occurred_at=payload.occurred_at,
        language=payload.language,
        approved_for_external_ai=payload.approved_for_external_ai,
        transcript_hash=digest,
        metadata_json=payload.metadata.model_dump(),
        created_by_id=current_user.id,
    )
    db.session.add(meeting)
    db.session.flush()
    db.session.add(
        MeetingParticipant(
            organization_id=current_user.organization_id,
            meeting_id=meeting.id,
            user_id=current_user.id,
            participation_role="organizer",
        )
    )
    for position, item in enumerate(segments):
        db.session.add(
            Segment(
                organization_id=current_user.organization_id,
                id=tenant_segment_id(current_user.organization_id, item.id),
                meeting_id=meeting.id,
                position=position,
                speaker=item.speaker,
                is_totvs=item.is_totvs,
                text=item.text,
                started_at_ms=item.started_at_ms,
                ended_at_ms=item.ended_at_ms,
            )
        )
    audit("MEETING_CREATED", "meeting", meeting.id)
    db.session.commit()
    return jsonify(meeting_json(meeting)), 201


@api_bp.get("/meetings")
@login_required
@limiter.limit("120 per minute")
def meetings():
    page = max(1, request.args.get("page", 1, type=int))
    per_page = min(100, max(1, request.args.get("per_page", 25, type=int)))
    query = meetings_for(current_user).order_by(
        Meeting.occurred_at.desc(), Meeting.created_at.desc()
    )
    search = request.args.get("q", "").strip()[:100]
    if search:
        query = query.filter(
            Meeting.external_id.ilike(f"%{search}%") | Meeting.title.ilike(f"%{search}%")
        )
    pagination = query.paginate(page=page, per_page=per_page, error_out=False)
    return jsonify(
        items=[meeting_json(item) for item in pagination.items],
        page=page,
        pages=pagination.pages,
        total=pagination.total,
    )


@api_bp.get("/meetings/<meeting_id>")
@login_required
@limiter.limit("120 per minute")
def meeting_detail(meeting_id: str):
    meeting = db.session.get(Meeting, meeting_id)
    if not can_access_meeting(current_user, meeting):
        return jsonify(error="Recurso não encontrado.", code="not_found"), 404
    return jsonify(meeting_json(meeting, include_segments=True))


@api_bp.post("/meetings/<meeting_id>/approval")
@role_required(Role.ADMIN)
@sensitive_operation
@limiter.limit(sensitive_request_limit)
def approve_meeting(meeting_id: str):
    meeting = db.session.get(Meeting, meeting_id)
    if not meeting:
        return jsonify(error="Recurso não encontrado.", code="not_found"), 404
    if not request.is_json or set((request.get_json(silent=True) or {}).keys()) != {
        "approved_for_external_ai"
    }:
        return jsonify(error="Corpo inválido.", code="invalid_payload"), 422
    approved = request.get_json()["approved_for_external_ai"]
    if not isinstance(approved, bool):
        return jsonify(error="Corpo inválido.", code="invalid_payload"), 422
    meeting.approved_for_external_ai = approved
    audit("MEETING_AI_APPROVAL_CHANGED", "meeting", meeting.id, approved=approved)
    db.session.commit()
    return jsonify(id=meeting.id, approved_for_external_ai=approved)


@api_bp.post("/meetings/<meeting_id>/analyses")
@role_required(Role.ADMIN, Role.MANAGER, Role.ANALYST)
@sensitive_operation
@limiter.limit(sensitive_request_limit)
def create_analysis(meeting_id: str):
    meeting = db.session.get(Meeting, meeting_id)
    if not can_access_meeting(current_user, meeting):
        return jsonify(error="Recurso não encontrado.", code="not_found"), 404

    organization = db.session.get(Organization, current_user.organization_id)
    if organization is None or not organization.external_ai_enabled:
        return (
            jsonify(error="IA externa desativada para esta organização.", code="ai_disabled"),
            422,
        )

    if request.data and not request.is_json:
        return jsonify(error="O corpo deve ser JSON.", code="invalid_content_type"), 415
    raw_payload = request.get_json(silent=True) or {}
    use_mode_bundle = bool(raw_payload)
    if use_mode_bundle:
        try:
            analysis_request = AnalysisRequestInput.model_validate(raw_payload)
        except ValidationError as exc:
            return (
                jsonify(
                    error="Seleção de modos inválida.",
                    code="invalid_payload",
                    details=exc.errors(include_url=False, include_context=False),
                ),
                422,
            )
        mode_strategy = analysis_request.mode_strategy
        requested_modes = list(analysis_request.requested_modes)
        prompt_version = MODE_BUNDLE_VERSION
        mode_bundle_version = MODE_BUNDLE_VERSION
        profile_hash = analysis_profile_hash(mode_strategy, requested_modes)
    else:
        # Backward-compatible production path: one Gemini request per meeting,
        # with one repair request only when the structured response is invalid.
        mode_strategy = "auto"
        requested_modes = []
        prompt_version = current_app.config["PROMPT_VERSION"]
        mode_bundle_version = prompt_version
        profile_hash = "legacy"

    context_snapshot = build_context_snapshot(meeting)
    profile_hash = autopsy_profile_hash(
        profile_hash,
        context_snapshot,
        current_app.config.get("AUTOPSY_ASSISTANT_MODE", "off"),
    )
    cache = Analysis.query.filter_by(
        meeting_id=meeting.id,
        transcript_hash=meeting.transcript_hash,
        prompt_version=prompt_version,
        score_version=current_app.config["SCORE_VERSION"],
        model=current_app.config["GEMINI_MODEL"],
        context_sha256=context_snapshot["context_sha256"],
        analysis_profile_hash=profile_hash,
        status=AnalysisStatus.SUCCEEDED,
    ).first()
    if cache:
        audit(
            "ANALYSIS_CACHE_REUSED",
            "analysis",
            cache.id,
            meeting_id=meeting.id,
            prompt_version=cache.prompt_version,
            mode_strategy=cache.mode_strategy,
            requested_modes=cache.requested_modes,
            selected_modes=cache.selected_modes,
            analysis_profile_hash=cache.analysis_profile_hash,
        )
        db.session.commit()
        return jsonify(
            analysis_id=cache.id,
            status=cache.status.value,
            status_url=f"/api/v1/analyses/{cache.id}",
            cached=True,
        ), 200

    active_analysis = (
        Analysis.query.filter(
            Analysis.meeting_id == meeting.id,
            Analysis.transcript_hash == meeting.transcript_hash,
            Analysis.prompt_version == prompt_version,
            Analysis.score_version == current_app.config["SCORE_VERSION"],
            Analysis.model == current_app.config["GEMINI_MODEL"],
            Analysis.context_sha256 == context_snapshot["context_sha256"],
            Analysis.analysis_profile_hash == profile_hash,
            Analysis.status.in_([AnalysisStatus.QUEUED, AnalysisStatus.RUNNING]),
        )
        .order_by(Analysis.requested_at.desc())
        .first()
    )
    if active_analysis:
        audit(
            "ANALYSIS_IN_PROGRESS_REUSED",
            "analysis",
            active_analysis.id,
            meeting_id=meeting.id,
            status=active_analysis.status.value,
        )
        db.session.commit()
        return jsonify(
            analysis_id=active_analysis.id,
            status=active_analysis.status.value,
            status_url=f"/api/v1/analyses/{active_analysis.id}",
            cached=False,
            in_progress=True,
            delivery="existing",
        ), 202

    if not current_app.config["QUEUE_ENABLED"]:
        audit(
            "ANALYSIS_REJECTED",
            "meeting",
            meeting.id,
            reason="queue_disabled",
        )
        db.session.commit()
        return (
            jsonify(
                error=(
                    "A interpretação por IA exige o worker Redis. "
                    "Use o modo Docker para processar uma nova análise."
                ),
                code="queue_disabled",
            ),
            503,
        )

    analysis = Analysis(
        organization_id=current_user.organization_id,
        provider=current_app.config["AI_PROVIDER"],
        meeting_id=meeting.id,
        requested_by_id=current_user.id,
        model=current_app.config["GEMINI_MODEL"],
        prompt_version=prompt_version,
        score_version=current_app.config["SCORE_VERSION"],
        transcript_hash=meeting.transcript_hash,
        context_snapshot=context_snapshot,
        context_version=context_snapshot["context_version"],
        context_sha256=context_snapshot["context_sha256"],
        mode_strategy=mode_strategy,
        requested_modes=requested_modes,
        mode_bundle_version=mode_bundle_version,
        analysis_profile_hash=profile_hash,
        local_metrics=calculate_metrics(meeting),
    )
    db.session.add(analysis)
    db.session.flush()
    audit(
        "ANALYSIS_REQUESTED",
        "analysis",
        analysis.id,
        meeting_id=meeting.id,
        mode_strategy=analysis.mode_strategy,
        requested_modes=analysis.requested_modes,
        analysis_profile_hash=analysis.analysis_profile_hash,
    )
    dispatch = add_outbox_event(
        organization_id=current_user.organization_id,
        queue_name="analyses",
        task_name="process_analysis",
        job_id=analysis.id,
        arguments=[analysis.id],
        retry_intervals=[60, 180, 600],
    )
    db.session.commit()
    delivered = dispatch_outbox_event(dispatch.id)
    return jsonify(
        analysis_id=analysis.id,
        status=analysis.status.value,
        status_url=f"/api/v1/analyses/{analysis.id}",
        cached=False,
        delivery="dispatched" if delivered else "pending",
    ), 202


@api_bp.get("/meetings/<meeting_id>/analyses")
@login_required
@limiter.limit("120 per minute")
def meeting_analyses(meeting_id: str):
    meeting = db.session.get(Meeting, meeting_id)
    if not can_access_meeting(current_user, meeting):
        return jsonify(error="Recurso não encontrado.", code="not_found"), 404
    analyses = (
        Analysis.query.filter_by(meeting_id=meeting.id)
        .order_by(Analysis.requested_at.desc())
        .limit(20)
        .all()
    )
    return jsonify(
        items=[
            {
                "id": analysis.id,
                "status": analysis.status.value,
                "status_url": f"/api/v1/analyses/{analysis.id}",
                "error": (
                    analysis.error_code if analysis.status == AnalysisStatus.FAILED else None
                ),
                "requested_at": analysis.requested_at.isoformat(),
                "finished_at": (analysis.finished_at.isoformat() if analysis.finished_at else None),
                "mode_strategy": analysis.mode_strategy,
                "requested_modes": analysis.requested_modes or [],
                "selected_modes": analysis.selected_modes or [],
                "mode_bundle_version": analysis.mode_bundle_version,
                "analysis_profile_hash": analysis.analysis_profile_hash,
            }
            for analysis in analyses
        ]
    )


@api_bp.post("/analyses/<analysis_id>/actions/<int:source_index>")
@role_required(Role.ADMIN, Role.MANAGER, Role.ANALYST)
@sensitive_operation
@limiter.limit(sensitive_request_limit)
def create_analysis_action(analysis_id: str, source_index: int):
    analysis = db.session.get(Analysis, analysis_id)
    if (
        not can_access_analysis(current_user, analysis)
        or analysis.status != AnalysisStatus.SUCCEEDED
    ):
        return jsonify(error="Recurso não encontrado.", code="not_found"), 404
    try:
        action, created = create_draft(analysis, source_index, actor_id=current_user.id)
    except (IndexError, ValueError):
        return jsonify(error="Sugestão inválida.", code="invalid_action_source"), 422
    audit(
        "ANALYSIS_ACTION_ADDED",
        "action_item",
        action.id,
        analysis_id=analysis.id,
        source_index=source_index,
        created=created,
    )
    db.session.commit()
    return jsonify({**action_json(action), "created": created}), 201 if created else 200


@api_bp.get("/meetings/<meeting_id>/analysis-context")
@login_required
@limiter.limit("120 per minute")
def meeting_analysis_context(meeting_id: str):
    meeting = db.session.get(Meeting, meeting_id)
    if not can_access_meeting(current_user, meeting):
        return jsonify(error="Recurso não encontrado.", code="not_found"), 404
    snapshot = build_context_snapshot(meeting)
    audit(
        "ANALYSIS_CONTEXT_VIEWED",
        "meeting",
        meeting.id,
        context_sha256=snapshot["context_sha256"],
        playbook_id=snapshot["playbook"]["id"],
        sector_source=snapshot["selection"]["source"],
    )
    db.session.commit()
    return jsonify(context=public_context_snapshot(snapshot))


@api_bp.get("/analyses/<analysis_id>/autopsy-comparison")
@role_required(Role.ADMIN, Role.MANAGER, Role.ANALYST)
def autopsy_comparison(analysis_id: str):
    analysis = db.session.get(Analysis, analysis_id)
    if not can_access_analysis(current_user, analysis):
        return jsonify(error="Recurso não encontrado.", code="not_found"), 404
    run = (
        AnalysisAutopsyRun.query.filter_by(analysis_id=analysis.id)
        .order_by(AnalysisAutopsyRun.started_at.desc())
        .first()
    )
    if run is None:
        return jsonify(error="Comparação não disponível.", code="not_found"), 404
    return jsonify(autopsy=autopsy_run_json(run))


@api_bp.get("/analyses/<analysis_id>")
@login_required
@limiter.limit("120 per minute")
def analysis_detail(analysis_id: str):
    analysis = db.session.get(Analysis, analysis_id)
    if not can_access_analysis(current_user, analysis):
        return jsonify(error="Recurso não encontrado.", code="not_found"), 404
    executions = (
        AIExecution.query.filter_by(analysis_id=analysis.id)
        .order_by(AIExecution.attempt.asc())
        .all()
    )
    can_audit = current_user.role in {Role.ADMIN, Role.MANAGER, Role.ANALYST}
    tracked_actions = ActionItem.query.filter_by(analysis_id=analysis.id).all()
    plan = MeetingPlan.query.filter_by(analysis_id=analysis.id).first()
    return jsonify(
        id=analysis.id,
        meeting_id=analysis.meeting_id,
        status=analysis.status.value,
        mode_strategy=analysis.mode_strategy,
        requested_modes=analysis.requested_modes or [],
        selected_modes=analysis.selected_modes or [],
        mode_bundle_version=analysis.mode_bundle_version,
        analysis_profile_hash=analysis.analysis_profile_hash,
        local_metrics=analysis.local_metrics if can_audit else None,
        result=analysis.result if analysis.status == AnalysisStatus.SUCCEEDED else None,
        plan=plan_json(plan),
        actions=[action_json(item, include_events=can_audit) for item in tracked_actions],
        action_assignees=(
            [
                {"id": user.id, "name": user.name}
                for user in User.query.filter_by(is_active_record=True)
                .order_by(User.name.asc())
                .all()
            ]
            if can_audit
            else []
        ),
        audit_context=(
            public_context_snapshot(analysis.context_snapshot or {}) if can_audit else None
        ),
        segments=(
            [
                {
                    "id": item.id,
                    "position": item.position,
                    "speaker": item.speaker,
                    "is_totvs": item.is_totvs,
                    "text": item.text,
                    "started_at_ms": item.started_at_ms,
                    "ended_at_ms": item.ended_at_ms,
                }
                for item in analysis.meeting.segments
            ]
            if can_audit
            else []
        ),
        traceability={
            "model": analysis.model,
            "prompt_version": analysis.prompt_version,
            "score_version": analysis.score_version,
            "transcript_hash": analysis.transcript_hash,
            "context_version": analysis.context_version,
            "context_sha256": analysis.context_sha256,
            "input_tokens": analysis.input_tokens,
            "output_tokens": analysis.output_tokens,
            "latency_ms": analysis.latency_ms,
            "provider": analysis.provider,
            "mode_strategy": analysis.mode_strategy,
            "requested_modes": analysis.requested_modes or [],
            "selected_modes": analysis.selected_modes or [],
            "mode_bundle_version": analysis.mode_bundle_version,
            "analysis_profile_hash": analysis.analysis_profile_hash,
        }
        if can_audit
        else {},
        error=analysis.error_code if analysis.status == AnalysisStatus.FAILED else None,
        requested_at=analysis.requested_at.isoformat(),
        finished_at=analysis.finished_at.isoformat() if analysis.finished_at else None,
        executions=[
            {
                "id": item.id,
                "attempt": item.attempt,
                "provider": item.provider,
                "model": item.model,
                "status": item.status,
                "schema_sha256": item.schema_sha256,
                "context_sha256": item.context_sha256,
                "result_sha256": item.result_sha256,
                "input_tokens": item.input_tokens,
                "output_tokens": item.output_tokens,
                "latency_ms": item.latency_ms,
                "estimated_cost_microunits": item.estimated_cost_microunits,
                "cost_currency": item.cost_currency,
                "error_code": item.error_code,
                "started_at": item.started_at.isoformat(),
                "finished_at": item.finished_at.isoformat() if item.finished_at else None,
            }
            for item in executions
        ]
        if can_audit
        else [],
    )


@api_bp.get("/analyses/<analysis_id>/plan")
@login_required
@limiter.limit("120 per minute")
def analysis_plan(analysis_id: str):
    analysis = db.session.get(Analysis, analysis_id)
    if not can_access_analysis(current_user, analysis):
        return jsonify(error="Recurso não encontrado.", code="not_found"), 404
    plan = MeetingPlan.query.filter_by(analysis_id=analysis.id).first()
    return jsonify(plan=plan_json(plan, include_actions=True))


@api_bp.post("/analyses/<analysis_id>/evidence-issues")
@login_required
@sensitive_operation
def report_evidence_issue(analysis_id: str):
    analysis = db.session.get(Analysis, analysis_id)
    if (
        not can_access_analysis(current_user, analysis)
        or analysis.status != AnalysisStatus.SUCCEEDED
    ):
        return jsonify(error="Recurso não encontrado.", code="not_found"), 404
    payload = request.get_json(silent=True) or {}
    segment_id = str(payload.get("segment_id") or "").strip()
    category = str(payload.get("category") or "other").strip()
    comment = str(payload.get("comment") or "").strip()
    segment = db.session.get(Segment, segment_id)
    if segment is None or segment.meeting_id != analysis.meeting_id:
        return jsonify(error="Evidência inválida.", code="invalid_evidence"), 422
    if category not in {"incorrect", "out_of_context", "speaker", "other"}:
        return jsonify(error="Categoria inválida.", code="invalid_payload"), 422
    if len(comment) < 3 or len(comment) > 2_000:
        return jsonify(error="Descreva o problema encontrado.", code="invalid_payload"), 422
    issue = EvidenceIssue(
        organization_id=analysis.organization_id,
        analysis_id=analysis.id,
        segment_id=segment.id,
        reporter_id=current_user.id,
        category=category,
        comment=comment,
    )
    db.session.add(issue)
    audit(
        "EVIDENCE_ISSUE_REPORTED",
        "analysis",
        analysis.id,
        segment_id=segment.id,
        category=category,
    )
    db.session.commit()
    return jsonify(id=issue.id, status="recorded"), 201


@api_bp.post("/analyses/<analysis_id>/actions/confirm-all")
@role_required(Role.ADMIN, Role.MANAGER, Role.ANALYST)
@sensitive_operation
def confirm_analysis_actions(analysis_id: str):
    analysis = db.session.get(Analysis, analysis_id)
    if (
        not can_access_analysis(current_user, analysis)
        or analysis.status != AnalysisStatus.SUCCEEDED
    ):
        return jsonify(error="Recurso não encontrado.", code="not_found"), 404
    plan = MeetingPlan.query.filter_by(analysis_id=analysis.id).first()
    if plan is None:
        return jsonify(error="Plano não encontrado.", code="not_found"), 404
    payload = request.get_json(silent=True) or {}
    entries = payload.get("actions")
    if not isinstance(entries, list) or not entries:
        return jsonify(error="Informe as ações para confirmação.", code="invalid_payload"), 422
    by_id = {
        str(entry.get("id")): entry
        for entry in entries
        if isinstance(entry, dict) and entry.get("id")
    }
    drafts = (
        ActionItem.query.filter_by(
            analysis_id=analysis.id,
            status=ActionStatus.PROPOSED.value,
        )
        .order_by(ActionItem.source_index.asc())
        .all()
    )
    if not drafts or set(by_id) != {action.id for action in drafts}:
        return jsonify(error="A lista de ações foi atualizada.", code="version_conflict"), 409
    prepared = []
    for action in drafts:
        entry = by_id[action.id]
        if int(entry.get("expected_version", -1)) != action.version:
            return (
                jsonify(
                    error="A ação foi alterada por outra pessoa.",
                    code="version_conflict",
                ),
                409,
            )
        title = str(entry.get("title") or action.title).strip()
        owner_id = str(entry.get("owner_id") or action.owner_id or "").strip()
        due_value = str(entry.get("due_at") or "").strip()
        owner = db.session.get(User, owner_id) if owner_id else None
        owner_allowed = bool(
            owner
            and owner.is_active_record
            and owner.role in {Role.ADMIN, Role.MANAGER, Role.ANALYST}
            and (
                current_user.role == Role.ADMIN
                or (current_user.role == Role.MANAGER and can_manage_user(current_user, owner))
                or owner.id == current_user.id
            )
        )
        if not owner_allowed:
            return jsonify(error="Defina um responsável válido.", code="owner_required"), 422
        if not due_value:
            return jsonify(error="Defina o prazo de todas as ações.", code="due_at_required"), 422
        try:
            due_at = datetime.fromisoformat(due_value.replace("Z", "+00:00"))
        except ValueError:
            return jsonify(error="Prazo inválido.", code="invalid_payload"), 422
        if not title:
            return jsonify(error="Título inválido.", code="invalid_payload"), 422
        prepared.append((action, title[:500], owner_id, due_at))
    for action, title, owner_id, due_at in prepared:
        action.title = title
        action.owner_id = owner_id
        action.due_at = due_at
        action.status = ActionStatus.ACCEPTED.value
        action.accepted_by_id = current_user.id
        action.accepted_at = utcnow()
        action.version += 1
        record_event(
            action,
            current_user.id,
            "accepted",
            owner_id=owner_id,
            due_at=due_at.isoformat(),
            bulk=True,
        )
    sync_plan_status(plan.id, current_user.id)
    audit(
        "ANALYSIS_ACTIONS_CONFIRMED",
        "meeting_plan",
        plan.id,
        analysis_id=analysis.id,
        action_count=len(prepared),
    )
    db.session.commit()
    return jsonify(
        plan=plan_json(plan),
        actions=[action_json(action) for action, *_ in prepared],
    )


@api_bp.get("/actions")
@login_required
@limiter.limit("120 per minute")
def actions_list():
    query = actions_for(current_user).outerjoin(
        Client, ActionItem.client_id == Client.id
    )
    status = request.args.get("status", "").strip()
    if status:
        query = query.filter(ActionItem.status == status)
    client_id = request.args.get("client_id", "").strip()
    if client_id:
        query = query.filter(ActionItem.client_id == client_id)
    priority = request.args.get("priority", "").strip()
    if priority:
        if priority not in {"high", "medium", "low"}:
            return jsonify(error="Prioridade inválida.", code="invalid_payload"), 422
        query = query.filter(ActionItem.priority == priority)
    owner = request.args.get("owner", "").strip()
    if owner == "mine":
        query = query.filter(ActionItem.owner_id == current_user.id)
    elif owner:
        query = query.filter(ActionItem.owner_id == owner)
    search = request.args.get("q", "").strip()
    if search:
        pattern = f"%{search[:100]}%"
        query = query.filter(
            or_(
                ActionItem.title.ilike(pattern),
                ActionItem.rationale.ilike(pattern),
                Client.name.ilike(pattern),
            )
        )
    if request.args.get("overdue") == "true":
        query = query.filter(
            ActionItem.due_at < utcnow(),
            ActionItem.status.notin_([ActionStatus.COMPLETED.value, ActionStatus.DISMISSED.value]),
        )
    sort = request.args.get("sort", "due_at")
    if sort == "created_at":
        query = query.order_by(ActionItem.created_at.desc())
    elif sort == "updated_at":
        query = query.order_by(ActionItem.updated_at.desc())
    else:
        query = query.order_by(ActionItem.due_at.asc(), ActionItem.created_at.desc())
    try:
        page = max(1, int(request.args.get("page", 1)))
        per_page = min(100, max(1, int(request.args.get("per_page", 50))))
    except ValueError:
        return jsonify(error="Paginação inválida.", code="invalid_payload"), 422
    total = query.count()
    items = query.offset((page - 1) * per_page).limit(per_page).all()
    return jsonify(
        items=[action_json(item) for item in items],
        pagination={
            "page": page,
            "per_page": per_page,
            "total": total,
            "pages": (total + per_page - 1) // per_page,
        },
    )


@api_bp.get("/actions/<action_id>")
@login_required
def action_detail(action_id: str):
    action = db.session.get(ActionItem, action_id)
    if action is None or not can_manage(current_user, action):
        return jsonify(error="Recurso não encontrado.", code="not_found"), 404
    return jsonify(action_json(action, include_events=True))


@api_bp.post("/actions/<action_id>/comments")
@login_required
@sensitive_operation
def comment_action(action_id: str):
    action = db.session.get(ActionItem, action_id)
    if action is None or not can_manage(current_user, action):
        return jsonify(error="Recurso não encontrado.", code="not_found"), 404
    payload = request.get_json(silent=True) or {}
    comment = str(payload.get("comment") or "").strip()
    if len(comment) < 2 or len(comment) > 4_000:
        return jsonify(error="Comentário inválido.", code="invalid_payload"), 422
    if int(payload.get("expected_version", -1)) != action.version:
        return jsonify(error="A ação foi alterada por outra pessoa.", code="version_conflict"), 409
    action.version += 1
    record_event(action, current_user.id, "commented", comment=comment)
    audit("ACTION_COMMENTED", "action_item", action.id)
    db.session.commit()
    return jsonify(action_json(action, include_events=True)), 201


@api_bp.patch("/actions/<action_id>")
@login_required
@sensitive_operation
def update_action(action_id: str):
    action = db.session.get(ActionItem, action_id)
    if action is None or not can_manage(current_user, action):
        return jsonify(error="Recurso não encontrado.", code="not_found"), 404
    payload = request.get_json(silent=True) or {}
    if int(payload.get("expected_version", -1)) != action.version:
        return jsonify(error="A ação foi alterada por outra pessoa.", code="version_conflict"), 409
    is_manager = current_user.role in {Role.ADMIN, Role.MANAGER, Role.ANALYST}
    requested_status = str(payload.get("status") or action.status)
    allowed_statuses = {item.value for item in ActionStatus}
    if requested_status not in allowed_statuses:
        return jsonify(error="Estado inválido.", code="invalid_payload"), 422
    if not is_manager and requested_status not in {
        ActionStatus.IN_PROGRESS.value,
        ActionStatus.COMPLETED.value,
    }:
        return jsonify(error="Recurso não encontrado.", code="not_found"), 404
    allowed_transitions = {
        ActionStatus.PROPOSED.value: {ActionStatus.PROPOSED.value},
        ActionStatus.ACCEPTED.value: {
            ActionStatus.ACCEPTED.value,
            ActionStatus.IN_PROGRESS.value,
            ActionStatus.COMPLETED.value,
        },
        ActionStatus.IN_PROGRESS.value: {
            ActionStatus.IN_PROGRESS.value,
            ActionStatus.COMPLETED.value,
        },
        ActionStatus.COMPLETED.value: {ActionStatus.COMPLETED.value},
        ActionStatus.DISMISSED.value: {ActionStatus.DISMISSED.value},
    }
    if requested_status not in allowed_transitions.get(action.status, set()):
        return jsonify(error="Transição inválida.", code="invalid_transition"), 409
    follow_up_action = None
    follow_up_data = payload.get("follow_up")
    prepared_follow_up = None
    if requested_status == ActionStatus.COMPLETED.value:
        outcome = str(payload.get("outcome") or action.outcome or "").strip()
        if len(outcome) < 5:
            return jsonify(error="Informe o resultado da ação.", code="outcome_required"), 422
        if follow_up_data is not None:
            if not isinstance(follow_up_data, dict):
                return jsonify(error="Acompanhamento inválido.", code="invalid_follow_up"), 422
            follow_up_title = str(follow_up_data.get("title") or "").strip()
            follow_up_owner = str(follow_up_data.get("owner_id") or "").strip()
            try:
                follow_up_due = datetime.fromisoformat(
                    str(follow_up_data.get("due_at") or "").replace("Z", "+00:00")
                )
            except ValueError:
                return (
                    jsonify(
                        error="Defina a data do acompanhamento.",
                        code="invalid_follow_up",
                    ),
                    422,
                )
            if follow_up_due.tzinfo is None:
                follow_up_due = follow_up_due.replace(tzinfo=UTC)
            follow_up_calendar = str(follow_up_data.get("calendar_provider") or "").strip() or None
            prepared_follow_up = (
                follow_up_title,
                follow_up_owner,
                follow_up_due,
                follow_up_calendar,
            )
        action.outcome = outcome[:4_000]
        action.customer_notified = bool(payload.get("customer_notified", False))
        action.completed_at = utcnow()
    if is_manager:
        if "title" in payload:
            title = str(payload.get("title") or "").strip()
            if not title:
                return jsonify(error="Título inválido.", code="invalid_payload"), 422
            action.title = title[:500]
        if "owner_id" in payload:
            owner_id = payload.get("owner_id") or None
            owner = db.session.get(User, owner_id) if owner_id else None
            if owner_id and (owner is None or owner.organization_id != action.organization_id):
                return jsonify(error="Responsável inválido.", code="invalid_payload"), 422
            action.owner_id = owner_id
        if "due_at" in payload:
            try:
                action.due_at = (
                    datetime.fromisoformat(str(payload["due_at"]).replace("Z", "+00:00"))
                    if payload.get("due_at")
                    else None
                )
            except ValueError:
                return jsonify(error="Prazo inválido.", code="invalid_payload"), 422
    previous_status = action.status
    action.status = requested_status
    action.version += 1
    record_event(
        action,
        current_user.id,
        "updated",
        previous_status=previous_status,
        status=action.status,
        customer_notified=(
            action.customer_notified if requested_status == ActionStatus.COMPLETED.value else None
        ),
    )
    calendar_event = None
    if prepared_follow_up is not None:
        try:
            follow_up_action = create_follow_up(
                action,
                current_user,
                title=prepared_follow_up[0],
                owner_id=prepared_follow_up[1],
                due_at=prepared_follow_up[2],
            )
            if prepared_follow_up[3]:
                calendar_event = create_event(
                    current_user,
                    prepared_follow_up[3],
                    title=prepared_follow_up[0],
                    starts_at=prepared_follow_up[2],
                )
                record_event(
                    follow_up_action,
                    current_user.id,
                    "calendar_scheduled",
                    **calendar_event,
                )
        except CalendarOAuthError:
            return jsonify(
                error="Não foi possível adicionar o acompanhamento ao calendário.",
                code="calendar_event_failed",
            ), 422
        except ValueError:
            return jsonify(error="Defina o acompanhamento completo.", code="invalid_follow_up"), 422
    sync_plan_status(action.plan_id, current_user.id)
    audit(
        "ACTION_UPDATED",
        "action_item",
        action.id,
        status=action.status,
        follow_up_action_id=follow_up_action.id if follow_up_action else None,
    )
    db.session.commit()
    result = action_json(action)
    if follow_up_action is not None:
        result["follow_up"] = action_json(follow_up_action)
    if calendar_event is not None:
        result["calendar_event"] = calendar_event
    return jsonify(result)


@api_bp.post("/actions/<action_id>/decision")
@role_required(Role.ADMIN, Role.MANAGER, Role.ANALYST)
@sensitive_operation
def decide_action(action_id: str):
    action = db.session.get(ActionItem, action_id)
    if action is None or not can_manage(current_user, action):
        return jsonify(error="Recurso não encontrado.", code="not_found"), 404
    if action.status != ActionStatus.PROPOSED.value:
        return jsonify(error="A ação já foi decidida.", code="invalid_state"), 409
    payload = request.get_json(silent=True) or {}
    if int(payload.get("expected_version", -1)) != action.version:
        return jsonify(error="A ação foi alterada por outra pessoa.", code="version_conflict"), 409
    decision = str(payload.get("decision") or "")
    if decision == "accept":
        title = str(payload.get("title") or action.title).strip()
        if not title:
            return jsonify(error="Título inválido.", code="invalid_payload"), 422
        owner_id = payload.get("owner_id") or action.owner_id
        owner = db.session.get(User, owner_id) if owner_id else None
        owner_allowed = bool(
            owner
            and owner.is_active_record
            and owner.role in {Role.ADMIN, Role.MANAGER, Role.ANALYST}
            and (
                current_user.role == Role.ADMIN
                or (current_user.role == Role.MANAGER and can_manage_user(current_user, owner))
                or owner.id == current_user.id
            )
        )
        if not owner_allowed:
            return jsonify(error="Defina um responsável válido.", code="owner_required"), 422
        if "due_at" in payload:
            try:
                action.due_at = (
                    datetime.fromisoformat(str(payload["due_at"]).replace("Z", "+00:00"))
                    if payload.get("due_at")
                    else None
                )
            except ValueError:
                return jsonify(error="Prazo inválido.", code="invalid_payload"), 422
        if action.due_at is None:
            return (
                jsonify(
                    error="Defina um prazo para confirmar a ação.",
                    code="due_at_required",
                ),
                422,
            )
        action.status = ActionStatus.ACCEPTED.value
        action.title = title[:500]
        action.accepted_by_id = current_user.id
        action.accepted_at = utcnow()
        action.owner_id = owner_id
        event_type = "accepted"
    elif decision == "dismiss":
        reason = str(payload.get("reason") or "").strip()
        if len(reason) < 5:
            return jsonify(error="Justificativa obrigatória.", code="reason_required"), 422
        action.status = ActionStatus.DISMISSED.value
        action.dismissal_reason = reason[:4_000]
        event_type = "dismissed"
    else:
        return jsonify(error="Decisão inválida.", code="invalid_payload"), 422
    action.version += 1
    record_event(
        action,
        current_user.id,
        event_type,
        owner_id=action.owner_id,
        due_at=action.due_at.isoformat() if action.due_at else None,
    )
    sync_plan_status(action.plan_id, current_user.id)
    audit("ACTION_DECIDED", "action_item", action.id, decision=decision)
    db.session.commit()
    return jsonify(action_json(action))


@api_bp.post("/analyses/<analysis_id>/feedback")
@login_required
@sensitive_operation
def analysis_feedback(analysis_id: str):
    analysis = db.session.get(Analysis, analysis_id)
    if (
        not can_access_analysis(current_user, analysis)
        or analysis.status != AnalysisStatus.SUCCEEDED
    ):
        return jsonify(error="Recurso não encontrado.", code="not_found"), 404
    payload = request.get_json(silent=True) or {}
    try:
        rating = int(payload.get("usefulness_rating"))
    except (TypeError, ValueError):
        rating = 0
    accuracy = str(payload.get("factual_accuracy") or "")
    if rating not in range(1, 6) or accuracy not in {"correct", "minor_issues", "major_issues"}:
        return jsonify(error="Feedback inválido.", code="invalid_payload"), 422
    if not isinstance(payload.get("action_useful"), bool):
        return jsonify(error="Feedback inválido.", code="invalid_payload"), 422
    correction = str(payload.get("correction") or "").strip()
    if accuracy != "correct" and len(correction) < 3:
        return jsonify(error="Descreva a correção necessária.", code="correction_required"), 422
    item = AnalysisFeedback.query.filter_by(
        analysis_id=analysis.id, reviewer_id=current_user.id
    ).first()
    if item is None:
        item = AnalysisFeedback(
            organization_id=current_user.organization_id,
            analysis_id=analysis.id,
            reviewer_id=current_user.id,
        )
        db.session.add(item)
    item.usefulness_rating = rating
    item.factual_accuracy = accuracy
    item.action_useful = payload["action_useful"]
    item.correction = correction[:4_000] or None
    item.comment = str(payload.get("comment") or "").strip()[:4_000] or None
    audit("ANALYSIS_FEEDBACK_RECORDED", "analysis", analysis.id, rating=rating)
    db.session.commit()
    return jsonify(id=item.id, saved=True, feedback=feedback_json(item))


@api_bp.get("/analyses/<analysis_id>/feedback")
@login_required
def own_analysis_feedback(analysis_id: str):
    analysis = db.session.get(Analysis, analysis_id)
    if not can_access_analysis(current_user, analysis):
        return jsonify(error="Recurso não encontrado.", code="not_found"), 404
    item = AnalysisFeedback.query.filter_by(
        analysis_id=analysis_id, reviewer_id=current_user.id
    ).first()
    return jsonify(feedback=feedback_json(item) if item else None)


@api_bp.get("/clients/<client_id>/timeline")
@login_required
def client_timeline(client_id: str):
    client = db.session.get(Client, client_id)
    if not can_access_client(current_user, client):
        return jsonify(error="Recurso não encontrado.", code="not_found"), 404
    meetings = (
        Meeting.query.filter_by(client_id=client.id).order_by(Meeting.occurred_at.desc()).all()
    )
    actions = (
        ActionItem.query.filter_by(client_id=client.id).order_by(ActionItem.created_at.desc()).all()
    )
    return jsonify(
        client={"id": client.id, "external_id": client.external_id, "name": client.name},
        meetings=[meeting_json(item) for item in meetings],
        actions=[action_json(item) for item in actions],
    )


@api_bp.get("/pov/metrics")
@role_required(Role.ADMIN)
def pov_metrics():
    actions = ActionItem.query.all()
    feedback = AnalysisFeedback.query.all()
    executions = AIExecution.query.filter(AIExecution.estimated_cost_microunits.is_not(None)).all()
    accepted = sum(
        item.status
        in {
            ActionStatus.ACCEPTED.value,
            ActionStatus.IN_PROGRESS.value,
            ActionStatus.COMPLETED.value,
        }
        for item in actions
    )
    accepted_statuses = {
        ActionStatus.ACCEPTED.value,
        ActionStatus.IN_PROGRESS.value,
        ActionStatus.COMPLETED.value,
    }
    decided = sum(
        item.status in accepted_statuses | {ActionStatus.DISMISSED.value} for item in actions
    )
    decision_events = ActionEvent.query.filter(
        ActionEvent.event_type.in_(["accepted", "dismissed"])
    ).all()
    actions_by_id = {item.id: item for item in actions}
    review_minutes = []
    for event in decision_events:
        source = actions_by_id.get(event.action_item_id)
        if source is not None:
            review_minutes.append(
                max(0, (event.occurred_at - source.created_at).total_seconds() / 60)
            )
    requests = KanbanRequest.query.all()
    response_hours = [
        max(0, (item.responded_at - item.created_at).total_seconds() / 3600)
        for item in requests
        if item.responded_at
    ]
    return jsonify(
        actions={
            "proposed": sum(item.status == ActionStatus.PROPOSED.value for item in actions),
            "accepted": sum(item.status == ActionStatus.ACCEPTED.value for item in actions),
            "in_progress": sum(item.status == ActionStatus.IN_PROGRESS.value for item in actions),
            "completed": sum(item.status == ActionStatus.COMPLETED.value for item in actions),
            "dismissed": sum(item.status == ActionStatus.DISMISSED.value for item in actions),
            "acceptance_rate": round(accepted / len(actions), 4) if actions else None,
            "decision_rate": round(decided / len(actions), 4) if actions else None,
            "dismissal_rate": (
                round(
                    sum(item.status == ActionStatus.DISMISSED.value for item in actions) / decided,
                    4,
                )
                if decided
                else None
            ),
            "average_review_minutes": (
                round(sum(review_minutes) / len(review_minutes), 2) if review_minutes else None
            ),
            "overdue": sum(action_json(item)["overdue"] for item in actions),
            "follow_ups_created": sum(bool(item.parent_action_id) for item in actions),
        },
        feedback={
            "count": len(feedback),
            "average_usefulness": (
                round(sum(item.usefulness_rating for item in feedback) / len(feedback), 2)
                if feedback
                else None
            ),
        },
        routing={
            "total": len(requests),
            "answered": sum(item.status == KanbanRequestStatus.ANSWERED.value for item in requests),
            "average_response_hours": (
                round(sum(response_hours) / len(response_hours), 2) if response_hours else None
            ),
        },
        ai_cost={
            "currency": executions[0].cost_currency if executions else "USD",
            "total_microunits": sum(item.estimated_cost_microunits or 0 for item in executions),
            "average_per_execution_microunits": (
                round(
                    sum(item.estimated_cost_microunits or 0 for item in executions)
                    / len(executions)
                )
                if executions
                else None
            ),
        },
    )


@api_bp.post("/telemetry/events")
@login_required
@limiter.limit("120 per minute")
def product_event():
    payload = request.get_json(silent=True) or {}
    event_name = str(payload.get("event_name") or "")
    allowed = {"result_opened", "evidence_opened", "action_accepted", "action_completed"}
    if event_name not in allowed:
        return jsonify(error="Evento inválido.", code="invalid_payload"), 422
    db.session.add(
        ProductEvent(
            organization_id=current_user.organization_id,
            user_id=current_user.id,
            event_name=event_name,
            target_type=str(payload.get("target_type") or "")[:50] or None,
            target_id=str(payload.get("target_id") or "")[:100] or None,
        )
    )
    db.session.commit()
    return "", 204


@api_bp.get("/privacy/export")
@login_required
@limiter.limit("10 per hour")
def privacy_export():
    submissions = MeetingSubmission.query.filter_by(created_by_id=current_user.id).all()
    meetings = Meeting.query.filter_by(created_by_id=current_user.id).all()
    feedback = AnalysisFeedback.query.filter_by(reviewer_id=current_user.id).all()
    actions = ActionItem.query.filter_by(owner_id=current_user.id).all()
    kanban_requests = KanbanRequest.query.filter_by(created_by_id=current_user.id).all()
    payload = {
        "subject": {
            "id": current_user.id,
            "email": current_user.email,
            "name": current_user.name,
            "role": current_user.role.value,
            "organization_id": current_user.organization_id,
            "auth_source": current_user.auth_source,
        },
        "meeting_ids": [item.id for item in meetings],
        "submission_ids": [item.id for item in submissions],
        "action_ids": [item.id for item in actions],
        "kanban_request_ids": [item.id for item in kanban_requests],
        "feedback": [
            {
                "analysis_id": item.analysis_id,
                "usefulness_rating": item.usefulness_rating,
                "factual_accuracy": item.factual_accuracy,
                "action_useful": item.action_useful,
                "comment": item.comment,
                "correction": item.correction,
            }
            for item in feedback
        ],
        "submissions": [
            {
                "id": item.id,
                "filename": item.original_filename,
                "status": item.status,
                "created_at": item.created_at.isoformat(),
                "original_deleted": item.original_deleted_at is not None,
            }
            for item in submissions
        ],
        "generated_at": utcnow().isoformat(),
        "content_included": False,
        "corporate_meeting_content_included": False,
    }
    audit("PRIVACY_EXPORT_GENERATED", "user", current_user.id)
    db.session.commit()
    response = jsonify(payload)
    response.headers["Content-Disposition"] = 'attachment; filename="meetmind-privacy-export.json"'
    return response


@api_bp.post("/privacy/requests")
@login_required
@sensitive_operation
@limiter.limit("3 per day")
def create_privacy_request():
    payload = request.get_json(silent=True) or {}
    request_type = str(payload.get("type") or "")
    reason = str(payload.get("reason") or "").strip()[:2_000]
    if request_type not in {"export", "deletion"} or len(reason) < 5:
        return jsonify(error="Tipo ou justificativa inválidos.", code="invalid_payload"), 422
    existing = PrivacyRequest.query.filter_by(
        requested_by_id=current_user.id,
        request_type=request_type,
        status="pending",
    ).first()
    if existing:
        return jsonify(id=existing.id, status=existing.status), 200
    privacy_request = PrivacyRequest(
        organization_id=current_user.organization_id,
        requested_by_id=current_user.id,
        request_type=request_type,
        reason=reason,
    )
    db.session.add(privacy_request)
    audit("PRIVACY_REQUEST_CREATED", "privacy_request", privacy_request.id, type=request_type)
    db.session.commit()
    return jsonify(id=privacy_request.id, status=privacy_request.status), 201


@api_bp.get("/dashboard")
@login_required
@limiter.limit("120 per minute")
def dashboard():
    visible_meetings = meetings_for(current_user)
    meeting_ids = visible_meetings.with_entities(Meeting.id).subquery()
    total = visible_meetings.count()
    approved = visible_meetings.filter(Meeting.approved_for_external_ai.is_(True)).count()
    succeeded = Analysis.query.filter(
        Analysis.meeting_id.in_(db.select(meeting_ids.c.id)),
        Analysis.status == AnalysisStatus.SUCCEEDED,
    ).count()
    failed = Analysis.query.filter(
        Analysis.meeting_id.in_(db.select(meeting_ids.c.id)),
        Analysis.status == AnalysisStatus.FAILED,
    ).count()
    nps_values = [
        float(item.metadata_json["nota_nps"])
        for item in visible_meetings.all()
        if item.metadata_json.get("nota_nps") is not None
    ]
    return jsonify(
        kpis={
            "meetings": total,
            "approved_for_ai": approved,
            "analyses_succeeded": succeeded,
            "analyses_failed": failed,
            "average_nps": round(sum(nps_values) / len(nps_values), 2) if nps_values else None,
        }
    )


def _dashboard_limit(default: int = 8) -> int:
    try:
        return min(20, max(1, int(request.args.get("limit", default))))
    except ValueError:
        return default


@api_bp.get("/dashboard/briefing")
@login_required
@limiter.limit("120 per minute")
def dashboard_briefing():
    return jsonify(dashboard_briefing_data(current_user))


@api_bp.get("/dashboard/daily-briefing")
@login_required
@limiter.limit("120 per minute")
def dashboard_daily_briefing():
    now = utcnow()
    calendar_items = events_for_user(current_user)
    task_items = []
    for item in dashboard_focus_data(current_user, 20).get("items", []):
        if not item.get("due_at"):
            continue
        task_items.append(
            {
                "id": f"task-{item['id']}",
                "type": "task",
                "title": item["title"],
                "starts_at": item["due_at"],
                "ends_at": None,
                "all_day": False,
                "detail": item.get("client") or item.get("status_label") or "Prazo interno",
                "critical": bool(item.get("overdue") or item.get("priority") == "high"),
                "source": "meetmind",
                "url": item.get("source_url"),
            }
        )
    items = [*calendar_items, *task_items]
    items.sort(key=lambda item: item["starts_at"])
    return jsonify(generated_at=now.isoformat(), items=items)


@api_bp.get("/dashboard/timeline")
@login_required
@limiter.limit("120 per minute")
def dashboard_timeline():
    return jsonify(dashboard_timeline_data(current_user, _dashboard_limit()))


@api_bp.get("/dashboard/focus")
@login_required
@limiter.limit("120 per minute")
def dashboard_focus():
    return jsonify(dashboard_focus_data(current_user, _dashboard_limit()))


@api_bp.post("/dashboard/seen")
@login_required
def dashboard_seen():
    current_user.last_briefing_seen_at = utcnow()
    audit("DASHBOARD_UPDATES_SEEN", "user", current_user.id)
    db.session.commit()
    return jsonify(seen_at=current_user.last_briefing_seen_at.isoformat())


@api_bp.post("/kanban/requests")
@role_required(Role.ADMIN, Role.MANAGER, Role.ANALYST)
@sensitive_operation
@limiter.limit(sensitive_request_limit)
def create_kanban_dispatch():
    payload = request.get_json(silent=True) or {}
    request_type = str(payload.get("request_type") or "").strip()
    source_action_id = str(payload.get("source_action_id") or "").strip()
    try:
        item = create_kanban_request(
            current_user,
            request_type=request_type,
            source_action_id=source_action_id or None,
            note=payload.get("note"),
            client_id=str(payload.get("client_id") or "").strip() or None,
            source_channel=str(payload.get("source_channel") or "").strip() or None,
        )
    except LookupError:
        return jsonify(error="Origem ou cliente não encontrado.", code="not_found"), 404
    except ValueError as error:
        messages = {
            "invalid_request_type": "Selecione uma necessidade válida.",
            "invalid_note": "A observação deve ter no máximo 2.000 caracteres.",
            "note_required": "Descreva o pedido recebido do cliente.",
            "invalid_source_channel": "Selecione o canal de origem.",
        }
        return jsonify(error=messages.get(str(error), "Dados inválidos."), code=str(error)), 422
    audit(
        "KANBAN_REQUEST_CREATED",
        "kanban_request",
        item.id,
        department=item.department,
        request_type=item.request_type,
        source_action_id=item.source_action_id,
        source_kind=item.source_kind,
        source_channel=item.source_channel,
    )
    db.session.commit()
    return jsonify(kanban_request_json(item)), 201


@api_bp.get("/kanban/requests")
@login_required
@limiter.limit("120 per minute")
def kanban_requests_list():
    status = str(request.args.get("status") or "").strip()
    allowed = {item.value for item in KanbanRequestStatus}
    if status and status not in allowed:
        return jsonify(error="Estado inválido.", code="invalid_payload"), 422
    return jsonify(
        items=[
            kanban_request_json(item) for item in list_kanban_requests(current_user, status=status)
        ]
    )


@api_bp.patch("/kanban/requests/<request_id>")
@role_required(Role.ADMIN, Role.MANAGER, Role.ANALYST)
@sensitive_operation
def kanban_request_update(request_id: str):
    item = db.session.get(KanbanRequest, request_id)
    if not can_manage_request(current_user, item):
        return jsonify(error="Recurso não encontrado.", code="not_found"), 404
    payload = request.get_json(silent=True) or {}
    assigned_to_id = str(payload.get("assigned_to_id") or item.assigned_to_id or "").strip() or None
    due_at = item.due_at
    if "due_at" in payload and not payload.get("due_at"):
        due_at = None
    elif payload.get("due_at"):
        try:
            due_at = datetime.fromisoformat(str(payload["due_at"]).replace("Z", "+00:00"))
        except ValueError:
            return jsonify(error="Prazo inválido.", code="invalid_payload"), 422
        if due_at.tzinfo is None:
            due_at = due_at.replace(tzinfo=UTC)
    try:
        updated = update_kanban_request(
            item,
            current_user,
            expected_version=int(payload.get("expected_version", -1)),
            status=str(payload.get("status") or item.status),
            assigned_to_id=assigned_to_id,
            due_at=due_at,
            response=payload.get("response"),
        )
    except LookupError:
        return jsonify(error="Recurso não encontrado.", code="not_found"), 404
    except RuntimeError:
        return (
            jsonify(
                error="O encaminhamento foi atualizado por outra pessoa.",
                code="version_conflict",
            ),
            409,
        )
    except (TypeError, ValueError) as error:
        messages = {
            "invalid_transition": "Transição inválida.",
            "invalid_assignee": "Responsável inválido.",
            "assignment_required": "Defina responsável e prazo antes de iniciar.",
            "response_required": "Informe a resposta antes de marcar como respondido.",
            "customer_return_required": (
                "Registre o retorno ao cliente ou justifique por que ele não é necessário."
            ),
        }
        return jsonify(error=messages.get(str(error), "Dados inválidos."), code=str(error)), 422
    audit(
        "KANBAN_REQUEST_UPDATED",
        "kanban_request",
        updated.id,
        status=updated.status,
        assigned_to_id=updated.assigned_to_id,
    )
    db.session.commit()
    return jsonify(kanban_request_json(updated))
