from __future__ import annotations

import math
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import cast
from zoneinfo import ZoneInfo

import yaml

from .agents import DEFAULT_AGENTS
from .collectors.calendar import KrxCalendarBundle
from .collectors.krx_live import KrxLiveActivitySnapshot
from .evidence import EvidenceRecord, EvidenceSource, VerificationStatus
from .market_rules import (
    OperationalDecision,
    OperationalEvidence,
    OperationalFilter,
    apply_operational_decision,
)
from .models import Candidate, HarnessResult, Signal, SignalGroup, StockReport
from .scanner import BalancedRanker


_KST = ZoneInfo("Asia/Seoul")


def add_trading_days(
    start: datetime, days: int, holidays: frozenset[date] = frozenset()
) -> datetime:
    current = start
    remaining = days
    while remaining:
        current += timedelta(days=1)
        if current.weekday() < 5 and current.date() not in holidays:
            remaining -= 1
    return current


def _validate_morning_krx_signal(
    candidate: Candidate,
    signal: Signal,
    price: EvidenceRecord,
    previous_business_date: date,
) -> None:
    expected_ids = {
        (
            f"krx:daily:KOSPI:{previous_business_date:%Y%m%d}:{candidate.ticker}",
            "https://data-dbg.krx.co.kr/svc/apis/sto/stk_bydd_trd",
        ),
        (
            f"krx:daily:KOSDAQ:{previous_business_date:%Y%m%d}:{candidate.ticker}",
            "https://data-dbg.krx.co.kr/svc/apis/sto/ksq_bydd_trd",
        ),
    }
    change_rate = price.metrics.get("change_rate")
    volume = price.metrics.get("volume")
    trading_value = price.metrics.get("trading_value")
    metrics_invalid = (
        isinstance(change_rate, bool)
        or not isinstance(change_rate, (int, float))
        or not math.isfinite(float(change_rate))
        or isinstance(volume, bool)
        or not isinstance(volume, int)
        or volume < 0
        or isinstance(trading_value, bool)
        or not isinstance(trading_value, int)
        or trading_value < 0
    )
    if metrics_invalid:
        raise ValueError("invalid official morning KRX signal provenance")
    rate = float(cast(int | float, change_rate))
    volume = cast(int, volume)
    trading_value = cast(int, trading_value)
    expected_reason = (
        f"전 거래일 KRX 등락률 {rate:+.2f}%, 거래량 {volume:,}주, "
        f"거래대금 {trading_value:,}원"
    )
    if (
        (price.evidence_id, price.source_url) not in expected_ids
        or price.source is not EvidenceSource.KRX
        or price.canonical_event_id != price.evidence_id
        or price.is_correction
        or price.is_withdrawn
        or price.verification is not VerificationStatus.OFFICIAL
        or price.kind != "daily-price"
        or price.ticker != candidate.ticker
        or price.company_name != candidate.name
        or price.published_date != previous_business_date
        or price.delay_minutes is not None
        or signal.group is not SignalGroup.PRICE_VOLUME
        or signal.evidence_id != price.evidence_id
        or signal.source_url != price.source_url
        or signal.observed_at != price.fetched_at
        or signal.score != min(100.0, abs(rate) * 10.0)
        or signal.reason != expected_reason
    ):
        raise ValueError("invalid official morning KRX signal provenance")


def _markdown_text(value: str) -> str:
    escaped = value.replace("\\", "\\\\")
    for character in "[]*_<>#|`":
        escaped = escaped.replace(character, f"\\{character}")
    return escaped


def _document(metadata: dict[str, object], body: str) -> str:
    frontmatter = yaml.safe_dump(
        metadata, allow_unicode=True, sort_keys=False, default_flow_style=False
    ).strip()
    return f"---\n{frontmatter}\n---\n{body.rstrip()}\n"


def _support_page(
    title: str, observed_at: datetime, body: str, page_type: str = "summary"
) -> str:
    return _document(
        {
            "title": title,
            "created": observed_at.date().isoformat(),
            "updated": observed_at.date().isoformat(),
            "type": page_type,
            "tags": ["market", "short-term"],
            "sources": ["https://data.krx.co.kr/", "https://www.nextrade.co.kr/"],
            "as_of": observed_at.isoformat(),
            "confidence": "medium",
        },
        body,
    )


def _ensure_support_pages(output_dir: Path, observed_at: datetime) -> None:
    pages = {
        "Home.md": "# kr-stock-wiki\n\n- [[후보종목|Candidates]]\n- [[분석 방법론|Methodology]]",
        "Methodology.md": (
            "# 분석 방법론\n\n일반 후보는 서로 다른 신호 그룹이 최소 2개 필요하며 "
            "1~5거래일 안에 재검토합니다.\n\n- [[Home]]\n- [[Candidates]]"
        ),
    }
    for filename, body in pages.items():
        path = output_dir / filename
        if not path.exists():
            path.write_text(
                _support_page(Path(filename).stem, observed_at, body), encoding="utf-8"
            )


def _report_markdown(report: StockReport, mode: str) -> str:
    safe_name = _markdown_text(report.name)
    findings = "\n".join(
        f"### {_markdown_text(role)}\n\n{_markdown_text(text)}\n"
        for role, text in report.agent_findings.items()
    )
    sources = "\n".join(f"- <{source}>" for source in report.sources)
    dissent = "\n".join(f"- {_markdown_text(item)}" for item in report.dissent)
    body = f"""# {safe_name} ({report.ticker})

- **상태:** {report.status}
- **복합 신호 점수:** {report.score:g}
- **유효기간:** {report.valid_until.date().isoformat()}까지 (최대 5거래일)
- **연결:** [[Home]] · [[Methodology]]

## 사실과 해석

아래 의견은 자동 생성된 조사 결과이며 사실, 해석, 반대 의견을 분리해 검토해야 합니다.

{findings}
## 반대 의견과 무효화 조건

{dissent}

## 출처

{sources}

> 투자 권유가 아니며 원문과 최신 시장 데이터를 직접 확인해야 합니다.
"""
    return _document(
        {
            "title": f"{report.name} 초단기 리포트",
            "created": report.observed_at.date().isoformat(),
            "updated": report.observed_at.date().isoformat(),
            "type": "stock-report",
            "tags": ["stock", "short-term", mode],
            "sources": report.sources,
            "as_of": report.observed_at.isoformat(),
            "confidence": "medium",
        },
        body,
    )


class ResearchHarness:
    def __init__(
        self,
        *,
        calendar_bundle: KrxCalendarBundle,
        ranker: BalancedRanker | None = None,
    ):
        self.ranker = ranker or BalancedRanker(minimum_score=20)
        self.calendar_bundle = calendar_bundle

    def run(
        self,
        candidates: list[Candidate],
        observed_at: datetime,
        mode: str,
        output_dir: Path,
        *,
        operational_evidence: dict[str, OperationalEvidence],
        previous_business_date: date | None = None,
        pre_market_nxt_evidence: dict[str, EvidenceRecord] | None = None,
        morning_krx_live_snapshot: KrxLiveActivitySnapshot | None = None,
        morning_nxt_evidence: dict[str, EvidenceRecord] | None = None,
    ) -> HarnessResult:
        if mode not in {"pre-market", "morning", "post-market"}:
            raise ValueError("mode must be pre-market, morning, or post-market")
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ValueError("observed_at must include a timezone")
        analysis_date = observed_at.astimezone(_KST).date()
        if self.calendar_bundle.as_of != observed_at:
            raise ValueError("calendar bundle must match analysis time")
        if not self.calendar_bundle.is_scheduled_trading_day(analysis_date):
            raise ValueError("analysis date is a scheduled KRX closure")
        tickers = [candidate.ticker for candidate in candidates]
        candidate_names = {candidate.ticker: candidate.name for candidate in candidates}
        candidates_by_ticker = {candidate.ticker: candidate for candidate in candidates}
        price_by_ticker: dict[str, Signal] = {}
        if mode == "pre-market":
            analysis_time = observed_at.astimezone(_KST).timetz().replace(tzinfo=None)
            if not time(7, 0) <= analysis_time < time(8, 0):
                raise ValueError("pre-market analysis requires 07:00-08:00 KST")
            if (
                previous_business_date is None
                or self.calendar_bundle.previous_business_date(analysis_date)
                != previous_business_date
            ):
                raise ValueError("pre-market requires exact previous business date")
            if pre_market_nxt_evidence is None:
                raise ValueError("pre-market requires official previous NXT evidence")
            if (
                morning_krx_live_snapshot is not None
                or morning_nxt_evidence is not None
            ):
                raise ValueError("morning evidence is invalid for pre-market")
            cross_by_ticker: dict[str, Signal] = {}
            for candidate in candidates:
                price_signals = [
                    signal
                    for signal in candidate.signals
                    if signal.group is SignalGroup.PRICE_VOLUME
                ]
                cross_signals = [
                    signal
                    for signal in candidate.signals
                    if signal.group is SignalGroup.CROSS_MARKET
                ]
                if len(price_signals) != 1:
                    raise ValueError(
                        "pre-market candidates require one KRX price signal"
                    )
                if len(cross_signals) > 1:
                    raise ValueError(
                        "pre-market candidates allow at most one NXT signal"
                    )
                price_by_ticker[candidate.ticker] = price_signals[0]
                if cross_signals:
                    cross_by_ticker[candidate.ticker] = cross_signals[0]
            if set(pre_market_nxt_evidence) != set(cross_by_ticker):
                raise ValueError(
                    "pre-market NXT evidence must match cross-market candidates"
                )
            for ticker, record in pre_market_nxt_evidence.items():
                signal = cross_by_ticker[ticker]
                source_value = record.metrics.get("source_as_of")
                if not isinstance(source_value, str):
                    raise ValueError("pre-market NXT source timestamp is invalid")
                try:
                    source_as_of = datetime.fromisoformat(source_value)
                except ValueError:
                    raise ValueError(
                        "pre-market NXT source timestamp is invalid"
                    ) from None
                change_rate = record.metrics.get("change_rate")
                volume = record.metrics.get("volume")
                trading_value = record.metrics.get("trading_value")
                metrics_invalid = (
                    isinstance(change_rate, bool)
                    or not isinstance(change_rate, (int, float))
                    or not math.isfinite(float(change_rate))
                    or isinstance(volume, bool)
                    or not isinstance(volume, int)
                    or volume < 0
                    or isinstance(trading_value, bool)
                    or not isinstance(trading_value, int)
                    or trading_value < 0
                )
                if metrics_invalid:
                    raise ValueError("invalid official pre-market NXT evidence")
                rate = float(cast(int | float, change_rate))
                volume = cast(int, volume)
                trading_value = cast(int, trading_value)
                expected_reason = (
                    f"전 거래일 NXT 20분 지연 등락률 {rate:+.2f}%, "
                    f"거래량 {volume:,}주, 거래대금 {trading_value:,}원"
                )
                expected_id = (
                    f"nxt:price-snapshot:{previous_business_date:%Y%m%d}:{ticker}"
                )
                if (
                    record.source is not EvidenceSource.NXT
                    or record.evidence_id != expected_id
                    or record.canonical_event_id != expected_id
                    or record.is_correction
                    or record.is_withdrawn
                    or record.verification is not VerificationStatus.OFFICIAL
                    or record.kind != "price-snapshot"
                    or record.ticker != ticker
                    or record.published_date != previous_business_date
                    or record.delay_minutes != 20
                    or record.company_name != candidate_names[ticker]
                    or record.source_url
                    != "https://www.nextrade.co.kr/menu/transactionStatusMain/menuList.do"
                    or record.fetched_at > observed_at
                    or source_as_of.tzinfo is None
                    or source_as_of.utcoffset() is None
                    or source_as_of.astimezone(_KST).date() != previous_business_date
                    or source_as_of.astimezone(_KST).time() < time(20, 0)
                    or source_as_of > record.fetched_at
                    or signal.evidence_id != record.evidence_id
                    or signal.source_url != record.source_url
                    or signal.observed_at != record.fetched_at
                    or signal.score != min(100.0, abs(rate) * 10.0)
                    or signal.reason != expected_reason
                ):
                    raise ValueError("invalid official pre-market NXT evidence")
        elif mode == "morning":
            analysis_time = observed_at.astimezone(_KST).timetz().replace(tzinfo=None)
            if not time(9, 20) <= analysis_time < time(12, 0):
                raise ValueError("morning analysis requires 09:20-12:00 KST")
            if (
                previous_business_date is None
                or self.calendar_bundle.previous_business_date(analysis_date)
                != previous_business_date
            ):
                raise ValueError("morning requires exact previous business date")
            if pre_market_nxt_evidence is not None:
                raise ValueError("pre-market evidence is invalid for morning")
            if morning_krx_live_snapshot is None or morning_nxt_evidence is None:
                raise ValueError("morning requires official KRX live and NXT evidence")
            live = morning_krx_live_snapshot
            if (
                live.business_date != analysis_date
                or live.fetched_at > observed_at
                or observed_at - live.source_as_of > timedelta(minutes=10)
            ):
                raise ValueError("morning KRX live evidence does not match analysis")
            cross_by_ticker = {}
            for candidate in candidates:
                cross_signals = [
                    signal
                    for signal in candidate.signals
                    if signal.group is SignalGroup.CROSS_MARKET
                ]
                price_signals = [
                    signal
                    for signal in candidate.signals
                    if signal.group is SignalGroup.PRICE_VOLUME
                ]
                if len(price_signals) != 1:
                    raise ValueError("morning candidates require one KRX price signal")
                price_by_ticker[candidate.ticker] = price_signals[0]
                groups = {signal.group for signal in candidate.signals}
                if len(cross_signals) > 1 or (
                    len(groups) >= 2 and len(cross_signals) != 1
                ):
                    raise ValueError("morning ranked candidates require one NXT signal")
                if cross_signals:
                    cross_by_ticker[candidate.ticker] = cross_signals[0]
            if set(morning_nxt_evidence) != set(cross_by_ticker):
                raise ValueError(
                    "morning NXT evidence must match cross-market candidates"
                )
            for ticker, record in morning_nxt_evidence.items():
                signal = cross_by_ticker[ticker]
                source_value = record.metrics.get("source_as_of")
                if not isinstance(source_value, str):
                    raise ValueError("morning NXT source timestamp is invalid")
                try:
                    source_as_of = datetime.fromisoformat(source_value)
                except (TypeError, ValueError):
                    raise ValueError(
                        "morning NXT source timestamp is invalid"
                    ) from None
                change_rate = record.metrics.get("change_rate")
                volume = record.metrics.get("volume")
                trading_value = record.metrics.get("trading_value")
                metrics_invalid = (
                    isinstance(change_rate, bool)
                    or not isinstance(change_rate, (int, float))
                    or not math.isfinite(float(change_rate))
                    or isinstance(volume, bool)
                    or not isinstance(volume, int)
                    or volume <= 0
                    or isinstance(trading_value, bool)
                    or not isinstance(trading_value, int)
                    or trading_value <= 0
                )
                if metrics_invalid:
                    raise ValueError("invalid official morning NXT evidence")
                rate = float(cast(int | float, change_rate))
                volume = cast(int, volume)
                trading_value = cast(int, trading_value)
                expected_reason = (
                    f"당일 NXT 20분 지연 등락률 {rate:+.2f}%, "
                    f"거래량 {volume:,}주, 거래대금 {trading_value:,}원"
                )
                expected_id = f"nxt:price-snapshot:{analysis_date:%Y%m%d}:{ticker}"
                if (
                    record.source is not EvidenceSource.NXT
                    or record.evidence_id != expected_id
                    or record.canonical_event_id != expected_id
                    or record.is_correction
                    or record.is_withdrawn
                    or record.verification is not VerificationStatus.OFFICIAL
                    or record.kind != "price-snapshot"
                    or record.ticker != ticker
                    or record.published_date != analysis_date
                    or record.delay_minutes != 20
                    or record.company_name != candidate_names[ticker]
                    or record.source_url
                    != "https://www.nextrade.co.kr/menu/transactionStatusMain/menuList.do"
                    or record.fetched_at > observed_at
                    or observed_at - record.fetched_at > timedelta(hours=2)
                    or source_as_of.tzinfo is None
                    or source_as_of.utcoffset() is None
                    or source_as_of.astimezone(_KST).date() != analysis_date
                    or source_as_of.astimezone(_KST).time() < time(9, 0)
                    or source_as_of > record.fetched_at
                    or observed_at - source_as_of > timedelta(hours=2)
                    or signal.evidence_id != record.evidence_id
                    or signal.source_url != record.source_url
                    or signal.observed_at != record.fetched_at
                    or signal.score != min(100.0, abs(rate) * 10.0)
                    or signal.reason != expected_reason
                ):
                    raise ValueError("invalid official morning NXT evidence")
        elif (
            previous_business_date is not None
            or pre_market_nxt_evidence is not None
            or morning_krx_live_snapshot is not None
            or morning_nxt_evidence is not None
        ):
            raise ValueError("mode-specific evidence does not match analysis mode")

        if len(tickers) != len(set(tickers)):
            raise ValueError("중복 종목코드는 허용되지 않습니다")
        if set(operational_evidence) != set(tickers):
            raise ValueError("모든 후보와 정확히 일치하는 운영 근거가 필요합니다")
        policy = OperationalFilter()
        decisions: dict[str, OperationalDecision] = {}
        for ticker, evidence in operational_evidence.items():
            if evidence.ticker != ticker:
                raise ValueError("운영 근거 map의 ticker가 일치하지 않습니다")
            price = evidence.price
            if mode in {"pre-market", "morning"} and price is None:
                raise ValueError(f"{mode} requires official KRX price evidence")
            if price is not None:
                if mode in {"pre-market", "morning"}:
                    price_invalid = (
                        price.published_date != previous_business_date
                        or price.fetched_at > observed_at
                    )
                else:
                    price_invalid = (
                        price.published_date != analysis_date
                        or price.fetched_at > observed_at
                        or observed_at - price.fetched_at > timedelta(hours=12)
                    )
                if price_invalid:
                    raise ValueError(
                        "KRX 가격 근거의 기준일 또는 수집시각이 분석 mode와 일치하지 않습니다"
                    )
                if mode in {"pre-market", "morning"}:
                    _validate_morning_krx_signal(
                        candidates_by_ticker[ticker],
                        price_by_ticker[ticker],
                        price,
                        cast(date, previous_business_date),
                    )
            risk_record = evidence.listing_risk.evidence
            if risk_record is not None and (
                risk_record.published_date != analysis_date
                or risk_record.fetched_at > observed_at
                or observed_at - risk_record.fetched_at > timedelta(hours=1)
            ):
                raise ValueError(
                    "KIND 상태 근거는 분석일 당일 1시간 이내에 수집돼야 합니다"
                )
            if price is None:
                decisions[ticker] = OperationalDecision(
                    ticker, False, ("공식 KRX 일별 시세 없음",)
                )
            else:
                decisions[ticker] = policy.evaluate(
                    price,
                    evidence.listing_risk,
                    analysis_date=analysis_date,
                )
        filtered_candidates = [
            apply_operational_decision(candidate, decisions[candidate.ticker])
            for candidate in candidates
        ]
        valid_until = self.calendar_bundle.add_trading_days(observed_at, 5)
        output_dir.mkdir(parents=True, exist_ok=True)
        if output_dir.is_symlink() or any(
            path.is_symlink() for path in output_dir.rglob("*")
        ):
            raise ValueError("output tree cannot contain a symlink")
        output_root = output_dir.resolve()
        _ensure_support_pages(output_dir, observed_at)
        reports_dir = output_dir / "stocks"
        reports_dir.mkdir(exist_ok=True)
        if reports_dir.is_symlink() or output_root not in reports_dir.resolve().parents:
            raise ValueError("stocks path escaped output directory or is a symlink")
        reports: list[StockReport] = []
        paths: list[Path] = []
        for evaluation in self.ranker.rank(filtered_candidates, as_of=observed_at):
            candidate = evaluation.candidate
            findings = {
                agent.role: agent.analyze(candidate, evaluation)
                for agent in DEFAULT_AGENTS
            }
            report = StockReport(
                ticker=candidate.ticker,
                name=candidate.name,
                status="관심" if evaluation.final_score >= 60 else "관찰",
                observed_at=observed_at,
                valid_until=valid_until,
                score=evaluation.final_score,
                agent_findings=findings,
                dissent=[
                    findings["risk-bear"],
                    "촉매가 가격에 이미 반영되면 판단을 무효화한다.",
                ],
                sources=sorted({signal.source_url for signal in candidate.signals}),
            )
            report.markdown = _report_markdown(report, mode)
            path = reports_dir / (
                f"{candidate.ticker}-{observed_at.date().isoformat()}-{mode}.md"
            )
            if output_root not in path.resolve().parents:
                raise ValueError("report path escaped output directory")
            path.write_text(report.markdown, encoding="utf-8")
            reports.append(report)
            paths.append(path)

        links = (
            "\n".join(
                f"- [[{_markdown_text(report.name)}|stocks/{path.stem}]] — "
                f"{report.status}, {report.score:g}점"
                for report, path in zip(reports, paths)
            )
            or "- 기준을 통과한 종목 없음"
        )
        index_path = output_dir / "Candidates.md"
        index_path.write_text(
            _support_page(
                "후보종목",
                observed_at,
                f"# 후보종목\n\n- [[Home]] · [[Methodology]]\n\n{links}",
            ),
            encoding="utf-8",
        )
        return HarnessResult(reports, index_path, paths)
