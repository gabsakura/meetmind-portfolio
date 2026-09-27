from __future__ import annotations

from datetime import date
from hashlib import sha256
from importlib.metadata import PackageNotFoundError, version

import click
import httpx
from google.genai import types
from google.genai.errors import ClientError, ServerError
from redis.exceptions import RedisError
from sqlalchemy.exc import SQLAlchemyError

from app.extensions import db
from app.models import (
    ActionItem,
    Analysis,
    AnalysisStatus,
    AuditEvent,
    Client,
    Meeting,
    Organization,
    Role,
    SecurityEvent,
    Segment,
    TeamsConnection,
    User,
)
from app.security import hash_value, redis_client
from app.services.actions import create_drafts
from app.services.ai_provider import client as ai_client
from app.services.ai_provider import is_configured as ai_is_configured
from app.services.ai_provider import provider_name
from app.services.gemini import GEMINI_RESPONSE_SCHEMA_SHA256, safe_provider_error
from app.services.importer import import_enriched_json
from app.services.metrics import calculate_metrics
from app.services.outbox import add as add_outbox_event
from app.services.outbox import dispatch as dispatch_outbox_event
from app.services.sector_context import (
    PLAYBOOK_DIR,
    build_context_snapshot,
    load_playbooks,
    validate_playbooks,
)
from app.tenancy import ensure_default_organization, tenant_scope


def register(app) -> None:
    def cli_organization(slug: str | None = None) -> Organization:
        if slug:
            organization = Organization.query.filter_by(slug=slug.strip().lower()).first()
            if organization is None:
                raise click.ClickException("Organização não encontrada.")
            return organization
        if app.config["APP_ENV"] == "production":
            raise click.ClickException("Informe --organization-slug em produção.")
        return ensure_default_organization(db)

    @app.cli.command("seed-demo")
    def seed_demo_command():
        """Cria contas e dados sintéticos para a demonstração isolada."""
        production_demo = (
            app.config["APP_ENV"] == "production"
            and app.config.get("PORTFOLIO_DEMO_ENABLED")
        )
        if app.config["APP_ENV"] == "production" and not production_demo:
            raise click.ClickException(
                "O conjunto de demonstração só pode ser criado em produção "
                "quando PORTFOLIO_DEMO_ENABLED=true."
            )
        from app.services.demo_seed import DEMO_USERS, generate_demo_password, seed_demo

        password = app.config.get("DEMO_PORTFOLIO_PASSWORD") or generate_demo_password()
        report = seed_demo(password=password)
        click.echo(
            f"Demonstração pronta: {report['users']} usuários; "
            f"{report['meetings_created']} novas reuniões."
        )
        for email, name, _role in DEMO_USERS:
            click.echo(f"- {name}: {email}")
        if not production_demo:
            click.echo(f"Senha temporária (não publique): {password}")

    @app.cli.command("reset-demo")
    @click.option(
        "--confirm",
        is_flag=True,
        help="Confirma a reconstrução exclusiva da organização meetmind-demo.",
    )
    def reset_demo_command(confirm: bool):
        """Reconstrói dados sintéticos com datas relativas ao momento atual."""
        if app.config["APP_ENV"] == "production":
            raise click.ClickException(
                "A demonstração não pode ser reconstruída em produção."
            )
        if not confirm:
            raise click.ClickException("Use --confirm para executar o reset.")
        from app.services.demo_seed import generate_demo_password, reset_demo

        password = app.config.get("DEMO_PORTFOLIO_PASSWORD") or generate_demo_password()
        report = reset_demo(password=password)
        click.echo(
            f"Demonstração reconstruída: {report.get('meetings_removed', 0)} removidas; "
            f"{report['meetings_created']} reuniões atuais criadas; "
            f"{report.get('calendar_connections_removed', 0)} conexões pessoais removidas."
        )
        click.echo(f"Senha temporária (não publique): {password}")

    @app.cli.command("validate-analysis-playbooks")
    def validate_analysis_playbooks():
        try:
            report = validate_playbooks()
        except (OSError, ValueError) as error:
            raise click.ClickException(str(error)) from error
        click.echo(
            f"Playbooks válidos: {report['count']}; fontes: {report['sources']}; "
            f"versão: {report['version']}."
        )
        click.echo("Setores: " + ", ".join(report["ids"]))

    @app.cli.command("explain-analysis-context")
    @click.option("--meeting-id", required=True, help="ID interno da reunião.")
    @click.option("--organization-slug")
    def explain_analysis_context(meeting_id: str, organization_slug: str | None):
        organization = cli_organization(organization_slug)
        with tenant_scope(organization.id):
            meeting = db.session.get(Meeting, meeting_id)
            if meeting is None:
                raise click.ClickException("Reunião não encontrada.")
            snapshot = build_context_snapshot(meeting)
        click.echo(f"Playbook: {snapshot['playbook']['label']} ({snapshot['playbook']['id']})")
        click.echo(f"Origem: {snapshot['selection']['source']}")
        click.echo(f"Subsetor: {snapshot['selection']['subsector'] or '-'}")
        click.echo(f"Completude: {snapshot['account']['completeness']:.0%}")
        click.echo(f"Hash: {snapshot['context_sha256']}")
        if snapshot["account"]["missing_fields"]:
            click.echo("Campos ausentes: " + ", ".join(snapshot["account"]["missing_fields"]))
        for uncertainty in snapshot["uncertainties"]:
            click.echo(f"Aviso: {uncertainty}")

    @app.cli.command("audit-analysis-playbooks")
    def audit_analysis_playbooks():
        try:
            validate_playbooks()
            playbooks = load_playbooks()
        except (OSError, ValueError) as error:
            raise click.ClickException(str(error)) from error
        today = date.today()
        stale = []
        click.echo("Playbook | revisão | idade | SHA-256")
        for playbook_id, playbook in sorted(playbooks.items()):
            path = PLAYBOOK_DIR / f"{playbook_id}.json"
            digest = sha256(path.read_bytes()).hexdigest()
            age = (today - playbook.reviewed_at).days
            click.echo(f"{playbook_id} | {playbook.reviewed_at} | {age}d | {digest}")
            if age > 365:
                stale.append(playbook_id)
        if stale:
            raise click.ClickException(
                "Fontes precisam de revisão em: " + ", ".join(stale)
            )
        click.echo("Auditoria concluída: nenhuma fonte vencida ou estrutura incompleta.")

    @app.cli.command("database-status")
    @click.option("--organization-slug")
    def database_status(organization_slug: str | None):
        organization = cli_organization(organization_slug)
        url = db.engine.url.render_as_string(hide_password=True)
        click.echo(f"Banco: {url}")
        with tenant_scope(organization.id):
            click.echo(
                "Registros: "
                f"{db.session.query(Meeting).count()} reuniões; "
                f"{db.session.query(Segment).count()} segmentos; "
                f"{db.session.query(Client).count()} clientes; "
                f"{db.session.query(User).count()} usuários; "
                f"{db.session.query(Analysis).count()} análises."
            )

    @app.cli.command("create-admin")
    @click.option("--email", prompt=True)
    @click.option("--name", prompt=True)
    @click.password_option(confirmation_prompt=True)
    @click.option("--break-glass", is_flag=True, help="Autoriza login local de emergência.")
    @click.option("--organization-slug")
    def create_admin(
        email: str,
        name: str,
        password: str,
        break_glass: bool,
        organization_slug: str | None,
    ):
        if len(password) < 12:
            raise click.ClickException("A senha deve ter ao menos 12 caracteres.")
        normalized_email = email.strip().lower()
        organization = cli_organization(organization_slug)
        with tenant_scope(organization.id):
            if User.query.filter_by(email=normalized_email).first():
                raise click.ClickException("O e-mail já está cadastrado na organização.")
            user = User(
                email=normalized_email,
                name=name,
                role=Role.ADMIN,
                organization_id=organization.id,
                is_break_glass=break_glass,
            )
            user.set_password(password)
            db.session.add(user)
            db.session.commit()
        click.echo(f"Administrador criado: {normalized_email}")

    @app.cli.command("create-organization")
    @click.option("--slug", required=True)
    @click.option("--name", required=True)
    @click.option("--retention-days", type=click.IntRange(1, 3650), default=7)
    def create_organization(slug: str, name: str, retention_days: int):
        normalized = slug.strip().lower()
        if Organization.query.filter_by(slug=normalized).first():
            raise click.ClickException("A organização já existe.")
        organization = Organization(
            slug=normalized,
            name=name.strip()[:200],
            retention_days=retention_days,
        )
        db.session.add(organization)
        db.session.commit()
        click.echo(f"Organização criada: {organization.id} ({organization.slug})")

    @app.cli.command("set-break-glass")
    @click.option("--email", required=True)
    @click.option("--organization-slug")
    @click.option("--enabled", required=True, type=click.Choice(["true", "false"]))
    def set_break_glass(email: str, organization_slug: str | None, enabled: str):
        organization = cli_organization(organization_slug)
        enabled_value = enabled == "true"
        with tenant_scope(organization.id):
            user = User.query.filter_by(
                email=email.strip().lower(), role=Role.ADMIN
            ).first()
            if user is None:
                raise click.ClickException("Administrador não encontrado.")
            user.is_break_glass = enabled_value
            db.session.add(
                AuditEvent(
                    organization_id=user.organization_id,
                    action="BREAK_GLASS_PERMISSION_CHANGED",
                    target_type="user",
                    target_id=user.id,
                    details={"enabled": enabled_value},
                )
            )
            db.session.commit()
        click.echo(f"Acesso de emergência: {'ativado' if enabled_value else 'desativado'}")

    @app.cli.command("cleanup-submissions")
    @click.option("--organization-slug")
    def cleanup_submissions(organization_slug: str | None):
        from app.services.submissions import cleanup_expired

        organization = cli_organization(organization_slug)
        with tenant_scope(organization.id):
            count = cleanup_expired()
        click.echo(f"Arquivos expirados removidos: {count}.")

    @app.cli.command("teams-graph-status")
    @click.option("--organization-slug", required=True)
    @click.option("--probe", is_flag=True)
    def teams_graph_status(organization_slug: str, probe: bool):
        from app.services.teams_graph import TeamsGraphError, configured, probe_credentials
        from app.tenancy import tenant_scope

        organization = Organization.query.filter_by(slug=organization_slug).first()
        if organization is None:
            raise click.ClickException("Organização não encontrada.")
        with tenant_scope(organization.id):
            connection = TeamsConnection.query.filter_by(
                organization_id=organization.id
            ).first()
            click.echo(f"Configuração do worker: {'sim' if configured() else 'não'}")
            if connection is None:
                click.echo("Conexão: não configurada")
                return
            click.echo(f"Conexão: {connection.status}")
            click.echo(f"Assinatura: {connection.subscription_id or '-'}")
            click.echo(
                "Expira em: "
                + (
                    connection.subscription_expires_at.isoformat()
                    if connection.subscription_expires_at
                    else "-"
                )
            )
            if probe:
                try:
                    probe_credentials(connection)
                except TeamsGraphError as error:
                    raise click.ClickException(error.code) from error
                click.echo("Credencial Graph: acessível")

    @app.cli.command("renew-teams-subscriptions")
    @click.option("--organization-slug")
    def renew_teams_subscriptions(organization_slug: str | None):
        from app.jobs import renew_teams_subscription
        from app.tenancy import tenant_scope

        query = Organization.query
        if organization_slug:
            query = query.filter_by(slug=organization_slug)
        renewed = 0
        for organization in query.all():
            with tenant_scope(organization.id):
                connection = TeamsConnection.query.filter_by(
                    organization_id=organization.id
                ).first()
                if connection is None:
                    continue
                renew_teams_subscription(connection.id)
                renewed += 1
        click.echo(f"Assinaturas processadas: {renewed}.")

    @app.cli.command("set-user-request-limit")
    @click.option("--email", required=True, help="E-mail do usuário.")
    @click.option("--organization-slug")
    @click.option(
        "--requests",
        type=click.IntRange(10, 100),
        required=True,
        help="Operações sensíveis permitidas por 10 minutos.",
    )
    def set_user_request_limit(email: str, organization_slug: str | None, requests: int):
        organization = cli_organization(organization_slug)
        normalized_email = email.strip().lower()
        with tenant_scope(organization.id):
            user = User.query.filter_by(email=normalized_email).first()
            if not user:
                raise click.ClickException("Usuário não encontrado.")
            previous = user.request_limit_per_10_minutes
            user.request_limit_per_10_minutes = requests
            db.session.add(
                AuditEvent(
                    organization_id=user.organization_id,
                    actor_id=None,
                    action="USER_REQUEST_LIMIT_CHANGED_BY_CLI",
                    target_type="user",
                    target_id=user.id,
                    details={"previous": previous, "current": requests},
                )
            )
            db.session.commit()
        click.echo(
            f"Cota atualizada para {normalized_email}: {requests} operações por 10 minutos."
        )

    @app.cli.command("security-blocks")
    def security_blocks():
        try:
            redis = redis_client()
            keys = list(redis.scan_iter(match="meetmind:blocked:*", count=100))
        except (RedisError, ValueError) as error:
            raise click.ClickException("Redis de segurança indisponível.") from error
        if not keys:
            click.echo("Nenhum IP bloqueado.")
            return
        click.echo("IP mascarado | evento mais recente | TTL")
        for key in keys:
            ip_hash = key.rsplit(":", 1)[-1]
            event = (
                SecurityEvent.query.filter_by(ip_hash=ip_hash)
                .order_by(SecurityEvent.occurred_at.desc())
                .first()
            )
            masked_ip = event.masked_ip if event else "não identificado"
            event_id = event.id if event else "-"
            click.echo(f"{masked_ip} | {event_id} | {redis.ttl(key)}s")

    @app.cli.command("unblock-ip")
    @click.option("--ip", help="IP exato a desbloquear.")
    @click.option("--event-id", help="ID de um evento do IP bloqueado.")
    @click.option(
        "--latest",
        is_flag=True,
        help="Usa o IP do evento de rate limit mais recente.",
    )
    def unblock_ip(ip: str | None, event_id: str | None, latest: bool):
        selected = sum(bool(value) for value in (ip, event_id, latest))
        if selected != 1:
            raise click.ClickException(
                "Informe exatamente uma opção: --ip, --event-id ou --latest."
            )
        event = None
        if ip:
            ip_hash = hash_value(ip.strip())
        else:
            event = (
                db.session.get(SecurityEvent, event_id)
                if event_id
                else SecurityEvent.query.filter_by(event_type="RATE_LIMIT_EXCEEDED")
                .order_by(SecurityEvent.occurred_at.desc())
                .first()
            )
            if not event:
                raise click.ClickException("Evento de segurança não encontrado.")
            ip_hash = event.ip_hash
        try:
            deleted = redis_client().delete(
                f"meetmind:blocked:{ip_hash}",
                f"meetmind:violations:{ip_hash}",
            )
        except (RedisError, ValueError) as error:
            raise click.ClickException("Redis de segurança indisponível.") from error
        db.session.add(
            AuditEvent(
                organization_id=(
                    event.organization_id
                    if event is not None
                    else "00000000-0000-0000-0000-000000000001"
                ),
                actor_id=None,
                action="SECURITY_IP_UNBLOCKED_BY_CLI",
                target_type="security_ip",
                target_id=event.id if event else None,
                details={"redis_keys_removed": deleted},
            )
        )
        db.session.commit()
        click.echo(
            "IP desbloqueado e contador de violações removido."
            if deleted
            else "O IP informado não possuía bloqueio ativo."
        )

    @app.cli.command("dispatch-outbox")
    @click.option("--limit", type=click.IntRange(1, 1000), default=100)
    def dispatch_outbox(limit: int):
        from app.services.outbox import dispatch_pending

        report = dispatch_pending(limit)
        click.echo(
            f"Selecionados: {report['selected']}; enviados: {report['dispatched']}; "
            f"pendentes: {report['pending']}."
        )

    @app.cli.command("queue-status")
    def queue_status():
        from rq import Queue
        from rq.registry import FailedJobRegistry, StartedJobRegistry

        try:
            connection = redis_client()
            connection.ping()
        except (RedisError, ValueError) as error:
            raise click.ClickException("Redis indisponível.") from error
        for name in ("analyses", "screening", "notifications", "maintenance"):
            click.echo(
                f"{name}: aguardando={len(Queue(name, connection=connection))}; "
                f"executando={StartedJobRegistry(name, connection=connection).count}; "
                f"falhas={FailedJobRegistry(name, connection=connection).count}"
            )

    @app.cli.command("gemini-status")
    @click.option(
        "--probe",
        is_flag=True,
        help="Valida chave e acesso ao modelo sem enviar uma transcrição.",
    )
    @click.option("--organization-slug")
    def gemini_status(probe: bool, organization_slug: str | None):
        model = app.config["GEMINI_MODEL"]
        provider = provider_name()
        try:
            sdk_version = version("google-genai")
        except PackageNotFoundError:
            sdk_version = "não instalado"
        click.echo(f"SDK google-genai: {sdk_version}")
        click.echo(f"Provedor: {provider}")
        click.echo(f"Modelo: {model}")
        click.echo(f"Política ativa: {app.config['PROMPT_VERSION']}")
        click.echo(f"Schema SHA-256: {GEMINI_RESPONSE_SCHEMA_SHA256}")
        click.echo(f"Identidade configurada: {'sim' if ai_is_configured() else 'não'}")
        try:
            click.echo(f"Redis: {'ok' if redis_client().ping() else 'indisponível'}")
        except (RedisError, ValueError):
            click.echo("Redis: indisponível")
        try:
            organization = cli_organization(organization_slug)
            with tenant_scope(organization.id):
                recent = Analysis.query.order_by(Analysis.requested_at.desc()).limit(5).all()
        except SQLAlchemyError:
            db.session.rollback()
            click.echo("Últimas análises: banco indisponível (execute as migrações)")
        else:
            if recent:
                click.echo("Últimas análises:")
                for analysis in recent:
                    click.echo(
                        f"- {analysis.id}: {analysis.status.value}; "
                        f"erro={analysis.error_code or '-'}; modelo={analysis.model}"
                    )
            else:
                click.echo("Últimas análises: nenhuma")
        if not probe:
            return
        if not ai_is_configured():
            requirement = (
                "VERTEX_PROJECT/GOOGLE_CLOUD_PROJECT e identidade de workload"
                if provider == "vertex_ai"
                else "GEMINI_API_KEY"
            )
            raise click.ClickException(f"Configuração ausente: {requirement}.")
        try:
            with ai_client() as provider_client:
                response = provider_client.models.generate_content(
                    model=model,
                    contents=(
                        "Responda apenas com um JSON que confirme que a conexão está ativa."
                    ),
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_json_schema={
                            "type": "object",
                            "properties": {"status": {"type": "string"}},
                            "required": ["status"],
                            "additionalProperties": False,
                        },
                    ),
                )
            if not response.text:
                raise click.ClickException("Gemini respondeu sem conteúdo.")
        except ClientError as error:
            raise click.ClickException(
                f"Gemini rejeitou a validação (HTTP {error.code}): "
                f"{safe_provider_error(error)}"
            ) from error
        except ServerError as error:
            raise click.ClickException(
                f"Gemini indisponível durante a validação (HTTP {error.code}): "
                f"{safe_provider_error(error)}"
            ) from error
        except httpx.HTTPError as error:
            raise click.ClickException(
                f"Não foi possível acessar o Gemini: {safe_provider_error(error)}"
            ) from error
        click.echo("Validação de IA: identidade e modelo acessíveis.")

    @app.cli.command("import-meetings")
    @click.argument("path", type=click.Path(exists=True, dir_okay=False))
    @click.option("--actor-email", required=True)
    @click.option("--organization-slug")
    def import_meetings(path: str, actor_email: str, organization_slug: str | None):
        organization = cli_organization(organization_slug)
        with tenant_scope(organization.id):
            actor = User.query.filter_by(email=actor_email.lower(), role=Role.ADMIN).first()
            if not actor:
                raise click.ClickException("Administrador não encontrado.")
            result = import_enriched_json(path, actor)
        click.echo(
            f"Importadas: {result['imported']}; ignoradas: {result['skipped']}; "
            f"segmentos: {result['segments']}."
        )

    @app.cli.command("backfill-analysis-actions")
    @click.option("--apply", "apply_changes", is_flag=True, help="Persiste os rascunhos.")
    @click.option("--organization-slug")
    def backfill_analysis_actions(apply_changes: bool, organization_slug: str | None):
        organization = cli_organization(organization_slug)
        with tenant_scope(organization.id):
            analyses = Analysis.query.filter_by(status=AnalysisStatus.SUCCEEDED).all()
            candidates = []
            expected_actions = 0
            for analysis in analyses:
                result = analysis.result if isinstance(analysis.result, dict) else {}
                raw_actions = result.get("action_items")
                if not isinstance(raw_actions, list) or not raw_actions:
                    continue
                existing = ActionItem.query.filter_by(analysis_id=analysis.id).count()
                missing = max(0, min(3, len(raw_actions)) - existing)
                if missing:
                    candidates.append(analysis)
                    expected_actions += missing
            click.echo(
                f"Análises candidatas: {len(candidates)}; "
                f"até {expected_actions} ações novas."
            )
            if not apply_changes:
                db.session.rollback()
                click.echo("Dry-run concluído. Use --apply para persistir.")
                return
            before = ActionItem.query.count()
            for analysis in candidates:
                create_drafts(analysis)
            db.session.commit()
            click.echo(f"Ações criadas: {ActionItem.query.count() - before}.")

    @app.cli.command("backfill-manufacturing-ai")
    @click.option("--apply", "apply_changes", is_flag=True, help="Enfileira o lote.")
    @click.option("--batch-size", default=10, show_default=True, type=click.IntRange(1, 50))
    @click.option("--max-outstanding", default=20, show_default=True, type=click.IntRange(1, 100))
    @click.option("--organization-slug")
    def backfill_manufacturing_ai(
        apply_changes: bool,
        batch_size: int,
        max_outstanding: int,
        organization_slug: str | None,
    ):
        """Cria, em lotes idempotentes, a nova leitura das reuniões de manufatura."""
        organization = cli_organization(organization_slug)
        with tenant_scope(organization.id):
            outstanding = Analysis.query.filter(
                Analysis.prompt_version == app.config["PROMPT_VERSION"],
                Analysis.status.in_([AnalysisStatus.QUEUED, AnalysisStatus.RUNNING]),
            ).count()
            capacity = max(0, max_outstanding - outstanding)
            meetings = (
                Meeting.query.filter(
                    db.func.upper(Meeting.metadata_json["nome_segmento"].as_string())
                    == "MANUFATURA"
                )
                .order_by(Meeting.occurred_at.asc(), Meeting.id.asc())
                .all()
            )
            candidates = []
            for meeting in meetings:
                exists = Analysis.query.filter(
                    Analysis.meeting_id == meeting.id,
                    Analysis.prompt_version == app.config["PROMPT_VERSION"],
                    Analysis.status.in_(
                        [AnalysisStatus.QUEUED, AnalysisStatus.RUNNING, AnalysisStatus.SUCCEEDED]
                    ),
                ).first()
                if not exists:
                    candidates.append(meeting)
            selected = candidates[: min(batch_size, capacity)]
            click.echo(
                f"Reuniões de manufatura: {len(meetings)}; pendentes: {len(candidates)}; "
                f"em andamento: {outstanding}; lote disponível: {len(selected)}."
            )
            if not apply_changes:
                db.session.rollback()
                click.echo("Dry-run concluído. Use --apply para enfileirar o lote.")
                return
            if not app.config["QUEUE_ENABLED"]:
                raise click.ClickException(
                    "A fila precisa estar habilitada para executar o backfill."
                )
            actor = User.query.filter_by(role=Role.ADMIN).order_by(User.created_at.asc()).first()
            if actor is None:
                raise click.ClickException(
                    "Nenhum administrador disponível para registrar o backfill."
                )
            dispatch_ids = []
            for meeting in selected:
                context_snapshot = build_context_snapshot(meeting)
                analysis = Analysis(
                    organization_id=organization.id,
                    provider=app.config["AI_PROVIDER"],
                    meeting_id=meeting.id,
                    requested_by_id=actor.id,
                    model=app.config["GEMINI_MODEL"],
                    prompt_version=app.config["PROMPT_VERSION"],
                    score_version=app.config["SCORE_VERSION"],
                    transcript_hash=meeting.transcript_hash,
                    context_snapshot=context_snapshot,
                    context_version=context_snapshot["context_version"],
                    context_sha256=context_snapshot["context_sha256"],
                    mode_strategy="auto",
                    requested_modes=[],
                    mode_bundle_version=app.config["PROMPT_VERSION"],
                    analysis_profile_hash="legacy",
                    local_metrics=calculate_metrics(meeting),
                )
                db.session.add(analysis)
                db.session.flush()
                event = add_outbox_event(
                    organization_id=organization.id,
                    queue_name="analyses",
                    task_name="process_analysis",
                    job_id=analysis.id,
                    arguments=[analysis.id],
                    retry_intervals=[60, 180, 600],
                )
                dispatch_ids.append(event.id)
                db.session.add(
                    AuditEvent(
                        organization_id=organization.id,
                        actor_id=actor.id,
                        action="MANUFACTURING_AI_BACKFILL_QUEUED",
                        target_type="analysis",
                        target_id=analysis.id,
                        details={
                            "meeting_id": meeting.id,
                            "prompt_version": analysis.prompt_version,
                        },
                    )
                )
            db.session.commit()
            delivered = sum(bool(dispatch_outbox_event(item)) for item in dispatch_ids)
            click.echo(f"Análises criadas: {len(dispatch_ids)}; entregues ao worker: {delivered}.")
