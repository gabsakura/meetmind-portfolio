from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

MEETING_TYPES = {
    "support_incident",
    "implementation",
    "relationship_success",
    "commercial_expansion",
    "adoption_training",
    "renewal_contract",
    "discovery",
    "mixed",
    "unknown",
}
HEALTH = {"healthy", "attention", "at_risk", "insufficient_evidence"}


def load_rows(path: Path) -> list[dict]:
    rows = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"Linha {line_number}: JSON inválido") from error
        for key in ("meeting_id", "sector", "expected", "actual", "review"):
            if key not in row:
                raise ValueError(f"Linha {line_number}: campo ausente {key}")
        if row["expected"].get("meeting_type") not in MEETING_TYPES:
            raise ValueError(f"Linha {line_number}: meeting_type esperado inválido")
        if row["actual"].get("meeting_type") not in MEETING_TYPES:
            raise ValueError(f"Linha {line_number}: meeting_type produzido inválido")
        if row["expected"].get("relationship_health") not in HEALTH:
            raise ValueError(f"Linha {line_number}: relationship_health esperado inválido")
        if row["actual"].get("relationship_health") not in HEALTH:
            raise ValueError(f"Linha {line_number}: relationship_health produzido inválido")
        rows.append(row)
    if not rows:
        raise ValueError("Dataset vazio")
    return rows


def macro_f1(rows: list[dict]) -> float:
    labels = sorted({row["expected"]["meeting_type"] for row in rows})
    scores = []
    for label in labels:
        true_positive = sum(
            row["expected"]["meeting_type"] == label
            and row["actual"]["meeting_type"] == label
            for row in rows
        )
        false_positive = sum(
            row["expected"]["meeting_type"] != label
            and row["actual"]["meeting_type"] == label
            for row in rows
        )
        false_negative = sum(
            row["expected"]["meeting_type"] == label
            and row["actual"]["meeting_type"] != label
            for row in rows
        )
        precision = true_positive / (true_positive + false_positive or 1)
        recall = true_positive / (true_positive + false_negative or 1)
        scores.append(2 * precision * recall / (precision + recall or 1))
    return sum(scores) / len(scores)


def cohen_kappa(rows: list[dict]) -> float:
    total = len(rows)
    observed = sum(
        row["expected"]["relationship_health"] == row["actual"]["relationship_health"]
        for row in rows
    ) / total
    expected_counts = Counter(row["expected"]["relationship_health"] for row in rows)
    actual_counts = Counter(row["actual"]["relationship_health"] for row in rows)
    chance = sum(expected_counts[label] * actual_counts[label] for label in HEALTH) / total**2
    return (observed - chance) / (1 - chance) if chance < 1 else 1.0


def evaluate(rows: list[dict]) -> dict:
    evidence_total = 0
    evidence_valid = 0
    factual_assertions = 0
    unsupported = 0
    useful = 0
    metadata_only_risk = 0
    pii_leaks = 0
    for row in rows:
        valid_ids = set(row["expected"].get("valid_segment_ids", []))
        actual_ids = row["actual"].get("evidence_segment_ids", [])
        evidence_total += len(actual_ids)
        evidence_valid += sum(item in valid_ids for item in actual_ids)
        factual_assertions += int(row["review"].get("factual_assertion_count", 0))
        unsupported += int(row["review"].get("unsupported_fact_count", 0))
        useful += bool(row["review"].get("action_useful"))
        metadata_only_risk += bool(row["review"].get("metadata_only_risk"))
        pii_leaks += int(row["review"].get("pii_leak_count", 0))
    return {
        "meetings": len(rows),
        "sectors": sorted({row["sector"] for row in rows}),
        "evidence_validity": evidence_valid / (evidence_total or 1),
        "meeting_type_macro_f1": macro_f1(rows),
        "relationship_health_kappa": cohen_kappa(rows),
        "action_usefulness": useful / len(rows),
        "unsupported_fact_rate": unsupported / (factual_assertions or 1),
        "metadata_only_risk_count": metadata_only_risk,
        "pii_leak_count": pii_leaks,
    }


def gates_pass(report: dict) -> bool:
    return bool(
        report["evidence_validity"] == 1
        and report["meeting_type_macro_f1"] >= 0.85
        and report["relationship_health_kappa"] >= 0.75
        and report["action_usefulness"] >= 0.8
        and report["unsupported_fact_rate"] < 0.01
        and report["metadata_only_risk_count"] == 0
        and report["pii_leak_count"] == 0
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Avalia o gate corporativo da IA MeetMind.")
    parser.add_argument("dataset", type=Path)
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--enforce-gates", action="store_true")
    args = parser.parse_args()
    rows = load_rows(args.dataset)
    report = evaluate(rows)
    report["gates_passed"] = gates_pass(report)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    if args.validate_only:
        return 0
    return 0 if report["gates_passed"] or not args.enforce_gates else 1


if __name__ == "__main__":
    raise SystemExit(main())
