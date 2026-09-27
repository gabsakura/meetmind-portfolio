from __future__ import annotations

import hashlib
import re
from datetime import UTC, timedelta
from typing import Any

from flask import current_app
from werkzeug.datastructures import FileStorage
from werkzeug.utils import secure_filename

from app.extensions import db
from app.models import (
    AuditEvent,
    Client,
    Meeting,
    MeetingSubmission,
    Organization,
    Role,
    SubmissionStatus,
    User,
    utcnow,
)
from app.services import antivirus, file_guard
from app.services.access_policy import can_access_client, can_share_with
from app.services.meeting_ingestion import PROTOCOL_VERSION
from app.services.sector_context import load_playbooks
from app.services.submission_parsers import (
    SubmissionParseError,
    detect_format,
    parse_submission,
    preview_text,
)
from app.services.submission_storage import (
    SubmissionStorageError,
    delete,
    store,
)

SAFE_LANGUAGE = {"pt-BR", "en-US", "es-ES"}


def _screen_submission(text: str) -> dict[str, Any]:
    """Apply the fail-closed rules required by automatic ingestion."""
    report = file_guard.screen("submission.txt", text.encode("utf-8"))
    blocked_warning_codes = {"pii_detected", "prompt_injection", "formula_injection"}
    report["blocked"] = any(
        item.get("severity") == "blocker" or item.get("code") in blocked_warning_codes
        for item in report.get("findings", [])
    )
    return report


def can_access(user: User, submission: MeetingSubmission) -> bool:
    return bool(
        submission.organization_id == user.organization_id
        and (user.role == Role.ADMIN or submission.created_by_id == user.id)
    )


def retention_expired(submission: MeetingSubmission) -> bool:
    retained_until = submission.retained_until
    if retained_until.tzinfo is None:
        retained_until = retained_until.replace(tzinfo=UTC)
    return utcnow() >= retained_until


def _public_report(report: dict[str, Any] | None) -> dict[str, Any]:
    return report if isinstance(report, dict) else {}


def _parser_report(parsed: Any) -> dict[str, Any]:
    """Keep parser telemetry, never untrusted rows, headers or source names."""
    report: dict[str, Any] = {
        "format": parsed.file_format,
        "row_count": len(parsed.rows),
    }
    if isinstance(parsed.quality, dict):
        report["quality"] = parsed.quality
    return report

def submission_json(submission: MeetingSubmission, *, detailed: bool = False) -> dict:
    submitter = db.session.get(User, submission.created_by_id)
    metadata = submission.proposed_metadata or {}
    payload = {
        "id": submission.id,
        "status": submission.status,
        "filename": submission.original_filename,
        "format": submission.file_format,
        "byte_size": submission.byte_size,
        "protocol_version": submission.protocol_version,
        "canonical_sha256": submission.canonical_sha256,
        "submitter": {
            "id": submitter.id,
            "name": submitter.name,
        }
        if submitter
        else None,
        "proposed_metadata": metadata,
        "source": metadata.get("source") or "manual_upload",
        "mapping": submission.mapping_json or {},
        "error_code": submission.error_code,
        "retryable": bool(
            submission.status == SubmissionStatus.FAILED.value
            and submission.error_code in {"storage_unavailable", "ingestion_error"}
            and bool(submission.stored_object_key)
        ),
        "notification": {
            "status": submission.notification_status,
            "attempts": submission.notification_attempts,
            "error": submission.notification_error,
        },
        "meeting_id": submission.meeting_id,
        "analysis_id": submission.analysis_id,
        "retained_until": submission.retained_until.isoformat(),
        "original_available": bool(
            submission.stored_object_key and not submission.original_deleted_at
        ),
        "created_at": submission.created_at.isoformat(),
        "updated_at": submission.updated_at.isoformat(),
        "version": submission.version,
    }
    if detailed:
        events = (
            AuditEvent.query.filter_by(
                target_type="meeting_submission",
                target_id=submission.id,
            )
            .order_by(AuditEvent.occurred_at.desc())
            .limit(50)
            .all()
        )
        payload.update(
            {
                "preview": (
                    submission.sanitized_preview
                    if (submission.antivirus_report or {}).get("status") == "clean"
                    else None
                ),
                "parser": _public_report(submission.parser_report),
                "guard": _public_report(submission.guard_report),
                "antivirus": _public_report(submission.antivirus_report),
                "file_sha256": submission.file_sha256,
                "timeline": [
                    {
                        "action": event.action,
                        "occurred_at": event.occurred_at.isoformat(),
                        "details": event.details or {},
                    }
                    for event in events
                ],
            }
        )
    return payload


def canonical_document(submission: MeetingSubmission) -> dict[str, Any]:
    """Export exactly the canonical data that reached the meeting tables."""
    if not submission.meeting_id:
        raise SubmissionStorageError("A reunião canônica ainda não está disponível.")
    meeting = db.session.get(Meeting, submission.meeting_id)
    if meeting is None:
        raise SubmissionStorageError("A reunião canônica não foi encontrada.")
    segments = list(meeting.segments)
    return {
        "schema_version": PROTOCOL_VERSION,
        "meeting": {
            "id": meeting.id,
            "external_id": meeting.external_id,
            "title": meeting.title,
            "client_id": meeting.client_id,
            "occurred_at": meeting.occurred_at.isoformat() if meeting.occurred_at else None,
            "duration_seconds": meeting.duration_seconds,
            "language": meeting.language,
            "transcript_sha256": meeting.transcript_hash,
            "source_filename": submission.original_filename,
        },
        "segments": [
            {
                "id": segment.id,
                "position": segment.position,
                "speaker": segment.speaker,
                "text": segment.text,
                "started_at_ms": segment.started_at_ms,
                "ended_at_ms": segment.ended_at_ms,
            }
            for segment in segments
        ],
        "normalization_report": {
            "protocol_version": PROTOCOL_VERSION,
            "source_format": submission.file_format,
            "source_sha256": submission.file_sha256,
            "segment_count": len(segments),
            "generated_at": utcnow().isoformat(),
            "submission_id": submission.id,
            "guard": _public_report(submission.guard_report),
            "antivirus": _public_report(submission.antivirus_report),
        },
    }

def sanitize_metadata(values: Any) -> dict[str, Any]:
    raw_fields = {
        "external_id": str(values.get("external_id") or ""),
        "title": str(values.get("title") or ""),
        "client_name": str(values.get("client_name") or ""),
        "occurred_at": str(values.get("occurred_at") or ""),
        "sector": str(values.get("sector") or ""),
    }
    metadata_report = file_guard.screen(
        "metadata.txt",
        "\n".join(f"{key}: {value}" for key, value in raw_fields.items()).encode("utf-8"),
    )
    unsafe_codes = {
        item.get("code")
        for item in metadata_report.get("findings", [])
        if (
            item.get("code") != "too_short"
            and (
                item.get("severity") == "blocker"
                or item.get("code")
                in {"pii_detected", "prompt_injection", "formula_injection"}
            )
        )
    }
    if unsafe_codes:
        raise SubmissionParseError(
            "unsafe_metadata",
            "Os metadados contêm conteúdo inseguro: "
            + ", ".join(sorted(str(code) for code in unsafe_codes if code)),
        )

    language = str(values.get("language") or "pt-BR")
    if language not in SAFE_LANGUAGE:
        language = "pt-BR"
    sector_id = str(values.get("sector") or "").strip()
    playbook = load_playbooks().get(sector_id)
    if playbook is None:
        raise SubmissionParseError(
            "invalid_sector",
            "Selecione um setor válido para orientar a leitura da IA.",
        )
    source = str(values.get("source") or "manual_upload").strip()
    if source not in {"manual_upload", "teams_rsc"}:
        source = "manual_upload"
    return {
        "external_id": re.sub(r"[^A-Za-z0-9._-]", "-", raw_fields["external_id"])[:100],
        "title": file_guard.sanitize(raw_fields["title"])[:200],
        "client_name": file_guard.sanitize(raw_fields["client_name"])[:200],
        "occurred_at": file_guard.sanitize(raw_fields["occurred_at"])[:40],
        "language": language,
        "sector": sector_id,
        "sector_label": playbook.label,
        "source": source,
    }


def create_submission(
    upload: FileStorage,
    form: Any,
    user: User,
) -> MeetingSubmission:
    filename = secure_filename(upload.filename or "")[:255]
    if not filename:
        raise SubmissionParseError("missing_filename", "Selecione um arquivo.")
    if str(form.get("consent") or "").casefold() not in {"true", "1", "on", "yes"}:
        raise SubmissionParseError(
            "consent_required",
            "Confirme que você possui autorização para processar a reunião.",
        )
    raw = upload.read()
    limit = int(current_app.config["MAX_SUBMISSION_BYTES"])
    if not raw:
        raise SubmissionParseError("empty_file", "O arquivo está vazio.")
    if len(raw) > limit:
        raise SubmissionParseError(
            "payload_too_large",
            f"O arquivo excede {limit // 1048576} MB.",
        )
    file_format = detect_format(filename)
    file_hash = hashlib.sha256(raw).hexdigest()
    organization = db.session.get(Organization, user.organization_id)
    retention_days = (
        organization.retention_days
        if organization is not None
        else int(current_app.config["SUBMISSION_RETENTION_DAYS"])
    )
    retained_until = utcnow() + timedelta(days=retention_days)
    av = antivirus.scan(raw)
    av_blocks = av["status"] == "infected" or (
        current_app.config.get("SUBMISSION_REQUIRE_ANTIVIRUS", False) and av["status"] != "clean"
    )
    proposed_metadata = sanitize_metadata(form)
    client_id = str(form.get("client_id") or "").strip() or None
    client = db.session.get(Client, client_id) if client_id else None
    if client_id and not can_access_client(user, client):
        raise SubmissionParseError("invalid_client", "Selecione um cliente permitido.")
    owner_id = (
        client.owner_id
        if client is not None
        else str(form.get("owner_id") or user.id).strip() or user.id
    )
    owner = db.session.get(User, owner_id)
    if owner_id != user.id and not can_share_with(user, owner):
        raise SubmissionParseError("invalid_owner", "Selecione um responsável permitido.")
    raw_participants = (
        form.getlist("participant_ids")
        if hasattr(form, "getlist")
        else form.get("participant_ids", [])
    )
    if isinstance(raw_participants, str):
        raw_participants = raw_participants.split(",")
    participant_ids = list(
        dict.fromkeys(
            [user.id]
            + [str(value).strip() for value in raw_participants if str(value).strip()]
        )
    )[:50]
    for participant_id in participant_ids:
        participant = db.session.get(User, participant_id)
        if participant_id != user.id and not can_share_with(user, participant):
            raise SubmissionParseError(
                "invalid_participant",
                "Um dos participantes não pertence à equipe permitida.",
            )
    proposed_metadata.update(
        {
            "client_id": client.id if client else None,
            "owner_id": owner_id,
            "participant_ids": participant_ids,
        }
    )
    client_id = str(form.get("client_id") or "").strip() or None
    client = db.session.get(Client, client_id) if client_id else None
    if client_id and not can_access_client(user, client):
        raise SubmissionParseError("invalid_client", "Selecione um cliente permitido.")
    owner_id = (
        client.owner_id
        if client is not None
        else str(form.get("owner_id") or user.id).strip() or user.id
    )
    owner = db.session.get(User, owner_id)
    if not can_share_with(user, owner) and owner_id != user.id:
        raise SubmissionParseError("invalid_owner", "Selecione um responsável permitido.")
    raw_participants = (
        form.getlist("participant_ids")
        if hasattr(form, "getlist")
        else form.get("participant_ids", [])
    )
    if isinstance(raw_participants, str):
        raw_participants = raw_participants.split(",")
    participant_ids = list(
        dict.fromkeys(
            [user.id]
            + [str(value).strip() for value in raw_participants if str(value).strip()]
        )
    )[:50]
    for participant_id in participant_ids:
        participant = db.session.get(User, participant_id)
        if participant_id != user.id and not can_share_with(user, participant):
            raise SubmissionParseError(
                "invalid_participant", "Um dos participantes não pertence à equipe permitida."
            )
    proposed_metadata.update(
        {
            "client_id": client.id if client else None,
            "owner_id": owner_id,
            "participant_ids": participant_ids,
        }
    )
    base = dict(
        organization_id=user.organization_id,
        original_filename=filename,
        file_format=file_format,
        content_type=re.sub(
            r"[^A-Za-z0-9.+;= /_-]",
            "",
            file_guard.sanitize(upload.mimetype or "application/octet-stream"),
        )[:120],
        byte_size=len(raw),
        file_sha256=file_hash,
        protocol_version=PROTOCOL_VERSION,
        retained_until=retained_until,
        consent_at=utcnow(),
        proposed_metadata=proposed_metadata,
        created_by_id=user.id,
        antivirus_report=av,
    )
    if av_blocks:
        submission = MeetingSubmission(
            **base,
            status=SubmissionStatus.BLOCKED.value,
            error_code="antivirus_blocked",
            guard_report={},
            parser_report={},
            mapping_json={},
            notification_status="not_requested",
        )
        db.session.add(submission)
        db.session.commit()
        return submission
    try:
        parsed = parse_submission(filename, raw)
        preview = preview_text(parsed)
        guard = _screen_submission(preview)
    except SubmissionParseError as error:
        submission = MeetingSubmission(
            **base,
            status=SubmissionStatus.BLOCKED.value,
            error_code=error.code,
            guard_report={"blocked": True, "findings": [{"detail": str(error)}]},
            parser_report={},
            mapping_json={},
            notification_status="not_requested",
        )
        db.session.add(submission)
        db.session.commit()
        return submission
    if guard["blocked"]:
        submission = MeetingSubmission(
            **base,
            status=SubmissionStatus.BLOCKED.value,
            error_code="structural_screening_blocked",
            guard_report={key: value for key, value in guard.items() if key != "clean_text"},
            parser_report=_parser_report(parsed),
            mapping_json={},
            sanitized_preview=None,
            notification_status="not_requested",
        )
        db.session.add(submission)
        db.session.commit()
        return submission
    object_key = store(raw)
    submission = MeetingSubmission(
        **base,
        status=SubmissionStatus.SCREENING.value,
        stored_object_key=object_key,
        guard_report={key: value for key, value in guard.items() if key != "clean_text"},
        parser_report=_parser_report(parsed),
        mapping_json={},
        sanitized_preview=preview,
        notification_status="not_requested",
    )
    db.session.add(submission)
    db.session.commit()
    return submission


def expire(submission: MeetingSubmission) -> None:
    previous_status = submission.status
    if submission.stored_object_key and not submission.original_deleted_at:
        delete(submission.stored_object_key)
        submission.original_deleted_at = utcnow()
        submission.stored_object_key = None
    if submission.status in {
        SubmissionStatus.UPLOADED.value,
        SubmissionStatus.SCREENING.value,
    }:
        submission.status = SubmissionStatus.EXPIRED.value
        submission.error_code = "retention_expired"
    submission.version += 1
    db.session.add(
        AuditEvent(
            organization_id=submission.organization_id,
            action="SUBMISSION_ORIGINAL_EXPIRED",
            target_type="meeting_submission",
            target_id=submission.id,
            details={
                "previous_status": previous_status,
                "status": submission.status,
            },
        )
    )
    db.session.commit()


def cleanup_expired() -> int:
    expired = MeetingSubmission.query.filter(
        MeetingSubmission.retained_until <= utcnow(),
        MeetingSubmission.original_deleted_at.is_(None),
    ).all()
    for submission in expired:
        expire(submission)
    return len(expired)
