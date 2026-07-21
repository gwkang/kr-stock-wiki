from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from kr_stock_wiki.collectors.calendar import (
    KrxCalendarBundle,
    KrxMarketCalendar,
    MarketHoliday,
)
from kr_stock_wiki.collectors.krx import KrxDailySnapshot, KrxMarket
from kr_stock_wiki.collectors.krx_live import (
    KrxLiveActivitySnapshot,
    KrxLiveMarketActivity,
)
from kr_stock_wiki.daily import build_morning_input, build_post_market_input
from kr_stock_wiki.evidence import EvidenceRecord, EvidenceSource, VerificationStatus


KST = ZoneInfo("Asia/Seoul")
BUSINESS_DATE = date(2026, 7, 20)
AS_OF = datetime(2026, 7, 20, 20, 45, tzinfo=KST)
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


def calendar_bundle(as_of: datetime) -> KrxCalendarBundle:
    year = as_of.astimezone(KST).year
    days = [date(year, 1, day) for day in range(1, 10)] + [date(year, 12, 31)]
    holidays = tuple(
        MarketHoliday(
            day,
            _WEEKDAY_CODES[day.weekday()],
            _WEEKDAY_NAMES[day.weekday()],
            "test",
        )
        for day in days
    )
    calendar = KrxMarketCalendar(year, holidays, as_of - timedelta(minutes=1))
    return KrxCalendarBundle((calendar,), as_of)


def price_record(
    ticker: str = "005930",
    *,
    source: EvidenceSource = EvidenceSource.KRX,
    name: str = "삼성전자",
    published_date: date = BUSINESS_DATE,
    fetched_at: datetime = AS_OF - timedelta(minutes=20),
) -> EvidenceRecord:
    if source is EvidenceSource.KRX:
        return EvidenceRecord(
            source=source,
            evidence_id=f"krx:daily:KOSPI:20260720:{ticker}",
            canonical_event_id=f"krx:daily:KOSPI:20260720:{ticker}",
            kind="daily-price",
            company_name=name,
            title=f"{name} KRX KOSPI 일별 시세",
            source_url="https://data-dbg.krx.co.kr/svc/apis/sto/stk_bydd_trd",
            published_date=published_date,
            fetched_at=fetched_at,
            verification=VerificationStatus.OFFICIAL,
            ticker=ticker,
            metrics={
                "close": 71_000,
                "change_rate": 2.5,
                "volume": 1_000_000,
                "trading_value": 71_000_000_000,
                "market_cap": 400_000_000_000_000,
            },
            raw={"MKT_NM": "KOSPI"},
        )
    return EvidenceRecord(
        source=source,
        evidence_id=f"nxt:price-snapshot:20260720:{ticker}",
        canonical_event_id=f"nxt:price-snapshot:20260720:{ticker}",
        kind="price-snapshot",
        company_name=name,
        title=f"{name} NXT 현재가 스냅샷",
        source_url="https://www.nextrade.co.kr/menu/transactionStatusMain/menuList.do",
        published_date=published_date,
        fetched_at=fetched_at,
        verification=VerificationStatus.OFFICIAL,
        ticker=ticker,
        delay_minutes=20,
        metrics={
            "market": "KOSPI",
            "current_price": 71_200,
            "change_rate": 2.8,
            "volume": 300_000,
            "trading_value": 21_000_000_000,
            "source_as_of": "2026-07-20T20:20:00+09:00",
        },
        raw={"setTime": "2026-07-20 20:20"},
    )


def krx_snapshot(*records: EvidenceRecord) -> KrxDailySnapshot:
    return KrxDailySnapshot(
        business_date=BUSINESS_DATE,
        requested_markets=(KrxMarket.KOSPI,),
        completed_markets=(KrxMarket.KOSPI,),
        record_counts=((KrxMarket.KOSPI, len(records)),),
        records=tuple(records),
        fetched_at=max(record.fetched_at for record in records),
    )


def session_summary(
    *, fetched_at: datetime = AS_OF - timedelta(minutes=19)
) -> EvidenceRecord:
    return EvidenceRecord(
        source=EvidenceSource.NXT,
        evidence_id="nxt:session-summary:20260720",
        canonical_event_id="nxt:session-summary:20260720",
        kind="session-summary",
        company_name="NEXTRADE",
        title="NXT 2026-07-20 세션별 거래 현황",
        source_url="https://www.nextrade.co.kr/menu/transactionStatusDaily/menuList.do",
        published_date=BUSINESS_DATE,
        fetched_at=fetched_at,
        verification=VerificationStatus.OFFICIAL,
        metrics={
            "pre_session": "08:00-08:50",
            "pre_instruments": 100,
            "pre_volume": 10,
            "pre_trading_value": 100,
            "main_session": "09:00:30-15:20",
            "main_instruments": 200,
            "main_volume": 20,
            "main_trading_value": 200,
            "after_session": "15:40-20:00",
            "after_instruments": 150,
            "after_volume": 30,
            "after_trading_value": 300,
            "total_instruments": 250,
            "total_volume": 60,
            "total_trading_value": 600,
            "volume_market_share": 12.3,
        },
    )


def nxt_payload(*records: EvidenceRecord) -> dict[str, object]:
    all_records = (*records, session_summary())
    return {
        "schema_version": 1,
        "source": "nxt",
        "collected_at": max(record.fetched_at for record in all_records).isoformat(),
        "date": BUSINESS_DATE.isoformat(),
        "quote_delay_minutes": 20,
        "records": [record.to_dict() for record in all_records],
    }


def watchlist(*items: tuple[str, str]) -> dict[str, object]:
    return {
        "schema_version": 1,
        "source": "user-watchlist",
        "stocks": [{"ticker": ticker, "name": name} for ticker, name in items],
    }


def test_build_post_market_input_uses_independent_official_krx_and_nxt_signals():
    krx = price_record()
    nxt = price_record(source=EvidenceSource.NXT)

    payload = build_post_market_input(
        watchlist(("005930", "삼성전자")),
        krx_snapshot(krx),
        nxt_payload(nxt),
        AS_OF,
    )

    assert payload["schema_version"] == 1
    assert payload["source"] == "official-post-market-builder"
    assert payload["as_of"] == AS_OF.isoformat()
    assert payload["business_date"] == BUSINESS_DATE.isoformat()
    assert payload["mode"] == "post-market"
    assert len(payload["candidates"]) == 1
    candidate = payload["candidates"][0]
    assert candidate["ticker"] == "005930"
    assert candidate["name"] == "삼성전자"
    assert candidate["risk_penalty"] == 0
    assert [signal["group"] for signal in candidate["signals"]] == [
        "price-volume",
        "cross-market",
    ]
    assert [signal["evidence_id"] for signal in candidate["signals"]] == [
        krx.evidence_id,
        nxt.evidence_id,
    ]
    assert [signal["score"] for signal in candidate["signals"]] == [25.0, 28.0]
    assert "2.50%" in candidate["signals"][0]["reason"]
    assert "20분 지연" in candidate["signals"][1]["reason"]


def test_build_post_market_input_keeps_watchlist_stock_unqualified_without_nxt_quote():
    payload = build_post_market_input(
        watchlist(("005930", "삼성전자")),
        krx_snapshot(price_record()),
        nxt_payload(
            price_record(
                "000660",
                source=EvidenceSource.NXT,
                name="SK하이닉스",
            )
        ),
        AS_OF,
    )

    assert [signal["group"] for signal in payload["candidates"][0]["signals"]] == [
        "price-volume"
    ]


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda watch, krx, nxt: watch.update(source="sample"), "watchlist"),
        (
            lambda watch, krx, nxt: watch["stocks"].append(
                {"ticker": "005930", "name": "삼성전자"}
            ),
            "unique",
        ),
        (
            lambda watch, krx, nxt: watch["stocks"][0].update(name="다른회사"),
            "name",
        ),
        (lambda watch, krx, nxt: nxt.update(date="2026-07-19"), "date"),
        (lambda watch, krx, nxt: nxt.update(quote_delay_minutes=0), "delay"),
        (lambda watch, krx, nxt: nxt.update(source="sample"), "NXT"),
    ],
)
def test_build_post_market_input_rejects_untrusted_or_inconsistent_envelopes(
    mutate, message
):
    watch = watchlist(("005930", "삼성전자"))
    krx = krx_snapshot(price_record())
    nxt = nxt_payload(price_record(source=EvidenceSource.NXT))
    mutate(watch, krx, nxt)

    with pytest.raises(ValueError, match=message):
        build_post_market_input(watch, krx, nxt, AS_OF)


def test_build_post_market_input_rejects_future_evidence_and_stale_nxt_source_time():
    future = price_record(
        source=EvidenceSource.NXT, fetched_at=AS_OF + timedelta(seconds=1)
    )
    with pytest.raises(ValueError, match="future"):
        build_post_market_input(
            watchlist(("005930", "삼성전자")),
            krx_snapshot(price_record()),
            nxt_payload(future),
            AS_OF,
        )

    stale = price_record(source=EvidenceSource.NXT)
    stale.metrics["source_as_of"] = "2026-07-20T17:00:00+09:00"
    with pytest.raises(ValueError, match="20:20"):
        build_post_market_input(
            watchlist(("005930", "삼성전자")),
            krx_snapshot(price_record()),
            nxt_payload(stale),
            AS_OF,
        )


def test_build_post_market_input_rejects_tampered_nxt_quote_lineage():
    payload = nxt_payload(price_record(source=EvidenceSource.NXT))
    payload["records"][0]["evidence_id"] = "nxt:price-snapshot:20260720:000660"
    with pytest.raises(ValueError, match="invalid NXT quote"):
        build_post_market_input(
            watchlist(("005930", "삼성전자")),
            krx_snapshot(price_record()),
            payload,
            AS_OF,
        )


def test_build_post_market_input_requires_complete_nxt_after_market_summary():
    quote = price_record(source=EvidenceSource.NXT)
    missing = nxt_payload(quote)
    missing["records"] = [quote.to_dict()]
    missing["collected_at"] = quote.fetched_at.isoformat()
    with pytest.raises(ValueError, match="session-summary"):
        build_post_market_input(
            watchlist(("005930", "삼성전자")),
            krx_snapshot(price_record()),
            missing,
            AS_OF,
        )

    inconsistent = nxt_payload(quote)
    inconsistent["records"][-1]["metrics"]["total_volume"] = 61
    with pytest.raises(ValueError, match="session totals"):
        build_post_market_input(
            watchlist(("005930", "삼성전자")),
            krx_snapshot(price_record()),
            inconsistent,
            AS_OF,
        )


def test_build_post_market_input_requires_after_market_delayed_source_time():
    quote = price_record(source=EvidenceSource.NXT)
    quote.metrics["source_as_of"] = "2026-07-20T20:19:59+09:00"
    with pytest.raises(ValueError, match="20:20"):
        build_post_market_input(
            watchlist(("005930", "삼성전자")),
            krx_snapshot(price_record()),
            nxt_payload(quote),
            AS_OF,
        )


def test_build_post_market_input_rejects_inconsistent_nxt_time_lineage():
    quote = price_record(source=EvidenceSource.NXT)
    payload = nxt_payload(quote)
    payload["collected_at"] = "2026-07-20T20:19:00+09:00"
    with pytest.raises(ValueError, match="time lineage"):
        build_post_market_input(
            watchlist(("005930", "삼성전자")),
            krx_snapshot(price_record()),
            payload,
            AS_OF,
        )

    quote = price_record(source=EvidenceSource.NXT)
    quote.metrics["source_as_of"] = "2026-07-20T20:30:00+09:00"
    with pytest.raises(ValueError, match="time lineage"):
        build_post_market_input(
            watchlist(("005930", "삼성전자")),
            krx_snapshot(price_record()),
            nxt_payload(quote),
            AS_OF,
        )


def test_build_post_market_input_rejects_pre_close_analysis_time():
    with pytest.raises(ValueError, match="20:20"):
        build_post_market_input(
            watchlist(("005930", "삼성전자")),
            krx_snapshot(price_record()),
            nxt_payload(price_record(source=EvidenceSource.NXT)),
            datetime(2026, 7, 20, 19, 0, tzinfo=KST),
        )


def test_build_post_market_input_limits_watchlist_to_twenty_stocks():
    stocks = tuple((f"{index:06d}", f"회사{index}") for index in range(21))
    with pytest.raises(ValueError, match="20"):
        build_post_market_input(
            watchlist(*stocks),
            krx_snapshot(price_record()),
            nxt_payload(price_record(source=EvidenceSource.NXT)),
            AS_OF,
        )


def _morning_nxt_payload(*records: EvidenceRecord) -> dict[str, object]:
    collected_at = max(record.fetched_at for record in records)
    return {
        "schema_version": 1,
        "source": "nxt",
        "collected_at": collected_at.isoformat(),
        "date": "2026-07-21",
        "quote_delay_minutes": 20,
        "records": [record.to_dict() for record in records],
    }


def _morning_nxt_record(
    *,
    ticker: str = "005930",
    name: str = "삼성전자",
    volume: int = 150_000,
    trading_value: int = 10_800_000_000,
) -> EvidenceRecord:
    fetched_at = datetime(2026, 7, 21, 9, 25, tzinfo=KST)
    evidence_id = f"nxt:price-snapshot:20260721:{ticker}"
    return EvidenceRecord(
        source=EvidenceSource.NXT,
        evidence_id=evidence_id,
        canonical_event_id=evidence_id,
        kind="price-snapshot",
        company_name=name,
        title=f"{name} NXT 현재가 스냅샷",
        source_url="https://www.nextrade.co.kr/menu/transactionStatusMain/menuList.do",
        published_date=date(2026, 7, 21),
        fetched_at=fetched_at,
        verification=VerificationStatus.OFFICIAL,
        ticker=ticker,
        delay_minutes=20,
        metrics={
            "market": "KOSPI",
            "current_price": 72_000,
            "change_rate": 1.5,
            "volume": volume,
            "trading_value": trading_value,
            "source_as_of": "2026-07-21T09:00:00+09:00",
        },
        raw={"setTime": "2026-07-21 09:00"},
    )


def _live_snapshot() -> KrxLiveActivitySnapshot:
    source_as_of = datetime(2026, 7, 21, 9, 24, tzinfo=KST)
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
    return KrxLiveActivitySnapshot(
        business_date=date(2026, 7, 21),
        source_as_of=source_as_of,
        fetched_at=datetime(2026, 7, 21, 9, 25, tzinfo=KST),
        activities=tuple(
            KrxLiveMarketActivity(market, source_as_of, 600, raw_rows)
            for market in (KrxMarket.KOSPI, KrxMarket.KOSDAQ)
        ),
    )


def test_build_morning_input_requires_live_krx_and_current_nxt_evidence():
    as_of = datetime(2026, 7, 21, 9, 25, tzinfo=KST)
    krx = price_record(fetched_at=datetime(2026, 7, 20, 20, 45, tzinfo=KST))
    nxt = _morning_nxt_record()

    payload = build_morning_input(
        watchlist(("005930", "삼성전자")),
        krx_snapshot(krx),
        _morning_nxt_payload(nxt),
        _live_snapshot(),
        calendar_bundle(as_of),
        date(2026, 7, 20),
        as_of,
    )

    assert payload["source"] == "official-morning-builder"
    assert payload["mode"] == "morning"
    assert payload["business_date"] == "2026-07-21"
    assert [signal["group"] for signal in payload["candidates"][0]["signals"]] == [
        "price-volume",
        "cross-market",
    ]


def test_build_morning_input_rejects_nxt_before_delayed_main_market_evidence():
    as_of = datetime(2026, 7, 21, 9, 25, tzinfo=KST)
    nxt = _morning_nxt_record()
    nxt.metrics["source_as_of"] = "2026-07-21T08:59:00+09:00"

    with pytest.raises(ValueError, match="09:00 KST"):
        build_morning_input(
            watchlist(("005930", "삼성전자")),
            krx_snapshot(
                price_record(fetched_at=datetime(2026, 7, 20, 20, 45, tzinfo=KST))
            ),
            _morning_nxt_payload(nxt),
            _live_snapshot(),
            calendar_bundle(as_of),
            date(2026, 7, 20),
            as_of,
        )


def test_build_morning_input_does_not_use_zero_trade_quote_as_cross_market():
    as_of = datetime(2026, 7, 21, 9, 25, tzinfo=KST)
    zero_candidate = _morning_nxt_record(volume=0, trading_value=0)
    unrelated_positive = _morning_nxt_record(ticker="000660", name="SK하이닉스")

    payload = build_morning_input(
        watchlist(("005930", "삼성전자")),
        krx_snapshot(
            price_record(fetched_at=datetime(2026, 7, 20, 20, 45, tzinfo=KST))
        ),
        _morning_nxt_payload(zero_candidate, unrelated_positive),
        _live_snapshot(),
        calendar_bundle(as_of),
        date(2026, 7, 20),
        as_of,
    )

    assert [signal["group"] for signal in payload["candidates"][0]["signals"]] == [
        "price-volume"
    ]


def test_build_morning_input_requires_exact_calendar_previous_business_date():
    as_of = datetime(2026, 7, 21, 9, 25, tzinfo=KST)
    stale_record = price_record(
        published_date=date(2026, 7, 17),
        fetched_at=datetime(2026, 7, 17, 20, 45, tzinfo=KST),
    )
    stale_snapshot = KrxDailySnapshot(
        business_date=date(2026, 7, 17),
        requested_markets=(KrxMarket.KOSPI,),
        completed_markets=(KrxMarket.KOSPI,),
        record_counts=((KrxMarket.KOSPI, 1),),
        records=(stale_record,),
        fetched_at=stale_record.fetched_at,
    )

    with pytest.raises(ValueError, match="official calendar"):
        build_morning_input(
            watchlist(("005930", "삼성전자")),
            stale_snapshot,
            _morning_nxt_payload(_morning_nxt_record()),
            _live_snapshot(),
            calendar_bundle(as_of),
            date(2026, 7, 17),
            as_of,
        )
