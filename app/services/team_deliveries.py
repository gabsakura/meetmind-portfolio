from __future__ import annotations

from datetime import UTC
from typing import Any

from app.extensions import db
from app.models import DeliveryStatus, TeamDelivery, User, UserNotification, utcnow
from app.services.calendar_oauth import CalendarOAuthError, create_task
from app.services.notifications import send_delivery_email


def delivery_json(item: TeamDelivery) -> dict[str, Any]:
    assignee = db.session.get(User, item.assignee_id)
    assigner = db.session.get(User, item.assigned_by_id)
    due_at = item.due_at
    if due_at.tzinfo is None:
        due_at = due_at.replace(tzinfo=UTC)
    return {
        "id": item.id,
        "title": item.title,
        "description": item.description,
        "priority": item.priority,
        "status": item.status,
        "assigned_by_id": item.assigned_by_id,
        "assigned_by": assigner.name if assigner else "Gestor indisponível",
        "assignee_id": item.assignee_id,
        "assignee": assignee.name if assignee else "Usuário indisponível",
        "due_at": due_at.isoformat(),
        "overdue": (
            item.status not in {DeliveryStatus.COMPLETED.value, DeliveryStatus.CANCELLED.value}
            and due_at < utcnow()
        ),
        "calendar_provider": item.calendar_provider,
        "seen_at": item.seen_at.isoformat() if item.seen_at else None,
        "change_request": item.change_request,
        "change_requested_at": item.change_requested_at.isoformat()
        if item.change_requested_at
        else None,
        "calendar_status": item.calendar_status,
        "calendar_event_id": item.calendar_event_id,
        "calendar_event_url": item.calendar_event_url,
        "calendar_error": item.calendar_error,
        "calendar_synced_at": (
            item.calendar_synced_at.isoformat() if item.calendar_synced_at else None
        ),
        "completed_at": item.completed_at.isoformat() if item.completed_at else None,
        "version": item.version,
        "created_at": item.created_at.isoformat(),
        "updated_at": item.updated_at.isoformat(),
    }


def notify_delivery(item: TeamDelivery, kind: str, title: str, body: str | None = None) -> None:
    db.session.add(
        UserNotification(
            organization_id=item.organization_id,
            user_id=item.assignee_id,
            kind=kind,
            title=title[:300],
            body=body,
            target_type="team_delivery",
            target_id=item.id,
        )
    )
    user = db.session.get(User, item.assignee_id)
    if user is not None:
        send_delivery_email(user, title, body or item.title)


def sync_delivery_calendar(item: TeamDelivery) -> dict[str, Any]:
    if item.calendar_provider not in {"google", "microsoft"}:
        item.calendar_status = "not_requested"
        item.calendar_error = None
        return delivery_json(item)
    if item.calendar_status == "synced" and item.calendar_event_id:
        return delivery_json(item)
    assignee = db.session.get(User, item.assignee_id)
    assigner = db.session.get(User, item.assigned_by_id)
    if assignee is None or not assignee.is_active_record:
        item.calendar_status = "failed"
        item.calendar_error = "delivery_assignee_unavailable"
        return delivery_json(item)
    description = "\n\n".join(
        part
        for part in (
            f"Entrega atribuída por {assigner.name}." if assigner else "Entrega da equipe.",
            item.description,
            "Acompanhe o andamento no MeetMind.",
        )
        if part
    )
    try:
        event = create_task(
            assignee,
            item.calendar_provider,
            title=f"[MeetMind] {item.title}",
            due_at=item.due_at,
            description=description,
        )
    except CalendarOAuthError as error:
        item.calendar_status = "failed"
        item.calendar_error = str(error)[:120]
        item.calendar_synced_at = None
        return delivery_json(item)
    item.calendar_status = "synced"
    item.calendar_event_id = str(event.get("id") or "")[:300] or None
    item.calendar_event_url = str(event.get("url") or "") or None
    item.calendar_error = None
    item.calendar_synced_at = utcnow()
    return delivery_json(item)
