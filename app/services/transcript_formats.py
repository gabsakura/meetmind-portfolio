from __future__ import annotations

import csv
import html
import io
import re
from dataclasses import dataclass
from typing import Any

from app.services.file_guard import decode_text

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
SAMSUNG_SPEAKER_LINE = re.compile(
    r"^\s*(?P<speaker>[^\n()]{1,120}?)\s+\((?P<time>\d{1,2}:\d{2}(?::\d{2})?)\)\s*$"
)
SAMSUNG_AI_NOTICE = "este conteúdo foi gerado por ia"
VOICE_TAG = re.compile(
    r"<v(?:\.[^ >]+)*\s+(?P<speaker>[^>]+)>(?P<text>.*?)</v>",
    re.IGNORECASE | re.DOTALL,
)
TAG = re.compile(r"<[^>]+>")


class TranscriptFormatError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass
class TranscriptRows:
    source_name: str
    columns: list[str]
    rows: list[dict[str, Any]]
    mapping: dict[str, str | None]
    quality: dict[str, Any]


def clean_text(value: Any) -> str:
    text = html.unescape(TAG.sub("", str(value or "")))
    return re.sub(r"\s+", " ", text.replace("\x00", " ")).strip()


def clean_speaker(value: Any) -> str | None:
    speaker = clean_text(value)[:200]
    return speaker or None


def time_to_ms(value: str | int | float | None) -> int | None:
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


def _speaker_and_text(lines: list[str]) -> tuple[str | None, str]:
    combined = "\n".join(lines)
    voice = VOICE_TAG.search(combined)
    if voice:
        return clean_speaker(voice.group("speaker")), clean_text(voice.group("text"))
    cleaned = [clean_text(line) for line in lines if clean_text(line)]
    if not cleaned:
        return None, ""
    first = PLAIN_LINE.match(cleaned[0])
    if first and not (first.group("speaker") or "").lower().startswith(("http", "https")):
        return (
            clean_speaker(first.group("speaker")),
            clean_text(" ".join([first.group("text"), *cleaned[1:]])),
        )
    if len(cleaned) > 1 and len(cleaned[0]) <= 120:
        return clean_speaker(cleaned[0]), clean_text(" ".join(cleaned[1:]))
    return None, clean_text(" ".join(cleaned))


def _caption_rows(text: str) -> list[dict[str, Any]]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    blocks = [
        [line.strip() for line in block.splitlines() if line.strip()]
        for block in re.split(r"\n\s*\n", normalized)
        if block.strip()
    ]
    rows: list[dict[str, Any]] = []
    for block in blocks:
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
        speaker, content = _speaker_and_text(block[timestamp_index + 1 :])
        if content:
            rows.append(
                {
                    "speaker": speaker,
                    "text": content,
                    "started_at_ms": time_to_ms(match.group("start")),
                    "ended_at_ms": time_to_ms(match.group("end")),
                }
            )
    return rows


def _plain_rows(text: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    unassigned: list[str] = []
    for raw_line in text.replace("\r\n", "\n").replace("\r", "\n").splitlines():
        line = clean_text(raw_line)
        if not line:
            continue
        if line.casefold() == SAMSUNG_AI_NOTICE:
            continue
        samsung = SAMSUNG_SPEAKER_LINE.match(line)
        if samsung:
            rows.append(
                {
                    "speaker": clean_speaker(samsung.group("speaker")),
                    "text": "",
                    "started_at_ms": time_to_ms(samsung.group("time")),
                    "ended_at_ms": None,
                }
            )
            continue
        match = PLAIN_LINE.match(line)
        if match and not (match.group("speaker") or "").lower().startswith(("http", "https")):
            rows.append(
                {
                    "speaker": clean_speaker(match.group("speaker")),
                    "text": clean_text(match.group("text")),
                    "started_at_ms": time_to_ms(
                        match.group("time1") or match.group("time2")
                    ),
                    "ended_at_ms": None,
                }
            )
        elif rows:
            rows[-1]["text"] = clean_text(f"{rows[-1]['text']} {line}")
        else:
            unassigned.append(line)
    if unassigned:
        rows.insert(
            0,
            {
                "speaker": None,
                "text": clean_text(" ".join(unassigned)),
                "started_at_ms": None,
                "ended_at_ms": None,
            },
        )
    return [row for row in rows if row["text"]]


def _infer(columns: list[str], candidates: tuple[str, ...]) -> str | None:
    exact = {column.casefold().strip(): column for column in columns}
    for candidate in candidates:
        if candidate in exact:
            return exact[candidate]
    for column in columns:
        if any(candidate in column.casefold() for candidate in candidates):
            return column
    return None


def _csv_rows(raw: bytes) -> tuple[list[str], list[dict[str, Any]], dict[str, str | None]]:
    text, decode_error = decode_text(raw)
    if decode_error or text is None:
        raise TranscriptFormatError("invalid_encoding", decode_error or "Codificação inválida.")
    try:
        dialect = csv.Sniffer().sniff(text[:8_192], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    columns = [str(column or "").strip() for column in (reader.fieldnames or [])]
    text_column = _infer(columns, TEXT_COLUMNS)
    if not text_column:
        raise TranscriptFormatError(
            "text_mapping_required",
            "Não foi possível identificar a coluna de texto do CSV.",
        )
    mapping = {
        "speaker": _infer(columns, SPEAKER_COLUMNS),
        "text": text_column,
        "started_at": _infer(columns, START_COLUMNS),
        "ended_at": _infer(columns, END_COLUMNS),
    }
    rows: list[dict[str, Any]] = []
    for row in reader:
        current = {
            column: clean_text(value) if value is not None else None
            for column, value in row.items()
            if column
        }
        if clean_text(current.get(text_column)):
            rows.append(current)
    return columns, rows, mapping


def quality_report(
    rows: list[dict[str, Any]],
    mapping: dict[str, str | None],
) -> dict[str, Any]:
    text_column = mapping.get("text")
    speaker_column = mapping.get("speaker")
    start_column = mapping.get("started_at")
    texts = [clean_text(row.get(text_column)) for row in rows if text_column]
    texts = [text for text in texts if text]
    speakers = [
        clean_speaker(row.get(speaker_column))
        for row in rows
        if speaker_column
    ]
    unidentified = sum(not speaker for speaker in speakers) if speaker_column else len(texts)
    timed = (
        sum(time_to_ms(row.get(start_column)) is not None for row in rows)
        if start_column
        else 0
    )
    warnings: list[dict[str, str]] = []
    if unidentified:
        warnings.append(
            {
                "code": "unidentified_speakers",
                "message": f"{unidentified} segmento(s) sem falante identificado.",
            }
        )
    if timed < len(texts):
        warnings.append(
            {
                "code": "partial_timestamps",
                "message": "Parte da transcrição não possui timestamp.",
            }
        )
    long_block = len(texts) == 1 and sum(map(len, texts)) > 1_000
    if long_block:
        warnings.append(
            {
                "code": "long_unstructured_block",
                "message": "O conteúdo permaneceu em um único bloco longo.",
            }
        )
    return {
        "usable": bool(texts),
        "needs_ai_review": bool(
            long_block or (texts and unidentified / len(texts) > 0.25)
        ),
        "segment_count": len(texts),
        "character_count": sum(map(len, texts)),
        "speaker_count": len({speaker for speaker in speakers if speaker}),
        "unidentified_speaker_count": unidentified,
        "timestamp_coverage": round(timed / len(texts), 4) if texts else 0,
        "warnings": warnings,
    }


def parse_transcript_format(filename: str, raw: bytes) -> TranscriptRows:
    suffix = filename.rsplit(".", 1)[-1].casefold()
    if suffix == "csv":
        columns, rows, mapping = _csv_rows(raw)
        source_name = "csv"
    else:
        text, decode_error = decode_text(raw)
        if decode_error or text is None:
            raise TranscriptFormatError(
                "invalid_encoding",
                decode_error or "A transcrição possui codificação inválida.",
            )
        rows = _caption_rows(text) if suffix in {"vtt", "srt"} else (
            _caption_rows(text) or _plain_rows(text)
        )
        columns = ["speaker", "text", "started_at_ms", "ended_at_ms"]
        mapping = {
            "speaker": "speaker",
            "text": "text",
            "started_at": "started_at_ms",
            "ended_at": "ended_at_ms",
        }
        source_name = suffix
    if not rows:
        raise TranscriptFormatError(
            "empty_transcript",
            "Nenhuma fala utilizável foi encontrada.",
        )
    return TranscriptRows(
        source_name=source_name,
        columns=columns,
        rows=rows,
        mapping=mapping,
        quality=quality_report(rows, mapping),
    )
