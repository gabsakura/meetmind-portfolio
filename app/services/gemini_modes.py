from __future__ import annotations

import json
import logging
import os
import re
import time
import unicodedata
from hashlib import sha256
from pathlib import Path
from typing import Any

from google import genai
from google.genai import types
from pydantic import BaseModel, ValidationError

from app.schemas import (
    AnalysisMode,
    MeetingOutcomeResult,
    ModeSynthesisResult,
    ProductOperationsResult,
    RelationshipRiskResult,
    RoutingResult,
    VocComplaintResult,
)
from app.services.gemini import (
    InvalidModelOutput,
    _as_list,
    _clip_text,
    _extract_json_payload,
    _gemini_json_schema,
    _response_usage,
    estimate_tokens,
)

logger = logging.getLogger(__name__)

AI_CONFIG_DIR = Path(__file__).parents[1] / "ai"
CORE_POLICY = json.loads(
    (AI_CONFIG_DIR / "core_policy.en.json").read_text(encoding="utf-8")
)
MODE_POLICIES = json.loads(
    (AI_CONFIG_DIR / "mode_policies.en.json").read_text(encoding="utf-8")
)
MANUFACTURING_CATALOG = json.loads(
    (AI_CONFIG_DIR / "manufacturing_catalog.json").read_text(encoding="utf-8")
)

MODE_BUNDLE_VERSION = str(CORE_POLICY["version"])
MODE_BUNDLE_SHA256 = sha256(
    json.dumps(
        {"core": CORE_POLICY, "modes": MODE_POLICIES},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
).hexdigest()
MANUFACTURING_CATALOG_SHA256 = sha256(
    json.dumps(
        MANUFACTURING_CATALOG,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
).hexdigest()

SUPPORTED_MODES: tuple[AnalysisMode, ...] = (
    "voc_complaint",
    "meeting_outcome",
    "product_operations",
    "relationship_risk",
)

STAGE_MODELS: dict[str, type[BaseModel]] = {
    "router": RoutingResult,
    "voc_complaint": VocComplaintResult,
    "meeting_outcome": MeetingOutcomeResult,
    "product_operations": ProductOperationsResult,
    "relationship_risk": RelationshipRiskResult,
    "synthesis": ModeSynthesisResult,
}


def analysis_profile_hash(mode_strategy: str, requested_modes: list[str]) -> str:
    payload = {
        "bundle_version": MODE_BUNDLE_VERSION,
        "bundle_sha256": MODE_BUNDLE_SHA256,
        "mode_strategy": mode_strategy,
        "requested_modes": sorted(requested_modes),
    }
    return sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _normalized(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value.casefold())
    return "".join(character for character in decomposed if not unicodedata.combining(character))


def _contains_term(normalized_text: str, term: str) -> bool:
    normalized_term = _normalized(term).strip()
    if not normalized_term:
        return False
    return bool(
        re.search(
            rf"(?<!\w){re.escape(normalized_term)}(?!\w)",
            normalized_text,
            flags=re.IGNORECASE,
        )
    )


def select_manufacturing_context(text: str, limit: int = 5) -> dict[str, Any]:
    normalized_text = _normalized(text)
    ranked: list[tuple[int, int, dict[str, Any]]] = []
    for product in MANUFACTURING_CATALOG["products"]:
        matched = [
            alias
            for alias in product.get("aliases", [])
            if _contains_term(normalized_text, alias)
        ]
        if not matched:
            continue
        ranked.append(
            (
                len(matched),
                max(len(alias) for alias in matched),
                {**product, "matched_aliases": matched[:5]},
            )
        )
    ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)

    ambiguous = []
    ambiguity_needles = {
        "replication / replicação": ("replication", "replicação"),
        "Progress User": ("progress user", "usuário progress"),
        "servidor / instabilidade": ("servidor", "instabilidade"),
        "licença": ("licença", "licencas", "licenças"),
    }
    for item in MANUFACTURING_CATALOG.get("ambiguous_terms", []):
        needles = ambiguity_needles.get(item["term"], (item["term"],))
        if any(_contains_term(normalized_text, needle) for needle in needles):
            ambiguous.append(item)

    return {
        "catalog_version": MANUFACTURING_CATALOG["schema_version"],
        "catalog_sha256": MANUFACTURING_CATALOG_SHA256,
        "products": [item[2] for item in ranked[:limit]],
        "ambiguous_terms": ambiguous,
    }


def _system_instruction(stage: str) -> str:
    policy = MODE_POLICIES[stage]
    return "\n".join(
        [
            f"<role>\n{CORE_POLICY['role']}\n</role>",
            "<language_policy>\n"
            + json.dumps(CORE_POLICY["language_policy"], ensure_ascii=False)
            + "\n</language_policy>",
            "<trust_boundary>\n"
            + json.dumps(CORE_POLICY["trust_boundary"], ensure_ascii=False)
            + "\n</trust_boundary>",
            "<evidence_policy>\n"
            + json.dumps(CORE_POLICY["evidence_policy"], ensure_ascii=False)
            + "\n</evidence_policy>",
            f"<manufacturing_context>\n{CORE_POLICY['manufacturing_context']}\n</manufacturing_context>",
            f"<task>\n{policy['objective']}\n</task>",
            "<mode_instructions>\n"
            + json.dumps(policy, ensure_ascii=False)
            + "\n</mode_instructions>",
            "<output_contract>\n"
            + json.dumps(CORE_POLICY["output_contract"], ensure_ascii=False)
            + "\n</output_contract>",
        ]
    )


def _schema_hash(model_class: type[BaseModel]) -> str:
    return sha256(
        json.dumps(
            model_class.model_json_schema(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _normalize_routing_payload(payload: Any) -> tuple[Any, bool]:
    if not isinstance(payload, dict):
        return payload, False

    normalized = dict(payload)
    changed = False

    reason = _clip_text(normalized.get("reason"), 300)
    if reason != normalized.get("reason"):
        normalized["reason"] = reason
        changed = True

    secondary_modes = _as_list(normalized.get("secondary_modes"))[:1]
    if secondary_modes != normalized.get("secondary_modes"):
        normalized["secondary_modes"] = secondary_modes
        changed = True

    evidence_items = []
    for item in _as_list(normalized.get("evidence"))[:2]:
        if not isinstance(item, dict):
            evidence_items.append(item)
            continue
        evidence = dict(item)
        for field, limit in (("segment_id", 100), ("quote", 350), ("relevance", 240)):
            clipped = _clip_text(evidence.get(field), limit)
            if clipped != evidence.get(field):
                evidence[field] = clipped
                changed = True
        evidence_items.append(evidence)
    if evidence_items != normalized.get("evidence"):
        normalized["evidence"] = evidence_items
        changed = True

    return normalized, changed


def _resolve_schema(schema: dict[str, Any], root: dict[str, Any]) -> dict[str, Any]:
    reference = schema.get("$ref")
    if not isinstance(reference, str) or not reference.startswith("#/$defs/"):
        return schema
    return root.get("$defs", {}).get(reference.removeprefix("#/$defs/"), schema)


def _matching_schema(value: Any, schema: dict[str, Any], root: dict[str, Any]):
    candidates = schema.get("anyOf")
    if not isinstance(candidates, list):
        return _resolve_schema(schema, root)
    value_type = (
        "null"
        if value is None
        else "object"
        if isinstance(value, dict)
        else "array"
        if isinstance(value, list)
        else "string"
        if isinstance(value, str)
        else "boolean"
        if isinstance(value, bool)
        else "number"
        if isinstance(value, (int, float))
        else None
    )
    for candidate in candidates:
        resolved = _resolve_schema(candidate, root)
        if resolved.get("type") in {value_type, "integer" if value_type == "number" else None}:
            return resolved
    return _resolve_schema(candidates[0], root)


_ENUM_ALIASES: dict[str, tuple[str, ...]] = {
    "tecnica": ("technical",),
    "tecnico": ("technical",),
    "operacional": ("operational",),
    "licenca": ("licensing",),
    "licenciamento": ("licensing",),
    "contrato": ("contract",),
    "contratual": ("contract",),
    "customizacao": ("customization",),
    "servico": ("service",),
    "atendimento": ("service",),
    "infraestrutura": ("infrastructure",),
    "fraca": ("weak",),
    "fraco": ("weak",),
    "moderada": ("moderate",),
    "moderado": ("moderate",),
    "media": ("medium", "moderate"),
    "medio": ("medium", "moderate"),
    "forte": ("strong",),
    "baixo": ("low",),
    "baixa": ("low",),
    "alto": ("high",),
    "alta": ("high",),
    "critico": ("critical",),
    "critica": ("critical",),
    "ausente": ("absent",),
    "resolvido": ("resolved",),
    "resolvida": ("resolved",),
    "concluido": ("resolved",),
    "concluida": ("resolved",),
    "parcial": ("partial",),
    "nao_resolvido": ("unresolved",),
    "nao_resolvida": ("unresolved",),
    "incerto": ("unclear",),
    "incerta": ("unclear",),
    "agendada": ("explicit_scheduled",),
    "agendado": ("explicit_scheduled",),
    "presencial": ("explicit_in_person",),
    "proposto": ("proposed",),
    "proposta": ("proposed",),
    "nenhum": ("none",),
    "nenhuma": ("none",),
    "alinhamento_de_ideias": ("idea_alignment",),
    "apresentacao_de_solucao": ("solution_presentation",),
    "diagnostico_tecnico": ("technical_diagnosis",),
    "duvidas_e_esclarecimentos": ("questions_clarifications",),
    "negociacao_contratual": ("contract_negotiation",),
    "resolucao_de_problema": ("problem_resolution",),
}

_UNCERTAINTY_MARKERS = (
    "hipotese",
    "pode",
    "possivel",
    "possivelmente",
    "provavel",
    "provavelmente",
    "a confirmar",
    "a validar",
    "nao confirmado",
)


def _is_qualified_hypothesis(statement: str) -> bool:
    normalized = _normalized(statement)
    return any(marker in normalized for marker in _UNCERTAINTY_MARKERS)


def _enum_value(value: str, allowed: list[Any]) -> tuple[str, bool]:
    if value in allowed:
        return value, False
    token = re.sub(r"[^a-z0-9]+", "_", _normalized(value)).strip("_")
    canonical_by_token = {
        re.sub(r"[^a-z0-9]+", "_", _normalized(str(item))).strip("_"): item
        for item in allowed
        if isinstance(item, str)
    }
    if token in canonical_by_token:
        return str(canonical_by_token[token]), True
    for candidate in _ENUM_ALIASES.get(token, ()):
        if candidate in allowed:
            return candidate, True
    for fallback in ("unknown", "unclear", "other", "absent"):
        if fallback in allowed:
            return fallback, True
    return value, False


def _apply_schema_bounds(
    value: Any, schema: dict[str, Any], root: dict[str, Any]
) -> tuple[Any, bool]:
    schema = _matching_schema(value, schema, root)
    if "const" in schema and value != schema["const"]:
        return schema["const"], True

    if isinstance(value, str):
        allowed = schema.get("enum")
        if isinstance(allowed, list):
            value, enum_changed = _enum_value(value, allowed)
        else:
            enum_changed = False
        clipped = _clip_text(value, int(schema.get("maxLength", len(value))))
        return clipped, enum_changed or clipped != value

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        normalized_number = value
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if isinstance(minimum, (int, float)):
            normalized_number = max(normalized_number, minimum)
        if isinstance(maximum, (int, float)):
            normalized_number = min(normalized_number, maximum)
        return normalized_number, normalized_number != value

    if isinstance(value, list):
        normalized_items = []
        changed = False
        item_schema = schema.get("items", {})
        for item in value:
            normalized, item_changed = _apply_schema_bounds(item, item_schema, root)
            normalized_items.append(normalized)
            changed = changed or item_changed
        maximum = schema.get("maxItems")
        if isinstance(maximum, int) and len(normalized_items) > maximum:
            normalized_items = normalized_items[:maximum]
            changed = True
        return normalized_items, changed

    if isinstance(value, dict):
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            normalized = {key: item for key, item in value.items() if key in properties}
            changed = len(normalized) != len(value)
        else:
            normalized = dict(value)
            changed = False
        for field, field_schema in properties.items():
            if field not in normalized:
                continue
            field_value, field_changed = _apply_schema_bounds(
                normalized[field], field_schema, root
            )
            normalized[field] = field_value
            changed = changed or field_changed
        return normalized, changed

    return value, False


def _normalize_stage_payload(
    payload: Any, model_class: type[BaseModel]
) -> tuple[Any, bool]:
    root_schema = model_class.model_json_schema()
    normalized, changed = _apply_schema_bounds(payload, root_schema, root_schema)
    if model_class is RoutingResult:
        routing, routing_changed = _normalize_routing_payload(normalized)
        primary_mode = routing.get("primary_mode") if isinstance(routing, dict) else None
        if isinstance(routing, dict) and primary_mode in routing.get("secondary_modes", []):
            routing["secondary_modes"] = [
                mode for mode in routing["secondary_modes"] if mode != primary_mode
            ]
            routing_changed = True
        return routing, changed or routing_changed
    if model_class is VocComplaintResult and isinstance(normalized, dict):
        complaints = normalized.get("complaints")
        if isinstance(complaints, list):
            for index, complaint in enumerate(complaints, start=1):
                expected_id = f"complaint-{index}"
                if (
                    isinstance(complaint, dict)
                    and complaint.get("complaint_id") != expected_id
                ):
                    complaint["complaint_id"] = expected_id
                    changed = True
    if model_class is ProductOperationsResult and isinstance(normalized, dict):
        hypotheses = normalized.get("hypotheses")
        if isinstance(hypotheses, list):
            for hypothesis in hypotheses:
                if not isinstance(hypothesis, dict):
                    continue
                statement = hypothesis.get("statement")
                if isinstance(statement, str) and statement and not _is_qualified_hypothesis(
                    statement
                ):
                    hypothesis["statement"] = _clip_text(
                        f"Hipótese a validar: {statement}",
                        300,
                    )
                    changed = True
    return normalized, changed


def _parse_stage_result(text: str, model_class: type[BaseModel]) -> BaseModel:
    try:
        payload, normalized = _normalize_stage_payload(
            _extract_json_payload(text), model_class
        )
        if normalized:
            logger.warning(
                "ai_stage_output_normalized schema=%s "
                "constraints=schema_bounds_or_safe_semantics",
                model_class.__name__,
            )
        return model_class.model_validate(payload)
    except ValidationError as error:
        reasons = ",".join(
            sorted({str(item["type"]) for item in error.errors(include_url=False)})
        )
        raise InvalidModelOutput(f"invalid_{model_class.__name__}:{reasons}") from error
    except (TypeError, ValueError) as error:
        raise InvalidModelOutput(f"invalid_{model_class.__name__}") from error


def _generate_stage(
    stage: str,
    model: str,
    contents: str,
    trace_name: str | None = None,
) -> tuple[BaseModel, dict[str, Any]]:
    model_class = STAGE_MODELS[stage]
    provider_schema = _gemini_json_schema(model_class.model_json_schema())
    started = time.monotonic()
    input_tokens = output_tokens = repairs = 0
    with genai.Client(api_key=os.environ["GEMINI_API_KEY"]) as client:
        response = client.models.generate_content(
            model=model,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=_system_instruction(stage),
                response_mime_type="application/json",
                response_json_schema=provider_schema,
            ),
        )
        first_input, first_output = _response_usage(response, contents)
        input_tokens += first_input
        output_tokens += first_output
        try:
            result = _parse_stage_result(response.text, model_class)
        except InvalidModelOutput as first_error:
            if not response.text:
                raise
            repairs = 1
            invalid_output = str(response.text)[:20_000]
            repair_contents = (
                f"{contents}\n"
                "<REPAIR_REQUEST>Repair only the JSON structure. Do not add facts, "
                "change evidence, translate quotes or change meaning. Use exactly the "
                f"provided schema. Local failure: {first_error}</REPAIR_REQUEST>\n"
                f"<INVALID_OUTPUT_UNTRUSTED>{invalid_output}</INVALID_OUTPUT_UNTRUSTED>"
            )
            repaired = client.models.generate_content(
                model=model,
                contents=repair_contents,
                config=types.GenerateContentConfig(
                    system_instruction=_system_instruction(stage),
                    response_mime_type="application/json",
                    response_json_schema=provider_schema,
                ),
            )
            repair_input, repair_output = _response_usage(repaired, repair_contents)
            input_tokens += repair_input
            output_tokens += repair_output
            try:
                result = _parse_stage_result(repaired.text, model_class)
            except InvalidModelOutput as repair_error:
                raise InvalidModelOutput(f"{repair_error}:repair_failed") from repair_error

    return result, {
        "stage": trace_name or stage,
        "model": model,
        "policy_version": MODE_BUNDLE_VERSION,
        "policy_sha256": MODE_BUNDLE_SHA256,
        "schema_name": model_class.__name__,
        "schema_version": "meetmind-modes-v1",
        "schema_sha256": _schema_hash(model_class),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "latency_ms": int((time.monotonic() - started) * 1000),
        "format_repair_count": repairs,
    }


def _transcript_contents(
    text: str,
    local_metrics: dict[str, Any] | None,
    manufacturing_context: dict[str, Any] | None = None,
) -> str:
    return (
        "<TRUSTED_LOCAL_METRICS>"
        f"{json.dumps(local_metrics or {}, ensure_ascii=False, separators=(',', ':'))}"
        "</TRUSTED_LOCAL_METRICS>\n"
        "<TRUSTED_MANUFACTURING_CONTEXT>"
        f"{json.dumps(manufacturing_context or {}, ensure_ascii=False, separators=(',', ':'))}"
        "</TRUSTED_MANUFACTURING_CONTEXT>\n"
        f"<TRANSCRIPT_UNTRUSTED>\n{text}\n</TRANSCRIPT_UNTRUSTED>"
    )


def _chunks(segment_lines: list[str], limit: int = 18_000) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    current_tokens = 0
    for line in segment_lines:
        tokens = estimate_tokens(line)
        if current and current_tokens + tokens > limit:
            chunks.append("\n".join(current))
            current = []
            current_tokens = 0
        current.append(line)
        current_tokens += tokens
    if current:
        chunks.append("\n".join(current))
    return chunks


def _run_transcript_stage(
    stage: str,
    text: str,
    model: str,
    local_metrics: dict[str, Any] | None,
    manufacturing_context: dict[str, Any] | None = None,
) -> tuple[BaseModel, list[dict[str, Any]]]:
    if estimate_tokens(text) <= 30_000:
        result, trace = _generate_stage(
            stage,
            model,
            _transcript_contents(text, local_metrics, manufacturing_context),
        )
        return result, [trace]

    partials: list[dict[str, Any]] = []
    traces: list[dict[str, Any]] = []
    for index, chunk in enumerate(_chunks(text.splitlines()), start=1):
        result, trace = _generate_stage(
            stage,
            model,
            _transcript_contents(chunk, local_metrics, manufacturing_context),
            trace_name=f"{stage}:chunk-{index}",
        )
        partials.append(result.model_dump())
        traces.append(trace)

    consolidation_contents = (
        "Consolidate the partial results below without adding facts, SEGMENT_ID values "
        "or evidence quotes. Respect all list limits and preserve the strongest supported "
        "items.\n<PARTIAL_RESULTS_UNTRUSTED>"
        f"{json.dumps(partials, ensure_ascii=False)}"
        "</PARTIAL_RESULTS_UNTRUSTED>"
    )
    result, trace = _generate_stage(
        stage,
        model,
        consolidation_contents,
        trace_name=f"{stage}:consolidation",
    )
    traces.append(trace)
    return result, traces


def _collect_evidence_ids(value: Any) -> set[str]:
    if isinstance(value, list):
        result: set[str] = set()
        for item in value:
            result.update(_collect_evidence_ids(item))
        return result
    if not isinstance(value, dict):
        return set()
    result = set()
    segment_id = value.get("segment_id")
    if isinstance(segment_id, str):
        result.add(segment_id)
    for item in value.values():
        result.update(_collect_evidence_ids(item))
    return result


def _validate_mode_semantics(
    mode: AnalysisMode,
    result: BaseModel,
    manufacturing_context: dict[str, Any] | None,
) -> BaseModel:
    payload = result.model_dump()
    if mode != "product_operations":
        return result

    allowed_products = {
        item["id"]: item
        for item in (manufacturing_context or {}).get("products", [])
    }
    validated_mentions = []
    omitted_mentions = 0
    for mention in payload.get("product_mentions", []):
        canonical_id = mention.get("canonical_id")
        canonical_name = mention.get("canonical_name")
        if canonical_name and not canonical_id:
            raise InvalidModelOutput("canonical_product_name_without_id")
        if canonical_id and canonical_id not in allowed_products:
            raise InvalidModelOutput("product_not_in_retrieved_catalog")
        if canonical_id and canonical_name != allowed_products[canonical_id]["name"]:
            raise InvalidModelOutput("canonical_product_name_mismatch")
        quote_text = " ".join(
            _normalized(item.get("quote", "")) for item in mention.get("evidence", [])
        )
        candidate_terms = [mention.get("original_term", "")]
        if canonical_id:
            candidate_terms.extend(allowed_products[canonical_id].get("matched_aliases", []))
        literal_term = next(
            (
                term
                for term in candidate_terms
                if isinstance(term, str)
                and _normalized(term).strip()
                and _normalized(term).strip() in quote_text
            ),
            None,
        )
        if literal_term is None:
            omitted_mentions += 1
            continue
        mention["original_term"] = literal_term
        validated_mentions.append(mention)

    if omitted_mentions:
        payload["product_mentions"] = validated_mentions
        uncertainty = "Menção de produto sem termo literal na evidência foi omitida."
        uncertainties = payload.setdefault("uncertainties", [])
        if uncertainty not in uncertainties and len(uncertainties) < 3:
            uncertainties.append(uncertainty)
        logger.warning(
            "ai_product_mentions_omitted_without_literal_evidence count=%s",
            omitted_mentions,
        )

    validated_result = ProductOperationsResult.model_validate(payload)

    for hypothesis in validated_result.hypotheses:
        if not _is_qualified_hypothesis(hypothesis.statement):
            raise InvalidModelOutput("technical_hypothesis_not_qualified")
    return validated_result


def analyze_transcript_modes(
    text: str,
    model: str,
    local_metrics: dict[str, Any] | None,
    mode_strategy: str,
    requested_modes: list[AnalysisMode],
) -> tuple[dict[str, Any], int, int, int, int]:
    traces: list[dict[str, Any]] = []
    routing: dict[str, Any]
    if mode_strategy == "auto":
        route_result, route_traces = _run_transcript_stage(
            "router", text, model, local_metrics
        )
        traces.extend(route_traces)
        assert isinstance(route_result, RoutingResult)
        selected_modes: list[AnalysisMode] = [
            route_result.primary_mode,
            *route_result.secondary_modes,
        ]
        routing = {"source": "automatic", **route_result.model_dump()}
    else:
        selected_modes = list(requested_modes)
        routing = {
            "source": "manual",
            "meeting_type": "unknown",
            "primary_mode": selected_modes[0],
            "secondary_modes": selected_modes[1:],
            "confidence": 1.0,
            "reason": "Modos selecionados manualmente pelo analista.",
            "evidence": [],
        }
    routing.update(
        {
            "strategy": mode_strategy,
            "selected_modes": selected_modes,
        }
    )

    mode_results: dict[str, dict[str, Any]] = {}
    manufacturing_context = select_manufacturing_context(text)
    for mode in selected_modes:
        context = manufacturing_context if mode == "product_operations" else None
        mode_result, mode_traces = _run_transcript_stage(
            mode,
            text,
            model,
            local_metrics,
            context,
        )
        traces.extend(mode_traces)
        mode_result = _validate_mode_semantics(mode, mode_result, context)
        mode_results[mode] = mode_result.model_dump()

    synthesis_contents = (
        "Synthesize only the validated mode results below. Do not introduce facts, quotes "
        "or SEGMENT_ID values that are absent from them.\n"
        "<VALIDATED_ROUTING>"
        f"{json.dumps(routing, ensure_ascii=False)}"
        "</VALIDATED_ROUTING>\n"
        "<VALIDATED_MODE_RESULTS>"
        f"{json.dumps(mode_results, ensure_ascii=False)}"
        "</VALIDATED_MODE_RESULTS>"
    )
    synthesis_result, synthesis_trace = _generate_stage(
        "synthesis", model, synthesis_contents
    )
    traces.append(synthesis_trace)
    assert isinstance(synthesis_result, ModeSynthesisResult)
    available_evidence_ids = _collect_evidence_ids(mode_results)
    synthesis_evidence_ids = set(synthesis_result.evidence_segment_ids)
    if not synthesis_evidence_ids.issubset(available_evidence_ids):
        raise InvalidModelOutput("synthesis_unknown_evidence_segment")
    if available_evidence_ids and not synthesis_evidence_ids:
        raise InvalidModelOutput("synthesis_missing_evidence_segment")

    total_input = sum(int(trace["input_tokens"]) for trace in traces)
    total_output = sum(int(trace["output_tokens"]) for trace in traces)
    total_latency = sum(int(trace["latency_ms"]) for trace in traces)
    total_repairs = sum(int(trace["format_repair_count"]) for trace in traces)
    payload = {
        "schema_version": "meetmind-modes-v1",
        "mode_strategy": mode_strategy,
        "requested_modes": list(requested_modes),
        "selected_modes": selected_modes,
        "routing": routing,
        "mode_results": mode_results,
        "synthesis": synthesis_result.model_dump(),
        "manufacturing_context": {
            "catalog_version": manufacturing_context["catalog_version"],
            "catalog_sha256": manufacturing_context["catalog_sha256"],
            "matched_product_ids": [
                item["id"] for item in manufacturing_context["products"]
            ],
        },
        "traceability": {
            "policy_version": MODE_BUNDLE_VERSION,
            "policy_sha256": MODE_BUNDLE_SHA256,
            "catalog_sha256": MANUFACTURING_CATALOG_SHA256,
            "stages": traces,
        },
    }
    return payload, total_input, total_output, total_latency, total_repairs


__all__ = [
    "MANUFACTURING_CATALOG_SHA256",
    "MODE_BUNDLE_SHA256",
    "MODE_BUNDLE_VERSION",
    "SUPPORTED_MODES",
    "analysis_profile_hash",
    "analyze_transcript_modes",
    "select_manufacturing_context",
]
