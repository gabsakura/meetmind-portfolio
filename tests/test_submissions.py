from __future__ import annotations

import io
import json

import pytest
from openpyxl import Workbook

from app.extensions import db
from app.models import MeetingSubmission, SubmissionStatus, User
from app.services.submission_parsers import SubmissionParseError, parse_submission

TRANSCRIPT = """Ana: Vamos revisar o incidente do faturamento e os próximos passos.
Carlos: A produção parou por quarenta minutos durante o fechamento.
Ana: Vou abrir o incidente prioritário e retorno amanhã com o diagnóstico.
Carlos: Combinado. Na próxima reunião confirmamos a correção e os indicadores.
"""


@pytest.fixture()
def clean_antivirus(monkeypatch):
    monkeypatch.setattr(
        "app.services.antivirus.scan",
        lambda _raw: {
            "status": "clean",
            "engine": "clamav",
            "signature": None,
            "detail": "clean",
        },
    )


def submit(client, *, sector="generico", consent="true"):
    return client.post(
        "/api/v1/submissions",
        data={
            "file": (io.BytesIO(TRANSCRIPT.encode()), "meeting.txt"),
            "title": "Revisão do incidente",
            "client_name": "Cliente teste",
            "occurred_at": "2026-09-17",
            "language": "pt-BR",
            "sector": sector,
            "external_id": f"submission-{sector}",
            "consent": consent,
        },
        content_type="multipart/form-data",
    )


def test_consent_and_multipart_are_required(analyst_client, clean_antivirus):
    no_consent = submit(analyst_client, consent="false")
    assert no_consent.status_code == 422
    assert no_consent.json["code"] == "consent_required"

    wrong_type = analyst_client.post("/api/v1/submissions", json={"file": "x"})
    assert wrong_type.status_code == 415


def test_only_owner_sees_submission_history(
    analyst_client, admin_client, app, clean_antivirus
):
    created = submit(analyst_client).json["submission"]
    own = analyst_client.get("/api/v1/submissions")
    assert own.status_code == 200
    assert [item["id"] for item in own.json["items"]] == [created["id"]]

    admin = admin_client.get("/api/v1/submissions?scope=review")
    assert admin.status_code == 200
    assert admin.json["items"] == []

    with app.app_context():
        assert db.session.get(MeetingSubmission, created["id"]).meeting_id


def test_submission_reports_source_and_allows_retry_of_transient_failure(
    analyst_client, app, clean_antivirus, monkeypatch
):
    created = submit(analyst_client).json["submission"]
    assert created["source"] == "manual_upload"

    with app.app_context():
        submission = db.session.get(MeetingSubmission, created["id"])
        submission.status = SubmissionStatus.FAILED.value
        submission.error_code = "ingestion_error"
        submission.stored_object_key = "retry/meeting.txt"
        db.session.commit()

    monkeypatch.setattr(
        "app.blueprints.api.submissions.run_submission_ingestion",
        lambda _submission_id: None,
    )
    response = analyst_client.post(f"/api/v1/submissions/{created['id']}/retry")

    assert response.status_code == 200
    assert response.json["submission"]["status"] == SubmissionStatus.SCREENING.value
    assert response.json["submission"]["retryable"] is False


def test_removed_presentation_sector_is_rejected(
    analyst_client, admin_client, clean_antivirus
):
    for client in (analyst_client, admin_client):
        response = submit(client, sector="apresentacao_meetmind")
        assert response.status_code == 422
        assert response.json["code"] == "invalid_sector"


def test_fail_closed_antivirus_never_stores_original(analyst_client, app, monkeypatch):
    app.config["SUBMISSION_REQUIRE_ANTIVIRUS"] = True
    monkeypatch.setattr(
        "app.services.antivirus.scan",
        lambda _raw: {
            "status": "unavailable",
            "engine": "clamav",
            "signature": None,
            "detail": "offline",
        },
    )
    response = submit(analyst_client)
    assert response.status_code == 201
    submission = response.json["submission"]
    assert submission["status"] == "blocked"
    assert submission["error_code"] == "antivirus_blocked"
    assert submission["original_available"] is False


def test_sql_parser_accepts_only_literal_insert_and_never_executes(app):
    safe = (
        b"INSERT INTO segments (speaker, text, started_at_ms) VALUES "
        b"('Ana', 'Bom dia equipe', 0), ('Bia', 'Combinado', 1000);"
    )
    parsed = parse_submission("meeting.sql", safe)
    assert parsed.mapping["text"] == "text"
    assert len(parsed.rows) == 2

    with pytest.raises(SubmissionParseError) as rejected:
        parse_submission("meeting.sql", b"DROP TABLE users;")
    assert rejected.value.code == "invalid_sql"
    with app.app_context():
        assert User.query.count() == 3


def test_json_depth_and_xlsx_formula_are_blocked():
    value: object = {"speaker": "Ana", "text": "Olá"}
    for _ in range(25):
        value = {"data": value}
    with pytest.raises(SubmissionParseError) as deep_error:
        parse_submission("deep.json", json.dumps(value).encode())
    assert deep_error.value.code == "json_too_deep"

    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["speaker", "text"])
    sheet.append(["Ana", '=HYPERLINK("https://example.test","texto")'])
    buffer = io.BytesIO()
    workbook.save(buffer)
    with pytest.raises(SubmissionParseError) as formula_error:
        parse_submission("meeting.xlsx", buffer.getvalue())
    assert formula_error.value.code == "xlsx_formula"


def test_retired_human_review_endpoints_do_not_exist(admin_client):
    assert admin_client.get("/api/v1/verifications").status_code == 404
    assert admin_client.get("/api/v1/submissions/pending-count").status_code == 404
    assert admin_client.post("/api/v1/submissions/legacy/decision").status_code == 404
