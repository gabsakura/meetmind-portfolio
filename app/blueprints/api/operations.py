from __future__ import annotations

from datetime import UTC, datetime

from flask import Blueprint, jsonify, request
from flask_login import current_user, login_required
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import (
    Client,
    ClientContact,
    DeliveryStatus,
    Meeting,
    MeetingParticipant,
    Role,
    TeamDelivery,
    User,
    UserNotification,
    utcnow,
)
from app.security import audit, role_required, sensitive_operation
from app.services.access_policy import (
    can_access_client,
    can_access_delivery,
    can_access_meeting,
    can_cancel_delivery,
    can_manage_client,
    can_manage_meeting,
    can_manage_user,
    can_share_with,
    deliveries_for,
)
from app.services.team_deliveries import (
    delivery_json,
    notify_delivery,
    notify_delivery_user,
    sync_delivery_calendar,
)

operations_api_bp = Blueprint("operations_api", __name__, url_prefix="/api/v1")


def _delivery_due_at(value) -> datetime:
    try:
        due_at = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("invalid_due_at") from error
    if due_at.tzinfo is None:
        due_at = due_at.replace(tzinfo=UTC)
    if due_at <= utcnow():
        raise ValueError("invalid_due_at")
    return due_at


def _contact_json(item: ClientContact) -> dict:
    return {
        "id": item.id,
        "client_id": item.client_id,
        "name": item.name,
        "email": item.email,
        "job_title": item.job_title,
        "phone": item.phone,
        "is_primary": item.is_primary,
        "is_active": item.is_active,
        "created_at": item.created_at.isoformat(),
        "updated_at": item.updated_at.isoformat(),
    }


def _contact_fields(payload: dict) -> tuple[str, str, str | None, str | None]:
    name = str(payload.get("name") or "").strip()
    email = str(payload.get("email") or "").strip().lower()
    job_title = str(payload.get("job_title") or "").strip() or None
    phone = str(payload.get("phone") or "").strip() or None
    if not 1 <= len(name) <= 200 or "@" not in email or len(email) > 254:
        raise ValueError("invalid_contact")
    if job_title and len(job_title) > 160:
        raise ValueError("invalid_contact")
    if phone and len(phone) > 40:
        raise ValueError("invalid_contact")
    return name, email, job_title, phone


@operations_api_bp.patch("/clients/<client_id>/owner")
@role_required(Role.ADMIN, Role.MANAGER)
@sensitive_operation
def transfer_client_owner(client_id: str):
    client = db.session.get(Client, client_id)
    if not can_manage_client(current_user, client):
        return jsonify(error="Cliente não encontrado.", code="not_found"), 404
    payload = request.get_json(silent=True) or {}
    owner_id = str(payload.get("owner_id") or "").strip()
    owner = db.session.get(User, owner_id)
    if not can_manage_user(current_user, owner) or owner.role not in {
        Role.MANAGER,
        Role.ANALYST,
    }:
        return jsonify(error="Responsável inválido.", code="invalid_owner"), 422
    previous_owner_id = client.owner_id
    client.owner_id = owner.id
    audit(
        "CLIENT_OWNER_CHANGED",
        "client",
        client.id,
        previous_owner_id=previous_owner_id,
        owner_id=owner.id,
    )
    db.session.commit()
    return jsonify(id=client.id, owner_id=client.owner_id, owner=owner.name)


@operations_api_bp.get("/clients/<client_id>/contacts")
@login_required
def client_contacts(client_id: str):
    client = db.session.get(Client, client_id)
    if not can_access_client(current_user, client):
        return jsonify(error="Cliente não encontrado.", code="not_found"), 404
    items = ClientContact.query.filter_by(client_id=client.id).order_by(
        ClientContact.is_primary.desc(), ClientContact.name.asc()
    )
    return jsonify(items=[_contact_json(item) for item in items])


@operations_api_bp.post("/clients/<client_id>/contacts")
@role_required(Role.ADMIN, Role.MANAGER, Role.ANALYST)
@sensitive_operation
def create_client_contact(client_id: str):
    client = db.session.get(Client, client_id)
    if not can_manage_client(current_user, client):
        return jsonify(error="Cliente não encontrado.", code="not_found"), 404
    payload = request.get_json(silent=True) or {}
    try:
        name, email, job_title, phone = _contact_fields(payload)
    except ValueError:
        return jsonify(error="Contato inválido.", code="invalid_contact"), 422
    is_primary = bool(payload.get("is_primary"))
    if is_primary:
        ClientContact.query.filter_by(client_id=client.id, is_primary=True).update(
            {"is_primary": False}
        )
    item = ClientContact(
        organization_id=current_user.organization_id,
        client_id=client.id,
        name=name,
        email=email,
        job_title=job_title,
        phone=phone,
        is_primary=is_primary,
    )
    db.session.add(item)
    try:
        db.session.flush()
    except IntegrityError:
        db.session.rollback()
        return jsonify(error="Este e-mail já está cadastrado.", code="conflict"), 409
    audit("CLIENT_CONTACT_CREATED", "client_contact", item.id, client_id=client.id)
    db.session.commit()
    return jsonify(_contact_json(item)), 201


@operations_api_bp.patch("/clients/<client_id>/contacts/<contact_id>")
@role_required(Role.ADMIN, Role.MANAGER, Role.ANALYST)
@sensitive_operation
def update_client_contact(client_id: str, contact_id: str):
    client = db.session.get(Client, client_id)
    item = db.session.get(ClientContact, contact_id)
    if (
        not can_manage_client(current_user, client)
        or item is None
        or item.client_id != client.id
    ):
        return jsonify(error="Contato não encontrado.", code="not_found"), 404
    payload = request.get_json(silent=True) or {}
    merged = {
        "name": payload.get("name", item.name),
        "email": payload.get("email", item.email),
        "job_title": payload.get("job_title", item.job_title),
        "phone": payload.get("phone", item.phone),
    }
    try:
        item.name, item.email, item.job_title, item.phone = _contact_fields(merged)
    except ValueError:
        return jsonify(error="Contato inválido.", code="invalid_contact"), 422
    if bool(payload.get("is_primary", item.is_primary)):
        ClientContact.query.filter(
            ClientContact.client_id == client.id,
            ClientContact.id != item.id,
        ).update({"is_primary": False})
        item.is_primary = True
    elif "is_primary" in payload:
        item.is_primary = False
    if "is_active" in payload:
        item.is_active = bool(payload["is_active"])
    audit("CLIENT_CONTACT_UPDATED", "client_contact", item.id, client_id=client.id)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return jsonify(error="Este e-mail já está cadastrado.", code="conflict"), 409
    return jsonify(_contact_json(item))


@operations_api_bp.delete("/clients/<client_id>/contacts/<contact_id>")
@role_required(Role.ADMIN, Role.MANAGER, Role.ANALYST)
@sensitive_operation
def deactivate_client_contact(client_id: str, contact_id: str):
    client = db.session.get(Client, client_id)
    item = db.session.get(ClientContact, contact_id)
    if (
        not can_manage_client(current_user, client)
        or item is None
        or item.client_id != client.id
    ):
        return jsonify(error="Contato não encontrado.", code="not_found"), 404
    item.is_active = False
    item.is_primary = False
    audit("CLIENT_CONTACT_DEACTIVATED", "client_contact", item.id, client_id=client.id)
    db.session.commit()
    return "", 204


def _participant_json(item: MeetingParticipant) -> dict:
    user = db.session.get(User, item.user_id)
    return {
        "id": item.id,
        "user_id": item.user_id,
        "name": user.name if user else "Usuário indisponível",
        "role": item.participation_role,
    }


@operations_api_bp.get("/meetings/<meeting_id>/participants")
@login_required
def meeting_participants(meeting_id: str):
    meeting = db.session.get(Meeting, meeting_id)
    if not can_access_meeting(current_user, meeting):
        return jsonify(error="Reunião não encontrada.", code="not_found"), 404
    creator = db.session.get(User, meeting.created_by_id)
    owner = (
        db.session.get(User, meeting.client.owner_id)
        if meeting.client and meeting.client.owner_id
        else None
    )
    return jsonify(
        items=[_participant_json(item) for item in meeting.participants],
        creator={"id": creator.id, "name": creator.name} if creator else None,
        client_owner={"id": owner.id, "name": owner.name} if owner else None,
        can_manage=can_manage_meeting(current_user, meeting),
    )


@operations_api_bp.post("/meetings/<meeting_id>/participants")
@role_required(Role.ADMIN, Role.MANAGER, Role.ANALYST)
@sensitive_operation
def add_meeting_participant(meeting_id: str):
    meeting = db.session.get(Meeting, meeting_id)
    if not can_manage_meeting(current_user, meeting):
        return jsonify(error="Reunião não encontrada.", code="not_found"), 404
    payload = request.get_json(silent=True) or {}
    user = db.session.get(User, str(payload.get("user_id") or ""))
    if not can_share_with(current_user, user):
        return jsonify(error="Participante inválido.", code="invalid_participant"), 422
    role = str(payload.get("role") or "participant").strip()[:30]
    item = MeetingParticipant.query.filter_by(
        meeting_id=meeting.id, user_id=user.id
    ).first()
    if item is None:
        item = MeetingParticipant(
            organization_id=current_user.organization_id,
            meeting_id=meeting.id,
            user_id=user.id,
            participation_role=role,
        )
        db.session.add(item)
    else:
        item.participation_role = role
    db.session.flush()
    audit("MEETING_PARTICIPANT_ADDED", "meeting", meeting.id, user_id=user.id)
    db.session.commit()
    return jsonify(_participant_json(item)), 201


@operations_api_bp.delete("/meetings/<meeting_id>/participants/<user_id>")
@role_required(Role.ADMIN, Role.MANAGER, Role.ANALYST)
@sensitive_operation
def remove_meeting_participant(meeting_id: str, user_id: str):
    meeting = db.session.get(Meeting, meeting_id)
    if not can_manage_meeting(current_user, meeting):
        return jsonify(error="Reunião não encontrada.", code="not_found"), 404
    if user_id == meeting.created_by_id:
        return jsonify(error="O criador da reunião não pode ser removido."), 422
    item = MeetingParticipant.query.filter_by(
        meeting_id=meeting.id, user_id=user_id
    ).first()
    if item is None:
        return jsonify(error="Participante não encontrado.", code="not_found"), 404
    db.session.delete(item)
    audit("MEETING_PARTICIPANT_REMOVED", "meeting", meeting.id, user_id=user_id)
    db.session.commit()
    return "", 204


@operations_api_bp.get("/team-deliveries")
@login_required
def list_team_deliveries():
    query = deliveries_for(current_user)
    status = str(request.args.get("status") or "").strip()
    if status:
        if status not in {item.value for item in DeliveryStatus}:
            return jsonify(error="Estado inválido.", code="invalid_payload"), 422
        query = query.filter(TeamDelivery.status == status)
    assignee_id = str(request.args.get("assignee_id") or "").strip()
    if assignee_id:
        query = query.filter(TeamDelivery.assignee_id == assignee_id)
    items = query.order_by(TeamDelivery.due_at.asc(), TeamDelivery.created_at.desc())
    return jsonify(items=[delivery_json(item) for item in items.limit(100)])


@operations_api_bp.post("/team-deliveries")
@role_required(Role.ADMIN, Role.MANAGER)
@sensitive_operation
def create_team_delivery():
    payload = request.get_json(silent=True) or {}
    title = str(payload.get("title") or "").strip()
    description = str(payload.get("description") or "").strip() or None
    priority = str(payload.get("priority") or "medium").strip()
    provider = str(payload.get("calendar_provider") or "").strip()
    assignee = db.session.get(User, str(payload.get("assignee_id") or "").strip())
    if not 3 <= len(title) <= 500 or (description and len(description) > 4_000):
        return jsonify(error="Informe uma entrega válida.", code="invalid_payload"), 422
    if priority not in {"high", "medium", "low"}:
        return jsonify(error="Prioridade inválida.", code="invalid_payload"), 422
    if provider not in {"", "google", "microsoft"}:
        return jsonify(error="Provedor inválido.", code="invalid_provider"), 422
    if (
        assignee is None
        or assignee.id == current_user.id
        or assignee.role != Role.ANALYST
        or not assignee.is_active_record
        or not can_manage_user(current_user, assignee)
    ):
        return jsonify(error="Subordinado inválido.", code="invalid_assignee"), 422
    try:
        due_at = _delivery_due_at(payload.get("due_at"))
    except ValueError:
        return jsonify(error="Defina um prazo futuro.", code="invalid_due_at"), 422
    item = TeamDelivery(
        organization_id=current_user.organization_id,
        title=title,
        description=description,
        priority=priority,
        assigned_by_id=current_user.id,
        assignee_id=assignee.id,
        due_at=due_at,
        calendar_provider=provider or "none",
    )
    db.session.add(item)
    db.session.flush()
    audit(
        "TEAM_DELIVERY_CREATED",
        "team_delivery",
        item.id,
        assignee_id=assignee.id,
        due_at=due_at.isoformat(),
        calendar_provider=provider,
    )
    notify_delivery(item, "delivery_assigned", "Nova tarefa atribuída", item.title)
    db.session.commit()
    response = sync_delivery_calendar(item)
    audit(
        "TEAM_DELIVERY_CALENDAR_SYNCED"
        if item.calendar_status == "synced"
        else "TEAM_DELIVERY_CALENDAR_FAILED",
        "team_delivery",
        item.id,
        calendar_provider=provider,
        calendar_error=item.calendar_error,
    )
    db.session.commit()
    return jsonify(response), 201


@operations_api_bp.patch("/team-deliveries/<delivery_id>")
@login_required
@sensitive_operation
def update_team_delivery(delivery_id: str):
    item = db.session.get(TeamDelivery, delivery_id)
    if not can_access_delivery(current_user, item):
        return jsonify(error="Entrega não encontrada.", code="not_found"), 404
    payload = request.get_json(silent=True) or {}
    if int(payload.get("expected_version", -1)) != item.version:
        return jsonify(error="A entrega foi alterada.", code="version_conflict"), 409
    editable = {"title", "description", "priority", "due_at", "assignee_id"}
    if editable.intersection(payload):
        if not can_cancel_delivery(current_user, item):
            return jsonify(error="Entrega não encontrada.", code="not_found"), 404
        if item.status in {DeliveryStatus.COMPLETED.value, DeliveryStatus.CANCELLED.value}:
            return jsonify(
                error="Entregas encerradas são imutáveis.", code="immutable_delivery"
            ), 409
        previous = delivery_json(item)
        title = str(payload.get("title", item.title) or "").strip()
        description = str(payload.get("description", item.description) or "").strip() or None
        priority = str(payload.get("priority", item.priority) or "").strip()
        if not 3 <= len(title) <= 500 or (description and len(description) > 4_000):
            return jsonify(error="Informe uma entrega válida.", code="invalid_payload"), 422
        if priority not in {"high", "medium", "low"}:
            return jsonify(error="Prioridade inválida.", code="invalid_payload"), 422
        due_at = item.due_at
        if "due_at" in payload:
            try:
                due_at = _delivery_due_at(payload["due_at"])
            except ValueError:
                return jsonify(error="Defina um prazo futuro.", code="invalid_due_at"), 422
        assignee = db.session.get(User, str(payload.get("assignee_id") or item.assignee_id))
        if not can_manage_user(current_user, assignee) or assignee.role != Role.ANALYST:
            return jsonify(error="Subordinado inválido.", code="invalid_assignee"), 422
        if assignee.id != item.assignee_id and item.status == DeliveryStatus.IN_PROGRESS.value:
            justification = str(payload.get("justification") or "").strip()
            if len(justification) < 5:
                return jsonify(
                    error="Justifique a troca de responsável.", code="justification_required"
                ), 422
        item.title, item.description, item.priority = title, description, priority
        item.due_at, item.assignee_id = due_at, assignee.id
        item.version += 1
        notify_delivery(item, "delivery_changed", "Tarefa atualizada", item.title)
        audit("TEAM_DELIVERY_FIELDS_UPDATED", "team_delivery", item.id, previous=previous)
        db.session.commit()
        return jsonify(delivery_json(item))

    requested = str(payload.get("status") or "").strip()
    if requested not in {status.value for status in DeliveryStatus}:
        return jsonify(error="Estado inválido.", code="invalid_payload"), 422
    if current_user.id == item.assignee_id:
        allowed = {
            DeliveryStatus.PENDING.value: {
                DeliveryStatus.IN_PROGRESS.value,
                DeliveryStatus.COMPLETED.value,
            },
            DeliveryStatus.IN_PROGRESS.value: {DeliveryStatus.COMPLETED.value},
        }
    elif can_cancel_delivery(current_user, item):
        allowed = {
            DeliveryStatus.PENDING.value: {DeliveryStatus.CANCELLED.value},
            DeliveryStatus.IN_PROGRESS.value: {DeliveryStatus.CANCELLED.value},
        }
    else:
        return jsonify(error="Entrega não encontrada.", code="not_found"), 404
    if requested not in allowed.get(item.status, set()):
        return jsonify(error="Transição inválida.", code="invalid_transition"), 409
    previous = item.status
    item.status = requested
    item.version += 1
    item.completed_at = utcnow() if requested == DeliveryStatus.COMPLETED.value else None
    if current_user.id == item.assignee_id:
        status_label = (
            "Tarefa concluída"
            if requested == DeliveryStatus.COMPLETED.value
            else "Tarefa iniciada"
        )
        notify_delivery_user(
            item,
            item.assigned_by_id,
            f"delivery_{requested}",
            status_label,
            f"{current_user.name}: {item.title}",
        )
    elif requested == DeliveryStatus.CANCELLED.value:
        notify_delivery(item, "delivery_cancelled", "Tarefa cancelada", item.title)
    audit(
        "TEAM_DELIVERY_UPDATED",
        "team_delivery",
        item.id,
        previous_status=previous,
        status=requested,
        assignee_id=item.assignee_id,
    )
    db.session.commit()
    return jsonify(delivery_json(item))


@operations_api_bp.post("/team-deliveries/<delivery_id>/seen")
@login_required
@sensitive_operation
def see_team_delivery(delivery_id: str):
    item = db.session.get(TeamDelivery, delivery_id)
    if (
        item is None
        or item.assignee_id != current_user.id
        or not can_access_delivery(current_user, item)
    ):
        return jsonify(error="Entrega não encontrada.", code="not_found"), 404
    if item.seen_at is None:
        item.seen_at = utcnow()
        audit("TEAM_DELIVERY_SEEN", "team_delivery", item.id)
    UserNotification.query.filter_by(
        user_id=current_user.id,
        target_type="team_delivery",
        target_id=item.id,
        read_at=None,
    ).update({"read_at": utcnow()})
    db.session.commit()
    return jsonify(delivery_json(item))


@operations_api_bp.post("/team-deliveries/<delivery_id>/change-request")
@login_required
@sensitive_operation
def request_team_delivery_change(delivery_id: str):
    item = db.session.get(TeamDelivery, delivery_id)
    if (
        item is None
        or item.assignee_id != current_user.id
        or not can_access_delivery(current_user, item)
    ):
        return jsonify(error="Entrega não encontrada.", code="not_found"), 404
    if item.status not in {DeliveryStatus.PENDING.value, DeliveryStatus.IN_PROGRESS.value}:
        return jsonify(error="A entrega já foi encerrada.", code="invalid_transition"), 409
    reason = str((request.get_json(silent=True) or {}).get("reason") or "").strip()
    if not 5 <= len(reason) <= 2000:
        return jsonify(error="Explique a alteração necessária.", code="invalid_payload"), 422
    item.change_request = reason
    item.change_requested_at = utcnow()
    item.version += 1
    notify_delivery_user(
        item,
        item.assigned_by_id,
        "delivery_change_requested",
        "Alteração solicitada",
        f"{current_user.name}: {item.title}",
    )
    audit("TEAM_DELIVERY_CHANGE_REQUESTED", "team_delivery", item.id, reason=reason)
    db.session.commit()
    return jsonify(delivery_json(item))


@operations_api_bp.post("/team-deliveries/<delivery_id>/calendar/retry")
@login_required
@sensitive_operation
def retry_team_delivery_calendar(delivery_id: str):
    item = db.session.get(TeamDelivery, delivery_id)
    if not can_access_delivery(current_user, item):
        return jsonify(error="Entrega não encontrada.", code="not_found"), 404
    if current_user.id != item.assignee_id and current_user.role not in {
        Role.ADMIN,
        Role.MANAGER,
    }:
        return jsonify(error="Entrega não encontrada.", code="not_found"), 404
    response = sync_delivery_calendar(item)
    audit(
        "TEAM_DELIVERY_CALENDAR_RETRIED",
        "team_delivery",
        item.id,
        calendar_status=item.calendar_status,
        calendar_error=item.calendar_error,
    )
    db.session.commit()
    return jsonify(response)
