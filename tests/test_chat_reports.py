from app.services.chat_reports import (
    ReportDraft,
    ReportSource,
    _verified_sources,
    is_report_request,
    render_report_pdf,
    report_filename,
    requested_meeting_numbers,
)


def test_report_intent_handles_generation_revision_and_clarification():
    assert is_report_request("Gere um relatório comparando as duas reuniões")
    assert is_report_request(
        "Use o risco de churn na comparação",
        {"draft": {"status": "clarify"}},
    )
    assert is_report_request(
        "Mude o resumo para ficar mais curto",
        {"draft": {"status": "ready"}},
    )
    assert not is_report_request("O que significa o risco de churn?")


def test_requested_meeting_ids_keep_explicit_and_prior_ids():
    ids = requested_meeting_numbers(
        "Gere relatório da reunião 721888 e da reunião 990001",
        {"meeting_external_ids": ["721888", "440004"]},
    )
    assert ids == ["721888", "990001", "440004"]


def test_report_sources_must_match_a_stored_quote_and_meeting():
    sources = [
        ReportSource(
            meeting_external_id="721888",
            segment_id="721888.4.0",
            quote="Revisar o cronograma",
            relevance="Sustenta o objetivo discutido.",
        ),
        ReportSource(
            meeting_external_id="999999",
            segment_id="999999.1.0",
            quote="Sinal não fornecido",
            relevance="Referência não disponível.",
        ),
    ]
    meetings = [{
        "id_externo": "721888",
        "resultado_analise": {
            "findings": [{"evidence": [{
                "segment_id": "721888.4.0",
                "quote": "Revisar o cronograma com o cliente.",
            }]}]
        },
    }]

    verified, rejected = _verified_sources(sources, meetings)

    assert [source.meeting_external_id for source in verified] == ["721888"]
    assert rejected is True


def test_render_report_pdf_returns_downloadable_pdf_and_safe_filename():
    draft = ReportDraft(
        status="ready",
        answer="Relatório pronto para baixar.",
        title="Objetivos e risco de churn",
        subtitle="Comparação das reuniões selecionadas",
        summary="As análises mostram objetivos operacionais distintos e sinais de atenção.",
        sections=[
            {
                "title": "Objetivos discutidos",
                "paragraphs": ["A reunião 721888 priorizou revisar o cronograma."],
                "bullets": ["O status do objetivo foi parcial."],
            }
        ],
        conclusion="Acompanhe os itens que ainda não foram resolvidos.",
        sources=[
            {
                "meeting_external_id": "721888",
                "segment_id": "721888.4.0",
                "quote": "Revisar o cronograma com o cliente.",
                "relevance": "Trecho que sustenta o objetivo registrado.",
            }
        ],
    )
    pdf_data = render_report_pdf(
        draft,
        [{
            "titulo": "Reunião de alinhamento",
            "id_externo": "721888",
            "cliente": "Cliente",
            "data": "2026-09-22",
            "resultado_analise": {
                "findings": [{
                    "evidence": [{
                        "segment_id": "721888.4.0",
                        "quote": "Revisar o cronograma com o cliente.",
                    }]
                }]
            },
        }],
    )

    assert pdf_data.startswith(b"%PDF-")
    assert report_filename(draft.title) == "relatorio_objetivos_e_risco_de_churn.pdf"
