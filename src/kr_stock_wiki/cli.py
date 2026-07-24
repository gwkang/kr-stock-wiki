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

from .collectors.calendar import (
    KrxCalendarBundle,
    KrxCalendarClient,
    KrxMarketCalendar,
)
from .collectors.dart import DartClient
from .collectors.kind import KindClient
from .collectors.kind_market_notices import KindMarketNoticeClient
from .collectors.kis import KisClient, KisDailySnapshot
from .collectors.krx import KrxClient, KrxDailySnapshot
from .collectors.krx_live import KrxLiveActivitySnapshot, KrxLiveClient
from .collectors.market_notices import KrxMarketNoticeClient
from .collectors.news import NewsFeed, YonhapRssClient
from .collectors.nxt import NxtClient
from .daily import build_morning_input, build_post_market_input, build_pre_market_input
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


def _load_candidates(
    path: Path,
) -> tuple[datetime, date, str, str, list[Candidate]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    envelope_fields = {
        "schema_version",
        "source",
        "as_of",
        "business_date",
        "mode",
        "candidates",
    }
    allowed_sources = {
        "manual-research-input",
        "official-pre-market-builder",
        "official-post-market-builder",
        "official-morning-builder",
    }
    if (
        not isinstance(data, dict)
        or set(data) != envelope_fields
        or data.get("schema_version") != 1
        or data.get("source") not in allowed_sources
        or not isinstance(data.get("candidates"), list)
    ):
        raise ValueError("invalid candidate input envelope")
    source = data["source"]
    observed = datetime.fromisoformat(data["as_of"])
    business_date = date.fromisoformat(data["business_date"])
    if observed.tzinfo is None or observed.utcoffset() is None:
        raise ValueError("as_of must include a timezone")
    analysis_date = observed.astimezone(_KST).date()
    mode = data["mode"]
    if mode not in {"pre-market", "morning", "post-market"}:
        raise ValueError("mode must be pre-market, morning, or post-market")
    if mode in {"morning", "post-market"} and business_date != analysis_date:
        raise ValueError(f"{mode} business_date must match the KST analysis date")
    if mode == "pre-market" and (
        (source == "official-pre-market-builder" and business_date != analysis_date)
        or (source != "official-pre-market-builder" and business_date >= analysis_date)
    ):
        raise ValueError("pre-market business_date is inconsistent with its source")
    expected_official_modes = {
        "official-pre-market-builder": "pre-market",
        "official-post-market-builder": "post-market",
        "official-morning-builder": "morning",
    }
    if source in expected_official_modes and mode != expected_official_modes[source]:
        raise ValueError("official candidate source and mode are inconsistent")
    if mode == "pre-market" and source != "official-pre-market-builder":
        raise ValueError("pre-market mode requires official pre-market candidate input")
    if mode == "morning" and source != "official-morning-builder":
        raise ValueError("morning mode requires official morning candidate input")
    candidates = []
    for item in data["candidates"]:
        if (
            not isinstance(item, dict)
            or not {"ticker", "name", "signals"} <= set(item)
            or not set(item)
            <= {
                "ticker",
                "name",
                "signals",
                "risk_penalty",
                "hard_exclusion",
            }
            or not isinstance(item.get("signals"), list)
        ):
            raise ValueError("invalid candidate record schema")
        signals = []
        for signal in item["signals"]:
            if (
                not isinstance(signal, dict)
                or not {"group", "score", "reason", "source_url"} <= set(signal)
                or (
                    source == "official-pre-market-builder"
                    and not {"observed_at", "evidence_id"} <= set(signal)
                )
                or not set(signal)
                <= {
                    "group",
                    "score",
                    "reason",
                    "source_url",
                    "observed_at",
                    "evidence_id",
                }
            ):
                raise ValueError("invalid candidate signal schema")
            signals.append(
                Signal(
                    SignalGroup(signal["group"]),
                    _number(signal["score"], "signal.score"),
                    signal["reason"],
                    signal["source_url"],
                    datetime.fromisoformat(signal.get("observed_at", data["as_of"])),
                    evidence_id=signal.get("evidence_id"),
                )
            )
        candidates.append(
            Candidate(
                ticker=item["ticker"],
                name=item["name"],
                signals=signals,
                risk_penalty=_number(item.get("risk_penalty", 0), "risk_penalty"),
                hard_exclusion=item.get("hard_exclusion"),
            )
        )
    return observed, business_date, mode, source, candidates


def _load_calendar_bundle(paths: list[Path], as_of: datetime) -> KrxCalendarBundle:
    calendars = tuple(
        sorted(
            (
                KrxMarketCalendar.from_payload(
                    json.loads(path.read_text(encoding="utf-8"))
                )
                for path in paths
            ),
            key=lambda item: item.year,
        )
    )
    return KrxCalendarBundle(calendars, as_of)


def _load_krx_snapshot(path: Path) -> KrxDailySnapshot:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("KRX snapshot must be a JSON object")
    return KrxDailySnapshot.from_payload(payload)


def _load_kis_snapshot(path: Path) -> KisDailySnapshot:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("KIS snapshot must be a JSON object")
    return KisDailySnapshot.from_payload(payload)


def _load_krx_live_snapshot(path: Path) -> KrxLiveActivitySnapshot:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return KrxLiveActivitySnapshot.from_payload(payload)


def _verify_official_candidate_input(
    candidate_path: Path,
    *,
    watchlist_path: Path,
    nxt_snapshot_path: Path,
    krx_snapshot: KrxDailySnapshot | KisDailySnapshot,
    calendar_bundle: KrxCalendarBundle,
    observed: datetime,
    krx_live_path: Path | None = None,
    previous_business_date: date | None = None,
) -> None:
    actual = json.loads(candidate_path.read_text(encoding="utf-8"))
    watchlist = json.loads(watchlist_path.read_text(encoding="utf-8"))
    nxt_snapshot = json.loads(nxt_snapshot_path.read_text(encoding="utf-8"))
    if (
        isinstance(actual, dict)
        and actual.get("source") == "official-pre-market-builder"
    ):
        if krx_live_path is not None or previous_business_date is None:
            raise ValueError(
                "official pre-market input requires previous date without KRX live"
            )
        expected = build_pre_market_input(
            watchlist,
            krx_snapshot,
            nxt_snapshot,
            calendar_bundle,
            previous_business_date,
            observed,
        )
    elif (
        isinstance(actual, dict) and actual.get("source") == "official-morning-builder"
    ):
        if krx_live_path is None or previous_business_date is None:
            raise ValueError(
                "official morning input requires KRX live snapshot and previous date"
            )
        expected = build_morning_input(
            watchlist,
            krx_snapshot,
            nxt_snapshot,
            _load_krx_live_snapshot(krx_live_path),
            calendar_bundle,
            previous_business_date,
            observed,
        )
    elif krx_live_path is not None or previous_business_date is not None:
        raise ValueError(
            "KRX live snapshot and previous date are only valid for morning"
        )
    else:
        expected = build_post_market_input(
            watchlist, krx_snapshot, nxt_snapshot, observed
        )
    if actual != expected:
        raise ValueError("official candidate input does not match source snapshots")


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
    krx_snapshot: KrxDailySnapshot | KisDailySnapshot,
    listing_risks: dict[str, ListingRisk],
    mode: str = "post-market",
    previous_business_date: date | None = None,
) -> dict[str, OperationalEvidence]:
    price_snapshot = krx_snapshot
    if mode in {"pre-market", "morning"}:
        if (
            previous_business_date is None
            or price_snapshot.business_date != previous_business_date
            or previous_business_date >= business_date
        ):
            raise ValueError("previous price snapshot must match exact previous date")
        maximum_age = None
    else:
        if price_snapshot.business_date != business_date:
            raise ValueError("price snapshot date must match candidate business_date")
        maximum_age = timedelta(hours=12)
    if price_snapshot.fetched_at > observed or (
        maximum_age is not None and observed - price_snapshot.fetched_at > maximum_age
    ):
        if isinstance(price_snapshot, KrxDailySnapshot):
            raise ValueError("KRX snapshot must be fetched within 12 hours before analysis")
        raise ValueError("price snapshot timestamp is invalid")
    if isinstance(price_snapshot, KrxDailySnapshot):
        market_day = TradingDayGate().assess(
            price_snapshot.business_date, price_snapshot
        )
        if market_day.status is not MarketDayStatus.OPEN:
            raise ValueError(f"KRX 거래일 확인 실패: {market_day.reason}")
    prices: dict[str, EvidenceRecord] = {}
    for record in price_snapshot.records:
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
    run.add_argument("--kis-snapshot", type=Path)
    run.add_argument("--krx-snapshot", type=Path)
    run.add_argument("--calendar", required=True, action="append", type=Path)
    run.add_argument("--watchlist", type=Path)
    run.add_argument("--nxt-snapshot", type=Path)
    run.add_argument("--krx-live-snapshot", type=Path)
    run.add_argument("--previous-business-date", type=date.fromisoformat)
    run.add_argument("--kind-status", required=True, type=Path)
    run.add_argument("--output", required=True, type=Path)
    daily = commands.add_parser(
        "build-daily-input",
        help="공식 KRX·NXT snapshot에서 post-market 후보 입력을 생성합니다",
    )
    daily.add_argument("--watchlist", required=True, type=Path)
    daily.add_argument("--kis-snapshot", type=Path)
    daily.add_argument("--krx-snapshot", type=Path)
    daily.add_argument("--nxt-snapshot", required=True, type=Path)
    daily.add_argument("--as-of", required=True, type=datetime.fromisoformat)
    daily.add_argument("--output", required=True, type=Path)
    pre_market = commands.add_parser(
        "build-pre-market-input",
        help="공식 직전 거래일 KRX·NXT 근거에서 07:30 장전 후보 입력을 생성합니다",
    )
    pre_market.add_argument("--watchlist", required=True, type=Path)
    pre_market.add_argument("--kis-snapshot", type=Path)
    pre_market.add_argument("--krx-snapshot", type=Path)
    pre_market.add_argument("--nxt-snapshot", required=True, type=Path)
    pre_market.add_argument("--calendar", required=True, action="append", type=Path)
    pre_market.add_argument(
        "--previous-business-date", required=True, type=date.fromisoformat
    )
    pre_market.add_argument("--as-of", required=True, type=datetime.fromisoformat)
    pre_market.add_argument("--output", required=True, type=Path)
    morning = commands.add_parser(
        "build-morning-input",
        help="공식 전일 KRX·당일 NXT·KRX live 근거에서 오전 후보 입력을 생성합니다",
    )
    morning.add_argument("--watchlist", required=True, type=Path)
    morning.add_argument("--krx-snapshot", required=True, type=Path)
    morning.add_argument("--nxt-snapshot", required=True, type=Path)
    morning.add_argument("--krx-live-snapshot", required=True, type=Path)
    morning.add_argument("--calendar", required=True, action="append", type=Path)
    morning.add_argument(
        "--previous-business-date", required=True, type=date.fromisoformat
    )
    morning.add_argument("--as-of", required=True, type=datetime.fromisoformat)
    morning.add_argument("--output", required=True, type=Path)
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
    kis = commands.add_parser(
        "collect-kis", help="KIS 국내주식 일별 시세를 watchlist 스냅샷으로 수집합니다"
    )
    kis.add_argument("--watchlist", required=True, type=Path)
    kis.add_argument("--date", required=True, type=date.fromisoformat)
    kis.add_argument("--output", required=True, type=Path)
    krx_live = commands.add_parser(
        "collect-krx-live",
        help="KRX 공식 메인 화면에서 당일 KOSPI·KOSDAQ 실거래 activity를 수집합니다",
    )
    krx_live.add_argument("--date", required=True, type=date.fromisoformat)
    krx_live.add_argument("--output", required=True, type=Path)
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
    notice_detail = commands.add_parser(
        "collect-kind-market-notice",
        help="KIND 공식 시장운영 공지 상세본문을 검증된 JSON artifact로 수집합니다",
    )
    notice_detail.add_argument("--acceptance-number", required=True)
    notice_detail.add_argument("--output", required=True, type=Path)
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
    if args.command == "build-pre-market-input":
        try:
            watchlist = json.loads(args.watchlist.read_text(encoding="utf-8"))
            if (args.kis_snapshot is None) == (args.krx_snapshot is None):
                raise ValueError("exactly one KIS or KRX snapshot is required")
            price_snapshot = (
                _load_kis_snapshot(args.kis_snapshot)
                if args.kis_snapshot is not None
                else _load_krx_snapshot(args.krx_snapshot)
            )
            nxt_snapshot = json.loads(args.nxt_snapshot.read_text(encoding="utf-8"))
            calendar_bundle = _load_calendar_bundle(args.calendar, args.as_of)
            payload = build_pre_market_input(
                watchlist,
                price_snapshot,
                nxt_snapshot,
                calendar_bundle,
                args.previous_business_date,
                args.as_of,
            )
            _write_json_atomic(args.output, payload)
        except (
            OSError,
            ValueError,
            KeyError,
            TypeError,
            json.JSONDecodeError,
        ) as error:
            print(f"장전 후보 입력 생성 오류: {error}", file=sys.stderr)
            return 2
        print(f"candidates={len(payload['candidates'])} output={args.output}")
        return 0
    if args.command == "build-morning-input":
        try:
            watchlist = json.loads(args.watchlist.read_text(encoding="utf-8"))
            krx_snapshot = _load_krx_snapshot(args.krx_snapshot)
            nxt_snapshot = json.loads(args.nxt_snapshot.read_text(encoding="utf-8"))
            krx_live_snapshot = _load_krx_live_snapshot(args.krx_live_snapshot)
            calendar_bundle = _load_calendar_bundle(args.calendar, args.as_of)
            payload = build_morning_input(
                watchlist,
                krx_snapshot,
                nxt_snapshot,
                krx_live_snapshot,
                calendar_bundle,
                args.previous_business_date,
                args.as_of,
            )
            _write_json_atomic(args.output, payload)
        except (
            OSError,
            ValueError,
            KeyError,
            TypeError,
            json.JSONDecodeError,
        ) as error:
            print(f"오전 후보 입력 생성 오류: {error}", file=sys.stderr)
            return 2
        print(f"candidates={len(payload['candidates'])} output={args.output}")
        return 0
    if args.command == "build-daily-input":
        try:
            watchlist = json.loads(args.watchlist.read_text(encoding="utf-8"))
            if (args.kis_snapshot is None) == (args.krx_snapshot is None):
                raise ValueError("exactly one KIS or KRX snapshot is required")
            price_snapshot = (
                _load_kis_snapshot(args.kis_snapshot)
                if args.kis_snapshot is not None
                else _load_krx_snapshot(args.krx_snapshot)
            )
            nxt_snapshot = json.loads(args.nxt_snapshot.read_text(encoding="utf-8"))
            payload = build_post_market_input(
                watchlist, price_snapshot, nxt_snapshot, args.as_of
            )
            _write_json_atomic(args.output, payload)
        except (
            OSError,
            ValueError,
            KeyError,
            TypeError,
            json.JSONDecodeError,
        ) as error:
            print(f"일일 후보 입력 생성 오류: {error}", file=sys.stderr)
            return 2
        print(f"candidates={len(payload['candidates'])} output={args.output}")
        return 0
    if args.command == "run":
        try:
            observed, business_date, mode, source, candidates = _load_candidates(
                args.input
            )
            if (args.kis_snapshot is None) == (args.krx_snapshot is None):
                raise ValueError("exactly one KIS or KRX snapshot is required")
            price_snapshot = (
                _load_kis_snapshot(args.kis_snapshot)
                if args.kis_snapshot is not None
                else _load_krx_snapshot(args.krx_snapshot)
            )
            calendar_bundle = _load_calendar_bundle(args.calendar, observed)
            has_watchlist = args.watchlist is not None
            has_nxt_snapshot = args.nxt_snapshot is not None
            has_krx_live = args.krx_live_snapshot is not None
            has_previous_date = args.previous_business_date is not None
            if has_watchlist != has_nxt_snapshot:
                raise ValueError("watchlist and NXT snapshot must be provided together")
            if has_watchlist:
                if source not in {
                    "official-pre-market-builder",
                    "official-post-market-builder",
                    "official-morning-builder",
                }:
                    raise ValueError(
                        "source snapshots require official candidate input"
                    )
                is_morning_source = source == "official-morning-builder"
                requires_previous = source in {
                    "official-pre-market-builder",
                    "official-morning-builder",
                }
                if has_krx_live != is_morning_source or (
                    has_previous_date != requires_previous
                ):
                    raise ValueError(
                        "official input artifact combination does not match its mode"
                    )
                _verify_official_candidate_input(
                    args.input,
                    watchlist_path=args.watchlist,
                    nxt_snapshot_path=args.nxt_snapshot,
                    krx_snapshot=price_snapshot,
                    calendar_bundle=calendar_bundle,
                    observed=observed,
                    krx_live_path=args.krx_live_snapshot,
                    previous_business_date=args.previous_business_date,
                )
            elif source in {
                "official-pre-market-builder",
                "official-post-market-builder",
                "official-morning-builder",
            }:
                raise ValueError(
                    "official candidate input requires watchlist and NXT snapshot"
                )
            elif has_krx_live or has_previous_date:
                raise ValueError(
                    "KRX live snapshot and previous date require official morning input"
                )
            listing_risks = _load_listing_risks(
                args.kind_status,
                analysis_time=observed,
                candidate_tickers={candidate.ticker for candidate in candidates},
            )
            operational_evidence = _operational_evidence(
                candidates,
                observed=observed,
                business_date=business_date,
                krx_snapshot=price_snapshot,
                listing_risks=listing_risks,
                mode=mode,
                previous_business_date=args.previous_business_date,
            )
            pre_market_nxt_evidence = None
            morning_live_snapshot = None
            morning_nxt_evidence = None
            if mode in {"pre-market", "morning"}:
                if args.nxt_snapshot is None:
                    raise ValueError(f"{mode} official NXT artifact is required")
                if mode == "morning":
                    if args.krx_live_snapshot is None:
                        raise ValueError(
                            "morning official KRX live artifact is required"
                        )
                    morning_live_snapshot = _load_krx_live_snapshot(
                        args.krx_live_snapshot
                    )
                nxt_payload = json.loads(args.nxt_snapshot.read_text(encoding="utf-8"))
                cross_tickers = {
                    candidate.ticker
                    for candidate in candidates
                    if any(
                        signal.group is SignalGroup.CROSS_MARKET
                        for signal in candidate.signals
                    )
                }
                matching_records = [
                    EvidenceRecord.from_dict(item)
                    for item in nxt_payload["records"]
                    if item.get("ticker") in cross_tickers
                ]
                nxt_evidence = {
                    record.ticker: record
                    for record in matching_records
                    if record.ticker is not None
                }
                if len(nxt_evidence) != len(matching_records):
                    raise ValueError(f"duplicate {mode} NXT ticker evidence")
                if mode == "pre-market":
                    pre_market_nxt_evidence = nxt_evidence
                else:
                    morning_nxt_evidence = nxt_evidence
            result = ResearchHarness(calendar_bundle=calendar_bundle).run(
                candidates,
                observed,
                mode,
                args.output,
                operational_evidence=operational_evidence,
                previous_business_date=args.previous_business_date,
                pre_market_nxt_evidence=pre_market_nxt_evidence,
                morning_krx_live_snapshot=morning_live_snapshot,
                morning_nxt_evidence=morning_nxt_evidence,
            )
        except (OSError, ValueError, KeyError, TypeError) as error:
            print(f"입력 오류: {error}", file=sys.stderr)
            return 2
        print(f"generated={len(result.reports)} index={result.index_path}")
        return 0
    if args.command == "collect-krx-live":
        try:
            snapshot = KrxLiveClient().current_activity(args.date)
            _write_json_atomic(args.output, snapshot.to_payload())
        except (OSError, ValueError, KeyError, TypeError) as error:
            print(f"KRX 당일 실거래 activity 수집 오류: {error}", file=sys.stderr)
            return 2
        print(f"collected={len(snapshot.activities)} output={args.output}")
        return 0
    if args.command == "collect-kind-market-notice":
        try:
            notice = KindMarketNoticeClient().document(args.acceptance_number)
            _write_json_atomic(args.output, notice.to_payload())
        except (OSError, ValueError, KeyError, TypeError) as error:
            print(f"KIND 시장운영 공지 상세 수집 오류: {error}", file=sys.stderr)
            return 2
        print(f"collected=1 output={args.output}")
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
    if args.command == "collect-kis":
        app_key = os.environ.get("KIS_APP_KEY")
        app_secret = os.environ.get("KIS_APP_SECRET")
        if not app_key or not app_secret:
            print(
                "환경변수 KIS_APP_KEY와 KIS_APP_SECRET이 필요합니다",
                file=sys.stderr,
            )
            return 2
        try:
            watchlist = json.loads(args.watchlist.read_text(encoding="utf-8"))
            if not isinstance(watchlist, dict) or not isinstance(
                watchlist.get("stocks"), list
            ):
                raise ValueError("invalid user watchlist envelope")
            stocks = {
                str(item["ticker"]): str(item["name"])
                for item in watchlist["stocks"]
                if isinstance(item, dict) and set(item) == {"ticker", "name"}
            }
            if len(stocks) != len(watchlist["stocks"]):
                raise ValueError("invalid user watchlist stocks")
            snapshot = KisClient(app_key=app_key, app_secret=app_secret).daily_snapshot(
                args.date, stocks
            )
            _write_json_atomic(args.output, snapshot.to_payload())
        except (
            OSError,
            ValueError,
            KeyError,
            TypeError,
            json.JSONDecodeError,
        ) as error:
            print(f"KIS 수집 오류: {error}", file=sys.stderr)
            return 2
        print(f"collected={len(snapshot.records)} output={args.output}")
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
