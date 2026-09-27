from app.extensions import db
from app.models import Analysis, AnalysisStatus, Meeting
from app.services.analysis_presentation import uncovered_result_fields


def _friendly_result():
    return {
        "executive_summary": {
            "what_happened": "O cliente confirmou avanço, mas pediu correção no prazo.",
            "why_it_matters": "O atraso afeta o aceite da implantação.",
            "next_step": "Confirmar o novo prazo com os responsáveis.",
        },
        "overall_sentiment": "mixed",
        "risk_level": "medium",
        "meeting_classification": {
            "sector": "Saúde",
            "meeting_type": "implementation",
        },
        "account_context": {"sector": "Saúde", "subsector": "Clínica"},
        "relationship_health": {
            "status": "attention",
            "rationale": "Há confiança na solução, mas o prazo precisa ser recuperado.",
            "confidence": 0.87,
        },
        "retention_signals": [
            {
                "signal_type": "attention",
                "label": "Prazo de implantação",
                "description": "O cliente vinculou o aceite à correção do cronograma.",
                "evidence": [
                    {
                        "segment_id": "friendly.0",
                        "quote": "Precisamos do novo prazo para aprovar o aceite.",
                        "relevance": "O prazo foi declarado como condição de aceite.",
                    }
                ],
            }
        ],
        "context_application": [],
        "contextual_priority": "high",
        "products_mentioned": ["Protheus"],
        "findings": [],
        "action_items": [
            {
                "action": "Publicar cronograma revisado",
                "rationale": "Dar previsibilidade ao cliente.",
                "urgency": "next_7_days",
            }
        ],
        "uncertainties": ["A nova data ainda não foi confirmada."],
    }


def test_presentation_covers_every_ai_contract_field():
    assert uncovered_result_fields() == set()


def test_friendly_results_page_requires_login(client):
    response = client.get("/inteligencia-artificial")
    assert response.status_code == 302
    assert "/auth/login" in response.headers["Location"]


def test_friendly_results_page_renders_plain_language(admin_client, app):
    created = admin_client.post(
        "/api/v1/meetings",
        json={
            "external_id": "friendly",
            "title": "Implantação da clínica",
            "client_external_id": "client-friendly",
            "client_name": "Clínica Exemplo",
            "approved_for_external_ai": True,
            "segments": [
                {
                    "id": "friendly.0",
                    "speaker": "Cliente",
                    "text": "Precisamos do novo prazo para aprovar o aceite.",
                }
            ],
        },
    )
    with app.app_context():
        meeting = db.session.get(Meeting, created.json["id"])
        meeting.metadata_json = {
            **(meeting.metadata_json or {}),
            "nome_segmento": "MANUFATURA",
        }
        analysis = Analysis(
            meeting_id=meeting.id,
            requested_by_id=meeting.created_by_id,
            status=AnalysisStatus.SUCCEEDED,
            model="gemini-test",
            prompt_version="meetmind-transparent-v4",
            score_version="voc-deterministic-v2",
            transcript_hash=meeting.transcript_hash,
            result=_friendly_result(),
        )
        db.session.add(analysis)
        db.session.commit()
        analysis_id = analysis.id

    response = admin_client.get(
        "/inteligencia-artificial", query_string={"analysis": analysis_id}
    )
    assert response.status_code == 200
    assert b"Pedidos do cliente" in response.data
    assert "Próximos passos".encode() in response.data
    assert b"Ver auditabilidade" in response.data
    assert b"ai-reading-card-grid" in response.data
    assert b"Contexto comercial e participantes" in response.data
    assert b"ai-transcript-panel" in response.data
    assert b"ai-transcript-search" in response.data


def test_friendly_results_page_has_empty_state(admin_client):
    response = admin_client.get("/inteligencia-artificial")
    assert response.status_code == 200
    assert "Nenhuma reunião analisada".encode() in response.data


def test_legacy_result_route_redirects_to_ai(admin_client):
    response = admin_client.get("/resultados", query_string={"analysis": "example"})
    assert response.status_code == 302
    assert "/inteligencia-artificial?analysis=example" in response.headers["Location"]


def test_reading_cards_keep_commercial_inferences_conservative():
    from pathlib import Path

    script = (
        Path(__file__).parents[1]
        / "app"
        / "static"
        / "js"
        / "inteligencia-artificial.js"
    ).read_text(encoding="utf-8")

    for label in (
        "Produto identificado",
        "Sinal de venda",
        "Ameaça de churn",
        "Persona identificada",
        "Budget do cliente",
        "Sentimento geral",
    ):
        assert label in script
    assert "continuidade não é tratada como venda" in script
    assert "Não representa probabilidade de churn" in script
    assert "revenue_band" not in script
    assert "de ${segments.length} trechos citados" in script
    assert "Pessoa, assunto ou palavra" not in script