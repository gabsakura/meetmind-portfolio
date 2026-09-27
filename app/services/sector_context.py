from __future__ import annotations

import json
import re
import unicodedata
from datetime import date
from functools import lru_cache
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models import Meeting

PLAYBOOK_DIR = Path(__file__).parents[1] / "ai" / "playbooks"
CONTEXT_VERSION = "sector-playbooks-v2"
MEETING_TYPES = {
    "support_incident",
    "implementation",
    "relationship_success",
    "commercial_expansion",
    "adoption_training",
    "renewal_contract",
    "discovery",
    "mixed",
    "unknown",
}


class PlaybookModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Source(PlaybookModel):
    id: str = Field(pattern=r"^[a-z0-9-]+$")
    title: str
    url: str = Field(pattern=r"^https://")
    publisher: str
    accessed_at: date


class KnowledgeItem(PlaybookModel):
    id: str = Field(pattern=r"^[a-z0-9-]+$")
    label: str
    aliases: list[str] = Field(default_factory=list)
    usage: str
    source_ids: list[str] = Field(min_length=1)


class SignalPattern(PlaybookModel):
    signal: str
    meaning: str
    usage: str
    source_ids: list[str] = Field(min_length=1)


class Subsector(PlaybookModel):
    id: str = Field(pattern=r"^[a-z0-9-]+$")
    label: str
    aliases: list[str] = Field(default_factory=list)
    cnae_prefixes: list[str] = Field(default_factory=list)


class MeetingRule(PlaybookModel):
    prioritize: list[str]
    avoid: list[str]


class SectorPlaybook(PlaybookModel):
    id: str = Field(pattern=r"^[a-z0-9_-]+$")
    label: str
    version: str
    reviewed_at: date
    aliases: list[str] = Field(min_length=1)
    unit_aliases: list[str] = Field(default_factory=list)
    cnae_prefixes: list[str] = Field(default_factory=list)
    detection_terms: list[str] = Field(default_factory=list)
    sources: list[Source] = Field(min_length=1)
    subsectors: list[Subsector]
    critical_processes: list[KnowledgeItem] = Field(min_length=1)
    technologies: list[KnowledgeItem] = Field(min_length=1)
    common_indicators: list[KnowledgeItem] = Field(min_length=1)
    possible_problems: list[SignalPattern] = Field(min_length=1)
    positive_signals: list[SignalPattern] = Field(min_length=1)
    attention_signals: list[SignalPattern] = Field(min_length=1)
    diagnostic_questions: list[KnowledgeItem] = Field(min_length=1)
    conditional_recommendations: list[KnowledgeItem] = Field(min_length=1)
    prohibited_inferences: list[str] = Field(min_length=1)
    meeting_type_rules: dict[str, MeetingRule]

    @model_validator(mode="after")
    def validate_references_and_uniqueness(self):
        source_ids = {source.id for source in self.sources}
        if len(source_ids) != len(self.sources):
            raise ValueError("source ids must be unique")
        item_groups = (
            self.critical_processes,
            self.technologies,
            self.common_indicators,
            self.diagnostic_questions,
            self.conditional_recommendations,
        )
        item_ids = [item.id for group in item_groups for item in group]
        if len(item_ids) != len(set(item_ids)):
            raise ValueError("knowledge item ids must be unique")
        referenced = [
            source_id
            for group in item_groups
            for item in group
            for source_id in item.source_ids
        ]
        referenced.extend(
            source_id
            for group in (self.possible_problems, self.positive_signals, self.attention_signals)
            for item in group
            for source_id in item.source_ids
        )
        unknown = set(referenced) - source_ids
        if unknown:
            raise ValueError(f"unknown source ids: {sorted(unknown)}")
        return self


def normalize_term(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(char for char in text if not unicodedata.combining(char))
    return " ".join(re.sub(r"[^a-zA-Z0-9]+", " ", text).casefold().split())


def normalize_cnae(value: Any) -> str:
    return re.sub(r"\D", "", str(value or ""))


@lru_cache(maxsize=1)
def load_playbooks() -> dict[str, SectorPlaybook]:
    loaded: dict[str, SectorPlaybook] = {}
    for path in sorted(PLAYBOOK_DIR.glob("*.json")):
        if path.name == "schema.json":
            continue
        playbook = SectorPlaybook.model_validate_json(path.read_text(encoding="utf-8"))
        if playbook.id in loaded:
            raise ValueError(f"duplicate playbook id: {playbook.id}")
        loaded[playbook.id] = playbook
    if "generico" not in loaded:
        raise ValueError("generic playbook is required")
    aliases: dict[str, str] = {}
    for playbook in loaded.values():
        for alias in [playbook.label, *playbook.aliases]:
            normalized = normalize_term(alias)
            previous = aliases.get(normalized)
            if previous and previous != playbook.id:
                raise ValueError(f"sector alias conflict: {alias}")
            aliases[normalized] = playbook.id
    return loaded


def validate_playbooks() -> dict[str, Any]:
    load_playbooks.cache_clear()
    playbooks = load_playbooks()
    return {
        "version": CONTEXT_VERSION,
        "count": len(playbooks),
        "ids": sorted(playbooks),
        "sources": sum(len(playbook.sources) for playbook in playbooks.values()),
    }


def _metadata_value(metadata: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = metadata.get(key)
        if value not in (None, ""):
            return value
    return None


def _sector_by_alias(value: Any, playbooks: dict[str, SectorPlaybook]) -> str | None:
    normalized = normalize_term(value)
    if not normalized:
        return None
    for playbook in playbooks.values():
        if playbook.id == "generico":
            continue
        aliases = {normalize_term(playbook.label), *(normalize_term(x) for x in playbook.aliases)}
        if normalized in aliases:
            return playbook.id
    return None


def _sector_by_cnae(cnae: str, playbooks: dict[str, SectorPlaybook]) -> str | None:
    matches = [
        (len(prefix), playbook.id)
        for playbook in playbooks.values()
        if playbook.id != "generico"
        for prefix in playbook.cnae_prefixes
        if cnae.startswith(prefix)
    ]
    if not matches:
        return None
    longest = max(length for length, _sector_id in matches)
    best = {sector_id for length, sector_id in matches if length == longest}
    return next(iter(best)) if len(best) == 1 else None


def _sector_by_unit(value: Any, playbooks: dict[str, SectorPlaybook]) -> str | None:
    normalized = normalize_term(value)
    matches = [
        playbook.id
        for playbook in playbooks.values()
        if playbook.id != "generico"
        and any(normalize_term(alias) in normalized for alias in playbook.unit_aliases)
    ]
    return matches[0] if len(set(matches)) == 1 else None


def _sector_by_transcript(text: str, playbooks: dict[str, SectorPlaybook]) -> str | None:
    normalized = normalize_term(text)
    scores = []
    for playbook in playbooks.values():
        if playbook.id == "generico":
            continue
        score = sum(
            1 for term in playbook.detection_terms if normalize_term(term) in normalized
        )
        scores.append((score, playbook.id))
    scores.sort(reverse=True)
    if not scores or scores[0][0] < 2:
        return None
    if len(scores) > 1 and scores[0][0] == scores[1][0]:
        return None
    return scores[0][1]


def _subsector(playbook: SectorPlaybook, cnae: str, text: str) -> str | None:
    cnae_matches = [
        item.id
        for item in playbook.subsectors
        if cnae and any(cnae.startswith(prefix) for prefix in item.cnae_prefixes)
    ]
    if len(set(cnae_matches)) == 1:
        return cnae_matches[0]
    normalized = normalize_term(text)
    text_matches = [
        item.id
        for item in playbook.subsectors
        if any(normalize_term(alias) in normalized for alias in item.aliases)
    ]
    return text_matches[0] if len(set(text_matches)) == 1 else None


def _selected_knowledge(playbook: SectorPlaybook, text: str) -> dict[str, list[dict[str, Any]]]:
    normalized = normalize_term(text)

    def choose(items: list[KnowledgeItem], fallback: int) -> list[dict[str, Any]]:
        matched = [
            item
            for item in items
            if any(normalize_term(alias) in normalized for alias in [item.label, *item.aliases])
        ]
        selected = matched[:8] if matched else items[:fallback]
        return [item.model_dump(mode="json") for item in selected]

    return {
        "critical_processes": choose(playbook.critical_processes, 4),
        "technologies": choose(playbook.technologies, 3),
        "common_indicators": choose(playbook.common_indicators, 3),
        "diagnostic_questions": choose(playbook.diagnostic_questions, 3),
        "conditional_recommendations": choose(playbook.conditional_recommendations, 3),
    }


def build_context_snapshot(meeting: Meeting) -> dict[str, Any]:
    playbooks = load_playbooks()
    metadata = meeting.metadata_json if isinstance(meeting.metadata_json, dict) else {}
    segment = _metadata_value(metadata, "nome_segmento", "segmento", "sector")
    cnae_raw = _metadata_value(metadata, "cnae", "codigo_cnae")
    unit = _metadata_value(metadata, "nome_unidade", "unidade")
    region = _metadata_value(metadata, "regiao_brasil", "regiao")
    state = _metadata_value(metadata, "uf", "estado")
    country = _metadata_value(metadata, "pais", "country")
    revenue = _metadata_value(
        metadata,
        "faixa_faturamento_cliente_ec",
        "faixa_faturamento",
        "revenue_band",
    )
    cnae = normalize_cnae(cnae_raw)
    transcript = "\n".join(segment_item.text for segment_item in meeting.segments)
    candidates = {
        "metadata": _sector_by_alias(segment, playbooks),
        "cnae": _sector_by_cnae(cnae, playbooks),
        "unit": _sector_by_unit(unit, playbooks),
    }
    confirmed = {value for value in candidates.values() if value}
    uncertainties: list[str] = []
    if len(confirmed) > 1:
        sector_id = "generico"
        source: Literal["metadata", "cnae", "unit", "transcript", "fallback", "conflict"] = (
            "conflict"
        )
        uncertainties.append(
            "Os metadados de setor, CNAE e unidade apontam para setores diferentes. "
            "O contexto genérico foi usado para evitar uma inferência silenciosa."
        )
    elif confirmed:
        sector_id = candidates["metadata"] or candidates["cnae"] or candidates["unit"]
        source = next(key for key in ("metadata", "cnae", "unit") if candidates[key])
    else:
        inferred = _sector_by_transcript(transcript, playbooks)
        sector_id = inferred or "generico"
        source = "transcript" if inferred else "fallback"
        if inferred:
            uncertainties.append(
                "O setor foi apenas sugerido por vocabulário da transcrição e não substitui "
                "o cadastro do cliente."
            )
        else:
            uncertainties.append("Não havia setor confiável; o playbook genérico foi aplicado.")
    playbook = playbooks[sector_id]
    default_meeting_rules = playbooks["generico"].meeting_type_rules
    meeting_type_rules = {
        meeting_type: playbook.meeting_type_rules.get(meeting_type)
        or default_meeting_rules[meeting_type]
        for meeting_type in sorted(MEETING_TYPES)
    }
    subsector = _subsector(playbook, cnae, transcript)
    fields = [segment, cnae_raw, region, state, revenue]
    completeness = round(sum(value not in (None, "") for value in fields) / len(fields), 2)
    normalized_metadata = {
        "sector": normalize_term(segment) or None,
        "cnae": cnae or None,
        "unit": str(unit) if unit else None,
        "country": str(country) if country else None,
        "region": str(region) if region else None,
        "state": str(state) if state else None,
        "revenue_band": str(revenue) if revenue else None,
    }
    snapshot = {
        "context_version": CONTEXT_VERSION,
        "playbook": {
            "id": playbook.id,
            "label": playbook.label,
            "version": playbook.version,
            "reviewed_at": playbook.reviewed_at.isoformat(),
        },
        "selection": {
            "source": source,
            "candidates": candidates,
            "subsector": subsector,
            "confidence": "low" if source in {"transcript", "fallback", "conflict"} else "high",
        },
        "account": {
            **normalized_metadata,
            "completeness": completeness,
            "missing_fields": [
                name
                for name, value in zip(
                    ["sector", "cnae", "region", "state", "revenue_band"],
                    fields,
                    strict=True,
                )
                if value in (None, "")
            ],
        },
        "knowledge": _selected_knowledge(playbook, transcript),
        "signals": {
            "possible_problems": [
                item.model_dump(mode="json") for item in playbook.possible_problems
            ],
            "positive": [item.model_dump(mode="json") for item in playbook.positive_signals],
            "attention": [item.model_dump(mode="json") for item in playbook.attention_signals],
        },
        "meeting_type_rules": {
            key: value.model_dump(mode="json") for key, value in meeting_type_rules.items()
        },
        "prohibited_inferences": playbook.prohibited_inferences,
        "sources": [source_item.model_dump(mode="json") for source_item in playbook.sources],
        "uncertainties": uncertainties,
        "context_policy": {
            "metadata_is_not_transcript_evidence": True,
            "metadata_cannot_raise_relationship_risk_alone": True,
            "revenue_and_location_only_adjust_operational_priority": True,
            "sector_patterns_are_hypotheses_until_supported": True,
        },
    }
    canonical = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    snapshot["context_sha256"] = sha256(canonical.encode()).hexdigest()
    return snapshot


def context_for_ai(snapshot: dict[str, Any] | None) -> dict[str, Any]:
    if not snapshot:
        return {}
    payload = {
        key: snapshot[key]
        for key in (
            "context_version",
            "playbook",
            "selection",
            "account",
            "knowledge",
            "signals",
            "meeting_type_rules",
            "prohibited_inferences",
            "uncertainties",
            "context_policy",
        )
        if key in snapshot
    }
    account = dict(payload.get("account") or {})
    for regional_field in ("country", "region", "state"):
        account.pop(regional_field, None)
    account["missing_fields"] = [
        field
        for field in account.get("missing_fields") or []
        if field not in {"country", "region", "state"}
    ]
    payload["account"] = account
    policy = dict(payload.get("context_policy") or {})
    policy.pop("revenue_and_location_only_adjust_operational_priority", None)
    policy["revenue_only_adjusts_operational_priority"] = True
    payload["context_policy"] = policy
    return payload


def public_context_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Return the auditable preview without exposing prompt guidance or internal rules."""
    return {
        key: snapshot[key]
        for key in (
            "context_version",
            "context_sha256",
            "playbook",
            "selection",
            "account",
            "sources",
            "uncertainties",
            "context_policy",
        )
        if key in snapshot
    }
