from __future__ import annotations

from app.models import Analysis, Meeting, User
from app.services.access_policy import can_access_meeting, meetings_for


def can_access_analysis(user: User, analysis: Analysis | None) -> bool:
    return bool(analysis and can_access_meeting(user, analysis.meeting))


__all__ = ["can_access_analysis", "can_access_meeting", "meetings_for", "Meeting"]
