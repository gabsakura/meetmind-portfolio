from __future__ import annotations

from types import SimpleNamespace

import httpx

from app import create_app


def test_gemini_probe_runs_when_local_database_is_not_migrated(monkeypatch, tmp_path):
    app = create_app(
        "config.TestConfig",
        {"SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / 'unmigrated.sqlite3'}"},
    )

    class FakeClient:
        models = SimpleNamespace(
            generate_content=lambda **_kwargs: SimpleNamespace(text='{"status":"ok"}')
        )

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr("app.cli.ai_is_configured", lambda: True)
    monkeypatch.setattr("app.cli.ai_client", FakeClient)

    result = app.test_cli_runner().invoke(args=["gemini-status", "--probe"])

    assert result.exit_code == 0
    assert "banco indisponível" in result.output
    assert "Validação de IA: identidade e modelo acessíveis." in result.output


def test_gemini_probe_reports_network_failure_without_traceback(monkeypatch, tmp_path):
    app = create_app(
        "config.TestConfig",
        {"SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / 'unmigrated.sqlite3'}"},
    )

    class FailingClient:
        def __enter__(self):
            raise httpx.ConnectError("network unavailable")

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr("app.cli.ai_is_configured", lambda: True)
    monkeypatch.setattr("app.cli.ai_client", FailingClient)

    result = app.test_cli_runner().invoke(args=["gemini-status", "--probe"])

    assert result.exit_code == 1
    assert "Não foi possível acessar o Gemini" in result.output
    assert "Traceback" not in result.output
