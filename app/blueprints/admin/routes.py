from __future__ import annotations

import math

from flask import Blueprint, current_app, jsonify, request
from flask_login import current_user
from pydantic import BaseModel, ConfigDict, EmailStr, Field, ValidationError
from sqlalchemy import func

from app.extensions import db, limiter, sensitive_request_limit
from app.models import (
    AIExecution,
    Organization,
    OutboxEvent,
    PrivacyRequest,
    Role,
    SecurityEvent,
    Team,
    TeamsConnection,
    TeamsTranscriptReceipt,
    User,
    utcnow,
)
from app.security import audit, redis_client, role_required, sensitive_operation
from app.services.outbox import add as add_outbox_event
from app.services.outbox import dispatch as dispatch_outbox_event
from app.services.security_events import group_security_events

admin_bp = Blueprint("admin", __name__, url_prefix="/api/v1/admin")


class UserInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    email: EmailStr
    name: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=12, max_length=200)
    role: Role
    team_id: str | None = None


class TeamInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=2, max_length=120)
    manager_id: str | None = None
    is_active: bool = True


@admin_bp.get("/users")
@role_required(Role.ADMIN)
@limiter.limit("120 per minute")
def users():
    return jsonify(
        items=[
            {
                "id": user.id,
                "email": user.email,
                "name": user.name,
                "role": user.role.value,
                "team_id": user.team_id,
                "active": user.is_active_record,
                "request_limit_per_10_minutes": user.request_limit_per_10_minutes,
            }
            for user in User.query.filter_by(
                organization_id=current_user.organization_id
            ).order_by(User.name).all()
        ]
    )


@admin_bp.post("/users")
@role_required(Role.ADMIN)
@sensitive_operation
@limiter.limit(sensitive_request_limit)
def create_user():
    if not request.is_json:
        return jsonify(error="Content-Type application/json é obrigatório."), 415
    try:
        payload = UserInput.model_validate(request.get_json())
    except ValidationError as error:
        return jsonify(error="Dados inválidos.", fields=error.errors(include_url=False)), 422
    if User.query.filter_by(
        organization_id=current_user.organization_id,
        email=str(payload.email).lower(),
    ).first():
        return jsonify(error="Usuário já cadastrado.", code="conflict"), 409
    team = db.session.get(Team, payload.team_id) if payload.team_id else None
    if payload.team_id and (
        team is None or team.organization_id != current_user.organization_id
    ):
        return jsonify(error="Equipe inválida.", code="invalid_team"), 422
    user = User(
        email=str(payload.email).lower(),
        name=payload.name,
        role=payload.role,
        team_id=team.id if team else None,
        organization_id=current_user.organization_id,
    )
    user.set_password(payload.password)
    db.session.add(user)
    db.session.flush()
    audit("USER_CREATED", "user", user.id, role=user.role.value)
    db.session.commit()
    return jsonify(id=user.id), 201


@admin_bp.patch("/users/<user_id>")
@role_required(Role.ADMIN)
@sensitive_operation
def update_user(user_id: str):
    user = db.session.get(User, user_id)
    if user is None or user.organization_id != current_user.organization_id:
        return jsonify(error="Usuário não encontrado.", code="not_found"), 404
    payload = request.get_json(silent=True) or {}
    if set(payload) - {"name", "role", "team_id", "active"}:
        return jsonify(error="Campos desconhecidos.", code="invalid_payload"), 422
    if "name" in payload:
        name = str(payload["name"] or "").strip()
        if not 1 <= len(name) <= 200:
            return jsonify(error="Nome inválido.", code="invalid_payload"), 422
        user.name = name
    if "role" in payload:
        try:
            user.role = Role(str(payload["role"]))
        except ValueError:
            return jsonify(error="Cargo inválido.", code="invalid_payload"), 422
    if "team_id" in payload:
        team_id = str(payload["team_id"] or "").strip() or None
        team = db.session.get(Team, team_id) if team_id else None
        if team_id and (
            team is None or team.organization_id != current_user.organization_id
        ):
            return jsonify(error="Equipe inválida.", code="invalid_team"), 422
        user.team_id = team_id
    if "active" in payload:
        if user.id == current_user.id and not bool(payload["active"]):
            return jsonify(error="Você não pode desativar seu próprio usuário."), 422
        user.is_active_record = bool(payload["active"])
        if not user.is_active_record:
            user.session_version += 1
    audit("USER_UPDATED", "user", user.id, fields=sorted(payload))
    db.session.commit()
    return jsonify(
        id=user.id,
        name=user.name,
        role=user.role.value,
        team_id=user.team_id,
        active=user.is_active_record,
    )


def _team_json(team: Team) -> dict:
    manager = db.session.get(User, team.manager_id) if team.manager_id else None
    return {
        "id": team.id,
        "name": team.name,
        "manager_id": team.manager_id,
        "manager": manager.name if manager else None,
        "active": team.is_active,
        "members": User.query.filter_by(
            organization_id=current_user.organization_id, team_id=team.id
        ).count(),
    }


@admin_bp.get("/teams")
@role_required(Role.ADMIN)
def teams():
    return jsonify(
        items=[
            _team_json(item)
            for item in Team.query.filter_by(
                organization_id=current_user.organization_id
            ).order_by(Team.name)
        ]
    )


@admin_bp.post("/teams")
@role_required(Role.ADMIN)
@sensitive_operation
def create_team():
    try:
        payload = TeamInput.model_validate(request.get_json(silent=True) or {})
    except ValidationError as error:
        return jsonify(error="Dados inválidos.", fields=error.errors(include_url=False)), 422
    manager = db.session.get(User, payload.manager_id) if payload.manager_id else None
    if payload.manager_id and (
        manager is None
        or manager.organization_id != current_user.organization_id
        or manager.role != Role.MANAGER
    ):
        return jsonify(error="Coordenador inválido.", code="invalid_manager"), 422
    team = Team(
        organization_id=current_user.organization_id,
        name=payload.name,
        manager_id=manager.id if manager else None,
        is_active=payload.is_active,
    )
    db.session.add(team)
    db.session.flush()
    audit("TEAM_CREATED", "team", team.id, manager_id=team.manager_id)
    db.session.commit()
    return jsonify(_team_json(team)), 201


@admin_bp.patch("/teams/<team_id>")
@role_required(Role.ADMIN)
@sensitive_operation
def update_team(team_id: str):
    team = db.session.get(Team, team_id)
    if team is None or team.organization_id != current_user.organization_id:
        return jsonify(error="Equipe não encontrada.", code="not_found"), 404
    try:
        payload = TeamInput.model_validate(request.get_json(silent=True) or {})
    except ValidationError as error:
        return jsonify(error="Dados inválidos.", fields=error.errors(include_url=False)), 422
    manager = db.session.get(User, payload.manager_id) if payload.manager_id else None
    if payload.manager_id and (
        manager is None
        or manager.organization_id != current_user.organization_id
        or manager.role != Role.MANAGER
    ):
        return jsonify(error="Coordenador inválido.", code="invalid_manager"), 422
    team.name = payload.name
    team.manager_id = manager.id if manager else None
    team.is_active = payload.is_active
    audit("TEAM_UPDATED", "team", team.id, manager_id=team.manager_id)
    db.session.commit()
    return jsonify(_team_json(team))


@admin_bp.get("/organization")
@role_required(Role.ADMIN)
def organization_detail():
    organization = db.session.get(Organization, current_user.organization_id)
    return jsonify(
        id=organization.id,
        slug=organization.slug,
        name=organization.name,
        retention_days=organization.retention_days,
        external_ai_enabled=organization.external_ai_enabled,
        legal_hold_enabled=organization.legal_hold_enabled,
        oidc_tenant_id=organization.oidc_tenant_id,
    )


@admin_bp.patch("/organization")
@role_required(Role.ADMIN)
@sensitive_operation
def update_organization():
    payload = request.get_json(silent=True) or {}
    if set(payload) - {
        "retention_days",
        "external_ai_enabled",
        "legal_hold_enabled",
        "oidc_tenant_id",
    }:
        return jsonify(error="Campos desconhecidos.", code="invalid_payload"), 422
    organization = db.session.get(Organization, current_user.organization_id)
    if "retention_days" in payload:
        value = payload["retention_days"]
        if not isinstance(value, int) or not 1 <= value <= 3650:
            return jsonify(error="Retenção inválida.", code="invalid_payload"), 422
        organization.retention_days = value
    if "external_ai_enabled" in payload:
        if not isinstance(payload["external_ai_enabled"], bool):
            return jsonify(error="Configuração inválida.", code="invalid_payload"), 422
        organization.external_ai_enabled = payload["external_ai_enabled"]
    if "legal_hold_enabled" in payload:
        if not isinstance(payload["legal_hold_enabled"], bool):
            return jsonify(error="Configuração inválida.", code="invalid_payload"), 422
        organization.legal_hold_enabled = payload["legal_hold_enabled"]
    if "oidc_tenant_id" in payload:
        organization.oidc_tenant_id = str(payload["oidc_tenant_id"] or "").strip()[:100] or None
    audit(
        "ORGANIZATION_POLICY_UPDATED",
        "organization",
        organization.id,
        retention_days=organization.retention_days,
        external_ai_enabled=organization.external_ai_enabled,
    )
    db.session.commit()
    return organization_detail()


@admin_bp.get("/operations")
@role_required(Role.ADMIN)
@limiter.limit("60 per minute")
def operations():
    from rq import Queue
    from rq.registry import FailedJobRegistry, StartedJobRegistry

    result = {
        "redis": "unavailable",
        "queues": {},
        "outbox_pending": OutboxEvent.query.filter_by(status="pending").count(),
        "ai": {
            "executions": db.session.scalar(db.select(func.count(AIExecution.id))) or 0,
            "estimated_cost_microunits": db.session.scalar(
                db.select(func.coalesce(func.sum(AIExecution.estimated_cost_microunits), 0))
            )
            or 0,
        },
    }
    try:
        connection = redis_client()
        connection.ping()
        result["redis"] = "ok"
        for name in ("analyses", "screening", "notifications", "maintenance"):
            queue = Queue(name, connection=connection)
            result["queues"][name] = {
                "queued": len(queue),
                "started": StartedJobRegistry(name, connection=connection).count,
                "failed": FailedJobRegistry(name, connection=connection).count,
            }
    except Exception as error:
        current_app.logger.warning("operations_redis_unavailable type=%s", type(error).__name__)
    return jsonify(result)


@admin_bp.get("/integrations/teams/status")
@role_required(Role.ADMIN)
def teams_integration_status():
    connection = TeamsConnection.query.filter_by(
        organization_id=current_user.organization_id
    ).first()
    return jsonify(
        configured=bool(connection),
        provider_configured=bool(current_app.config["TEAMS_GRAPH_ENABLED"]),
        connection=(
            {
                "id": connection.id,
                "tenant_id": connection.tenant_id,
                "teams_app_id": connection.teams_app_id,
                "subscription_id": connection.subscription_id,
                "status": connection.status,
                "expires_at": (
                    connection.subscription_expires_at.isoformat()
                    if connection.subscription_expires_at
                    else None
                ),
                "last_error": connection.last_error,
            }
            if connection
            else None
        ),
        receipts={
            "received": TeamsTranscriptReceipt.query.filter_by(status="received").count(),
            "processed": TeamsTranscriptReceipt.query.filter_by(status="processed").count(),
            "failed": TeamsTranscriptReceipt.query.filter_by(status="failed").count(),
        },
    )


@admin_bp.post("/integrations/teams/subscription")
@role_required(Role.ADMIN)
@sensitive_operation
def configure_teams_subscription():
    payload = request.get_json(silent=True) or {}
    tenant_id = str(payload.get("tenant_id") or "").strip()[:100]
    teams_app_id = str(payload.get("teams_app_id") or "").strip()[:100]
    if not tenant_id or not teams_app_id:
        return jsonify(error="Tenant e aplicativo são obrigatórios.", code="invalid_payload"), 422
    connection = TeamsConnection.query.filter_by(
        organization_id=current_user.organization_id
    ).first()
    if connection is None:
        connection = TeamsConnection(
            organization_id=current_user.organization_id,
            tenant_id=tenant_id,
            teams_app_id=teams_app_id,
            status="pending",
        )
        db.session.add(connection)
    else:
        connection.tenant_id = tenant_id
        connection.teams_app_id = teams_app_id
        connection.status = "pending"
    db.session.flush()
    dispatch = add_outbox_event(
        organization_id=current_user.organization_id,
        queue_name="maintenance",
        task_name="renew_teams_subscription",
        job_id=f"teams-subscription-{connection.id}-{int(utcnow().timestamp())}",
        arguments=[connection.id],
        retry_intervals=[60, 300, 900],
    )
    audit("TEAMS_SUBSCRIPTION_REQUESTED", "teams_connection", connection.id)
    db.session.commit()
    delivered = dispatch_outbox_event(dispatch.id) if current_app.config["QUEUE_ENABLED"] else False
    return jsonify(id=connection.id, queued=delivered, status=connection.status), 202


@admin_bp.get("/privacy-requests")
@role_required(Role.ADMIN)
def privacy_requests():
    items = PrivacyRequest.query.order_by(PrivacyRequest.created_at.desc()).limit(200).all()
    return jsonify(
        items=[
            {
                "id": item.id,
                "requested_by_id": item.requested_by_id,
                "type": item.request_type,
                "status": item.status,
                "reason": item.reason,
                "resolution": item.resolution,
                "created_at": item.created_at.isoformat(),
                "completed_at": item.completed_at.isoformat() if item.completed_at else None,
                "approved_at": item.approved_at.isoformat() if item.approved_at else None,
            }
            for item in items
        ]
    )


@admin_bp.patch("/privacy-requests/<request_id>")
@role_required(Role.ADMIN)
@sensitive_operation
def resolve_privacy_request(request_id: str):
    item = db.session.get(PrivacyRequest, request_id)
    payload = request.get_json(silent=True) or {}
    status = payload.get("status")
    resolution = str(payload.get("resolution") or "").strip()[:2_000]
    if item is None or status != "rejected" or len(resolution) < 5:
        return jsonify(error="Solicitação ou resolução inválida.", code="invalid_payload"), 422
    item.status = status
    item.resolution = resolution
    item.completed_at = utcnow()
    audit("PRIVACY_REQUEST_RESOLVED", "privacy_request", item.id, status=status)
    db.session.commit()
    return jsonify(id=item.id, status=item.status)


@admin_bp.post("/privacy-requests/<request_id>/approve")
@role_required(Role.ADMIN)
@sensitive_operation
def approve_privacy_request(request_id: str):
    item = db.session.get(PrivacyRequest, request_id)
    if item is None or item.request_type != "deletion" or item.status != "pending":
        return jsonify(error="Solicitação inválida.", code="invalid_state"), 409
    organization = db.session.get(Organization, current_user.organization_id)
    if organization.legal_hold_enabled:
        return jsonify(error="Organização em retenção legal.", code="legal_hold"), 409
    item.status = "approved"
    item.approved_by_id = current_user.id
    item.approved_at = utcnow()
    audit("PRIVACY_REQUEST_APPROVED", "privacy_request", item.id)
    db.session.commit()
    return jsonify(id=item.id, status=item.status)


@admin_bp.post("/privacy-requests/<request_id>/execute")
@role_required(Role.ADMIN)
@sensitive_operation
def execute_privacy_request_endpoint(request_id: str):
    item = db.session.get(PrivacyRequest, request_id)
    if item is None or item.status not in {"approved", "failed"}:
        return jsonify(error="Solicitação inválida.", code="invalid_state"), 409
    organization = db.session.get(Organization, current_user.organization_id)
    if organization.legal_hold_enabled:
        return jsonify(error="Organização em retenção legal.", code="legal_hold"), 409
    if not current_app.config["QUEUE_ENABLED"]:
        from app.services.privacy_execution import execute

        execution = execute(item)
        audit("PRIVACY_REQUEST_EXECUTED", "privacy_request", item.id)
        db.session.commit()
        return jsonify(
            id=item.id,
            queued=False,
            status=item.status,
            execution_id=execution.id,
        )
    dispatch = add_outbox_event(
        organization_id=item.organization_id,
        queue_name="maintenance",
        task_name="execute_privacy_request",
        job_id=f"privacy-{item.id}",
        arguments=[item.id],
        retry_intervals=[60, 300],
    )
    db.session.commit()
    delivered = dispatch_outbox_event(dispatch.id)
    return jsonify(id=item.id, queued=delivered, outbox_status=dispatch.status), 202


@admin_bp.post("/outbox/<event_id>/replay")
@role_required(Role.ADMIN)
@sensitive_operation
def replay_outbox(event_id: str):
    event = db.session.get(OutboxEvent, event_id)
    if event is None or event.status not in {"failed", "dead_letter"}:
        return jsonify(error="Evento inválido.", code="invalid_state"), 409
    event.status = "pending"
    event.last_error = None
    db.session.commit()
    delivered = dispatch_outbox_event(event.id)
    audit("OUTBOX_REPLAYED", "outbox_event", event.id, delivered=delivered)
    db.session.commit()
    return jsonify(id=event.id, delivered=delivered, status=event.status)


@admin_bp.get("/security-events")
@role_required(Role.ADMIN)
@limiter.limit("120 per minute")
def security_events():
    page = max(1, request.args.get("page", 1, type=int))
    per_page = min(100, max(1, request.args.get("per_page", 25, type=int)))
    raw_events = (
        SecurityEvent.query.order_by(SecurityEvent.occurred_at.desc()).limit(5_000).all()
    )
    groups = group_security_events(raw_events)
    total = len(groups)
    pages = math.ceil(total / per_page) if total else 0
    selected = groups[(page - 1) * per_page : page * per_page]
    return jsonify(
        items=[
            {
                "id": event["id"],
                "type": event["event_type"],
                "severity": event["severity"],
                "route": event["route"],
                "masked_ip": event["masked_ip"],
                "count": event["count"],
                "first_occurred_at": event["first_occurred_at"].isoformat(),
                "occurred_at": event["occurred_at"].isoformat(),
                "acknowledged": event["acknowledged_at"] is not None,
            }
            for event in selected
        ],
        page=page,
        pages=pages,
        total=total,
    )


@admin_bp.post("/security-events/<event_id>/acknowledge")
@role_required(Role.ADMIN)
@sensitive_operation
@limiter.limit(sensitive_request_limit)
def acknowledge(event_id: str):
    event = db.session.scalar(
        db.select(SecurityEvent).where(
            SecurityEvent.id == event_id,
            SecurityEvent.organization_id == current_user.organization_id,
        )
    )
    if not event:
        return jsonify(error="Recurso não encontrado.", code="not_found"), 404
    acknowledged_at = utcnow()
    events = SecurityEvent.query.filter_by(
        ip_hash=event.ip_hash,
        event_type=event.event_type,
        organization_id=current_user.organization_id,
    ).all()
    for grouped_event in events:
        grouped_event.acknowledged_at = acknowledged_at
        grouped_event.acknowledged_by_id = current_user.id
    audit(
        "SECURITY_EVENT_GROUP_ACKNOWLEDGED",
        "security_event_group",
        event.id,
        event_type=event.event_type,
        event_count=len(events),
    )
    db.session.commit()
    return jsonify(status="acknowledged", count=len(events))
