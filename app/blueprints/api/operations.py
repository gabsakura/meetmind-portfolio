from __future__ import annotations

from flask import Blueprint, jsonify, request
from flask_login import current_user, login_required
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import Client, ClientContact, Meeting, MeetingParticipant, Role, User
from app.security import audit, role_required, sensitive_operation
from app.services.access_policy import (
    can_access_client,
    can_access_meeting,
    can_manage_client,
    can_manage_meeting,
    can_manage_user,
    can_share_with,
)

operations_api_bp = Blueprint("operations_api", __name__, url_prefix="/api/v1")


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
