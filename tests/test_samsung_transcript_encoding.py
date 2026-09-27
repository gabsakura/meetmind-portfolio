from __future__ import annotations

from app.services import file_guard
from app.services.submission_parsers import parse_submission

SAMSUNG_TRANSCRIPT = """Este conteúdo foi gerado por IA


Participante 1  (00:01)
Como você acha que foi a reunião?

Participante 2  (00:05)
Foi muito boa, mas precisamos melhorar a apresentação da auditabilidade e explicar
com clareza como cada conclusão permanece conectada às evidências da reunião.
"""


def samsung_utf16_be() -> bytes:
    return b"\xfe\xff" + SAMSUNG_TRANSCRIPT.encode("utf-16-be")


def test_security_accepts_utf16_be_export_with_bom():
    report = file_guard.screen("transcricao.txt", samsung_utf16_be())

    assert report["blocked"] is False
    assert "Participante 1" in report["clean_text"]
    assert "\x00" not in report["clean_text"]
    assert "null_bytes" not in {item["code"] for item in report["findings"]}


def test_samsung_export_preserves_speakers_and_timestamps():
    parsed = parse_submission("transcricao.txt", samsung_utf16_be())

    assert len(parsed.rows) == 2
    assert parsed.rows[0] == {
        "speaker": "Participante 1",
        "text": "Como você acha que foi a reunião?",
        "started_at_ms": 1_000,
        "ended_at_ms": None,
    }
    assert parsed.rows[1]["speaker"] == "Participante 2"
    assert parsed.rows[1]["started_at_ms"] == 5_000
    assert parsed.quality["speaker_count"] == 2
    assert parsed.quality["timestamp_coverage"] == 1
