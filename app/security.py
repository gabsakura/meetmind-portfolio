from __future__ import annotations

import hashlib
import ipaddress
from collections.abc import Callable
from functools import wraps

from flask import current_app, jsonify, request
from flask_login import current_user
from redis import Redis
from redis.exceptions import RedisError
from werkzeug.middleware.proxy_fix import ProxyFix

from app.extensions import db
from app.models import AuditEvent, Role, SecurityEvent


def hash_value(value: str) -> str:
    pepper = current_app.config["SECRET_KEY"]
    return hashlib.sha256(f"{pepper}:{value}".encode()).hexdigest()


def mask_ip(value: str) -> str:
    try:
        address = ipaddress.ip_address(value)
        if address.version == 4:
            parts = value.split(".")
            return ".".join(parts[:2] + ["x", "x"])
        return f"{address.exploded[:9]}::/48"
    except ValueError:
        return "unknown"


def redis_client() -> Redis:
    return Redis.from_url(
        current_app.config["REDIS_URL"],
        decode_responses=True,
        socket_connect_timeout=2,
        socket_timeout=2,
    )


def security_redis_enabled() -> bool:
    return bool(current_app.config.get("SECURITY_REDIS_ENABLED", True))


def redis_available() -> bool:
    if not security_redis_enabled():
        return False
    try:
        return bool(redis_client().ping())
    except (RedisError, ValueError):
        return False


def audit(action: str, target_type: str, target_id: str | None, **details) -> None:
    event = AuditEvent(
        actor_id=current_user.get_id() if current_user.is_authenticated else None,
        organization_id=(
            current_user.organization_id
            if current_user.is_authenticated
            else current_app.config.get(
                "DEFAULT_ORGANIZATION_ID", "00000000-0000-0000-0000-000000000001"
            )
        ),
        action=action,
        target_type=target_type,
        target_id=target_id,
        details=details,
    )
    db.session.add(event)


def role_required(*roles: Role) -> Callable:
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapped(*args, **kwargs):
            if not current_user.is_authenticated:
                return jsonify(error="Autenticação necessária.", code="unauthorized"), 401
            if current_user.role not in roles:
                return jsonify(error="Recurso não encontrado.", code="not_found"), 404
            return func(*args, **kwargs)

        return wrapped

    return decorator


def sensitive_operation(func: Callable) -> Callable:
    @wraps(func)
    def wrapped(*args, **kwargs):
        if current_app.config.get("TESTING") or not security_redis_enabled():
            return func(*args, **kwargs)
        if not redis_available():
            return (
                jsonify(
                    error="Proteção temporariamente indisponível. Tente novamente.",
                    code="security_backend_unavailable",
                ),
                503,
            )
        return func(*args, **kwargs)

    return wrapped


def record_rate_limit(req, user=None) -> None:
    ip = req.remote_addr or "unknown"
    user_agent = req.user_agent.string or "unknown"
    severity = "medium"
    details: dict[str, object] = {"method": req.method}
    if security_redis_enabled():
        try:
            redis = redis_client()
            key = f"meetmind:violations:{hash_value(ip)}"
            count = redis.incr(key)
            if count == 1:
                redis.expire(key, 3600)
            threshold = max(3, int(current_app.config["RATE_LIMIT_BLOCK_THRESHOLD"]))
            block_seconds = max(60, int(current_app.config["RATE_LIMIT_BLOCK_SECONDS"]))
            details["violation_count"] = count
            if count >= threshold:
                severity = "high"
                redis.setex(f"meetmind:blocked:{hash_value(ip)}", block_seconds, "1")
                details["blocked_for_seconds"] = block_seconds
        except (RedisError, ValueError):
            details["counter_unavailable"] = True
    else:
        details["counter_disabled"] = True

    db.session.add(
        SecurityEvent(
            event_type="RATE_LIMIT_EXCEEDED",
            severity=severity,
            user_id=user.get_id() if user else None,
            organization_id=(
                user.organization_id
                if user
                else "00000000-0000-0000-0000-000000000001"
            ),
            route=req.path[:200],
            masked_ip=mask_ip(ip),
            ip_hash=hash_value(ip),
            user_agent_hash=hash_value(user_agent),
            details=details,
        )
    )
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()


def configure_security(app) -> None:
    trusted = app.config.get("TRUSTED_PROXY_COUNT", 0)
    if trusted:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=trusted, x_proto=trusted)

    @app.before_request
    def block_repeated_abuse():
        if (
            request.endpoint == "static"
            or app.config.get("TESTING")
            or not security_redis_enabled()
        ):
            return None
        try:
            ip_hash = hash_value(request.remote_addr or "unknown")
            if redis_client().exists(f"meetmind:blocked:{ip_hash}"):
                return (
                    jsonify(error="Acesso temporariamente bloqueado.", code="temporarily_blocked"),
                    403,
                )
        except (RedisError, ValueError):
            pass
        return None

    @app.after_request
    def add_headers(response):
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; font-src 'self'; connect-src 'self'; "
            "object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
        )
        response.headers["X-Frame-Options"] = "DENY"
        return response
