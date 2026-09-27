from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import UTC, datetime

from flask import g, request
from flask_login import current_user


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in ("correlation_id", "organization_id", "meeting_id", "analysis_id", "job_id"):
            value = getattr(record, key, None)
            if value:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def configure_observability(app) -> None:
    if app.config.get("JSON_LOGS", False):
        handler = logging.StreamHandler()
        handler.setFormatter(JsonFormatter())
        root = logging.getLogger()
        root.handlers.clear()
        root.addHandler(handler)
        root.setLevel(app.config.get("LOG_LEVEL", "INFO"))
        app.logger.handlers.clear()
        app.logger.propagate = True

    @app.before_request
    def _start_request_trace():
        supplied = request.headers.get("X-Correlation-ID", "")
        g.correlation_id = supplied[:100] if supplied else str(uuid.uuid4())
        g.request_started = time.monotonic()

    @app.after_request
    def _finish_request_trace(response):
        correlation_id = getattr(g, "correlation_id", str(uuid.uuid4()))
        response.headers["X-Correlation-ID"] = correlation_id
        duration_ms = int(
            (time.monotonic() - getattr(g, "request_started", time.monotonic())) * 1000
        )
        if request.path != "/api/v1/health/live":
            app.logger.info(
                "request_completed method=%s path=%s status=%s duration_ms=%s",
                request.method,
                request.path,
                response.status_code,
                duration_ms,
                extra={
                    "correlation_id": correlation_id,
                    "organization_id": (
                        current_user.organization_id if current_user.is_authenticated else None
                    ),
                },
            )
        return response
