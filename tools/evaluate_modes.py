from __future__ import annotations

import argparse
import json
import unicodedata
from collections import defaultdict
from pathlib import Path
from typing import Any

MODES = (
    "voc_complaint",
    "meeting_outcome",
    "product_operations",
    "relationship_risk",
)


def normalized(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", str(value).casefold())
    return "".join(char for char in decomposed if not unicodedata.combining(char)).strip()


def evidence_items(value: Any):
    if isinstance(value, list):
        for item in value:
            yield from evidence_items(item)
    elif isinstance(value, dict):
        if {"segment_id", "quote", "relevance"}.issubset(value):
            yield value
        for item in value.values():
            yield from evidence_items(item)


def safe_div(numerator: float, denominator: float) -> float:
    return numerator / denominator if denominator else 0.0


def load_cases(path: Path) -> list[dict]:
    cases = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            cases.append(json.loads(line))
        except json.JSONDecodeError as error:
            raise SystemExit(f"Linha {line_number}: JSON inválido: {error}") from error
    return cases


def evaluate(cases: list[dict]) -> tuple[dict, bool]:
    schema_valid = citations_valid = 0
    complaint_true = complaint_predicted = 0
    type_matches = risk_matches = 0
    type_total = risk_total = 0
    unsupported_claims = 0
    mode_counts = defaultdict(lambda: {"tp": 0, "fp": 0, "fn": 0})
    blind = defaultdict(int)

    for case in cases:
        prediction = case.get("prediction_modes") or {}
        human = case.get("human") or {}
        routing = prediction.get("routing") or {}
        mode_results = prediction.get("mode_results") or {}
        synthesis = prediction.get("synthesis") or {}
        valid = (
            prediction.get("schema_version") == "meetmind-modes-v1"
            and isinstance(routing.get("selected_modes"), list)
            and 1 <= len(routing["selected_modes"]) <= 2
            and isinstance(mode_results, dict)
            and isinstance(synthesis.get("executive_summary"), dict)
        )
        schema_valid += int(valid)

        segments = {item["id"]: item["text"] for item in case.get("segments") or []}
        citation_ok = True
        for evidence in evidence_items({"routing": routing, "mode_results": mode_results}):
            segment_text = segments.get(evidence["segment_id"])
            if not segment_text or evidence["quote"] not in segment_text:
                citation_ok = False
                break
        citations_valid += int(citation_ok)

        expected_modes = set(human.get("expected_modes") or [])
        predicted_modes = set(routing.get("selected_modes") or [])
        for mode in MODES:
            if mode in expected_modes and mode in predicted_modes:
                mode_counts[mode]["tp"] += 1
            elif mode in predicted_modes:
                mode_counts[mode]["fp"] += 1
            elif mode in expected_modes:
                mode_counts[mode]["fn"] += 1

        expected_targets = {normalized(item) for item in human.get("complaint_targets") or []}
        complaints = (mode_results.get("voc_complaint") or {}).get("complaints") or []
        predicted_targets = [normalized(item.get("target", "")) for item in complaints]
        complaint_predicted += len(predicted_targets)
        complaint_true += sum(target in expected_targets for target in predicted_targets)

        if human.get("meeting_type") is not None:
            type_total += 1
            type_matches += int(routing.get("meeting_type") == human["meeting_type"])
        predicted_risk = (mode_results.get("relationship_risk") or {}).get("risk_level")
        if human.get("risk_level") is not None and predicted_risk is not None:
            risk_total += 1
            risk_matches += int(predicted_risk == human["risk_level"])
        unsupported_claims += int(human.get("unsupported_claims", 0))
        blind[case.get("blind_preference", "not_reviewed")] += 1

    f1_values = []
    for values in mode_counts.values():
        precision = safe_div(values["tp"], values["tp"] + values["fp"])
        recall = safe_div(values["tp"], values["tp"] + values["fn"])
        f1_values.append(safe_div(2 * precision * recall, precision + recall))
    total = len(cases)
    metrics = {
        "cases": total,
        "schema_valid_rate": safe_div(schema_valid, total),
        "literal_citation_rate": safe_div(citations_valid, total),
        "unsupported_claims": unsupported_claims,
        "complaint_precision": safe_div(complaint_true, complaint_predicted),
        "routing_macro_f1": safe_div(sum(f1_values), len(f1_values)),
        "meeting_type_agreement": safe_div(type_matches, type_total),
        "risk_level_agreement": safe_div(risk_matches, risk_total),
        "blind_preference": dict(blind),
    }
    passed = (
        30 <= total <= 50
        and metrics["schema_valid_rate"] == 1
        and metrics["literal_citation_rate"] == 1
        and unsupported_claims == 0
        and metrics["complaint_precision"] >= 0.9
        and metrics["routing_macro_f1"] >= 0.8
        and metrics["meeting_type_agreement"] >= 0.8
        and metrics["risk_level_agreement"] >= 0.8
    )
    return metrics, passed


def main() -> int:
    parser = argparse.ArgumentParser(description="Avalia o gate humano do MeetMind Modes v1.")
    parser.add_argument("dataset", type=Path)
    args = parser.parse_args()
    metrics, passed = evaluate(load_cases(args.dataset))
    print(json.dumps({"passed": passed, "metrics": metrics}, ensure_ascii=False, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
