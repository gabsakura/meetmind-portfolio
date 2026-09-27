from __future__ import annotations

import logging

from app.models import AuditEvent, MeetingSubmission, SubmissionStatus

logger = logging.getLogger(__name__)


def process_submission_ingestion(submission_id: str) -> None:
    """Convert an untrusted upload into the canonical meeting model."""
    from app import create_app

    app = create_app()
    with app.app_context():
        run_submission_ingestion(submission_id)


def run_submission_ingestion(submission_id: str) -> None:
    """Run automatic ingestion inside the current application context."""
    from app.extensions import db
    from app.services.meeting_ingestion import (
        IngestionRejected,
        ingest_submission,
        reject_submission,
    )
    from app.services.submission_parsers import SubmissionParseError
    from app.services.submission_storage import SubmissionStorageError

    submission = db.session.get(MeetingSubmission, submission_id)
    retryable_error_codes = {"storage_unavailable", "ingestion_error"}
    accepted_statuses = {
        SubmissionStatus.SCREENING.value,
        SubmissionStatus.UPLOADED.value,
    }
    if not submission:
        return
    if submission.status == SubmissionStatus.FAILED.value:
        if submission.error_code not in retryable_error_codes:
            return
        submission.status = SubmissionStatus.SCREENING.value
        submission.error_code = None
        submission.version += 1
        db.session.commit()
    elif submission.status not in accepted_statuses:
        return
    try:
        ingest_submission(submission)
    except (IngestionRejected, SubmissionParseError) as error:
        db.session.rollback()
        submission = db.session.get(MeetingSubmission, submission_id)
        if submission is not None:
            reject_submission(
                submission,
                getattr(error, "code", type(error).__name__.lower()),
            )
    except SubmissionStorageError:
        db.session.rollback()
        submission = db.session.get(MeetingSubmission, submission_id)
        if submission is not None:
            submission.status = SubmissionStatus.FAILED.value
            submission.error_code = "storage_unavailable"
            submission.version += 1
            db.session.add(
                AuditEvent(
                    organization_id=submission.organization_id,
                    actor_id=submission.created_by_id,
                    action="MEETING_INGESTION_FAILED",
                    target_type="meeting_submission",
                    target_id=submission.id,
                    details={"error_code": submission.error_code},
                )
            )
            db.session.commit()
        raise
    except Exception as error:
        db.session.rollback()
        submission = db.session.get(MeetingSubmission, submission_id)
        if submission is not None:
            submission.status = SubmissionStatus.FAILED.value
            submission.error_code = "ingestion_error"
            submission.version += 1
            db.session.add(
                AuditEvent(
                    organization_id=submission.organization_id,
                    actor_id=submission.created_by_id,
                    action="MEETING_INGESTION_FAILED",
                    target_type="meeting_submission",
                    target_id=submission.id,
                    details={
                        "error_code": submission.error_code,
                        "exception_type": type(error).__name__,
                    },
                )
            )
            db.session.commit()
        logger.exception("meeting_ingestion_failed submission_id=%s", submission_id)
        raise


def cleanup_submission_files() -> int:
    from app import create_app
    from app.services.submissions import cleanup_expired

    app = create_app()
    with app.app_context():
        return cleanup_expired()


def expire_submission_file(submission_id: str) -> None:
    from app import create_app
    from app.extensions import db
    from app.services.submissions import expire, retention_expired

    app = create_app()
    with app.app_context():
        submission = db.session.get(MeetingSubmission, submission_id)
        if submission and submission.original_deleted_at is None and retention_expired(submission):
            expire(submission)


def execute_privacy_request(request_id: str) -> None:
    from app import create_app
    from app.extensions import db
    from app.models import PrivacyRequest
    from app.services.privacy_execution import execute

    app = create_app()
    with app.app_context():
        item = db.session.get(PrivacyRequest, request_id)
        if item is None or item.status == "completed":
            return
        execute(item)


def process_teams_transcript(receipt_id: str) -> None:
    from io import BytesIO

    from werkzeug.datastructures import FileStorage

    from app import create_app
    from app.extensions import db
    from app.models import Role, TeamsConnection, TeamsTranscriptReceipt, User, utcnow
    from app.services.submissions import create_submission
    from app.services.teams_graph import TeamsGraphError, download_transcript

    app = create_app()
    with app.app_context():
        receipt = db.session.get(TeamsTranscriptReceipt, receipt_id)
        if receipt is None or receipt.status == "processed":
            return
        connection = TeamsConnection.query.filter_by(
            organization_id=receipt.organization_id
        ).first()
        actor = User.query.filter_by(
            organization_id=receipt.organization_id, role=Role.ADMIN
        ).first()
        if connection is None or actor is None:
            receipt.status = "failed"
            receipt.error_code = "teams_connection_or_actor_missing"
            db.session.commit()
            return
        receipt.attempts += 1
        try:
            raw = download_transcript(connection, receipt.resource_url)
            upload = FileStorage(
                stream=BytesIO(raw),
                filename=f"teams-{receipt.transcript_id[:80]}.vtt",
                content_type="text/vtt",
            )
            submission = create_submission(
                upload,
                {
                    "consent": "true",
                    "title": "Reunião importada do Microsoft Teams",
                    "source": "teams_rsc",
                    "external_id": receipt.meeting_id or receipt.transcript_id,
                },
                actor,
            )
            receipt.submission_id = submission.id
            if submission.status == SubmissionStatus.SCREENING.value:
                run_submission_ingestion(submission.id)
                submission = db.session.get(MeetingSubmission, submission.id)
            receipt.status = (
                "processed"
                if submission.status
                in {SubmissionStatus.COMPLETED.value, SubmissionStatus.ANALYSIS_QUEUED.value}
                else "failed"
            )
            receipt.error_code = submission.error_code
            receipt.processed_at = utcnow()
            db.session.commit()
        except TeamsGraphError as error:
            receipt.status = "failed"
            receipt.error_code = error.code
            db.session.commit()
            raise


def renew_teams_subscription(connection_id: str) -> None:
    from datetime import datetime

    from app import create_app
    from app.extensions import db
    from app.models import TeamsConnection
    from app.services.teams_graph import TeamsGraphError, create_or_renew_subscription

    app = create_app()
    with app.app_context():
        connection = db.session.get(TeamsConnection, connection_id)
        if connection is None:
            return
        try:
            result = create_or_renew_subscription(connection)
            connection.subscription_id = str(result.get("id") or connection.subscription_id)
            expires = result.get("expirationDateTime")
            if expires:
                connection.subscription_expires_at = datetime.fromisoformat(
                    str(expires).replace("Z", "+00:00")
                )
            connection.status = "active"
            connection.last_error = None
            db.session.commit()
        except TeamsGraphError as error:
            connection.status = "failed"
            connection.last_error = error.code
            db.session.commit()
            raise
