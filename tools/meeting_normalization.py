from __future__ import annotations

import copy
import csv
import hashlib
import html
import io
import json
import re
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

SUPPORTED_EXTENSIONS = {".txt", ".vtt", ".srt", ".json", ".csv", ".xlsx", ".sql"}
MAX_NORMALIZED_CHARS = 250_000
MAX_SEGMENTS = 5_000
TEXT_COLUMNS = ("text", "texto", "fala", "content", "conteudo", "transcript", "transcricao")
SPEAKER_COLUMNS = (
    "speaker",
    "falante",
    "locutor",
    "participante",
    "nome_interlocutor",
    "interlocutor",
)
START_COLUMNS = ("started_at_ms", "start_ms", "inicio_ms", "start", "inicio")
END_COLUMNS = ("ended_at_ms", "end_ms", "fim_ms", "end", "fim")

TIMESTAMP_RANGE = re.compile(
    r"(?P<start>(?:\d{1,2}:)?\d{1,2}:\d{2}[.,]\d{1,3})"
    r"\s*-->\s*"
    r"(?P<end>(?:\d{1,2}:)?\d{1,2}:\d{2}[.,]\d{1,3})"
)
PLAIN_LINE = re.compile(
    r"^\s*(?:\[(?P<time1>\d{1,2}:\d{2}(?::\d{2})?)\]|"
    r"(?P<time2>\d{1,2}:\d{2}(?::\d{2})?))?\s*"
    r"(?P<speaker>[^:\n]{1,120}):\s*(?P<text>.+?)\s*$"
)
VOICE_TAG = re.compile(
    r"<v(?:\.[^ >]+)*\s+(?P<speaker>[^>]+)>(?P<text>.*?)</v>",
    re.IGNORECASE | re.DOTALL,
)
TAG = re.compile(r"<[^>]+>")


class NormalizationError(ValueError):
    pass


@dataclass
class Segment:
    speaker: str | None
    text: str
    started_at_ms: int | None = None
    ended_at_ms: int | None = None


def _clean_text(value: Any) -> str:
    text = html.unescape(TAG.sub("", str(value or "")))
    return re.sub(r"\s+", " ", text.replace("\x00", " ")).strip()


def _clean_speaker(value: Any) -> str | None:
    speaker = _clean_text(value)[:200]
    return speaker or None


def _time_to_ms(value: str | int | float | None) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        number = int(value)
        return number if number >= 0 else None
    raw = str(value).strip().replace(",", ".")
    if raw.isdigit():
        return int(raw)
    parts = raw.split(":")
    try:
        if len(parts) == 3:
            hours, minutes, seconds = int(parts[0]), int(parts[1]), float(parts[2])
        elif len(parts) == 2:
            hours, minutes, seconds = 0, int(parts[0]), float(parts[1])
        else:
            return None
    except ValueError:
        return None
    if hours < 0 or minutes < 0 or seconds < 0:
        return None
    return int(round((hours * 3600 + minutes * 60 + seconds) * 1000))


def _split_blocks(text: str) -> list[list[str]]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    return [
        [line.strip() for line in block.splitlines() if line.strip()]
        for block in re.split(r"\n\s*\n", normalized)
        if block.strip()
    ]


def _speaker_and_text(lines: list[str]) -> tuple[str | None, str]:
    combined = "\n".join(lines)
    voice = VOICE_TAG.search(combined)
    if voice:
        return _clean_speaker(voice.group("speaker")), _clean_text(voice.group("text"))

    cleaned = [_clean_text(line) for line in lines if _clean_text(line)]
    if not cleaned:
        return None, ""
    first = PLAIN_LINE.match(cleaned[0])
    if first and not (first.group("speaker") or "").lower().startswith(("http", "https")):
        remaining = " ".join([first.group("text"), *cleaned[1:]])
        return _clean_speaker(first.group("speaker")), _clean_text(remaining)
    if len(cleaned) > 1 and len(cleaned[0]) <= 120:
        return _clean_speaker(cleaned[0]), _clean_text(" ".join(cleaned[1:]))
    return None, _clean_text(" ".join(cleaned))


def parse_caption_text(text: str) -> list[Segment]:
    segments: list[Segment] = []
    for block in _split_blocks(text):
        if block[0].upper().startswith(("WEBVTT", "NOTE", "STYLE", "REGION")):
            continue
        timestamp_index = next(
            (index for index, line in enumerate(block) if TIMESTAMP_RANGE.search(line)),
            None,
        )
        if timestamp_index is None:
            continue
        match = TIMESTAMP_RANGE.search(block[timestamp_index])
        if match is None:
            continue
        payload = block[timestamp_index + 1 :]
        speaker, content = _speaker_and_text(payload)
        if content:
            segments.append(
                Segment(
                    speaker=speaker,
                    text=content,
                    started_at_ms=_time_to_ms(match.group("start")),
                    ended_at_ms=_time_to_ms(match.group("end")),
                )
            )
    return segments


def parse_plain_text(text: str) -> list[Segment]:
    segments: list[Segment] = []
    unassigned: list[str] = []
    for raw_line in text.replace("\r\n", "\n").replace("\r", "\n").splitlines():
        line = _clean_text(raw_line)
        if not line:
            continue
        match = PLAIN_LINE.match(line)
        if match and not (match.group("speaker") or "").lower().startswith(("http", "https")):
            segments.append(
                Segment(
                    speaker=_clean_speaker(match.group("speaker")),
                    text=_clean_text(match.group("text")),
                    started_at_ms=_time_to_ms(match.group("time1") or match.group("time2")),
                )
            )
        elif segments:
            segments[-1].text = _clean_text(f"{segments[-1].text} {line}")
        else:
            unassigned.append(line)
    if unassigned:
        segments.insert(0, Segment(speaker=None, text=_clean_text(" ".join(unassigned))))
    return segments


def _infer_column(columns: Iterable[str], candidates: tuple[str, ...]) -> str | None:
    available = list(columns)
    exact = {column.casefold().strip(): column for column in available}
    for candidate in candidates:
        if candidate in exact:
            return exact[candidate]
    for column in available:
        folded = column.casefold()
        if any(candidate in folded for candidate in candidates):
            return column
    return None


def parse_csv_bytes(raw: bytes) -> list[Segment]:
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise NormalizationError("O CSV deve estar codificado em UTF-8.") from error
    try:
        dialect = csv.Sniffer().sniff(text[:8_192], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    columns = [str(column or "").strip() for column in (reader.fieldnames or [])]
    text_column = _infer_column(columns, TEXT_COLUMNS)
    if not text_column:
        raise NormalizationError("Não foi possível identificar a coluna de texto do CSV.")
    speaker_column = _infer_column(columns, SPEAKER_COLUMNS)
    start_column = _infer_column(columns, START_COLUMNS)
    end_column = _infer_column(columns, END_COLUMNS)
    segments: list[Segment] = []
    for row in reader:
        content = _clean_text(row.get(text_column))
        if not content:
            continue
        segments.append(
            Segment(
                speaker=_clean_speaker(row.get(speaker_column)) if speaker_column else None,
                text=content,
                started_at_ms=_time_to_ms(row.get(start_column)) if start_column else None,
                ended_at_ms=_time_to_ms(row.get(end_column)) if end_column else None,
            )
        )
    return segments


def parse_structured_file(path: Path, raw: bytes) -> list[Segment]:
    try:
        from app.services.submission_parsers import (
            SubmissionParseError,
            build_segments,
            parse_submission,
        )

        parsed = parse_submission(path.name, raw)
        built = build_segments(parsed)
    except ImportError as error:
        raise NormalizationError(
            "As dependências do MeetMind são necessárias para JSON, XLSX e SQL. "
            "Execute pelo Docker ou instale requirements.txt."
        ) from error
    except SubmissionParseError as error:
        raise NormalizationError(str(error)) from error
    return [
        Segment(
            speaker=_clean_speaker(row.get("speaker")),
            text=_clean_text(row.get("text")),
            started_at_ms=row.get("started_at_ms"),
            ended_at_ms=row.get("ended_at_ms"),
        )
        for row in built
    ]


def _quality_report(segments: list[Segment]) -> dict[str, Any]:
    if not segments:
        raise NormalizationError("Nenhum segmento de fala foi identificado.")
    if len(segments) > MAX_SEGMENTS:
        raise NormalizationError(f"A transcrição excede {MAX_SEGMENTS} segmentos.")
    total_chars = sum(len(segment.text) for segment in segments)
    if total_chars > MAX_NORMALIZED_CHARS:
        raise NormalizationError(
            f"A transcrição excede {MAX_NORMALIZED_CHARS} caracteres normalizados."
        )
    unidentified = sum(not segment.speaker for segment in segments)
    timed = sum(segment.started_at_ms is not None for segment in segments)
    speakers = sorted({segment.speaker for segment in segments if segment.speaker})
    warnings: list[dict[str, str]] = []
    if unidentified:
        warnings.append(
            {
                "code": "unidentified_speakers",
                "message": f"{unidentified} segmento(s) não possuem falante identificado.",
            }
        )
    if timed < len(segments):
        warnings.append(
            {
                "code": "partial_timestamps",
                "message": "Parte da transcrição não possui timestamp.",
            }
        )
    previous = -1
    non_monotonic = False
    for segment in segments:
        if segment.started_at_ms is None:
            continue
        if segment.started_at_ms < previous:
            non_monotonic = True
            break
        previous = segment.started_at_ms
    if non_monotonic:
        warnings.append(
            {
                "code": "non_monotonic_timestamps",
                "message": "Os timestamps não estão em ordem crescente.",
            }
        )
    long_unstructured = len(segments) == 1 and total_chars > 1_000
    if long_unstructured:
        warnings.append(
            {
                "code": "long_unstructured_block",
                "message": "O conteúdo ficou em um único bloco longo.",
            }
        )
    needs_ai_review = bool(
        non_monotonic
        or long_unstructured
        or unidentified / len(segments) > 0.25
    )
    return {
        "usable": True,
        "needs_ai_review": needs_ai_review,
        "segment_count": len(segments),
        "character_count": total_chars,
        "speaker_count": len(speakers),
        "speakers": speakers,
        "unidentified_speaker_count": unidentified,
        "timestamp_coverage": round(timed / len(segments), 4),
        "warnings": warnings,
    }


def normalize_file(
    path: str | Path,
    *,
    title: str | None = None,
    client_name: str | None = None,
    occurred_at: str | None = None,
    language: str = "pt-BR",
) -> dict[str, Any]:
    source = Path(path)
    suffix = source.suffix.casefold()
    if suffix not in SUPPORTED_EXTENSIONS:
        raise NormalizationError(
            f"Formato {suffix or 'sem extensão'} não suportado. "
            f"Use: {', '.join(sorted(SUPPORTED_EXTENSIONS))}."
        )
    raw = source.read_bytes()
    if not raw:
        raise NormalizationError("O arquivo está vazio.")
    if suffix in {".vtt", ".srt"}:
        try:
            segments = parse_caption_text(raw.decode("utf-8-sig"))
        except UnicodeDecodeError as error:
            raise NormalizationError("A legenda deve estar em UTF-8.") from error
    elif suffix == ".txt":
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as error:
            raise NormalizationError("O TXT deve estar em UTF-8.") from error
        segments = parse_caption_text(text) or parse_plain_text(text)
    elif suffix == ".csv":
        segments = parse_csv_bytes(raw)
    else:
        segments = parse_structured_file(source, raw)

    report = _quality_report(segments)
    transcript = "\n".join(
        f"{segment.speaker}: {segment.text}" if segment.speaker else segment.text
        for segment in segments
    )
    canonical_segments = [
        {
            "id": f"segment-{index:06d}",
            "speaker": segment.speaker or "Não identificado",
            "text": segment.text,
            "started_at_ms": segment.started_at_ms,
            "ended_at_ms": segment.ended_at_ms,
        }
        for index, segment in enumerate(segments, start=1)
    ]
    return {
        "schema_version": "meetmind-transcript-v1",
        "meeting": {
            "title": (title or source.stem).strip()[:200],
            "client_name": (client_name or "").strip()[:200] or None,
            "occurred_at": occurred_at or None,
            "language": language,
            "source_filename": source.name,
        },
        "segments": canonical_segments,
        "normalization_report": {
            **report,
            "source_format": suffix.removeprefix("."),
            "source_sha256": hashlib.sha256(raw).hexdigest(),
            "normalized_transcript_sha256": hashlib.sha256(
                transcript.encode("utf-8")
            ).hexdigest(),
            "generated_at": datetime.now(UTC).isoformat(),
            "transformations": [
                "normalização de espaços e quebras de linha",
                "remoção de marcação VTT/HTML sem alteração das palavras",
                "conversão determinística de timestamps para milissegundos",
            ],
        },
    }


def validate_canonical(document: dict[str, Any]) -> dict[str, Any]:
    if document.get("schema_version") != "meetmind-transcript-v1":
        raise NormalizationError("schema_version inválida.")
    raw_segments = document.get("segments")
    if not isinstance(raw_segments, list) or not raw_segments:
        raise NormalizationError("O documento não possui segmentos.")
    segments: list[Segment] = []
    ids: set[str] = set()
    for index, row in enumerate(raw_segments, start=1):
        if not isinstance(row, dict):
            raise NormalizationError(f"O segmento {index} não é um objeto.")
        segment_id = str(row.get("id") or "")
        if not re.fullmatch(r"segment-\d{6}", segment_id) or segment_id in ids:
            raise NormalizationError(f"ID inválido ou duplicado no segmento {index}.")
        ids.add(segment_id)
        content = _clean_text(row.get("text"))
        if not content:
            raise NormalizationError(f"O segmento {segment_id} não possui texto.")
        start = _time_to_ms(row.get("started_at_ms"))
        end = _time_to_ms(row.get("ended_at_ms"))
        if start is not None and end is not None and end < start:
            raise NormalizationError(f"Timestamp final inválido em {segment_id}.")
        segments.append(
            Segment(
                speaker=_clean_speaker(row.get("speaker")),
                text=content,
                started_at_ms=start,
                ended_at_ms=end,
            )
        )
    return _quality_report(segments)


def ai_handoff(document: dict[str, Any]) -> dict[str, Any]:
    report = document["normalization_report"]
    return {
        "task": "review_transcript_structure",
        "schema_version": "meetmind-ai-normalization-v1",
        "source_sha256": report["source_sha256"],
        "privacy_notice": (
            "Este pacote contém falas da reunião. Envie ao provedor externo "
            "somente após consentimento e pelo worker do MeetMind."
        ),
        "immutable_rules": [
            "Não adicionar, remover, resumir, traduzir ou reescrever palavras.",
            "Não inventar participantes ou timestamps.",
            "Preservar a ordem e os IDs dos segmentos.",
            "Marcar incerteza em vez de adivinhar.",
        ],
        "allowed_changes": [
            "sugerir falante usando somente evidência contida no texto",
            "registrar dúvidas em uncertainties sem alterar o segmento",
        ],
        "warnings": report["warnings"],
        "segments": document["segments"],
        "expected_output": {
            "suggestions": [
                {
                    "segment_id": "segment-000001",
                    "suggested_speaker": None,
                    "reason": "evidência curta",
                    "confidence": 0.0,
                }
            ],
            "uncertainties": [],
        },
    }


def apply_ai_suggestions(
    document: dict[str, Any],
    response: dict[str, Any],
    *,
    min_confidence: float = 0.85,
) -> tuple[dict[str, Any], dict[str, Any]]:
    validate_canonical(document)
    if not isinstance(response, dict):
        raise NormalizationError("A resposta da IA precisa ser um objeto JSON.")
    if set(response) - {"suggestions", "uncertainties", "model", "prompt_version"}:
        raise NormalizationError("A resposta da IA contém campos não permitidos.")
    suggestions = response.get("suggestions")
    if not isinstance(suggestions, list):
        raise NormalizationError("A resposta da IA não contém uma lista de sugestões.")
    uncertainties = response.get("uncertainties") or []
    if not isinstance(uncertainties, list):
        raise NormalizationError("uncertainties precisa ser uma lista.")
    updated = copy.deepcopy(document)
    by_id = {row["id"]: row for row in updated["segments"]}
    original_texts = {
        row["id"]: row["text"]
        for row in document["segments"]
    }
    applied: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    seen: set[str] = set()
    allowed_fields = {"segment_id", "suggested_speaker", "reason", "confidence"}
    for index, suggestion in enumerate(suggestions, start=1):
        if not isinstance(suggestion, dict):
            raise NormalizationError(f"A sugestão {index} não é um objeto.")
        unexpected = set(suggestion) - allowed_fields
        if unexpected:
            raise NormalizationError(
                f"A sugestão {index} tenta alterar campos proibidos: "
                f"{', '.join(sorted(unexpected))}."
            )
        segment_id = str(suggestion.get("segment_id") or "")
        if segment_id not in by_id:
            raise NormalizationError(f"A sugestão referencia ID inexistente: {segment_id}.")
        if segment_id in seen:
            raise NormalizationError(f"Há sugestões duplicadas para {segment_id}.")
        seen.add(segment_id)
        speaker = _clean_speaker(suggestion.get("suggested_speaker"))
        reason = _clean_text(suggestion.get("reason"))[:400]
        try:
            confidence = float(suggestion.get("confidence"))
        except (TypeError, ValueError) as error:
            raise NormalizationError(
                f"A confiança de {segment_id} é inválida."
            ) from error
        current_speaker = _clean_speaker(by_id[segment_id].get("speaker"))
        rejection_reason = None
        if not speaker:
            rejection_reason = "falante_sugerido_vazio"
        elif not reason:
            rejection_reason = "justificativa_ausente"
        elif not 0 <= confidence <= 1:
            rejection_reason = "confianca_fora_do_intervalo"
        elif confidence < min_confidence:
            rejection_reason = "confianca_abaixo_do_limite"
        elif current_speaker and current_speaker != "Não identificado":
            rejection_reason = "falante_ja_identificado"
        if rejection_reason:
            rejected.append(
                {
                    "segment_id": segment_id,
                    "reason": rejection_reason,
                    "confidence": confidence,
                }
            )
            continue
        by_id[segment_id]["speaker"] = speaker
        applied.append(
            {
                "segment_id": segment_id,
                "previous_speaker": current_speaker,
                "speaker": speaker,
                "reason": reason,
                "confidence": confidence,
            }
        )
    if {
        row["id"]: row["text"]
        for row in updated["segments"]
    } != original_texts:
        raise NormalizationError("A aplicação tentou modificar o texto original.")
    validate_canonical(updated)
    response_bytes = json.dumps(
        response,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    audit = {
        "response_sha256": hashlib.sha256(response_bytes).hexdigest(),
        "model": str(response.get("model") or "")[:100] or None,
        "prompt_version": str(response.get("prompt_version") or "")[:100] or None,
        "minimum_confidence": min_confidence,
        "applied": applied,
        "rejected": rejected,
        "uncertainties": [
            _clean_text(item)[:400]
            for item in uncertainties
            if _clean_text(item)
        ][:20],
        "applied_at": datetime.now(UTC).isoformat(),
        "text_unchanged": True,
    }
    normalization_report = updated.setdefault("normalization_report", {})
    normalization_report.setdefault("ai_repairs", []).append(audit)
    normalization_report["needs_ai_review"] = bool(
        audit["uncertainties"] or rejected
    )
    return updated, audit


def write_json(path: str | Path, document: dict[str, Any]) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(destination)
