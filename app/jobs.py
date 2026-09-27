from __future__ import annotations

import difflib
import json
import logging
import re
from copy import deepcopy
from datetime import UTC, datetime
from hashlib import sha256

import httpx
from flask import current_app
from google.genai.errors import ClientError, ServerError

from app.models import (
    AIExecution,
    Analysis,
    AnalysisAutopsyRun,
    AnalysisStatus,
    AuditEvent,
    MeetingSubmission,
    Segment,
    SubmissionStatus,
    utcnow,
)
from app.services.ai_provider import is_configured as ai_is_configured
from app.services.autopsy import (
    assisted_candidate,
    build_review_guide,
    bundle_sha256,
    compare_decisions,
    load_bundle,
    normalize_mode,
)
from app.services.gemini import (
    ANALYSIS_POLICY_SHA256,
    GEMINI_RESPONSE_SCHEMA_SHA256,
    InvalidModelOutput,
    analyze_transcript,
    safe_provider_error,
)
from app.services.gemini_autopsy import review_transcript_with_autopsy
from app.services.gemini_modes import (
    MODE_BUNDLE_SHA256,
    MODE_BUNDLE_VERSION,
    analyze_transcript_modes,
)
from app.services.metrics import transcript_text
from app.services.privacy import detect_pii

logger = logging.getLogger(__name__)


def _literal_quote_alignment(quote: str, source: str) -> tuple[str, str] | None:
    if quote in source:
        return quote, "exact"
    quote_words = [match.group(0).casefold() for match in re.finditer(r"\w+", quote)]
    source_matches = list(re.finditer(r"\w+", source))
    source_words = [match.group(0).casefold() for match in source_matches]
    if len(quote_words) < 3 or len(source_words) < 3:
        return None
    match = difflib.SequenceMatcher(
        None,
        quote_words,
        source_words,
        autojunk=False,
    ).find_longest_match()
    if match.size < 3 or match.size / len(quote_words) < 0.5:
        return None
    start_word = max(0, match.b - 3)
    end_word = min(len(source_matches) - 1, match.b + match.size + 2)
    start = source_matches[start_word].start()
    end = min(len(source), source_matches[end_word].end())
    if end - start > 350:
        end = min(len(source), start + 350)
    return source[start:end].strip(), "deterministic_alignment"


def _validated_result_payload(
    result,
    segments: list[Segment],
    analysis: Analysis,
    format_repair_count: int = 0,
) -> dict:
    segments_by_id = {segment.id: segment for segment in segments}
    payload = result.model_dump()
    evidence_count = 0
    verification_counts = {"exact": 0, "deterministic_alignment": 0}
    discarded_reference_count = 0
    reassigned_evidence_segment_count = 0

    def enrich_evidence(evidence, target: dict) -> None:
        nonlocal evidence_count, reassigned_evidence_segment_count
        segment = segments_by_id.get(evidence.segment_id)
        if not segment:
            literal_matches = [
                candidate
                for candidate in segments
                if evidence.quote and evidence.quote in candidate.text
            ]
            if len(literal_matches) != 1:
                raise InvalidModelOutput("unknown_evidence_segment")
            segment = literal_matches[0]
            target["segment_id"] = segment.id
            reassigned_evidence_segment_count += 1
        aligned = _literal_quote_alignment(evidence.quote, segment.text)
        if not aligned:
            raise InvalidModelOutput("evidence_quote_not_literal")
        literal_quote, verification_method = aligned
        verification_counts[verification_method] += 1
        target.update(
            {
                "quote": literal_quote,
                "verification_method": verification_method,
                "position": segment.position,
                "speaker": segment.speaker,
                "started_at_ms": segment.started_at_ms,
                "ended_at_ms": segment.ended_at_ms,
            }
        )
        evidence_count += 1

    for finding_index, finding in enumerate(result.findings):
        for evidence_index, evidence in enumerate(finding.evidence):
            enriched = payload["findings"][finding_index]["evidence"][evidence_index]
            enrich_evidence(evidence, enriched)

    for signal_index, signal in enumerate(result.retention_signals):
        for evidence_index, evidence in enumerate(signal.evidence):
            enriched = payload["retention_signals"][signal_index]["evidence"][evidence_index]
            enrich_evidence(evidence, enriched)

    valid_ids = set(segments_by_id)

    def verified_references(values: list[str]) -> list[str]:
        nonlocal discarded_reference_count
        discarded_reference_count += sum(value not in valid_ids for value in values)
        verified = list(dict.fromkeys(value for value in values if value in valid_ids))
        return verified

    def verified_evidence_items(items: list, payload_items: list[dict]) -> list[dict]:
        nonlocal discarded_reference_count
        verified_items: list[dict] = []
        for evidence, evidence_payload in zip(items, payload_items, strict=False):
            try:
                enrich_evidence(evidence, evidence_payload)
            except InvalidModelOutput:
                discarded_reference_count += 1
                continue
            verified_items.append(evidence_payload)
        return verified_items

    brief = payload.get("manufacturing_brief") or {}
    model_brief = result.manufacturing_brief
    brief["what_happened_evidence"] = verified_evidence_items(
        model_brief.what_happened_evidence,
        brief.get("what_happened_evidence") or [],
    )
    if not brief["what_happened_evidence"]:
        brief["what_happened"] = (
            "Não foi possível confirmar uma descrição geral com citação válida."
        )
        brief.setdefault("uncertainties", []).append(
            "O resumo operacional não recebeu evidência literal válida."
        )

    verified_requests = []
    for request_model, request_payload in zip(
        model_brief.customer_requests,
        brief.get("customer_requests") or [],
        strict=False,
    ):
        request_payload["evidence"] = verified_evidence_items(
            request_model.evidence,
            request_payload.get("evidence") or [],
        )
        if request_payload["evidence"]:
            verified_requests.append(request_payload)
    brief["customer_requests"] = verified_requests

    neutral_status = {
        "objective": "unclear",
        "follow_up": "unclear",
        "conflict": "unclear",
        "concern": "unclear",
    }
    for field_name in neutral_status:
        decision_model = getattr(model_brief, field_name)
        decision_payload = brief.get(field_name) or {}
        decision_payload["evidence"] = verified_evidence_items(
            decision_model.evidence,
            decision_payload.get("evidence") or [],
        )
        affirmative = decision_model.status not in {"none", "none_detected", "unclear"}
        if affirmative and not decision_payload["evidence"]:
            decision_payload.update(
                {
                    "status": neutral_status[field_name],
                    "rationale": "A conclusão não recebeu evidência literal válida.",
                    "confidence": 0.0,
                }
            )
            brief.setdefault("uncertainties", []).append(
                f"O campo {field_name} foi rebaixado por falta de evidência válida."
            )
        brief[field_name] = decision_payload

    verified_brief_actions = []
    for action_model, action_payload in zip(
        model_brief.suggested_actions,
        brief.get("suggested_actions") or [],
        strict=False,
    ):
        references = verified_references(action_model.evidence_segment_ids)
        if not references:
            continue
        action_payload["evidence_segment_ids"] = references
        verified_brief_actions.append(action_payload)
    brief["suggested_actions"] = verified_brief_actions
    brief["uncertainties"] = list(dict.fromkeys(brief.get("uncertainties") or []))[:3]
    payload["manufacturing_brief"] = brief

    verified_actions = []
    for action_index, action in enumerate(result.action_items):
        references = verified_references(action.evidence_segment_ids)
        if not references:
            continue
        action_payload = payload["action_items"][action_index]
        action_payload["evidence_segment_ids"] = references
        verified_actions.append(action_payload)
    payload["action_items"] = verified_actions
    if verified_brief_actions:
        payload["action_items"] = verified_brief_actions

    classification_references = verified_references(
        result.meeting_classification.evidence_segment_ids
    )
    payload["meeting_classification"]["evidence_segment_ids"] = classification_references
    if not classification_references:
        payload["meeting_classification"].update({"meeting_type": "unknown", "confidence": 0.0})
        payload.setdefault("uncertainties", []).append(
            "A classificação da reunião não recebeu uma referência de segmento válida."
        )

    relationship_references = verified_references(result.relationship_health.evidence_segment_ids)
    payload["relationship_health"]["evidence_segment_ids"] = relationship_references
    if not relationship_references:
        payload["relationship_health"].update(
            {
                "status": "insufficient_evidence",
                "rationale": (
                    "Não há referência de segmento válida para avaliar a saúde da relação."
                ),
                "confidence": 0.0,
            }
        )
        payload.setdefault("uncertainties", []).append(
            "A saúde da relação não recebeu uma referência de segmento válida."
        )

    context = analysis.context_snapshot or {}
    selection = context.get("selection") or {}
    account = context.get("account") or {}
    playbook = context.get("playbook") or {}
    payload["meeting_classification"].update(
        {
            "sector": playbook.get("label") or "Contexto genérico",
            "subsector": selection.get("subsector"),
            "sector_source": selection.get("source") or "fallback",
        }
    )
    payload["account_context"] = {
        "sector": playbook.get("label") or "Contexto genérico",
        "subsector": selection.get("subsector"),
        "revenue_band": account.get("revenue_band"),
        "completeness": account.get("completeness", 0),
        "missing_fields": account.get("missing_fields") or [],
        "limitations": (context.get("uncertainties") or [])[:5],
    }
    contextual_values = {
        "sector": payload["account_context"]["sector"],
        "subsector": payload["account_context"]["subsector"],
        "revenue_band": account.get("revenue_band"),
    }
    payload["context_application"] = [
        {**item, "value": contextual_values[item["factor"]]}
        for item in payload.get("context_application") or []
        if contextual_values.get(item.get("factor"))
    ]
    payload["uncertainties"] = list(
        dict.fromkeys(
            [
                *(payload.get("uncertainties") or []),
                *(context.get("uncertainties") or []),
            ]
        )
    )[:3]

    payload["schema_version"] = "meetmind-manufacturing-v1"
    payload["traceability"] = {
        "policy_version": analysis.prompt_version,
        "policy_sha256": ANALYSIS_POLICY_SHA256,
        "score_version": analysis.score_version,
        "model": analysis.model,
        "transcript_hash": analysis.transcript_hash,
        "context_version": analysis.context_version,
        "context_sha256": analysis.context_sha256,
        "playbook_id": playbook.get("id"),
        "playbook_version": playbook.get("version"),
        "sector_source": selection.get("source"),
        "context_completeness": account.get("completeness"),
        "context_sources": [source.get("url") for source in context.get("sources") or []],
        "evidence_count": evidence_count,
        "exact_evidence_count": verification_counts["exact"],
        "realigned_evidence_count": verification_counts["deterministic_alignment"],
        "reassigned_evidence_segment_count": reassigned_evidence_segment_count,
        "discarded_reference_count": discarded_reference_count,
        "format_repair_count": format_repair_count,
        "generated_at": datetime.now(UTC).isoformat(),
        "evidence_policy": "segment_id_and_backend_literal_quote_verified",
    }
    return payload


def _validated_modes_payload(
    result_payload: dict,
    segments: list[Segment],
    analysis: Analysis,
    format_repair_count: int = 0,
) -> dict:
    segments_by_id = {segment.id: segment for segment in segments}
    payload = deepcopy(result_payload)
    verification_counts = {"exact": 0, "deterministic_alignment": 0}
    evidence_count = 0

    def enrich(value):
        nonlocal evidence_count
        if isinstance(value, list):
            for item in value:
                enrich(item)
            return
        if not isinstance(value, dict):
            return
        if {"segment_id", "quote", "relevance"}.issubset(value):
            segment = segments_by_id.get(value["segment_id"])
            if not segment:
                raise InvalidModelOutput("unknown_evidence_segment")
            aligned = _literal_quote_alignment(value["quote"], segment.text)
            if not aligned:
                raise InvalidModelOutput("evidence_quote_not_literal")
            literal_quote, verification_method = aligned
            verification_counts[verification_method] += 1
            value.update(
                {
                    "quote": literal_quote,
                    "verification_method": verification_method,
                    "position": segment.position,
                    "speaker": segment.speaker,
                    "started_at_ms": segment.started_at_ms,
                    "ended_at_ms": segment.ended_at_ms,
                }
            )
            evidence_count += 1
        for child in list(value.values()):
            enrich(child)

    enrich(payload.get("routing"))
    enrich(payload.get("mode_results"))
    synthesis_ids = set(payload.get("synthesis", {}).get("evidence_segment_ids", []))
    if not synthesis_ids.issubset(segments_by_id):
        raise InvalidModelOutput("unknown_synthesis_evidence_segment")

    context = analysis.context_snapshot or {}
    traceability = payload.setdefault("traceability", {})
    traceability.update(
        {
            "score_version": analysis.score_version,
            "model": analysis.model,
            "transcript_hash": analysis.transcript_hash,
            "context_version": analysis.context_version,
            "context_sha256": analysis.context_sha256,
            "analysis_profile_hash": analysis.analysis_profile_hash,
            "evidence_count": evidence_count,
            "exact_evidence_count": verification_counts["exact"],
            "realigned_evidence_count": verification_counts["deterministic_alignment"],
            "format_repair_count": format_repair_count,
            "generated_at": datetime.now(UTC).isoformat(),
            "evidence_policy": "segment_id_and_backend_literal_quote_verified",
            "context_sources": [source.get("url") for source in context.get("sources") or []],
        }
    )
    return payload


def _add_analysis_audit_event(analysis: Analysis) -> None:
    from app.extensions import db

    db.session.add(
        AuditEvent(
            organization_id=analysis.organization_id,
            actor_id=analysis.requested_by_id,
            action=(
                "ANALYSIS_SUCCEEDED"
                if analysis.status == AnalysisStatus.SUCCEEDED
                else "ANALYSIS_ATTEMPT_FAILED"
            ),
            target_type="analysis",
            target_id=analysis.id,
            details={
                "meeting_id": analysis.meeting_id,
                "model": analysis.model,
                "prompt_version": analysis.prompt_version,
                "score_version": analysis.score_version,
                "mode_strategy": analysis.mode_strategy,
                "requested_modes": analysis.requested_modes,
                "selected_modes": analysis.selected_modes,
                "analysis_profile_hash": analysis.analysis_profile_hash,
                "status": analysis.status.value,
                "error_code": analysis.error_code,
                "latency_ms": analysis.latency_ms,
            },
        )
    )


def _validated_autopsy_payload(result, segments: list[Segment]) -> dict:
    segments_by_id = {segment.id: segment for segment in segments}
    payload = result.model_dump()
    discarded = 0
    evidence_count = 0

    def verified(items: list, payload_items: list[dict]) -> list[dict]:
        nonlocal discarded, evidence_count
        valid: list[dict] = []
        for evidence, evidence_payload in zip(items, payload_items, strict=False):
            segment = segments_by_id.get(evidence.segment_id)
            if segment is None:
                literal_matches = [
                    candidate
                    for candidate in segments
                    if evidence.quote and evidence.quote in candidate.text
                ]
                if len(literal_matches) != 1:
                    discarded += 1
                    continue
                segment = literal_matches[0]
                evidence_payload["segment_id"] = segment.id
            aligned = _literal_quote_alignment(evidence.quote, segment.text)
            if not aligned:
                discarded += 1
                continue
            quote, method = aligned
            evidence_payload.update(
                {
                    "quote": quote,
                    "verification_method": method,
                    "position": segment.position,
                    "speaker": segment.speaker,
                    "started_at_ms": segment.started_at_ms,
                    "ended_at_ms": segment.ended_at_ms,
                }
            )
            evidence_count += 1
            valid.append(evidence_payload)
        return valid

    neutral_status = {
        "objective": "unclear",
        "follow_up": "unclear",
        "conflict": "unclear",
        "customization": "unclear",
    }
    for field_name, neutral in neutral_status.items():
        decision = getattr(result, field_name)
        decision_payload = payload[field_name]
        decision_payload["evidence"] = verified(
            decision.evidence, decision_payload.get("evidence") or []
        )
        affirmative = decision.status not in {
            "none",
            "none_detected",
            "absent",
            "unclear",
        }
        if affirmative and not decision_payload["evidence"]:
            decision_payload.update(
                {
                    "status": neutral,
                    "confidence": 0.0,
                    "rationale": "A revisão não recebeu evidência literal válida.",
                }
            )
            payload.setdefault("uncertainties", []).append(
                f"O campo {field_name} foi rebaixado por falta de evidência válida."
            )
    payload["uncertainties"] = list(dict.fromkeys(payload.get("uncertainties") or []))[:4]
    payload["traceability"] = {
        "evidence_count": evidence_count,
        "discarded_reference_count": discarded,
        "evidence_policy": "segment_id_and_backend_literal_quote_verified",
    }
    return payload


def _run_autopsy_assistant(
    analysis: Analysis,
    transcript: str,
    baseline_result: dict,
) -> AnalysisAutopsyRun | None:
    from app.extensions import db

    mode = normalize_mode(current_app.config.get("AUTOPSY_ASSISTANT_MODE"))
    try:
        guide = build_review_guide(
            baseline_result,
            transcript,
            analysis.local_metrics,
            analysis.context_snapshot,
        )
    except ValueError:
        return None
    if mode == "off":
        return None

    bundle = load_bundle()
    run = AnalysisAutopsyRun(
        organization_id=analysis.organization_id,
        analysis_id=analysis.id,
        mode=mode,
        status="running",
        bundle_version=bundle["version"],
        bundle_sha256=bundle_sha256(),
        source_references=guide["source_references"],
        path_id=guide["path_id"],
        questions=guide["questions"],
        quality_flags=guide["baseline_observations"]["capture_quality_flags"],
        baseline_result=baseline_result,
    )
    db.session.add(run)
    db.session.flush()
    try:
        result, input_tokens, output_tokens, latency, repairs = review_transcript_with_autopsy(
            transcript,
            analysis.model,
            analysis.local_metrics,
            analysis.context_snapshot,
            baseline_result,
            guide,
        )
        guided_review = _validated_autopsy_payload(result, analysis.meeting.segments)
        candidate = assisted_candidate(baseline_result, guided_review)
        run.guided_review = guided_review
        run.assisted_result = candidate
        run.disagreements = compare_decisions(baseline_result, guided_review)
        run.input_tokens = input_tokens
        run.output_tokens = output_tokens
        run.latency_ms = latency
        run.format_repair_count = repairs
        run.status = "succeeded"
        if mode == "active":
            analysis.result = candidate
        analysis.input_tokens = int(analysis.input_tokens or 0) + input_tokens
        analysis.output_tokens = int(analysis.output_tokens or 0) + output_tokens
        analysis.latency_ms = int(analysis.latency_ms or 0) + latency
    except Exception as error:
        run.status = "failed"
        run.error_code = re.sub(r"[^a-zA-Z0-9_,:-]", "_", f"{type(error).__name__}:{error}")[:100]
        logger.warning(
            "autopsy_assistant_failed analysis_id=%s error_code=%s",
            analysis.id,
            run.error_code,
        )
    finally:
        run.finished_at = utcnow()
        db.session.add(
            AuditEvent(
                organization_id=analysis.organization_id,
                actor_id=analysis.requested_by_id,
                action="AUTOPSY_ASSISTANT_FINISHED",
                target_type="analysis_autopsy_run",
                target_id=run.id,
                details={
                    "analysis_id": analysis.id,
                    "mode": mode,
                    "status": run.status,
                    "bundle_version": run.bundle_version,
                    "path_id": run.path_id,
                    "disagreement_count": len(run.disagreements or []),
                    "error_code": run.error_code,
                },
            )
        )
    return run


def _sync_submission_from_analysis(analysis: Analysis) -> None:
    submission = MeetingSubmission.query.filter_by(analysis_id=analysis.id).first()
    if submission is None:
        return
    previous_status = submission.status
    submission.status = (
        SubmissionStatus.COMPLETED.value
        if analysis.status == AnalysisStatus.SUCCEEDED
        else SubmissionStatus.FAILED.value
    )
    submission.error_code = (
        None if analysis.status == AnalysisStatus.SUCCEEDED else analysis.error_code
    )
    submission.version += 1
    from app.extensions import db

    db.session.add(
        AuditEvent(
            organization_id=submission.organization_id,
            actor_id=analysis.requested_by_id,
            action="SUBMISSION_ANALYSIS_FINISHED",
            target_type="meeting_submission",
            target_id=submission.id,
            details={
                "previous_status": previous_status,
                "status": submission.status,
                "analysis_id": analysis.id,
                "error_code": submission.error_code,
            },
        )
    )


def process_analysis(analysis_id: str) -> None:
    from app import create_app
    from app.extensions import db

    app = create_app()
    with app.app_context():
        analysis = db.session.get(Analysis, analysis_id)
        if not analysis or analysis.status == AnalysisStatus.SUCCEEDED:
            return
        analysis.status = AnalysisStatus.RUNNING
        analysis.error_code = None
        analysis.started_at = utcnow()
        analysis.finished_at = None
        previous_attempts = (
            db.session.scalar(
                db.select(db.func.count(AIExecution.id)).where(
                    AIExecution.analysis_id == analysis.id
                )
            )
            or 0
        )
        execution = AIExecution(
            organization_id=analysis.organization_id,
            analysis_id=analysis.id,
            attempt=previous_attempts + 1,
            provider=analysis.provider,
            model=analysis.model,
            prompt_version=analysis.prompt_version,
            schema_sha256=(
                MODE_BUNDLE_SHA256
                if analysis.prompt_version == MODE_BUNDLE_VERSION
                else GEMINI_RESPONSE_SCHEMA_SHA256
            ),
            transcript_sha256=analysis.transcript_hash,
            context_sha256=analysis.context_sha256,
            parameters={"temperature": None, "structured_output": True},
        )
        db.session.add(execution)
        db.session.commit()
        text = transcript_text(analysis.meeting.segments)
        pii = detect_pii(text)
        if pii:
            analysis.status = AnalysisStatus.FAILED
            analysis.error_code = "pii_detected"
            analysis.finished_at = utcnow()
            execution.status = "failed"
            execution.error_code = analysis.error_code
            execution.finished_at = analysis.finished_at
            _add_analysis_audit_event(analysis)
            _sync_submission_from_analysis(analysis)
            db.session.commit()
            return
        if not ai_is_configured():
            analysis.status = AnalysisStatus.FAILED
            analysis.error_code = "ai_not_configured"
            analysis.finished_at = utcnow()
            execution.status = "failed"
            execution.error_code = analysis.error_code
            execution.finished_at = analysis.finished_at
            _add_analysis_audit_event(analysis)
            _sync_submission_from_analysis(analysis)
            db.session.commit()
            return
        try:
            if analysis.prompt_version == MODE_BUNDLE_VERSION:
                (
                    mode_payload,
                    input_tokens,
                    output_tokens,
                    latency,
                    format_repairs,
                ) = analyze_transcript_modes(
                    text,
                    analysis.model,
                    analysis.local_metrics,
                    analysis.mode_strategy,
                    list(analysis.requested_modes or []),
                )
                analysis.selected_modes = mode_payload["selected_modes"]
                analysis.result = _validated_modes_payload(
                    mode_payload,
                    analysis.meeting.segments,
                    analysis,
                    format_repair_count=format_repairs,
                )
            else:
                (
                    result,
                    input_tokens,
                    output_tokens,
                    latency,
                    format_repairs,
                ) = analyze_transcript(
                    text,
                    analysis.model,
                    analysis.local_metrics,
                    analysis.context_snapshot,
                )
                analysis.result = _validated_result_payload(
                    result,
                    analysis.meeting.segments,
                    analysis,
                    format_repair_count=format_repairs,
                )
            analysis.input_tokens = input_tokens
            analysis.output_tokens = output_tokens
            analysis.latency_ms = latency
            autopsy_run = _run_autopsy_assistant(analysis, text, analysis.result)
            if autopsy_run is not None:
                execution.parameters = {
                    **(execution.parameters or {}),
                    "autopsy_mode": autopsy_run.mode,
                    "autopsy_version": autopsy_run.bundle_version,
                    "autopsy_status": autopsy_run.status,
                }
            analysis.status = AnalysisStatus.SUCCEEDED
            try:
                from app.services.actions import create_or_refresh_plan

                with db.session.begin_nested():
                    create_or_refresh_plan(analysis)
            except Exception as error:
                logger.exception(
                    "customer_success_plan_failed analysis_id=%s exception_type=%s",
                    analysis.id,
                    type(error).__name__,
                )
        except InvalidModelOutput as error:
            analysis.status = AnalysisStatus.FAILED
            safe_reason = re.sub(r"[^a-zA-Z0-9_,:-]", "_", str(error))
            analysis.error_code = f"invalid_model_output_{safe_reason}"[:100]
            logger.error(
                "analysis_failed analysis_id=%s error_code=%s validation_reason=%s",
                analysis.id,
                analysis.error_code,
                str(error),
            )
        except ClientError as error:
            analysis.status = AnalysisStatus.FAILED
            if error.code in {401, 403}:
                analysis.error_code = "ai_auth_rejected"
            elif error.code == 404:
                analysis.error_code = "ai_model_unavailable"
            elif error.code == 429:
                analysis.error_code = "ai_temporarily_unavailable"
            else:
                analysis.error_code = "ai_request_rejected"
            logger.error(
                "analysis_failed analysis_id=%s error_code=%s provider_status=%s "
                "provider_detail=%s",
                analysis.id,
                analysis.error_code,
                error.code,
                safe_provider_error(error),
            )
            if error.code == 429:
                db.session.commit()
                raise
        except ServerError as error:
            analysis.status = AnalysisStatus.FAILED
            analysis.error_code = (
                "ai_temporarily_unavailable" if error.code == 503 else "ai_provider_error"
            )
            logger.error(
                "analysis_failed analysis_id=%s error_code=%s provider_status=%s "
                "provider_detail=%s",
                analysis.id,
                analysis.error_code,
                error.code,
                safe_provider_error(error),
            )
            if error.code == 503:
                db.session.commit()
                raise
        except (TimeoutError, httpx.TimeoutException):
            analysis.status = AnalysisStatus.FAILED
            analysis.error_code = "ai_temporarily_unavailable"
            db.session.commit()
            raise
        except Exception as error:
            analysis.status = AnalysisStatus.FAILED
            analysis.error_code = "ai_processing_error"
            logger.error(
                "analysis_failed analysis_id=%s error_code=%s exception_type=%s",
                analysis.id,
                analysis.error_code,
                type(error).__name__,
            )
        finally:
            analysis.finished_at = utcnow()
            execution.status = (
                "succeeded" if analysis.status == AnalysisStatus.SUCCEEDED else "failed"
            )
            execution.input_tokens = analysis.input_tokens
            execution.output_tokens = analysis.output_tokens
            execution.latency_ms = analysis.latency_ms
            execution.estimated_cost_microunits = (
                int(analysis.input_tokens or 0)
                * int(current_app.config["AI_INPUT_COST_PER_MILLION_MICROUNITS"])
                + int(analysis.output_tokens or 0)
                * int(current_app.config["AI_OUTPUT_COST_PER_MILLION_MICROUNITS"])
            ) // 1_000_000
            execution.cost_currency = current_app.config["AI_COST_CURRENCY"]
            execution.error_code = analysis.error_code
            execution.finished_at = analysis.finished_at
            if analysis.result:
                execution.result_sha256 = sha256(
                    json.dumps(analysis.result, sort_keys=True, ensure_ascii=False).encode()
                ).hexdigest()
            _add_analysis_audit_event(analysis)
            _sync_submission_from_analysis(analysis)
            db.session.commit()


from app.jobs_operations import (  # noqa: E402
    cleanup_submission_files,
    execute_privacy_request,
    expire_submission_file,
    process_submission_ingestion,
    process_teams_transcript,
    renew_teams_subscription,
    run_submission_ingestion,
)

__all__ = [
    "cleanup_submission_files",
    "execute_privacy_request",
    "expire_submission_file",
    "process_submission_ingestion",
    "process_teams_transcript",
    "renew_teams_subscription",
    "run_submission_ingestion",
]
