from __future__ import annotations

import json
import re
import unicodedata
from functools import lru_cache
from hashlib import sha256
from pathlib import Path
from typing import Any

BUNDLE_PATH = Path(__file__).parents[1] / "ai" / "autopsy" / "manufacturing-v1.json"
ALLOWED_MODES = {"off", "shadow", "active"}


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _normalize(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    return "".join(char for char in text if not unicodedata.combining(char)).casefold()


@lru_cache(maxsize=1)
def load_bundle() -> dict[str, Any]:
    payload = json.loads(BUNDLE_PATH.read_text(encoding="utf-8"))
    if payload.get("version") != "meetmind-autopsy-manufacturing-v1":
        raise ValueError("invalid_autopsy_bundle_version")
    if payload.get("scope", {}).get("purpose") != "guided_review_not_prediction":
        raise ValueError("invalid_autopsy_bundle_purpose")
    if not payload.get("questions") or not payload.get("prohibited_inferences"):
        raise ValueError("incomplete_autopsy_bundle")
    return payload


def bundle_sha256() -> str:
    return sha256(_canonical(load_bundle()).encode()).hexdigest()


def normalize_mode(value: Any) -> str:
    mode = str(value or "off").strip().casefold()
    return mode if mode in ALLOWED_MODES else "off"


def applies_to(context_snapshot: dict[str, Any] | None) -> bool:
    playbook = (context_snapshot or {}).get("playbook") or {}
    return playbook.get("id") in set(load_bundle()["scope"]["playbook_ids"])


def profile_hash(
    base_profile_hash: str,
    context_snapshot: dict[str, Any] | None,
    mode: str,
) -> str:
    normalized_mode = normalize_mode(mode)
    if normalized_mode == "off" or not applies_to(context_snapshot):
        return base_profile_hash
    return sha256(
        _canonical(
            {
                "base": base_profile_hash,
                "autopsy_mode": normalized_mode,
                "autopsy_version": load_bundle()["version"],
                "autopsy_sha256": bundle_sha256(),
            }
        ).encode()
    ).hexdigest()


def _segment_id(line: str) -> str | None:
    match = re.match(r"\[SEGMENT_ID=([^\]]+)\]", line)
    return match.group(1) if match else None


def _customization_candidates(transcript: str, terms: list[str]) -> list[str]:
    normalized_terms = [_normalize(term) for term in terms]
    matches: list[str] = []
    for line in transcript.splitlines():
        normalized_line = _normalize(line)
        if not any(term in normalized_line for term in normalized_terms):
            continue
        identifier = _segment_id(line)
        if identifier and identifier not in matches:
            matches.append(identifier)
    return matches[:8]


def build_review_guide(
    baseline_result: dict[str, Any],
    transcript: str,
    local_metrics: dict[str, Any] | None,
    context_snapshot: dict[str, Any] | None,
) -> dict[str, Any]:
    if not applies_to(context_snapshot):
        raise ValueError("autopsy_not_applicable")
    bundle = load_bundle()
    brief = baseline_result.get("manufacturing_brief") or {}
    objective = (brief.get("objective") or {}).get("status") or "unclear"
    follow_up = (brief.get("follow_up") or {}).get("status") or "unclear"
    conflict = (brief.get("conflict") or {}).get("status") or "unclear"
    candidates = _customization_candidates(
        transcript, bundle.get("customization_terms") or []
    )
    if objective in {"not_achieved"}:
        path_id = "objective_unresolved"
    elif objective == "unclear":
        path_id = "objective_unclear"
    elif candidates:
        path_id = "objective_resolved_with_customization"
    else:
        path_id = "objective_resolved_without_customization"

    metrics = local_metrics or {}
    quality_flags: list[str] = []
    words_per_minute = metrics.get("words_per_minute")
    client_share = metrics.get("client_word_share")
    duration_minutes = metrics.get("duration_minutes")
    if isinstance(words_per_minute, (int, float)) and words_per_minute < 40:
        quality_flags.append("low_transcript_density")
    if isinstance(client_share, (int, float)) and client_share in {0, 100}:
        quality_flags.append("extreme_client_speaker_share")
    if isinstance(duration_minutes, (int, float)) and duration_minutes < 14.75:
        quality_flags.append("short_meeting_capture")

    question_ids = list(bundle["operational_paths"][path_id])
    if quality_flags and "capture_quality" not in question_ids:
        question_ids.append("capture_quality")
    if client_share in {0, 100} and "participants" not in question_ids:
        question_ids.append("participants")
    return {
        "guide_version": bundle["version"],
        "guide_sha256": bundle_sha256(),
        "purpose": "guided_review_not_prediction",
        "path_id": path_id,
        "baseline_observations": {
            "objective_status": objective,
            "follow_up_status": follow_up,
            "conflict_status": conflict,
            "customization_candidate_segment_ids": candidates,
            "capture_quality_flags": quality_flags,
        },
        "questions": [
            {"id": question_id, "text": bundle["questions"][question_id]}
            for question_id in question_ids
        ],
        "definitions": bundle["definitions"],
        "exploratory_cautions": bundle["exploratory_cautions"],
        "prohibited_inferences": bundle["prohibited_inferences"],
        "source_references": [
            {
                "kind": source["kind"],
                "id": source["id"],
                "reference_sha256": source["reference_sha256"],
            }
            for source in bundle["sources"]
        ],
    }


def core_decisions(
    result: dict[str, Any] | None,
    *,
    guided_review: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = result or {}
    brief = payload.get("manufacturing_brief") or {}
    review = guided_review or {}
    return {
        "objective": (review.get("objective") or brief.get("objective") or {}).get(
            "status"
        ),
        "follow_up": (review.get("follow_up") or brief.get("follow_up") or {}).get(
            "status"
        ),
        "conflict": (review.get("conflict") or brief.get("conflict") or {}).get(
            "status"
        ),
        "customization": (review.get("customization") or {}).get("status"),
    }


def compare_decisions(
    baseline_result: dict[str, Any], guided_review: dict[str, Any]
) -> list[dict[str, Any]]:
    baseline = core_decisions(baseline_result)
    assisted = core_decisions(baseline_result, guided_review=guided_review)
    return [
        {"field": field, "baseline": baseline[field], "assisted": assisted[field]}
        for field in assisted
        if baseline.get(field) != assisted.get(field)
    ]


def assisted_candidate(
    baseline_result: dict[str, Any], guided_review: dict[str, Any]
) -> dict[str, Any]:
    candidate = json.loads(json.dumps(baseline_result, ensure_ascii=False))
    brief = candidate.setdefault("manufacturing_brief", {})
    for field in ("objective", "follow_up", "conflict"):
        baseline = brief.get(field) or {}
        reviewed = guided_review.get(field) or {}
        if reviewed.get("status") == baseline.get("status"):
            continue
        baseline_has_evidence = bool(baseline.get("evidence"))
        review_has_evidence = bool(reviewed.get("evidence"))
        if review_has_evidence or not baseline_has_evidence:
            brief[field] = reviewed
    traceability = candidate.setdefault("traceability", {})
    traceability["autopsy_guide_version"] = load_bundle()["version"]
    traceability["autopsy_guide_sha256"] = bundle_sha256()
    traceability["autopsy_policy"] = "guided_review_not_prediction"
    return candidate
