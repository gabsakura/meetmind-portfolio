from __future__ import annotations

import os

from google import genai


def provider_name() -> str:
    return os.environ.get("AI_PROVIDER", "gemini_api").strip().lower()


def is_configured() -> bool:
    if provider_name() == "vertex_ai":
        return bool(os.environ.get("VERTEX_PROJECT") or os.environ.get("GOOGLE_CLOUD_PROJECT"))
    return bool(os.environ.get("GEMINI_API_KEY"))


def client():
    if provider_name() == "vertex_ai":
        project = os.environ.get("VERTEX_PROJECT") or os.environ.get("GOOGLE_CLOUD_PROJECT")
        location = os.environ.get("VERTEX_LOCATION", "global")
        if not project:
            raise RuntimeError("VERTEX_PROJECT não configurado")
        return genai.Client(vertexai=True, project=project, location=location)
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        raise RuntimeError("GEMINI_API_KEY não configurada")
    return genai.Client(api_key=key)
