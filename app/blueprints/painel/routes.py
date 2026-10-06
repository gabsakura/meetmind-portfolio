from __future__ import annotations

import hmac

from flask import (
    Blueprint,
    Response,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_login import current_user, login_required
from sqlalchemy import func, or_
from sqlalchemy.orm import selectinload

from app.extensions import db, limiter
from app.models import (
    ActionItem,
    ActionStatus,
    Analysis,
    AnalysisStatus,
    CalendarConnection,
    Client,
    ClientContact,
    CommunicationDraft,
    CommunicationRevision,
    DeliveryStatus,
    KanbanRequest,
    Meeting,
    MeetingParticipant,
    MeetingPlan,
    MeetingSubmission,
    Role,
    SecurityEvent,
    Team,
    TeamDelivery,
    User,
    utcnow,
)
from app.security import audit
from app.services import avatars
from app.services.access_policy import (
    actions_for,
    can_access_client,
    can_manage_client,
    can_manage_user,
    clients_for,
    deliveries_for,
    managed_user_ids,
)
from app.services.actions import action_json
from app.services.calendar_oauth import (
    CalendarOAuthError,
    authorization_request,
    authorized_account_email,
    events_for_user,
    exchange_code,
    provider_statuses,
    save_connection,
)
from app.services.calendar_oauth import (
    disconnect as disconnect_calendar,
)
from app.services.kanban import KANBAN_ROUTING
from app.services.kanban import list_requests as list_kanban_requests
from app.services.kanban import request_json as kanban_request_json
from app.services.meeting_access import can_access_analysis, meetings_for
from app.services.sector_context import load_playbooks
from app.services.security_events import group_security_events
from app.services.team_deliveries import delivery_json

painel_bp = Blueprint("painel", __name__)


@painel_bp.route("/")
@login_required
def dashboard():
    """Painel diário de contexto, foco e encaminhamento para Customer Success."""
    return render_template(
        "dashboard.html",
        aba="dashboard",
        first_name=(current_user.name or "").split()[0] or "usuário",
        can_route=current_user.role in {Role.ADMIN, Role.MANAGER, Role.ANALYST},
    )


def _ai_enabled_meetings():
    return (
        meetings_for(current_user)
        .filter(
            or_(
                Meeting.approved_for_external_ai.is_(True),
                db.func.upper(Meeting.metadata_json["nome_segmento"].as_string()) == "MANUFATURA",
            )
        )
        .order_by(Meeting.created_at.desc())
        .all()
    )


@painel_bp.route("/inteligencia-artificial")
@login_required
def inteligencia_artificial():
    selected_meeting_id = request.args.get("meeting", "")
    selected_analysis_id = request.args.get("analysis", "")
    if selected_analysis_id and not selected_meeting_id:
        selected_analysis = db.session.get(Analysis, selected_analysis_id)
        if can_access_analysis(current_user, selected_analysis):
            selected_meeting_id = selected_analysis.meeting_id
    if current_user.role == Role.ADMIN:
        participant_options = User.query.filter_by(
            organization_id=current_user.organization_id,
            is_active_record=True,
        ).order_by(User.name.asc()).all()
    elif current_user.role == Role.MANAGER:
        participant_options = User.query.filter(
            User.organization_id == current_user.organization_id,
            User.id.in_(managed_user_ids(current_user)),
            User.is_active_record.is_(True),
        ).order_by(User.name.asc()).all()
    elif current_user.role == Role.ANALYST and current_user.team_id:
        participant_options = User.query.filter(
            User.organization_id == current_user.organization_id,
            User.team_id == current_user.team_id,
            User.is_active_record.is_(True),
        ).order_by(User.name.asc()).all()
    else:
        participant_options = [current_user]
    return render_template(
        "inteligencia_artificial.html",
        aba="inteligencia_artificial",
        meetings=_ai_enabled_meetings(),
        selected_meeting_id=selected_meeting_id,
        selected_analysis_id=selected_analysis_id,
        participant_options=participant_options,
    )


@painel_bp.route("/analise")
@login_required
def analise():
    query = {}
    if request.args.get("meeting"):
        query["meeting"] = request.args["meeting"]
    if request.args.get("analysis"):
        query["analysis"] = request.args["analysis"]
    return redirect(url_for("painel.inteligencia_artificial", **query), code=302)


@painel_bp.route("/auditabilidade-ia")
@login_required
def auditabilidade_ia():
    if current_user.role not in {Role.ADMIN, Role.MANAGER, Role.ANALYST}:
        abort(404)
    meetings = _auditable_meetings()
    return render_template(
        "analise.html",
        aba="auditabilidade_ia",
        meetings=meetings,
        selected_meeting_id=request.args.get("meeting", ""),
        selected_analysis_id=request.args.get("analysis", ""),
    )


def _auditable_meetings():
    """Reuniões que já possuem ao menos uma leitura concluída pela IA."""
    return (
        meetings_for(current_user)
        .filter(Meeting.analyses.any(Analysis.status == AnalysisStatus.SUCCEEDED))
        .order_by(Meeting.created_at.desc())
        .all()
    )


def _latest_successful_analyses(limit: int = 100) -> list[Analysis]:
    latest_analyses = (
        db.select(
            Analysis.id.label("analysis_id"),
            func.row_number()
            .over(
                partition_by=Analysis.meeting_id,
                order_by=(
                    Analysis.finished_at.desc(),
                    Analysis.requested_at.desc(),
                    Analysis.id.desc(),
                ),
            )
            .label("position"),
        )
        .where(Analysis.status == AnalysisStatus.SUCCEEDED)
        .subquery()
    )
    statement = (
        db.select(Analysis)
        .join(latest_analyses, Analysis.id == latest_analyses.c.analysis_id)
        .join(Meeting, Analysis.meeting_id == Meeting.id)
        .where(latest_analyses.c.position == 1)
        .options(selectinload(Analysis.meeting).selectinload(Meeting.client))
        .order_by(Analysis.finished_at.desc(), Analysis.requested_at.desc())
        .limit(limit)
    )
    meeting_scope = meetings_for(current_user).with_entities(Meeting.id).subquery()
    statement = statement.join(
        meeting_scope, Analysis.meeting_id == meeting_scope.c.id
    )
    return db.session.scalars(statement).all()


@painel_bp.route("/resultados")
@login_required
def resultados():
    query = {}
    if request.args.get("analysis"):
        query["analysis"] = request.args["analysis"]
    return redirect(url_for("painel.inteligencia_artificial", **query), code=302)


@painel_bp.route("/acoes")
@login_required
def acoes():
    scope = actions_for(current_user)
    query = scope
    status_filter = request.args.get("status", "").strip()
    owner_filter = request.args.get("owner", "").strip()
    client_filter = request.args.get("client", "").strip()
    priority_filter = request.args.get("priority", "").strip()
    search = request.args.get("q", "").strip()
    overdue_filter = request.args.get("overdue") == "true"
    sort_filter = request.args.get("sort", "due_at").strip()
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1
    if status_filter:
        query = query.filter(ActionItem.status == status_filter)
    if owner_filter == "mine":
        query = query.filter(ActionItem.owner_id == current_user.id)
    elif owner_filter:
        query = query.filter(ActionItem.owner_id == owner_filter)
    if client_filter:
        query = query.filter(ActionItem.client_id == client_filter)
    if priority_filter:
        query = query.filter(ActionItem.priority == priority_filter)
    if search:
        pattern = f"%{search[:100]}%"
        matching_clients = db.select(Client.id).where(Client.name.ilike(pattern))
        query = query.filter(
            or_(
                ActionItem.title.ilike(pattern),
                ActionItem.rationale.ilike(pattern),
                ActionItem.client_id.in_(matching_clients),
            )
        )
    if overdue_filter:
        query = query.filter(
            ActionItem.due_at < utcnow(),
            ActionItem.status.notin_([ActionStatus.COMPLETED.value, ActionStatus.DISMISSED.value]),
        )
    if sort_filter == "created_at":
        query = query.order_by(ActionItem.created_at.desc())
    elif sort_filter == "updated_at":
        query = query.order_by(ActionItem.updated_at.desc())
    else:
        sort_filter = "due_at"
        query = query.order_by(ActionItem.due_at.asc(), ActionItem.created_at.desc())
    per_page = 40
    total = query.count()
    pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, pages)
    actions = query.offset((page - 1) * per_page).limit(per_page).all()
    if current_user.role == Role.ADMIN:
        active_users = User.query.filter_by(is_active_record=True).order_by(User.name).all()
    elif current_user.role == Role.MANAGER:
        active_users = (
            User.query.filter(
                User.id.in_(managed_user_ids(current_user)),
                User.is_active_record.is_(True),
            )
            .order_by(User.name)
            .all()
        )
    else:
        active_users = [current_user]
    clients = {item.id: item for item in clients_for(current_user).all()}
    groups = {
        status.value: [action for action in actions if action.status == status.value]
        for status in ActionStatus
    }
    stats = {status.value: 0 for status in ActionStatus}
    stats.update(
        {
            status: count
            for status, count in scope.with_entities(
                ActionItem.status, func.count(ActionItem.id)
            ).group_by(ActionItem.status)
        }
    )
    stats["overdue"] = scope.filter(
        ActionItem.due_at < utcnow(),
        ActionItem.status.notin_([ActionStatus.COMPLETED.value, ActionStatus.DISMISSED.value]),
    ).count()
    delivery_rows = (
        deliveries_for(current_user)
        .order_by(TeamDelivery.due_at.asc(), TeamDelivery.created_at.desc())
        .limit(100)
        .all()
    )
    delivery_assignees = [
        user
        for user in active_users
        if user.id != current_user.id and user.role == Role.ANALYST
    ]
    calendar_options_by_user = {
        user.id: [
            {
                "id": item["id"],
                "label": item["label"],
            }
            for item in provider_statuses(user)
            if (
                item["connection"] is not None
                and item["tasks_enabled"]
                and item["connection"].allow_team_deliveries
            )
        ]
        for user in delivery_assignees
    }
    can_assign_deliveries = current_user.role in {Role.ADMIN, Role.MANAGER}
    if current_app.config.get("PORTFOLIO_DEMO_ENABLED"):
        can_assign_deliveries = bool(
            can_assign_deliveries
            and current_app.config.get("PORTFOLIO_CALENDAR_OAUTH_ENABLED")
            and str(current_user.email).casefold()
            == str(current_app.config.get("PORTFOLIO_MANAGER_TEST_EMAIL", "")).casefold()
        )
    return render_template(
        "acoes.html",
        aba="acoes",
        actions=actions,
        action_groups=groups,
        action_stats=stats,
        users=active_users,
        user_names={user.id: user.name for user in active_users},
        client_names={client_id: client.name for client_id, client in clients.items()},
        overdue_ids={action.id for action in actions if action_json(action)["overdue"]},
        selected_filters={
            "status": status_filter,
            "owner": owner_filter,
            "client": client_filter,
            "priority": priority_filter,
            "q": search,
            "overdue": overdue_filter,
            "sort": sort_filter,
        },
        pagination={"page": page, "pages": pages, "total": total},
        calendar_providers=[
            item
            for item in provider_statuses(current_user)
            if item["connection"] is not None
            and item["connection"].status == "connected"
            and item["write_enabled"]
        ],
        can_assign_deliveries=can_assign_deliveries,
        delivery_assignees=delivery_assignees,
        delivery_calendar_options=calendar_options_by_user,
        deliveries=[delivery_json(item) for item in delivery_rows],
        delivery_status_labels={
            DeliveryStatus.PENDING.value: "Pendente",
            DeliveryStatus.IN_PROGRESS.value: "Em andamento",
            DeliveryStatus.COMPLETED.value: "Concluída",
            DeliveryStatus.CANCELLED.value: "Cancelada",
        },
    )


@painel_bp.route("/clientes/<client_id>")
@login_required
def cliente_timeline(client_id: str):
    client = db.session.get(Client, client_id)
    if not can_access_client(current_user, client):
        abort(404)
    meetings = (
        Meeting.query.filter_by(client_id=client.id).order_by(Meeting.occurred_at.desc()).all()
    )
    actions = (
        ActionItem.query.filter_by(client_id=client.id).order_by(ActionItem.created_at.desc()).all()
    )
    analyses = (
        Analysis.query.join(Meeting, Analysis.meeting_id == Meeting.id)
        .filter(Meeting.client_id == client.id, Analysis.status == AnalysisStatus.SUCCEEDED)
        .order_by(Analysis.finished_at.desc())
        .all()
    )
    analysis_cards = [_analysis_card(item) for item in analyses]
    requests = (
        KanbanRequest.query.filter_by(client_id=client.id)
        .order_by(KanbanRequest.updated_at.desc(), KanbanRequest.created_at.desc())
        .all()
    )
    plans = MeetingPlan.query.filter_by(client_id=client.id).order_by(
        MeetingPlan.updated_at.desc()
    ).all()
    communications = CommunicationDraft.query.filter_by(client_id=client.id).order_by(
        CommunicationDraft.updated_at.desc()
    ).all()
    active_actions = [
        item
        for item in actions
        if item.status
        in {
            ActionStatus.PROPOSED.value,
            ActionStatus.ACCEPTED.value,
            ActionStatus.IN_PROGRESS.value,
        }
    ]
    next_action = min(
        (item for item in active_actions if item.due_at),
        key=lambda item: item.due_at,
        default=None,
    )
    latest_card = analysis_cards[0] if analysis_cards else None
    events = []
    for meeting in meetings:
        events.append(
            {
                "at": meeting.occurred_at or meeting.created_at,
                "kind": "Reunião",
                "title": meeting.title,
                "detail": "Transcrição e leitura disponíveis.",
                "url": url_for("painel.inteligencia_artificial", meeting=meeting.id),
            }
        )
    for action in actions:
        events.append(
            {
                "at": action.updated_at,
                "kind": "Ação",
                "title": action.title,
                "detail": action.outcome or action.rationale,
                "url": url_for("painel.acoes", q=action.title),
            }
        )
    for plan in plans:
        events.append(
            {
                "at": plan.updated_at,
                "kind": "Plano",
                "title": plan.objective,
                "detail": plan.recommendation,
                "url": url_for(
                    "painel.inteligencia_artificial",
                    meeting=plan.meeting_id,
                    analysis=plan.analysis_id,
                    _anchor="ai-workflow",
                ),
            }
        )
    for item in requests:
        request_data = kanban_request_json(item)
        events.append(
            {
                "at": item.updated_at or item.created_at,
                "kind": "Encaminhamento",
                "title": request_data["request_label"],
                "detail": item.response or item.note or f"Enviado para {item.department}.",
                "url": url_for("painel.encaminhamentos", _anchor=f"request-{item.id}"),
            }
        )
    for draft in communications:
        revision = CommunicationRevision.query.filter_by(
            communication_draft_id=draft.id,
            revision_number=draft.current_revision,
        ).first()
        events.append(
            {
                "at": draft.updated_at,
                "kind": "Comunicação",
                "title": revision.subject if revision else "Comunicação ao cliente",
                "detail": {
                    "draft": "Rascunho em revisão",
                    "reviewed": "Texto revisado",
                    "approved": "Aprovado, aguardando uso",
                    "scheduled": "Follow-up agendado",
                    "sent_confirmed": "Envio confirmado pelo CS",
                }.get(draft.status, draft.status),
                "url": url_for(
                    "painel.encaminhamentos",
                    _anchor=f"request-{draft.kanban_request_id}",
                )
                if draft.kanban_request_id
                else url_for("painel.cliente_timeline", client_id=client.id),
            }
        )
    events.sort(key=lambda item: item["at"], reverse=True)
    owner = db.session.get(User, client.owner_id) if client.owner_id else None
    if current_user.role == Role.ADMIN:
        owner_options = User.query.filter(
            User.organization_id == current_user.organization_id,
            User.is_active_record.is_(True),
            User.role.in_([Role.MANAGER, Role.ANALYST]),
        ).order_by(User.name.asc()).all()
    elif current_user.role == Role.MANAGER:
        owner_options = User.query.filter(
            User.organization_id == current_user.organization_id,
            User.id.in_(managed_user_ids(current_user)),
            User.is_active_record.is_(True),
            User.role.in_([Role.MANAGER, Role.ANALYST]),
        ).order_by(User.name.asc()).all()
    else:
        owner_options = []
    return render_template(
        "cliente.html",
        aba="clientes",
        client=client,
        owner=owner,
        meetings=meetings,
        actions=actions,
        analysis_cards=analysis_cards,
        latest_card=latest_card,
        active_actions=active_actions,
        next_action=next_action,
        requests=requests,
        active_requests=[item for item in requests if item.status not in {"closed", "cancelled"}],
        timeline=events,
        contacts=ClientContact.query.filter_by(client_id=client.id).order_by(
            ClientContact.is_primary.desc(), ClientContact.name.asc()
        ).all(),
        can_manage_client=can_manage_client(current_user, client),
        can_transfer=current_user.role in {Role.ADMIN, Role.MANAGER},
        owner_options=owner_options,
    )


@painel_bp.route("/clientes")
@login_required
def clientes():
    query = clients_for(current_user).order_by(Client.name.asc())
    rows = []
    for client in query.all():
        latest_meeting = (
            Meeting.query.filter_by(client_id=client.id)
            .order_by(Meeting.occurred_at.desc(), Meeting.created_at.desc())
            .first()
        )
        active = ActionItem.query.filter(
            ActionItem.client_id == client.id,
            ActionItem.status.in_(
                [
                    ActionStatus.PROPOSED.value,
                    ActionStatus.ACCEPTED.value,
                    ActionStatus.IN_PROGRESS.value,
                ]
            ),
        ).all()
        latest_analysis = (
            Analysis.query.join(Meeting, Analysis.meeting_id == Meeting.id)
            .filter(
                Meeting.client_id == client.id,
                Analysis.status == AnalysisStatus.SUCCEEDED,
            )
            .order_by(Analysis.finished_at.desc())
            .first()
        )
        card = _analysis_card(latest_analysis) if latest_analysis else None
        rows.append(
            {
                "client": client,
                "last_meeting": latest_meeting,
                "open_actions": len(active),
                "overdue": sum(1 for item in active if action_json(item)["overdue"]),
                "health": (card or {})
                .get("relationship_health", {})
                .get("status", "insufficient_evidence"),
            }
        )
    return render_template("clientes.html", aba="clientes", rows=rows)


@painel_bp.route("/encaminhamentos")
@login_required
def encaminhamentos():
    items = [kanban_request_json(item) for item in list_kanban_requests(current_user)]
    client_ids = {item["client_id"] for item in items if item.get("client_id")}
    contacts_by_client = {
        client_id: ClientContact.query.filter_by(client_id=client_id, is_active=True)
        .order_by(ClientContact.is_primary.desc(), ClientContact.name.asc())
        .all()
        for client_id in client_ids
    }
    if current_user.role == Role.ADMIN:
        users = User.query.filter_by(is_active_record=True).order_by(User.name.asc()).all()
    elif current_user.role == Role.MANAGER:
        users = (
            User.query.filter(
                User.id.in_(managed_user_ids(current_user)),
                User.is_active_record.is_(True),
            )
            .order_by(User.name.asc())
            .all()
        )
    else:
        users = [current_user]
    return render_template(
        "encaminhamentos.html",
        aba="encaminhamentos",
        items=items,
        users=users,
        can_manage=current_user.role in {Role.ADMIN, Role.MANAGER, Role.ANALYST},
        clients=clients_for(current_user).order_by(Client.name.asc()).all(),
        routing_options=KANBAN_ROUTING,
        contacts_by_client=contacts_by_client,
        calendar_providers=[
            item for item in provider_statuses(current_user) if item["write_enabled"]
        ],
    )


@painel_bp.route("/historico")
@login_required
def historico():
    analyses = _latest_successful_analyses()

    cards = [_analysis_card(analysis) for analysis in analyses]
    products = sorted(
        {product for card in cards for product in card["products"]},
        key=str.casefold,
    )
    sectors = sorted({card["sector"] for card in cards if card["sector"]}, key=str.casefold)
    meeting_types = sorted(
        {card["meeting_type"] for card in cards if card["meeting_type"]},
        key=str.casefold,
    )
    counters = {
        "all": len(cards),
        "risk": sum(card["has_risk"] for card in cards),
        "opportunity": sum(card["has_opportunity"] for card in cards),
        "review": sum(card["needs_review"] for card in cards),
    }
    return render_template(
        "historico.html",
        aba="historico",
        cards=cards,
        products=products,
        sectors=sectors,
        meeting_types=meeting_types,
        counters=counters,
    )


def _analysis_card(analysis: Analysis) -> dict:
    result = analysis.result if isinstance(analysis.result, dict) else {}
    schema_version = result.get("schema_version")
    if schema_version == "meetmind-modes-v1":
        mode_results = result.get("mode_results") or {}
        synthesis = result.get("synthesis") or {}
        routing = result.get("routing") or {}
        voc = mode_results.get("voc_complaint") or {}
        outcome = mode_results.get("meeting_outcome") or {}
        product = mode_results.get("product_operations") or {}
        relationship = mode_results.get("relationship_risk") or {}
        risk_level = relationship.get("risk_level", "low")
        health_status = {
            "low": "healthy",
            "medium": "attention",
            "high": "at_risk",
            "critical": "at_risk",
        }.get(risk_level, "insufficient_evidence")
        findings = [
            {
                "title": item.get("target") or "Reclamação atribuída ao cliente",
                "observation": item.get("observation"),
                "interpretation": item.get("impact"),
                "business_impact": item.get("impact"),
                "recommendation": item.get("desired_outcome"),
                "confidence": item.get("confidence", 0),
                "evidence": item.get("evidence") or [],
                "trigger": {
                    "trigger_type": "dissatisfaction",
                    "label": item.get("category", "complaint"),
                    "reason": item.get("observation"),
                },
            }
            for item in (voc.get("complaints") or [])
            if isinstance(item, dict)
        ]
        signals = []
        for signal_type, items in (
            ("positive", relationship.get("positive_signals") or []),
            ("attention", relationship.get("negative_signals") or []),
        ):
            signals.extend(
                {
                    "signal_type": signal_type,
                    "label": item.get("statement") or "Sinal observado",
                    "description": item.get("statement"),
                    "confidence": item.get("confidence", 0),
                    "evidence": item.get("evidence") or [],
                }
                for item in items
                if isinstance(item, dict)
            )
        result = {
            **result,
            "executive_summary": synthesis.get("executive_summary") or {},
            "findings": findings,
            "action_items": [],
            "uncertainties": synthesis.get("uncertainties") or [],
            "meeting_classification": {
                "meeting_type": outcome.get("meeting_type")
                or routing.get("meeting_type", "unknown")
            },
            "account_context": {},
            "relationship_health": {
                "status": health_status,
                "rationale": relationship.get("rationale"),
                "confidence": relationship.get("confidence", 0),
            },
            "retention_signals": signals,
            "context_application": [],
            "contextual_priority": ("immediate" if risk_level == "critical" else "monitor"),
            "products_mentioned": [
                item.get("canonical_name") or item.get("original_term")
                for item in (product.get("product_mentions") or [])
                if isinstance(item, dict)
                and (item.get("canonical_name") or item.get("original_term"))
            ],
            "overall_sentiment": "neutral",
            "risk_level": risk_level,
        }
    summary = result.get("executive_summary") or {}
    findings = result.get("findings") or []
    action_items = result.get("action_items") or []
    uncertainties = result.get("uncertainties") or []
    classification = result.get("meeting_classification") or {}
    account_context = result.get("account_context") or {}
    relationship_health = result.get("relationship_health") or {}
    retention_signals = result.get("retention_signals") or []
    context_application = result.get("context_application") or []
    products = [
        str(product) for product in (result.get("products_mentioned") or []) if str(product).strip()
    ]
    confidences = [
        float(finding.get("confidence", 0))
        for finding in findings
        if isinstance(finding, dict) and isinstance(finding.get("confidence"), (int, float))
    ]
    trigger_types = {
        finding.get("trigger", {}).get("trigger_type")
        for finding in findings
        if isinstance(finding, dict) and isinstance(finding.get("trigger"), dict)
    }
    risk_level = result.get("risk_level", "low")
    has_risk = risk_level in {"high", "critical"} or bool(
        trigger_types & {"commercial_risk", "dissatisfaction"}
    )
    has_opportunity = "opportunity" in trigger_types
    needs_review = bool(uncertainties) or not confidences or min(confidences) < 0.65
    evidence_count = sum(
        len(finding.get("evidence") or []) for finding in findings if isinstance(finding, dict)
    )
    primary_finding = findings[0] if findings and isinstance(findings[0], dict) else {}
    primary_trigger = (
        primary_finding.get("trigger") if isinstance(primary_finding.get("trigger"), dict) else {}
    )
    groups = ["all"]
    if has_risk:
        groups.append("risk")
    if has_opportunity:
        groups.append("opportunity")
    if needs_review:
        groups.append("review")

    meeting = analysis.meeting
    metadata = meeting.metadata_json if isinstance(meeting.metadata_json, dict) else {}
    traceability = (
        result.get("traceability") if isinstance(result.get("traceability"), dict) else {}
    )
    return {
        "analysis": analysis,
        "schema_version": schema_version,
        "meeting": meeting,
        "client_name": meeting.client.name if meeting.client else "Não identificado",
        "segment": metadata.get("nome_segmento") or metadata.get("segmento"),
        "owner": metadata.get("responsavel") or metadata.get("owner"),
        "sector": account_context.get("sector") or metadata.get("nome_segmento"),
        "subsector": account_context.get("subsector"),
        "meeting_type": classification.get("meeting_type", "unknown"),
        "relationship_health": relationship_health,
        "retention_signals": retention_signals,
        "context_application": context_application,
        "contextual_priority": result.get("contextual_priority", "low"),
        "account_context": account_context,
        "summary": summary,
        "sentiment": result.get("overall_sentiment", "neutral"),
        "risk_level": risk_level,
        "products": products,
        "findings": findings,
        "action_items": action_items,
        "tracked_actions": ActionItem.query.filter_by(analysis_id=analysis.id)
        .order_by(ActionItem.source_index.asc())
        .all(),
        "uncertainties": uncertainties,
        "confidence": round(sum(confidences) / len(confidences) * 100) if confidences else 0,
        "evidence_count": evidence_count,
        "primary_trigger": primary_trigger,
        "has_risk": has_risk,
        "has_opportunity": has_opportunity,
        "needs_review": needs_review,
        "groups": " ".join(groups),
        "date_iso": meeting.occurred_at.date().isoformat() if meeting.occurred_at else "",
        "traceability": {
            "model": traceability.get("model") or analysis.model,
            "policy_version": (
                traceability.get("policy_version")
                or traceability.get("prompt_version")
                or analysis.prompt_version
            ),
            "score_version": traceability.get("score_version") or analysis.score_version,
            "transcript_hash": traceability.get("transcript_hash") or analysis.transcript_hash,
            "context_version": traceability.get("context_version") or analysis.context_version,
            "context_sha256": traceability.get("context_sha256") or analysis.context_sha256,
            "playbook_id": traceability.get("playbook_id"),
            "playbook_version": traceability.get("playbook_version"),
            "sector_source": traceability.get("sector_source"),
            "context_completeness": traceability.get("context_completeness"),
            "exact_evidence_count": traceability.get("exact_evidence_count"),
            "realigned_evidence_count": traceability.get("realigned_evidence_count"),
            "format_repair_count": traceability.get("format_repair_count"),
            "latency_ms": traceability.get("latency_ms") or analysis.latency_ms,
            "input_tokens": traceability.get("input_tokens") or analysis.input_tokens,
            "output_tokens": traceability.get("output_tokens") or analysis.output_tokens,
        },
    }


@painel_bp.route("/seguranca")
@login_required
def seguranca():
    if current_user.role != Role.ADMIN:
        from flask import abort

        abort(404)
    raw_events = SecurityEvent.query.order_by(SecurityEvent.occurred_at.desc()).limit(2_000).all()
    events = group_security_events(raw_events)[:100]
    return render_template(
        "seguranca.html",
        aba="seguranca",
        events=events,
    )


@painel_bp.get("/equipe")
@login_required
def equipe():
    if current_user.role not in {Role.ADMIN, Role.MANAGER}:
        abort(404)
    if current_user.role == Role.ADMIN:
        teams = Team.query.filter_by(
            organization_id=current_user.organization_id
        ).order_by(Team.name.asc()).all()
        users = User.query.filter_by(
            organization_id=current_user.organization_id
        ).order_by(User.name.asc()).all()
    else:
        team_ids = {
            team.id
            for team in Team.query.filter_by(
                organization_id=current_user.organization_id,
                manager_id=current_user.id,
                is_active=True,
            )
        }
        teams = Team.query.filter(Team.id.in_(team_ids)).order_by(Team.name.asc()).all()
        users = (
            User.query.filter(
                User.organization_id == current_user.organization_id,
                User.team_id.in_(team_ids),
            ).order_by(User.name.asc()).all()
            if team_ids else []
        )
    return render_template(
        "equipe.html",
        aba="equipe",
        teams=teams,
        users=users,
        can_administer=current_user.role == Role.ADMIN,
    )


@painel_bp.get("/equipe/<user_id>")
@login_required
def membro_equipe(user_id: str):
    if current_user.role not in {Role.ADMIN, Role.MANAGER}:
        abort(404)
    member = db.session.get(User, user_id)
    if not can_manage_user(current_user, member):
        abort(404)
    team = db.session.get(Team, member.team_id) if member.team_id else None
    clients = Client.query.filter_by(owner_id=member.id).order_by(Client.name.asc()).all()
    meetings = (
        Meeting.query.outerjoin(Client, Meeting.client_id == Client.id)
        .filter(
            or_(
                Meeting.created_by_id == member.id,
                Client.owner_id == member.id,
                Meeting.participants.any(MeetingParticipant.user_id == member.id),
            )
        )
        .order_by(Meeting.occurred_at.desc())
        .limit(12)
        .all()
    )
    actions = (
        ActionItem.query.outerjoin(Client, ActionItem.client_id == Client.id)
        .filter(or_(ActionItem.owner_id == member.id, Client.owner_id == member.id))
        .order_by(ActionItem.due_at.asc(), ActionItem.created_at.desc())
        .limit(20)
        .all()
    )
    deliveries = (
        TeamDelivery.query.filter_by(assignee_id=member.id)
        .order_by(TeamDelivery.due_at.asc(), TeamDelivery.created_at.desc())
        .limit(20)
        .all()
    )
    requests = (
        KanbanRequest.query.filter(
            or_(
                KanbanRequest.created_by_id == member.id,
                KanbanRequest.assigned_to_id == member.id,
            )
        )
        .order_by(KanbanRequest.updated_at.desc())
        .limit(12)
        .all()
    )
    open_action_statuses = {
        ActionStatus.PROPOSED.value,
        ActionStatus.ACCEPTED.value,
        ActionStatus.IN_PROGRESS.value,
    }
    open_delivery_statuses = {
        DeliveryStatus.PENDING.value,
        DeliveryStatus.IN_PROGRESS.value,
    }
    return render_template(
        "membro_equipe.html",
        aba="equipe",
        member=member,
        team=team,
        clients=clients,
        meetings=meetings,
        actions=actions,
        deliveries=[delivery_json(item) for item in deliveries],
        requests=requests,
        calendar_providers=provider_statuses(member),
        stats={
            "clients": len(clients),
            "meetings": len(meetings),
            "open_actions": sum(item.status in open_action_statuses for item in actions),
            "open_deliveries": sum(item.status in open_delivery_statuses for item in deliveries),
        },
    )


@painel_bp.get("/verificacao")
@login_required
def verificacao():
    if current_user.role == Role.VIEWER:
        abort(404)
    mine = (
        MeetingSubmission.query.filter_by(created_by_id=current_user.id)
        .order_by(MeetingSubmission.created_at.desc())
        .limit(25)
        .all()
    )
    playbooks = sorted(
        load_playbooks().values(),
        key=lambda item: (
            item.id == "generico",
            item.label.casefold(),
        ),
    )
    if current_user.role == Role.ADMIN:
        available_users = User.query.filter_by(
            organization_id=current_user.organization_id,
            is_active_record=True,
        ).order_by(User.name.asc()).all()
    elif current_user.role == Role.MANAGER:
        available_users = User.query.filter(
            User.organization_id == current_user.organization_id,
            User.id.in_(managed_user_ids(current_user)),
            User.is_active_record.is_(True),
        ).order_by(User.name.asc()).all()
    else:
        available_users = User.query.filter(
            User.organization_id == current_user.organization_id,
            User.team_id == current_user.team_id,
            User.is_active_record.is_(True),
        ).order_by(User.name.asc()).all() if current_user.team_id else [current_user]
    return render_template(
        "verificacao.html",
        aba="verificacao",
        mine=mine,
        sectors=[{"id": playbook.id, "label": playbook.label} for playbook in playbooks],
        clients=clients_for(current_user).order_by(Client.name.asc()).all(),
        available_users=available_users,
    )


@painel_bp.get("/diagnostico")
@login_required
def diagnostico():
    return redirect(url_for("painel.verificacao"), code=308)


@painel_bp.get("/triagem")
@login_required
def triagem():
    return redirect(url_for("painel.verificacao"), code=308)


@painel_bp.get("/perfil")
@login_required
def perfil():
    return render_template(
        "perfil.html",
        aba="perfil",
        limite_kb=avatars.max_bytes() // 1024,
        calendar_providers=provider_statuses(current_user),
    )


def _calendar_redirect_uri(provider: str) -> str:
    path = url_for("painel.perfil_calendario_callback", provider=provider)
    return f"{str(current_app.config['PUBLIC_BASE_URL']).rstrip('/')}{path}"


@painel_bp.get("/perfil/calendario/conectar/<provider>")
@login_required
@limiter.limit("20 per hour")
def perfil_calendario_conectar(provider: str):
    try:
        authorize_url, state, verifier = authorization_request(
            provider, _calendar_redirect_uri(provider)
        )
    except CalendarOAuthError:
        flash("Esse calendário ainda não foi configurado pelo administrador.", "error")
        return redirect(url_for("painel.perfil", _anchor="calendario"))
    session[f"calendar_oauth_{provider}"] = {
        "state": state,
        "verifier": verifier,
    }
    return redirect(authorize_url)


@painel_bp.get("/perfil/calendario/callback/<provider>")
@login_required
@limiter.limit("30 per hour")
def perfil_calendario_callback(provider: str):
    pending = session.pop(f"calendar_oauth_{provider}", None) or {}
    state = str(request.args.get("state") or "")
    expected = str(pending.get("state") or "")
    if request.args.get("error"):
        flash("A conexão com o calendário foi cancelada.", "error")
        return redirect(url_for("painel.perfil", _anchor="calendario"))
    if not state or not expected or not hmac.compare_digest(state, expected):
        flash("Não foi possível validar a conexão com o calendário.", "error")
        return redirect(url_for("painel.perfil", _anchor="calendario"))
    code = str(request.args.get("code") or "")
    if not code:
        flash("O provedor não retornou autorização para o calendário.", "error")
        return redirect(url_for("painel.perfil", _anchor="calendario"))
    try:
        token = exchange_code(provider, code, _calendar_redirect_uri(provider), pending["verifier"])
        account_email = authorized_account_email(provider, token, current_user.email)
        connection = save_connection(current_user, provider, token, account_email=account_email)
        db.session.flush()
        audit("CALENDAR_CONNECTED", "calendar_connection", connection.id, provider=provider)
        db.session.commit()
    except (CalendarOAuthError, KeyError):
        db.session.rollback()
        flash("Não foi possível concluir a conexão com o calendário.", "error")
        return redirect(url_for("painel.perfil", _anchor="calendario"))
    flash("Calendário conectado ao Resumo do Dia.", "success")
    return redirect(url_for("painel.perfil", _anchor="calendario"))


@painel_bp.post("/perfil/calendario/<provider>/entregas")
@login_required
def perfil_calendario_entregas(provider: str):
    connection = CalendarConnection.query.filter_by(
        organization_id=current_user.organization_id,
        user_id=current_user.id,
        provider=provider,
        status="connected",
    ).first()
    if connection is None:
        abort(404)
    enabled = request.form.get("enabled") == "true"
    statuses = {item["id"]: item for item in provider_statuses(current_user)}
    if enabled and not statuses.get(provider, {}).get("tasks_enabled"):
        flash("Reconecte o provedor para autorizar tarefas antes de ativar.", "error")
        return redirect(url_for("painel.perfil", _anchor="calendario"))
    connection.allow_team_deliveries = enabled
    audit(
        "TEAM_DELIVERY_CALENDAR_CONSENT_CHANGED",
        "calendar_connection",
        connection.id,
        provider=provider,
        enabled=enabled,
    )
    db.session.commit()
    flash(
        "Tarefas da equipe serão sincronizadas." if enabled
        else "Novas tarefas não serão mais sincronizadas.",
        "success",
    )
    return redirect(url_for("painel.perfil", _anchor="calendario"))


@painel_bp.post("/perfil/calendario/<provider>/sincronizar")
@login_required
@limiter.limit("30 per hour")
def perfil_calendario_sincronizar(provider: str):
    statuses = {item["id"]: item for item in provider_statuses(current_user)}
    selected = statuses.get(provider)
    if selected is None or selected["connection"] is None:
        abort(404)
    try:
        events_for_user(current_user, provider)
    except CalendarOAuthError:
        flash("Não foi possível atualizar o calendário agora.", "error")
    else:
        connection = selected["connection"]
        if connection.status == "error":
            flash("Não foi possível atualizar o calendário agora.", "error")
        else:
            flash("Calendário atualizado.", "success")
    return redirect(url_for("painel.perfil", _anchor="calendario"))


@painel_bp.post("/perfil/calendario/<provider>/desconectar")
@login_required
def perfil_calendario_desconectar(provider: str):
    try:
        removed = disconnect_calendar(current_user, provider)
    except CalendarOAuthError:
        abort(404)
    if removed:
        audit("CALENDAR_DISCONNECTED", "user", current_user.id, provider=provider)
        db.session.commit()
    flash("Calendário desconectado.", "success")
    return redirect(url_for("painel.perfil", _anchor="calendario"))


@painel_bp.post("/perfil/notificacoes")
@login_required
def perfil_notificacoes():
    current_user.email_delivery_notifications = (
        request.form.get("email_delivery_notifications") == "true"
    )
    audit(
        "DELIVERY_EMAIL_PREFERENCE_CHANGED",
        "user",
        current_user.id,
        enabled=current_user.email_delivery_notifications,
    )
    db.session.commit()
    flash("Preferências de notificação atualizadas.", "success")
    return redirect(url_for("painel.perfil", _anchor="notificacoes"))


@painel_bp.post("/perfil/foto")
@login_required
@limiter.limit("10 per hour")
def perfil_foto_upload():
    enviado = request.files.get("foto")
    dados = enviado.read() if enviado else b""
    mime, erro = avatars.validate(dados)
    if erro:
        flash(erro, "error")
        return redirect(url_for("painel.perfil"))
    current_user.avatar_data = dados
    current_user.avatar_mime = mime
    current_user.avatar_updated_at = utcnow()
    db.session.commit()
    flash("Foto atualizada.", "success")
    return redirect(url_for("painel.perfil"))


@painel_bp.post("/perfil/foto/remover")
@login_required
def perfil_foto_remover():
    current_user.avatar_data = None
    current_user.avatar_mime = None
    current_user.avatar_updated_at = None
    db.session.commit()
    flash("Foto removida. Voltamos a mostrar suas iniciais.", "success")
    return redirect(url_for("painel.perfil"))


@painel_bp.get("/perfil/foto/<user_id>")
@login_required
def perfil_foto(user_id: str):
    """Serve a foto de um usuário.

    Exige sessão: o arquivo é guardado como veio, com EXIF e tudo, então não
    fica aberto para quem não está autenticado. O parâmetro ?v= no template
    troca a URL a cada upload, o que invalida o cache do navegador sem
    precisar de no-store aqui.
    """
    user = db.session.get(User, user_id)
    if not user or not user.avatar_mime or not user.avatar_data:
        abort(404)
    resposta = Response(user.avatar_data, mimetype=user.avatar_mime)
    resposta.headers["Content-Disposition"] = "inline"
    resposta.headers["Cache-Control"] = "private, max-age=86400"
    return resposta
