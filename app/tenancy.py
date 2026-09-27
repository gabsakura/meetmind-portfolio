from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from hashlib import sha256

from flask import current_app, g, has_app_context, has_request_context
from sqlalchemy import event
from sqlalchemy.orm import Session, with_loader_criteria

from app.models import DEFAULT_ORGANIZATION_ID, Organization, TenantScopedMixin

_installed = False
_organization_context: ContextVar[str | None] = ContextVar(
    "meetmind_organization_id", default=None
)


def current_organization_id(*, required: bool = False) -> str:
    organization_id = getattr(g, "organization_id", None) if has_request_context() else None
    organization_id = organization_id or _organization_context.get()
    if organization_id:
        return str(organization_id)
    if required or (has_app_context() and current_app.config.get("APP_ENV") == "production"):
        raise RuntimeError("organization_context_required")
    return DEFAULT_ORGANIZATION_ID


def set_organization_context(organization_id: str):
    if not organization_id:
        raise ValueError("organization_id_required")
    return _organization_context.set(str(organization_id))


def reset_organization_context(token) -> None:
    _organization_context.reset(token)


@contextmanager
def tenant_scope(organization_id: str):
    token = set_organization_context(organization_id)
    try:
        yield
    finally:
        reset_organization_context(token)


def install_tenant_guard() -> None:
    """Aplica isolamento ORM e a variável transacional usada pelo PostgreSQL RLS."""
    global _installed
    if _installed:
        return

    def _active_organization_id() -> str | None:
        organization_id = (
            getattr(g, "organization_id", None) if has_request_context() else None
        ) or _organization_context.get()
        return str(organization_id) if organization_id else None

    def _apply_postgresql_rls_context(session: Session) -> str | None:
        """Sincroniza o tenant mesmo quando a transação começou antes do escopo.

        Comandos CLI podem consultar a organização antes de entrar em
        ``tenant_scope``. Nesse caso, o evento ``after_begin`` já ocorreu sem um
        tenant e não será disparado novamente. Reaplicar ``set_config`` antes de
        consultas e flushes mantém o RLS fail-closed sem exigir uma nova sessão.
        """
        organization_id = _active_organization_id()
        if not organization_id:
            return None
        connection = session.connection()
        if connection.dialect.name == "postgresql":
            connection.exec_driver_sql(
                "SELECT set_config('meetmind.organization_id', %s, true)",
                (organization_id,),
            )
        return organization_id

    @event.listens_for(Session, "do_orm_execute")
    def _scope_authenticated_request(execute_state):
        organization_id = _apply_postgresql_rls_context(execute_state.session)
        if not execute_state.is_select:
            return
        if not organization_id or execute_state.execution_options.get("include_all_tenants"):
            return
        execute_state.statement = execute_state.statement.options(
            with_loader_criteria(
                TenantScopedMixin,
                lambda model: model.organization_id == organization_id,
                include_aliases=True,
            )
        )

    @event.listens_for(Session, "before_flush")
    def _scope_pending_writes(session, _flush_context, _instances):
        _apply_postgresql_rls_context(session)

    @event.listens_for(Session, "after_begin")
    def _set_postgresql_rls_context(_session, _transaction, connection):
        organization_id = _active_organization_id()
        if connection.dialect.name == "postgresql" and organization_id:
            connection.exec_driver_sql(
                "SELECT set_config('meetmind.organization_id', %s, true)",
                (str(organization_id),),
            )

    _installed = True


def ensure_default_organization(db) -> Organization:
    organization = db.session.get(Organization, DEFAULT_ORGANIZATION_ID)
    if organization is None:
        organization = Organization(
            id=DEFAULT_ORGANIZATION_ID,
            slug="meetmind-default",
            name="MeetMind",
            retention_days=7,
        )
        db.session.add(organization)
        db.session.flush()
    return organization


def tenant_segment_id(organization_id: str, logical_id: str) -> str:
    """Mantém IDs legados no tenant padrão e evita colisões nos demais."""
    if organization_id == DEFAULT_ORGANIZATION_ID:
        return logical_id[:100]
    prefix = sha256(organization_id.encode()).hexdigest()[:10]
    return f"{prefix}.{logical_id}"[:100]
