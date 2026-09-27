from __future__ import annotations

from app.models import Meeting, Segment
from app.services.metrics import calculate_metrics


def test_deterministic_metrics():
    meeting = Meeting(duration_seconds=60, metadata_json={"nota_nps": 9})
    meeting.segments = [
        Segment(id="1.0", position=0, speaker="Cliente", is_totvs=False, text="Ótimo, obrigado."),
        Segment(id="1.1", position=1, speaker="TOTVS", is_totvs=True, text="Como posso ajudar?"),
    ]
    metrics = calculate_metrics(meeting)
    assert metrics["deterministic"] is True
    assert metrics["score_voc"] > 0
    assert metrics["nps"] == 9
    assert metrics["client_word_share"] == 40.0
    assert metrics["trigger_events"][0]["term"] in {"ótimo", "obrigado"}
    assert metrics["trigger_events"][0]["evidence"][0]["segment_id"] == "1.0"


def test_negated_problem_and_courtesy_do_not_change_score():
    meeting = Meeting(duration_seconds=30, metadata_json={})
    meeting.segments = [
        Segment(
            id="2.0",
            position=0,
            speaker="Cliente",
            is_totvs=False,
            text="Não tem problema, obrigado.",
        )
    ]
    metrics = calculate_metrics(meeting)
    assert metrics["score_voc"] == 0
    assert metrics["trigger_events"] == []
