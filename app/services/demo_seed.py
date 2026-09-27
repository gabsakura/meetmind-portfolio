from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta

from flask import current_app

from app.extensions import db
from app.models import (
    ActionEvent,
    ActionItem,
    ActionStatus,
    AIExecution,
    Analysis,
    AnalysisAutopsyRun,
    AnalysisFeedback,
    AnalysisStatus,
    CalendarConnection,
    Client,
    ClientContact,
    CommunicationDraft,
    CommunicationRevision,
    EvidenceIssue,
    KanbanRequest,
    KanbanRequestStatus,
    Meeting,
    MeetingParticipant,
    MeetingPlan,
    Organization,
    PlanStatus,
    Role,
    Segment,
    Team,
    User,
    utcnow,
)
from app.tenancy import current_organization_id, tenant_scope

DEMO_ORGANIZATION_SLUG = "meetmind-demo"
DEMO_USERS = (
    ("marina@meetmind.example", "Marina Costa — Coordenação", Role.MANAGER),
    ("ana@meetmind.example", "Ana Souza — Analista de CS", Role.ANALYST),
    ("bruno@meetmind.example", "Bruno Lima — Analista de CS", Role.ANALYST),
    ("carla@meetmind.example", "Carla Oliveira — Coordenação de CS", Role.MANAGER),
    ("diego@meetmind.example", "Diego Martins — Somente leitura", Role.VIEWER),
)


def generate_demo_password() -> str:
    """Cria uma credencial temporária que nunca é persistida no repositório."""
    return secrets.token_urlsafe(24)

MEETINGS = (
    {
        "key": "atlas-risco",
        "owner": "ana@meetmind.example",
        "client": "Indústria Atlas",
        "title": "Revisão operacional — Indústria Atlas",
        "days_ago": 1,
        "product": "ERP de Manufatura",
        "meeting_type": "support_incident",
        "sentiment": "negative",
        "risk": "high",
        "health": "at_risk",
        "summary": "O cliente relatou lentidão recorrente no fechamento da produção.",
        "impact": "A demora está afetando o apontamento do segundo turno.",
        "next_step": "Solicitar diagnóstico técnico e retornar ao cliente até sexta-feira.",
        "action": "Solicitar diagnóstico da lentidão",
        "urgency": "immediate",
        "trigger": "dissatisfaction",
        "segments": (
            ("Ana Souza", True, "Quero revisar os pontos que ainda estão afetando a operação."),
            (
                "Ricardo Mendes — Gerente de Produção",
                False,
                "A lentidão continua no fechamento e ontem atrasou o apontamento do segundo turno.",
            ),
            (
                "Ana Souza",
                True,
                "Vou encaminhar o diagnóstico para Infraestrutura e retorno até sexta-feira.",
            ),
        ),
    },
    {
        "key": "horizonte-expansao",
        "owner": "ana@meetmind.example",
        "client": "Metalúrgica Horizonte",
        "title": "Planejamento de expansão — Metalúrgica Horizonte",
        "days_ago": 3,
        "product": "APS",
        "meeting_type": "commercial_expansion",
        "sentiment": "positive",
        "risk": "low",
        "health": "healthy",
        "summary": (
            "O cliente demonstrou interesse explícito em ampliar o planejamento "
            "da produção."
        ),
        "impact": "A nova unidade precisa integrar capacidade e sequenciamento.",
        "next_step": "Preparar demonstração do APS para a nova unidade.",
        "action": "Preparar demonstração do APS",
        "urgency": "next_7_days",
        "trigger": "opportunity",
        "segments": (
            ("Ana Souza", True, "Como a nova unidade muda o planejamento de vocês?"),
            (
                "Fernanda Rocha — Diretora Industrial",
                False,
                "Queremos avaliar o APS para a nova planta e temos orçamento "
                "previsto para este trimestre.",
            ),
            (
                "Ana Souza",
                True,
                "Vou envolver o Comercial e preparar uma demonstração focada no sequenciamento.",
            ),
        ),
    },
    {
        "key": "nova-era-followup",
        "owner": "ana@meetmind.example",
        "client": "Fábrica Nova Era",
        "title": "Acompanhamento de implantação — Fábrica Nova Era",
        "days_ago": 0,
        "product": "MES",
        "meeting_type": "implementation",
        "sentiment": "mixed",
        "risk": "medium",
        "health": "attention",
        "summary": "A implantação avançou, mas o treinamento dos supervisores permanece pendente.",
        "impact": "A falta de treinamento limita a adoção no chão de fábrica.",
        "next_step": "Confirmar treinamento dos supervisores na próxima terça-feira.",
        "action": "Confirmar treinamento dos supervisores",
        "urgency": "next_7_days",
        "trigger": "adoption_gap",
        "segments": (
            (
                "Juliana Alves — Supervisora",
                False,
                "O sistema está funcionando, mas dois supervisores ainda não "
                "fizeram o treinamento.",
            ),
            ("Ana Souza", True, "Vou confirmar a turma para terça-feira às dez horas."),
            ("Juliana Alves — Supervisora", False, "Combinado, esse horário funciona para o time."),
        ),
    },
    {
        "key": "prisma-treinamento",
        "owner": "bruno@meetmind.example",
        "client": "Grupo Prisma",
        "title": "Adoção do PCP — Grupo Prisma",
        "days_ago": 2,
        "product": "PCP",
        "meeting_type": "adoption_training",
        "sentiment": "neutral",
        "risk": "medium",
        "health": "attention",
        "summary": "O cliente pediu reforço de treinamento para a equipe de planejamento.",
        "impact": "Parte do processo ainda é mantida em planilhas paralelas.",
        "next_step": "Montar uma sessão prática sobre ordens de produção.",
        "action": "Planejar treinamento de PCP",
        "urgency": "next_7_days",
        "trigger": "adoption_gap",
        "segments": (
            ("Bruno Lima", True, "Onde a equipe ainda encontra dificuldade no processo?"),
            (
                "Paulo Nunes — Coordenador de PCP",
                False,
                "Ainda usamos uma planilha para organizar algumas ordens porque "
                "o time não domina o fluxo completo.",
            ),
            (
                "Bruno Lima",
                True,
                "Vou organizar um treinamento prático com essas ordens como exemplo.",
            ),
        ),
    },
    {
        "key": "aurora-renovacao",
        "owner": "bruno@meetmind.example",
        "client": "Indústria Aurora",
        "title": "Revisão de relacionamento — Indústria Aurora",
        "days_ago": 5,
        "product": "Suite de Manufatura",
        "meeting_type": "relationship_success",
        "sentiment": "positive",
        "risk": "low",
        "health": "healthy",
        "summary": "O cliente confirmou melhora após a correção e manteve o acompanhamento mensal.",
        "impact": "A correção recuperou estabilidade no processo de produção.",
        "next_step": "Acompanhar os indicadores na reunião mensal já confirmada.",
        "action": "Preparar indicadores para o acompanhamento mensal",
        "urgency": "next_30_days",
        "trigger": "positive_outcome",
        "segments": (
            (
                "Marcos Vieira — Gerente de TI",
                False,
                "Depois da correção não tivemos novas paradas e o fechamento ficou estável.",
            ),
            ("Bruno Lima", True, "Ótimo. Mantemos então a reunião mensal no dia quinze."),
            ("Marcos Vieira — Gerente de TI", False, "Confirmado, podemos seguir dessa forma."),
        ),
    },
    {
        "key": "delta-gestao",
        "owner": "carla@meetmind.example",
        "client": "Manufatura Delta",
        "title": "Revisão executiva — Manufatura Delta",
        "days_ago": 4,
        "product": "Analytics Industrial",
        "meeting_type": "discovery",
        "sentiment": "mixed",
        "risk": "medium",
        "health": "attention",
        "summary": "A liderança solicitou melhor visibilidade dos indicadores industriais.",
        "impact": "As decisões semanais dependem de consolidação manual de dados.",
        "next_step": "Mapear os indicadores prioritários antes de propor solução.",
        "action": "Mapear indicadores industriais prioritários",
        "urgency": "next_7_days",
        "trigger": "operational_need",
        "segments": (
            ("Carla Oliveira", True, "Quais decisões hoje dependem de consolidação manual?"),
            (
                "Sérgio Almeida — Diretor de Operações",
                False,
                "Toda segunda-feira o time leva horas para juntar produção, refugo e eficiência.",
            ),
            (
                "Carla Oliveira",
                True,
                "Primeiro vamos mapear os indicadores e suas fontes, sem assumir "
                "uma solução antes do diagnóstico.",
            ),
        ),
    },
)


def _evidence(segment: Segment, relevance: str) -> dict:
    return {
        "segment_id": segment.id,
        "quote": segment.text,
        "relevance": relevance,
        "speaker": segment.speaker,
    }


def _result(spec: dict, segments: list[Segment]) -> dict:
    customer_segment = next(item for item in segments if not item.is_totvs)
    action_segment = segments[-1]
    evidence = _evidence(customer_segment, "Trecho literal que sustenta o achado.")
    return {
        "schema_version": "meetmind-manufacturing-v1",
        "manufacturing_brief": {
            "what_happened": spec["summary"],
            "customer_requests": [
                {
                    "request": spec["next_step"],
                    "target": spec["product"],
                    "evidence": [evidence],
                }
            ],
            "objective": {
                "status": "partial" if spec["risk"] in {"medium", "high"} else "achieved",
                "rationale": spec["summary"],
                "confidence": 0.9,
                "evidence": [evidence],
            },
            "follow_up": {
                "status": "scheduled"
                if "terça" in spec["next_step"].lower() or "confirm" in action_segment.text.lower()
                else "proposed",
                "rationale": spec["next_step"],
                "confidence": 0.88,
                "evidence": [_evidence(action_segment, "Registra o próximo passo.")],
            },
            "conflict": {
                "status": "none_detected",
                "rationale": "Não há tensão explícita entre os participantes.",
                "confidence": 0.82,
                "evidence": [],
            },
            "concern": {
                "status": "attention" if spec["risk"] in {"medium", "high"} else "unclear",
                "rationale": spec["impact"],
                "confidence": 0.9,
                "evidence": [evidence],
            },
            "suggested_actions": [
                {
                    "action": spec["action"],
                    "rationale": spec["impact"],
                    "owner": "Customer Success",
                    "urgency": spec["urgency"],
                    "evidence_segment_ids": [customer_segment.id],
                }
            ],
            "uncertainties": ["Dado sintético criado exclusivamente para demonstração do fluxo."],
        },
        "executive_summary": {
            "what_happened": spec["summary"],
            "why_it_matters": spec["impact"],
            "next_step": spec["next_step"],
        },
        "overall_sentiment": spec["sentiment"],
        "risk_level": spec["risk"],
        "meeting_classification": {
            "sector": "Manufatura",
            "subsector": None,
            "sector_source": "meeting_metadata",
            "meeting_type": spec["meeting_type"],
            "confidence": 0.93,
            "evidence_segment_ids": [customer_segment.id],
        },
        "account_context": {
            "sector": "Manufatura",
            "subsector": None,
            "revenue_band": None,
            "completeness": 0.6,
            "missing_fields": ["revenue_band"],
            "limitations": ["Dados cadastrais financeiros não fazem parte da demonstração."],
        },
        "relationship_health": {
            "status": spec["health"],
            "rationale": spec["impact"],
            "confidence": 0.9,
            "evidence_segment_ids": [customer_segment.id],
        },
        "retention_signals": [
            {
                "signal_type": "attention" if spec["risk"] in {"medium", "high"} else "positive",
                "label": spec["summary"],
                "description": spec["impact"],
                "confidence": 0.9,
                "evidence": [evidence],
            }
        ],
        "context_application": [],
        "contextual_priority": spec["urgency"],
        "products_mentioned": [spec["product"]],
        "findings": [
            {
                "finding_id": f"demo-{spec['key']}",
                "category": "commercial" if spec["trigger"] == "opportunity" else "operational",
                "title": spec["summary"],
                "observation": customer_segment.text,
                "interpretation": spec["impact"],
                "business_impact": spec["impact"],
                "recommendation": spec["next_step"],
                "confidence": 0.92,
                "trigger": {
                    "trigger_type": spec["trigger"],
                    "label": spec["trigger"].replace("_", " ").title(),
                    "reason": spec["summary"],
                    "matched_terms": [],
                    "local_metric_category": "demo",
                },
                "evidence": [evidence],
            }
        ],
        "action_items": [
            {
                "action": spec["action"],
                "rationale": spec["impact"],
                "owner": "Customer Success",
                "urgency": spec["urgency"],
                "evidence_segment_ids": [customer_segment.id],
            }
        ],
        "uncertainties": [
            "Conteúdo sintético: não representa uma reunião, cliente ou resultado real."
        ],
        "traceability": {
            "model": "demo-synthetic",
            "prompt_version": "meetmind-demo-v1",
            "score_version": "demo-v1",
            "transcript_hash": "synthetic",
            "playbook_id": "manufatura",
            "playbook_version": "demo-v1",
            "sector_source": "meeting_metadata",
            "exact_evidence_count": 2,
        },
    }


def seed_demo(*, password: str | None = None, commit: bool = True) -> dict[str, int | str]:
    password = password or str(current_app.config.get("DEMO_PORTFOLIO_PASSWORD") or "")
    if len(password) < 9:
        raise ValueError(
            "Informe uma senha de demonstração com pelo menos 9 caracteres."
        )
    organization = Organization.query.filter_by(slug=DEMO_ORGANIZATION_SLUG).first()
    if organization is None:
        organization = Organization(
            slug=DEMO_ORGANIZATION_SLUG,
            name="MeetMind — Demonstração",
            retention_days=30,
            external_ai_enabled=False,
        )
        db.session.add(organization)
        db.session.flush()
    organization.external_ai_enabled = False
    organization.retention_days = 30
    try:
        active_organization_id = current_organization_id()
    except RuntimeError as error:
        if str(error) != "organization_context_required":
            raise
        active_organization_id = None
    if active_organization_id != organization.id:
        with tenant_scope(organization.id):
            return seed_demo(password=password, commit=commit)

    users: dict[str, User] = {}
    legacy_admin = User.query.filter_by(
        organization_id=organization.id,
        email="admin@meetmind.example",
    ).first()
    if legacy_admin is not None:
        legacy_admin.role = Role.VIEWER
        legacy_admin.is_active_record = False
        legacy_admin.session_version += 1
        legacy_admin.set_password(generate_demo_password())
    for email, name, role in DEMO_USERS:
        user = User.query.filter_by(organization_id=organization.id, email=email).first()
        if user is None:
            user = User(
                organization_id=organization.id,
                email=email,
                name=name,
                role=role,
                auth_source="local",
            )
            db.session.add(user)
        user.name = name
        user.role = role
        user.is_active_record = True
        user.set_password(password)
        users[email] = user
    db.session.flush()
    team = Team.query.filter_by(
        organization_id=organization.id, name="Customer Success Manufatura"
    ).first()
    if team is None:
        team = Team(
            organization_id=organization.id,
            name="Customer Success Manufatura",
        )
        db.session.add(team)
        db.session.flush()
    team.manager_id = users["carla@meetmind.example"].id
    for email in (
        "ana@meetmind.example",
        "bruno@meetmind.example",
        "carla@meetmind.example",
        "diego@meetmind.example",
    ):
        users[email].team_id = team.id

    now = utcnow()
    created_meetings = 0
    for index, spec in enumerate(MEETINGS):
        external_id = f"demo-{spec['key']}"
        if Meeting.query.filter_by(
            organization_id=organization.id, external_id=external_id
        ).first():
            continue
        owner = users[spec["owner"]]
        client = Client(
            organization_id=organization.id,
            external_id=f"demo-client-{spec['key']}",
            name=spec["client"],
            owner_id=owner.id,
        )
        db.session.add(client)
        db.session.flush()
        db.session.add(
            ClientContact(
                organization_id=organization.id,
                client_id=client.id,
                name=next(
                    speaker.split(" — ")[0]
                    for speaker, is_totvs, _text in spec["segments"]
                    if not is_totvs
                ),
                email=f"contato.{spec['key']}@example.com",
                job_title="Contato principal",
                is_primary=True,
            )
        )
        transcript = "\n".join(item[2] for item in spec["segments"])
        meeting = Meeting(
            organization_id=organization.id,
            external_id=external_id,
            title=spec["title"],
            client=client,
            occurred_at=now - timedelta(days=spec["days_ago"], hours=index),
            duration_seconds=1_800,
            language="pt-BR",
            approved_for_external_ai=False,
            transcript_hash=hashlib.sha256(transcript.encode()).hexdigest(),
            metadata_json={
                "nome_segmento": "MANUFATURA",
                "responsavel": owner.name,
                "demo": True,
                "data_classification": "synthetic_demo",
            },
            created_by_id=owner.id,
        )
        db.session.add(meeting)
        db.session.flush()
        db.session.add(
            MeetingParticipant(
                organization_id=organization.id,
                meeting_id=meeting.id,
                user_id=owner.id,
                participation_role="cs_owner",
            )
        )
        if spec["key"] == "aurora-renovacao":
            db.session.add(
                MeetingParticipant(
                    organization_id=organization.id,
                    meeting_id=meeting.id,
                    user_id=users["diego@meetmind.example"].id,
                    participation_role="viewer",
                )
            )
        segments = []
        for position, (speaker, is_totvs, text) in enumerate(spec["segments"]):
            segment = Segment(
                organization_id=organization.id,
                id=f"{external_id}.{position}",
                meeting_id=meeting.id,
                position=position,
                speaker=speaker,
                is_totvs=is_totvs,
                text=text,
                started_at_ms=position * 45_000,
                ended_at_ms=(position + 1) * 45_000,
            )
            db.session.add(segment)
            segments.append(segment)
        db.session.flush()
        result = _result(spec, segments)
        analysis = Analysis(
            organization_id=organization.id,
            meeting_id=meeting.id,
            requested_by_id=owner.id,
            status=AnalysisStatus.SUCCEEDED,
            model="demo-synthetic",
            provider="demo",
            prompt_version="meetmind-demo-v1",
            score_version="demo-v1",
            transcript_hash=meeting.transcript_hash,
            context_snapshot={"demo": True, "playbook": {"id": "manufatura"}},
            context_version="demo-v1",
            context_sha256=hashlib.sha256(b"meetmind-demo-v1").hexdigest(),
            mode_strategy="auto",
            requested_modes=[],
            selected_modes=[],
            mode_bundle_version="meetmind-demo-v1",
            analysis_profile_hash=f"demo-{spec['key']}",
            local_metrics={"demo": True},
            result=result,
            input_tokens=0,
            output_tokens=0,
            latency_ms=0,
            requested_at=meeting.occurred_at + timedelta(minutes=31),
            started_at=meeting.occurred_at + timedelta(minutes=31),
            finished_at=meeting.occurred_at + timedelta(minutes=32),
        )
        db.session.add(analysis)
        db.session.flush()
        evidence_ids = [segments[1].id]
        plan = MeetingPlan(
            organization_id=organization.id,
            client_id=client.id,
            meeting_id=meeting.id,
            analysis_id=analysis.id,
            objective=spec["next_step"],
            situation=spec["summary"],
            recommendation=spec["next_step"],
            evidence_segment_ids=evidence_ids,
            status=PlanStatus.AWAITING_REVIEW.value,
        )
        db.session.add(plan)
        db.session.flush()
        action_status = {
            "atlas-risco": ActionStatus.IN_PROGRESS.value,
            "horizonte-expansao": ActionStatus.ACCEPTED.value,
        }.get(spec["key"], ActionStatus.PROPOSED.value)
        due_days = {
            "atlas-risco": -1,
            "horizonte-expansao": 0,
            "nova-era-followup": 2,
        }.get(spec["key"], 3 + index)
        action = ActionItem(
            organization_id=organization.id,
            client_id=client.id,
            meeting_id=meeting.id,
            analysis_id=analysis.id,
            plan_id=plan.id,
            source_index=0,
            title=spec["action"],
            rationale=spec["impact"],
            urgency=spec["urgency"],
            priority="high" if spec["risk"] == "high" else "medium",
            evidence_segment_ids=evidence_ids,
            owner_id=owner.id,
            status=action_status,
            due_at=now + timedelta(days=due_days),
            accepted_by_id=owner.id if action_status != ActionStatus.PROPOSED.value else None,
            accepted_at=now - timedelta(days=1)
            if action_status != ActionStatus.PROPOSED.value
            else None,
        )
        db.session.add(action)
        db.session.flush()
        db.session.add(
            ActionEvent(
                organization_id=organization.id,
                action_item_id=action.id,
                actor_id=owner.id,
                event_type="updated"
                if action_status == ActionStatus.IN_PROGRESS.value
                else "proposed",
                details={"status": action_status, "demo": True},
                occurred_at=analysis.finished_at + timedelta(minutes=1),
            )
        )
        if spec["key"] == "atlas-risco":
            db.session.add(
                KanbanRequest(
                    organization_id=organization.id,
                    client_id=client.id,
                    source_action_id=action.id,
                    meeting_id=meeting.id,
                    analysis_id=analysis.id,
                    source_kind="meeting",
                    request_type="performance_analysis",
                    department="Infraestrutura",
                    priority="high",
                    note="Validar consumo e horários de pico relatados pelo cliente.",
                    payload_json={"demo": True, "channel": "internal_queue"},
                    created_by_id=owner.id,
                    assigned_to_id=users["carla@meetmind.example"].id,
                    due_at=now + timedelta(days=1),
                    response="Análise iniciada; os logs do horário de pico já foram separados.",
                    status=KanbanRequestStatus.ANSWERED.value,
                    responded_at=now - timedelta(hours=2),
                )
            )
        elif spec["key"] == "horizonte-expansao":
            db.session.add(
                KanbanRequest(
                    organization_id=organization.id,
                    client_id=client.id,
                    source_action_id=action.id,
                    meeting_id=meeting.id,
                    analysis_id=analysis.id,
                    source_kind="meeting",
                    request_type="commercial_opportunity",
                    department="Comercial",
                    priority="medium",
                    note="Preparar abordagem para a nova unidade.",
                    payload_json={"demo": True, "channel": "internal_queue"},
                    created_by_id=owner.id,
                    assigned_to_id=users["carla@meetmind.example"].id,
                    due_at=now + timedelta(days=2),
                    status=KanbanRequestStatus.IN_PROGRESS.value,
                )
            )
        created_meetings += 1
    if commit:
        db.session.commit()
    return {
        "organization": organization.slug,
        "users": len(DEMO_USERS),
        "meetings_created": created_meetings,
    }


def reset_demo(*, password: str | None = None) -> dict[str, int | str]:
    password = password or str(current_app.config.get("DEMO_PORTFOLIO_PASSWORD") or "")
    if len(password) < 9:
        raise ValueError(
            "Informe uma senha de demonstração com pelo menos 9 caracteres."
        )
    organization = Organization.query.filter_by(slug=DEMO_ORGANIZATION_SLUG).first()
    if organization is None:
        return seed_demo(password=password)
    with tenant_scope(organization.id):
        calendar_connections_removed = CalendarConnection.query.filter_by(
            organization_id=organization.id
        ).delete(synchronize_session=False)
        meetings = Meeting.query.filter(
            Meeting.organization_id == organization.id,
            Meeting.metadata_json["demo"].as_boolean().is_(True),
        ).all()
        meeting_ids = [item.id for item in meetings]
        analysis_ids = [
            item.id for item in Analysis.query.filter(Analysis.meeting_id.in_(meeting_ids)).all()
        ] if meeting_ids else []
        action_ids = (
            [
                item.id
                for item in ActionItem.query.filter(
                    ActionItem.meeting_id.in_(meeting_ids)
                ).all()
            ]
            if meeting_ids
            else []
        )
        draft_ids = [
            item.id
            for item in CommunicationDraft.query.filter(
                CommunicationDraft.meeting_id.in_(meeting_ids)
            ).all()
        ] if meeting_ids else []
        try:
            if draft_ids:
                CommunicationRevision.query.filter(
                    CommunicationRevision.communication_draft_id.in_(draft_ids)
                ).delete(synchronize_session=False)
                CommunicationDraft.query.filter(
                    CommunicationDraft.id.in_(draft_ids)
                ).delete(synchronize_session=False)
            if action_ids:
                ActionEvent.query.filter(
                    ActionEvent.action_item_id.in_(action_ids)
                ).delete(synchronize_session=False)
            KanbanRequest.query.filter(
                KanbanRequest.meeting_id.in_(meeting_ids)
            ).delete(synchronize_session=False)
            ActionItem.query.filter(
                ActionItem.meeting_id.in_(meeting_ids)
            ).delete(synchronize_session=False)
            if analysis_ids:
                EvidenceIssue.query.filter(
                    EvidenceIssue.analysis_id.in_(analysis_ids)
                ).delete(synchronize_session=False)
                AnalysisFeedback.query.filter(
                    AnalysisFeedback.analysis_id.in_(analysis_ids)
                ).delete(synchronize_session=False)
                AnalysisAutopsyRun.query.filter(
                    AnalysisAutopsyRun.analysis_id.in_(analysis_ids)
                ).delete(synchronize_session=False)
                AIExecution.query.filter(
                    AIExecution.analysis_id.in_(analysis_ids)
                ).delete(synchronize_session=False)
            MeetingPlan.query.filter(
                MeetingPlan.meeting_id.in_(meeting_ids)
            ).delete(synchronize_session=False)
            Analysis.query.filter(
                Analysis.meeting_id.in_(meeting_ids)
            ).delete(synchronize_session=False)
            MeetingParticipant.query.filter(
                MeetingParticipant.meeting_id.in_(meeting_ids)
            ).delete(synchronize_session=False)
            Segment.query.filter(Segment.meeting_id.in_(meeting_ids)).delete(
                synchronize_session=False
            )
            Meeting.query.filter(Meeting.id.in_(meeting_ids)).delete(
                synchronize_session=False
            )
            demo_clients = Client.query.filter(
                Client.organization_id == organization.id,
                Client.external_id.like("demo-client-%"),
            ).all()
            client_ids = [item.id for item in demo_clients]
            if client_ids:
                ClientContact.query.filter(
                    ClientContact.client_id.in_(client_ids)
                ).delete(synchronize_session=False)
                Client.query.filter(Client.id.in_(client_ids)).delete(
                    synchronize_session=False
                )
            report = seed_demo(password=password, commit=False)
            db.session.commit()
            report["calendar_connections_removed"] = calendar_connections_removed
            report["meetings_removed"] = len(meeting_ids)
            return report
        except Exception:
            db.session.rollback()
            raise
