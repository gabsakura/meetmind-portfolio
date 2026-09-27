from app.models import Meeting, Segment
from app.services.sector_context import (
    MEETING_TYPES,
    build_context_snapshot,
    public_context_snapshot,
    validate_playbooks,
)


def meeting_with(metadata=None, text="Conversa sem vocabulário específico"):
    meeting = Meeting(
        external_id="context-test",
        title="Reunião",
        transcript_hash="a" * 64,
        created_by_id="user-test",
        metadata_json=metadata or {},
    )
    meeting.segments.append(
        Segment(id="context-test.0", position=0, text=text, speaker="Cliente")
    )
    return meeting


def test_catalog_contains_all_dataset_sectors_and_fallback():
    report = validate_playbooks()
    assert report["count"] == 11
    assert set(report["ids"]) == {
        "agroindustria",
        "construcao-projetos",
        "distribuicao",
        "educacional",
        "generico",
        "logistica",
        "manufatura",
        "saude",
        "servicos",
        "supermercados",
        "varejo",
    }


def test_registered_sector_has_priority_and_preserves_account_context():
    snapshot = build_context_snapshot(
        meeting_with(
            {
                "nome_segmento": "SAUDE",
                "cnae": "8610-1/02",
                "regiao_brasil": "Centro-Oeste",
                "uf": "DF",
                "faixa_faturamento_cliente_ec": "SMB III",
            },
            "Falamos sobre prontuário e faturamento.",
        )
    )
    assert snapshot["playbook"]["id"] == "saude"
    assert snapshot["selection"]["source"] == "metadata"
    assert snapshot["account"]["region"] == "Centro-Oeste"
    assert snapshot["account"]["revenue_band"] == "SMB III"
    assert snapshot["context_policy"]["metadata_cannot_raise_relationship_risk_alone"]


def test_specific_supermarket_cnae_wins_over_broad_retail_prefix():
    snapshot = build_context_snapshot(meeting_with({"cnae": "4711-3/02"}))
    assert snapshot["playbook"]["id"] == "supermercados"
    assert snapshot["selection"]["source"] == "cnae"


def test_manufacturing_v2_covers_food_beverage_and_tobacco_cnae():
    for cnae in ("1011-2/01", "1113-5/02", "1220-4/99"):
        snapshot = build_context_snapshot(meeting_with({"cnae": cnae}))
        assert snapshot["playbook"]["id"] == "manufatura"
        assert snapshot["playbook"]["version"] == "manufatura-v2"
        assert snapshot["context_version"] == "sector-playbooks-v2"


def test_conflicting_metadata_falls_back_without_silent_choice():
    snapshot = build_context_snapshot(
        meeting_with({"nome_segmento": "SAUDE", "cnae": "4711-3/02"})
    )
    assert snapshot["playbook"]["id"] == "generico"
    assert snapshot["selection"]["source"] == "conflict"
    assert snapshot["uncertainties"]


def test_transcript_inference_is_low_confidence_and_does_not_fill_metadata():
    snapshot = build_context_snapshot(
        meeting_with(text="O paciente usa prontuário e precisamos enviar a guia TISS ao convênio.")
    )
    assert snapshot["playbook"]["id"] == "saude"
    assert snapshot["selection"]["source"] == "transcript"
    assert snapshot["selection"]["confidence"] == "low"
    assert snapshot["account"]["sector"] is None


def test_every_snapshot_has_rules_for_all_meeting_types():
    snapshot = build_context_snapshot(meeting_with({"nome_segmento": "SAUDE"}))
    assert set(snapshot["meeting_type_rules"]) == MEETING_TYPES


def test_public_preview_does_not_expose_internal_prompt_guidance():
    snapshot = build_context_snapshot(meeting_with({"nome_segmento": "SAUDE"}))
    public = public_context_snapshot(snapshot)
    assert public["context_sha256"] == snapshot["context_sha256"]
    assert "knowledge" not in public
    assert "signals" not in public
    assert "meeting_type_rules" not in public
    assert "prohibited_inferences" not in public
