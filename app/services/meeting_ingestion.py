"""Canonical and automatic ingestion of meeting transcript submissions.

Only data produced by this module may be persisted as ``Meeting``/``Segment``.
The original upload is treated as untrusted bytes and is never executed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from typing import Any

from flask import current_app

from app.extensions import db
from app.models import (
    Analysis,
    AuditEvent,
    Client,
    Meeting,
    MeetingParticipant,
    MeetingSubmission,
    Organization,
    Segment,
    SubmissionStatus,
    User,
    utcnow,
)
from app.services import file_guard
from app.services.autopsy import profile_hash as autopsy_profile_hash
from app.services.gemini_modes import analysis_profile_hash
from app.services.metrics import calculate_metrics
from app.services.outbox import add as add_outbox_event
from app.services.outbox import dispatch as dispatch_outbox_event
from app.services.sector_context import build_context_snapshot
from app.services.submission_parsers import build_segments, normalized_text, parse_submission
from app.services.submission_storage import delete, load
from app.tenancy import tenant_segment_id

PROTOCOL_VERSION = "meetmind-transcript-v1"
MAX_CANONICAL_SEGMENTS = 5_000
MAX_CANONICAL_SEGMENT_CHARS = 20_000
MAX_TIMESTAMP_MS = 7 * 24 * 60 * 60 * 1000
BLOCKED_WARNING_CODES = {"pii_detected", "prompt_injection", "formula_injection"}


class IngestionRejected(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class CanonicalTranscript:
    segments: list[dict[str, Any]]
    text: str
    sha256: str
    guard_report: dict[str, Any]


def _canonical_segments(submission: MeetingSubmission, raw: bytes) -> CanonicalTranscript:
    parsed = parse_submission(
        submission.original_filename,
        raw,
        submission.mapping_json or {},
    )
    source_segments = build_segments(parsed)
    if len(source_segments) > MAX_CANONICAL_SEGMENTS:
        raise IngestionRejected("too_many_segments", "A transcrição possui segmentos demais.")

    segments: list[dict[str, Any]] = []
    previous_started_at_ms: int | None = None
    for source in source_segments:
        text = file_guard.sanitize(str(source.get("text") or ""))
        if not text:
            continue
        if file_guard.FORMULA_INJECTION.search(text):
            raise IngestionRejected(
                "formula_injection",
                "Um segmento começa com uma fórmula de planilha perigosa.",
            )
        if len(text) > MAX_CANONICAL_SEGMENT_CHARS:
            raise IngestionRejected(
                "segment_too_large",
                "Um segmento excede o limite do protocolo canônico.",
            )
        speaker = file_guard.sanitize(str(source.get("speaker") or ""))[:200] or None
        started_at_ms = source.get("started_at_ms")
        ended_at_ms = source.get("ended_at_ms")
        if any(
            value is not None and value > MAX_TIMESTAMP_MS
            for value in (started_at_ms, ended_at_ms)
        ):
            raise IngestionRejected(
                "timestamp_out_of_range",
                "Um timestamp excede o limite de sete dias do protocolo.",
            )
        if (
            started_at_ms is not None
            and ended_at_ms is not None
            and ended_at_ms < started_at_ms
        ):
            raise IngestionRejected(
                "invalid_timestamp_order",
                "O fim de um segmento não pode ocorrer antes do início.",
            )
        if (
            started_at_ms is not None
            and previous_started_at_ms is not None
            and started_at_ms < previous_started_at_ms
        ):
            raise IngestionRejected(
                "non_monotonic_timestamps",
                "Os segmentos precisam estar em ordem cronológica.",
            )
        if started_at_ms is not None:
            previous_started_at_ms = started_at_ms
        segments.append(
            {
                "speaker": speaker,
                "text": text,
                "started_at_ms": started_at_ms,
                "ended_at_ms": ended_at_ms,
            }
        )
    if not segments:
        raise IngestionRejected("empty_transcript", "A transcrição não contém falas válidas.")

    text = normalized_text(segments)
    report = file_guard.screen("meeting.txt", text.encode("utf-8"))
    blocked_codes = {
        item.get("code")
        for item in report.get("findings", [])
        if item.get("severity") == "blocker" or item.get("code") in BLOCKED_WARNING_CODES
    }
    if blocked_codes:
        codes = sorted(str(code) for code in blocked_codes if code)
        raise IngestionRejected(
            "canonical_protocol_blocked",
            "O conteúdo não atende ao protocolo seguro de reuniões: " + ", ".join(codes),
        )
    report = {key: value for key, value in report.items() if key != "clean_text"}
    return CanonicalTranscript(
        segments=segments,
        text=text,
        sha256=sha256(text.encode("utf-8")).hexdigest(),
        guard_report=report,
    )


def _safe_external_id(submission: MeetingSubmission, transcript_hash: str) -> str:
    proposed = str((submission.proposed_metadata or {}).get("external_id") or "").strip()
    proposed = re.sub(r"[^A-Za-z0-9._-]", "-", proposed)[:100]
    return proposed or f"upload-{transcript_hash[:20]}"


def _client(submission: MeetingSubmission) -> Client | None:
    metadata = submission.proposed_metadata or {}
    client_id = str(metadata.get("client_id") or "").strip()
    if client_id:
        existing = db.session.get(Client, client_id)
        if existing is None or existing.organization_id != submission.organization_id:
            raise IngestionRejected("invalid_client", "O cliente selecionado não existe.")
        return existing
    name = str(metadata.get("client_name") or "").strip()[:200]
    if not name:
        return None
    external_id = f"upload-client-{sha256(name.casefold().encode()).hexdigest()[:16]}"
    existing = Client.query.filter_by(
        organization_id=submission.organization_id,
        external_id=external_id,
    ).one_or_none()
    if existing:
        return existing
    client = Client(
        organization_id=submission.organization_id,
        external_id=external_id,
        name=name,
        owner_id=str(metadata.get("owner_id") or submission.created_by_id),
    )
    db.session.add(client)
    db.session.flush()
    return client


def _sync_participants(submission: MeetingSubmission, meeting: Meeting) -> None:
    participant_ids = list(
        dict.fromkeys(
            [submission.created_by_id]
            + [
                str(value)
                for value in (submission.proposed_metadata or {}).get(
                    "participant_ids", []
                )
                if value
            ]
        )
    )
    existing = {
        item.user_id
        for item in MeetingParticipant.query.filter_by(meeting_id=meeting.id).all()
    }
    valid_users = {
        item.id
        for item in User.query.filter(
            User.organization_id == submission.organization_id,
            User.id.in_(participant_ids),
            User.is_active_record.is_(True),
        ).all()
    }
    if set(participant_ids) != valid_users:
        raise IngestionRejected("invalid_participant", "Um participante não está disponível.")
    for user_id in participant_ids:
        if user_id not in existing:
            db.session.add(
                MeetingParticipant(
                    organization_id=submission.organization_id,
                    meeting_id=meeting.id,
                    user_id=user_id,
                    participation_role=(
                        "organizer"
                        if user_id == submission.created_by_id
                        else "participant"
                    ),
                )
            )


def _occurred_at(submission: MeetingSubmission) -> datetime | None:
    value = str((submission.proposed_metadata or {}).get("occurred_at") or "").strip()
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise IngestionRejected("invalid_meeting_date", "A data da reunião é inválida.") from error


def _destroy_original(submission: MeetingSubmission) -> None:
    if submission.stored_object_key:
        delete(submission.stored_object_key)
        submission.stored_object_key = None
        submission.original_deleted_at = utcnow()


def reject_submission(submission: MeetingSubmission, code: str) -> None:
    """Fail closed and retain only metadata/hashes about a rejected upload."""
    _destroy_original(submission)
    submission.status = SubmissionStatus.BLOCKED.value
    submission.error_code = code[:100]
    submission.sanitized_preview = None
    submission.parser_report = {}
    submission.mapping_json = {}
    submission.version += 1
    db.session.add(
        AuditEvent(
            organization_id=submission.organization_id,
            actor_id=submission.created_by_id,
            action="MEETING_INGESTION_BLOCKED",
            target_type="meeting_submission",
            target_id=submission.id,
            details={"protocol_version": PROTOCOL_VERSION, "error_code": submission.error_code},
        )
    )
    db.session.commit()


def ingest_submission(submission: MeetingSubmission) -> Meeting:
    """Validate, canonicalize and persist one meeting in a single transaction."""
    if submission.meeting_id:
        meeting = db.session.get(Meeting, submission.meeting_id)
        if meeting is None:
            raise IngestionRejected("missing_meeting", "A reunião associada não existe.")
        return meeting
    if submission.status not in {
        SubmissionStatus.SCREENING.value,
        SubmissionStatus.UPLOADED.value,
    }:
        raise IngestionRejected("invalid_ingestion_state", "A entrada não pode ser processada.")
    if not submission.stored_object_key:
        raise IngestionRejected("missing_original", "O arquivo original não está disponível.")

    raw = load(submission.stored_object_key)
    if sha256(raw).hexdigest() != submission.file_sha256:
        raise IngestionRejected("file_hash_mismatch", "A integridade do arquivo não confere.")
    canonical = _canonical_segments(submission, raw)

    duplicate = Meeting.query.filter_by(
        organization_id=submission.organization_id,
        transcript_hash=canonical.sha256,
    ).first()
    if duplicate:
        _sync_participants(submission, duplicate)
        submission.meeting_id = duplicate.id
        submission.status = SubmissionStatus.COMPLETED.value
        submission.error_code = None
        submission.guard_report = canonical.guard_report
        submission.protocol_version = PROTOCOL_VERSION
        submission.canonical_sha256 = canonical.sha256
        submission.sanitized_preview = None
        submission.version += 1
        _destroy_original(submission)
        db.session.add(
            AuditEvent(
                organization_id=submission.organization_id,
                actor_id=submission.created_by_id,
                action="MEETING_INGESTION_DEDUPLICATED",
                target_type="meeting_submission",
                target_id=submission.id,
                details={"meeting_id": duplicate.id, "protocol_version": PROTOCOL_VERSION},
            )
        )
        db.session.commit()
        return duplicate

    external_id = _safe_external_id(submission, canonical.sha256)
    conflict = Meeting.query.filter_by(
        organization_id=submission.organization_id,
        external_id=external_id,
    ).first()
    if conflict:
        external_id = f"{external_id[:78]}-{canonical.sha256[:12]}"

    metadata = submission.proposed_metadata or {}
    organization = db.session.get(Organization, submission.organization_id)
    client = _client(submission)
    meeting = Meeting(
        organization_id=submission.organization_id,
        external_id=external_id,
        title=str(metadata.get("title") or submission.original_filename).strip()[:200],
        client_id=client.id if client else None,
        occurred_at=_occurred_at(submission),
        duration_seconds=max(
            (
                int(item["ended_at_ms"] / 1000)
                for item in canonical.segments
                if item["ended_at_ms"] is not None
            ),
            default=None,
        ),
        language=str(metadata.get("language") or "pt-BR")[:20],
        approved_for_external_ai=bool(organization and organization.external_ai_enabled),
        transcript_hash=canonical.sha256,
        metadata_json={
            "source": "automatic_ingestion",
            "submission_id": submission.id,
            "protocol_version": PROTOCOL_VERSION,
            "source_sha256": submission.file_sha256,
            "source_format": submission.file_format,
            "consent_at": submission.consent_at.isoformat(),
            "sector": metadata["sector"],
            "nome_segmento": metadata["sector_label"],
        },
        created_by_id=submission.created_by_id,
    )
    db.session.add(meeting)
    db.session.flush()
    _sync_participants(submission, meeting)
    for position, item in enumerate(canonical.segments, start=1):
        meeting.segments.append(
            Segment(
                organization_id=submission.organization_id,
                id=tenant_segment_id(
                    submission.organization_id,
                    f"{meeting.external_id}.{position:05d}",
                ),
                position=position,
                speaker=item["speaker"],
                is_totvs=bool(item["speaker"] and "totvs" in item["speaker"].casefold()),
                text=item["text"],
                started_at_ms=item["started_at_ms"],
                ended_at_ms=item["ended_at_ms"],
            )
        )
    db.session.flush()

    submission.meeting_id = meeting.id
    submission.guard_report = canonical.guard_report
    submission.protocol_version = PROTOCOL_VERSION
    submission.canonical_sha256 = canonical.sha256
    submission.sanitized_preview = None
    submission.error_code = None
    submission.version += 1
    _destroy_original(submission)

    analysis = None
    dispatch = None
    if organization and organization.external_ai_enabled and current_app.config["QUEUE_ENABLED"]:
        context = build_context_snapshot(meeting)
        analysis = Analysis(
            organization_id=submission.organization_id,
            provider=current_app.config["AI_PROVIDER"],
            meeting_id=meeting.id,
            requested_by_id=submission.created_by_id,
            model=current_app.config["GEMINI_MODEL"],
            prompt_version=current_app.config["PROMPT_VERSION"],
            score_version=current_app.config["SCORE_VERSION"],
            transcript_hash=meeting.transcript_hash,
            context_snapshot=context,
            context_version=context["context_version"],
            context_sha256=context["context_sha256"],
            mode_strategy="auto",
            requested_modes=[],
            mode_bundle_version=current_app.config["PROMPT_VERSION"],
            analysis_profile_hash=autopsy_profile_hash(
                analysis_profile_hash("auto", []),
                context,
                current_app.config.get("AUTOPSY_ASSISTANT_MODE", "off"),
            ),
            local_metrics=calculate_metrics(meeting),
        )
        db.session.add(analysis)
        db.session.flush()
        submission.analysis_id = analysis.id
        submission.status = SubmissionStatus.ANALYSIS_QUEUED.value
        dispatch = add_outbox_event(
            organization_id=submission.organization_id,
            queue_name="analyses",
            task_name="process_analysis",
            job_id=analysis.id,
            arguments=[analysis.id],
            retry_intervals=[60, 180, 600],
        )
    else:
        submission.status = SubmissionStatus.COMPLETED.value

    db.session.add(
        AuditEvent(
            organization_id=submission.organization_id,
            actor_id=submission.created_by_id,
            action="MEETING_INGESTED_AUTOMATICALLY",
            target_type="meeting",
            target_id=meeting.id,
            details={
                "submission_id": submission.id,
                "analysis_id": analysis.id if analysis else None,
                "protocol_version": PROTOCOL_VERSION,
                "segment_count": len(canonical.segments),
                "transcript_sha256": canonical.sha256,
            },
        )
    )
    db.session.commit()
    if dispatch:
        dispatch_outbox_event(dispatch.id)
    return meeting
