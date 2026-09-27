from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from flask import url_for

from app.extensions import db
from app.models import (
    ActionEvent,
    ActionItem,
    ActionStatus,
    Analysis,
    AnalysisStatus,
    Client,
    KanbanRequest,
    KanbanRequestStatus,
    Meeting,
    MeetingPlan,
    PlanStatus,
    Role,
    User,
    utcnow,
)
from app.services.access_policy import actions_for, meetings_for, requests_for

ACTIVE_ACTION_STATUSES = {
    ActionStatus.PROPOSED.value,
    ActionStatus.ACCEPTED.value,
    ActionStatus.IN_PROGRESS.value,
}


def _aware(value: datetime | None) -> datetime:
    if value is None:
        return datetime.max.replace(tzinfo=UTC)
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _is_admin(user: User) -> bool:
    return user.role == Role.ADMIN


def _action_query(user: User):
    return actions_for(user)


def briefing(user: User) -> dict[str, Any]:
    now = utcnow()
    active = [ActionStatus.ACCEPTED.value, ActionStatus.IN_PROGRESS.value]
    actions = _action_query(user)
    overdue = actions.filter(
        ActionItem.status.in_(active), ActionItem.due_at < now
    ).count()
    if _is_admin(user):
        awaiting_review = MeetingPlan.query.filter_by(
            status=PlanStatus.AWAITING_REVIEW.value
        ).count()
    else:
        awaiting_review = (
            db.session.scalar(
                db.select(db.func.count(db.distinct(MeetingPlan.id)))
                .join(ActionItem, ActionItem.plan_id == MeetingPlan.id)
                .where(
                    MeetingPlan.status == PlanStatus.AWAITING_REVIEW.value,
                    ActionItem.owner_id == user.id,
                )
            )
            or 0
        )
    month_start = datetime(now.year, now.month, 1, tzinfo=UTC)
    completed = actions.filter(
        ActionItem.status == ActionStatus.COMPLETED.value,
        ActionItem.completed_at >= month_start,
    ).count()
    cutoff = user.last_briefing_seen_at or user.created_at
    updates: list[dict[str, Any]] = []
    request_query = requests_for(user).filter(
        KanbanRequest.status == KanbanRequestStatus.ANSWERED.value,
        KanbanRequest.responded_at > cutoff,
    )
    answered = request_query.order_by(KanbanRequest.responded_at.desc()).limit(5).all()
    client_ids = {item.client_id for item in answered if item.client_id}
    clients = {
        item.id: item.name for item in Client.query.filter(Client.id.in_(client_ids)).all()
    } if client_ids else {}
    for item in answered:
        updates.append({
            "id": f"kanban-{item.id}",
            "title": f"{item.department} respondeu",
            "summary": item.response,
            "client": clients.get(item.client_id, "Cliente não identificado"),
            "tone": "success",
            "occurred_at": item.responded_at.isoformat(),
            "url": url_for("painel.encaminhamentos", _anchor=f"request-{item.id}"),
        })

    meeting_scope = meetings_for(user).with_entities(Meeting.id).subquery()
    risk_query = (
        Analysis.query.join(
            meeting_scope,
            Analysis.meeting_id == meeting_scope.c.id,
        )
        .join(Meeting, Analysis.meeting_id == Meeting.id)
        .outerjoin(Client, Meeting.client_id == Client.id)
        .filter(
            Analysis.status == AnalysisStatus.SUCCEEDED,
            Analysis.finished_at > cutoff,
        )
    )
    for analysis in risk_query.order_by(Analysis.finished_at.desc()).limit(10).all():
        title, summary, tone = _analysis_summary(analysis)
        if tone != "risk":
            continue
        updates.append({
            "id": f"risk-{analysis.id}",
            "title": title,
            "summary": summary,
            "client": (
                analysis.meeting.client.name
                if analysis.meeting.client
                else "Cliente não identificado"
            ),
            "tone": tone,
            "occurred_at": analysis.finished_at.isoformat(),
            "url": url_for(
                "painel.inteligencia_artificial",
                meeting=analysis.meeting_id,
                analysis=analysis.id,
            ),
        })
    updates.sort(
        key=lambda item: datetime.fromisoformat(item["occurred_at"].replace("Z", "+00:00")),
        reverse=True,
    )
    return {
        "metrics": [
            {"id": "overdue", "label": "Ações vencidas", "value": overdue, "tone": "risk"},
            {
                "id": "review",
                "label": "Planos para revisar",
                "value": awaiting_review,
                "tone": "attention",
            },
            {
                "id": "completed",
                "label": "Concluídas no mês",
                "value": completed,
                "tone": "success",
            },
        ],
        "updates": updates[:5],
        "new_count": len(updates),
    }


def focus(user: User, limit: int = 8) -> dict[str, Any]:
    now = utcnow()
    actions = (
        _action_query(user)
        .filter(ActionItem.status.in_(ACTIVE_ACTION_STATUSES))
        .order_by(ActionItem.created_at.desc())
        .limit(200)
        .all()
    )
    plan_ids = {item.plan_id for item in actions if item.plan_id}
    plans = {
        item.id: item for item in MeetingPlan.query.filter(MeetingPlan.id.in_(plan_ids)).all()
    } if plan_ids else {}
    client_ids = {item.client_id for item in actions if item.client_id}
    clients = {
        item.id: item.name for item in Client.query.filter(Client.id.in_(client_ids)).all()
    } if client_ids else {}

    def rank(action: ActionItem) -> tuple[int, datetime, datetime]:
        due = _aware(action.due_at)
        if action.due_at and due < now:
            group = 0
        elif action.status == ActionStatus.PROPOSED.value:
            group = 1
        elif action.due_at and due <= now + timedelta(days=7):
            group = 2
        elif action.status == ActionStatus.IN_PROGRESS.value:
            group = 3
        else:
            group = 4
        return group, due, _aware(action.created_at)

    labels = {
        ActionStatus.PROPOSED.value: "Aguardando revisão",
        ActionStatus.ACCEPTED.value: "Pendente",
        ActionStatus.IN_PROGRESS.value: "Em andamento",
    }
    items = []
    for action in sorted(actions, key=rank)[:limit]:
        plan = plans.get(action.plan_id)
        overdue = bool(action.due_at and _aware(action.due_at) < now)
        items.append({
            "id": action.id,
            "title": action.title,
            "context": action.rationale,
            "approach": plan.recommendation if plan else None,
            "client": clients.get(action.client_id, "Cliente não identificado"),
            "client_id": action.client_id,
            "meeting_id": action.meeting_id,
            "analysis_id": action.analysis_id,
            "status": action.status,
            "status_label": "Vencida" if overdue else labels.get(action.status, action.status),
            "priority": action.priority,
            "due_at": action.due_at.isoformat() if action.due_at else None,
            "overdue": overdue,
            "version": action.version,
            "can_manage": user.role in {Role.ADMIN, Role.MANAGER, Role.ANALYST}
            or action.owner_id == user.id,
            "can_route": user.role in {Role.ADMIN, Role.MANAGER, Role.ANALYST},
            "source_url": url_for(
                "painel.inteligencia_artificial", meeting=action.meeting_id,
                analysis=action.analysis_id, _anchor="ai-workflow",
            ),
        })
    return {"items": items}


def _analysis_summary(analysis: Analysis) -> tuple[str, str, str]:
    result = analysis.result if isinstance(analysis.result, dict) else {}
    modes = result.get("mode_results") if isinstance(result.get("mode_results"), dict) else {}
    relationship = (
        modes.get("relationship_risk")
        if isinstance(modes.get("relationship_risk"), dict)
        else {}
    )
    risk = str(relationship.get("risk_level") or result.get("risk_level") or "").lower()
    synthesis = result.get("synthesis") if isinstance(result.get("synthesis"), dict) else {}
    summary = (
        synthesis.get("executive_summary")
        if isinstance(synthesis.get("executive_summary"), dict)
        else {}
    )
    if not summary:
        summary = (
            result.get("executive_summary")
            if isinstance(result.get("executive_summary"), dict)
            else {}
        )
    text = str(summary.get("what_happened") or "A leitura da reunião foi concluída.")
    if risk in {"high", "critical"}:
        return "Sinal de risco encontrado", text, "risk"
    return "Reunião analisada", text, "neutral"


def timeline(user: User, limit: int = 8) -> dict[str, Any]:
    now = utcnow()
    events: list[dict[str, Any]] = []
    action_scope = _action_query(user).with_entities(ActionItem.id).subquery()
    event_rows = db.session.execute(
        db.select(ActionEvent, ActionItem)
        .join(ActionItem, ActionItem.id == ActionEvent.action_item_id)
        .join(action_scope, ActionItem.id == action_scope.c.id)
        .order_by(ActionEvent.occurred_at.desc())
        .limit(60)
    ).all()
    client_ids = {action.client_id for _, action in event_rows if action.client_id}
    clients = {
        item.id: item.name for item in Client.query.filter(Client.id.in_(client_ids)).all()
    } if client_ids else {}
    for event, action in event_rows:
        status = str((event.details or {}).get("status") or "")
        definition = {
            "proposed": ("Nova ação sugerida", "attention"),
            "accepted": ("Ação confirmada", "neutral"),
            "dismissed": ("Ação descartada", "neutral"),
        }.get(event.event_type)
        if event.event_type == "updated" and status == ActionStatus.IN_PROGRESS.value:
            definition = ("Ação iniciada", "attention")
        elif event.event_type == "updated" and status == ActionStatus.COMPLETED.value:
            definition = ("Ação concluída", "success")
        if definition is None:
            continue
        events.append({
            "id": f"action-{event.id}",
            "title": definition[0],
            "summary": action.title,
            "client": clients.get(action.client_id, "Cliente não identificado"),
            "occurred_at": event.occurred_at.isoformat(),
            "tone": definition[1],
            "url": url_for(
                "painel.inteligencia_artificial", meeting=action.meeting_id,
                analysis=action.analysis_id, _anchor="ai-workflow",
            ),
        })

    meeting_scope = meetings_for(user).with_entities(Meeting.id).subquery()
    analysis_query = (
        Analysis.query.join(meeting_scope, Analysis.meeting_id == meeting_scope.c.id)
        .join(Meeting, Analysis.meeting_id == Meeting.id)
        .outerjoin(Client, Meeting.client_id == Client.id)
        .filter(Analysis.status == AnalysisStatus.SUCCEEDED)
    )
    for analysis in analysis_query.order_by(Analysis.finished_at.desc()).limit(30).all():
        title, summary, tone = _analysis_summary(analysis)
        events.append({
            "id": f"analysis-{analysis.id}",
            "title": title,
            "summary": summary,
            "client": (
                analysis.meeting.client.name
                if analysis.meeting.client
                else "Cliente não identificado"
            ),
            "occurred_at": (analysis.finished_at or analysis.requested_at).isoformat(),
            "tone": tone,
            "url": url_for(
                "painel.inteligencia_artificial", meeting=analysis.meeting_id,
                analysis=analysis.id,
            ),
        })

    overdue = _action_query(user).filter(
        ActionItem.status.in_([ActionStatus.ACCEPTED.value, ActionStatus.IN_PROGRESS.value]),
        ActionItem.due_at < now,
    ).limit(30).all()
    overdue_client_ids = {item.client_id for item in overdue if item.client_id}
    overdue_clients = {
        item.id: item.name for item in Client.query.filter(Client.id.in_(overdue_client_ids)).all()
    } if overdue_client_ids else {}
    for action in overdue:
        events.append({
            "id": f"overdue-{action.id}",
            "title": "Prazo vencido",
            "summary": action.title,
            "client": overdue_clients.get(action.client_id, "Cliente não identificado"),
            "occurred_at": action.due_at.isoformat(),
            "tone": "risk",
            "url": url_for(
                "painel.inteligencia_artificial", meeting=action.meeting_id,
                analysis=action.analysis_id, _anchor="ai-workflow",
            ),
        })

    request_query = requests_for(user).filter(
        KanbanRequest.status == KanbanRequestStatus.ANSWERED.value
    )
    answered = request_query.order_by(KanbanRequest.responded_at.desc()).limit(20).all()
    answer_client_ids = {item.client_id for item in answered if item.client_id}
    answer_clients = {
        item.id: item.name for item in Client.query.filter(Client.id.in_(answer_client_ids)).all()
    } if answer_client_ids else {}
    for item in answered:
        events.append({
            "id": f"kanban-{item.id}",
            "title": "Encaminhamento respondido",
            "summary": item.response or "Uma resposta foi registrada.",
            "client": answer_clients.get(item.client_id, "Cliente não identificado"),
            "occurred_at": (item.responded_at or item.updated_at or item.created_at).isoformat(),
            "tone": "success",
            "url": url_for("painel.encaminhamentos", _anchor=f"request-{item.id}"),
        })

    events.sort(
        key=lambda item: datetime.fromisoformat(item["occurred_at"].replace("Z", "+00:00")),
        reverse=True,
    )
    return {"items": events[:limit]}
