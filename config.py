import os


def normalize_database_url(value: str) -> str:
    """Select the installed psycopg 3 driver for Render-style PostgreSQL URLs."""
    if value.startswith("postgres://"):
        return "postgresql+psycopg://" + value.removeprefix("postgres://")
    if value.startswith("postgresql://"):
        return "postgresql+psycopg://" + value.removeprefix("postgresql://")
    return value


class Config:
    APP_ENV = os.environ.get("APP_ENV", "development")
    SERVICE_ROLE = os.environ.get("SERVICE_ROLE", "web").lower()
    AUTH_MODE = os.environ.get("AUTH_MODE", "local").lower()
    OIDC_ENABLED = AUTH_MODE in {"oidc", "hybrid"}
    OIDC_DISCOVERY_URL = os.environ.get("OIDC_DISCOVERY_URL", "")
    OIDC_CLIENT_ID = os.environ.get("OIDC_CLIENT_ID", "")
    OIDC_CLIENT_SECRET = os.environ.get("OIDC_CLIENT_SECRET", "")
    OIDC_ALLOWED_DOMAIN = os.environ.get("OIDC_ALLOWED_DOMAIN", "")
    OIDC_JIT_PROVISIONING = os.environ.get("OIDC_JIT_PROVISIONING", "false").lower() == "true"
    OIDC_ADMIN_GROUP_ID = os.environ.get("OIDC_ADMIN_GROUP_ID", "")
    OIDC_MANAGER_GROUP_ID = os.environ.get("OIDC_MANAGER_GROUP_ID", "")
    OIDC_ANALYST_GROUP_ID = os.environ.get("OIDC_ANALYST_GROUP_ID", "")
    ANTIVIRUS_ENABLED = os.environ.get("ANTIVIRUS_ENABLED", "false").lower() == "true"
    ANTIVIRUS_HOST = os.environ.get("ANTIVIRUS_HOST", "clamav")
    ANTIVIRUS_PORT = int(os.environ.get("ANTIVIRUS_PORT", "3310"))
    ANTIVIRUS_TIMEOUT_SECONDS = float(os.environ.get("ANTIVIRUS_TIMEOUT_SECONDS", "8"))
    ANTIVIRUS_FAIL_CLOSED = (
        os.environ.get("ANTIVIRUS_FAIL_CLOSED", "true").lower() == "true"
    )
    SECRET_KEY = os.environ.get("SECRET_KEY", "development-only-change-this-secret-key")
    SQLALCHEMY_DATABASE_URI = normalize_database_url(
        os.environ.get("DATABASE_URL", "sqlite:///meetmind-development.sqlite3")
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    CHAT_REDIS_URL = os.environ.get("CHAT_REDIS_URL", "redis://localhost:6380/0")
    RATELIMIT_STORAGE_URI = os.environ.get("RATELIMIT_STORAGE_URI", REDIS_URL)
    RATELIMIT_HEADERS_ENABLED = True
    SENSITIVE_REQUEST_LIMIT_DEFAULT = int(
        os.environ.get("SENSITIVE_REQUEST_LIMIT_DEFAULT", "30")
    )
    RATE_LIMIT_BLOCK_THRESHOLD = int(os.environ.get("RATE_LIMIT_BLOCK_THRESHOLD", "10"))
    RATE_LIMIT_BLOCK_SECONDS = int(os.environ.get("RATE_LIMIT_BLOCK_SECONDS", "3600"))
    # Limite global do corpo da requisição: continua apertado, protegendo
    # todos os endpoints. Só a verificação sobe esse teto, por requisição.
    MAX_CONTENT_LENGTH = 2 * 1024 * 1024
    MAX_SUBMISSION_BYTES = int(
        os.environ.get("MAX_SUBMISSION_BYTES", 25 * 1024 * 1024)
    )
    # Foto de perfil. Fica bem abaixo do MAX_CONTENT_LENGTH global de 2 MB, então
    # o upload passa pelo teto padrão sem precisar de exceção no before_request.
    AVATAR_MAX_BYTES = int(os.environ.get("AVATAR_MAX_BYTES", 1024 * 1024))
    SUBMISSION_STORAGE_DIR = os.environ.get(
        "SUBMISSION_STORAGE_DIR", "instance/submissions"
    )
    SUBMISSION_STORAGE_BACKEND = os.environ.get("SUBMISSION_STORAGE_BACKEND", "filesystem")
    GCS_SUBMISSION_BUCKET = os.environ.get("GCS_SUBMISSION_BUCKET", "")
    GCS_SUBMISSION_PREFIX = os.environ.get("GCS_SUBMISSION_PREFIX", "meetmind/submissions")
    GCS_KMS_KEY_NAME = os.environ.get("GCS_KMS_KEY_NAME", "")
    SUBMISSION_ENCRYPTION_KEY = os.environ.get("SUBMISSION_ENCRYPTION_KEY", "")
    SUBMISSION_RETENTION_DAYS = int(os.environ.get("SUBMISSION_RETENTION_DAYS", "7"))
    SUBMISSION_REQUIRE_ANTIVIRUS = (
        os.environ.get("SUBMISSION_REQUIRE_ANTIVIRUS", "false").lower() == "true"
    )
    TEAMS_GRAPH_ENABLED = os.environ.get("TEAMS_GRAPH_ENABLED", "false").lower() == "true"
    TEAMS_GRAPH_CLIENT_ID = os.environ.get("TEAMS_GRAPH_CLIENT_ID", "")
    TEAMS_GRAPH_CLIENT_SECRET = os.environ.get("TEAMS_GRAPH_CLIENT_SECRET", "")
    TEAMS_GRAPH_CLIENT_STATE = os.environ.get("TEAMS_GRAPH_CLIENT_STATE", "")
    TEAMS_GRAPH_BASE_URL = os.environ.get(
        "TEAMS_GRAPH_BASE_URL", "https://graph.microsoft.com/v1.0"
    ).rstrip("/")
    CALENDAR_TOKEN_ENCRYPTION_KEY = os.environ.get(
        "CALENDAR_TOKEN_ENCRYPTION_KEY", ""
    )
    MICROSOFT_CALENDAR_CLIENT_ID = os.environ.get(
        "MICROSOFT_CALENDAR_CLIENT_ID", ""
    )
    MICROSOFT_CALENDAR_CLIENT_SECRET = os.environ.get(
        "MICROSOFT_CALENDAR_CLIENT_SECRET", ""
    )
    MICROSOFT_CALENDAR_TENANT = os.environ.get(
        "MICROSOFT_CALENDAR_TENANT", "organizations"
    )
    GOOGLE_CALENDAR_CLIENT_ID = os.environ.get("GOOGLE_CALENDAR_CLIENT_ID", "")
    GOOGLE_CALENDAR_CLIENT_SECRET = os.environ.get(
        "GOOGLE_CALENDAR_CLIENT_SECRET", ""
    )
    CALENDAR_SYNC_DAYS = int(os.environ.get("CALENDAR_SYNC_DAYS", "30"))
    PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "http://localhost:8000")
    PRIVACY_CONTACT_EMAIL = os.environ.get("PRIVACY_CONTACT_EMAIL", "").strip()
    DEMO_PORTFOLIO_PASSWORD = os.environ.get("DEMO_PORTFOLIO_PASSWORD", "")
    PORTFOLIO_DEMO_ENABLED = (
        os.environ.get("PORTFOLIO_DEMO_ENABLED", "false").lower() == "true"
    )
    PORTFOLIO_CHAT_ENABLED = (
        os.environ.get("PORTFOLIO_CHAT_ENABLED", "false").lower() == "true"
    )
    PORTFOLIO_CALENDAR_OAUTH_ENABLED = (
        os.environ.get("PORTFOLIO_CALENDAR_OAUTH_ENABLED", "false").lower() == "true"
    )
    SMTP_HOST = os.environ.get("SMTP_HOST", "")
    SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
    SMTP_USERNAME = os.environ.get("SMTP_USERNAME", "")
    SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
    SMTP_FROM = os.environ.get("SMTP_FROM", "")
    SMTP_USE_TLS = os.environ.get("SMTP_USE_TLS", "true").lower() == "true"
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "false").lower() == "true"
    REMEMBER_COOKIE_HTTPONLY = True
    REMEMBER_COOKIE_SAMESITE = "Lax"
    REMEMBER_COOKIE_SECURE = SESSION_COOKIE_SECURE
    TRUSTED_PROXY_COUNT = int(os.environ.get("TRUSTED_PROXY_COUNT", "0"))
    AI_PROVIDER = os.environ.get("AI_PROVIDER", "gemini_api").lower()
    GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")
    VERTEX_PROJECT = os.environ.get("VERTEX_PROJECT", "")
    VERTEX_LOCATION = os.environ.get("VERTEX_LOCATION", "global")
    AI_INPUT_COST_PER_MILLION_MICROUNITS = int(
        os.environ.get("AI_INPUT_COST_PER_MILLION_MICROUNITS", "0")
    )
    AI_OUTPUT_COST_PER_MILLION_MICROUNITS = int(
        os.environ.get("AI_OUTPUT_COST_PER_MILLION_MICROUNITS", "0")
    )
    AI_COST_CURRENCY = os.environ.get("AI_COST_CURRENCY", "USD")[:3].upper()
    # The manufacturing contract is the reliable, single-request production path.
    # The multi-mode bundle remains available as an explicit API opt-in.
    PROMPT_VERSION = "meetmind-manufacturing-v2"
    CONTEXT_VERSION = "sector-playbooks-v1"
    AUTOPSY_ASSISTANT_MODE = os.environ.get("AUTOPSY_ASSISTANT_MODE", "shadow").lower()
    AUTOPSY_CALIBRATION_SAMPLE_SIZE = int(
        os.environ.get("AUTOPSY_CALIBRATION_SAMPLE_SIZE", "40")
    )
    SCORE_VERSION = "voc-deterministic-v2"
    WTF_CSRF_TIME_LIMIT = 3600
    QUEUE_ENABLED = os.environ.get("QUEUE_ENABLED", "true").lower() == "true"
    SECURITY_REDIS_ENABLED = (
        os.environ.get("SECURITY_REDIS_ENABLED", "true").lower() == "true"
    )
    JSON_LOGS = os.environ.get("JSON_LOGS", "false").lower() == "true"
    LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()


class TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite://"
    WTF_CSRF_ENABLED = False
    RATELIMIT_ENABLED = False
    QUEUE_ENABLED = False
    SECURITY_REDIS_ENABLED = False
    SUBMISSION_REQUIRE_ANTIVIRUS = False
    AUTH_MODE = "local"
    OIDC_ENABLED = False
    JSON_LOGS = False
    PRIVACY_CONTACT_EMAIL = "privacy@example.test"
    DEMO_PORTFOLIO_PASSWORD = "test-only-demo-password"
    PORTFOLIO_DEMO_ENABLED = True
