from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.extensions import db
from app.models import (
    ActionEvent,
    ActionItem,
    ActionStatus,
    Analysis,
    MeetingPlan,
    PlanStatus,
    User,
    utcnow,
)
from app.services.access_policy import can_manage_action


def _priority(urgency: str) -> str:
    return {
        "immediate": "high",
        "next_7_days": "medium",
        "next_30_days": "low",
        "monitor": "low",
    }.get(urgency, "low")


def _explicit_due_at(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _verified_action_sources(analysis: Analysis) -> list[tuple[int, dict[str, Any], list[str]]]:
    result = analysis.result if isinstance(analysis.result, dict) else {}
    raw_actions = result.get("action_items") if isinstance(result.get("action_items"), list) else []
    valid_segment_ids = {segment.id for segment in analysis.meeting.segments}
    verified: list[tuple[int, dict[str, Any], list[str]]] = []
    for source_index, item in enumerate(raw_actions[:3]):
        if not isinstance(item, dict) or not str(item.get("action") or "").strip():
            continue
        references = list(
            dict.fromkeys(
                str(segment_id)
                for segment_id in (item.get("evidence_segment_ids") or [])
                if str(segment_id) in valid_segment_ids
            )
        )
        if not references:
            continue
        verified.append((source_index, item, references))
    return verified


def _plan_copy(analysis: Analysis, sources: list[tuple[int, dict[str, Any], list[str]]]):
    result = analysis.result if isinstance(analysis.result, dict) else {}
    brief = result.get("manufacturing_brief")
    summary = result.get("executive_summary")
    brief = brief if isinstance(brief, dict) else {}
    summary = summary if isinstance(summary, dict) else {}
    objective_decision = brief.get("objective")
    objective_decision = objective_decision if isinstance(objective_decision, dict) else {}
    titles = [str(item.get("action") or "").strip() for _, item, _ in sources]
    objective = (
        str(summary.get("next_step") or "").strip()
        or str(objective_decision.get("rationale") or "").strip()
        or titles[0]
    )
    situation = (
        str(brief.get("what_happened") or "").strip()
        or str(summary.get("what_happened") or "").strip()
        or "Situação sustentada pelas evidências da reunião."
    )
    recommendation = (
        str(summary.get("why_it_matters") or "").strip()
        or "; ".join(titles)
    )
    evidence_ids = list(
        dict.fromkeys(segment_id for _, _, references in sources for segment_id in references)
    )
    return objective[:1_000], situation[:2_000], recommendation[:2_000], evidence_ids


def create_or_refresh_plan(analysis: Analysis) -> tuple[MeetingPlan | None, list[ActionItem]]:
    sources = _verified_action_sources(analysis)
    existing = MeetingPlan.query.filter_by(analysis_id=analysis.id).first()
    if existing and existing.status != PlanStatus.AWAITING_REVIEW.value:
        actions = ActionItem.query.filter_by(plan_id=existing.id).order_by(
            ActionItem.source_index.asc()
        ).all()
        return existing, actions
    if not sources:
        if existing:
            for action in ActionItem.query.filter_by(plan_id=existing.id).all():
                if action.status == ActionStatus.PROPOSED.value:
                    db.session.delete(action)
            db.session.delete(existing)
        return None, []

    objective, situation, recommendation, evidence_ids = _plan_copy(analysis, sources)
    client = analysis.meeting.client
    if existing is not None:
        existing_actions = (
            ActionItem.query.filter_by(plan_id=existing.id)
            .order_by(ActionItem.source_index.asc())
            .all()
        )
        if any(
            action.status != ActionStatus.PROPOSED.value
            for action in existing_actions
        ):
            return existing, existing_actions
        expected = [
            (
                source_index,
                str(item.get("action") or "").strip()[:500],
                str(item.get("rationale") or "").strip()[:4_000],
                references,
            )
            for source_index, item, references in sources
        ]
        current = [
            (
                action.source_index,
                action.title,
                action.rationale,
                action.evidence_segment_ids or [],
            )
            for action in existing_actions
        ]
        if (
            existing.objective == objective
            and existing.situation == situation
            and existing.recommendation == recommendation
            and (existing.evidence_segment_ids or []) == evidence_ids
            and current == expected
        ):
            return existing, existing_actions
    if existing is None:
        plan = MeetingPlan(
            organization_id=analysis.organization_id,
            client_id=analysis.meeting.client_id,
            meeting_id=analysis.meeting_id,
            analysis_id=analysis.id,
            objective=objective,
            situation=situation,
            recommendation=recommendation,
            evidence_segment_ids=evidence_ids,
        )
        db.session.add(plan)
        db.session.flush()
    else:
        plan = existing
        plan.version_number += 1
        plan.objective = objective
        plan.situation = situation
        plan.recommendation = recommendation
        plan.evidence_segment_ids = evidence_ids
        for action in ActionItem.query.filter_by(plan_id=plan.id).all():
            if action.status == ActionStatus.PROPOSED.value:
                db.session.delete(action)
        db.session.flush()

    actions = []
    for source_index, item, references in sources:
        urgency = str(item.get("urgency") or "monitor")[:30]
        action = ActionItem(
            organization_id=analysis.organization_id,
            client_id=analysis.meeting.client_id,
            meeting_id=analysis.meeting_id,
            analysis_id=analysis.id,
            plan_id=plan.id,
            source_index=source_index,
            title=str(item.get("action") or "").strip()[:500],
            rationale=str(item.get("rationale") or "").strip()[:4_000],
            urgency=urgency,
            priority=_priority(urgency),
            evidence_segment_ids=references,
            owner_id=client.owner_id if client else None,
            due_at=_explicit_due_at(item.get("due_at")),
        )
        db.session.add(action)
        db.session.flush()
        record_event(
            action,
            analysis.requested_by_id,
            "proposed",
            source="analysis",
            source_index=source_index,
            evidence_segment_ids=references,
        )
        actions.append(action)
    return plan, actions


def create_draft(
    analysis: Analysis, source_index: int, *, actor_id: str | None = None
) -> tuple[ActionItem, bool]:
    existing = ActionItem.query.filter_by(
        analysis_id=analysis.id, source_index=source_index
    ).first()
    if existing:
        return existing, False
    sources = {
        index: (item, references)
        for index, item, references in _verified_action_sources(analysis)
    }
    if source_index not in sources:
        raise ValueError("A sugestão não possui evidência válida")
    plan, actions = create_or_refresh_plan(analysis)
    if plan is None:
        raise ValueError("A análise não possui plano acionável")
    action = next((item for item in actions if item.source_index == source_index), None)
    if action is None:
        raise ValueError("A sugestão não pode ser convertida em ação")
    if actor_id and actor_id != analysis.requested_by_id:
        record_event(action, actor_id, "manually_requested")
    return action, True


def create_drafts(analysis: Analysis) -> list[ActionItem]:
    return create_or_refresh_plan(analysis)[1]


def can_manage(user: User, action: ActionItem) -> bool:
    return can_manage_action(user, action)


def _is_overdue(action: ActionItem) -> bool:
    if not action.due_at or action.status in {
        ActionStatus.COMPLETED.value,
        ActionStatus.DISMISSED.value,
    }:
        return False
    due_at = action.due_at
    if due_at.tzinfo is None:
        due_at = due_at.replace(tzinfo=UTC)
    return due_at < utcnow()


def _evidence_json(action: ActionItem) -> list[dict[str, Any]]:
    analysis = db.session.get(Analysis, action.analysis_id)
    segments = (
        {segment.id: segment for segment in analysis.meeting.segments}
        if analysis is not None
        else {}
    )
    return [
        {
            "segment_id": segment.id,
            "speaker": segment.speaker,
            "quote": segment.text,
            "position": segment.position,
        }
        for segment_id in (action.evidence_segment_ids or [])
        if (segment := segments.get(segment_id)) is not None
    ]


def plan_json(plan: MeetingPlan | None, *, include_actions: bool = False) -> dict[str, Any] | None:
    if plan is None:
        return None
    payload: dict[str, Any] = {
        "id": plan.id,
        "client_id": plan.client_id,
        "meeting_id": plan.meeting_id,
        "analysis_id": plan.analysis_id,
        "version": plan.version_number,
        "objective": plan.objective,
        "situation": plan.situation,
        "recommendation": plan.recommendation,
        "evidence_segment_ids": plan.evidence_segment_ids or [],
        "status": plan.status,
        "reviewed_by_id": plan.reviewed_by_id,
        "reviewed_at": plan.reviewed_at.isoformat() if plan.reviewed_at else None,
        "created_at": plan.created_at.isoformat(),
        "updated_at": plan.updated_at.isoformat(),
    }
    if include_actions:
        payload["actions"] = [
            action_json(action)
            for action in ActionItem.query.filter_by(plan_id=plan.id)
            .order_by(ActionItem.source_index.asc())
            .all()
        ]
    return payload


def action_json(action: ActionItem, *, include_events: bool = False) -> dict[str, Any]:
    payload = {
        "id": action.id,
        "client_id": action.client_id,
        "meeting_id": action.meeting_id,
        "analysis_id": action.analysis_id,
        "plan_id": action.plan_id,
        "parent_action_id": action.parent_action_id,
        "source_index": action.source_index,
        "title": action.title,
        "rationale": action.rationale,
        "urgency": action.urgency,
        "priority": action.priority,
        "evidence_segment_ids": action.evidence_segment_ids or [],
        "evidence": _evidence_json(action),
        "owner_id": action.owner_id,
        "status": action.status,
        "due_at": action.due_at.isoformat() if action.due_at else None,
        "outcome": action.outcome,
        "customer_notified": action.customer_notified,
        "dismissal_reason": action.dismissal_reason,
        "version": action.version,
        "overdue": _is_overdue(action),
        "created_at": action.created_at.isoformat(),
        "updated_at": action.updated_at.isoformat(),
    }
    if include_events:
        events = ActionEvent.query.filter_by(action_item_id=action.id).order_by(
            ActionEvent.occurred_at.asc()
        )
        payload["events"] = [
            {
                "id": event.id,
                "type": event.event_type,
                "actor_id": event.actor_id,
                "details": event.details,
                "occurred_at": event.occurred_at.isoformat(),
            }
            for event in events
        ]
    return payload


def create_follow_up(
    source: ActionItem,
    actor: User,
    *,
    title: str,
    owner_id: str,
    due_at: datetime,
) -> ActionItem:
    clean_title = title.strip()
    owner = db.session.get(User, owner_id)
    if not clean_title or owner is None or owner.organization_id != source.organization_id:
        raise ValueError("invalid_follow_up")
    next_index = (
        db.session.scalar(
            db.select(db.func.max(ActionItem.source_index)).where(
                ActionItem.analysis_id == source.analysis_id
            )
        )
        or 0
    ) + 1
    follow_up = ActionItem(
        organization_id=source.organization_id,
        client_id=source.client_id,
        meeting_id=source.meeting_id,
        analysis_id=source.analysis_id,
        plan_id=source.plan_id,
        parent_action_id=source.id,
        source_index=next_index,
        title=clean_title[:500],
        rationale=f"Acompanhamento após a conclusão: {source.outcome}"[:4_000],
        urgency="next_7_days",
        priority=source.priority,
        evidence_segment_ids=source.evidence_segment_ids or [],
        owner_id=owner.id,
        status=ActionStatus.ACCEPTED.value,
        due_at=due_at,
        accepted_by_id=actor.id,
        accepted_at=utcnow(),
    )
    db.session.add(follow_up)
    db.session.flush()
    record_event(
        follow_up,
        actor.id,
        "accepted",
        owner_id=owner.id,
        due_at=due_at.isoformat(),
        follow_up_of=source.id,
    )
    record_event(source, actor.id, "follow_up_created", follow_up_action_id=follow_up.id)
    return follow_up


def sync_plan_status(plan_id: str | None, actor_id: str | None = None) -> None:
    if not plan_id:
        return
    plan = db.session.get(MeetingPlan, plan_id)
    if plan is None:
        return
    actions = ActionItem.query.filter_by(plan_id=plan.id).all()
    if not actions:
        return
    statuses = {action.status for action in actions}
    if statuses <= {ActionStatus.DISMISSED.value}:
        plan.status = PlanStatus.DISMISSED.value
    elif statuses <= {ActionStatus.COMPLETED.value, ActionStatus.DISMISSED.value}:
        plan.status = PlanStatus.COMPLETED.value
    elif ActionStatus.PROPOSED.value not in statuses:
        plan.status = PlanStatus.ACTIVE.value
    else:
        plan.status = PlanStatus.AWAITING_REVIEW.value
    if plan.status != PlanStatus.AWAITING_REVIEW.value:
        plan.reviewed_by_id = actor_id or plan.reviewed_by_id
        plan.reviewed_at = plan.reviewed_at or utcnow()


def record_event(action: ActionItem, actor_id: str | None, event_type: str, **details) -> None:
    db.session.add(
        ActionEvent(
            organization_id=action.organization_id,
            action_item_id=action.id,
            actor_id=actor_id,
            event_type=event_type,
            details=details,
        )
    )
