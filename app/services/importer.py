from __future__ import annotations

import hashlib
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import ijson

from app.extensions import db
from app.models import Client, Meeting, MeetingParticipant, Segment, User
from app.tenancy import tenant_segment_id

ALLOWED_METADATA = {
    "cnae",
    "codt",
    "dt_criacao",
    "dt_meeting",
    "dt_ultima_pesquisa",
    "duracao_meeting",
    "eh_customer_crm",
    "faixa_faturamento_cliente_ec",
    "flg_externo",
    "formato_meeting",
    "id_status_meeting",
    "nome_segmento",
    "nome_unidade",
    "nota_nps",
    "pais",
    "possui_linha_csv",
    "regiao_brasil",
    "status_meeting",
    "tem_nps",
    "tp_recurso",
    "tp_recurso_normalizado",
    "uf",
}


def _json_scalar(value):
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    return value


def _duration_seconds(value: str | None) -> int | None:
    if not value:
        return None
    try:
        hours, minutes, seconds = (int(part) for part in value.split(":"))
        return hours * 3600 + minutes * 60 + seconds
    except (TypeError, ValueError):
        return None


def _date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _utterances(item: dict) -> list[dict]:
    rows = []
    meeting_id = str(item["id_reuniao"])
    for block_index, block in enumerate(item.get("transcricao") or []):
        for order_key, text in (block.get("falas") or {}).items():
            if not text:
                continue
            try:
                order = int(order_key.rsplit("_", 1)[-1])
            except ValueError:
                order = len(rows)
            rows.append(
                {
                    "id": f"{meeting_id}.{order}.{block_index}",
                    "order": order,
                    "speaker": str(block.get("locutor") or "Não identificado"),
                    "is_totvs": bool(block.get("totvs")),
                    "text": str(text),
                }
            )
    rows.sort(key=lambda row: (row["order"], row["id"]))
    return rows


def import_enriched_json(path: str | Path, created_by: User, commit_every: int = 25) -> dict:
    source = Path(path)
    actor_id = created_by.id
    imported = 0
    skipped = 0
    segments = 0
    clients: dict[str, str] = {}

    with source.open("rb") as stream:
        for item in ijson.items(stream, "item"):
            external_id = str(item.get("id_reuniao", ""))
            if not external_id or Meeting.query.filter_by(
                external_id=external_id,
                organization_id=created_by.organization_id,
            ).first():
                skipped += 1
                continue

            metadata_raw = item.get("metadados") or {}
            metadata = {
                key: _json_scalar(value)
                for key, value in metadata_raw.items()
                if key in ALLOWED_METADATA and (value is None or len(str(value)) <= 200)
            }
            client_external_id = str(metadata.get("codt") or f"meeting-{external_id}")
            client_id = clients.get(client_external_id)
            if not client_id:
                client = Client.query.filter_by(
                    external_id=client_external_id,
                    organization_id=created_by.organization_id,
                ).first()
                client_id = client.id if client else None
            if not client_id:
                client = Client(
                    external_id=client_external_id,
                    name=f"Cliente anonimizado {client_external_id}",
                    organization_id=created_by.organization_id,
                )
                db.session.add(client)
                db.session.flush()
                client_id = client.id
            clients[client_external_id] = client_id

            utterances = _utterances(item)
            digest = hashlib.sha256(
                "\n".join(row["text"] for row in utterances).encode("utf-8")
            ).hexdigest()
            meeting = Meeting(
                organization_id=created_by.organization_id,
                external_id=external_id,
                title=f"Reunião {external_id}",
                client_id=client_id,
                occurred_at=_date(metadata.get("dt_meeting")),
                duration_seconds=_duration_seconds(metadata.get("duracao_meeting")),
                approved_for_external_ai=False,
                transcript_hash=digest,
                metadata_json=metadata
                | {
                    "sem_cliente_detectado": item.get("sem_cliente_detectado"),
                    "confianca_cliente": item.get("confianca_cliente"),
                    "source": source.name,
                },
                created_by_id=actor_id,
            )
            db.session.add(meeting)
            db.session.flush()
            db.session.add(
                MeetingParticipant(
                    organization_id=created_by.organization_id,
                    meeting_id=meeting.id,
                    user_id=actor_id,
                    participation_role="organizer",
                )
            )
            db.session.execute(
                db.insert(Segment),
                [
                    {
                        "id": tenant_segment_id(created_by.organization_id, row["id"]),
                        "organization_id": created_by.organization_id,
                        "meeting_id": meeting.id,
                        "position": position,
                        "speaker": row["speaker"],
                        "is_totvs": row["is_totvs"],
                        "text": row["text"][:20_000],
                    }
                    for position, row in enumerate(utterances)
                ],
            )
            imported += 1
            segments += len(utterances)
            if imported % commit_every == 0:
                db.session.commit()
        db.session.commit()
    return {"imported": imported, "skipped": skipped, "segments": segments}
