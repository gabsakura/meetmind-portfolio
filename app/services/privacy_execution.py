from __future__ import annotations

import json
from hashlib import sha256

from app.extensions import db
from app.models import (
    AnalysisFeedback,
    KanbanRequest,
    MeetingSubmission,
    Organization,
    PrivacyExecution,
    PrivacyRequest,
    ProductEvent,
    User,
    utcnow,
)
from app.services.submission_storage import delete


def execute(request_item: PrivacyRequest) -> PrivacyExecution:
    if request_item.request_type != "deletion" or request_item.status not in {
        "approved",
        "failed",
    }:
        raise ValueError("privacy_request_not_executable")
    organization = request_item.organization_id
    organization_record = db.session.get(Organization, organization)
    if organization_record is None or organization_record.legal_hold_enabled:
        raise ValueError("legal_hold")
    if request_item.requested_by_id is None:
        raise ValueError("privacy_subject_missing")
    user = db.session.get(User, request_item.requested_by_id)
    if user is None:
        raise ValueError("privacy_subject_missing")
    if user.organization_id != organization:
        raise ValueError("privacy_tenant_mismatch")
    if user.is_break_glass:
        raise ValueError("break_glass_cannot_be_anonymized")

    attempts = PrivacyExecution.query.filter_by(privacy_request_id=request_item.id).count()
    execution = PrivacyExecution(
        organization_id=organization,
        privacy_request_id=request_item.id,
        attempt=attempts + 1,
    )
    db.session.add(execution)
    request_item.status = "processing"
    db.session.commit()

    deleted_objects = 0
    missing_objects = 0
    try:
        submissions = MeetingSubmission.query.filter_by(created_by_id=user.id).all()
        for submission in submissions:
            if submission.stored_object_key:
                if delete(submission.stored_object_key):
                    deleted_objects += 1
                else:
                    missing_objects += 1
                submission.stored_object_key = None
                submission.original_deleted_at = utcnow()

        feedback_items = AnalysisFeedback.query.filter_by(reviewer_id=user.id).all()
        for feedback in feedback_items:
            feedback.comment = None
            feedback.correction = None

        product_events = ProductEvent.query.filter_by(user_id=user.id).delete(
            synchronize_session=False
        )
        kanban_requests = KanbanRequest.query.filter_by(created_by_id=user.id).all()
        for kanban_request in kanban_requests:
            kanban_request.note = None
            kanban_request.payload_json = {
                **(kanban_request.payload_json or {}),
                "note": None,
            }
        pseudonym = sha256(f"{organization}:{user.id}".encode()).hexdigest()[:20]
        user.email = f"deleted-{pseudonym}@invalid.local"
        user.name = "Usuário anonimizado"
        user.auth_subject = None
        user.password_hash = "!anonymized"
        user.is_active_record = False
        user.session_version += 1

        manifest = {
            "schema_version": "meetmind-privacy-execution-v1",
            "request_id": request_item.id,
            "subject_pseudonym": pseudonym,
            "deleted_objects": deleted_objects,
            "missing_objects": missing_objects,
            "submissions_processed": len(submissions),
            "feedback_texts_removed": len(feedback_items),
            "product_events_removed": product_events,
            "kanban_notes_removed": len(kanban_requests),
            "corporate_records_preserved": True,
            "completed_at": utcnow().isoformat(),
        }
        encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
        execution.manifest = manifest
        execution.manifest_sha256 = sha256(encoded).hexdigest()
        execution.status = "completed"
        execution.completed_at = utcnow()
        request_item.status = "completed"
        request_item.completed_at = execution.completed_at
        request_item.resolution = "Anonimização controlada concluída e verificada."
        db.session.commit()
        return execution
    except Exception as error:
        db.session.rollback()
        execution = db.session.get(PrivacyExecution, execution.id)
        request_item = db.session.get(PrivacyRequest, request_item.id)
        execution.status = "failed"
        execution.error_code = type(error).__name__[:100]
        execution.completed_at = utcnow()
        request_item.status = "failed"
        db.session.commit()
        raise
