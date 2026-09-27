from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from meeting_normalization import NormalizationError, validate_canonical


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Valida um JSON canônico produzido pelo normalizador."
    )
    parser.add_argument("path", type=Path)
    args = parser.parse_args()
    try:
        document = json.loads(args.path.read_text(encoding="utf-8-sig"))
        report = validate_canonical(document)
    except (OSError, json.JSONDecodeError, NormalizationError) as error:
        print(f"INVÁLIDO: {error}", file=sys.stderr)
        return 1
    print(
        "VÁLIDO: "
        f"{report['segment_count']} segmentos; "
        f"{report['speaker_count']} falantes; "
        f"timestamps={report['timestamp_coverage']:.0%}; "
        f"IA={'sim' if report['needs_ai_review'] else 'não'}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
