from __future__ import annotations

from datetime import datetime

from flask import Blueprint, jsonify, request
from flask_login import current_user

from app.extensions import db
from app.models import (
    Client,
    CommunicationDraft,
    CommunicationDraftStatus,
    KanbanRequest,
    Role,
    utcnow,
)
from app.security import audit, role_required, sensitive_operation
from app.services import communication_ai, communications
from app.services.access_policy import can_manage_client, can_manage_request
from app.services.calendar_oauth import CalendarOAuthError, create_event
from app.services.kanban import request_json

communications_api_bp = Blueprint(
    "communications_api", __name__, url_prefix="/api/v1"
)
WRITERS = (Role.ADMIN, Role.MANAGER, Role.ANALYST)


def _data() -> dict:
    return request.get_json(silent=True) or {}


def _draft(draft_id: str) -> CommunicationDraft | None:
    item = db.session.get(CommunicationDraft, draft_id)
    return item if communications.can_access(current_user, item) else None


def _error(error: Exception):
    code = str(error)
    if isinstance(error, LookupError):
        return jsonify(error="Recurso não encontrado.", code="not_found"), 404
    if isinstance(error, PermissionError):
        return jsonify(error="Uso de IA externa não autorizado.", code=code), 403
    if isinstance(error, RuntimeError) and code == "version_conflict":
        return jsonify(error="O registro foi alterado por outra pessoa.", code=code), 409
    if isinstance(error, RuntimeError):
        return jsonify(error="Não foi possível gerar a comunicação.", code=code), 502
    return jsonify(error="Dados inválidos para esta operação.", code=code), 422


@communications_api_bp.post("/communication-drafts")
@role_required(*WRITERS)
@sensitive_operation
def create_draft():
    data = _data()
    try:
        client_id = str(data.get("client_id") or "")
        client = db.session.get(Client, client_id)
        if not can_manage_client(current_user, client):
            raise LookupError("client_not_found")
        draft_type = str(data.get("draft_type") or "customer_message")
        channel = str(data.get("channel") or "email")
        meeting, action, item = communications.resolve_sources(
            current_user,
            client,
            meeting_id=str(data.get("meeting_id") or "") or None,
            action_id=str(data.get("action_id") or "") or None,
            request_id=str(data.get("request_id") or "") or None,
        )
        evidence = communications.validated_evidence(
            meeting, list(data.get("evidence_segment_ids") or []), action
        )
        generated = bool(data.get("generate_with_ai"))
        subject, body = str(data.get("subject") or ""), str(data.get("body") or "")
        metadata = None
        if generated:
            if meeting is None:
                raise ValueError("meeting_required")
            subject, body, ids, metadata = communication_ai.generate(
                meeting=meeting,
                client=client,
                action=action,
                item=item,
                segments=evidence,
                draft_type=draft_type,
                channel=channel,
            )
            evidence = communications.validated_evidence(meeting, ids, action)
        draft = communications.create(
            current_user,
            client_id=client_id,
            draft_type=draft_type,
            channel=channel,
            recipient_contact_ids=list(data.get("recipient_contact_ids") or []),
            meeting_id=meeting.id if meeting else None,
            action_id=action.id if action else None,
            request_id=item.id if item else None,
            evidence_segment_ids=[segment.id for segment in evidence],
            subject=subject,
            body=body,
            generated_by_ai=generated,
            generation_metadata=metadata,
        )
        audit("COMMUNICATION_DRAFT_CREATED", "communication_draft", draft.id)
        db.session.commit()
        return jsonify(communications.json_data(draft)), 201
    except (LookupError, PermissionError, RuntimeError, ValueError) as error:
        db.session.rollback()
        return _error(error)


@communications_api_bp.get("/communication-drafts/<draft_id>")
@role_required(Role.ADMIN, Role.MANAGER, Role.ANALYST, Role.VIEWER)
def get_draft(draft_id: str):
    draft = _draft(draft_id)
    return (
        jsonify(communications.json_data(draft))
        if draft
        else (jsonify(error="Rascunho não encontrado.", code="not_found"), 404)
    )


@communications_api_bp.patch("/communication-drafts/<draft_id>")
@role_required(*WRITERS)
@sensitive_operation
def update_draft(draft_id: str):
    draft = _draft(draft_id)
    if not draft:
        return jsonify(error="Rascunho não encontrado.", code="not_found"), 404
    data = _data()
    try:
        communications.add_revision(
            draft,
            current_user,
            expected_version=int(data.get("version") or 0),
            subject=str(data.get("subject") or ""),
            body=str(data.get("body") or ""),
            recipient_contact_ids=list(data.get("recipient_contact_ids") or []),
        )
        audit("COMMUNICATION_DRAFT_REVISED", "communication_draft", draft.id)
        db.session.commit()
        return jsonify(communications.json_data(draft))
    except (LookupError, RuntimeError, ValueError) as error:
        db.session.rollback()
        return _error(error)


@communications_api_bp.post("/communication-drafts/<draft_id>/approve")
@role_required(*WRITERS)
@sensitive_operation
def approve_draft(draft_id: str):
    draft = _draft(draft_id)
    if not draft:
        return jsonify(error="Rascunho não encontrado.", code="not_found"), 404
    try:
        communications.approve(draft, current_user, int(_data().get("version") or 0))
        audit("COMMUNICATION_DRAFT_APPROVED", "communication_draft", draft.id)
        db.session.commit()
        return jsonify(communications.json_data(draft))
    except (LookupError, RuntimeError, ValueError) as error:
        db.session.rollback()
        return _error(error)


@communications_api_bp.post("/communication-drafts/<draft_id>/confirm-send")
@role_required(*WRITERS)
@sensitive_operation
def confirm_send(draft_id: str):
    draft = _draft(draft_id)
    if not draft:
        return jsonify(error="Rascunho não encontrado.", code="not_found"), 404
    data = _data()
    try:
        if draft.version != int(data.get("version") or 0):
            raise RuntimeError("version_conflict")
        if draft.status != CommunicationDraftStatus.APPROVED.value:
            raise ValueError("approval_required")
        channel = str(data.get("channel") or draft.channel)
        if channel not in communications.CHANNELS - {"calendar"}:
            raise ValueError("invalid_channel")
        draft.channel = channel
        draft.status = CommunicationDraftStatus.SENT_CONFIRMED.value
        draft.sent_by_id = current_user.id
        draft.sent_at = utcnow()
        draft.version += 1
        audit("COMMUNICATION_SEND_CONFIRMED", "communication_draft", draft.id, channel=channel)
        db.session.commit()
        return jsonify(communications.json_data(draft))
    except (RuntimeError, ValueError) as error:
        db.session.rollback()
        return _error(error)


@communications_api_bp.post("/communication-drafts/<draft_id>/schedule")
@role_required(*WRITERS)
@sensitive_operation
def schedule_draft(draft_id: str):
    draft = _draft(draft_id)
    if not draft:
        return jsonify(error="Rascunho não encontrado.", code="not_found"), 404
    data = _data()
    try:
        if draft.external_id:
            return jsonify(communications.json_data(draft))
        if draft.version != int(data.get("version") or 0):
            raise RuntimeError("version_conflict")
        if draft.status != CommunicationDraftStatus.APPROVED.value:
            raise ValueError("approval_required")
        revision = communications.revision(draft, draft.approved_revision)
        contacts = communications.contacts_for(
            db.session.get(Client, draft.client_id), list(draft.recipient_contact_ids or [])
        )
        if not contacts:
            raise ValueError("recipients_required")
        starts_at = datetime.fromisoformat(
            str(data.get("starts_at") or "").replace("Z", "+00:00")
        )
        event = create_event(
            current_user,
            str(data.get("provider") or ""),
            title=revision.subject,
            starts_at=starts_at,
            duration_minutes=int(data.get("duration_minutes") or 30),
            description=revision.body,
            attendee_emails=[contact.email for contact in contacts],
            idempotency_key=draft.id,
        )
        draft.status = CommunicationDraftStatus.SCHEDULED.value
        draft.external_provider, draft.external_id = event["provider"], event["id"]
        draft.external_url = event.get("url")
        draft.sent_by_id, draft.sent_at = current_user.id, utcnow()
        draft.version += 1
        audit(
            "COMMUNICATION_CALENDAR_SCHEDULED",
            "communication_draft",
            draft.id,
            provider=event["provider"],
        )
        db.session.commit()
        return jsonify(communications.json_data(draft))
    except (CalendarOAuthError, LookupError, RuntimeError, ValueError) as error:
        db.session.rollback()
        return _error(error)


@communications_api_bp.post("/kanban/requests/<request_id>/customer-return")
@role_required(*WRITERS)
@sensitive_operation
def register_customer_return(request_id: str):
    item = db.session.get(KanbanRequest, request_id)
    if not can_manage_request(current_user, item):
        return jsonify(error="Encaminhamento não encontrado.", code="not_found"), 404
    data = _data()
    try:
        if item.version != int(data.get("version") or 0):
            raise RuntimeError("version_conflict")
        reason = str(data.get("not_required_reason") or "").strip()[:1000]
        returned = bool(data.get("returned"))
        if not returned and len(reason) < 3:
            raise ValueError("return_or_reason_required")
        item.customer_returned_at = utcnow() if returned else None
        item.customer_returned_by_id = current_user.id if returned else None
        item.customer_return_not_required_reason = None if returned else reason
        item.version += 1
        audit("KANBAN_CUSTOMER_RETURN_RECORDED", "kanban_request", item.id, returned=returned)
        db.session.commit()
        return jsonify(request_json(item))
    except (RuntimeError, ValueError) as error:
        db.session.rollback()
        return _error(error)
