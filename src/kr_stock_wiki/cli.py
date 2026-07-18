from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from .harness import ResearchHarness
from .models import Candidate, Signal, SignalGroup
from .wiki_lint import lint_wiki


def _number(value: Any, field: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{field} must be a finite number")
    return number


def _load_candidates(path: Path) -> tuple[datetime, str, list[Candidate]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    observed = datetime.fromisoformat(data["as_of"])
    candidates = []
    for item in data["candidates"]:
        signals = [
            Signal(
                SignalGroup(signal["group"]),
                _number(signal["score"], "signal.score"),
                signal["reason"],
                signal["source_url"],
                datetime.fromisoformat(signal.get("observed_at", data["as_of"])),
                evidence_id=signal.get("evidence_id"),
            )
            for signal in item["signals"]
        ]
        candidates.append(
            Candidate(
                ticker=item["ticker"],
                name=item["name"],
                signals=signals,
                risk_penalty=_number(item.get("risk_penalty", 0), "risk_penalty"),
                hard_exclusion=item.get("hard_exclusion"),
            )
        )
    return observed, data["mode"], candidates


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kr-stock-wiki")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="JSON 신호 입력으로 Wiki 리포트를 생성합니다")
    run.add_argument("--input", required=True, type=Path)
    run.add_argument("--output", required=True, type=Path)
    lint = commands.add_parser("lint", help="Wiki 무결성을 검사합니다")
    lint.add_argument("--wiki", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "run":
        try:
            observed, mode, candidates = _load_candidates(args.input)
            result = ResearchHarness().run(candidates, observed, mode, args.output)
        except (OSError, ValueError, KeyError, TypeError) as error:
            print(f"입력 오류: {error}", file=sys.stderr)
            return 2
        print(f"generated={len(result.reports)} index={result.index_path}")
        return 0
    issues = lint_wiki(args.wiki)
    for issue in issues:
        print(f"{issue.code}: {issue.path}: {issue.message}")
    return 1 if issues else 0


if __name__ == "__main__":
    raise SystemExit(main())
