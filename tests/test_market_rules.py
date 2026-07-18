from datetime import date, datetime
from zoneinfo import ZoneInfo

from kr_stock_wiki.collectors.krx import KrxDailySnapshot, KrxMarket
from kr_stock_wiki.evidence import EvidenceRecord, EvidenceSource, VerificationStatus
from kr_stock_wiki.market_rules import (
    ListingRisk,
    MarketDayStatus,
    OperationalDecision,
    OperationalFilter,
    TradingDayGate,
    apply_operational_decision,
)
from kr_stock_wiki.models import Candidate, Signal, SignalGroup
from kr_stock_wiki.scanner import BalancedRanker


def krx_record(
    ticker: str = "005930",
    market: str = "KOSPI",
    *,
    business_date: date = date(2026, 7, 17),
    close: int = 100_000,
    volume: int = 1_000_000,
    trading_value: int = 100_000_000_000,
    market_cap: int = 500_000_000_000,
) -> EvidenceRecord:
    date_text = business_date.strftime("%Y%m%d")
    evidence_id = f"krx:daily:{market}:{date_text}:{ticker}"
    return EvidenceRecord(
        source=EvidenceSource.KRX,
        evidence_id=evidence_id,
        canonical_event_id=evidence_id,
        kind="daily-price",
        company_name="테스트 종목",
        title="KRX 일별 시세",
        source_url=(
            "https://data-dbg.krx.co.kr/svc/apis/sto/"
            + ("stk_bydd_trd" if market == "KOSPI" else "ksq_bydd_trd")
        ),
        published_date=business_date,
        fetched_at=datetime(2026, 7, 17, 20, 30, tzinfo=ZoneInfo("Asia/Seoul")),
        verification=VerificationStatus.OFFICIAL,
        ticker=ticker,
        metrics={
            "close": close,
            "volume": volume,
            "trading_value": trading_value,
            "market_cap": market_cap,
        },
        raw={"MKT_NM": market},
    )


def krx_snapshot(
    business_date: date,
    records: list[EvidenceRecord],
    *,
    completed: tuple[KrxMarket, ...] = (KrxMarket.KOSPI, KrxMarket.KOSDAQ),
) -> KrxDailySnapshot:
    counts = tuple(
        (
            market,
            sum(1 for record in records if record.raw.get("MKT_NM") == market.value),
        )
        for market in completed
    )
    return KrxDailySnapshot(
        business_date=business_date,
        requested_markets=(KrxMarket.KOSPI, KrxMarket.KOSDAQ),
        completed_markets=completed,
        record_counts=counts,
        records=tuple(records),
        fetched_at=max(
            (record.fetched_at for record in records),
            default=datetime(
                business_date.year,
                business_date.month,
                business_date.day,
                20,
                30,
                tzinfo=ZoneInfo("Asia/Seoul"),
            ),
        ),
    )


def listing_risk(
    *,
    ticker: str = "005930",
    as_of: date = date(2026, 7, 17),
    administrative_issue: int = 0,
    trading_halt: int = 0,
    investment_warning: int = 0,
) -> ListingRisk:
    evidence_id = f"kind:listing-risk:{as_of.isoformat()}:{ticker}"
    evidence = EvidenceRecord(
        source=EvidenceSource.KIND,
        evidence_id=evidence_id,
        canonical_event_id=evidence_id,
        kind="listing-risk-status",
        company_name="테스트 종목",
        title="KRX KIND 투자유의 상태",
        source_url="https://kind.krx.co.kr/investwarn/adminissue.do",
        published_date=as_of,
        fetched_at=datetime(2026, 7, 18, 20, 30, tzinfo=ZoneInfo("Asia/Seoul")),
        verification=VerificationStatus.OFFICIAL,
        ticker=ticker,
        metrics={
            "administrative_issue": administrative_issue,
            "trading_halt": trading_halt,
            "investment_warning": investment_warning,
        },
    )
    return ListingRisk(ticker=ticker, as_of=as_of, evidence=evidence)


def test_trading_day_gate_closes_weekends_and_confirms_only_complete_official_day():
    gate = TradingDayGate(
        minimum_record_counts=((KrxMarket.KOSPI, 1), (KrxMarket.KOSDAQ, 1))
    )
    weekend = gate.assess(date(2026, 7, 18), None)
    assert weekend.status is MarketDayStatus.CLOSED
    assert weekend.reason == "주말 휴장"

    records = [krx_record(), krx_record("247540", "KOSDAQ")]
    opened = gate.assess(date(2026, 7, 17), krx_snapshot(date(2026, 7, 17), records))
    assert opened.status is MarketDayStatus.OPEN
    assert opened.markets == ("KOSDAQ", "KOSPI")

    incomplete = gate.assess(
        date(2026, 7, 17),
        krx_snapshot(date(2026, 7, 17), [records[0]], completed=(KrxMarket.KOSPI,)),
    )
    assert incomplete.status is MarketDayStatus.UNKNOWN
    assert "KOSDAQ" in incomplete.reason

    absent = gate.assess(date(2026, 7, 17), None)
    assert absent.status is MarketDayStatus.UNKNOWN
    assert "공식 KRX 스냅샷 없음" in absent.reason

    weekday_holiday = gate.assess(
        date(2026, 7, 17), krx_snapshot(date(2026, 7, 17), [])
    )
    assert weekday_holiday.status is MarketDayStatus.UNKNOWN
    assert "거래 레코드 없음" in weekday_holiday.reason


def test_trading_day_gate_default_rejects_implausibly_small_market_snapshot():
    business_date = date(2026, 7, 17)
    snapshot = krx_snapshot(
        business_date,
        [
            krx_record(market="KOSPI", business_date=business_date),
            krx_record(ticker="247540", market="KOSDAQ", business_date=business_date),
        ],
    )

    decision = TradingDayGate().assess(business_date, snapshot)

    assert decision.status is MarketDayStatus.UNKNOWN
    assert "cardinality" in decision.reason
    assert "KOSPI=1<500" in decision.reason
    assert "KOSDAQ=1<1000" in decision.reason


def test_trading_day_gate_rejects_inconsistent_weekend_or_record_contract():
    import pytest

    gate = TradingDayGate()
    weekend_record = krx_record(business_date=date(2026, 7, 18))
    with pytest.raises(ValueError, match="weekend"):
        gate.assess(
            date(2026, 7, 18),
            krx_snapshot(date(2026, 7, 18), [weekend_record]),
        )
    with pytest.raises(ValueError, match="business date"):
        gate.assess(
            date(2026, 7, 17),
            krx_snapshot(date(2026, 7, 16), []),
        )


def test_operational_filter_requires_liquidity_and_verified_listing_risk():
    policy = OperationalFilter()
    record = krx_record()

    eligible = policy.evaluate(record, listing_risk())
    assert eligible.eligible is True
    assert eligible.reasons == ()

    unknown = policy.evaluate(
        record, ListingRisk(ticker="005930", as_of=date(2026, 7, 17))
    )
    assert unknown.eligible is False
    assert set(unknown.reasons) == {
        "관리종목 여부 미확인",
        "거래정지 여부 미확인",
        "투자경고 여부 미확인",
    }

    risky = policy.evaluate(
        record,
        listing_risk(
            administrative_issue=1,
            trading_halt=1,
            investment_warning=1,
        ),
    )
    assert risky.eligible is False
    assert set(risky.reasons) == {"관리종목", "거래정지", "투자경고"}


def test_operational_filter_applies_explicit_configurable_liquidity_thresholds():
    policy = OperationalFilter(
        minimum_close=1_000,
        minimum_volume=100_000,
        minimum_trading_value=5_000_000_000,
        minimum_market_cap=100_000_000_000,
    )
    decision = policy.evaluate(
        krx_record(
            close=999,
            volume=99_999,
            trading_value=4_999_999_999,
            market_cap=99_999_999_999,
        ),
        listing_risk(),
    )

    assert decision.eligible is False
    assert len(decision.reasons) == 4
    assert "종가 1,000원 미만" in decision.reasons
    assert "거래량 100,000주 미만" in decision.reasons
    assert "거래대금 5,000,000,000원 미만" in decision.reasons
    assert "시가총액 100,000,000,000원 미만" in decision.reasons


def test_operational_decision_is_applied_as_ranker_hard_exclusion():
    import pytest

    observed = datetime(2026, 7, 17, 20, 30, tzinfo=ZoneInfo("Asia/Seoul"))
    candidate = Candidate(
        "005930",
        "삼성전자",
        [
            Signal(
                SignalGroup.CATALYST,
                25,
                "공식 촉매",
                "https://example.com/catalyst",
                observed,
            ),
            Signal(
                SignalGroup.PRICE_VOLUME,
                20,
                "가격·거래량",
                "https://example.com/price",
                observed,
            ),
        ],
    )
    decision = OperationalFilter().evaluate(
        krx_record(), ListingRisk(ticker="005930", as_of=date(2026, 7, 17))
    )
    filtered = apply_operational_decision(candidate, decision)

    assert candidate.hard_exclusion is None
    assert filtered.hard_exclusion is not None
    assert "관리종목 여부 미확인" in filtered.hard_exclusion
    result = BalancedRanker(minimum_score=0).evaluate(filtered)
    assert result.qualified is False
    assert any("강제 제외" in reason for reason in result.reasons)

    with pytest.raises(ValueError, match="ticker"):
        apply_operational_decision(Candidate("000660", "SK하이닉스"), decision)


def test_listing_risk_requires_official_kind_evidence_and_matching_as_of():
    from dataclasses import replace

    import pytest

    valid = listing_risk()
    assert valid.evidence is not None
    for invalid, message in (
        (replace(valid.evidence, source=EvidenceSource.KRX), "official KIND"),
        (
            replace(valid.evidence, verification=VerificationStatus.UNVERIFIED),
            "official KIND",
        ),
        (replace(valid.evidence, ticker="000660"), "ticker"),
        (replace(valid.evidence, published_date=date(2026, 7, 16)), "as-of"),
        (
            replace(
                valid.evidence,
                metrics={**valid.evidence.metrics, "trading_halt": 2},
            ),
            "0 or 1",
        ),
    ):
        with pytest.raises(ValueError, match=message):
            ListingRisk(ticker="005930", as_of=date(2026, 7, 17), evidence=invalid)

    non_integer_metrics = dict(valid.evidence.metrics)
    non_integer_metrics["trading_halt"] = 1.0
    with pytest.raises(ValueError, match="integer 0 or 1"):
        ListingRisk(
            ticker=valid.ticker,
            as_of=valid.as_of,
            evidence=replace(valid.evidence, metrics=non_integer_metrics),
        )

    with pytest.raises(ValueError, match="as-of date"):
        OperationalFilter().evaluate(krx_record(business_date=date(2026, 7, 16)), valid)


def test_operational_decision_rejects_inconsistent_state():
    import pytest

    with pytest.raises(ValueError, match="eligible decisions"):
        OperationalDecision("005930", True, ("모순",))
    with pytest.raises(ValueError, match="eligible decisions"):
        OperationalDecision("005930", False, ())


def test_operational_filter_rejects_non_official_or_malformed_price_evidence():
    import pytest

    record = krx_record()
    object.__setattr__(record, "verification", VerificationStatus.UNVERIFIED)
    with pytest.raises(ValueError, match="official KRX daily-price"):
        OperationalFilter().evaluate(record, listing_risk())

    malformed = krx_record()
    malformed.metrics["trading_value"] = None
    with pytest.raises(ValueError, match="trading_value"):
        OperationalFilter().evaluate(malformed, listing_risk())
