from dataclasses import replace
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from kr_stock_wiki.collectors.calendar import (
    KrxCalendarBundle,
    KrxMarketCalendar,
    MarketHoliday,
)
from kr_stock_wiki.collectors.krx import KrxMarket
from kr_stock_wiki.collectors.krx_live import (
    KrxLiveActivitySnapshot,
    KrxLiveMarketActivity,
)
from kr_stock_wiki.evidence import EvidenceRecord, EvidenceSource, VerificationStatus
from kr_stock_wiki.harness import ResearchHarness
from kr_stock_wiki.market_rules import ListingRisk, OperationalEvidence
from kr_stock_wiki.models import Candidate, Signal, SignalGroup


_WEEKDAY_CODES = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")
_WEEKDAY_NAMES = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)


def calendar_bundle(
    as_of: datetime, extra_holidays: tuple[date, ...] = ()
) -> KrxCalendarBundle:
    year = as_of.astimezone(ZoneInfo("Asia/Seoul")).year
    days = {date(year, 1, day) for day in range(1, 10)} | {date(year, 12, 31)}
    days.update(extra_holidays)
    holidays = tuple(
        MarketHoliday(
            day,
            _WEEKDAY_CODES[day.weekday()],
            _WEEKDAY_NAMES[day.weekday()],
            "test",
        )
        for day in sorted(days)
    )
    calendar = KrxMarketCalendar(year, holidays, as_of - timedelta(minutes=1))
    return KrxCalendarBundle((calendar,), as_of)


def operational_evidence(
    *candidates: Candidate, eligible: bool = True
) -> dict[str, OperationalEvidence]:
    result: dict[str, OperationalEvidence] = {}
    for candidate in candidates:
        observed = candidate.signals[0].observed_at
        analysis_date = observed.astimezone(ZoneInfo("Asia/Seoul")).date()
        price_id = f"krx:daily:KOSPI:{analysis_date:%Y%m%d}:{candidate.ticker}"
        price = EvidenceRecord(
            source=EvidenceSource.KRX,
            evidence_id=price_id,
            canonical_event_id=price_id,
            kind="daily-price",
            company_name=candidate.name,
            title="KRX 일별 시세",
            source_url="https://data-dbg.krx.co.kr/svc/apis/sto/stk_bydd_trd",
            published_date=analysis_date,
            fetched_at=observed,
            verification=VerificationStatus.OFFICIAL,
            ticker=candidate.ticker,
            metrics={
                "close": 71_000,
                "volume": 1_000_000,
                "trading_value": 100_000_000_000,
                "market_cap": 500_000_000_000,
            },
            raw={"MKT_NM": "KOSPI"},
        )
        status = None
        if eligible:
            status_id = f"kind:listing-risk:{analysis_date}:{candidate.ticker}"
            status = EvidenceRecord(
                source=EvidenceSource.KIND,
                evidence_id=status_id,
                canonical_event_id=status_id,
                kind="listing-risk-status",
                company_name=candidate.name,
                title="KIND 투자유의 상태",
                source_url="https://kind.krx.co.kr/investwarn/adminissue.do",
                published_date=analysis_date,
                fetched_at=observed,
                verification=VerificationStatus.OFFICIAL,
                ticker=candidate.ticker,
                metrics={
                    "administrative_issue": 0,
                    "trading_halt": 0,
                    "investment_warning": 0,
                },
            )
        result[candidate.ticker] = OperationalEvidence(
            candidate.ticker,
            price,
            ListingRisk(candidate.ticker, analysis_date, status),
        )
    return result


def test_harness_runs_seven_roles_and_preserves_disagreement(tmp_path):
    observed = datetime(2026, 7, 20, 20, 30, tzinfo=ZoneInfo("Asia/Seoul"))
    candidate = Candidate(
        "005930",
        "삼성전자",
        [
            Signal(
                SignalGroup.CATALYST,
                25,
                "공식 공시 촉매",
                "https://dart.fss.or.kr/example",
                observed,
            ),
            Signal(
                SignalGroup.PRICE_VOLUME,
                20,
                "거래대금 증가",
                "https://data.krx.co.kr/example",
                observed,
            ),
        ],
    )

    result = ResearchHarness(calendar_bundle=calendar_bundle(observed)).run(
        [candidate],
        observed,
        "post-market",
        tmp_path,
        operational_evidence=operational_evidence(candidate),
    )

    assert len(result.reports) == 1
    report = result.reports[0]
    assert set(report.agent_findings) == {
        "market-scanner",
        "fundamental",
        "industry",
        "valuation",
        "disclosure-event",
        "risk-bear",
        "editor-in-chief",
    }
    assert report.dissent
    assert "사실과 해석" in report.markdown
    assert "https://dart.fss.or.kr/example" in report.markdown
    assert "원문 조회가 필요" in report.agent_findings["disclosure-event"]
    assert "판정 후보" in report.agent_findings["editor-in-chief"]
    assert result.index_path.exists()
    assert (
        "[[삼성전자|stocks/005930-2026-07-20-post-market]]"
        in result.index_path.read_text(encoding="utf-8")
    )
    assert result.report_paths[0].exists()


def test_harness_rejects_symlink_inside_existing_output_tree(tmp_path):
    import pytest

    observed = datetime(2026, 7, 20, 20, 30, tzinfo=ZoneInfo("Asia/Seoul"))
    outside = tmp_path / "outside"
    outside.mkdir()
    output = tmp_path / "wiki"
    output.mkdir()
    (output / "stocks").symlink_to(outside, target_is_directory=True)
    candidate = Candidate(
        "005930",
        "삼성전자",
        [
            Signal(
                SignalGroup.CATALYST, 25, "공시", "https://dart.fss.or.kr/a", observed
            ),
            Signal(SignalGroup.FLOW, 15, "수급", "https://data.krx.co.kr/a", observed),
        ],
    )

    with pytest.raises(ValueError, match="symlink"):
        ResearchHarness(calendar_bundle=calendar_bundle(observed)).run(
            [candidate],
            observed,
            "post-market",
            output,
            operational_evidence=operational_evidence(candidate),
        )
    assert not list(outside.glob("*.md"))


def test_harness_rejects_duplicate_tickers(tmp_path):
    import pytest

    observed = datetime(2026, 7, 20, 20, 30, tzinfo=ZoneInfo("Asia/Seoul"))
    candidate = Candidate(
        "005930",
        "삼성전자",
        [
            Signal(
                SignalGroup.CATALYST, 25, "공시", "https://dart.fss.or.kr/a", observed
            ),
            Signal(SignalGroup.FLOW, 15, "수급", "https://data.krx.co.kr/a", observed),
        ],
    )

    with pytest.raises(ValueError, match="중복 종목코드"):
        ResearchHarness(calendar_bundle=calendar_bundle(observed)).run(
            [candidate, candidate],
            observed,
            "post-market",
            tmp_path,
            operational_evidence=operational_evidence(candidate),
        )


def test_harness_requires_and_applies_operational_evidence(tmp_path):
    import pytest

    observed = datetime(2026, 7, 20, 20, 30, tzinfo=ZoneInfo("Asia/Seoul"))
    candidate = Candidate(
        "005930",
        "삼성전자",
        [
            Signal(
                SignalGroup.CATALYST, 25, "공시", "https://dart.fss.or.kr/a", observed
            ),
            Signal(
                SignalGroup.PRICE_VOLUME,
                20,
                "가격",
                "https://data.krx.co.kr/a",
                observed,
            ),
        ],
    )
    with pytest.raises(ValueError, match="모든 후보"):
        ResearchHarness(calendar_bundle=calendar_bundle(observed)).run(
            [candidate],
            observed,
            "post-market",
            tmp_path / "missing",
            operational_evidence={},
        )

    result = ResearchHarness(calendar_bundle=calendar_bundle(observed)).run(
        [candidate],
        observed,
        "post-market",
        tmp_path / "excluded",
        operational_evidence=operational_evidence(candidate, eligible=False),
    )
    assert result.reports == []


def test_harness_rejects_pre_market_without_official_same_day_operating_status(
    tmp_path,
):
    import pytest

    observed = datetime(2026, 7, 20, 7, 30, tzinfo=ZoneInfo("Asia/Seoul"))
    candidate = Candidate(
        "005930",
        "삼성전자",
        [
            Signal(
                SignalGroup.CATALYST, 40, "공시", "https://dart.fss.or.kr/a", observed
            )
        ],
    )

    with pytest.raises(ValueError, match="공식 KRX 당일 운영상태"):
        ResearchHarness(calendar_bundle=calendar_bundle(observed)).run(
            [candidate],
            observed,
            "pre-market",
            tmp_path,
            operational_evidence=operational_evidence(candidate),
        )


def test_harness_rejects_future_operational_evidence(tmp_path):
    from dataclasses import replace

    import pytest

    observed = datetime(2026, 7, 20, 20, 30, tzinfo=ZoneInfo("Asia/Seoul"))
    candidate = Candidate(
        "005930",
        "삼성전자",
        [
            Signal(
                SignalGroup.CATALYST, 40, "공시", "https://dart.fss.or.kr/a", observed
            )
        ],
    )
    evidence = operational_evidence(candidate)[candidate.ticker]
    assert evidence.price is not None
    future = observed + timedelta(seconds=1)

    with pytest.raises(ValueError, match="KRX 가격 근거"):
        ResearchHarness(calendar_bundle=calendar_bundle(observed)).run(
            [candidate],
            observed,
            "post-market",
            tmp_path / "future-price",
            operational_evidence={
                candidate.ticker: replace(
                    evidence, price=replace(evidence.price, fetched_at=future)
                )
            },
        )

    assert evidence.listing_risk.evidence is not None
    future_risk = replace(evidence.listing_risk.evidence, fetched_at=future)
    with pytest.raises(ValueError, match="KIND 상태 근거"):
        ResearchHarness(calendar_bundle=calendar_bundle(observed)).run(
            [candidate],
            observed,
            "post-market",
            tmp_path / "future-risk",
            operational_evidence={
                candidate.ticker: replace(
                    evidence,
                    listing_risk=replace(evidence.listing_risk, evidence=future_risk),
                )
            },
        )


def test_harness_skips_exchange_holiday_when_calculating_expiry(tmp_path):
    from datetime import date

    observed = datetime(2026, 7, 16, 7, 30, tzinfo=ZoneInfo("Asia/Seoul"))
    candidate = Candidate(
        "000660",
        "SK하이닉스",
        [
            Signal(
                SignalGroup.CATALYST, 25, "실적", "https://dart.fss.or.kr/a", observed
            ),
            Signal(SignalGroup.FLOW, 15, "수급", "https://data.krx.co.kr/a", observed),
        ],
    )

    report = (
        ResearchHarness(calendar_bundle=calendar_bundle(observed, (date(2026, 7, 17),)))
        .run(
            [candidate],
            observed,
            "post-market",
            tmp_path,
            operational_evidence=operational_evidence(candidate),
        )
        .reports[0]
    )

    assert report.valid_until.date().isoformat() == "2026-07-24"


def test_generated_wiki_is_self_contained_and_lint_clean(tmp_path):
    from kr_stock_wiki.wiki_lint import lint_wiki

    observed = datetime(2026, 7, 20, 20, 30, tzinfo=ZoneInfo("Asia/Seoul"))
    candidate = Candidate(
        "005930",
        "삼성전자",
        [
            Signal(
                SignalGroup.CATALYST, 25, "공시", "https://dart.fss.or.kr/a", observed
            ),
            Signal(SignalGroup.FLOW, 15, "수급", "https://data.krx.co.kr/a", observed),
        ],
    )

    ResearchHarness(calendar_bundle=calendar_bundle(observed)).run(
        [candidate],
        observed,
        "post-market",
        tmp_path,
        operational_evidence=operational_evidence(candidate),
    )

    assert (tmp_path / "Home.md").exists()
    assert (tmp_path / "Methodology.md").exists()
    assert lint_wiki(tmp_path) == []


def test_harness_marks_report_expiry_at_five_trading_days(tmp_path):
    observed = datetime(2026, 7, 20, 7, 30, tzinfo=ZoneInfo("Asia/Seoul"))
    candidate = Candidate(
        "000660",
        "SK하이닉스",
        [
            Signal(
                SignalGroup.CATALYST,
                25,
                "실적 촉매",
                "https://dart.fss.or.kr/a",
                observed,
            ),
            Signal(
                SignalGroup.FLOW, 15, "기관 수급", "https://data.krx.co.kr/a", observed
            ),
        ],
    )

    report = (
        ResearchHarness(calendar_bundle=calendar_bundle(observed))
        .run(
            [candidate],
            observed,
            "post-market",
            tmp_path,
            operational_evidence=operational_evidence(candidate),
        )
        .reports[0]
    )

    assert report.valid_until.date().isoformat() == "2026-07-27"


def test_harness_preserves_existing_wiki_when_expiry_calendar_is_missing(tmp_path):
    observed = datetime(2026, 12, 29, 20, 45, tzinfo=ZoneInfo("Asia/Seoul"))
    candidate = Candidate(
        "005930",
        "삼성전자",
        [
            Signal(
                SignalGroup.PRICE_VOLUME,
                20,
                "공식 KRX 시세",
                "https://data-dbg.krx.co.kr/svc/apis/sto/stk_bydd_trd",
                observed,
            )
        ],
    )
    output = tmp_path / "wiki"
    output.mkdir()
    sentinel = output / "Home.md"
    sentinel.write_text("preserve exactly\n", encoding="utf-8")

    import pytest

    with pytest.raises(ValueError, match="missing for 2027"):
        ResearchHarness(calendar_bundle=calendar_bundle(observed)).run(
            [candidate],
            observed,
            "post-market",
            output,
            operational_evidence=operational_evidence(candidate),
        )

    assert sentinel.read_text(encoding="utf-8") == "preserve exactly\n"
    assert list(output.iterdir()) == [sentinel]


def test_harness_morning_mode_uses_previous_krx_price_and_same_day_kind(tmp_path):
    observed = datetime(2026, 7, 21, 9, 25, tzinfo=ZoneInfo("Asia/Seoul"))
    nxt_url = "https://www.nextrade.co.kr/menu/transactionStatusMain/menuList.do"
    nxt_id = "nxt:price-snapshot:20260721:005930"
    krx_url = "https://data-dbg.krx.co.kr/svc/apis/sto/stk_bydd_trd"
    krx_id = "krx:daily:KOSPI:20260720:005930"
    krx_fetched_at = datetime(2026, 7, 20, 20, 45, tzinfo=ZoneInfo("Asia/Seoul"))
    candidate = Candidate(
        "005930",
        "삼성전자",
        [
            Signal(
                SignalGroup.CROSS_MARKET,
                15,
                "당일 NXT 20분 지연 등락률 +1.50%, 거래량 150,000주, "
                "거래대금 10,800,000,000원",
                nxt_url,
                observed,
                evidence_id=nxt_id,
            ),
            Signal(
                SignalGroup.PRICE_VOLUME,
                25,
                "전 거래일 KRX 등락률 +2.50%, 거래량 1,000,000주, "
                "거래대금 100,000,000,000원",
                krx_url,
                krx_fetched_at,
                evidence_id=krx_id,
            ),
        ],
    )
    evidence = operational_evidence(candidate)[candidate.ticker]
    previous_price = replace(
        evidence.price,
        evidence_id=krx_id,
        canonical_event_id=krx_id,
        published_date=date(2026, 7, 20),
        fetched_at=krx_fetched_at,
        metrics=(evidence.price.metrics | {"change_rate": 2.5}),
    )

    import pytest

    operational = {
        candidate.ticker: OperationalEvidence(
            candidate.ticker,
            previous_price,
            evidence.listing_risk,
        )
    }
    with pytest.raises(ValueError, match="official KRX live and NXT"):
        ResearchHarness(calendar_bundle=calendar_bundle(observed)).run(
            [candidate],
            observed,
            "morning",
            tmp_path,
            operational_evidence=operational,
            previous_business_date=date(2026, 7, 20),
        )

    source_as_of = datetime(2026, 7, 21, 9, 24, tzinfo=ZoneInfo("Asia/Seoul"))
    raw_rows = tuple(
        tuple(
            {
                "TRD_DD": "20260721",
                "DD_TP": "T_DD",
                "INVST_TP": investor,
                "ACC_BID_TRDVAL": "100",
                "ACC_ASK_TRDVAL": "100",
                "NETBID_TRDVAL": "0",
            }.items()
        )
        for investor in ("기관(십억원)", "외국인(십억원)", "개인(십억원)")
    )
    live = KrxLiveActivitySnapshot(
        date(2026, 7, 21),
        source_as_of,
        observed,
        tuple(
            KrxLiveMarketActivity(market, source_as_of, 600, raw_rows)
            for market in (KrxMarket.KOSPI, KrxMarket.KOSDAQ)
        ),
    )
    nxt = EvidenceRecord(
        source=EvidenceSource.NXT,
        evidence_id=nxt_id,
        canonical_event_id=nxt_id,
        kind="price-snapshot",
        company_name="삼성전자",
        title="NXT current price",
        source_url=nxt_url,
        published_date=date(2026, 7, 21),
        fetched_at=observed,
        verification=VerificationStatus.OFFICIAL,
        ticker="005930",
        delay_minutes=20,
        metrics={
            "market": "KOSPI",
            "current_price": 72_000,
            "change_rate": 1.5,
            "volume": 150_000,
            "trading_value": 10_800_000_000,
            "source_as_of": "2026-07-21T09:00:00+09:00",
        },
    )
    harness = ResearchHarness(calendar_bundle=calendar_bundle(observed))
    base_args = {
        "operational_evidence": operational,
        "previous_business_date": date(2026, 7, 20),
        "morning_krx_live_snapshot": live,
        "morning_nxt_evidence": {"005930": nxt},
    }
    mismatched_calendar_harness = ResearchHarness(
        calendar_bundle=calendar_bundle(observed + timedelta(minutes=1))
    )
    with pytest.raises(ValueError, match="calendar bundle must match"):
        mismatched_calendar_harness.run(
            [candidate], observed, "morning", tmp_path, **base_args
        )
    closure_harness = ResearchHarness(
        calendar_bundle=calendar_bundle(observed, (observed.date(),))
    )
    with pytest.raises(ValueError, match="scheduled KRX closure"):
        closure_harness.run([candidate], observed, "morning", tmp_path, **base_args)
    evening = observed.replace(hour=20)
    with pytest.raises(ValueError, match="09:20-12:00 KST"):
        ResearchHarness(calendar_bundle=calendar_bundle(evening)).run(
            [candidate], evening, "morning", tmp_path, **base_args
        )
    with pytest.raises(ValueError, match="mode must"):
        harness.run([candidate], observed, "invalid", tmp_path, **base_args)
    with pytest.raises(ValueError, match="timezone"):
        harness.run(
            [candidate], observed.replace(tzinfo=None), "morning", tmp_path, **base_args
        )
    with pytest.raises(ValueError, match="exact previous"):
        harness.run(
            [candidate],
            observed,
            "morning",
            tmp_path,
            **(base_args | {"previous_business_date": None}),
        )
    with pytest.raises(ValueError, match="KRX live"):
        harness.run(
            [candidate],
            observed,
            "morning",
            tmp_path,
            **(
                base_args
                | {
                    "morning_krx_live_snapshot": replace(
                        live, fetched_at=observed + timedelta(minutes=1)
                    )
                }
            ),
        )
    malformed_candidate = replace(
        candidate,
        signals=[
            candidate.signals[1],
            Signal(
                SignalGroup.CATALYST,
                20,
                "임의 촉매",
                "https://example.com/",
                observed,
            ),
        ],
    )
    with pytest.raises(ValueError, match="one NXT signal"):
        harness.run([malformed_candidate], observed, "morning", tmp_path, **base_args)
    with pytest.raises(ValueError, match="must match"):
        harness.run(
            [candidate],
            observed,
            "morning",
            tmp_path,
            **(base_args | {"morning_nxt_evidence": {}}),
        )
    for bad_source in (123, "not-a-timestamp"):
        bad_nxt = replace(nxt, metrics=nxt.metrics | {"source_as_of": bad_source})
        with pytest.raises(ValueError, match="timestamp"):
            harness.run(
                [candidate],
                observed,
                "morning",
                tmp_path,
                **(base_args | {"morning_nxt_evidence": {"005930": bad_nxt}}),
            )
    bad_nxt = replace(nxt, metrics=nxt.metrics | {"volume": 0})
    with pytest.raises(ValueError, match="invalid official"):
        harness.run(
            [candidate],
            observed,
            "morning",
            tmp_path,
            **(base_args | {"morning_nxt_evidence": {"005930": bad_nxt}}),
        )
    forged_id = "nxt:price-snapshot:20260721:forged"
    forged_candidate = replace(
        candidate,
        signals=[
            replace(candidate.signals[0], evidence_id=forged_id),
            candidate.signals[1],
        ],
    )
    forged_record = replace(nxt, evidence_id=forged_id, canonical_event_id=forged_id)
    invalid_canonical_records = (
        (forged_candidate, forged_record),
        (candidate, replace(nxt, canonical_event_id="nxt:forged")),
        (candidate, replace(nxt, is_correction=True)),
        (candidate, replace(nxt, is_withdrawn=True)),
        (
            replace(
                candidate,
                signals=[replace(candidate.signals[0], score=99), candidate.signals[1]],
            ),
            nxt,
        ),
        (
            replace(
                candidate,
                signals=[
                    replace(candidate.signals[0], reason="자가선언 NXT"),
                    candidate.signals[1],
                ],
            ),
            nxt,
        ),
        (candidate, replace(nxt, metrics=nxt.metrics | {"change_rate": 3.0})),
    )
    for bad_candidate, bad_record in invalid_canonical_records:
        with pytest.raises(ValueError, match="invalid official"):
            harness.run(
                [bad_candidate],
                observed,
                "morning",
                tmp_path,
                **(base_args | {"morning_nxt_evidence": {"005930": bad_record}}),
            )

    def with_krx_signal(signal):
        return replace(candidate, signals=[candidate.signals[0], signal])

    krx_signal = candidate.signals[1]
    forged_krx_id = "krx:daily:KOSPI:20260720:forged"
    invalid_krx_pairs = (
        (
            with_krx_signal(replace(krx_signal, evidence_id=forged_krx_id)),
            replace(
                previous_price,
                evidence_id=forged_krx_id,
                canonical_event_id=forged_krx_id,
            ),
        ),
        (candidate, replace(previous_price, canonical_event_id="krx:forged")),
        (candidate, replace(previous_price, is_correction=True)),
        (candidate, replace(previous_price, is_withdrawn=True)),
        (candidate, replace(previous_price, delay_minutes=1)),
        (with_krx_signal(replace(krx_signal, score=99)), previous_price),
        (with_krx_signal(replace(krx_signal, reason="자가선언 KRX")), previous_price),
        (
            with_krx_signal(replace(krx_signal, source_url="https://example.com/")),
            replace(previous_price, source_url="https://example.com/"),
        ),
        (
            with_krx_signal(
                replace(krx_signal, observed_at=krx_fetched_at + timedelta(seconds=1))
            ),
            previous_price,
        ),
        (
            candidate,
            replace(
                previous_price,
                metrics=previous_price.metrics | {"change_rate": 3.0},
            ),
        ),
    )
    for bad_candidate, bad_price in invalid_krx_pairs:
        with pytest.raises(ValueError, match="KRX signal provenance"):
            harness.run(
                [bad_candidate],
                observed,
                "morning",
                tmp_path,
                **(
                    base_args
                    | {
                        "operational_evidence": {
                            "005930": replace(operational["005930"], price=bad_price)
                        }
                    }
                ),
            )
    with pytest.raises(ValueError, match="official KRX price evidence"):
        harness.run(
            [candidate],
            observed,
            "morning",
            tmp_path / "missing-price",
            **(
                base_args
                | {
                    "operational_evidence": {
                        "005930": replace(operational["005930"], price=None)
                    }
                }
            ),
        )
    with pytest.raises(ValueError, match="only valid for morning"):
        harness.run([candidate], observed, "post-market", tmp_path, **base_args)

    result = ResearchHarness(calendar_bundle=calendar_bundle(observed)).run(
        [candidate],
        observed,
        "morning",
        tmp_path,
        operational_evidence=operational,
        previous_business_date=date(2026, 7, 20),
        morning_krx_live_snapshot=live,
        morning_nxt_evidence={"005930": nxt},
    )

    assert len(result.reports) == 1
    assert result.report_paths[0].name.endswith("-morning.md")
