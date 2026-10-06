from __future__ import annotations

import base64
import hashlib
import json
import secrets
from datetime import UTC, datetime, timedelta
from urllib.parse import quote, urlencode

import httpx
from cryptography.fernet import Fernet, InvalidToken
from flask import current_app

from app.extensions import db
from app.models import CalendarConnection, User, utcnow


class CalendarOAuthError(RuntimeError):
    pass


PROVIDERS = {
    "microsoft": {
        "label": "Microsoft 365",
        "authorize": "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize",
        "token": "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
        "scope": (
            "openid profile email offline_access "
            "https://graph.microsoft.com/User.Read "
            "https://graph.microsoft.com/Calendars.ReadWrite "
            "https://graph.microsoft.com/Tasks.ReadWrite"
        ),
    },
    "google": {
        "label": "Google Calendar",
        "authorize": "https://accounts.google.com/o/oauth2/v2/auth",
        "token": "https://oauth2.googleapis.com/token",
        "scope": (
            "openid email profile "
            "https://www.googleapis.com/auth/calendar.events "
            "https://www.googleapis.com/auth/tasks"
        ),
    },
}


def _provider(provider: str) -> dict[str, str]:
    try:
        return PROVIDERS[provider]
    except KeyError as error:
        raise CalendarOAuthError("calendar_provider_invalid") from error


def _credentials(provider: str) -> tuple[str, str]:
    prefix = "MICROSOFT" if provider == "microsoft" else "GOOGLE"
    return (
        str(current_app.config.get(f"{prefix}_CALENDAR_CLIENT_ID", "")).strip(),
        str(current_app.config.get(f"{prefix}_CALENDAR_CLIENT_SECRET", "")).strip(),
    )


def configured(provider: str) -> bool:
    _provider(provider)
    return all(_credentials(provider))


def provider_statuses(user: User) -> list[dict]:
    rows = {
        item.provider: item
        for item in CalendarConnection.query.filter_by(
            organization_id=user.organization_id,
            user_id=user.id,
        ).all()
    }
    statuses = []
    for provider, definition in PROVIDERS.items():
        connection = rows.get(provider)
        scopes = set(connection.scopes.split()) if connection else set()
        tasks_enabled = (
            "https://graph.microsoft.com/Tasks.ReadWrite" in scopes
            if provider == "microsoft"
            else "https://www.googleapis.com/auth/tasks" in scopes
        )
        statuses.append({
            "id": provider,
            "label": definition["label"],
            "configured": configured(provider),
            "connection": connection,
            "write_enabled": bool(
                connection
                and (
                    "https://graph.microsoft.com/Calendars.ReadWrite"
                    in scopes
                    or "https://www.googleapis.com/auth/calendar.events"
                    in scopes
                )
            ),
            "tasks_enabled": tasks_enabled,
            "reconnect_required": bool(
                connection and not tasks_enabled
            ),
            "features": (
                ["Reuniões do Teams", "To Do e Planner", "Convites"]
                if provider == "microsoft"
                else ["Agenda", "Google Tasks", "Convites"]
            ),
        })
    return statuses


def authorization_request(provider: str, redirect_uri: str) -> tuple[str, str, str]:
    definition = _provider(provider)
    client_id, _ = _credentials(provider)
    if not configured(provider):
        raise CalendarOAuthError("calendar_provider_not_configured")
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")
    tenant = str(current_app.config["MICROSOFT_CALENDAR_TENANT"]).strip()
    authorize_url = definition["authorize"].format(tenant=tenant)
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": definition["scope"],
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    if provider == "google":
        params.update(
            access_type="offline",
            include_granted_scopes="true",
            prompt="consent",
        )
    return f"{authorize_url}?{urlencode(params)}", state, verifier


def exchange_code(
    provider: str, code: str, redirect_uri: str, verifier: str
) -> dict:
    definition = _provider(provider)
    client_id, client_secret = _credentials(provider)
    if not client_id or not client_secret:
        raise CalendarOAuthError("calendar_provider_not_configured")
    tenant = str(current_app.config["MICROSOFT_CALENDAR_TENANT"]).strip()
    try:
        response = httpx.post(
            definition["token"].format(tenant=tenant),
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "code": code,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
                "code_verifier": verifier,
            },
            timeout=20,
        )
    except httpx.HTTPError as error:
        raise CalendarOAuthError("calendar_token_unavailable") from error
    if response.status_code >= 400:
        raise CalendarOAuthError("calendar_token_rejected")
    return _clean_token(response.json())


def authorized_account_email(provider: str, token: dict, fallback: str) -> str:
    url = (
        "https://openidconnect.googleapis.com/v1/userinfo"
        if provider == "google"
        else "https://graph.microsoft.com/v1.0/me?$select=mail,userPrincipalName"
    )
    try:
        response = httpx.get(
            url,
            headers={"Authorization": f"Bearer {token['access_token']}"},
            timeout=20,
        )
    except httpx.HTTPError as error:
        raise CalendarOAuthError("calendar_identity_unavailable") from error
    if response.status_code >= 400:
        raise CalendarOAuthError("calendar_identity_rejected")
    payload = response.json()
    email = str(
        payload.get("email")
        or payload.get("mail")
        or payload.get("userPrincipalName")
        or fallback
    ).strip().lower()
    if not email:
        raise CalendarOAuthError("calendar_identity_missing")
    return email


def _clean_token(payload: dict, previous: dict | None = None) -> dict:
    access_token = str(payload.get("access_token") or "")
    if not access_token:
        raise CalendarOAuthError("calendar_token_missing")
    previous = previous or {}
    return {
        "access_token": access_token,
        "refresh_token": str(
            payload.get("refresh_token") or previous.get("refresh_token") or ""
        ),
        "token_type": str(payload.get("token_type") or "Bearer"),
        "expires_at": int(datetime.now(UTC).timestamp())
        + int(payload.get("expires_in") or 3600),
    }


def _fernet() -> Fernet:
    configured_key = str(
        current_app.config.get("CALENDAR_TOKEN_ENCRYPTION_KEY", "")
    ).strip()
    if configured_key:
        try:
            return Fernet(configured_key.encode("ascii"))
        except (ValueError, TypeError) as error:
            raise CalendarOAuthError("calendar_encryption_key_invalid") from error
    secret = str(current_app.config["SECRET_KEY"]).encode("utf-8")
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret).digest()))


def _encrypt(token: dict) -> bytes:
    return _fernet().encrypt(
        json.dumps(token, separators=(",", ":")).encode("utf-8")
    )


def _decrypt(connection: CalendarConnection) -> dict:
    try:
        return json.loads(_fernet().decrypt(connection.token_ciphertext))
    except (InvalidToken, ValueError, TypeError, json.JSONDecodeError) as error:
        raise CalendarOAuthError("calendar_token_unreadable") from error


def save_connection(
    user: User,
    provider: str,
    token: dict,
    account_email: str | None = None,
) -> CalendarConnection:
    definition = _provider(provider)
    connection = CalendarConnection.query.filter_by(
        organization_id=user.organization_id,
        user_id=user.id,
        provider=provider,
    ).first()
    if connection is None:
        connection = CalendarConnection(
            organization_id=user.organization_id,
            user_id=user.id,
            provider=provider,
            account_email=account_email or user.email,
            token_ciphertext=b"",
            scopes=definition["scope"],
        )
        db.session.add(connection)
    connection.account_email = account_email or connection.account_email or user.email
    connection.token_ciphertext = _encrypt(token)
    connection.scopes = definition["scope"]
    connection.status = "connected"
    connection.last_error = None
    return connection


def disconnect(user: User, provider: str) -> bool:
    _provider(provider)
    connection = CalendarConnection.query.filter_by(
        organization_id=user.organization_id,
        user_id=user.id,
        provider=provider,
    ).first()
    if connection is None:
        return False
    db.session.delete(connection)
    return True


def _access_token(connection: CalendarConnection) -> str:
    token = _decrypt(connection)
    if int(token.get("expires_at") or 0) > int(datetime.now(UTC).timestamp()) + 60:
        return str(token["access_token"])
    refresh_token = str(token.get("refresh_token") or "")
    if not refresh_token:
        raise CalendarOAuthError("calendar_refresh_token_missing")
    definition = _provider(connection.provider)
    client_id, client_secret = _credentials(connection.provider)
    tenant = str(current_app.config["MICROSOFT_CALENDAR_TENANT"]).strip()
    try:
        response = httpx.post(
            definition["token"].format(tenant=tenant),
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
                "scope": definition["scope"],
            },
            timeout=20,
        )
    except httpx.HTTPError as error:
        raise CalendarOAuthError("calendar_refresh_unavailable") from error
    if response.status_code >= 400:
        raise CalendarOAuthError("calendar_refresh_rejected")
    token = _clean_token(response.json(), token)
    connection.token_ciphertext = _encrypt(token)
    return str(token["access_token"])


def _iso(value: str | None, timezone_name: str = "UTC") -> str | None:
    if not value:
        return None
    if len(value) == 10:
        return value
    normalized = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None and timezone_name == "UTC":
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.isoformat()


def _microsoft_event(item: dict) -> dict:
    start = item.get("start") or {}
    finish = item.get("end") or {}
    all_day = bool(item.get("isAllDay"))
    starts_at = str(start.get("dateTime") or "")
    ends_at = str(finish.get("dateTime") or "")
    is_teams = (
        item.get("onlineMeetingProvider") == "teamsForBusiness"
        or bool((item.get("onlineMeeting") or {}).get("joinUrl"))
    )
    return {
        "id": f"microsoft-{item['id']}",
        "type": "meeting",
        "title": str(item.get("subject") or "Compromisso"),
        "starts_at": starts_at[:10] if all_day else _iso(starts_at, start.get("timeZone", "UTC")),
        "ends_at": ends_at[:10] if all_day else _iso(ends_at, finish.get("timeZone", "UTC")),
        "all_day": all_day,
        "detail": (
            "Microsoft Teams"
            if is_teams
            else str((item.get("location") or {}).get("displayName") or "Microsoft 365")
        ),
        "critical": False,
        "source": "microsoft",
        "kind_label": "Teams" if is_teams else "Reunião",
        "url": (
            (item.get("onlineMeeting") or {}).get("joinUrl")
            or item.get("webLink")
        ),
    }


def _microsoft_events(token: str, start: datetime, end: datetime) -> list[dict]:
    response = httpx.get(
        "https://graph.microsoft.com/v1.0/me/calendarView",
        headers={
            "Authorization": f"Bearer {token}",
            "Prefer": 'outlook.timezone="UTC"',
        },
        params={
            "startDateTime": start.isoformat(),
            "endDateTime": end.isoformat(),
            "$select": (
                "id,subject,start,end,location,isCancelled,isAllDay,webLink,"
                "isOnlineMeeting,onlineMeetingProvider,onlineMeeting"
            ),
            "$orderby": "start/dateTime",
            "$top": "100",
        },
        timeout=20,
    )
    if response.status_code >= 400:
        raise CalendarOAuthError("calendar_events_rejected")
    return [
        _microsoft_event(item)
        for item in response.json().get("value", [])
        if not item.get("isCancelled") and item.get("id")
    ]


def _microsoft_task(item: dict, source: str, detail: str) -> dict | None:
    due = item.get("dueDateTime")
    due_at = (
        _iso(due.get("dateTime"), due.get("timeZone", "UTC"))
        if isinstance(due, dict)
        else _iso(str(due or ""))
    )
    if (
        not item.get("id")
        or not due_at
        or item.get("status") == "completed"
        or int(item.get("percentComplete") or 0) >= 100
    ):
        return None
    priority = item.get("priority")
    return {
        "id": f"{source}-{item['id']}",
        "type": "task",
        "title": str(item.get("title") or "Tarefa do Microsoft 365"),
        "starts_at": due_at,
        "ends_at": None,
        "all_day": False,
        "detail": detail,
        "critical": item.get("importance") == "high"
        or (isinstance(priority, int) and priority <= 3),
        "source": source,
        "kind_label": "Planner" if source == "microsoft_planner" else "To Do",
        "url": None,
    }


def _microsoft_tasks(token: str, start: datetime, end: datetime) -> list[dict]:
    headers = {
        "Authorization": f"Bearer {token}",
        "Prefer": 'outlook.timezone="UTC"',
    }
    lists_response = httpx.get(
        "https://graph.microsoft.com/v1.0/me/todo/lists",
        headers=headers,
        params={"$select": "id,displayName", "$top": "50"},
        timeout=20,
    )
    if lists_response.status_code >= 400:
        raise CalendarOAuthError("microsoft_tasks_rejected")
    tasks: list[dict] = []
    for task_list in lists_response.json().get("value", []):
        list_id = str(task_list.get("id") or "")
        if not list_id:
            continue
        response = httpx.get(
            f"https://graph.microsoft.com/v1.0/me/todo/lists/{quote(list_id, safe='')}/tasks",
            headers=headers,
            params={
                "$select": "id,title,status,importance,dueDateTime",
                "$top": "100",
            },
            timeout=20,
        )
        if response.status_code >= 400:
            raise CalendarOAuthError("microsoft_tasks_rejected")
        for item in response.json().get("value", []):
            task = _microsoft_task(
                item,
                "microsoft_todo",
                str(task_list.get("displayName") or "Microsoft To Do"),
            )
            if task:
                tasks.append(task)

    planner = httpx.get(
        "https://graph.microsoft.com/v1.0/me/planner/tasks",
        headers=headers,
        timeout=20,
    )
    if planner.status_code not in {403, 404}:
        if planner.status_code >= 400:
            raise CalendarOAuthError("microsoft_tasks_rejected")
        for item in planner.json().get("value", []):
            task = _microsoft_task(item, "microsoft_planner", "Microsoft Planner")
            if task:
                tasks.append(task)

    def in_window(item: dict) -> bool:
        value = datetime.fromisoformat(item["starts_at"].replace("Z", "+00:00"))
        value = value if value.tzinfo else value.replace(tzinfo=UTC)
        return start <= value <= end

    return [item for item in tasks if in_window(item)]


def _google_event(item: dict) -> dict:
    start = item.get("start") or {}
    finish = item.get("end") or {}
    all_day = bool(start.get("date") and not start.get("dateTime"))
    return {
        "id": f"google-{item['id']}",
        "type": "meeting",
        "title": str(item.get("summary") or "Compromisso"),
        "starts_at": _iso(start.get("date") if all_day else start.get("dateTime")),
        "ends_at": _iso(finish.get("date") if all_day else finish.get("dateTime")),
        "all_day": all_day,
        "detail": str(item.get("location") or "Google Calendar"),
        "critical": False,
        "source": "google",
    }


def _google_events(token: str, start: datetime, end: datetime) -> list[dict]:
    response = httpx.get(
        "https://www.googleapis.com/calendar/v3/calendars/primary/events",
        headers={"Authorization": f"Bearer {token}"},
        params={
            "timeMin": start.isoformat(),
            "timeMax": end.isoformat(),
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": "100",
        },
        timeout=20,
    )
    if response.status_code >= 400:
        raise CalendarOAuthError("calendar_events_rejected")
    return [
        _google_event(item)
        for item in response.json().get("items", [])
        if item.get("status") != "cancelled" and item.get("id")
    ]


def _event_post(url: str, **kwargs) -> httpx.Response:
    try:
        return httpx.post(url, **kwargs)
    except httpx.HTTPError as error:
        raise CalendarOAuthError("calendar_event_unavailable") from error

def create_event(
    user: User,
    provider: str,
    *,
    title: str,
    starts_at: datetime,
    duration_minutes: int = 30,
    description: str = "",
    attendee_emails: list[str] | None = None,
    idempotency_key: str | None = None,
    online_meeting: bool = True,
) -> dict:
    _provider(provider)
    connection = CalendarConnection.query.filter_by(
        organization_id=user.organization_id,
        user_id=user.id,
        provider=provider,
        status="connected",
    ).first()
    if connection is None:
        raise CalendarOAuthError("calendar_connection_missing")
    if starts_at.tzinfo is None:
        starts_at = starts_at.replace(tzinfo=UTC)
    ends_at = starts_at + timedelta(minutes=max(15, min(duration_minutes, 480)))
    token = _access_token(connection)
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    attendees = list(
        dict.fromkeys(
            email.strip().lower()
            for email in (attendee_emails or [])
            if email.strip()
        )
    )
    if provider == "google":
        payload = {
            "summary": title[:500],
            "description": description[:8_000],
            "start": {"dateTime": starts_at.isoformat()},
            "end": {"dateTime": ends_at.isoformat()},
            "attendees": [{"email": email} for email in attendees],
        }
        if idempotency_key:
            payload["extendedProperties"] = {
                "private": {"meetmindId": idempotency_key[:200]}
            }
        response = _event_post(
            "https://www.googleapis.com/calendar/v3/calendars/primary/events",
            headers=headers,
            params={"sendUpdates": "all" if attendees else "none"},
            json=payload,
            timeout=20,
        )
        payload = response.json() if response.status_code < 400 else {}
        event = {"id": payload.get("id"), "url": payload.get("htmlLink")}
    else:
        response = _event_post(
            "https://graph.microsoft.com/v1.0/me/events",
            headers=headers,
            json={
                "subject": title[:500],
                "body": {"contentType": "text", "content": description[:8_000]},
                "start": {"dateTime": starts_at.isoformat(), "timeZone": "UTC"},
                "end": {"dateTime": ends_at.isoformat(), "timeZone": "UTC"},
                "attendees": [
                    {
                        "emailAddress": {"address": email},
                        "type": "required",
                    }
                    for email in attendees
                ],
                "isOnlineMeeting": online_meeting,
                **(
                    {"onlineMeetingProvider": "teamsForBusiness"}
                    if online_meeting
                    else {}
                ),
                **({"transactionId": idempotency_key[:255]} if idempotency_key else {}),
            },
            timeout=20,
        )
        payload = response.json() if response.status_code < 400 else {}
        event = {
            "id": payload.get("id"),
            "url": (payload.get("onlineMeeting") or {}).get("joinUrl")
            or payload.get("webLink"),
        }
    if response.status_code >= 400 or not event["id"]:
        raise CalendarOAuthError("calendar_event_rejected")
    return {"provider": provider, **event}

def create_task(
    user: User,
    provider: str,
    *,
    title: str,
    due_at: datetime,
    description: str = "",
) -> dict:
    _provider(provider)
    connection = CalendarConnection.query.filter_by(
        organization_id=user.organization_id,
        user_id=user.id,
        provider=provider,
        status="connected",
    ).first()
    if connection is None:
        raise CalendarOAuthError("calendar_connection_missing")
    if not connection.allow_team_deliveries:
        raise CalendarOAuthError("team_delivery_consent_required")
    scopes = set(connection.scopes.split())
    required_scope = (
        "https://www.googleapis.com/auth/tasks"
        if provider == "google"
        else "https://graph.microsoft.com/Tasks.ReadWrite"
    )
    if required_scope not in scopes:
        raise CalendarOAuthError("calendar_reconnect_required")
    if due_at.tzinfo is None:
        due_at = due_at.replace(tzinfo=UTC)
    token = _access_token(connection)
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    if provider == "google":
        response = _event_post(
            "https://tasks.googleapis.com/tasks/v1/lists/@default/tasks",
            headers=headers,
            json={
                "title": title[:1024],
                "notes": description[:8192],
                "due": due_at.isoformat(),
            },
            timeout=20,
        )
        payload = response.json() if response.status_code < 400 else {}
        task = {"id": payload.get("id"), "url": payload.get("webViewLink")}
    else:
        lists = httpx.get(
            "https://graph.microsoft.com/v1.0/me/todo/lists",
            headers=headers,
            params={"$select": "id,displayName,wellknownListName", "$top": "50"},
            timeout=20,
        )
        if lists.status_code >= 400:
            raise CalendarOAuthError("calendar_task_rejected")
        choices = lists.json().get("value", [])
        selected = next(
            (item for item in choices if item.get("wellknownListName") == "defaultList"),
            choices[0] if choices else None,
        )
        if not selected or not selected.get("id"):
            raise CalendarOAuthError("calendar_task_list_missing")
        list_id = quote(str(selected["id"]), safe="")
        response = _event_post(
            f"https://graph.microsoft.com/v1.0/me/todo/lists/{list_id}/tasks",
            headers=headers,
            json={
                "title": title[:255],
                "body": {"contentType": "text", "content": description[:8000]},
                "dueDateTime": {
                    "dateTime": due_at.astimezone(UTC).replace(tzinfo=None).isoformat(),
                    "timeZone": "UTC",
                },
            },
            timeout=20,
        )
        payload = response.json() if response.status_code < 400 else {}
        task = {"id": payload.get("id"), "url": None}
    if response.status_code >= 400 or not task["id"]:
        raise CalendarOAuthError("calendar_task_rejected")
    return {"provider": provider, **task}


def events_for_user(user: User, provider: str | None = None) -> list[dict]:
    start = utcnow()
    end = start + timedelta(days=int(current_app.config["CALENDAR_SYNC_DAYS"]))
    events: list[dict] = []
    query = CalendarConnection.query.filter_by(
        organization_id=user.organization_id,
        user_id=user.id,
    )
    if provider is not None:
        _provider(provider)
        query = query.filter_by(provider=provider)
    connections = query.all()
    for connection in connections:
        sync_warning = None
        try:
            token = _access_token(connection)
            if connection.provider == "microsoft":
                fetched = _microsoft_events(token, start, end)
                if "https://graph.microsoft.com/Tasks.ReadWrite" in connection.scopes.split():
                    try:
                        fetched.extend(_microsoft_tasks(token, start, end))
                    except (CalendarOAuthError, httpx.HTTPError):
                        sync_warning = "microsoft_tasks_unavailable"
            elif connection.provider == "google":
                fetched = _google_events(token, start, end)
            else:
                raise CalendarOAuthError("calendar_provider_invalid")
            events.extend(item for item in fetched if item.get("starts_at"))
            connection.status = "connected"
            connection.last_error = sync_warning
            connection.last_synced_at = utcnow()
        except (CalendarOAuthError, httpx.HTTPError) as error:
            connection.status = "error"
            connection.last_error = (
                str(error)[:100]
                if isinstance(error, CalendarOAuthError)
                else "calendar_events_unavailable"
            )
    if connections:
        db.session.commit()
    events.sort(key=lambda item: item["starts_at"])
    return events
