from __future__ import annotations


def validate_runtime_config(app) -> None:
    if app.config.get("APP_ENV") != "production":
        return
    errors = []
    role = app.config.get("SERVICE_ROLE", "web")
    portfolio_demo = bool(app.config.get("PORTFOLIO_DEMO_ENABLED"))
    secret = str(app.config.get("SECRET_KEY", ""))
    if len(secret) < 32 or secret == "development-only-change-this-secret-key":
        errors.append("SECRET_KEY forte")
    if role == "web":
        if not portfolio_demo and app.config.get("AUTH_MODE") not in {"oidc", "hybrid"}:
            errors.append("AUTH_MODE=oidc|hybrid")
        if not portfolio_demo and app.config.get("AUTH_MODE") in {"oidc", "hybrid"} and not all(
            app.config.get(key)
            for key in ("OIDC_DISCOVERY_URL", "OIDC_CLIENT_ID", "OIDC_CLIENT_SECRET")
        ):
            errors.append("configuração OIDC completa")
        if portfolio_demo and len(str(app.config.get("DEMO_PORTFOLIO_PASSWORD", ""))) < 16:
            errors.append("DEMO_PORTFOLIO_PASSWORD com 16+ caracteres")
        if not app.config.get("SESSION_COOKIE_SECURE"):
            errors.append("COOKIE_SECURE=true")
        if not app.config.get("PRIVACY_CONTACT_EMAIL"):
            errors.append("PRIVACY_CONTACT_EMAIL")
    if role in {"web", "worker"} and not portfolio_demo:
        if not app.config.get("SUBMISSION_REQUIRE_ANTIVIRUS"):
            errors.append("SUBMISSION_REQUIRE_ANTIVIRUS=true")
        if app.config.get("SUBMISSION_STORAGE_BACKEND") != "gcs":
            errors.append("SUBMISSION_STORAGE_BACKEND=gcs")
        if not app.config.get("GCS_SUBMISSION_BUCKET"):
            errors.append("GCS_SUBMISSION_BUCKET")
    if role == "worker" and not portfolio_demo:
        if app.config.get("AI_PROVIDER") != "vertex_ai":
            errors.append("AI_PROVIDER=vertex_ai")
        if not app.config.get("VERTEX_PROJECT"):
            errors.append("VERTEX_PROJECT")
    if not app.config.get("JSON_LOGS"):
        errors.append("JSON_LOGS=true")
    if errors:
        raise RuntimeError("Configuração de produção incompleta: " + ", ".join(errors))
