from pathlib import Path


def test_analysis_ui_hides_unknown_model_validation_details():
    source = (Path(__file__).parents[1] / "app" / "static" / "js" / "app.js").read_text(
        encoding="utf-8"
    )

    assert 'code?.startsWith("invalid_model_output_")' in source
    assert "return analysisErrors.invalid_model_output" in source
