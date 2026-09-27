from __future__ import annotations

from typing import Any

from app.schemas import GeminiResult

RESULT_FIELDS_USED_BY_PRESENTATION = frozenset(
    {
        "manufacturing_brief",
        "executive_summary",
        "overall_sentiment",
        "risk_level",
        "meeting_classification",
        "account_context",
        "relationship_health",
        "retention_signals",
        "context_application",
        "contextual_priority",
        "products_mentioned",
        "findings",
        "action_items",
        "uncertainties",
    }
)

HEALTH_PRESENTATION = {
    "healthy": {
        "label": "Satisfação favorável",
        "short_label": "Favorável",
        "tone": "positive",
        "guidance": "Preserve os acordos e mantenha o acompanhamento combinado.",
    },
    "attention": {
        "label": "Satisfação requer atenção",
        "short_label": "Atenção",
        "tone": "attention",
        "guidance": (
            "Trate o ponto principal e confirme com o cliente se a ação resolveu "
            "a necessidade."
        ),
    },
    "at_risk": {
        "label": "Satisfação em risco",
        "short_label": "Em risco",
        "tone": "risk",
        "guidance": "Priorize contato, responsável e prazo para reduzir o impacto observado.",
    },
    "insufficient_evidence": {
        "label": "Satisfação ainda inconclusiva",
        "short_label": "Inconclusiva",
        "tone": "unknown",
        "guidance": "Confirme a percepção do cliente antes de tomar uma decisão de relacionamento.",
    },
}

SENTIMENT_LABELS = {
    "positive": "Positivo",
    "neutral": "Neutro",
    "negative": "Negativo",
    "mixed": "Misto",
}

URGENCY_LABELS = {
    "immediate": "Agora",
    "next_7_days": "Nos próximos 7 dias",
    "next_30_days": "Nos próximos 30 dias",
    "monitor": "Acompanhar",
}

MEETING_TYPE_LABELS = {
    "support_incident": "Suporte ou incidente",
    "implementation": "Implantação",
    "relationship_success": "Relacionamento e sucesso",
    "commercial_expansion": "Expansão comercial",
    "adoption_training": "Adoção e treinamento",
    "renewal_contract": "Renovação ou contrato",
    "discovery": "Descoberta",
    "mixed": "Mista",
    "unknown": "Não definida",
    "idea_alignment": "Alinhamento de ideias",
    "solution_presentation": "Apresentação de solução",
    "technical_diagnosis": "Diagnóstico técnico",
    "questions_clarifications": "Dúvidas e esclarecimentos",
    "contract_negotiation": "Negociação contratual",
    "problem_resolution": "Resolução de problema",
}


def uncovered_result_fields() -> set[str]:
    """Fail safely when the AI contract grows without a presentation decision."""
    return set(GeminiResult.model_fields) - RESULT_FIELDS_USED_BY_PRESENTATION


def _objects(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _text(value: Any, fallback: str = "") -> str:
    normalized = str(value or "").strip()
    return normalized or fallback


def _confidence_percentage(value: Any) -> int:
    try:
        if isinstance(value, str) and value.strip().endswith("%"):
            return max(0, min(100, round(float(value.strip()[:-1]))))
        return max(0, min(100, round(float(value or 0) * 100)))
    except (TypeError, ValueError):
        return 0


def build_friendly_analysis(card: dict[str, Any]) -> dict[str, Any]:
    summary = card.get("summary") if isinstance(card.get("summary"), dict) else {}
    health = (
        card.get("relationship_health")
        if isinstance(card.get("relationship_health"), dict)
        else {}
    )
    health_status = _text(health.get("status"), "insufficient_evidence")
    health_view = HEALTH_PRESENTATION.get(
        health_status, HEALTH_PRESENTATION["insufficient_evidence"]
    )
    actions = []
    for item in _objects(card.get("action_items"))[:3]:
        action = _text(item.get("action"))
        if not action:
            continue
        urgency = _text(item.get("urgency"), "monitor")
        actions.append(
            {
                "action": action,
                "rationale": _text(
                    item.get("rationale"),
                    "A justificativa detalhada não foi informada.",
                ),
                "urgency": urgency,
                "urgency_label": URGENCY_LABELS.get(urgency, "Acompanhar"),
            }
        )
    if not actions and _text(summary.get("next_step")):
        actions.append(
            {
                "action": _text(summary.get("next_step")),
                "rationale": "Próximo passo priorizado no resumo da análise.",
                "urgency": "monitor",
                "urgency_label": "Acompanhar",
            }
        )

    reasons = []
    for signal in _objects(card.get("retention_signals")):
        trigger = signal.get("trigger") if isinstance(signal.get("trigger"), dict) else {}
        reasons.append(
            {
                "kind": "favorável" if signal.get("signal_type") == "positive" else "atenção",
                "title": _text(signal.get("label"), "Sinal observado"),
                "explanation": _text(
                    signal.get("description") or trigger.get("reason"),
                    "A interpretação foi sustentada pelas falas associadas.",
                ),
                "evidence": _objects(signal.get("evidence"))[:2],
            }
        )
    if not reasons:
        for finding in _objects(card.get("findings"))[:3]:
            reasons.append(
                {
                    "kind": "análise",
                    "title": _text(finding.get("title"), "Ponto observado"),
                    "explanation": _text(
                        finding.get("interpretation") or finding.get("observation"),
                        "A análise não forneceu uma explicação adicional.",
                    ),
                    "evidence": _objects(finding.get("evidence"))[:2],
                }
            )

    classification = card.get("meeting_type", "unknown")
    return {
        "analysis_id": card["analysis"].id,
        "meeting_id": card["meeting"].id,
        "meeting_external_id": card["meeting"].external_id,
        "meeting_title": card["meeting"].title,
        "client_name": card.get("client_name") or "Cliente não identificado",
        "occurred_at": card["meeting"].occurred_at,
        "sector": card.get("sector") or "Não identificado",
        "meeting_type": classification,
        "meeting_type_label": MEETING_TYPE_LABELS.get(classification, "Não definida"),
        "sentiment_label": SENTIMENT_LABELS.get(card.get("sentiment"), "Não definido"),
        "health_status": health_status,
        "health": health_view,
        "health_rationale": _text(
            health.get("rationale") or summary.get("why_it_matters"),
            "Ainda não há evidência suficiente para explicar a satisfação.",
        ),
        "confidence": _confidence_percentage(health.get("confidence")),
        "what_happened": _text(
            summary.get("what_happened"), "A análise não forneceu um resumo factual."
        ),
        "why_it_matters": _text(
            summary.get("why_it_matters"), "O impacto ainda precisa ser confirmado."
        ),
        "recommended_next_step": _text(summary.get("next_step"), health_view["guidance"]),
        "actions": actions,
        "reasons": reasons[:3],
        "uncertainties": [
            _text(item)
            for item in (card.get("uncertainties") or [])
            if _text(item)
        ][:3],
        "needs_review": bool(card.get("needs_review")),
        "tracked_actions": [
            {
                "id": item.id,
                "title": item.title,
                "rationale": item.rationale,
                "status": item.status,
                "owner_id": item.owner_id,
                "due_at": item.due_at,
                "version": item.version,
            }
            for item in (card.get("tracked_actions") or [])
        ],
    }
