from __future__ import annotations

from sqlalchemy import or_, select

from app.extensions import db
from app.models import (
    ActionItem,
    Client,
    KanbanRequest,
    Meeting,
    MeetingParticipant,
    Role,
    Team,
    TeamDelivery,
    User,
)


def managed_team_ids(user: User) -> set[str]:
    if user.role != Role.MANAGER:
        return set()
    return set(
        db.session.scalars(
            select(Team.id).where(
                Team.organization_id == user.organization_id,
                Team.manager_id == user.id,
                Team.is_active.is_(True),
            )
        )
    )


def managed_user_ids(user: User) -> set[str]:
    ids = {user.id}
    teams = managed_team_ids(user)
    if teams:
        ids.update(
            db.session.scalars(
                select(User.id).where(
                    User.organization_id == user.organization_id,
                    User.team_id.in_(teams),
                    User.is_active_record.is_(True),
                )
            )
        )
    return ids


def can_manage_user(actor: User, target: User | None) -> bool:
    if target is None or target.organization_id != actor.organization_id:
        return False
    return actor.role == Role.ADMIN or (
        actor.role == Role.MANAGER and target.id in managed_user_ids(actor)
    )


def can_share_with(actor: User, target: User | None) -> bool:
    if target is None or target.organization_id != actor.organization_id:
        return False
    if not target.is_active_record or actor.role == Role.VIEWER:
        return False
    if actor.role == Role.ADMIN:
        return True
    if actor.role == Role.MANAGER:
        return target.id in managed_user_ids(actor)
    return bool(actor.team_id and target.team_id == actor.team_id)


def clients_for(user: User):
    query = Client.query
    if user.role == Role.ADMIN:
        return query
    if user.role == Role.MANAGER:
        return query.filter(Client.owner_id.in_(managed_user_ids(user)))
    if user.role == Role.ANALYST:
        return query.filter(
            or_(
                Client.owner_id == user.id,
                Client.meetings.any(
                    Meeting.participants.any(MeetingParticipant.user_id == user.id)
                ),
            )
        )
    return query.filter(
        Client.meetings.any(
            Meeting.participants.any(MeetingParticipant.user_id == user.id)
        )
    )


def can_access_client(user: User, client: Client | None) -> bool:
    if client is None or client.organization_id != user.organization_id:
        return False
    return clients_for(user).filter(Client.id == client.id).first() is not None


def can_manage_client(user: User, client: Client | None) -> bool:
    if not can_access_client(user, client):
        return False
    if user.role == Role.ADMIN:
        return True
    if user.role == Role.MANAGER:
        return client.owner_id in managed_user_ids(user)
    return user.role == Role.ANALYST and client.owner_id == user.id


def meetings_for(user: User):
    query = Meeting.query
    if user.role == Role.ADMIN:
        return query
    participant = Meeting.participants.any(MeetingParticipant.user_id == user.id)
    if user.role == Role.MANAGER:
        owners = managed_user_ids(user)
        return query.outerjoin(Client, Meeting.client_id == Client.id).filter(
            or_(
                Meeting.created_by_id.in_(owners),
                Client.owner_id.in_(owners),
                Meeting.participants.any(MeetingParticipant.user_id.in_(owners)),
                participant,
            )
        )
    if user.role == Role.ANALYST:
        return query.outerjoin(Client, Meeting.client_id == Client.id).filter(
            or_(
                Meeting.created_by_id == user.id,
                Client.owner_id == user.id,
                participant,
            )
        )
    return query.filter(participant)


def can_access_meeting(user: User, meeting: Meeting | None) -> bool:
    if meeting is None or meeting.organization_id != user.organization_id:
        return False
    return meetings_for(user).filter(Meeting.id == meeting.id).first() is not None


def can_manage_meeting(user: User, meeting: Meeting | None) -> bool:
    if not can_access_meeting(user, meeting) or user.role == Role.VIEWER:
        return False
    if user.role == Role.ADMIN:
        return True
    if user.role == Role.MANAGER:
        client = db.session.get(Client, meeting.client_id) if meeting.client_id else None
        return meeting.created_by_id in managed_user_ids(user) or bool(
            client and client.owner_id in managed_user_ids(user)
        )
    return meeting.created_by_id == user.id or bool(
        meeting.client_id
        and db.session.scalar(
            select(Client.id).where(
                Client.id == meeting.client_id, Client.owner_id == user.id
            )
        )
    )


def actions_for(user: User):
    query = ActionItem.query
    if user.role == Role.ADMIN:
        return query
    if user.role == Role.MANAGER:
        ids = managed_user_ids(user)
        return query.outerjoin(Client, ActionItem.client_id == Client.id).filter(
            or_(ActionItem.owner_id.in_(ids), Client.owner_id.in_(ids))
        )
    if user.role == Role.ANALYST:
        return query.outerjoin(Client, ActionItem.client_id == Client.id).filter(
            or_(ActionItem.owner_id == user.id, Client.owner_id == user.id)
        )
    return query.filter(ActionItem.owner_id == user.id)


def can_access_action(user: User, action: ActionItem | None) -> bool:
    if action is None or action.organization_id != user.organization_id:
        return False
    if actions_for(user).filter(ActionItem.id == action.id).first() is not None:
        return True
    meeting = db.session.get(Meeting, action.meeting_id) if action.meeting_id else None
    return can_access_meeting(user, meeting)


def can_manage_action(user: User, action: ActionItem | None) -> bool:
    return user.role != Role.VIEWER and can_access_action(user, action)


def deliveries_for(user: User):
    query = TeamDelivery.query
    if user.role == Role.ADMIN:
        return query
    if user.role == Role.MANAGER:
        ids = managed_user_ids(user)
        return query.filter(
            or_(
                TeamDelivery.assigned_by_id == user.id,
                TeamDelivery.assignee_id.in_(ids),
            )
        )
    return query.filter(TeamDelivery.assignee_id == user.id)


def can_access_delivery(user: User, delivery: TeamDelivery | None) -> bool:
    if delivery is None or delivery.organization_id != user.organization_id:
        return False
    return deliveries_for(user).filter(TeamDelivery.id == delivery.id).first() is not None


def can_cancel_delivery(user: User, delivery: TeamDelivery | None) -> bool:
    if not can_access_delivery(user, delivery):
        return False
    return user.role == Role.ADMIN or (
        user.role == Role.MANAGER and delivery.assigned_by_id == user.id
    )


def requests_for(user: User):
    query = KanbanRequest.query
    if user.role == Role.ADMIN:
        return query
    if user.role == Role.MANAGER:
        ids = managed_user_ids(user)
        return query.outerjoin(Client, KanbanRequest.client_id == Client.id).filter(
            or_(
                KanbanRequest.created_by_id.in_(ids),
                KanbanRequest.assigned_to_id.in_(ids),
                Client.owner_id.in_(ids),
            )
        )
    return query.outerjoin(Client, KanbanRequest.client_id == Client.id).filter(
        or_(
            KanbanRequest.created_by_id == user.id,
            KanbanRequest.assigned_to_id == user.id,
            Client.owner_id == user.id,
        )
    )


def can_access_request(user: User, item: KanbanRequest | None) -> bool:
    if item is None or item.organization_id != user.organization_id:
        return False
    return requests_for(user).filter(KanbanRequest.id == item.id).first() is not None


def can_manage_request(user: User, item: KanbanRequest | None) -> bool:
    return user.role != Role.VIEWER and can_access_request(user, item)
