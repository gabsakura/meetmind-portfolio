from __future__ import annotations

import argparse
import sys
from pathlib import Path

from meeting_normalization import (
    SUPPORTED_EXTENSIONS,
    NormalizationError,
    ai_handoff,
    normalize_file,
    write_json,
)


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Converte transcrições para o JSON canônico do MeetMind."
    )
    parser.add_argument("input", type=Path, help="Arquivo ou diretório de entrada.")
    parser.add_argument("--output", type=Path, help="Arquivo ou diretório de saída.")
    parser.add_argument("--title", help="Título da reunião (modo de arquivo único).")
    parser.add_argument("--client-name", help="Cliente (modo de arquivo único).")
    parser.add_argument("--occurred-at", help="Data/hora ISO-8601.")
    parser.add_argument(
        "--language",
        choices=("pt-BR", "en-US", "es-ES"),
        default="pt-BR",
    )
    parser.add_argument(
        "--ai-handoff",
        action="store_true",
        help="Gera pacote local para revisão por IA quando houver ambiguidade.",
    )
    return parser.parse_args()


def normalize_one(source: Path, output: Path, args: argparse.Namespace) -> bool:
    document = normalize_file(
        source,
        title=args.title,
        client_name=args.client_name,
        occurred_at=args.occurred_at,
        language=args.language,
    )
    write_json(output, document)
    report = document["normalization_report"]
    if args.ai_handoff and report["needs_ai_review"]:
        handoff = output.with_name(f"{output.stem}.ai-review.json")
        write_json(handoff, ai_handoff(document))
        print(f"  revisão por IA sugerida: {handoff}")
    print(
        f"OK {source.name} -> {output} | "
        f"{report['segment_count']} segmentos | "
        f"IA={'sim' if report['needs_ai_review'] else 'não'}"
    )
    return True


def main() -> int:
    args = arguments()
    source = args.input.resolve()
    if not source.exists():
        print(f"Erro: entrada não encontrada: {source}", file=sys.stderr)
        return 2
    try:
        if source.is_file():
            output = (
                args.output.resolve()
                if args.output
                else source.with_name(f"{source.stem}.meetmind.json")
            )
            normalize_one(source, output, args)
            return 0

        output_dir = (
            args.output.resolve()
            if args.output
            else source / "meetmind-normalized"
        )
        files = sorted(
            item
            for item in source.iterdir()
            if item.is_file() and item.suffix.casefold() in SUPPORTED_EXTENSIONS
        )
        if not files:
            raise NormalizationError("O diretório não contém formatos suportados.")
        failures = 0
        for item in files:
            try:
                normalize_one(
                    item,
                    output_dir / f"{item.stem}.meetmind.json",
                    args,
                )
            except (NormalizationError, OSError) as error:
                failures += 1
                print(f"FALHA {item.name}: {error}", file=sys.stderr)
        print(f"Concluído: {len(files) - failures} OK; {failures} falha(s).")
        return 1 if failures else 0
    except (NormalizationError, OSError) as error:
        print(f"Erro: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
