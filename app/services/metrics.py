from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable

from app.models import Meeting, Segment

LEXICON = {
    "satisfaction": {
        "words": ("perfeito", "ótimo", "excelente", "gostei", "resolvido"),
        "weight": 3.0,
    },
    "dissatisfaction": {
        "words": ("cancelar", "reclamação", "insatisfeito", "horrível", "decepcionado"),
        "weight": -3.5,
    },
    "operational_problems": {
        "words": ("problema", "erro", "falha", "dificuldade", "travou", "queda"),
        "weight": -3.0,
    },
    "commercial_risk": {
        "words": ("caro", "concorrente", "cancelamento"),
        "weight": -2.0,
    },
}


def _term_matches(text: str, term: str) -> list[re.Match]:
    valid_matches = []
    for match in re.finditer(rf"\b{re.escape(term)}\b", text, flags=re.IGNORECASE):
        prefix = text[max(0, match.start() - 45) : match.start()]
        negated = re.search(
            r"\b(?:não|nunca|jamais|sem|nenhum|nenhuma)\b"
            r"(?:[\s,.:;-]+\w+){0,3}[\s,.:;-]*$",
            prefix,
            flags=re.IGNORECASE,
        )
        if not negated:
            valid_matches.append(match)
    return valid_matches


def _count_terms(text: str, terms: Iterable[str]) -> dict[str, int]:
    return {
        term: len(_term_matches(text, term))
        for term in terms
        if _term_matches(text, term)
    }


def _excerpt_around(text: str, term: str, radius: int = 90) -> str:
    matches = _term_matches(text, term)
    if not matches:
        return ""
    match = matches[0]
    start = max(0, match.start() - radius)
    end = min(len(text), match.end() + radius)
    prefix = "…" if start else ""
    suffix = "…" if end < len(text) else ""
    return f"{prefix}{' '.join(text[start:end].split())}{suffix}"


def calculate_metrics(meeting: Meeting) -> dict:
    client_segments = [segment for segment in meeting.segments if not segment.is_totvs]
    totvs_segments = [segment for segment in meeting.segments if segment.is_totvs]
    client_text = " ".join(segment.text for segment in client_segments)
    totvs_text = " ".join(segment.text for segment in totvs_segments)
    all_text = f"{client_text} {totvs_text}".strip()
    client_words = len(client_text.split())
    totvs_words = len(totvs_text.split())
    total_words = max(1, client_words + totvs_words)

    triggers: dict[str, dict[str, int]] = {}
    trigger_events: list[dict] = []
    weighted_score = 0.0
    for category, spec in LEXICON.items():
        category_matches: dict[str, int] = {}
        category_evidence: dict[str, list[dict]] = {}
        for segment in client_segments:
            matches = _count_terms(segment.text, spec["words"])
            for term, count in matches.items():
                category_matches[term] = category_matches.get(term, 0) + count
                evidence = category_evidence.setdefault(term, [])
                if len(evidence) < 3:
                    evidence.append(
                        {
                            "segment_id": segment.id,
                            "position": segment.position,
                            "speaker": segment.speaker,
                            "excerpt": _excerpt_around(segment.text, term),
                        }
                    )
        triggers[category] = category_matches
        for term, count in category_matches.items():
            weight = float(spec["weight"])
            trigger_events.append(
                {
                    "category": category,
                    "term": term,
                    "count": count,
                    "weight": weight,
                    "score_impact": round(count * weight * 4, 2),
                    "evidence": category_evidence[term],
                }
            )
        weighted_score += sum(category_matches.values()) * float(spec["weight"])

    normalized_score = round(max(-100.0, min(100.0, weighted_score * 4)), 2)
    if normalized_score >= 12:
        health = "healthy"
    elif normalized_score <= -12:
        health = "at_risk"
    else:
        health = "attention"

    minutes = (meeting.duration_seconds or 0) / 60
    top_words = Counter(
        word
        for word in re.findall(r"[A-Za-zÀ-ÿ]{4,}", client_text.lower())
        if word not in {"para", "como", "mais", "uma", "isso", "essa", "aqui", "então"}
    ).most_common(10)

    return {
        "duration_minutes": round(minutes, 2) if minutes > 0 else None,
        "score_voc": normalized_score,
        "health": health,
        "client_word_share": round(client_words / total_words * 100, 2),
        "totvs_word_share": round(totvs_words / total_words * 100, 2),
        "client_words": client_words,
        "totvs_words": totvs_words,
        "words_per_minute": round(total_words / minutes, 2) if minutes > 0 else None,
        "nps": meeting.metadata_json.get("nota_nps"),
        "triggers": triggers,
        "trigger_events": sorted(
            trigger_events,
            key=lambda event: (abs(event["score_impact"]), event["count"]),
            reverse=True,
        ),
        "top_client_terms": [{"term": word, "count": count} for word, count in top_words],
        "segment_count": len(meeting.segments),
        "deterministic": True,
        "transcript_characters": len(all_text),
    }


def transcript_text(segments: list[Segment]) -> str:
    return "\n".join(
        f"[SEGMENT_ID={segment.id}] [{segment.speaker or 'Não identificado'}] {segment.text}"
        for segment in segments
    )
