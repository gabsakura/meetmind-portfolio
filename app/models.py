from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from flask_login import UserMixin
from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, declared_attr, mapped_column, relationship
from werkzeug.security import check_password_hash, generate_password_hash

from app.extensions import db, login_manager


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    return str(uuid.uuid4())


DEFAULT_ORGANIZATION_ID = "00000000-0000-0000-0000-000000000001"


class TenantScopedMixin:
    """Marca registros que nunca podem atravessar a fronteira de uma organização."""

    @declared_attr
    def organization_id(cls) -> Mapped[str]:
        return mapped_column(
            ForeignKey("organizations.id"),
            default=DEFAULT_ORGANIZATION_ID,
            index=True,
            nullable=False,
        )


class Organization(db.Model):
    __tablename__ = "organizations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    slug: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    oidc_tenant_id: Mapped[str | None] = mapped_column(String(100), unique=True, index=True)
    retention_days: Mapped[int] = mapped_column(Integer, default=7)
    external_ai_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    legal_hold_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Role(StrEnum):
    ADMIN = "admin"
    MANAGER = "manager"
    ANALYST = "analyst"
    VIEWER = "viewer"


class AnalysisStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class User(TenantScopedMixin, UserMixin, db.Model):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("organization_id", "email", name="uq_users_org_email"),
        UniqueConstraint(
            "organization_id",
            "auth_source",
            "auth_subject",
            name="uq_users_org_auth_identity",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    email: Mapped[str] = mapped_column(String(254), index=True)
    name: Mapped[str] = mapped_column(String(200))
    password_hash: Mapped[str] = mapped_column(String(256))
    role: Mapped[Role] = mapped_column(Enum(Role), default=Role.VIEWER, index=True)
    team_id: Mapped[str | None] = mapped_column(ForeignKey("teams.id"), index=True)
    request_limit_per_10_minutes: Mapped[int] = mapped_column(Integer, default=30)
    auth_source: Mapped[str] = mapped_column(String(20), default="local", index=True)
    auth_subject: Mapped[str | None] = mapped_column(String(200), index=True)
    is_break_glass: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active_record: Mapped[bool] = mapped_column(Boolean, default=True)
    session_version: Mapped[int] = mapped_column(Integer, default=1)
    last_briefing_seen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # deferred: o binário só é lido do banco quando alguém acessa o atributo.
    # Sem isso, cada requisição autenticada carregaria a foto junto do usuário.
    avatar_data: Mapped[bytes | None] = mapped_column(
        LargeBinary, nullable=True, deferred=True
    )
    avatar_mime: Mapped[str | None] = mapped_column(String(40), nullable=True)
    avatar_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    @property
    def is_active(self) -> bool:
        return self.is_active_record

    @property
    def has_avatar(self) -> bool:
        """Consulta barata: olha só o mime, sem trazer o binário."""
        return bool(self.avatar_mime)

    @property
    def avatar_version(self) -> str:
        """Sufixo de cache. Muda a cada troca de foto e invalida a anterior."""
        if not self.avatar_updated_at:
            return "0"
        return str(int(self.avatar_updated_at.timestamp()))

    @property
    def initials(self) -> str:
        partes = (self.name or "").split()
        if not partes:
            return ""
        if len(partes) == 1:
            return partes[0][:1].upper()
        return (partes[0][:1] + partes[-1][:1]).upper()

    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password, method="scrypt")

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)


class Team(TenantScopedMixin, db.Model):
    __tablename__ = "teams"
    __table_args__ = (
        UniqueConstraint("organization_id", "name", name="uq_teams_org_name"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(120), index=True)
    manager_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


@login_manager.user_loader
def load_user(user_id: str) -> User | None:
    return db.session.get(User, user_id)


class Client(TenantScopedMixin, db.Model):
    __tablename__ = "clients"
    __table_args__ = (
        UniqueConstraint("organization_id", "external_id", name="uq_clients_org_external"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    external_id: Mapped[str] = mapped_column(String(100), index=True)
    name: Mapped[str] = mapped_column(String(200), index=True)
    owner_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    meetings: Mapped[list[Meeting]] = relationship(back_populates="client")


class ClientContact(TenantScopedMixin, db.Model):
    __tablename__ = "client_contacts"
    __table_args__ = (
        UniqueConstraint(
            "organization_id", "client_id", "email", name="uq_client_contacts_email"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    client_id: Mapped[str] = mapped_column(ForeignKey("clients.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    email: Mapped[str] = mapped_column(String(254), index=True)
    job_title: Mapped[str | None] = mapped_column(String(160))
    phone: Mapped[str | None] = mapped_column(String(40))
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class Meeting(TenantScopedMixin, db.Model):
    __tablename__ = "meetings"
    __table_args__ = (
        UniqueConstraint("organization_id", "external_id", name="uq_meetings_org_external"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    external_id: Mapped[str] = mapped_column(String(100), index=True)
    title: Mapped[str] = mapped_column(String(200))
    client_id: Mapped[str | None] = mapped_column(ForeignKey("clients.id"), index=True)
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    duration_seconds: Mapped[int | None] = mapped_column(Integer)
    language: Mapped[str] = mapped_column(String(20), default="pt-BR")
    approved_for_external_ai: Mapped[bool] = mapped_column(Boolean, default=False)
    transcript_hash: Mapped[str] = mapped_column(String(64), index=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_by_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    client: Mapped[Client | None] = relationship(back_populates="meetings")
    segments: Mapped[list[Segment]] = relationship(
        back_populates="meeting", cascade="all, delete-orphan", order_by="Segment.position"
    )
    analyses: Mapped[list[Analysis]] = relationship(
        back_populates="meeting", cascade="all, delete-orphan"
    )
    participants: Mapped[list[MeetingParticipant]] = relationship(
        back_populates="meeting", cascade="all, delete-orphan"
    )


class MeetingParticipant(TenantScopedMixin, db.Model):
    """Vincula explicitamente quem pode trabalhar com uma reunião."""

    __tablename__ = "meeting_participants"
    __table_args__ = (
        UniqueConstraint(
            "meeting_id", "user_id", name="uq_meeting_participants_meeting_user"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    meeting_id: Mapped[str] = mapped_column(ForeignKey("meetings.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    participation_role: Mapped[str] = mapped_column(String(30), default="participant")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    meeting: Mapped[Meeting] = relationship(back_populates="participants")


class Segment(TenantScopedMixin, db.Model):
    __tablename__ = "segments"
    __table_args__ = (Index("ix_segments_meeting_position", "meeting_id", "position", unique=True),)

    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    meeting_id: Mapped[str] = mapped_column(ForeignKey("meetings.id"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    speaker: Mapped[str | None] = mapped_column(String(200))
    is_totvs: Mapped[bool] = mapped_column(Boolean, default=False)
    text: Mapped[str] = mapped_column(Text)
    started_at_ms: Mapped[int | None] = mapped_column(Integer)
    ended_at_ms: Mapped[int | None] = mapped_column(Integer)
    meeting: Mapped[Meeting] = relationship(back_populates="segments")


class Analysis(TenantScopedMixin, db.Model):
    __tablename__ = "analyses"
    __table_args__ = (
        Index(
            "ix_analyses_cache_key",
            "transcript_hash",
            "prompt_version",
            "score_version",
            "model",
            "context_sha256",
            "analysis_profile_hash",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    meeting_id: Mapped[str] = mapped_column(ForeignKey("meetings.id"), index=True)
    requested_by_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    status: Mapped[AnalysisStatus] = mapped_column(
        Enum(AnalysisStatus), default=AnalysisStatus.QUEUED, index=True
    )
    model: Mapped[str] = mapped_column(String(100))
    provider: Mapped[str] = mapped_column(String(30), default="gemini_api")
    prompt_version: Mapped[str] = mapped_column(String(50))
    score_version: Mapped[str] = mapped_column(String(50))
    transcript_hash: Mapped[str] = mapped_column(String(64))
    context_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    context_version: Mapped[str | None] = mapped_column(String(50))
    context_sha256: Mapped[str | None] = mapped_column(String(64), index=True)
    mode_strategy: Mapped[str] = mapped_column(String(20), default="auto")
    requested_modes: Mapped[list[str]] = mapped_column(JSON, default=list)
    selected_modes: Mapped[list[str] | None] = mapped_column(JSON)
    mode_bundle_version: Mapped[str] = mapped_column(
        String(50), default="meetmind-manufacturing-v1"
    )
    analysis_profile_hash: Mapped[str] = mapped_column(String(64), default="legacy")
    local_metrics: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(String(100))
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    meeting: Mapped[Meeting] = relationship(back_populates="analyses")


class AnalysisAutopsyRun(TenantScopedMixin, db.Model):
    __tablename__ = "analysis_autopsy_runs"
    __table_args__ = (
        UniqueConstraint(
            "analysis_id", "bundle_version", name="uq_autopsy_analysis_bundle"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    analysis_id: Mapped[str] = mapped_column(ForeignKey("analyses.id"), index=True)
    mode: Mapped[str] = mapped_column(String(20), index=True)
    status: Mapped[str] = mapped_column(String(30), default="running", index=True)
    bundle_version: Mapped[str] = mapped_column(String(80), index=True)
    bundle_sha256: Mapped[str] = mapped_column(String(64), index=True)
    source_references: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    path_id: Mapped[str | None] = mapped_column(String(100), index=True)
    questions: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    quality_flags: Mapped[list[str]] = mapped_column(JSON, default=list)
    baseline_result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    guided_review: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    assisted_result: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    disagreements: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    format_repair_count: Mapped[int] = mapped_column(Integer, default=0)
    error_code: Mapped[str | None] = mapped_column(String(100))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

class AuditEvent(TenantScopedMixin, db.Model):
    __tablename__ = "audit_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    actor_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), index=True)
    action: Mapped[str] = mapped_column(String(100), index=True)
    target_type: Mapped[str] = mapped_column(String(100))
    target_id: Mapped[str | None] = mapped_column(String(100))
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SecurityEvent(TenantScopedMixin, db.Model):
    __tablename__ = "security_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    event_type: Mapped[str] = mapped_column(String(100), index=True)
    severity: Mapped[str] = mapped_column(String(20), index=True)
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), index=True)
    route: Mapped[str] = mapped_column(String(200))
    masked_ip: Mapped[str] = mapped_column(String(100))
    ip_hash: Mapped[str] = mapped_column(String(64), index=True)
    user_agent_hash: Mapped[str] = mapped_column(String(64))
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    acknowledged_by_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SubmissionStatus(StrEnum):
    UPLOADED = "uploaded"
    SCREENING = "screening"
    ANALYSIS_QUEUED = "analysis_queued"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    FAILED = "failed"
    EXPIRED = "expired"


class MeetingSubmission(TenantScopedMixin, db.Model):
    __tablename__ = "meeting_submissions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    status: Mapped[str] = mapped_column(
        String(32), default=SubmissionStatus.UPLOADED.value, index=True
    )
    created_by_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    meeting_id: Mapped[str | None] = mapped_column(ForeignKey("meetings.id"), index=True)
    analysis_id: Mapped[str | None] = mapped_column(ForeignKey("analyses.id"), index=True)

    original_filename: Mapped[str] = mapped_column(String(255))
    file_format: Mapped[str] = mapped_column(String(20), index=True)
    content_type: Mapped[str] = mapped_column(String(120))
    byte_size: Mapped[int] = mapped_column(Integer)
    file_sha256: Mapped[str] = mapped_column(String(64), index=True)
    protocol_version: Mapped[str] = mapped_column(
        String(50), default="meetmind-transcript-v1"
    )
    canonical_sha256: Mapped[str | None] = mapped_column(String(64), index=True)
    stored_object_key: Mapped[str | None] = mapped_column(String(100), unique=True)
    retained_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    original_deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    consent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    proposed_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    parser_report: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    mapping_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    sanitized_preview: Mapped[str | None] = mapped_column(Text)
    guard_report: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    antivirus_report: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error_code: Mapped[str | None] = mapped_column(String(100))
    notification_status: Mapped[str] = mapped_column(
        String(30), default="not_requested", index=True
    )
    notification_attempts: Mapped[int] = mapped_column(Integer, default=0)
    notification_error: Mapped[str | None] = mapped_column(String(200))
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, default=1)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class AIExecution(TenantScopedMixin, db.Model):
    """Registro imutável de uma tentativa de geração para auditoria e avaliação."""

    __tablename__ = "ai_executions"
    __table_args__ = (
        UniqueConstraint("analysis_id", "attempt", name="uq_ai_executions_analysis_attempt"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    analysis_id: Mapped[str] = mapped_column(ForeignKey("analyses.id"), index=True)
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    provider: Mapped[str] = mapped_column(String(30))
    model: Mapped[str] = mapped_column(String(100))
    prompt_version: Mapped[str] = mapped_column(String(50))
    schema_sha256: Mapped[str] = mapped_column(String(64))
    transcript_sha256: Mapped[str] = mapped_column(String(64))
    context_sha256: Mapped[str | None] = mapped_column(String(64))
    parameters: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="running", index=True)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    estimated_cost_microunits: Mapped[int | None] = mapped_column(Integer)
    cost_currency: Mapped[str] = mapped_column(String(3), default="USD")
    result_sha256: Mapped[str | None] = mapped_column(String(64))
    error_code: Mapped[str | None] = mapped_column(String(100))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PrivacyRequest(TenantScopedMixin, db.Model):
    __tablename__ = "privacy_requests"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    requested_by_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    request_type: Mapped[str] = mapped_column(String(30), index=True)
    status: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    reason: Mapped[str] = mapped_column(Text)
    resolution: Mapped[str | None] = mapped_column(Text)
    approved_by_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OutboxEvent(TenantScopedMixin, db.Model):
    __tablename__ = "outbox_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    queue_name: Mapped[str] = mapped_column(String(50), index=True)
    task_name: Mapped[str] = mapped_column(String(100))
    job_id: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    arguments: Mapped[list[Any]] = mapped_column(JSON, default=list)
    retry_intervals: Mapped[list[int]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    last_error: Mapped[str | None] = mapped_column(String(100))
    correlation_id: Mapped[str | None] = mapped_column(String(100), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PlanStatus(StrEnum):
    AWAITING_REVIEW = "awaiting_review"
    ACTIVE = "active"
    COMPLETED = "completed"
    DISMISSED = "dismissed"


class MeetingPlan(TenantScopedMixin, db.Model):
    __tablename__ = "meeting_plans"
    __table_args__ = (
        UniqueConstraint("analysis_id", name="uq_meeting_plans_analysis"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    client_id: Mapped[str | None] = mapped_column(ForeignKey("clients.id"), index=True)
    meeting_id: Mapped[str] = mapped_column(ForeignKey("meetings.id"), index=True)
    analysis_id: Mapped[str] = mapped_column(ForeignKey("analyses.id"), index=True)
    version_number: Mapped[int] = mapped_column(Integer, default=1)
    objective: Mapped[str] = mapped_column(Text)
    situation: Mapped[str] = mapped_column(Text)
    recommendation: Mapped[str] = mapped_column(Text)
    evidence_segment_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(
        String(30), default=PlanStatus.AWAITING_REVIEW.value, index=True
    )
    reviewed_by_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class ActionStatus(StrEnum):
    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    DISMISSED = "dismissed"


class ActionItem(TenantScopedMixin, db.Model):
    __tablename__ = "action_items"
    __table_args__ = (
        UniqueConstraint("analysis_id", "source_index", name="uq_actions_analysis_source"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    client_id: Mapped[str | None] = mapped_column(ForeignKey("clients.id"), index=True)
    meeting_id: Mapped[str] = mapped_column(ForeignKey("meetings.id"), index=True)
    analysis_id: Mapped[str] = mapped_column(ForeignKey("analyses.id"), index=True)
    plan_id: Mapped[str | None] = mapped_column(ForeignKey("meeting_plans.id"), index=True)
    parent_action_id: Mapped[str | None] = mapped_column(
        ForeignKey("action_items.id"), index=True
    )
    source_index: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(500))
    rationale: Mapped[str] = mapped_column(Text)
    urgency: Mapped[str] = mapped_column(String(30), default="monitor", index=True)
    priority: Mapped[str] = mapped_column(String(20), default="low", index=True)
    evidence_segment_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    owner_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), index=True)
    status: Mapped[str] = mapped_column(
        String(30), default=ActionStatus.PROPOSED.value, index=True
    )
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    outcome: Mapped[str | None] = mapped_column(Text)
    customer_notified: Mapped[bool] = mapped_column(Boolean, default=False)
    dismissal_reason: Mapped[str | None] = mapped_column(Text)
    accepted_by_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"))
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    external_system: Mapped[str | None] = mapped_column(String(50))
    external_id: Mapped[str | None] = mapped_column(String(200))
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class ActionEvent(TenantScopedMixin, db.Model):
    __tablename__ = "action_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    action_item_id: Mapped[str] = mapped_column(ForeignKey("action_items.id"), index=True)
    actor_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), index=True)
    event_type: Mapped[str] = mapped_column(String(50), index=True)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class KanbanRequestStatus(StrEnum):
    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    ANSWERED = "answered"
    CLOSED = "closed"
    CANCELLED = "cancelled"
    DELIVERED = "delivered"
    FAILED = "failed"


class KanbanRequest(TenantScopedMixin, db.Model):
    __tablename__ = "kanban_requests"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    client_id: Mapped[str | None] = mapped_column(ForeignKey("clients.id"), index=True)
    source_action_id: Mapped[str | None] = mapped_column(
        ForeignKey("action_items.id"), index=True
    )
    meeting_id: Mapped[str | None] = mapped_column(ForeignKey("meetings.id"), index=True)
    analysis_id: Mapped[str | None] = mapped_column(ForeignKey("analyses.id"), index=True)
    source_kind: Mapped[str] = mapped_column(String(20), default="meeting", index=True)
    source_channel: Mapped[str | None] = mapped_column(String(30))
    request_type: Mapped[str] = mapped_column(String(50), index=True)
    department: Mapped[str] = mapped_column(String(100), index=True)
    priority: Mapped[str] = mapped_column(String(20), index=True)
    note: Mapped[str | None] = mapped_column(Text)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_by_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    assigned_to_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id"), index=True
    )
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    response: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        String(20), default=KanbanRequestStatus.QUEUED.value, index=True
    )
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    customer_returned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    customer_returned_by_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"))
    customer_return_not_required_reason: Mapped[str | None] = mapped_column(Text)


class CommunicationDraftStatus(StrEnum):
    DRAFT = "draft"
    REVIEWED = "reviewed"
    APPROVED = "approved"
    COPIED = "copied"
    SCHEDULED = "scheduled"
    SENT_CONFIRMED = "sent_confirmed"
    DISCARDED = "discarded"


class CommunicationDraft(TenantScopedMixin, db.Model):
    __tablename__ = "communication_drafts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    client_id: Mapped[str] = mapped_column(ForeignKey("clients.id"), index=True)
    meeting_id: Mapped[str | None] = mapped_column(ForeignKey("meetings.id"), index=True)
    action_id: Mapped[str | None] = mapped_column(ForeignKey("action_items.id"), index=True)
    kanban_request_id: Mapped[str | None] = mapped_column(
        ForeignKey("kanban_requests.id"), index=True
    )
    draft_type: Mapped[str] = mapped_column(String(40), index=True)
    channel: Mapped[str] = mapped_column(String(30), index=True)
    status: Mapped[str] = mapped_column(
        String(30), default=CommunicationDraftStatus.DRAFT.value, index=True
    )
    recipient_contact_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    current_revision: Mapped[int] = mapped_column(Integer, default=1)
    approved_revision: Mapped[int | None] = mapped_column(Integer)
    created_by_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    approved_by_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_by_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    external_provider: Mapped[str | None] = mapped_column(String(30))
    external_id: Mapped[str | None] = mapped_column(String(200))
    external_url: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class CommunicationRevision(TenantScopedMixin, db.Model):
    __tablename__ = "communication_revisions"
    __table_args__ = (
        UniqueConstraint(
            "communication_draft_id",
            "revision_number",
            name="uq_communication_revision_number",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    communication_draft_id: Mapped[str] = mapped_column(
        ForeignKey("communication_drafts.id"), index=True
    )
    revision_number: Mapped[int] = mapped_column(Integer)
    subject: Mapped[str] = mapped_column(String(500))
    body: Mapped[str] = mapped_column(Text)
    evidence_segment_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    generated_by_ai: Mapped[bool] = mapped_column(Boolean, default=False)
    generation_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_by_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class EvidenceIssue(TenantScopedMixin, db.Model):
    __tablename__ = "evidence_issues"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    analysis_id: Mapped[str] = mapped_column(ForeignKey("analyses.id"), index=True)
    segment_id: Mapped[str] = mapped_column(ForeignKey("segments.id"), index=True)
    reporter_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    category: Mapped[str] = mapped_column(String(40), index=True)
    comment: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AnalysisFeedback(TenantScopedMixin, db.Model):
    __tablename__ = "analysis_feedback"
    __table_args__ = (
        UniqueConstraint("analysis_id", "reviewer_id", name="uq_feedback_analysis_reviewer"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    analysis_id: Mapped[str] = mapped_column(ForeignKey("analyses.id"), index=True)
    reviewer_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    usefulness_rating: Mapped[int] = mapped_column(Integer)
    factual_accuracy: Mapped[str] = mapped_column(String(30), index=True)
    action_useful: Mapped[bool] = mapped_column(Boolean)
    correction: Mapped[str | None] = mapped_column(Text)
    comment: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class ProductEvent(TenantScopedMixin, db.Model):
    __tablename__ = "product_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    event_name: Mapped[str] = mapped_column(String(50), index=True)
    target_type: Mapped[str | None] = mapped_column(String(50))
    target_id: Mapped[str | None] = mapped_column(String(100), index=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PrivacyExecution(TenantScopedMixin, db.Model):
    __tablename__ = "privacy_executions"
    __table_args__ = (
        UniqueConstraint("privacy_request_id", "attempt", name="uq_privacy_execution_attempt"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    privacy_request_id: Mapped[str] = mapped_column(
        ForeignKey("privacy_requests.id"), index=True
    )
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(30), default="running", index=True)
    manifest: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    manifest_sha256: Mapped[str | None] = mapped_column(String(64))
    error_code: Mapped[str | None] = mapped_column(String(100))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TeamsConnection(db.Model):
    """Metadados de controle; segredos permanecem fora do banco."""

    __tablename__ = "teams_connections"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id"), unique=True, index=True
    )
    tenant_id: Mapped[str] = mapped_column(String(100), index=True)
    teams_app_id: Mapped[str] = mapped_column(String(100))
    subscription_id: Mapped[str | None] = mapped_column(String(200), unique=True, index=True)
    subscription_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(30), default="not_configured", index=True)
    last_error: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class CalendarConnection(TenantScopedMixin, db.Model):
    __tablename__ = "calendar_connections"
    __table_args__ = (
        UniqueConstraint(
            "organization_id", "user_id", "provider",
            name="uq_calendar_connection_user_provider",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    provider: Mapped[str] = mapped_column(String(20), index=True)
    account_email: Mapped[str | None] = mapped_column(String(254))
    token_ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    scopes: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="connected", index=True)
    last_error: Mapped[str | None] = mapped_column(String(100))
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class TeamsTranscriptReceipt(TenantScopedMixin, db.Model):
    __tablename__ = "teams_transcript_receipts"
    __table_args__ = (
        UniqueConstraint(
            "organization_id", "transcript_id", name="uq_teams_receipt_org_transcript"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    transcript_id: Mapped[str] = mapped_column(String(200), index=True)
    meeting_id: Mapped[str | None] = mapped_column(String(200), index=True)
    resource_url: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), default="received", index=True)
    submission_id: Mapped[str | None] = mapped_column(
        ForeignKey("meeting_submissions.id"), index=True
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error_code: Mapped[str | None] = mapped_column(String(100))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
