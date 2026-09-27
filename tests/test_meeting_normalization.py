from __future__ import annotations

import json

import pytest

from app.services.submission_parsers import build_segments, parse_submission
from tools.meeting_normalization import (
    NormalizationError,
    ai_handoff,
    apply_ai_suggestions,
    normalize_file,
    validate_canonical,
    write_json,
)


def test_normalizes_teams_vtt_without_rewriting_words(tmp_path):
    source = tmp_path / "teams.vtt"
    source.write_text(
        """WEBVTT

00:00:01.000 --> 00:00:04.500
<v Gabriel Sakura>Bom dia, podemos começar?</v>

00:00:05.000 --> 00:00:08.000
<v Cliente Exemplo>Sim. Precisamos revisar o prazo.</v>
""",
        encoding="utf-8",
    )

    document = normalize_file(source, title="Reunião do Teams")

    assert [row["speaker"] for row in document["segments"]] == [
        "Gabriel Sakura",
        "Cliente Exemplo",
    ]
    assert document["segments"][0]["text"] == "Bom dia, podemos começar?"
    assert document["segments"][0]["started_at_ms"] == 1_000
    assert document["segments"][0]["ended_at_ms"] == 4_500
    assert document["normalization_report"]["needs_ai_review"] is False


def test_normalizes_plain_speaker_text_and_flags_missing_timestamps(tmp_path):
    source = tmp_path / "teams.txt"
    source.write_text(
        "Gabriel: Vamos revisar a implantação.\n"
        "Cliente: O prazo precisa ser confirmado.\n",
        encoding="utf-8",
    )

    document = normalize_file(source)

    assert len(document["segments"]) == 2
    assert document["segments"][1]["speaker"] == "Cliente"
    assert document["normalization_report"]["timestamp_coverage"] == 0
    assert document["normalization_report"]["needs_ai_review"] is False


def test_long_unstructured_text_creates_ai_review_handoff(tmp_path):
    source = tmp_path / "ambiguous.txt"
    original = "conteúdo sem identificação " * 60
    source.write_text(original, encoding="utf-8")

    document = normalize_file(source)
    handoff = ai_handoff(document)

    assert document["normalization_report"]["needs_ai_review"] is True
    assert handoff["segments"] == document["segments"]
    assert any("Não adicionar" in rule for rule in handoff["immutable_rules"])


def test_ai_can_only_fill_unknown_speaker_without_changing_text(tmp_path):
    source = tmp_path / "ambiguous.txt"
    source.write_text("fala sem identificação", encoding="utf-8")
    document = normalize_file(source)
    original_text = document["segments"][0]["text"]

    updated, audit = apply_ai_suggestions(
        document,
        {
            "model": "test-model",
            "prompt_version": "test-v1",
            "suggestions": [
                {
                    "segment_id": "segment-000001",
                    "suggested_speaker": "Pessoa A",
                    "reason": "A pessoa se identifica literalmente no contexto.",
                    "confidence": 0.92,
                }
            ],
            "uncertainties": [],
        },
    )

    assert updated["segments"][0]["speaker"] == "Pessoa A"
    assert updated["segments"][0]["text"] == original_text
    assert audit["text_unchanged"] is True


def test_ai_attempt_to_change_text_is_rejected(tmp_path):
    source = tmp_path / "ambiguous.txt"
    source.write_text("fala sem identificação", encoding="utf-8")
    document = normalize_file(source)

    with pytest.raises(NormalizationError, match="campos proibidos"):
        apply_ai_suggestions(
            document,
            {
                "suggestions": [
                    {
                        "segment_id": "segment-000001",
                        "suggested_speaker": "Pessoa A",
                        "reason": "tentativa inválida",
                        "confidence": 0.99,
                        "text": "texto reescrito",
                    }
                ],
                "uncertainties": [],
            },
        )


def test_csv_is_converted_to_canonical_segments(tmp_path):
    source = tmp_path / "meeting.csv"
    source.write_text(
        "falante;texto;inicio_ms;fim_ms\n"
        "Pessoa A;Primeira fala;100;500\n"
        "Pessoa B;Segunda fala;600;900\n",
        encoding="utf-8",
    )

    document = normalize_file(source)

    assert document["segments"][0]["speaker"] == "Pessoa A"
    assert document["segments"][1]["started_at_ms"] == 600


def test_output_is_accepted_by_submission_parser(tmp_path):
    source = tmp_path / "teams.vtt"
    source.write_text(
        "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n<v A>Texto literal.</v>\n",
        encoding="utf-8",
    )
    destination = tmp_path / "teams.meetmind.json"
    write_json(destination, normalize_file(source))

    raw = destination.read_bytes()
    parsed = parse_submission(destination.name, raw)
    segments = build_segments(parsed)

    assert validate_canonical(json.loads(raw))["segment_count"] == 1
    assert segments[0]["text"] == "Texto literal."


def test_sql_with_non_insert_statement_is_rejected(tmp_path):
    source = tmp_path / "unsafe.sql"
    source.write_text("DROP TABLE meetings;", encoding="utf-8")

    with pytest.raises(NormalizationError):
        normalize_file(source)
