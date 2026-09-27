from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SAFE_ID_PATTERN = r"^[A-Za-z0-9._-]{1,100}$"
SAFE_ID = re.compile(SAFE_ID_PATTERN)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class SegmentInput(StrictModel):
    id: str = Field(min_length=1, max_length=100, pattern=SAFE_ID_PATTERN)
    text: str = Field(min_length=1, max_length=20_000)
    speaker: str | None = Field(default=None, max_length=200)
    is_totvs: bool = False
    started_at_ms: int | None = Field(default=None, ge=0)
    ended_at_ms: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_times(self):
        if (
            self.started_at_ms is not None
            and self.ended_at_ms is not None
            and self.ended_at_ms < self.started_at_ms
        ):
            raise ValueError("ended_at_ms deve ser maior ou igual a started_at_ms")
        return self


class MetadataInput(StrictModel):
    source: Literal["manual", "import", "integration"] = "manual"
    department: str | None = Field(default=None, max_length=200)
    owner: str | None = Field(default=None, max_length=200)
    tags: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def validate_tags(self):
        if any(len(tag) > 200 for tag in self.tags):
            raise ValueError("Cada tag deve ter até 200 caracteres")
        return self


class MeetingInput(StrictModel):
    external_id: str = Field(min_length=1, max_length=100, pattern=SAFE_ID_PATTERN)
    title: str = Field(min_length=1, max_length=200)
    client_external_id: str | None = Field(default=None, max_length=100, pattern=SAFE_ID_PATTERN)
    client_name: str | None = Field(default=None, max_length=200)
    occurred_at: datetime | None = None
    language: Literal["pt-BR", "en-US", "es-ES"] = "pt-BR"
    approved_for_external_ai: bool = False
    transcript: str | None = Field(default=None, min_length=1, max_length=250_000)
    segments: list[SegmentInput] | None = Field(default=None, max_length=5_000)
    metadata: MetadataInput = Field(default_factory=MetadataInput)

    @model_validator(mode="after")
    def validate_transcript(self):
        if bool(self.transcript) == bool(self.segments):
            raise ValueError("Informe transcript ou segments, exclusivamente")
        total = len(self.transcript or "") + sum(len(item.text) for item in self.segments or [])
        if total < 1 or total > 250_000:
            raise ValueError("A transcrição total deve ter entre 1 e 250 mil caracteres")
        if self.client_external_id and not self.client_name:
            raise ValueError("client_name é obrigatório com client_external_id")
        ids = [item.id for item in self.segments or []]
        if len(ids) != len(set(ids)):
            raise ValueError("IDs de segmento devem ser únicos")
        return self


class ExecutiveSummary(StrictModel):
    what_happened: str = Field(
        min_length=1,
        max_length=400,
        description="Síntese factual e curta do que ocorreu na reunião.",
    )
    why_it_matters: str = Field(
        min_length=1,
        max_length=400,
        description="Impacto comprovado ou provável, sem transformar hipótese em fato.",
    )
    next_step: str = Field(
        min_length=1,
        max_length=300,
        description="Ação prioritária concreta, proporcional às evidências.",
    )


class EvidenceTrace(StrictModel):
    segment_id: str = Field(
        min_length=1,
        max_length=100,
        pattern=SAFE_ID_PATTERN,
        description="ID literal de um SEGMENT_ID recebido.",
    )
    quote: str = Field(
        min_length=3,
        max_length=350,
        description="Trecho literal e contínuo copiado do segmento.",
    )
    relevance: str = Field(
        min_length=1,
        max_length=240,
        description="Explicação curta de como o trecho sustenta a conclusão.",
    )


class TriggerTrace(StrictModel):
    trigger_type: Literal[
        "explicit_statement",
        "repeated_signal",
        "dissatisfaction",
        "operational_problem",
        "commercial_risk",
        "commitment",
        "objection",
        "opportunity",
        "uncertainty",
    ]
    label: str = Field(min_length=1, max_length=100)
    reason: str = Field(
        min_length=1,
        max_length=280,
        description="Por que o trigger foi acionado neste contexto.",
    )
    matched_terms: list[str] = Field(default_factory=list, max_length=5)
    local_metric_category: (
        Literal[
            "satisfaction",
            "dissatisfaction",
            "operational_problems",
            "commercial_risk",
        ]
        | None
    ) = None

    @model_validator(mode="after")
    def validate_matched_terms(self):
        if any(len(term) > 80 for term in self.matched_terms):
            raise ValueError("Cada termo de trigger deve ter até 80 caracteres")
        return self


class AnalysisFinding(StrictModel):
    finding_id: str = Field(pattern=r"^finding-[1-3]$")
    category: Literal[
        "financial",
        "technical",
        "operational",
        "relationship",
        "commercial",
        "adoption",
    ]
    title: str = Field(min_length=1, max_length=100)
    observation: str = Field(
        min_length=1,
        max_length=350,
        description="O que foi explicitamente observado na reunião.",
    )
    interpretation: str = Field(
        min_length=1,
        max_length=350,
        description="Leitura analítica separada da observação.",
    )
    business_impact: str = Field(min_length=1, max_length=300)
    recommendation: str = Field(min_length=1, max_length=300)
    confidence: float = Field(ge=0, le=1)
    trigger: TriggerTrace
    evidence: list[EvidenceTrace] = Field(min_length=1, max_length=3)


class ActionItem(StrictModel):
    action: str = Field(min_length=1, max_length=180)
    rationale: str = Field(min_length=1, max_length=240)
    owner: str | None = Field(default=None, max_length=100)
    urgency: Literal["immediate", "next_7_days", "next_30_days", "monitor"]
    due_at: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    evidence_segment_ids: list[str] = Field(min_length=1, max_length=5)

    @model_validator(mode="after")
    def validate_evidence_ids(self):
        if any(not SAFE_ID.fullmatch(segment_id) for segment_id in self.evidence_segment_ids):
            raise ValueError("ID de evidência inválido")
        return self


class MeetingClassification(StrictModel):
    sector: str = Field(min_length=1, max_length=100)
    subsector: str | None = Field(default=None, max_length=100)
    sector_source: Literal["metadata", "cnae", "unit", "transcript", "fallback", "conflict"]
    meeting_type: Literal[
        "support_incident",
        "implementation",
        "relationship_success",
        "commercial_expansion",
        "adoption_training",
        "renewal_contract",
        "discovery",
        "mixed",
        "unknown",
    ]
    confidence: float = Field(ge=0, le=1)
    evidence_segment_ids: list[str] = Field(default_factory=list, max_length=5)

    @model_validator(mode="after")
    def validate_classification_evidence(self):
        if self.meeting_type != "unknown" and not self.evidence_segment_ids:
            raise ValueError("A classificação da reunião exige evidência")
        if any(not SAFE_ID.fullmatch(item) for item in self.evidence_segment_ids):
            raise ValueError("ID de evidência da classificação inválido")
        return self


class AccountContext(StrictModel):
    sector: str = Field(min_length=1, max_length=100)
    subsector: str | None = Field(default=None, max_length=100)
    revenue_band: str | None = Field(default=None, max_length=200)
    completeness: float = Field(ge=0, le=1)
    missing_fields: list[str] = Field(default_factory=list, max_length=8)
    limitations: list[str] = Field(default_factory=list, max_length=5)


class RelationshipHealth(StrictModel):
    status: Literal["healthy", "attention", "at_risk", "insufficient_evidence"]
    rationale: str = Field(min_length=1, max_length=360)
    confidence: float = Field(ge=0, le=1)
    evidence_segment_ids: list[str] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def validate_health_evidence(self):
        if self.status != "insufficient_evidence" and not self.evidence_segment_ids:
            raise ValueError("A saúde da relação exige evidência da reunião")
        if any(not SAFE_ID.fullmatch(item) for item in self.evidence_segment_ids):
            raise ValueError("ID de evidência da relação inválido")
        return self


class RetentionSignal(StrictModel):
    signal_type: Literal["positive", "attention"]
    label: str = Field(min_length=1, max_length=100)
    description: str = Field(min_length=1, max_length=300)
    confidence: float = Field(ge=0, le=1)
    trigger: TriggerTrace
    evidence: list[EvidenceTrace] = Field(min_length=1, max_length=3)


class ContextInfluence(StrictModel):
    factor: Literal["sector", "subsector", "revenue_band"]
    value: str = Field(min_length=1, max_length=200)
    influence: str = Field(min_length=1, max_length=300)
    limitation: str = Field(min_length=1, max_length=240)


class ManufacturingDecisionBase(StrictModel):
    rationale: str = Field(min_length=1, max_length=280)
    confidence: float = Field(ge=0, le=1)
    evidence: list[EvidenceTrace] = Field(default_factory=list, max_length=3)


class ManufacturingObjective(ManufacturingDecisionBase):
    status: Literal["achieved", "partial", "not_achieved", "unclear"]


class ManufacturingFollowUp(ManufacturingDecisionBase):
    status: Literal["scheduled", "in_person", "proposed", "none", "unclear"]


class ManufacturingConflict(ManufacturingDecisionBase):
    status: Literal["none_detected", "tension", "argument", "unclear"]


class ManufacturingConcern(ManufacturingDecisionBase):
    status: Literal["none_detected", "attention", "high", "unclear"]


class AutopsyCustomization(ManufacturingDecisionBase):
    status: Literal["present", "absent", "unclear"]


class AutopsyGuidedReview(StrictModel):
    objective: ManufacturingObjective
    follow_up: ManufacturingFollowUp
    conflict: ManufacturingConflict
    customization: AutopsyCustomization
    uncertainties: list[str] = Field(default_factory=list, max_length=4)

    @model_validator(mode="after")
    def validate_guided_review(self):
        for field_name in ("objective", "follow_up", "conflict", "customization"):
            decision = getattr(self, field_name)
            affirmative = decision.status not in {
                "none",
                "none_detected",
                "absent",
                "unclear",
            }
            if affirmative and not decision.evidence:
                raise ValueError(f"{field_name} afirmativo exige evidência")
        if any(len(item) > 240 for item in self.uncertainties):
            raise ValueError("Incertezas devem ter até 240 caracteres")
        return self


class ManufacturingCustomerRequest(StrictModel):
    request: str = Field(min_length=1, max_length=280)
    target: str | None = Field(default=None, max_length=160)
    confidence: float = Field(ge=0, le=1)
    evidence: list[EvidenceTrace] = Field(min_length=1, max_length=3)


class ManufacturingBrief(StrictModel):
    what_happened: str = Field(min_length=1, max_length=400)
    what_happened_evidence: list[EvidenceTrace] = Field(min_length=1, max_length=3)
    customer_requests: list[ManufacturingCustomerRequest] = Field(
        default_factory=list, max_length=3
    )
    objective: ManufacturingObjective
    follow_up: ManufacturingFollowUp
    conflict: ManufacturingConflict
    concern: ManufacturingConcern
    suggested_actions: list[ActionItem] = Field(default_factory=list, max_length=3)
    uncertainties: list[str] = Field(default_factory=list, max_length=3)

    @model_validator(mode="after")
    def validate_manufacturing_statuses(self):
        for field_name in ("objective", "follow_up", "conflict", "concern"):
            decision = getattr(self, field_name)
            affirmative = decision.status not in {"none", "none_detected", "unclear"}
            if affirmative and not decision.evidence:
                raise ValueError(f"{field_name} afirmativo exige evidência")
        if any(len(item) > 240 for item in self.uncertainties):
            raise ValueError("Incertezas devem ter até 240 caracteres")
        return self


class GeminiResult(StrictModel):
    manufacturing_brief: ManufacturingBrief
    executive_summary: ExecutiveSummary
    overall_sentiment: Literal["positive", "neutral", "negative", "mixed"]
    risk_level: Literal["low", "medium", "high", "critical"]
    meeting_classification: MeetingClassification
    account_context: AccountContext
    relationship_health: RelationshipHealth
    retention_signals: list[RetentionSignal] = Field(default_factory=list, max_length=6)
    context_application: list[ContextInfluence] = Field(default_factory=list, max_length=4)
    contextual_priority: Literal["low", "medium", "high"]
    products_mentioned: list[str] = Field(default_factory=list, max_length=8)
    findings: list[AnalysisFinding] = Field(min_length=1, max_length=3)
    action_items: list[ActionItem] = Field(min_length=1, max_length=3)
    uncertainties: list[str] = Field(default_factory=list, max_length=3)

    @model_validator(mode="after")
    def validate_short_lists(self):
        if any(len(item) > 240 for item in self.products_mentioned + self.uncertainties):
            raise ValueError("Produtos e incertezas devem ter até 240 caracteres")
        finding_ids = [finding.finding_id for finding in self.findings]
        if len(finding_ids) != len(set(finding_ids)):
            raise ValueError("finding_id deve ser único")
        return self


AnalysisMode = Literal[
    "voc_complaint",
    "meeting_outcome",
    "product_operations",
    "relationship_risk",
]
MeetingType = Literal[
    "idea_alignment",
    "solution_presentation",
    "technical_diagnosis",
    "questions_clarifications",
    "contract_negotiation",
    "problem_resolution",
    "unknown",
]


class AnalysisRequestInput(StrictModel):
    mode_strategy: Literal["auto", "manual"] = "auto"
    requested_modes: list[AnalysisMode] = Field(default_factory=list, max_length=2)

    @model_validator(mode="after")
    def validate_mode_selection(self):
        if len(self.requested_modes) != len(set(self.requested_modes)):
            raise ValueError("requested_modes não pode conter duplicatas")
        if self.mode_strategy == "auto" and self.requested_modes:
            raise ValueError("requested_modes deve estar vazio no modo automático")
        if self.mode_strategy == "manual" and not self.requested_modes:
            raise ValueError("Selecione ao menos um modo na estratégia manual")
        return self


class RoutingResult(StrictModel):
    meeting_type: MeetingType
    primary_mode: AnalysisMode
    secondary_modes: list[AnalysisMode] = Field(default_factory=list, max_length=1)
    confidence: float = Field(ge=0, le=1)
    reason: str = Field(min_length=1, max_length=300)
    evidence: list[EvidenceTrace] = Field(min_length=1, max_length=2)

    @model_validator(mode="after")
    def validate_routing_modes(self):
        if self.primary_mode in self.secondary_modes:
            raise ValueError("O modo primário não pode ser repetido como secundário")
        return self


class ComplaintItem(StrictModel):
    complaint_id: str = Field(pattern=r"^complaint-[1-3]$")
    category: Literal[
        "technical",
        "operational",
        "licensing",
        "contract",
        "customization",
        "service",
        "infrastructure",
        "other",
    ]
    target: str = Field(min_length=1, max_length=160)
    observation: str = Field(min_length=1, max_length=350)
    impact: str = Field(min_length=1, max_length=300)
    desired_outcome: str | None = Field(default=None, max_length=300)
    strength: Literal["weak", "moderate", "strong"]
    confidence: float = Field(ge=0, le=1)
    evidence: list[EvidenceTrace] = Field(min_length=1, max_length=3)


class VocComplaintResult(StrictModel):
    mode: Literal["voc_complaint"]
    summary: str = Field(min_length=1, max_length=400)
    complaints: list[ComplaintItem] = Field(default_factory=list, max_length=3)
    evidence: list[EvidenceTrace] = Field(min_length=1, max_length=3)
    uncertainties: list[str] = Field(default_factory=list, max_length=3)


class MeetingCommitment(StrictModel):
    action: str = Field(min_length=1, max_length=220)
    owner: str | None = Field(default=None, max_length=120)
    due_date_reference: str | None = Field(default=None, max_length=120)
    evidence: list[EvidenceTrace] = Field(min_length=1, max_length=2)


class MeetingOutcomeResult(StrictModel):
    mode: Literal["meeting_outcome"]
    meeting_type: MeetingType
    objective_status: Literal["resolved", "partial", "unresolved", "unclear"]
    follow_up_status: Literal[
        "explicit_scheduled",
        "explicit_in_person",
        "proposed",
        "none",
        "unclear",
    ]
    follow_up_purpose: str | None = Field(default=None, max_length=260)
    interpretation: str = Field(min_length=1, max_length=400)
    confidence: float = Field(ge=0, le=1)
    commitments: list[MeetingCommitment] = Field(default_factory=list, max_length=3)
    evidence: list[EvidenceTrace] = Field(min_length=1, max_length=3)
    uncertainties: list[str] = Field(default_factory=list, max_length=3)


class ProductMention(StrictModel):
    canonical_id: str | None = Field(default=None, max_length=100)
    canonical_name: str | None = Field(default=None, max_length=180)
    original_term: str = Field(min_length=1, max_length=120)
    ambiguity: str | None = Field(default=None, max_length=300)
    confidence: float = Field(ge=0, le=1)
    evidence: list[EvidenceTrace] = Field(min_length=1, max_length=2)


class TechnicalHypothesis(StrictModel):
    statement: str = Field(min_length=1, max_length=300)
    missing_validation: str = Field(min_length=1, max_length=260)
    confidence: float = Field(ge=0, le=1)
    evidence: list[EvidenceTrace] = Field(min_length=1, max_length=2)


class ProductOperationsResult(StrictModel):
    mode: Literal["product_operations"]
    summary: str = Field(min_length=1, max_length=400)
    product_mentions: list[ProductMention] = Field(default_factory=list, max_length=5)
    symptoms: list[str] = Field(default_factory=list, max_length=3)
    impacted_processes: list[str] = Field(default_factory=list, max_length=3)
    hypotheses: list[TechnicalHypothesis] = Field(default_factory=list, max_length=3)
    next_diagnostic_step: str = Field(min_length=1, max_length=300)
    evidence: list[EvidenceTrace] = Field(min_length=1, max_length=3)
    uncertainties: list[str] = Field(default_factory=list, max_length=3)


class RelationshipSignal(StrictModel):
    statement: str = Field(min_length=1, max_length=260)
    confidence: float = Field(ge=0, le=1)
    evidence: list[EvidenceTrace] = Field(min_length=1, max_length=2)


class RelationshipRiskResult(StrictModel):
    mode: Literal["relationship_risk"]
    risk_level: Literal["low", "medium", "high", "critical"]
    churn_signal_strength: Literal["absent", "weak", "moderate", "strong"]
    rationale: str = Field(min_length=1, max_length=400)
    confidence: float = Field(ge=0, le=1)
    positive_signals: list[RelationshipSignal] = Field(default_factory=list, max_length=3)
    negative_signals: list[RelationshipSignal] = Field(default_factory=list, max_length=3)
    evidence: list[EvidenceTrace] = Field(min_length=1, max_length=3)
    factors_reducing_confidence: list[str] = Field(default_factory=list, max_length=3)


ModeResult = Annotated[
    VocComplaintResult | MeetingOutcomeResult | ProductOperationsResult | RelationshipRiskResult,
    Field(discriminator="mode"),
]


class ModeSynthesisResult(StrictModel):
    executive_summary: ExecutiveSummary
    uncertainties: list[str] = Field(default_factory=list, max_length=3)
    evidence_segment_ids: list[str] = Field(default_factory=list, max_length=6)

    @model_validator(mode="after")
    def validate_synthesis_evidence_ids(self):
        if any(not SAFE_ID.fullmatch(segment_id) for segment_id in self.evidence_segment_ids):
            raise ValueError("ID de evidência inválido")
        if len(self.evidence_segment_ids) != len(set(self.evidence_segment_ids)):
            raise ValueError("evidence_segment_ids não pode conter duplicatas")
        return self
