from __future__ import annotations

import argparse
import json
import math
import os
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .collectors.calendar import KrxCalendarClient
from .collectors.dart import DartClient
from .collectors.kind import KindClient
from .collectors.krx import KrxClient, KrxDailySnapshot
from .collectors.market_notices import KrxMarketNoticeClient
from .collectors.news import NewsFeed, YonhapRssClient
from .collectors.nxt import NxtClient
from .evidence import EvidenceRecord
from .harness import ResearchHarness
from .market_rules import (
    ListingRisk,
    MarketDayStatus,
    OperationalEvidence,
    TradingDayGate,
)
from .models import Candidate, Signal, SignalGroup
from .wiki_lint import lint_wiki

_KST = ZoneInfo("Asia/Seoul")


def _number(value: Any, field: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{field} must be a finite number")
    return number


def _load_candidates(path: Path) -> tuple[datetime, date, str, list[Candidate]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    observed = datetime.fromisoformat(data["as_of"])
    business_date = date.fromisoformat(data["business_date"])
    if observed.tzinfo is None or observed.utcoffset() is None:
        raise ValueError("as_of must include a timezone")
    analysis_date = observed.astimezone(_KST).date()
    mode = data["mode"]
    if mode not in {"pre-market", "post-market"}:
        raise ValueError("mode must be pre-market or post-market")
    if mode == "post-market" and business_date != analysis_date:
        raise ValueError("post-market business_date must match the KST analysis date")
    if mode == "pre-market" and business_date >= analysis_date:
        raise ValueError("pre-market business_date must precede the KST analysis date")
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
    return observed, business_date, mode, candidates


def _load_krx_snapshot(path: Path) -> KrxDailySnapshot:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("KRX snapshot must be a JSON object")
    return KrxDailySnapshot.from_payload(payload)


def _load_listing_risks(
    path: Path,
    *,
    analysis_time: datetime,
    candidate_tickers: set[str],
) -> dict[str, ListingRisk]:
    analysis_date = analysis_time.astimezone(_KST).date()
    payload = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != 1
        or payload.get("source") != "kind"
        or payload.get("coverage_complete") is not True
    ):
        raise ValueError("invalid or incomplete KIND status snapshot")
    if date.fromisoformat(str(payload.get("date"))) != analysis_date:
        raise ValueError("KIND status snapshot date must match analysis date")
    collected_at = datetime.fromisoformat(str(payload.get("collected_at")))
    if (
        collected_at.tzinfo is None
        or collected_at.utcoffset() is None
        or collected_at.astimezone(_KST).date() != analysis_date
        or collected_at > analysis_time
        or analysis_time - collected_at > timedelta(hours=1)
    ):
        raise ValueError(
            "KIND status snapshot must be collected within one hour before analysis"
        )
    requested = payload.get("requested_tickers")
    completed = payload.get("completed_tickers")
    records_raw = payload.get("records")
    if (
        not isinstance(requested, list)
        or not isinstance(completed, list)
        or not isinstance(records_raw, list)
    ):
        raise ValueError("invalid KIND status coverage metadata")
    requested_set = set(requested)
    completed_set = set(completed)
    if (
        len(requested_set) != len(requested)
        or len(completed_set) != len(completed)
        or completed_set != requested_set
        or not candidate_tickers <= completed_set
    ):
        raise ValueError("KIND status snapshot does not cover every candidate")
    risks: dict[str, ListingRisk] = {}
    for raw in records_raw:
        record = EvidenceRecord.from_dict(raw)
        if (
            record.fetched_at.astimezone(_KST).date() != analysis_date
            or record.fetched_at > analysis_time
            or analysis_time - record.fetched_at > timedelta(hours=1)
        ):
            raise ValueError(
                "KIND status evidence must be fetched within one hour before analysis"
            )
        if record.ticker is None or record.ticker in risks:
            raise ValueError("KIND status records require unique tickers")
        risks[record.ticker] = ListingRisk(
            ticker=record.ticker,
            as_of=analysis_date,
            evidence=record,
        )
    if set(risks) != completed_set:
        raise ValueError("KIND status records do not match completed tickers")
    if (
        max(risk.evidence.fetched_at for risk in risks.values() if risk.evidence)
        != collected_at
    ):
        raise ValueError("KIND status collected_at does not match evidence")
    return {ticker: risks[ticker] for ticker in candidate_tickers}


def _operational_evidence(
    candidates: list[Candidate],
    *,
    observed: datetime,
    business_date: date,
    krx_snapshot: KrxDailySnapshot,
    listing_risks: dict[str, ListingRisk],
) -> dict[str, OperationalEvidence]:
    if krx_snapshot.business_date != business_date:
        raise ValueError("KRX snapshot date must match candidate business_date")
    if (
        krx_snapshot.fetched_at > observed
        or observed - krx_snapshot.fetched_at > timedelta(hours=12)
    ):
        raise ValueError("KRX snapshot must be fetched within 12 hours before analysis")
    market_day = TradingDayGate().assess(business_date, krx_snapshot)
    if market_day.status is not MarketDayStatus.OPEN:
        raise ValueError(f"KRX 거래일 확인 실패: {market_day.reason}")
    prices: dict[str, EvidenceRecord] = {}
    for record in krx_snapshot.records:
        if record.ticker is None:
            continue
        if record.ticker in prices:
            raise ValueError("KRX snapshot contains duplicate tickers")
        prices[record.ticker] = record
    return {
        candidate.ticker: OperationalEvidence(
            candidate.ticker,
            prices.get(candidate.ticker),
            listing_risks[candidate.ticker],
        )
        for candidate in candidates
    }


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
    run.add_argument("--krx-snapshot", required=True, type=Path)
    run.add_argument("--kind-status", required=True, type=Path)
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
    calendar = commands.add_parser(
        "collect-calendar", help="Global KRX 공식 연간 휴장일 캘린더를 수집합니다"
    )
    calendar.add_argument("--year", required=True, type=int)
    calendar.add_argument("--output", required=True, type=Path)
    notices = commands.add_parser(
        "collect-market-notices",
        help="KRX 공식 시장운영 공지 목록을 완전한 JSON 스냅샷으로 수집합니다",
    )
    notices.add_argument("--begin", required=True, type=date.fromisoformat)
    notices.add_argument("--end", required=True, type=date.fromisoformat)
    notices.add_argument("--output", required=True, type=Path)
    kind = commands.add_parser(
        "collect-kind", help="KRX KIND 공식 관리·정지·투자경고 상태를 수집합니다"
    )
    kind.add_argument("--date", required=True, type=date.fromisoformat)
    kind.add_argument("--ticker", required=True, action="append")
    kind.add_argument("--output", required=True, type=Path)
    nxt = commands.add_parser(
        "collect-nxt", help="NXT 공식 20분 지연 시세와 세션별 거래 현황을 수집합니다"
    )
    nxt.add_argument("--date", required=True, type=date.fromisoformat)
    nxt.add_argument("--output", required=True, type=Path)
    news = commands.add_parser(
        "collect-news", help="연합뉴스 공식 RSS 기사를 수집합니다"
    )
    news.add_argument("--begin", required=True, type=date.fromisoformat)
    news.add_argument("--end", required=True, type=date.fromisoformat)
    news.add_argument("--output", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "run":
        try:
            observed, business_date, mode, candidates = _load_candidates(args.input)
            if mode == "pre-market":
                raise ValueError(
                    "pre-market 실행에는 캘린더 외에 공식 KRX 당일 운영상태 근거가 필요합니다"
                )
            krx_snapshot = _load_krx_snapshot(args.krx_snapshot)
            listing_risks = _load_listing_risks(
                args.kind_status,
                analysis_time=observed,
                candidate_tickers={candidate.ticker for candidate in candidates},
            )
            operational_evidence = _operational_evidence(
                candidates,
                observed=observed,
                business_date=business_date,
                krx_snapshot=krx_snapshot,
                listing_risks=listing_risks,
            )
            result = ResearchHarness().run(
                candidates,
                observed,
                mode,
                args.output,
                operational_evidence=operational_evidence,
            )
        except (OSError, ValueError, KeyError, TypeError) as error:
            print(f"입력 오류: {error}", file=sys.stderr)
            return 2
        print(f"generated={len(result.reports)} index={result.index_path}")
        return 0
    if args.command == "collect-market-notices":
        try:
            snapshot = KrxMarketNoticeClient().notices(args.begin, args.end)
            _write_json_atomic(args.output, snapshot.to_payload())
        except (OSError, ValueError, KeyError, TypeError) as error:
            print(f"KRX 시장운영 공지 수집 오류: {error}", file=sys.stderr)
            return 2
        print(f"collected={snapshot.total_count} output={args.output}")
        return 0
    if args.command == "collect-calendar":
        try:
            snapshot = KrxCalendarClient().annual_calendar(args.year)
            _write_json_atomic(args.output, snapshot.to_payload())
        except (OSError, ValueError, KeyError, TypeError) as error:
            print(f"KRX 캘린더 수집 오류: {error}", file=sys.stderr)
            return 2
        print(f"collected={len(snapshot.holidays)} output={args.output}")
        return 0
    if args.command == "collect-kind":
        try:
            records = KindClient().statuses(args.ticker, args.date)
            collected_at = max(record.fetched_at for record in records)
            payload = {
                "schema_version": 1,
                "source": "kind",
                "coverage_complete": True,
                "collected_at": collected_at.isoformat(),
                "date": args.date.isoformat(),
                "requested_tickers": list(args.ticker),
                "completed_tickers": [record.ticker for record in records],
                "records": [record.to_dict() for record in records],
            }
            _write_json_atomic(args.output, payload)
        except (OSError, ValueError, KeyError, TypeError) as error:
            print(f"KIND 수집 오류: {error}", file=sys.stderr)
            return 2
        print(f"collected={len(records)} output={args.output}")
        return 0
    if args.command == "collect-news":
        try:
            records = YonhapRssClient().latest(args.begin, args.end)
            payload = {
                "schema_version": 1,
                "source": "official-news",
                "coverage_complete": True,
                "publisher": "연합뉴스",
                "feeds": [feed.value for feed in NewsFeed],
                "collected_at": datetime.now().astimezone().isoformat(),
                "begin": args.begin.isoformat(),
                "end": args.end.isoformat(),
                "records": [record.to_dict() for record in records],
            }
            _write_json_atomic(args.output, payload)
        except (
            OSError,
            ValueError,
            KeyError,
            TypeError,
        ) as error:
            print(f"뉴스 수집 오류: {error}", file=sys.stderr)
            return 2
        print(f"collected={len(records)} output={args.output}")
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
            snapshot = KrxClient(api_key=api_key).daily_snapshot(args.date)
            records = snapshot.records
            payload = snapshot.to_payload()
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
