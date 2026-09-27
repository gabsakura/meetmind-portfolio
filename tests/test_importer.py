from __future__ import annotations

import json

from app.extensions import db
from app.models import Meeting, User
from app.services.importer import import_enriched_json


def test_import_is_idempotent(app, tmp_path):
    sample = [
        {
            "id_reuniao": 1247082,
            "sem_cliente_detectado": False,
            "confianca_cliente": "alta",
            "transcricao": [
                {"locutor": 10, "totvs": False, "falas": {"ordem_0": "Olá"}},
                {"locutor": 11, "totvs": True, "falas": {"ordem_1": "Bem-vindo"}},
            ],
            "metadados": {
                "codt": "T27261",
                "dt_meeting": "2026-03-18 16:00:00",
                "duracao_meeting": "00:01:00",
                "nome_segmento": "SAUDE",
            },
        }
    ]
    source = tmp_path / "banco_json_enriquecido.json"
    source.write_text(json.dumps(sample), encoding="utf-8")
    with app.app_context():
        actor = User.query.filter_by(email="admin@example.test").first()
        first = import_enriched_json(source, actor)
        second = import_enriched_json(source, actor)
        assert first == {"imported": 1, "skipped": 0, "segments": 2}
        assert second == {"imported": 0, "skipped": 1, "segments": 0}
        assert Meeting.query.count() == 1
        assert len(Meeting.query.first().segments) == 2
        db.session.rollback()
