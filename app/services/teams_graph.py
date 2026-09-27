from __future__ import annotations

from datetime import timedelta

import httpx
from flask import current_app

from app.models import TeamsConnection, utcnow


class TeamsGraphError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def configured() -> bool:
    return bool(
        current_app.config["TEAMS_GRAPH_ENABLED"]
        and current_app.config["TEAMS_GRAPH_CLIENT_ID"]
        and current_app.config["TEAMS_GRAPH_CLIENT_SECRET"]
        and current_app.config["TEAMS_GRAPH_CLIENT_STATE"]
    )


def _token(connection: TeamsConnection) -> str:
    if not configured():
        raise TeamsGraphError("teams_graph_not_configured")
    response = httpx.post(
        f"https://login.microsoftonline.com/{connection.tenant_id}/oauth2/v2.0/token",
        data={
            "client_id": current_app.config["TEAMS_GRAPH_CLIENT_ID"],
            "client_secret": current_app.config["TEAMS_GRAPH_CLIENT_SECRET"],
            "scope": "https://graph.microsoft.com/.default",
            "grant_type": "client_credentials",
        },
        timeout=15,
    )
    if response.status_code in {401, 403}:
        raise TeamsGraphError("teams_graph_auth_rejected")
    if response.status_code >= 400:
        raise TeamsGraphError("teams_graph_token_failed")
    token = str(response.json().get("access_token") or "")
    if not token:
        raise TeamsGraphError("teams_graph_token_missing")
    return token


def _headers(connection: TeamsConnection) -> dict[str, str]:
    return {"Authorization": f"Bearer {_token(connection)}"}


def probe_credentials(connection: TeamsConnection) -> bool:
    return bool(_token(connection))


def create_or_renew_subscription(connection: TeamsConnection) -> dict:
    base = current_app.config["TEAMS_GRAPH_BASE_URL"]
    expires = utcnow() + timedelta(hours=1)
    payload = {
        "changeType": "created",
        "notificationUrl": (
            current_app.config["PUBLIC_BASE_URL"].rstrip("/")
            + "/api/v1/integrations/teams/notifications"
        ),
        "lifecycleNotificationUrl": (
            current_app.config["PUBLIC_BASE_URL"].rstrip("/")
            + "/api/v1/integrations/teams/notifications"
        ),
        "resource": (
            f"appCatalogs/teamsApps/{connection.teams_app_id}"
            "/installedToOnlineMeetings/getAllTranscripts"
        ),
        "expirationDateTime": expires.isoformat(),
        "clientState": current_app.config["TEAMS_GRAPH_CLIENT_STATE"],
    }
    if connection.subscription_id:
        response = httpx.patch(
            f"{base}/subscriptions/{connection.subscription_id}",
            headers={**_headers(connection), "Content-Type": "application/json"},
            json={"expirationDateTime": expires.isoformat()},
            timeout=20,
        )
    else:
        response = httpx.post(
            f"{base}/subscriptions",
            headers={**_headers(connection), "Content-Type": "application/json"},
            json=payload,
            timeout=20,
        )
    if response.status_code in {401, 403}:
        raise TeamsGraphError("teams_graph_permission_denied")
    if response.status_code == 429:
        raise TeamsGraphError("teams_graph_rate_limited")
    if response.status_code >= 400:
        raise TeamsGraphError("teams_graph_subscription_failed")
    return response.json()


def download_transcript(connection: TeamsConnection, resource_url: str) -> bytes:
    resource = resource_url.lstrip("/")
    if not (
        resource.startswith("communications/onlineMeetings/")
        or resource.startswith("appCatalogs/teamsApps/")
        or resource.startswith("users/")
    ):
        raise TeamsGraphError("teams_graph_resource_rejected")
    base = current_app.config["TEAMS_GRAPH_BASE_URL"]
    url = f"{base}/{resource}"
    if not url.endswith("/content"):
        url += "/content"
    response = httpx.get(
        url,
        headers={**_headers(connection), "Accept": "text/vtt, text/plain"},
        timeout=30,
    )
    if response.status_code in {401, 403}:
        raise TeamsGraphError("teams_graph_permission_denied")
    if response.status_code == 404:
        raise TeamsGraphError("teams_transcript_not_available")
    if response.status_code == 429:
        raise TeamsGraphError("teams_graph_rate_limited")
    if response.status_code >= 400:
        raise TeamsGraphError("teams_graph_download_failed")
    if not response.content:
        raise TeamsGraphError("teams_transcript_empty")
    return response.content
