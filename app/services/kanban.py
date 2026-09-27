from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from app.extensions import db
from app.models import (
    ActionItem,
    Client,
    KanbanRequest,
    KanbanRequestStatus,
    User,
    utcnow,
)
from app.services.access_policy import (
    can_access_action,
    can_access_client,
    can_access_request,
    can_manage_user,
    requests_for,
)
from app.services.actions import record_event

KANBAN_ROUTING: dict[str, dict[str, Any]] = {
    "performance_analysis": {
        "label": "Solicitar análise de lentidão",
        "department": "Infraestrutura",
        "priority": "high",
        "expected_days": 2,
        "channel": "internal_queue",
        "required_fields": ["client_id", "note"],
    },
    "product_bug": {
        "label": "Investigar erro no produto",
        "department": "Engenharia de Produto",
        "priority": "high",
        "expected_days": 2,
        "channel": "internal_queue",
        "required_fields": ["client_id", "note"],
    },
    "commercial_opportunity": {
        "label": "Avaliar oportunidade comercial",
        "department": "Comercial",
        "priority": "medium",
        "expected_days": 3,
        "channel": "internal_queue",
        "required_fields": ["client_id", "note"],
    },
    "enablement_request": {
        "label": "Planejar treinamento",
        "department": "Customer Success",
        "priority": "medium",
        "expected_days": 3,
        "channel": "internal_queue",
        "required_fields": ["client_id", "note"],
    },
}


def create_request(
    user: User,
    *,
    request_type: str,
    source_action_id: str | None,
    note: str | None,
    client_id: str | None = None,
    source_channel: str | None = None,
) -> KanbanRequest:
    route = KANBAN_ROUTING.get(request_type)
    if route is None:
        raise ValueError("invalid_request_type")
    clean_note = (note or "").strip()
    if len(clean_note) > 2_000:
        raise ValueError("invalid_note")

    action = db.session.get(ActionItem, source_action_id) if source_action_id else None
    if source_action_id and not can_access_action(user, action):
        raise LookupError("source_action_not_found")
    if action is not None:
        client_id = action.client_id
        meeting_id = action.meeting_id
        analysis_id = action.analysis_id
        source_kind = "meeting"
        source_channel = None
    else:
        client = db.session.get(Client, client_id) if client_id else None
        if not can_access_client(user, client):
            raise LookupError("client_not_found")
        if len(clean_note) < 3:
            raise ValueError("note_required")
        if source_channel not in {"email", "teams", "phone", "other"}:
            raise ValueError("invalid_source_channel")
        meeting_id = None
        analysis_id = None
        source_kind = "external"

    client = db.session.get(Client, client_id) if client_id else None
    origin = (
        f"Reunião / ação: {action.title}"
        if action
        else f"Canal informado pelo CS: {source_channel}"
    )
    request_detail = clean_note or (action.rationale if action else "")
    expected_due = utcnow() + timedelta(days=int(route["expected_days"]))
    message = (
        f"Cliente: {client.name if client else 'Não identificado'}\n"
        f"Solicitação: {route['label']}\n"
        f"Contexto: {request_detail}\n"
        f"Origem: {origin}\n"
        f"Prazo esperado: {expected_due.date().isoformat()}"
    )
    payload: dict[str, Any] = {
        "request_type": request_type,
        "title": route["label"],
        "department": route["department"],
        "priority": route["priority"],
        "channel": route["channel"],
        "expected_days": route["expected_days"],
        "required_fields": route["required_fields"],
        "message": message,
        "source_kind": source_kind,
        "source_channel": source_channel,
        "source_action_id": action.id if action else None,
        "client_id": client_id,
        "meeting_id": meeting_id,
        "analysis_id": analysis_id,
        "note": clean_note or None,
    }
    item = KanbanRequest(
        organization_id=user.organization_id,
        client_id=client_id,
        source_action_id=action.id if action else None,
        meeting_id=meeting_id,
        analysis_id=analysis_id,
        source_kind=source_kind,
        source_channel=source_channel,
        request_type=request_type,
        department=route["department"],
        priority=route["priority"],
        note=clean_note or None,
        payload_json=payload,
        created_by_id=user.id,
        due_at=expected_due,
        status=KanbanRequestStatus.QUEUED.value,
    )
    db.session.add(item)
    db.session.flush()
    return item

def request_json(item: KanbanRequest) -> dict[str, Any]:
    client = db.session.get(Client, item.client_id) if item.client_id else None
    action = db.session.get(ActionItem, item.source_action_id)
    assignee = db.session.get(User, item.assigned_to_id) if item.assigned_to_id else None
    creator = db.session.get(User, item.created_by_id)
    return {
        "id": item.id,
        "client_id": item.client_id,
        "client": client.name if client else "Cliente não identificado",
        "source_action_id": item.source_action_id,
        "source_action": (
            action.title if action else "Solicitação registrada pelo CS"
        ),
        "source_kind": item.source_kind,
        "source_channel": item.source_channel,
        "meeting_id": item.meeting_id,
        "analysis_id": item.analysis_id,
        "request_type": item.request_type,
        "request_label": KANBAN_ROUTING.get(item.request_type, {}).get("label", item.request_type),
        "department": item.department,
        "priority": item.priority,
        "note": item.note,
        "message": (item.payload_json or {}).get("message"),
        "channel": (item.payload_json or {}).get("channel", "internal_queue"),
        "status": item.status,
        "assigned_to_id": item.assigned_to_id,
        "assigned_to": assignee.name if assignee else None,
        "created_by": creator.name if creator else "Usuário indisponível",
        "due_at": item.due_at.isoformat() if item.due_at else None,
        "response": item.response,
        "version": item.version,
        "created_at": item.created_at.isoformat(),
        "updated_at": (item.updated_at or item.created_at).isoformat(),
        "responded_at": item.responded_at.isoformat() if item.responded_at else None,
        "customer_return_pending": bool(
            item.status == KanbanRequestStatus.ANSWERED.value
            and not item.customer_returned_at
            and not item.customer_return_not_required_reason
        ),
        "customer_returned_at": (
            item.customer_returned_at.isoformat() if item.customer_returned_at else None
        ),
        "customer_return_not_required_reason": item.customer_return_not_required_reason,
    }


def list_requests(user: User, *, status: str = "") -> list[KanbanRequest]:
    query = requests_for(user)
    if status:
        query = query.filter(KanbanRequest.status == status)
    return query.order_by(KanbanRequest.updated_at.desc(), KanbanRequest.created_at.desc()).all()


def update_request(
    item: KanbanRequest,
    actor: User,
    *,
    expected_version: int,
    status: str,
    assigned_to_id: str | None,
    due_at: datetime | None,
    response: str | None,
) -> KanbanRequest:
    if not can_access_request(actor, item):
        raise LookupError("not_found")
    if expected_version != item.version:
        raise RuntimeError("version_conflict")
    allowed = {
        KanbanRequestStatus.QUEUED.value: {
            KanbanRequestStatus.QUEUED.value,
            KanbanRequestStatus.IN_PROGRESS.value,
            KanbanRequestStatus.CANCELLED.value,
        },
        KanbanRequestStatus.IN_PROGRESS.value: {
            KanbanRequestStatus.IN_PROGRESS.value,
            KanbanRequestStatus.ANSWERED.value,
            KanbanRequestStatus.CANCELLED.value,
        },
        KanbanRequestStatus.ANSWERED.value: {
            KanbanRequestStatus.ANSWERED.value,
            KanbanRequestStatus.IN_PROGRESS.value,
            KanbanRequestStatus.CLOSED.value,
        },
        KanbanRequestStatus.CLOSED.value: {KanbanRequestStatus.CLOSED.value},
        KanbanRequestStatus.CANCELLED.value: {KanbanRequestStatus.CANCELLED.value},
        KanbanRequestStatus.DELIVERED.value: {
            KanbanRequestStatus.ANSWERED.value,
            KanbanRequestStatus.CLOSED.value,
        },
        KanbanRequestStatus.FAILED.value: {KanbanRequestStatus.CANCELLED.value},
    }
    if status not in allowed.get(item.status, set()):
        raise ValueError("invalid_transition")
    if status == KanbanRequestStatus.CLOSED.value and not (
        item.customer_returned_at or item.customer_return_not_required_reason
    ):
        raise ValueError("customer_return_required")
    assignee = db.session.get(User, assigned_to_id) if assigned_to_id else None
    if assigned_to_id and not can_manage_user(actor, assignee):
        raise ValueError("invalid_assignee")
    if status in {
        KanbanRequestStatus.IN_PROGRESS.value,
        KanbanRequestStatus.ANSWERED.value,
    } and (assignee is None or due_at is None):
        raise ValueError("assignment_required")
    clean_response = (response or item.response or "").strip()
    if status == KanbanRequestStatus.ANSWERED.value and len(clean_response) < 3:
        raise ValueError("response_required")
    item.assigned_to_id = assigned_to_id
    item.due_at = due_at
    item.status = status
    item.response = clean_response[:4_000] or None
    if status == KanbanRequestStatus.ANSWERED.value:
        item.responded_at = utcnow()
        if item.source_action_id:
            source_action = db.session.get(ActionItem, item.source_action_id)
            if source_action is not None:
                record_event(
                    source_action,
                    actor.id,
                    "routing_answered",
                    request_id=item.id,
                    department=item.department,
                    response=clean_response[:4_000],
                )
    item.updated_at = utcnow()
    item.version += 1
    db.session.flush()
    return item
