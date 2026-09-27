from __future__ import annotations

import os

from flask import (
    Blueprint,
    current_app,
    flash,
    g,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_login import current_user, login_user, logout_user

from app.extensions import db, limiter
from app.models import DEFAULT_ORGANIZATION_ID, Organization, Role, SecurityEvent, User
from app.security import hash_value, mask_ip
from app.tenancy import tenant_scope

auth_bp = Blueprint("auth", __name__, url_prefix="/auth")


def safe_next_url(value: str | None) -> str | None:
    candidate = str(value or "").strip()[:500]
    if not candidate.startswith("/") or candidate.startswith("//") or "\\" in candidate:
        return None
    return candidate


@auth_bp.get("/privacidade")
def privacidade():
    return render_template(
        "privacidade.html",
        contact_email=current_app.config.get("PRIVACY_CONTACT_EMAIL"),
    )


@auth_bp.get("/termos")
def termos():
    return render_template(
        "termos.html",
        contact_email=current_app.config.get("PRIVACY_CONTACT_EMAIL"),
    )


def login_key() -> str:
    email = request.form.get("email", "").strip().lower()[:254]
    return f"{request.remote_addr or 'unknown'}:{email}"


@auth_bp.route("/login", methods=["GET", "POST"])
@limiter.limit("5 per 10 minutes", key_func=login_key, methods=["POST"])
def login():
    next_url = safe_next_url(request.values.get("next"))
    if current_user.is_authenticated:
        return redirect(next_url or url_for("painel.dashboard"))
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        portfolio_demo = bool(current_app.config.get("PORTFOLIO_DEMO_ENABLED"))
        demo_organization = (
            Organization.query.filter_by(slug="meetmind-demo").first()
            if portfolio_demo
            else None
        )
        organization_id = str(session.get("organization_id") or DEFAULT_ORGANIZATION_ID)
        with tenant_scope(organization_id):
            user = User.query.filter_by(organization_id=organization_id, email=email).first()
        if user is None and demo_organization is not None:
            with tenant_scope(demo_organization.id):
                user = User.query.filter_by(
                    organization_id=demo_organization.id,
                    email=email,
                ).first()
        if user is None and current_app.config["APP_ENV"] != "production":
            candidates = []
            organizations = db.session.scalars(
                db.select(Organization).order_by(Organization.created_at.asc())
            ).all()
            for organization in organizations:
                with tenant_scope(organization.id):
                    candidate = User.query.filter_by(
                        organization_id=organization.id, email=email
                    ).first()
                if candidate is not None:
                    candidates.append(candidate)
            user = candidates[0] if len(candidates) == 1 else None
        portfolio_local = False
        if user and current_app.config.get("PORTFOLIO_DEMO_ENABLED"):
            user_organization = db.session.get(Organization, user.organization_id)
            portfolio_local = bool(
                user_organization and user_organization.slug == "meetmind-demo"
            )
        local_allowed = portfolio_local or current_app.config["AUTH_MODE"] == "local" or (
            current_app.config["AUTH_MODE"] == "hybrid"
            and current_app.config["APP_ENV"] != "production"
        )
        break_glass = bool(user and user.is_break_glass)
        if (
            user
            and user.is_active
            and (local_allowed or break_glass)
            and user.check_password(password)
        ):
            login_user(user)
            session["organization_id"] = user.organization_id
            session["session_version"] = user.session_version
            return redirect(next_url or url_for("painel.dashboard"))
        event_organization_id = (
            user.organization_id
            if user
            else demo_organization.id
            if demo_organization is not None
            else DEFAULT_ORGANIZATION_ID
        )
        with tenant_scope(event_organization_id):
            db.session.add(SecurityEvent(
                event_type="LOGIN_FAILED",
                severity="medium",
                user_id=user.id if user else None,
                organization_id=event_organization_id,
                route=request.path,
                masked_ip=mask_ip(request.remote_addr or "unknown"),
                ip_hash=hash_value(request.remote_addr or "unknown"),
                user_agent_hash=hash_value(request.user_agent.string or "unknown"),
                details={},
            ))
            db.session.commit()
        flash("E-mail ou senha inválidos.", "error")
    emergency = request.args.get("emergency") == "1"
    local_enabled = current_app.config["AUTH_MODE"] in {"local", "hybrid"}
    if current_app.config["APP_ENV"] == "production":
        local_enabled = emergency or bool(
            current_app.config.get("PORTFOLIO_DEMO_ENABLED")
        )
    demo_accounts = []
    if current_app.config.get("PORTFOLIO_DEMO_ENABLED"):
        from app.services.demo_seed import DEMO_USERS

        demo_accounts = [
            {"email": email, "name": name.split(" — ", 1)[0]}
            for email, name, _role in DEMO_USERS
        ]
    return render_template(
        "login.html",
        next_url=next_url,
        oidc_enabled=current_app.config["OIDC_ENABLED"],
        local_enabled=local_enabled,
        emergency=emergency,
        demo_accounts=demo_accounts,
    )


@auth_bp.get("/oidc/start")
def oidc_start():
    if not current_app.config["OIDC_ENABLED"]:
        return redirect(url_for("auth.login"))
    client = _oidc_client()
    return client.authorize_redirect(
        url_for("auth.oidc_callback", _external=True),
        nonce=os.urandom(24).hex(),
    )


@auth_bp.get("/oidc/callback")
def oidc_callback():
    if not current_app.config["OIDC_ENABLED"]:
        return redirect(url_for("auth.login"))
    try:
        token = _oidc_client().authorize_access_token()
        claims = token.get("userinfo") or _oidc_client().parse_id_token(token)
    except Exception:
        current_app.logger.exception("oidc_callback_failed")
        flash("Não foi possível validar a conta corporativa.", "error")
        return redirect(url_for("auth.login"))
    email = str(claims.get("preferred_username") or claims.get("email") or "").lower()
    subject = str(claims.get("oid") or claims.get("sub") or "")
    tenant_id = str(claims.get("tid") or "")
    allowed_domain = current_app.config["OIDC_ALLOWED_DOMAIN"].lower().lstrip("@")
    if (
        not email
        or not subject
        or not tenant_id
        or (allowed_domain and not email.endswith(f"@{allowed_domain}"))
    ):
        flash("Conta corporativa não autorizada.", "error")
        return redirect(url_for("auth.login"))
    organization = Organization.query.filter_by(oidc_tenant_id=tenant_id).first()
    if organization is None and current_app.config["APP_ENV"] != "production":
        organization = db.session.get(Organization, DEFAULT_ORGANIZATION_ID)
    if organization is None or not organization.is_active:
        flash("Tenant corporativo não provisionado no MeetMind.", "error")
        return redirect(url_for("auth.login"))
    g.organization_id = organization.id
    session["organization_id"] = organization.id
    with tenant_scope(organization.id):
        user = User.query.filter_by(
            organization_id=organization.id,
            auth_source="oidc",
            auth_subject=subject,
        ).first()
        if user is None:
            user = User.query.filter_by(organization_id=organization.id, email=email).first()
        if user is None and current_app.config["OIDC_JIT_PROVISIONING"]:
            user = User(
                email=email,
                name=str(claims.get("name") or email)[:200],
                role=Role.VIEWER,
                auth_source="oidc",
                auth_subject=subject,
                organization_id=organization.id,
            )
            user.set_password(os.urandom(32).hex())
            db.session.add(user)
    if user is None or not user.is_active:
        flash("Usuário não provisionado no MeetMind.", "error")
        return redirect(url_for("auth.login"))
    groups = {str(item) for item in claims.get("groups", [])}
    if current_app.config["OIDC_ADMIN_GROUP_ID"] in groups:
        user.role = Role.ADMIN
    elif current_app.config["OIDC_MANAGER_GROUP_ID"] in groups:
        user.role = Role.MANAGER
    elif current_app.config["OIDC_ANALYST_GROUP_ID"] in groups:
        user.role = Role.ANALYST
    else:
        user.role = Role.VIEWER
    user.auth_source = "oidc"
    user.auth_subject = subject
    db.session.commit()
    login_user(user)
    session["organization_id"] = organization.id
    session["session_version"] = user.session_version
    return redirect(url_for("painel.dashboard"))


def _oidc_client():
    client = current_app.extensions.get("meetmind_oidc")
    if client is not None:
        return client
    from authlib.integrations.flask_client import OAuth

    oauth = OAuth(current_app)
    client = oauth.register(
        name="entra",
        client_id=current_app.config["OIDC_CLIENT_ID"],
        client_secret=current_app.config["OIDC_CLIENT_SECRET"],
        server_metadata_url=current_app.config["OIDC_DISCOVERY_URL"],
        client_kwargs={"scope": "openid profile email"},
    )
    current_app.extensions["meetmind_oidc"] = client
    return client


@auth_bp.post("/logout")
def logout():
    logout_user()
    session.clear()
    return redirect(url_for("auth.login"))
