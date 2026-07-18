from __future__ import annotations

import argparse
import json
import math
import os
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .collectors.dart import DartClient
from .collectors.krx import KrxClient
from .collectors.nxt import NxtClient
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


def _write_json_atomic(output: Path, payload: dict[str, Any]) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=output.parent,
            prefix=f".{output.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            json.dump(
                payload,
                temporary,
                ensure_ascii=False,
                indent=2,
                allow_nan=False,
            )
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, output)
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        directory_fd = os.open(output.parent, directory_flags)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kr-stock-wiki")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="JSON 신호 입력으로 Wiki 리포트를 생성합니다")
    run.add_argument("--input", required=True, type=Path)
    run.add_argument("--output", required=True, type=Path)
    lint = commands.add_parser("lint", help="Wiki 무결성을 검사합니다")
    lint.add_argument("--wiki", required=True, type=Path)
    dart = commands.add_parser(
        "collect-dart", help="OpenDART 공식 공시를 JSON 근거 스냅샷으로 수집합니다"
    )
    dart.add_argument("--begin", required=True, type=date.fromisoformat)
    dart.add_argument("--end", required=True, type=date.fromisoformat)
    dart.add_argument("--corp-code")
    dart.add_argument("--output", required=True, type=Path)
    krx = commands.add_parser(
        "collect-krx", help="KRX 공식 KOSPI·KOSDAQ 일별 시세를 수집합니다"
    )
    krx.add_argument("--date", required=True, type=date.fromisoformat)
    krx.add_argument("--output", required=True, type=Path)
    nxt = commands.add_parser(
        "collect-nxt", help="NXT 공식 20분 지연 시세와 세션별 거래 현황을 수집합니다"
    )
    nxt.add_argument("--date", required=True, type=date.fromisoformat)
    nxt.add_argument("--output", required=True, type=Path)
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
    if args.command == "collect-nxt":
        try:
            client = NxtClient()
            records = client.daily_quotes(args.date)
            summary = client.session_summary(args.date)
            if summary is not None:
                records.append(summary)
            payload = {
                "schema_version": 1,
                "source": "nxt",
                "collected_at": datetime.now().astimezone().isoformat(),
                "date": args.date.isoformat(),
                "quote_delay_minutes": 20,
                "records": [record.to_dict() for record in records],
            }
            _write_json_atomic(args.output, payload)
        except (
            OSError,
            ValueError,
            KeyError,
            TypeError,
            json.JSONDecodeError,
        ) as error:
            print(f"NXT 수집 오류: {error}", file=sys.stderr)
            return 2
        print(f"collected={len(records)} output={args.output}")
        return 0
    if args.command == "collect-krx":
        api_key = os.environ.get("KRX_API_KEY")
        if not api_key:
            print("환경변수 KRX_API_KEY가 필요합니다", file=sys.stderr)
            return 2
        try:
            records = KrxClient(api_key=api_key).daily_prices(args.date)
            payload = {
                "schema_version": 1,
                "source": "krx",
                "collected_at": datetime.now().astimezone().isoformat(),
                "date": args.date.isoformat(),
                "markets": ["KOSPI", "KOSDAQ"],
                "records": [record.to_dict() for record in records],
            }
            _write_json_atomic(args.output, payload)
        except (
            OSError,
            ValueError,
            KeyError,
            TypeError,
            json.JSONDecodeError,
        ) as error:
            print(f"KRX 수집 오류: {error}", file=sys.stderr)
            return 2
        print(f"collected={len(records)} output={args.output}")
        return 0
    if args.command == "collect-dart":
        api_key = os.environ.get("DART_API_KEY")
        if not api_key:
            print("환경변수 DART_API_KEY가 필요합니다", file=sys.stderr)
            return 2
        try:
            records = DartClient(api_key=api_key).search(
                args.begin, args.end, corp_code=args.corp_code
            )
            payload = {
                "schema_version": 1,
                "source": "dart",
                "collected_at": datetime.now().astimezone().isoformat(),
                "begin": args.begin.isoformat(),
                "end": args.end.isoformat(),
                "corp_code": args.corp_code,
                "records": [record.to_dict() for record in records],
            }
            _write_json_atomic(args.output, payload)
        except (
            OSError,
            ValueError,
            KeyError,
            TypeError,
            json.JSONDecodeError,
        ) as error:
            print(f"DART 수집 오류: {error}", file=sys.stderr)
            return 2
        print(f"collected={len(records)} output={args.output}")
        return 0
    issues = lint_wiki(args.wiki)
    for issue in issues:
        print(f"{issue.code}: {issue.path}: {issue.message}")
    return 1 if issues else 0


if __name__ == "__main__":
    raise SystemExit(main())
