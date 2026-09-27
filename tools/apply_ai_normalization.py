from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from meeting_normalization import (
    NormalizationError,
    apply_ai_suggestions,
    write_json,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Aplica sugestões estruturais da IA sem permitir alteração das falas."
        )
    )
    parser.add_argument("meeting", type=Path, help="JSON canônico original.")
    parser.add_argument("ai_response", type=Path, help="Resposta JSON da IA.")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--min-confidence", type=float, default=0.85)
    args = parser.parse_args()
    if not 0 <= args.min_confidence <= 1:
        print("INVÁLIDO: --min-confidence deve estar entre 0 e 1.", file=sys.stderr)
        return 2
    try:
        meeting = json.loads(args.meeting.read_text(encoding="utf-8-sig"))
        response = json.loads(args.ai_response.read_text(encoding="utf-8-sig"))
        updated, audit = apply_ai_suggestions(
            meeting,
            response,
            min_confidence=args.min_confidence,
        )
        write_json(args.output, updated)
    except (OSError, json.JSONDecodeError, NormalizationError) as error:
        print(f"INVÁLIDO: {error}", file=sys.stderr)
        return 1
    print(
        f"OK: {len(audit['applied'])} aplicada(s); "
        f"{len(audit['rejected'])} rejeitada(s); "
        f"texto_inalterado={audit['text_unchanged']}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
