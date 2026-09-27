from __future__ import annotations

from app.extensions import db
from app.models import (
    ActionItem,
    Client,
    ClientContact,
    CommunicationDraft,
    CommunicationDraftStatus,
    CommunicationRevision,
    KanbanRequest,
    Meeting,
    Segment,
    User,
    utcnow,
)
from app.services.access_policy import (
    can_access_action,
    can_access_client,
    can_access_meeting,
    can_access_request,
    can_manage_client,
)

DRAFT_TYPES = {
    "customer_message",
    "internal_request",
    "follow_up",
    "calendar_invite",
    "executive_summary",
}
CHANNELS = {"email", "teams", "calendar", "internal", "other"}


def contacts_for(client: Client, ids: list[str]) -> list[ClientContact]:
    unique_ids = list(dict.fromkeys(str(value) for value in ids if value))
    if not unique_ids:
        return []
    contacts = ClientContact.query.filter(
        ClientContact.organization_id == client.organization_id,
        ClientContact.client_id == client.id,
        ClientContact.id.in_(unique_ids),
        ClientContact.is_active.is_(True),
    ).all()
    if {item.id for item in contacts} != set(unique_ids):
        raise ValueError("invalid_recipients")
    return contacts


def resolve_sources(
    user: User,
    client: Client,
    *,
    meeting_id: str | None,
    action_id: str | None,
    request_id: str | None,
) -> tuple[Meeting | None, ActionItem | None, KanbanRequest | None]:
    meeting = db.session.get(Meeting, meeting_id) if meeting_id else None
    action = db.session.get(ActionItem, action_id) if action_id else None
    item = db.session.get(KanbanRequest, request_id) if request_id else None
    if meeting_id and (
        not can_access_meeting(user, meeting) or meeting.client_id != client.id
    ):
        raise LookupError("source_not_found")
    if action_id and (
        not can_access_action(user, action) or action.client_id != client.id
    ):
        raise LookupError("source_not_found")
    if request_id and (
        not can_access_request(user, item) or item.client_id != client.id
    ):
        raise LookupError("source_not_found")
    if meeting is None and action is not None:
        meeting = db.session.get(Meeting, action.meeting_id)
    if meeting is None and item is not None and item.meeting_id:
        meeting = db.session.get(Meeting, item.meeting_id)
    return meeting, action, item


def validated_evidence(
    meeting: Meeting | None,
    requested_ids: list[str],
    action: ActionItem | None,
) -> list[Segment]:
    requested = [str(value) for value in requested_ids if value]
    ids = list(
        dict.fromkeys(
            requested
            if requested
            else list(action.evidence_segment_ids or []) if action else []
        )
    )
    if not ids:
        return []
    if meeting is None:
        raise ValueError("invalid_evidence")
    segments = Segment.query.filter(
        Segment.organization_id == meeting.organization_id,
        Segment.meeting_id == meeting.id,
        Segment.id.in_(ids),
    ).order_by(Segment.position.asc()).all()
    if {item.id for item in segments} != set(ids):
        raise ValueError("invalid_evidence")
    return segments


def create(
    user: User,
    *,
    client_id: str,
    draft_type: str,
    channel: str,
    recipient_contact_ids: list[str],
    meeting_id: str | None = None,
    action_id: str | None = None,
    request_id: str | None = None,
    evidence_segment_ids: list[str] | None = None,
    subject: str,
    body: str,
    generated_by_ai: bool = False,
    generation_metadata: dict | None = None,
) -> CommunicationDraft:
    client = db.session.get(Client, client_id)
    if not can_manage_client(user, client):
        raise LookupError("client_not_found")
    if draft_type not in DRAFT_TYPES or channel not in CHANNELS:
        raise ValueError("invalid_type")
    recipients = contacts_for(client, recipient_contact_ids)
    meeting, action, item = resolve_sources(
        user,
        client,
        meeting_id=meeting_id,
        action_id=action_id,
        request_id=request_id,
    )
    segments = validated_evidence(meeting, evidence_segment_ids or [], action)
    subject, body = subject.strip()[:500], body.strip()[:6_000]
    if not subject or not body:
        raise ValueError("content_required")
    if generated_by_ai and not segments:
        raise ValueError("evidence_required")
    draft = CommunicationDraft(
        organization_id=user.organization_id,
        client_id=client.id,
        meeting_id=meeting.id if meeting else None,
        action_id=action.id if action else None,
        kanban_request_id=item.id if item else None,
        draft_type=draft_type,
        channel=channel,
        recipient_contact_ids=[contact.id for contact in recipients],
        created_by_id=user.id,
    )
    db.session.add(draft)
    db.session.flush()
    db.session.add(
        CommunicationRevision(
            organization_id=user.organization_id,
            communication_draft_id=draft.id,
            revision_number=1,
            subject=subject,
            body=body,
            evidence_segment_ids=[segment.id for segment in segments],
            generated_by_ai=generated_by_ai,
            generation_metadata=generation_metadata or {},
            created_by_id=user.id,
        )
    )
    db.session.flush()
    return draft


def revision(
    draft: CommunicationDraft, number: int | None = None
) -> CommunicationRevision:
    return CommunicationRevision.query.filter_by(
        organization_id=draft.organization_id,
        communication_draft_id=draft.id,
        revision_number=number or draft.current_revision,
    ).one()


def can_access(user: User, draft: CommunicationDraft | None) -> bool:
    if draft is None or draft.organization_id != user.organization_id:
        return False
    return can_access_client(user, db.session.get(Client, draft.client_id))


def can_edit(user: User, draft: CommunicationDraft | None) -> bool:
    return bool(
        can_access(user, draft)
        and can_manage_client(user, db.session.get(Client, draft.client_id))
    )


def add_revision(
    draft: CommunicationDraft,
    user: User,
    *,
    expected_version: int,
    subject: str,
    body: str,
    recipient_contact_ids: list[str],
) -> CommunicationRevision:
    if not can_edit(user, draft):
        raise LookupError("not_found")
    if draft.version != expected_version:
        raise RuntimeError("version_conflict")
    if draft.status not in {
        CommunicationDraftStatus.DRAFT.value,
        CommunicationDraftStatus.REVIEWED.value,
    }:
        raise ValueError("invalid_state")
    subject, body = subject.strip()[:500], body.strip()[:6_000]
    if not subject or not body:
        raise ValueError("content_required")
    client = db.session.get(Client, draft.client_id)
    contacts = contacts_for(client, recipient_contact_ids)
    previous = revision(draft)
    draft.current_revision += 1
    draft.version += 1
    draft.status = CommunicationDraftStatus.REVIEWED.value
    draft.recipient_contact_ids = [item.id for item in contacts]
    item = CommunicationRevision(
        organization_id=draft.organization_id,
        communication_draft_id=draft.id,
        revision_number=draft.current_revision,
        subject=subject,
        body=body,
        evidence_segment_ids=list(previous.evidence_segment_ids or []),
        generated_by_ai=False,
        generation_metadata={},
        created_by_id=user.id,
    )
    db.session.add(item)
    db.session.flush()
    return item


def approve(draft: CommunicationDraft, user: User, expected_version: int) -> None:
    if not can_edit(user, draft):
        raise LookupError("not_found")
    if draft.version != expected_version:
        raise RuntimeError("version_conflict")
    if draft.status not in {
        CommunicationDraftStatus.DRAFT.value,
        CommunicationDraftStatus.REVIEWED.value,
    }:
        raise ValueError("invalid_state")
    draft.status = CommunicationDraftStatus.APPROVED.value
    draft.approved_revision = draft.current_revision
    draft.approved_by_id = user.id
    draft.approved_at = utcnow()
    draft.version += 1


def json_data(draft: CommunicationDraft, *, include_history: bool = True) -> dict:
    current = revision(draft)
    contacts = (
        ClientContact.query.filter(
            ClientContact.organization_id == draft.organization_id,
            ClientContact.id.in_(draft.recipient_contact_ids or []),
        ).all()
        if draft.recipient_contact_ids
        else []
    )
    data = {
        "id": draft.id,
        "client_id": draft.client_id,
        "meeting_id": draft.meeting_id,
        "action_id": draft.action_id,
        "request_id": draft.kanban_request_id,
        "draft_type": draft.draft_type,
        "channel": draft.channel,
        "status": draft.status,
        "subject": current.subject,
        "body": current.body,
        "evidence_segment_ids": current.evidence_segment_ids or [],
        "recipients": [
            {"id": item.id, "name": item.name, "email": item.email}
            for item in contacts
        ],
        "current_revision": draft.current_revision,
        "approved_revision": draft.approved_revision,
        "version": draft.version,
        "external_provider": draft.external_provider,
        "external_id": draft.external_id,
        "external_url": draft.external_url,
        "created_at": draft.created_at.isoformat(),
        "updated_at": draft.updated_at.isoformat(),
    }
    if include_history:
        data["revisions"] = [
            {
                "number": item.revision_number,
                "subject": item.subject,
                "body": item.body,
                "generated_by_ai": item.generated_by_ai,
                "created_by_id": item.created_by_id,
                "created_at": item.created_at.isoformat(),
            }
            for item in CommunicationRevision.query.filter_by(
                organization_id=draft.organization_id,
                communication_draft_id=draft.id,
            ).order_by(CommunicationRevision.revision_number.asc())
        ]
    return data
