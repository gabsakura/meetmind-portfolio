from __future__ import annotations

import json
import zipfile
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any

from openpyxl import load_workbook
from pyparsing import (
    CaselessKeyword,
    Group,
    OneOrMore,
    Optional,
    ParseException,
    ParserElement,
    QuotedString,
    Regex,
    StringEnd,
    Suppress,
    Word,
    alphanums,
    alphas,
    delimited_list,
)

MAX_NORMALIZED_CHARS = 250_000
MAX_SEGMENTS = 5_000
MAX_JSON_DEPTH = 20
MAX_TABULAR_ROWS = 5_000
MAX_COLUMNS = 50
MAX_ZIP_ENTRIES = 1_000
MAX_UNCOMPRESSED_XLSX = 60 * 1024 * 1024
ALLOWED_EXTENSIONS = {
    ".json": "json",
    ".xlsx": "xlsx",
    ".sql": "sql",
    ".csv": "csv",
    ".txt": "txt",
    ".vtt": "vtt",
    ".srt": "srt",
}

TEXT_CANDIDATES = ("text", "texto", "fala", "content", "conteudo", "transcript")
SPEAKER_CANDIDATES = (
    "speaker",
    "falante",
    "participante",
    "nome_interlocutor",
    "interlocutor",
)
START_CANDIDATES = ("started_at_ms", "start_ms", "inicio_ms", "start", "inicio")
END_CANDIDATES = ("ended_at_ms", "end_ms", "fim_ms", "end", "fim")


class SubmissionParseError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass
class ParsedSubmission:
    file_format: str
    source_name: str
    columns: list[str]
    rows: list[dict[str, Any]]
    mapping: dict[str, str | None]
    choices: dict[str, Any]
    quality: dict[str, Any] | None = None

    def report(self) -> dict[str, Any]:
        report = {
            "format": self.file_format,
            "source_name": self.source_name,
            "columns": self.columns,
            "row_count": len(self.rows),
            "choices": self.choices,
            "preview_rows": self.rows[:12],
        }
        if self.quality is not None:
            report["quality"] = self.quality
        return report


def detect_format(filename: str) -> str:
    suffix = Path(filename).suffix.casefold()
    try:
        return ALLOWED_EXTENSIONS[suffix]
    except KeyError as error:
        raise SubmissionParseError(
            "unsupported_format",
            "Envie JSON, XLSX, SQL, CSV, TXT, VTT ou SRT.",
        ) from error


def _json_depth(value: Any, depth: int = 0) -> int:
    if depth > MAX_JSON_DEPTH:
        raise SubmissionParseError(
            "json_too_deep", f"O JSON excede {MAX_JSON_DEPTH} níveis."
        )
    if isinstance(value, dict):
        return max((_json_depth(item, depth + 1) for item in value.values()), default=depth)
    if isinstance(value, list):
        return max((_json_depth(item, depth + 1) for item in value), default=depth)
    return depth


def _safe_cell(value: Any) -> str | int | float | bool | None:
    if value is None or isinstance(value, (int, float, bool)):
        return value
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False)
    text = str(value).replace("\x00", "").strip()
    return text[:20_000]


def _rows_from_json(value: Any) -> tuple[str, list[dict[str, Any]]]:
    if isinstance(value, list) and value and all(isinstance(item, dict) for item in value):
        return "root", value
    if isinstance(value, dict):
        for key in ("segments", "segmentos", "transcript", "transcricao", "items", "data"):
            candidate = value.get(key)
            if (
                isinstance(candidate, list)
                and candidate
                and all(isinstance(item, dict) for item in candidate)
            ):
                return key, candidate
        for key, candidate in value.items():
            if (
                isinstance(candidate, list)
                and candidate
                and all(isinstance(item, dict) for item in candidate)
            ):
                return str(key), candidate
        for key in TEXT_CANDIDATES:
            if isinstance(value.get(key), str):
                return "root", [value]
    raise SubmissionParseError(
        "json_no_rows",
        "O JSON precisa conter uma lista de segmentos ou objetos tabulares.",
    )


def _infer(columns: list[str], candidates: tuple[str, ...]) -> str | None:
    normalized = {column.casefold().strip(): column for column in columns}
    for candidate in candidates:
        if candidate in normalized:
            return normalized[candidate]
    for column in columns:
        folded = column.casefold()
        if any(candidate in folded for candidate in candidates):
            return column
    return None


def _mapping(columns: list[str]) -> dict[str, str | None]:
    return {
        "speaker": _infer(columns, SPEAKER_CANDIDATES),
        "text": _infer(columns, TEXT_CANDIDATES),
        "started_at": _infer(columns, START_CANDIDATES),
        "ended_at": _infer(columns, END_CANDIDATES),
    }


def _normalize_rows(rows: list[dict[str, Any]]) -> tuple[list[str], list[dict[str, Any]]]:
    if len(rows) > MAX_TABULAR_ROWS:
        raise SubmissionParseError(
            "too_many_rows", f"O arquivo excede {MAX_TABULAR_ROWS} registros."
        )
    columns: list[str] = []
    seen: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for row in rows:
        current: dict[str, Any] = {}
        for key, value in row.items():
            column = str(key).strip()[:120]
            if not column:
                continue
            if column not in seen:
                if len(columns) >= MAX_COLUMNS:
                    raise SubmissionParseError(
                        "too_many_columns", f"O arquivo excede {MAX_COLUMNS} colunas."
                    )
                seen.add(column)
                columns.append(column)
            current[column] = _safe_cell(value)
        if current:
            normalized.append(current)
    if not normalized:
        raise SubmissionParseError("empty_file", "Nenhum registro útil foi encontrado.")
    return columns, normalized


def _parse_json(raw: bytes) -> ParsedSubmission:
    try:
        value = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SubmissionParseError("invalid_json", "O arquivo JSON é inválido.") from error
    _json_depth(value)
    source, raw_rows = _rows_from_json(value)
    columns, rows = _normalize_rows(raw_rows)
    return ParsedSubmission("json", source, columns, rows, _mapping(columns), {"sources": [source]})


def _xlsx_zip_guard(raw: bytes) -> None:
    try:
        with zipfile.ZipFile(BytesIO(raw)) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_ZIP_ENTRIES:
                raise SubmissionParseError("xlsx_zip_bomb", "A planilha contém arquivos demais.")
            total = sum(item.file_size for item in entries)
            compressed = max(1, sum(item.compress_size for item in entries))
            names = [item.filename.casefold() for item in entries]
            if total > MAX_UNCOMPRESSED_XLSX or total / compressed > 100:
                raise SubmissionParseError(
                    "xlsx_zip_bomb", "A expansão interna da planilha excede o limite seguro."
                )
            if any("vbaproject.bin" in name or "externallinks/" in name for name in names):
                raise SubmissionParseError(
                    "xlsx_active_content",
                    "Planilhas com macros ou vínculos externos não são aceitas.",
                )
    except zipfile.BadZipFile as error:
        raise SubmissionParseError("invalid_xlsx", "O XLSX está corrompido.") from error


def _parse_xlsx(raw: bytes, selected_source: str | None = None) -> ParsedSubmission:
    _xlsx_zip_guard(raw)
    try:
        workbook = load_workbook(
            BytesIO(raw), read_only=True, data_only=False, keep_links=False
        )
    except Exception as error:
        raise SubmissionParseError("invalid_xlsx", "O XLSX não pôde ser lido.") from error
    if len(workbook.sheetnames) > 10:
        raise SubmissionParseError("too_many_sheets", "O XLSX excede 10 planilhas.")
    source = selected_source if selected_source in workbook.sheetnames else workbook.sheetnames[0]
    sheet = workbook[source]
    iterator = sheet.iter_rows(values_only=False)
    try:
        header_cells = next(iterator)
    except StopIteration as error:
        raise SubmissionParseError("empty_file", "A planilha está vazia.") from error
    columns = [str(cell.value or "").strip()[:120] for cell in header_cells[:MAX_COLUMNS]]
    if not any(columns):
        raise SubmissionParseError("missing_header", "A planilha não possui cabeçalho.")
    rows: list[dict[str, Any]] = []
    for cells in iterator:
        if len(rows) >= MAX_TABULAR_ROWS:
            raise SubmissionParseError(
                "too_many_rows", f"A planilha excede {MAX_TABULAR_ROWS} linhas."
            )
        row: dict[str, Any] = {}
        for column, cell in zip(columns, cells, strict=False):
            if not column:
                continue
            if cell.data_type == "f":
                raise SubmissionParseError(
                    "xlsx_formula", "Planilhas com fórmulas não são aceitas."
                )
            row[column] = _safe_cell(cell.value)
        if any(value not in (None, "") for value in row.values()):
            rows.append(row)
    columns, rows = _normalize_rows(rows)
    return ParsedSubmission(
        "xlsx",
        source,
        columns,
        rows,
        _mapping(columns),
        {"sources": workbook.sheetnames},
    )


def _sql_grammar():
    ParserElement.enable_packrat()
    bare_identifier = Word(alphas + "_", alphanums + "_$")
    quoted_identifier = QuotedString('"', esc_quote='""', unquote_results=True)
    identifier = quoted_identifier | bare_identifier
    table = delimited_list(identifier, delim=".", combine=True)
    string_value = QuotedString("'", esc_quote="''", unquote_results=True)
    number_value = Regex(r"[+-]?(?:\d+\.\d+|\d+)").set_parse_action(
        lambda tokens: float(tokens[0]) if "." in tokens[0] else int(tokens[0])
    )
    true_value = CaselessKeyword("TRUE").set_parse_action(lambda: True)
    false_value = CaselessKeyword("FALSE").set_parse_action(lambda: False)
    null_value = CaselessKeyword("NULL").set_parse_action(lambda: None)
    literal = string_value | number_value | true_value | false_value | null_value
    columns = Group(Suppress("(") + delimited_list(identifier) + Suppress(")"))
    row = Group(Suppress("(") + delimited_list(literal) + Suppress(")"))
    rows = Group(delimited_list(row))
    statement = Group(
        CaselessKeyword("INSERT").suppress()
        + CaselessKeyword("INTO").suppress()
        + table("table")
        + columns("columns")
        + CaselessKeyword("VALUES").suppress()
        + rows("rows")
        + Optional(Suppress(";"))
    )
    return OneOrMore(statement) + StringEnd()


def _parse_sql(raw: bytes, selected_source: str | None = None) -> ParsedSubmission:
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise SubmissionParseError("invalid_encoding", "O SQL deve estar em UTF-8.") from error
    try:
        statements = _sql_grammar().parse_string(text, parse_all=True)
    except ParseException as error:
        raise SubmissionParseError("invalid_sql", "O SQL não pôde ser interpretado.") from error
    if not statements or len(statements) > 500:
        raise SubmissionParseError("sql_statement_limit", "Quantidade de comandos SQL inválida.")
    grouped: dict[str, tuple[list[str], list[dict[str, Any]]]] = {}
    for statement in statements:
        table = str(statement["table"])
        columns = [str(column) for column in statement["columns"]]
        if not table or not columns or len(columns) > MAX_COLUMNS:
            raise SubmissionParseError("sql_columns", "Colunas SQL inválidas.")
        existing_columns, rows = grouped.setdefault(table, (columns, []))
        if existing_columns != columns:
            raise SubmissionParseError(
                "sql_inconsistent_columns", "Os INSERTs da tabela usam colunas diferentes."
            )
        for tuple_values in statement["rows"]:
            if len(tuple_values) != len(columns):
                raise SubmissionParseError("sql_values", "Linha VALUES incompatível.")
            if len(rows) >= MAX_TABULAR_ROWS:
                raise SubmissionParseError(
                    "too_many_rows", f"O SQL excede {MAX_TABULAR_ROWS} linhas."
                )
            rows.append(
                {
                    column: _safe_cell(value)
                    for column, value in zip(columns, tuple_values, strict=True)
                }
            )
    sources = sorted(grouped)
    source = selected_source if selected_source in grouped else sources[0]
    columns, rows = _normalize_rows(grouped[source][1])
    return ParsedSubmission(
        "sql", source, columns, rows, _mapping(columns), {"sources": sources}
    )


def parse_submission(
    filename: str, raw: bytes, mapping: dict[str, Any] | None = None
) -> ParsedSubmission:
    file_format = detect_format(filename)
    selected_source = str((mapping or {}).get("source") or "") or None
    if file_format in {"csv", "txt", "vtt", "srt"}:
        from app.services.transcript_formats import (
            TranscriptFormatError,
            parse_transcript_format,
        )

        try:
            transcript = parse_transcript_format(filename, raw)
        except TranscriptFormatError as error:
            raise SubmissionParseError(error.code, str(error)) from error
        parsed = ParsedSubmission(
            file_format,
            transcript.source_name,
            transcript.columns,
            transcript.rows,
            transcript.mapping,
            {"sources": [transcript.source_name]},
            transcript.quality,
        )
    elif file_format == "json":
        parsed = _parse_json(raw)
    elif file_format == "xlsx":
        parsed = _parse_xlsx(raw, selected_source)
    else:
        parsed = _parse_sql(raw, selected_source)
    if mapping:
        for key in ("speaker", "text", "started_at", "ended_at"):
            selected = mapping.get(key)
            if selected is not None and selected not in parsed.columns:
                raise SubmissionParseError(
                    "invalid_mapping", f"A coluna mapeada para {key} não existe."
                )
            parsed.mapping[key] = selected
    parsed.mapping["source"] = parsed.source_name
    return parsed


def _time_value(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def build_segments(parsed: ParsedSubmission) -> list[dict[str, Any]]:
    text_column = parsed.mapping.get("text")
    if not text_column:
        raise SubmissionParseError(
            "text_mapping_required", "Selecione a coluna que contém o texto falado."
        )
    segments: list[dict[str, Any]] = []
    total_chars = 0
    for row in parsed.rows:
        text = str(row.get(text_column) or "").strip()
        if not text:
            continue
        total_chars += len(text)
        if total_chars > MAX_NORMALIZED_CHARS:
            raise SubmissionParseError(
                "transcript_too_large",
                f"A transcrição normalizada excede {MAX_NORMALIZED_CHARS} caracteres.",
            )
        speaker_column = parsed.mapping.get("speaker")
        start_column = parsed.mapping.get("started_at")
        end_column = parsed.mapping.get("ended_at")
        segments.append(
            {
                "speaker": (
                    str(row.get(speaker_column) or "").strip()[:200]
                    if speaker_column
                    else None
                ),
                "text": text[:20_000],
                "started_at_ms": _time_value(row.get(start_column)) if start_column else None,
                "ended_at_ms": _time_value(row.get(end_column)) if end_column else None,
            }
        )
        if len(segments) > MAX_SEGMENTS:
            raise SubmissionParseError(
                "too_many_segments", f"A transcrição excede {MAX_SEGMENTS} segmentos."
            )
    if not segments:
        raise SubmissionParseError("empty_transcript", "Nenhum texto foi mapeado.")
    return segments


def preview_text(parsed: ParsedSubmission) -> str:
    try:
        segments = build_segments(parsed)
    except SubmissionParseError:
        return json.dumps(parsed.rows[:8], ensure_ascii=False, indent=2)[:8_000]
    lines = []
    for segment in segments[:20]:
        prefix = f"{segment['speaker']}: " if segment["speaker"] else ""
        lines.append(f"{prefix}{segment['text']}")
    return "\n".join(lines)[:8_000]


def normalized_text(segments: list[dict[str, Any]]) -> str:
    return "\n".join(
        f"{segment['speaker']}: {segment['text']}"
        if segment.get("speaker")
        else segment["text"]
        for segment in segments
    )
