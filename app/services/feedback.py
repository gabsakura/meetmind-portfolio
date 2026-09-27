from __future__ import annotations

from typing import Any

from app.extensions import db
from app.models import Analysis, AnalysisAutopsyRun, AnalysisFeedback
from app.services.autopsy import core_decisions


def _analysis_labels_from_result(
    result: dict[str, Any] | None,
    *,
    guided_review: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = result if isinstance(result, dict) else {}
    classification = payload.get("meeting_classification") or {}
    health = payload.get("relationship_health") or {}
    evidence_ids = {
        str(item)
        for item in [
            *(classification.get("evidence_segment_ids") or []),
            *(health.get("evidence_segment_ids") or []),
        ]
        if str(item).strip()
    }
    brief = payload.get("manufacturing_brief") or {}
    for field in ("objective", "follow_up", "conflict"):
        for evidence in (brief.get(field) or {}).get("evidence") or []:
            if isinstance(evidence, dict) and evidence.get("segment_id"):
                evidence_ids.add(str(evidence["segment_id"]))
    for finding in payload.get("findings") or []:
        if not isinstance(finding, dict):
            continue
        for evidence in finding.get("evidence") or []:
            if isinstance(evidence, dict) and evidence.get("segment_id"):
                evidence_ids.add(str(evidence["segment_id"]))
    return {
        "meeting_type": classification.get("meeting_type") or classification.get("type"),
        "relationship_health": health.get("status"),
        "evidence_segment_ids": sorted(evidence_ids),
        "action_count": min(3, len(payload.get("action_items") or [])),
        **core_decisions(payload, guided_review=guided_review),
    }


def autopsy_run_json(run: AnalysisAutopsyRun | None) -> dict[str, Any] | None:
    if run is None:
        return None
    return {
        "id": run.id,
        "analysis_id": run.analysis_id,
        "mode": run.mode,
        "status": run.status,
        "bundle_version": run.bundle_version,
        "bundle_sha256": run.bundle_sha256,
        "path_id": run.path_id,
        "questions": run.questions or [],
        "quality_flags": run.quality_flags or [],
        "disagreements": run.disagreements or [],
        "baseline": _analysis_labels_from_result(run.baseline_result),
        "assisted": _analysis_labels_from_result(
            run.assisted_result or run.baseline_result,
            guided_review=run.guided_review,
        ),
        "traceability": (run.guided_review or {}).get("traceability") or {},
        "input_tokens": run.input_tokens,
        "output_tokens": run.output_tokens,
        "latency_ms": run.latency_ms,
        "error_code": run.error_code,
        "started_at": run.started_at.isoformat(),
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
    }


def feedback_json(item: AnalysisFeedback) -> dict[str, Any]:
    analysis = db.session.get(Analysis, item.analysis_id)
    autopsy_run = (
        AnalysisAutopsyRun.query.filter_by(analysis_id=item.analysis_id)
        .order_by(AnalysisAutopsyRun.started_at.desc())
        .first()
    )
    context = (
        analysis.context_snapshot
        if analysis and isinstance(analysis.context_snapshot, dict)
        else {}
    )
    account = context.get("account") or {}
    playbook = context.get("playbook") or {}
    return {
        "id": item.id,
        "analysis_id": item.analysis_id,
        "reviewer_id": item.reviewer_id,
        "usefulness_rating": item.usefulness_rating,
        "factual_accuracy": item.factual_accuracy,
        "action_useful": item.action_useful,
        "correction": item.correction,
        "comment": item.comment,
        "model": analysis.model if analysis else None,
        "prompt_version": analysis.prompt_version if analysis else None,
        "playbook": playbook.get("id"),
        "context_sha256": analysis.context_sha256 if analysis else None,
        "sector": playbook.get("label") or account.get("sector"),
        "actual": _analysis_labels_from_result(analysis.result) if analysis else {},
        "autopsy": autopsy_run_json(autopsy_run),
        "created_at": item.created_at.isoformat(),
        "updated_at": item.updated_at.isoformat(),
    }