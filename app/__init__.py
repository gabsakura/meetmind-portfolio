from flask import Flask, g, jsonify, request, session
from flask_login import current_user
from flask_wtf.csrf import CSRFError
from werkzeug.exceptions import RequestEntityTooLarge


def create_app(config_object="config.Config", config_overrides=None):
    app = Flask(__name__)
    app.config.from_object(config_object)
    if config_overrides:
        app.config.update(config_overrides)

    from app.config_validation import validate_runtime_config

    validate_runtime_config(app)

    from app.extensions import csrf, db, limiter, login_manager, migrate
    db.init_app(app)
    migrate.init_app(app, db)
    login_manager.init_app(app)

    from app.tenancy import (
        install_tenant_guard,
        reset_organization_context,
        set_organization_context,
    )

    install_tenant_guard()

    @app.before_request
    def _bind_organization_context():
        session_organization_id = session.get("organization_id")
        if session_organization_id:
            g.organization_id = str(session_organization_id)
            g.organization_context_token = set_organization_context(g.organization_id)
        if current_user.is_authenticated:
            if session_organization_id and str(current_user.organization_id) != str(
                session_organization_id
            ):
                session.clear()
                raise RuntimeError("session_organization_mismatch")
            if session.get("session_version") not in {None, current_user.session_version}:
                session.clear()
                return jsonify(error="Sessão revogada.", code="session_revoked"), 401
            g.organization_id = str(current_user.organization_id)
            session["organization_id"] = g.organization_id
            if not session_organization_id:
                g.organization_context_token = set_organization_context(g.organization_id)

    @app.teardown_request
    def _clear_organization_context(_error=None):
        token = getattr(g, "organization_context_token", None)
        if token is not None:
            reset_organization_context(token)

    # O upload multipart tem um teto próprio; a folga cobre os demais campos
    # do formulário sem afrouxar o limite global das APIs JSON.
    @app.before_request
    def _submission_upload_limit():
        if request.endpoint == "submission_api.create_submission_endpoint":
            limite = int(app.config.get("MAX_SUBMISSION_BYTES", 25 * 1024 * 1024))
            request.max_content_length = limite + (1024 * 1024)

    @app.before_request
    def _protect_portfolio_demo():
        """Mantém a vitrine sintética sem upload, OAuth ou alterações de perfil."""
        if not current_user.is_authenticated:
            return None
        from app.models import Organization

        organization = db.session.get(Organization, current_user.organization_id)
        if not organization or organization.slug != "meetmind-demo":
            return None
        endpoint = str(request.endpoint or "")
        calendar_endpoints = {
            "painel.perfil_calendario_conectar",
            "painel.perfil_calendario_callback",
            "painel.perfil_calendario_sincronizar",
            "painel.perfil_calendario_desconectar",
        }
        calendar_oauth_available = bool(
            app.config.get("PORTFOLIO_CALENDAR_OAUTH_ENABLED")
            and str(current_user.email).casefold() in {
                str(app.config.get("PORTFOLIO_CALENDAR_TEST_EMAIL", "")).casefold(),
                str(app.config.get("PORTFOLIO_MANAGER_TEST_EMAIL", "")).casefold(),
            }
        )
        if endpoint in calendar_endpoints and calendar_oauth_available:
            return None
        private_operation_accounts = {
            str(app.config.get("PORTFOLIO_CALENDAR_TEST_EMAIL", "")).casefold(),
            str(app.config.get("PORTFOLIO_MANAGER_TEST_EMAIL", "")).casefold(),
        }
        if (
            app.config.get("PORTFOLIO_CALENDAR_OAUTH_ENABLED")
            and str(current_user.email).casefold() in private_operation_accounts
            and request.path.startswith("/api/v1/team-deliveries")
        ):
            return None
        blocked = (
            request.method not in {"GET", "HEAD", "OPTIONS"}
            and endpoint != "auth.logout"
        ) or endpoint in {
            "submission_api.create_submission_endpoint",
            "submission_api.retry_submission",
            "painel.perfil_calendario_conectar",
            "painel.perfil_calendario_callback",
            "painel.perfil_calendario_sincronizar",
            "painel.perfil_calendario_desconectar",
            "painel.perfil_foto_upload",
            "painel.perfil_foto_remover",
        }
        if not blocked:
            return None
        message = "Esta alteração está desativada na demonstração pública."
        if request.path.startswith("/api/"):
            return jsonify(error=message, code="demo_read_only"), 403
        return message, 403

    csrf.init_app(app)
    limiter.init_app(app)

    from app.blueprints.admin.routes import admin_bp
    from app.blueprints.api.chat import chat_api_bp
    from app.blueprints.api.communications import communications_api_bp
    from app.blueprints.api.operations import operations_api_bp
    from app.blueprints.api.routes import api_bp
    from app.blueprints.api.submissions import submission_api_bp
    from app.blueprints.auth.routes import auth_bp
    from app.blueprints.painel.routes import painel_bp
    app.register_blueprint(auth_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(painel_bp)
    app.register_blueprint(api_bp)
    app.register_blueprint(operations_api_bp)
    app.register_blueprint(communications_api_bp)
    app.register_blueprint(chat_api_bp)
    app.register_blueprint(submission_api_bp)

    from app.security import configure_security, record_rate_limit
    configure_security(app)

    from app.observability import configure_observability

    configure_observability(app)

    @login_manager.unauthorized_handler
    def unauthorized():
        if request.path.startswith("/api/"):
            return jsonify(error="Autenticação necessária.", code="unauthorized"), 401
        from flask import redirect, url_for

        return redirect(url_for("auth.login", next=request.path))

    @app.errorhandler(CSRFError)
    def csrf_error(_error):
        if request.path.startswith("/api/"):
            return jsonify(error="Requisição inválida.", code="csrf_failed"), 400
        return "Requisição inválida.", 400

    @app.errorhandler(RequestEntityTooLarge)
    def too_large(_error):
        limite = getattr(request, "max_content_length", None) or app.config[
            "MAX_CONTENT_LENGTH"
        ]
        return (
            jsonify(
                error=f"O corpo excede o limite de {limite // 1048576} MB.",
                code="payload_too_large",
            ),
            413,
        )

    @app.errorhandler(429)
    def rate_limited(error):
        record_rate_limit(request, current_user if current_user.is_authenticated else None)
        response = jsonify(error="Limite temporário excedido.", code="rate_limit_exceeded")
        response.status_code = 429
        response.headers["Retry-After"] = str(getattr(error, "retry_after", 600) or 600)
        return response

    @app.errorhandler(404)
    def not_found(_error):
        if request.path.startswith("/api/"):
            return jsonify(error="Recurso não encontrado.", code="not_found"), 404
        return "Página não encontrada.", 404

    @app.errorhandler(500)
    def server_error(_error):
        db.session.rollback()
        if request.path.startswith("/api/"):
            return jsonify(error="Não foi possível concluir a operação.", code="server_error"), 500
        return "Não foi possível concluir a operação.", 500

    from app.cli import register
    register(app)

    @app.context_processor
    def _demo_environment():
        if not current_user.is_authenticated:
            return {
                "is_demo_environment": False,
                "calendar_oauth_available": False,
                "portfolio_operations_available": False,
            }
        from app.models import Organization

        organization = db.session.get(Organization, current_user.organization_id)
        is_demo = bool(organization and organization.slug == "meetmind-demo")
        return {
            "is_demo_environment": is_demo,
            "calendar_oauth_available": bool(
                not is_demo
                or (
                    app.config.get("PORTFOLIO_CALENDAR_OAUTH_ENABLED")
                    and str(current_user.email).casefold() in {
                        str(app.config.get("PORTFOLIO_CALENDAR_TEST_EMAIL", "")).casefold(),
                        str(app.config.get("PORTFOLIO_MANAGER_TEST_EMAIL", "")).casefold(),
                    }
                )
            ),
            "portfolio_operations_available": bool(
                not is_demo
                or (
                    app.config.get("PORTFOLIO_CALENDAR_OAUTH_ENABLED")
                    and str(current_user.email).casefold() in {
                        str(app.config.get("PORTFOLIO_CALENDAR_TEST_EMAIL", "")).casefold(),
                        str(app.config.get("PORTFOLIO_MANAGER_TEST_EMAIL", "")).casefold(),
                    }
                )
            ),
        }
    return app
