from __future__ import annotations

import json
import re
import uuid
from io import BytesIO

from flask import Blueprint, current_app, jsonify, request, send_file
from flask_login import current_user, login_required
from pydantic import BaseModel, Field, ValidationError
from redis import Redis
from redis.exceptions import RedisError
from rq import Queue
from rq.exceptions import NoSuchJobError
from rq.job import Job
from sqlalchemy.orm import selectinload

from app.extensions import db, limiter
from app.models import Analysis, AnalysisStatus, Meeting, Organization, Role
from app.services.chat_reports import (
    is_new_report_request,
    is_report_request,
    requested_meeting_numbers,
)
from app.services.meeting_access import (
    can_access_analysis,
    can_access_meeting,
    meetings_for,
)

chat_api_bp = Blueprint("chat_api", __name__, url_prefix="/api/v1/chat")

_PAGE_LABELS = {
    "/": "Visão geral",
    "/clientes": "Clientes",
    "/inteligencia-artificial": "Análise IA",
    "/auditabilidade-ia": "Auditabilidade IA",
    "/acoes": "Ações",
    "/encaminhamentos": "Encaminhamentos",
    "/historico": "Reuniões analisadas",
    "/verificacao": "Enviar reunião",
    "/seguranca": "Segurança",
    "/perfil": "Perfil",
}

_PAGE_GUIDANCE = {
    "/": "Visão geral do fluxo das reuniões, indicadores e itens que precisam de atenção.",
    "/clientes": "Lista de clientes e atalhos para o histórico e contexto de cada cliente.",
    "/inteligencia-artificial": (
        "Escolha uma reunião para consultar a leitura concluída, suas evidências, "
        "contexto e próximos passos."
    ),
    "/auditabilidade-ia": (
        "Selecione uma reunião para conferir evidências, transcrição, execuções e "
        "rastreabilidade da leitura."
    ),
    "/acoes": (
        "Acompanhe ações, responsáveis, prazos e status; use os filtros para localizar itens."
    ),
    "/encaminhamentos": "Acompanhe solicitações encaminhadas e suas respostas.",
    "/historico": (
        "Consulte e filtre reuniões analisadas por cliente, data, setor, risco e outros critérios."
    ),
    "/verificacao": (
        "Envie uma transcrição, informe seus dados e selecione o setor para iniciar "
        "o processamento."
    ),
    "/seguranca": "Consulte eventos de segurança e, quando autorizado, reconheça itens revisados.",
    "/perfil": "Consulte e atualize as informações do próprio perfil.",
}


class ChatTurn(BaseModel):
    role: str = Field(pattern="^(user|assistant)$")
    content: str = Field(min_length=1, max_length=1500)


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=1200)
    page: str = Field(min_length=1, max_length=80)
    page_context: str = Field(default="", max_length=7000)
    meeting_id: str | None = Field(default=None, max_length=36)
    analysis_id: str | None = Field(default=None, max_length=36)
    history: list[ChatTurn] = Field(default_factory=list, max_length=8)
    report_context: dict | None = None


def _page_snapshot(payload: ChatRequest) -> dict:
    label = _PAGE_LABELS.get(payload.page)
    if payload.page.startswith("/clientes/"):
        label = "Cliente"
    if not label:
        raise ValueError("unknown_page")
    if payload.page == "/auditabilidade-ia" and current_user.role not in {
        Role.ADMIN,
        Role.ANALYST,
    }:
        raise LookupError("not_found")

    snapshot = {
        "pagina_aberta": label,
        "orientacao_da_pagina": _PAGE_GUIDANCE.get(
            payload.page, "Histórico e resumo de um cliente da organização."
        ),
        "conteudo_visivel": payload.page_context.strip(),
    }

    visible_meeting_ids = meetings_for(current_user).with_entities(Meeting.id).subquery()
    recent_analyses = (
        Analysis.query.options(selectinload(Analysis.meeting).selectinload(Meeting.client))
        .filter(
            Analysis.status == AnalysisStatus.SUCCEEDED,
            Analysis.meeting_id.in_(db.select(visible_meeting_ids.c.id)),
        )
        .order_by(Analysis.finished_at.desc(), Analysis.requested_at.desc())
        .limit(50)
        .all()
    )
    summaries = []
    seen_meetings = set()
    for recent in recent_analyses:
        if not recent.meeting or recent.meeting_id in seen_meetings:
            continue
        seen_meetings.add(recent.meeting_id)
        result = recent.result if isinstance(recent.result, dict) else {}
        executive = result.get("executive_summary") or {}
        brief = result.get("manufacturing_brief") or {}
        summaries.append(
            {
                "reuniao": recent.meeting.title,
                "cliente": (
                    recent.meeting.client.name if recent.meeting.client else "Não identificado"
                ),
                "id_externo": recent.meeting.external_id,
                "data": recent.meeting.occurred_at.isoformat()
                if recent.meeting.occurred_at
                else None,
                "resumo": executive.get("what_happened") or brief.get("what_happened"),
                "relacao": result.get("relationship_health"),
                "produtos": result.get("products_mentioned"),
                "incertezas": result.get("uncertainties") or brief.get("uncertainties"),
            }
        )
        if len(summaries) == 12:
            break
    snapshot["reunioes_analisadas_recentes"] = summaries
    analysis = None
    if payload.analysis_id:
        analysis = db.session.get(Analysis, payload.analysis_id)
        if (
            not can_access_analysis(current_user, analysis)
            or analysis.status != AnalysisStatus.SUCCEEDED
        ):
            raise LookupError("not_found")
        if payload.meeting_id and analysis.meeting_id != payload.meeting_id:
            raise LookupError("not_found")

    meeting_id = payload.meeting_id or (analysis.meeting_id if analysis else None)
    if meeting_id:
        meeting = db.session.get(Meeting, meeting_id)
        if not can_access_meeting(current_user, meeting):
            raise LookupError("not_found")
        if analysis is None:
            analysis = (
                Analysis.query.filter_by(
                    meeting_id=meeting.id,
                    status=AnalysisStatus.SUCCEEDED,
                )
                .order_by(Analysis.finished_at.desc(), Analysis.requested_at.desc())
                .first()
            )
        snapshot["reuniao_selecionada"] = {
            "titulo": meeting.title,
            "cliente": meeting.client.name if meeting.client else "Não identificado",
            "id_externo": meeting.external_id,
            "data": meeting.occurred_at.isoformat() if meeting.occurred_at else None,
        }
        if analysis:
            result = analysis.result if isinstance(analysis.result, dict) else {}
            snapshot["analise_concluida"] = {
                "concluida_em": analysis.finished_at.isoformat() if analysis.finished_at else None,
                "resultado": {
                    key: result.get(key)
                    for key in (
                        "schema_version",
                        "executive_summary",
                        "manufacturing_brief",
                        "meeting_classification",
                        "relationship_health",
                        "retention_signals",
                        "findings",
                        "uncertainties",
                        "products_mentioned",
                        "action_items",
                    )
                    if key in result
                },
            }

    if is_report_request(payload.question, payload.report_context):
        prior_context = None if is_new_report_request(payload.question) else payload.report_context
        requested_ids = requested_meeting_numbers(payload.question, prior_context)
        report_meetings = []
        seen_ids = set()
        open_meeting = db.session.get(Meeting, meeting_id) if meeting_id else None
        if open_meeting:
            if open_meeting.external_id:
                requested_ids = list(
                    dict.fromkeys([*requested_ids, str(open_meeting.external_id)])
                )[:5]
            report_meetings.append((open_meeting, analysis))
            seen_ids.add(open_meeting.id)
        if requested_ids:
            requested_meetings = (
                meetings_for(current_user)
                .options(selectinload(Meeting.client))
                .filter(Meeting.external_id.in_(requested_ids))
                .all()
            )
            by_external_id = {meeting.external_id: meeting for meeting in requested_meetings}
            for external_id in requested_ids:
                meeting = by_external_id.get(external_id)
                if meeting and meeting.id not in seen_ids:
                    report_meetings.append((meeting, None))
                    seen_ids.add(meeting.id)

        available_meetings = []
        for meeting, selected_analysis in report_meetings[:5]:
            if selected_analysis is None or selected_analysis.meeting_id != meeting.id:
                selected_analysis = (
                    Analysis.query.filter_by(
                        meeting_id=meeting.id,
                        status=AnalysisStatus.SUCCEEDED,
                    )
                    .order_by(Analysis.finished_at.desc(), Analysis.requested_at.desc())
                    .first()
                )
            result = (
                selected_analysis.result
                if selected_analysis and isinstance(selected_analysis.result, dict)
                else {}
            )
            available_meetings.append(
                {
                    "titulo": meeting.title,
                    "id_externo": meeting.external_id,
                    "cliente": meeting.client.name if meeting.client else "Não identificado",
                    "data": meeting.occurred_at.isoformat() if meeting.occurred_at else None,
                    "analise_disponivel": bool(selected_analysis),
                    "resultado_analise": {
                        key: result[key]
                        for key in (
                            "executive_summary",
                            "manufacturing_brief",
                            "meeting_classification",
                            "relationship_health",
                            "retention_signals",
                            "findings",
                            "uncertainties",
                            "products_mentioned",
                            "action_items",
                            "risk_level",
                            "overall_sentiment",
                            "contextual_priority",
                            "context_application",
                        )
                        if key in result
                    },
                }
            )
        found_external_ids = {meeting["id_externo"] for meeting in available_meetings}
        snapshot["reunioes_para_relatorio"] = available_meetings
        snapshot["reunioes_nao_encontradas"] = [
            external_id for external_id in requested_ids if external_id not in found_external_ids
        ]
    return snapshot


@chat_api_bp.post("")
@login_required
@limiter.limit("20 per minute")
def submit_chat_message():
    try:
        payload = ChatRequest.model_validate(request.get_json(silent=True) or {})
        report_context_size = len(json.dumps(payload.report_context or {}, ensure_ascii=False))
        if report_context_size > 24_000:
            return (
                jsonify(
                    error="O rascunho do relatório excedeu o limite.",
                    code="report_context_too_large",
                ),
                413,
            )
        snapshot = _page_snapshot(payload)
    except ValidationError:
        return jsonify(error="Confira a pergunta e a página aberta.", code="invalid_payload"), 422
    except ValueError:
        return jsonify(error="Não reconheci a página aberta.", code="unknown_page"), 422
    except LookupError:
        return jsonify(error="O conteúdo solicitado não está disponível.", code="not_found"), 404

    organization = db.session.get(Organization, current_user.organization_id)
    if not organization or not organization.external_ai_enabled:
        return (
            jsonify(
                error="O uso de IA externa está desativado para esta organização.",
                code="external_ai_disabled",
            ),
            403,
        )
    if not current_app.config.get("QUEUE_ENABLED"):
        return (
            jsonify(error="O serviço do assistente está indisponível.", code="queue_disabled"),
            503,
        )

    job_id = str(uuid.uuid4())
    redis_connection = Redis.from_url(current_app.config["CHAT_REDIS_URL"])
    try:
        queue = Queue("chat", connection=redis_connection, default_timeout=90)
        job = queue.enqueue(
            "app.jobs_chatbot.process_chat",
            organization.id,
            current_user.id,
            current_app.config["GEMINI_MODEL"],
            payload.question.strip(),
            snapshot,
            [turn.model_dump() for turn in payload.history],
            payload.report_context,
            job_id=job_id,
            job_timeout=90,
            ttl=180,
            result_ttl=60,
            failure_ttl=30,
        )
    except (RedisError, ValueError, TypeError):
        return (
            jsonify(error="O serviço do assistente está indisponível.", code="queue_unavailable"),
            503,
        )
    return jsonify(job_id=job.id, status="queued", status_url=f"/api/v1/chat/{job.id}"), 202


@chat_api_bp.get("/<job_id>")
@login_required
@limiter.limit("120 per minute")
def chat_message_status(job_id: str):
    try:
        job = Job.fetch(
            job_id,
            connection=Redis.from_url(current_app.config["CHAT_REDIS_URL"]),
        )
    except (NoSuchJobError, RedisError, ValueError):
        return jsonify(error="Esta resposta temporária expirou.", code="chat_expired"), 404
    if (
        len(job.args or ()) < 2
        or job.args[0] != current_user.organization_id
        or job.args[1] != current_user.id
    ):
        return jsonify(error="Esta resposta temporária não está disponível.", code="not_found"), 404
    organization = db.session.get(Organization, current_user.organization_id)
    if not organization or not organization.external_ai_enabled:
        job.delete()
        return (
            jsonify(
                error="O uso de IA externa está desativado para esta organização.",
                code="external_ai_disabled",
            ),
            403,
        )

    status = job.get_status(refresh=True)
    if status == "finished":
        result = job.result
        job.delete()
        if isinstance(result, dict) and result.get("ok") is False:
            return jsonify(status="failed", error=result.get("answer", "O assistente falhou."))
        if isinstance(result, dict) and result.get("ok") is True:
            return jsonify(
                status="succeeded",
                answer=result.get("answer", ""),
                attachment=result.get("attachment"),
                report_context=result.get("report_context"),
            )
        answer = result if isinstance(result, str) else "Não consegui preparar uma resposta."
        return jsonify(status="succeeded", answer=answer)
    if status in {"failed", "stopped", "canceled"}:
        job.delete()
        return jsonify(
            status="failed",
            error=(
                "O worker-chat falhou antes de preparar a resposta. "
                "Confira se o serviço está ativo e veja os logs dele."
            ),
        )
    return jsonify(status="processing")


@chat_api_bp.get("/reports/<token>")
@login_required
@limiter.limit("30 per minute")
def download_chat_report(token: str):
    if not re.fullmatch(r"[A-Za-z0-9_-]{40,60}", token):
        return (
            jsonify(error="Este relatório temporário não está disponível.", code="not_found"),
            404,
        )
    key = f"meetmind:chat-pdf:{current_user.organization_id}:{current_user.id}:{token}"
    try:
        redis_connection = Redis.from_url(current_app.config["CHAT_REDIS_URL"])
        pdf_data = redis_connection.get(key)
        filename = redis_connection.get(f"{key}:filename")
    except RedisError:
        error_response = jsonify(
            error="O download está temporariamente indisponível.",
            code="redis_unavailable",
        )
        return error_response, 503
    if not pdf_data:
        return jsonify(error="Este relatório temporário expirou.", code="report_expired"), 404
    if isinstance(filename, bytes):
        filename = filename.decode("utf-8", errors="ignore")
    if not filename or not re.fullmatch(r"relatorio_[a-z0-9_]+\.pdf", filename):
        filename = "relatorio_personalizado.pdf"
    response = send_file(
        BytesIO(pdf_data),
        mimetype="application/pdf",
        as_attachment=True,
        download_name=filename,
        max_age=0,
    )
    response.headers["Cache-Control"] = "private, no-store"
    return response
