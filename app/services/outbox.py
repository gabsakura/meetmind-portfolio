from __future__ import annotations

from datetime import UTC, datetime

from flask import g, has_request_context
from redis.exceptions import RedisError
from rq import Queue, Retry

from app.extensions import db
from app.models import Organization, OutboxEvent, utcnow
from app.security import redis_client
from app.tenancy import tenant_scope


def add(
    *,
    organization_id: str,
    queue_name: str,
    task_name: str,
    job_id: str,
    arguments: list | None = None,
    retry_intervals: list[int] | None = None,
    scheduled_for: datetime | None = None,
    correlation_id: str | None = None,
) -> OutboxEvent:
    if correlation_id is None and has_request_context():
        correlation_id = getattr(g, "correlation_id", None)
    existing = OutboxEvent.query.filter_by(job_id=job_id).first()
    if existing is not None:
        return existing
    event = OutboxEvent(
        organization_id=organization_id,
        queue_name=queue_name,
        task_name=task_name,
        job_id=job_id,
        arguments=arguments or [],
        retry_intervals=retry_intervals or [],
        scheduled_for=scheduled_for,
        correlation_id=correlation_id,
    )
    db.session.add(event)
    return event


def _task(name: str):
    from app import jobs

    allowed = {
        "expire_submission_file",
        "process_analysis",
        "process_submission_ingestion",
        "execute_privacy_request",
        "process_teams_transcript",
        "renew_teams_subscription",
    }
    if name not in allowed:
        raise ValueError("outbox_task_not_allowed")
    return getattr(jobs, name)


def dispatch(event_id: str) -> bool:
    event = db.session.get(OutboxEvent, event_id)
    if event is None or event.status in {"dispatched", "processing", "completed"}:
        return bool(event)
    if event.attempts == 0:
        event.attempts = 1
    try:
        queue = Queue(event.queue_name, connection=redis_client(), default_timeout=1800)
        options = {"job_id": event.job_id}
        if event.retry_intervals:
            options["retry"] = Retry(
                max=len(event.retry_intervals),
                interval=event.retry_intervals,
            )
        scheduled_for = event.scheduled_for
        if scheduled_for and scheduled_for.tzinfo is None:
            scheduled_for = scheduled_for.replace(tzinfo=UTC)
        if scheduled_for and scheduled_for > utcnow():
            queue.enqueue_at(
                scheduled_for,
                execute_event,
                event.organization_id,
                event.id,
                **options,
            )
        else:
            queue.enqueue(
                execute_event,
                event.organization_id,
                event.id,
                **options,
            )
        event.status = "dispatched"
        event.dispatched_at = utcnow()
        event.last_error = None
    except (RedisError, ValueError, TypeError) as error:
        event.status = "pending"
        event.last_error = type(error).__name__[:100]
        db.session.commit()
        return False
    db.session.commit()
    return True


def execute_event(organization_id: str, event_id: str) -> None:
    """Executa um evento no tenant certo e registra o ciclo completo do job."""
    from app import create_app

    app = create_app()
    with app.app_context(), tenant_scope(organization_id):
        event = db.session.get(OutboxEvent, event_id)
        if event is None or event.status == "completed":
            return
        if event.started_at is not None:
            event.attempts += 1
        event.status = "processing"
        event.started_at = utcnow()
        db.session.commit()
        app.logger.info(
            "outbox_processing task=%s",
            event.task_name,
            extra={
                "correlation_id": event.correlation_id,
                "organization_id": organization_id,
                "job_id": event.job_id,
            },
        )
        try:
            _task(event.task_name)(*event.arguments)
        except Exception as error:
            db.session.rollback()
            event = db.session.get(OutboxEvent, event_id)
            event.status = "dead_letter" if event.attempts >= 3 else "failed"
            event.last_error = type(error).__name__[:100]
            db.session.commit()
            raise
        event = db.session.get(OutboxEvent, event_id)
        event.status = "completed"
        event.completed_at = utcnow()
        event.last_error = None
        db.session.commit()
        app.logger.info(
            "outbox_completed task=%s",
            event.task_name,
            extra={
                "correlation_id": event.correlation_id,
                "organization_id": organization_id,
                "job_id": event.job_id,
            },
        )


def dispatch_pending(limit: int = 100) -> dict[str, int]:
    selected: list[tuple[str, str]] = []
    organizations = Organization.query.filter_by(is_active=True).order_by(Organization.id).all()
    for organization in organizations:
        remaining = limit - len(selected)
        if remaining <= 0:
            break
        with tenant_scope(organization.id):
            event_ids = db.session.scalars(
                db.select(OutboxEvent.id)
                .where(OutboxEvent.status.in_(["pending", "failed"]))
                .order_by(OutboxEvent.created_at.asc())
                .limit(remaining)
            ).all()
        selected.extend((organization.id, event_id) for event_id in event_ids)

    delivered = 0
    for organization_id, event_id in selected:
        with tenant_scope(organization_id):
            delivered += int(dispatch(event_id))
    return {
        "selected": len(selected),
        "dispatched": delivered,
        "pending": len(selected) - delivered,
    }
