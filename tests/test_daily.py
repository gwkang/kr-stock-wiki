from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from kr_stock_wiki.collectors.krx import KrxDailySnapshot, KrxMarket
from kr_stock_wiki.daily import build_post_market_input
from kr_stock_wiki.evidence import EvidenceRecord, EvidenceSource, VerificationStatus


KST = ZoneInfo("Asia/Seoul")
BUSINESS_DATE = date(2026, 7, 20)
AS_OF = datetime(2026, 7, 20, 20, 45, tzinfo=KST)


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
