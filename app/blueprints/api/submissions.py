from __future__ import annotations

from flask import Blueprint, current_app, jsonify, request
from flask_login import current_user, login_required

from app.extensions import db, limiter, sensitive_request_limit
from app.jobs import run_submission_ingestion
from app.models import MeetingSubmission, Role, SubmissionStatus
from app.security import audit, role_required, sensitive_operation
from app.services.outbox import add as add_outbox_event
from app.services.outbox import dispatch as dispatch_outbox_event
from app.services.submission_parsers import SubmissionParseError
from app.services.submission_storage import SubmissionStorageError
from app.services.submissions import (
    can_access,
    canonical_document,
    create_submission,
    submission_json,
)

submission_api_bp = Blueprint("submission_api", __name__, url_prefix="/api/v1")


@submission_api_bp.post("/submissions")
@role_required(Role.ADMIN, Role.MANAGER, Role.ANALYST)
@sensitive_operation
@limiter.limit(sensitive_request_limit)
def create_submission_endpoint():
    if not request.content_type or not request.content_type.startswith("multipart/form-data"):
        return (
            jsonify(
                error="Use multipart/form-data para enviar a solicitação.",
                code="invalid_media_type",
            ),
            415,
        )
    upload = request.files.get("file")
    if upload is None:
        return jsonify(error="Selecione um arquivo.", code="missing_file"), 400
    try:
        submission = create_submission(upload, request.form, current_user)
    except SubmissionParseError as error:
        status = 413 if error.code == "payload_too_large" else 422
        return jsonify(error=str(error), code=error.code), status
    except SubmissionStorageError:
        return (
            jsonify(
                error="O armazenamento privado está indisponível.",
                code="storage_unavailable",
            ),
            503,
        )
    audit(
        "SUBMISSION_CREATED",
        "meeting_submission",
        submission.id,
        status=submission.status,
        format=submission.file_format,
        byte_size=submission.byte_size,
    )
    db.session.commit()
    response_status = 201
    if submission.status == SubmissionStatus.SCREENING.value:
        if current_app.config["QUEUE_ENABLED"]:
            ingestion_dispatch = add_outbox_event(
                organization_id=submission.organization_id,
                queue_name="screening",
                task_name="process_submission_ingestion",
                job_id=f"meeting-ingestion-{submission.id}",
                arguments=[submission.id],
                retry_intervals=[30, 120, 300],
            )
            expiry_dispatch = add_outbox_event(
                organization_id=submission.organization_id,
                queue_name="maintenance",
                task_name="expire_submission_file",
                job_id=f"submission-expiry-{submission.id}",
                arguments=[submission.id],
                scheduled_for=submission.retained_until,
            )
            db.session.commit()
            dispatch_outbox_event(ingestion_dispatch.id)
            dispatch_outbox_event(expiry_dispatch.id)
            response_status = 202
        else:
            try:
                run_submission_ingestion(submission.id)
            except SubmissionStorageError:
                return (
                    jsonify(
                        error="O armazenamento privado está indisponível.",
                        code="storage_unavailable",
                    ),
                    503,
                )
            submission = db.session.get(MeetingSubmission, submission.id)
    return (
        jsonify(submission=submission_json(submission, detailed=True)),
        response_status,
    )


@submission_api_bp.get("/submissions")
@login_required
@limiter.limit("120 per minute")
def submissions():
    page = max(1, request.args.get("page", 1, type=int))
    per_page = min(100, max(1, request.args.get("per_page", 25, type=int)))
    query = MeetingSubmission.query.filter_by(created_by_id=current_user.id)
    query = query.order_by(MeetingSubmission.created_at.desc())
    pagination = query.paginate(page=page, per_page=per_page, error_out=False)
    return jsonify(
        items=[submission_json(item) for item in pagination.items],
        page=page,
        pages=pagination.pages,
        total=pagination.total,
    )


@submission_api_bp.get("/submissions/<submission_id>")
@login_required
@limiter.limit("120 per minute")
def submission_detail(submission_id: str):
    submission = db.session.get(MeetingSubmission, submission_id)
    if not submission or not can_access(current_user, submission):
        return jsonify(error="Recurso não encontrado.", code="not_found"), 404
    return jsonify(submission=submission_json(submission, detailed=True))


@submission_api_bp.post("/submissions/<submission_id>/retry")
@login_required
@sensitive_operation
@limiter.limit(sensitive_request_limit)
def retry_submission(submission_id: str):
    submission = db.session.get(MeetingSubmission, submission_id)
    if not submission or not can_access(current_user, submission):
        return jsonify(error="Recurso não encontrado.", code="not_found"), 404
    if not (
        submission.status == SubmissionStatus.FAILED.value
        and submission.error_code in {"storage_unavailable", "ingestion_error"}
        and submission.stored_object_key
    ):
        return jsonify(
            error="Esta entrada não pode ser processada novamente.",
            code="not_retryable",
        ), 409

    submission.status = SubmissionStatus.SCREENING.value
    submission.error_code = None
    submission.version += 1
    audit("SUBMISSION_RETRY_REQUESTED", "meeting_submission", submission.id)
    db.session.commit()

    if current_app.config["QUEUE_ENABLED"]:
        dispatch = add_outbox_event(
            organization_id=submission.organization_id,
            queue_name="screening",
            task_name="process_submission_ingestion",
            job_id=f"meeting-ingestion-retry-{submission.id}-{submission.version}",
            arguments=[submission.id],
            retry_intervals=[30, 120, 300],
        )
        db.session.commit()
        dispatch_outbox_event(dispatch.id)
        response_status = 202
    else:
        run_submission_ingestion(submission.id)
        submission = db.session.get(MeetingSubmission, submission.id)
        response_status = 200
    return jsonify(submission=submission_json(submission, detailed=True)), response_status

@submission_api_bp.get("/submissions/<submission_id>/normalized")
@login_required
@limiter.limit("30 per minute")
def submission_normalized(submission_id: str):
    submission = db.session.get(MeetingSubmission, submission_id)
    if not submission or not can_access(current_user, submission):
        return jsonify(error="Recurso não encontrado.", code="not_found"), 404
    try:
        document = canonical_document(submission)
    except (SubmissionParseError, SubmissionStorageError) as error:
        return (
            jsonify(
                error=str(error),
                code=getattr(error, "code", "original_unavailable"),
            ),
            422,
        )
    audit(
        "SUBMISSION_NORMALIZED_DOWNLOADED",
        "meeting_submission",
        submission.id,
        segment_count=len(document["segments"]),
    )
    db.session.commit()
    response = jsonify(document)
    response.headers["Content-Disposition"] = (
        f'attachment; filename="{submission.id}.meetmind.json"'
    )
    return response
