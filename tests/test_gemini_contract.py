from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest
from google.genai.errors import ServerError
from pydantic import ValidationError

from app.jobs import _literal_quote_alignment, _validated_result_payload
from app.models import Analysis, Segment
from app.schemas import GeminiResult
from app.services.gemini import (
    ANALYSIS_POLICY,
    ANALYSIS_POLICY_SHA256,
    GEMINI_RESPONSE_JSON_SCHEMA,
    GEMINI_RESPONSE_SCHEMA_SHA256,
    SYSTEM_INSTRUCTION,
    InvalidModelOutput,
    _analysis_contents,
    _generate,
    _parse_model_result,
    estimate_tokens,
)


def valid_result():
    return {
        "manufacturing_brief": {
            "what_happened": "O cliente relatou uma falha operacional.",
            "what_happened_evidence": [
                {
                    "segment_id": "meeting.0",
                    "quote": "o sistema travou",
                    "relevance": "Confirma a falha relatada.",
                }
            ],
            "customer_requests": [],
            "objective": {
                "status": "unclear",
                "rationale": "O objetivo não foi explicitado.",
                "confidence": 0.2,
                "evidence": [],
            },
            "follow_up": {
                "status": "none",
                "rationale": "Nenhum próximo encontro foi explicitado.",
                "confidence": 0.5,
                "evidence": [],
            },
            "conflict": {
                "status": "none_detected",
                "rationale": "Nenhum conflito explícito foi identificado.",
                "confidence": 0.5,
                "evidence": [],
            },
            "concern": {
                "status": "attention",
                "rationale": "A interrupção operacional exige atenção.",
                "confidence": 0.9,
                "evidence": [
                    {
                        "segment_id": "meeting.0",
                        "quote": "o sistema travou",
                        "relevance": "Confirma impacto operacional.",
                    }
                ],
            },
            "suggested_actions": [
                {
                    "action": "Validar logs",
                    "rationale": "Identificar a causa antes de propor solução.",
                    "owner": "Suporte",
                    "urgency": "immediate",
                    "evidence_segment_ids": ["meeting.0"],
                }
            ],
            "uncertainties": ["Sem data de decisão."],
        },
        "executive_summary": {
            "what_happened": "O cliente relatou uma falha.",
            "why_it_matters": "A falha interrompe a operação.",
            "next_step": "Validar a causa técnica.",
        },
        "overall_sentiment": "negative",
        "risk_level": "high",
        "meeting_classification": {
            "sector": "Contexto genérico",
            "subsector": None,
            "sector_source": "fallback",
            "meeting_type": "support_incident",
            "confidence": 0.92,
            "evidence_segment_ids": ["meeting.0"],
        },
        "account_context": {
            "sector": "Contexto genérico",
            "subsector": None,
            "revenue_band": None,
            "completeness": 0,
            "missing_fields": ["sector", "cnae", "region", "state", "revenue_band"],
            "limitations": ["Contexto cadastral ausente."],
        },
        "relationship_health": {
            "status": "attention",
            "rationale": "O problema operacional exige acompanhamento.",
            "confidence": 0.9,
            "evidence_segment_ids": ["meeting.0"],
        },
        "retention_signals": [
            {
                "signal_type": "attention",
                "label": "Interrupção operacional",
                "description": "O cliente relatou interrupção da operação.",
                "confidence": 0.9,
                "trigger": {
                    "trigger_type": "operational_problem",
                    "label": "Travamento",
                    "reason": "A fala relata interrupção.",
                    "matched_terms": ["travou"],
                    "local_metric_category": "operational_problems",
                },
                "evidence": [
                    {
                        "segment_id": "meeting.0",
                        "quote": "o sistema travou",
                        "relevance": "Confirma a interrupção.",
                    }
                ],
            }
        ],
        "context_application": [],
        "contextual_priority": "high",
        "products_mentioned": ["Protheus"],
        "findings": [
            {
                "finding_id": "finding-1",
                "category": "technical",
                "title": "Falha operacional",
                "observation": "O cliente afirmou que o sistema travou.",
                "interpretation": "Há impacto operacional explícito.",
                "business_impact": "A atividade ficou interrompida.",
                "recommendation": "Validar logs e causa raiz.",
                "confidence": 0.95,
                "trigger": {
                    "trigger_type": "operational_problem",
                    "label": "Relato de travamento",
                    "reason": "A fala descreve diretamente uma interrupção.",
                    "matched_terms": ["travou"],
                    "local_metric_category": "operational_problems",
                },
                "evidence": [
                    {
                        "segment_id": "meeting.0",
                        "quote": "o sistema travou",
                        "relevance": "Confirma o problema técnico.",
                    }
                ],
            }
        ],
        "action_items": [
            {
                "action": "Validar logs",
                "rationale": "Identificar a causa antes de propor solução.",
                "owner": "Suporte",
                "urgency": "immediate",
                "evidence_segment_ids": ["meeting.0"],
            }
        ],
        "uncertainties": ["Sem data de decisão."],
    }


def test_structured_output_rejects_extra_fields():
    payload = valid_result() | {"command": "DROP TABLE meetings"}
    with pytest.raises(ValidationError):
        GeminiResult.model_validate(payload)


def test_prompt_marks_transcript_as_untrusted_and_has_no_tools():
    assert "CONTEÚDO NÃO CONFIÁVEL" in SYSTEM_INSTRUCTION
    assert "não use ferramentas" in SYSTEM_INSTRUCTION
    assert "observation" in SYSTEM_INSTRUCTION
    assert "interpretation" in SYSTEM_INSTRUCTION
    assert "TRUSTED_SECTOR_CONTEXT" in _analysis_contents("texto", {}, {})
    assert ANALYSIS_POLICY["version"] == "meetmind-manufacturing-v1"


def test_evidence_quote_is_verified_and_enriched():
    result = GeminiResult.model_validate(valid_result())
    segment = Segment(
        id="meeting.0",
        position=0,
        speaker="Cliente",
        text="Durante o fechamento o sistema travou e paramos a operação.",
        started_at_ms=1200,
        ended_at_ms=5400,
    )
    analysis = Analysis(
        model="gemini-test",
        prompt_version="meetmind-transparent-v4",
        score_version="voc-deterministic-v1",
        transcript_hash="a" * 64,
    )
    payload = _validated_result_payload(result, [segment], analysis)
    evidence = payload["findings"][0]["evidence"][0]
    assert evidence["speaker"] == "Cliente"
    assert evidence["started_at_ms"] == 1200
    assert payload["traceability"]["policy_sha256"] == ANALYSIS_POLICY_SHA256
    assert payload["traceability"]["exact_evidence_count"] == 4
    assert payload["traceability"]["realigned_evidence_count"] == 0


def test_quote_with_punctuation_difference_is_realigned_to_literal_source():
    aligned = _literal_quote_alignment(
        "durante o fechamento, o sistema travou",
        "Durante o fechamento o sistema travou e paramos a operação.",
    )

    assert aligned is not None
    quote, method = aligned
    assert quote in "Durante o fechamento o sistema travou e paramos a operação."
    assert method == "deterministic_alignment"


def test_unknown_evidence_id_is_reassigned_only_by_unique_literal_quote():
    payload = valid_result()
    payload["findings"][0]["evidence"][0]["segment_id"] = "invented.99"
    result = GeminiResult.model_validate(payload)
    segment = Segment(
        id="meeting.0",
        position=0,
        speaker="Cliente",
        text="Durante o fechamento o sistema travou e paramos a operação.",
    )
    analysis = Analysis(
        model="gemini-test",
        prompt_version="meetmind-transparent-v4",
        score_version="voc-deterministic-v1",
        transcript_hash="a" * 64,
    )

    validated = _validated_result_payload(result, [segment], analysis)

    assert validated["findings"][0]["evidence"][0]["segment_id"] == "meeting.0"
    assert validated["traceability"]["reassigned_evidence_segment_count"] == 1


def test_invalid_unquoted_references_degrade_conclusions_instead_of_failing():
    payload = valid_result()
    payload["meeting_classification"]["evidence_segment_ids"] = ["invented.1"]
    payload["relationship_health"]["evidence_segment_ids"] = ["invented.2"]
    payload["action_items"][0]["evidence_segment_ids"] = ["invented.3"]
    result = GeminiResult.model_validate(payload)
    segment = Segment(
        id="meeting.0",
        position=0,
        speaker="Cliente",
        text="Durante o fechamento o sistema travou e paramos a operação.",
    )
    analysis = Analysis(
        model="gemini-test",
        prompt_version="meetmind-transparent-v4",
        score_version="voc-deterministic-v1",
        transcript_hash="a" * 64,
    )

    validated = _validated_result_payload(result, [segment], analysis)

    assert validated["meeting_classification"]["meeting_type"] == "unknown"
    assert validated["meeting_classification"]["confidence"] == 0
    assert validated["relationship_health"]["status"] == "insufficient_evidence"
    assert validated["relationship_health"]["confidence"] == 0
    assert validated["action_items"][0]["action"] == "Validar logs"
    assert validated["traceability"]["discarded_reference_count"] == 3
    assert len(validated["uncertainties"]) == 3


def test_invalid_manufacturing_evidence_degrades_only_affected_fields():
    payload = valid_result()
    payload["manufacturing_brief"]["customer_requests"] = [
        {
            "request": "Trocar o servidor",
            "target": "infraestrutura",
            "confidence": 0.9,
            "evidence": [
                {
                    "segment_id": "invented.request",
                    "quote": "pedido que não existe",
                    "relevance": "Inválida",
                }
            ],
        }
    ]
    payload["manufacturing_brief"]["concern"]["evidence"] = [
        {
            "segment_id": "invented.concern",
            "quote": "preocupação que não existe",
            "relevance": "Inválida",
        }
    ]
    result = GeminiResult.model_validate(payload)
    segment = Segment(
        id="meeting.0",
        position=0,
        speaker="Cliente",
        text="Durante o fechamento o sistema travou e paramos a operação.",
    )
    analysis = Analysis(
        model="gemini-test",
        prompt_version="meetmind-manufacturing-v1",
        score_version="voc-deterministic-v1",
        transcript_hash="a" * 64,
    )

    validated = _validated_result_payload(result, [segment], analysis)

    assert validated["schema_version"] == "meetmind-manufacturing-v1"
    assert validated["manufacturing_brief"]["customer_requests"] == []
    assert validated["manufacturing_brief"]["concern"]["status"] == "unclear"
    assert validated["findings"][0]["evidence"][0]["segment_id"] == "meeting.0"
    assert validated["traceability"]["discarded_reference_count"] == 2


def test_duplicate_valid_references_are_deduplicated_without_being_discarded():
    payload = valid_result()
    payload["meeting_classification"]["evidence_segment_ids"] = [
        "meeting.0",
        "meeting.0",
    ]
    result = GeminiResult.model_validate(payload)
    segment = Segment(
        id="meeting.0",
        position=0,
        speaker="Cliente",
        text="Durante o fechamento o sistema travou e paramos a operação.",
    )
    analysis = Analysis(
        model="gemini-test",
        prompt_version="meetmind-transparent-v4",
        score_version="voc-deterministic-v1",
        transcript_hash="a" * 64,
    )

    validated = _validated_result_payload(result, [segment], analysis)

    assert validated["meeting_classification"]["evidence_segment_ids"] == [
        "meeting.0"
    ]
    assert validated["traceability"]["discarded_reference_count"] == 0


def test_non_literal_evidence_quote_is_rejected():
    payload = valid_result()
    payload["findings"][0]["evidence"][0]["quote"] = "frase inventada"
    result = GeminiResult.model_validate(payload)
    segment = Segment(id="meeting.0", position=0, text="O sistema travou.")
    analysis = Analysis(
        model="gemini-test",
        prompt_version="meetmind-transparent-v4",
        score_version="voc-deterministic-v1",
        transcript_hash="a" * 64,
    )
    with pytest.raises(InvalidModelOutput, match="evidence_quote_not_literal"):
        _validated_result_payload(result, [segment], analysis)


def test_token_estimate_is_deterministic():
    assert estimate_tokens("a" * 120_000) == 30_000


def test_provider_schema_is_compact_and_uses_only_supported_keywords():
    serialized = str(GEMINI_RESPONSE_JSON_SCHEMA)
    schema_keywords = set()

    def collect_keywords(value, parent_key=None):
        if isinstance(value, list):
            for item in value:
                collect_keywords(item)
            return
        if not isinstance(value, dict):
            return
        for key, item in value.items():
            if parent_key not in {"$defs", "properties"}:
                schema_keywords.add(key)
            collect_keywords(item, key)

    collect_keywords(GEMINI_RESPONSE_JSON_SCHEMA)
    assert "pattern" not in serialized
    assert "default" not in serialized
    assert not {
        "additionalProperties",
        "description",
    } & schema_keywords
    assert not {
        "minLength",
        "maxLength",
        "minItems",
        "maxItems",
        "minimum",
        "maximum",
    } & schema_keywords
    assert ["string", "null"] in [
        item.get("type")
        for item in GEMINI_RESPONSE_JSON_SCHEMA["$defs"]["AccountContext"][
            "properties"
        ].values()
        if isinstance(item, dict)
    ]
    assert GEMINI_RESPONSE_JSON_SCHEMA["type"] == "object"
    assert "executive_summary" in GEMINI_RESPONSE_JSON_SCHEMA["properties"]
    assert len(json.dumps(GEMINI_RESPONSE_JSON_SCHEMA, ensure_ascii=False)) < 8_000
    assert len(GEMINI_RESPONSE_SCHEMA_SHA256) == 64


def test_provider_output_normalizes_wrappers_lists_ids_and_extra_fields():
    payload = copy.deepcopy(valid_result())
    payload["unexpected"] = "ignored"
    payload["products_mentioned"] = "Protheus"
    payload["findings"][0]["finding_id"] = "achado-1"
    payload["findings"][0]["unexpected"] = "ignored"
    payload["findings"][0]["evidence"] = payload["findings"][0]["evidence"][0]
    payload["findings"][0]["evidence"]["segment_id"] = "[SEGMENT_ID=meeting.0]"
    payload["action_items"][0]["evidence_segment_ids"] = "[meeting.0]"
    encoded = f"```json\n{json.dumps(payload, ensure_ascii=False)}\n```"

    result = _parse_model_result(encoded)

    assert result.products_mentioned == ["Protheus"]
    assert result.findings[0].finding_id == "finding-1"
    assert result.findings[0].evidence[0].segment_id == "meeting.0"
    assert result.action_items[0].evidence_segment_ids == ["meeting.0"]


def test_provider_output_clips_long_text_before_strict_validation():
    payload = copy.deepcopy(valid_result())
    payload["executive_summary"]["what_happened"] = "palavra " * 100
    payload["findings"][0]["recommendation"] = "ação " * 100

    result = _parse_model_result(json.dumps(payload, ensure_ascii=False))

    assert len(result.executive_summary.what_happened) <= 400
    assert len(result.findings[0].recommendation) <= 300


def test_provider_output_normalizes_portuguese_enums_and_percentage():
    payload = copy.deepcopy(valid_result())
    payload["overall_sentiment"] = "negativo"
    payload["risk_level"] = "alto"
    payload["findings"][0]["category"] = "técnico"
    payload["findings"][0]["confidence"] = "95%"
    payload["findings"][0]["trigger"]["trigger_type"] = "problema operacional"
    payload["findings"][0]["trigger"]["local_metric_category"] = (
        "problemas operacionais"
    )
    payload["action_items"][0]["urgency"] = "imediata"
    payload["action_items"][0]["owner"] = "não informado"

    result = _parse_model_result(json.dumps(payload, ensure_ascii=False))

    assert result.overall_sentiment == "negative"
    assert result.risk_level == "high"
    assert result.findings[0].category == "technical"
    assert result.findings[0].confidence == 0.95
    assert result.findings[0].trigger.trigger_type == "operational_problem"
    assert result.action_items[0].urgency == "immediate"
    assert result.action_items[0].owner is None


def test_generation_repairs_invalid_nonempty_json_once(monkeypatch):
    responses = [
        SimpleNamespace(
            text="{}",
            usage_metadata=SimpleNamespace(
                prompt_token_count=10,
                candidates_token_count=2,
            ),
        ),
        SimpleNamespace(
            text=json.dumps(valid_result(), ensure_ascii=False),
            usage_metadata=SimpleNamespace(
                prompt_token_count=20,
                candidates_token_count=30,
            ),
        ),
    ]

    class FakeModels:
        def __init__(self):
            self.calls = 0

        def generate_content(self, **_kwargs):
            response = responses[self.calls]
            self.calls += 1
            return response

    class FakeClient:
        models = FakeModels()

        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr("app.services.gemini.genai.Client", FakeClient)

    result, input_tokens, output_tokens, _latency, repairs = _generate(
        "[SEGMENT_ID=meeting.0] O sistema travou.",
        "gemini-test",
    )

    assert result.findings[0].evidence[0].segment_id == "meeting.0"
    assert input_tokens == 30
    assert output_tokens == 32
    assert repairs == 1
    assert FakeClient.models.calls == 2


def test_generation_retries_one_transient_capacity_failure(monkeypatch):
    valid_response = SimpleNamespace(
        text=json.dumps(valid_result(), ensure_ascii=False),
        usage_metadata=SimpleNamespace(
            prompt_token_count=10,
            candidates_token_count=20,
        ),
    )

    class FakeModels:
        calls = 0

        def generate_content(self, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                raise ServerError(
                    503,
                    {"error": {"code": 503, "status": "UNAVAILABLE"}},
                    None,
                )
            return valid_response

    class FakeClient:
        models = FakeModels()

        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr("app.services.gemini.genai.Client", FakeClient)
    monkeypatch.setattr("app.services.gemini.time.sleep", lambda _seconds: None)

    result, _input, _output, _latency, repairs = _generate(
        "[SEGMENT_ID=meeting.0] O sistema travou.",
        "gemini-test",
    )

    assert result.findings[0].evidence[0].segment_id == "meeting.0"
    assert repairs == 0
    assert FakeClient.models.calls == 2


def test_generation_does_not_repair_an_empty_response(monkeypatch):
    class FakeModels:
        calls = 0

        def generate_content(self, **_kwargs):
            self.calls += 1
            return SimpleNamespace(
                text="",
                usage_metadata=SimpleNamespace(
                    prompt_token_count=10,
                    candidates_token_count=0,
                ),
            )

    class FakeClient:
        models = FakeModels()

        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr("app.services.gemini.genai.Client", FakeClient)

    with pytest.raises(InvalidModelOutput, match="empty_response"):
        _generate("[SEGMENT_ID=meeting.0] O sistema travou.", "gemini-test")

    assert FakeClient.models.calls == 1


def test_generation_stops_after_one_failed_repair(monkeypatch):
    class FakeModels:
        calls = 0

        def generate_content(self, **_kwargs):
            self.calls += 1
            return SimpleNamespace(
                text="{}",
                usage_metadata=SimpleNamespace(
                    prompt_token_count=10,
                    candidates_token_count=2,
                ),
            )

    class FakeClient:
        models = FakeModels()

        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr("app.services.gemini.genai.Client", FakeClient)

    with pytest.raises(InvalidModelOutput, match="repair_failed"):
        _generate("[SEGMENT_ID=meeting.0] O sistema travou.", "gemini-test")

    assert FakeClient.models.calls == 2
