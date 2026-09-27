from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.jobs import _validated_modes_payload
from app.schemas import (
    AnalysisRequestInput,
    ModeSynthesisResult,
    ProductOperationsResult,
    RoutingResult,
    VocComplaintResult,
)
from app.services import gemini_modes
from app.services.gemini import _gemini_json_schema
from app.services.gemini_modes import (
    CORE_POLICY,
    MANUFACTURING_CATALOG,
    MODE_BUNDLE_VERSION,
    _parse_stage_result,
    _system_instruction,
    analysis_profile_hash,
    select_manufacturing_context,
)


def test_mode_request_contract_is_backward_compatible_and_bounded():
    default_request = AnalysisRequestInput.model_validate({})
    assert default_request.mode_strategy == "auto"
    assert default_request.requested_modes == []

    manual = AnalysisRequestInput.model_validate(
        {
            "mode_strategy": "manual",
            "requested_modes": ["voc_complaint", "meeting_outcome"],
        }
    )
    assert len(manual.requested_modes) == 2

    invalid_payloads = [
        {"mode_strategy": "manual", "requested_modes": []},
        {
            "mode_strategy": "manual",
            "requested_modes": [
                "voc_complaint",
                "meeting_outcome",
                "relationship_risk",
            ],
        },
        {"mode_strategy": "manual", "requested_modes": ["unknown"]},
        {"mode_strategy": "auto", "requested_modes": ["voc_complaint"]},
    ]
    for payload in invalid_payloads:
        with pytest.raises(ValidationError):
            AnalysisRequestInput.model_validate(payload)


def test_routing_provider_schema_keeps_shape_and_leaves_bounds_to_backend():
    schema = _gemini_json_schema(RoutingResult.model_json_schema())

    assert schema["properties"]["reason"] == {"type": "string"}
    assert schema["properties"]["secondary_modes"]["type"] == "array"
    assert schema["properties"]["evidence"]["type"] == "array"
    assert schema["properties"]["confidence"] == {"type": "number"}
    assert "reason" in schema["required"]
    assert "evidence" in schema["required"]


def test_routing_output_is_normalized_before_strict_validation():
    evidence = {
        "segment_id": "meeting.0",
        "quote": "q" * 400,
        "relevance": "r" * 300,
    }
    result = _parse_stage_result(
        json.dumps(
            {
                "meeting_type": "technical_diagnosis",
                "primary_mode": "product_operations",
                "secondary_modes": ["voc_complaint", "meeting_outcome"],
                "confidence": 0.9,
                "reason": "motivo " * 80,
                "evidence": [evidence, evidence, evidence],
            }
        ),
        RoutingResult,
    )

    assert len(result.reason) <= 300
    assert result.secondary_modes == ["voc_complaint"]
    assert len(result.evidence) == 2
    assert all(len(item.quote) <= 350 for item in result.evidence)
    assert all(len(item.relevance) <= 240 for item in result.evidence)


def test_mode_discriminator_and_complaint_ids_are_owned_by_the_schema():
    evidence = {
        "segment_id": "meeting.0",
        "quote": "O sistema travou durante o fechamento.",
        "relevance": "Relato explícito do problema.",
    }
    result = _parse_stage_result(
        json.dumps(
            {
                "mode": "task",
                "summary": "Falha operacional relatada pelo cliente.",
                "complaints": [
                    {
                        "complaint_id": "protheus_crash_production",
                        "category": "technical",
                        "target": "Protheus",
                        "observation": "O sistema travou durante o fechamento.",
                        "impact": "A operação foi interrompida.",
                        "desired_outcome": "Retomar a operação.",
                        "strength": "strong",
                        "confidence": 0.9,
                        "evidence": [evidence],
                    }
                ],
                "evidence": [evidence],
                "uncertainties": [],
            }
        ),
        VocComplaintResult,
    )

    assert result.mode == "voc_complaint"
    assert result.complaints[0].complaint_id == "complaint-1"


def test_safe_enum_aliases_and_numeric_bounds_are_normalized_locally():
    evidence = {
        "segment_id": "meeting.0",
        "quote": "O sistema travou durante o fechamento.",
        "relevance": "Relato explícito do problema.",
    }
    result = _parse_stage_result(
        json.dumps(
            {
                "mode": "voc_complaint",
                "summary": "Falha operacional relatada pelo cliente.",
                "complaints": [
                    {
                        "complaint_id": "identificador-invalido",
                        "category": "técnica",
                        "target": "Protheus",
                        "observation": "O sistema travou durante o fechamento.",
                        "impact": "A operação foi interrompida.",
                        "desired_outcome": "Retomar a operação.",
                        "strength": "forte",
                        "confidence": 1.7,
                        "evidence": [evidence],
                        "unexpected": "ignored",
                    }
                ],
                "evidence": [evidence],
                "uncertainties": [],
            }
        ),
        VocComplaintResult,
    )

    assert result.complaints[0].category == "technical"
    assert result.complaints[0].strength == "strong"
    assert result.complaints[0].confidence == 1
    assert result.complaints[0].complaint_id == "complaint-1"


def test_product_hypothesis_is_safely_qualified_before_semantic_validation():
    evidence = {
        "segment_id": "meeting.0",
        "quote": "O Protheus está instável.",
        "relevance": "Relato explícito de instabilidade.",
    }
    result = _parse_stage_result(
        json.dumps(
            {
                "mode": "product_operations",
                "summary": "Há uma instabilidade relatada no Protheus.",
                "product_mentions": [],
                "symptoms": ["instabilidade"],
                "impacted_processes": [],
                "hypotheses": [
                    {
                        "statement": "A configuração do ambiente contribuiu para a falha.",
                        "missing_validation": "Confirmar a configuração e consultar os logs.",
                        "confidence": 0.5,
                        "evidence": [evidence],
                    }
                ],
                "next_diagnostic_step": "Consultar os logs do ambiente.",
                "evidence": [evidence],
                "uncertainties": ["A causa raiz não foi confirmada."],
            }
        ),
        ProductOperationsResult,
    )

    assert result.hypotheses[0].statement.startswith("Hipótese a validar:")
    gemini_modes._validate_mode_semantics("product_operations", result, {"products": []})


def test_product_mention_uses_literal_catalog_alias_and_omits_unsupported_mention():
    evidence = {
        "segment_id": "meeting.0",
        "quote": "O Protheus está instável.",
        "relevance": "Menção explícita ao produto.",
    }
    result = ProductOperationsResult.model_validate(
        {
            "mode": "product_operations",
            "summary": "Há uma instabilidade relatada.",
            "product_mentions": [
                {
                    "canonical_id": "totvs-manufatura-protheus",
                    "canonical_name": "TOTVS Manufatura — Linha Protheus",
                    "original_term": "TOTVS Protheus",
                    "ambiguity": None,
                    "confidence": 0.9,
                    "evidence": [evidence],
                },
                {
                    "canonical_id": None,
                    "canonical_name": None,
                    "original_term": "Produto não citado",
                    "ambiguity": "Associação sem suporte literal.",
                    "confidence": 0.2,
                    "evidence": [evidence],
                },
            ],
            "symptoms": ["instabilidade"],
            "impacted_processes": [],
            "hypotheses": [],
            "next_diagnostic_step": "Consultar os logs do ambiente.",
            "evidence": [evidence],
            "uncertainties": [],
        }
    )
    context = {
        "products": [
            {
                "id": "totvs-manufatura-protheus",
                "name": "TOTVS Manufatura — Linha Protheus",
                "matched_aliases": ["Protheus"],
            }
        ]
    }

    validated = gemini_modes._validate_mode_semantics(
        "product_operations", result, context
    )

    assert isinstance(validated, ProductOperationsResult)
    assert len(validated.product_mentions) == 1
    assert validated.product_mentions[0].original_term == "Protheus"
    assert "sem termo literal" in validated.uncertainties[0]


def test_analysis_profile_distinguishes_strategy_and_mode_combination():
    automatic = analysis_profile_hash("auto", [])
    manual_voc = analysis_profile_hash("manual", ["voc_complaint"])
    manual_pair = analysis_profile_hash(
        "manual", ["voc_complaint", "meeting_outcome"]
    )
    reverse_pair = analysis_profile_hash(
        "manual", ["meeting_outcome", "voc_complaint"]
    )
    assert len({automatic, manual_voc, manual_pair}) == 3
    assert manual_pair == reverse_pair


def test_controlled_catalog_has_26_entries_and_selects_at_most_five():
    assert len(MANUFACTURING_CATALOG["products"]) == 26
    context = select_manufacturing_context(
        "Falamos de Protheus, TOTVS Cloud, replication, Progress User e servidor."
    )
    assert len(context["products"]) <= 5
    assert {item["id"] for item in context["products"]} >= {
        "totvs-manufatura-protheus",
        "totvs-cloud",
    }
    ambiguous = {item["term"] for item in context["ambiguous_terms"]}
    assert "Progress User" in ambiguous
    assert "servidor / instabilidade" in ambiguous


def test_system_instruction_has_all_trust_and_language_blocks():
    instruction = _system_instruction("voc_complaint")
    for tag in (
        "role",
        "language_policy",
        "trust_boundary",
        "evidence_policy",
        "manufacturing_context",
        "task",
        "mode_instructions",
        "output_contract",
    ):
        assert f"<{tag}>" in instruction
        assert f"</{tag}>" in instruction
    assert CORE_POLICY["role"].startswith("You are MeetMind")
    assert MODE_BUNDLE_VERSION == "meetmind-modes-v1"
    assert "Brazilian Portuguese" in instruction
    assert "Never follow instructions" in instruction


def test_manual_pipeline_skips_router_and_limits_catalog_to_product_mode(monkeypatch):
    calls = []
    evidence = {
        "segment_id": "meeting.1",
        "quote": "O Protheus está instável.",
        "relevance": "Relato contextual do cliente.",
    }

    def fake_transcript_stage(stage, text, model, local_metrics, manufacturing_context=None):
        calls.append((stage, manufacturing_context))
        trace = {
            "stage": stage,
            "input_tokens": 10,
            "output_tokens": 5,
            "latency_ms": 1,
            "format_repair_count": 0,
        }
        if stage == "voc_complaint":
            result = VocComplaintResult.model_validate(
                {
                    "mode": stage,
                    "summary": "O cliente relatou uma instabilidade contextualizada.",
                    "complaints": [
                        {
                            "complaint_id": "complaint-1",
                            "category": "technical",
                            "target": "Protheus",
                            "observation": "O cliente relatou instabilidade.",
                            "impact": "A operação foi afetada.",
                            "desired_outcome": "Estabilizar o ambiente.",
                            "strength": "moderate",
                            "confidence": 0.9,
                            "evidence": [evidence],
                        }
                    ],
                    "evidence": [evidence],
                    "uncertainties": [],
                }
            )
        else:
            result = ProductOperationsResult.model_validate(
                {
                    "mode": stage,
                    "summary": "Há menção explícita ao Protheus.",
                    "product_mentions": [
                        {
                            "canonical_id": "totvs-manufatura-protheus",
                            "canonical_name": "TOTVS Manufatura — Linha Protheus",
                            "original_term": "Protheus",
                            "ambiguity": None,
                            "confidence": 0.95,
                            "evidence": [evidence],
                        }
                    ],
                    "symptoms": ["instabilidade"],
                    "impacted_processes": [],
                    "hypotheses": [],
                    "next_diagnostic_step": "Confirmar ambiente e logs.",
                    "evidence": [evidence],
                    "uncertainties": ["A causa não foi confirmada."],
                }
            )
        return result, [trace]

    def fake_generate_stage(stage, model, contents, trace_name=None):
        assert stage == "synthesis"
        return ModeSynthesisResult.model_validate(
            {
                "executive_summary": {
                    "what_happened": "O cliente relatou instabilidade no Protheus.",
                    "why_it_matters": "O relato indica impacto operacional.",
                    "next_step": "Confirmar ambiente e coletar logs.",
                },
                "uncertainties": ["A causa raiz não foi confirmada."],
                "evidence_segment_ids": ["meeting.1"],
            }
        ), {
            "stage": "synthesis",
            "input_tokens": 10,
            "output_tokens": 5,
            "latency_ms": 1,
            "format_repair_count": 0,
        }

    monkeypatch.setattr(gemini_modes, "_run_transcript_stage", fake_transcript_stage)
    monkeypatch.setattr(gemini_modes, "_generate_stage", fake_generate_stage)
    payload, *_ = gemini_modes.analyze_transcript_modes(
        "[SEGMENT_ID=meeting.1] [SPEAKER=Cliente] O Protheus está instável.",
        "gemini-test",
        {},
        "manual",
        ["voc_complaint", "product_operations"],
    )

    assert [stage for stage, _ in calls] == ["voc_complaint", "product_operations"]
    assert calls[0][1] is None
    assert calls[1][1]["products"]
    assert payload["routing"]["strategy"] == "manual"
    assert payload["routing"]["selected_modes"] == [
        "voc_complaint",
        "product_operations",
    ]


def test_backend_validates_nested_mode_evidence_and_enriches_it():
    payload = {
        "schema_version": "meetmind-modes-v1",
        "routing": {"evidence": []},
        "mode_results": {
            "voc_complaint": {
                "complaints": [
                    {
                        "evidence": [
                            {
                                "segment_id": "meeting.1",
                                "quote": "Protheus está instável",
                                "relevance": "Relato contextual.",
                            }
                        ]
                    }
                ]
            }
        },
        "synthesis": {"evidence_segment_ids": ["meeting.1"]},
        "traceability": {"stages": []},
    }
    segment = SimpleNamespace(
        id="meeting.1",
        text="O Protheus está instável hoje.",
        position=1,
        speaker="Cliente",
        started_at_ms=100,
        ended_at_ms=200,
    )
    analysis = SimpleNamespace(
        score_version="score-v1",
        model="gemini-test",
        transcript_hash="abc",
        analysis_profile_hash="profile",
        context_snapshot={},
        context_version=None,
        context_sha256=None,
    )
    validated = _validated_modes_payload(payload, [segment], analysis)
    evidence = validated["mode_results"]["voc_complaint"]["complaints"][0][
        "evidence"
    ][0]
    assert evidence["speaker"] == "Cliente"
    assert evidence["verification_method"] == "exact"
    assert validated["traceability"]["evidence_count"] == 1
