from __future__ import annotations

from collections.abc import Iterable

from app.models import SecurityEvent


def group_security_events(events: Iterable[SecurityEvent]) -> list[dict]:
    groups: dict[tuple[str, str], dict] = {}
    for event in events:
        key = (event.ip_hash, event.event_type)
        group = groups.get(key)
        if group is None:
            groups[key] = {
                "id": event.id,
                "event_type": event.event_type,
                "severity": event.severity,
                "route": event.route,
                "masked_ip": event.masked_ip,
                "occurred_at": event.occurred_at,
                "first_occurred_at": event.occurred_at,
                "acknowledged_at": event.acknowledged_at,
                "count": 1,
            }
            continue
        group["count"] += 1
        group["first_occurred_at"] = min(group["first_occurred_at"], event.occurred_at)
        if event.acknowledged_at is None:
            group["acknowledged_at"] = None
        if event.severity == "high":
            group["severity"] = "high"
    return sorted(groups.values(), key=lambda item: item["occurred_at"], reverse=True)
