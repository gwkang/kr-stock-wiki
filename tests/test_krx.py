import json
import traceback
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from kr_stock_wiki.collectors.krx import (
    KrxClient,
    KrxDailySnapshot,
    KrxMarket,
    KrxResponseError,
    KrxTransportError,
)
from kr_stock_wiki.evidence import EvidenceSource, VerificationStatus


def _official_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "BAS_DD": "20260717",
        "ISU_CD": "005930",
        "ISU_NM": "Samsung Electronics",
        "MKT_NM": "KOSPI",
        "TDD_CLSPRC": "71,000",
        "CMPPREVDD_PRC": "1,000",
        "FLUC_RT": "1.43",
        "TDD_OPNPRC": "70,100",
        "TDD_HGPRC": "71,500",
        "TDD_LWPRC": "69,900",
        "ACC_TRDVOL": "12,345,678",
        "ACC_TRDVAL": "876,543,210,000",
        "MKTCAP": "423,000,000,000,000",
        "LIST_SHRS": "5,969,782,550",
    }
    row.update(overrides)
    return row


def test_krx_verified_snapshot_proves_market_completion_and_counts():
    import pytest

    def transport(url: str, _timeout: float) -> bytes:
        market = "KOSPI" if "/stk_bydd_trd?" in url else "KOSDAQ"
        ticker = "005930" if market == "KOSPI" else "247540"
        return json.dumps(
            {"OutBlock_1": [_official_row(ISU_CD=ticker, ISU_NM=ticker, MKT_NM=market)]}
        ).encode()

    snapshot = KrxClient(
        api_key="test-key",
        transport=transport,
        minimum_record_counts=((KrxMarket.KOSPI, 1), (KrxMarket.KOSDAQ, 1)),
    ).daily_snapshot(date(2026, 7, 17))

    assert snapshot.coverage_complete is True
    assert snapshot.requested_markets == (KrxMarket.KOSPI, KrxMarket.KOSDAQ)
    assert snapshot.completed_markets == snapshot.requested_markets
    assert snapshot.counts == {KrxMarket.KOSPI: 1, KrxMarket.KOSDAQ: 1}
    assert len(snapshot.records) == 2
    assert KrxDailySnapshot.from_payload(snapshot.to_payload()) == snapshot

    tampered = snapshot.to_payload()
    tampered["coverage_complete"] = False
    with pytest.raises(ValueError, match="coverage_complete"):
        KrxDailySnapshot.from_payload(tampered)

    with pytest.raises(ValueError, match="record counts"):
        KrxDailySnapshot(
            business_date=snapshot.business_date,
            requested_markets=snapshot.requested_markets,
            completed_markets=snapshot.completed_markets,
            record_counts=((KrxMarket.KOSPI, 2), (KrxMarket.KOSDAQ, 1)),
            records=snapshot.records,
            fetched_at=snapshot.fetched_at,
        )

    from dataclasses import replace

    with pytest.raises(ValueError, match="official endpoint"):
        KrxDailySnapshot(
            business_date=snapshot.business_date,
            requested_markets=snapshot.requested_markets,
            completed_markets=snapshot.completed_markets,
            record_counts=snapshot.record_counts,
            records=(
                replace(
                    snapshot.records[0],
                    source_url="https://example.com/svc/apis/sto/stk_bydd_trd",
                ),
                snapshot.records[1],
            ),
            fetched_at=snapshot.fetched_at,
        )

    future = snapshot.fetched_at + timedelta(seconds=1)
    with pytest.raises(ValueError, match="latest record"):
        KrxDailySnapshot(
            business_date=snapshot.business_date,
            requested_markets=snapshot.requested_markets,
            completed_markets=snapshot.completed_markets,
            record_counts=snapshot.record_counts,
            records=tuple(
                replace(record, fetched_at=future) for record in snapshot.records
            ),
            fetched_at=snapshot.fetched_at,
        )


def test_krx_default_snapshot_rejects_partial_market_cardinality():
    import pytest

    def transport(url: str, _timeout: float) -> bytes:
        market = "KOSPI" if "/stk_bydd_trd?" in url else "KOSDAQ"
        ticker = "005930" if market == "KOSPI" else "247540"
        return json.dumps(
            {"OutBlock_1": [_official_row(ISU_CD=ticker, ISU_NM=ticker, MKT_NM=market)]}
        ).encode()

    with pytest.raises(KrxResponseError, match="minimum market cardinality"):
        KrxClient(api_key="test-key", transport=transport).daily_snapshot(
            date(2026, 7, 17)
        )


def test_krx_requests_both_official_market_endpoints_for_empty_trading_day():
    calls: list[str] = []

    def transport(url: str, _timeout: float) -> bytes:
        calls.append(url)
        return b'{"OutBlock_1": []}'

    records = KrxClient(api_key="test-key", transport=transport).daily_prices(
        date(2026, 7, 18)
    )

    assert records == []
    assert "/sto/stk_bydd_trd?" in calls[0]
    assert "/sto/ksq_bydd_trd?" in calls[1]
    assert all("basDd=20260718" in url for url in calls)


def test_krx_rejects_business_date_mismatch():
    import pytest

    payload = json.dumps({"OutBlock_1": [_official_row(BAS_DD="20260716")]}).encode()
    client = KrxClient(api_key="test-key", transport=lambda _url, _timeout: payload)

    with pytest.raises(KrxResponseError, match="business date mismatch"):
        client.daily_prices(date(2026, 7, 17), markets=(KrxMarket.KOSPI,))


def test_krx_preserves_numeric_zero_values():
    payload = json.dumps(
        {"OutBlock_1": [_official_row(CMPPREVDD_PRC=0, FLUC_RT=0.0, ACC_TRDVOL=0)]}
    ).encode()
    client = KrxClient(api_key="test-key", transport=lambda _url, _timeout: payload)

    record = client.daily_prices(date(2026, 7, 17), markets=(KrxMarket.KOSPI,))[0]

    assert record.metrics["change"] == 0
    assert record.metrics["change_rate"] == 0.0
    assert record.metrics["volume"] == 0


def test_krx_rejects_missing_required_quote_field():
    import pytest

    row = _official_row()
    del row["TDD_CLSPRC"]
    payload = json.dumps({"OutBlock_1": [row]}).encode()
    client = KrxClient(api_key="test-key", transport=lambda _url, _timeout: payload)

    with pytest.raises(KrxResponseError, match="005930.*TDD_CLSPRC"):
        client.daily_prices(date(2026, 7, 17), markets=(KrxMarket.KOSPI,))


def test_krx_rejects_market_mismatch():
    import pytest

    payload = json.dumps({"OutBlock_1": [_official_row(MKT_NM="KOSDAQ")]}).encode()
    client = KrxClient(api_key="test-key", transport=lambda _url, _timeout: payload)

    with pytest.raises(KrxResponseError, match="005930.*KOSPI.*KOSDAQ"):
        client.daily_prices(date(2026, 7, 17), markets=(KrxMarket.KOSPI,))


def test_krx_rejects_error_code_even_when_records_are_present():
    import pytest

    payload = json.dumps(
        {
            "respCode": "401",
            "respMsg": "Unauthorized Key",
            "OutBlock_1": [_official_row()],
        }
    ).encode()
    client = KrxClient(api_key="test-key", transport=lambda _url, _timeout: payload)

    with pytest.raises(KrxResponseError, match="401.*Unauthorized Key"):
        client.daily_prices(date(2026, 7, 17), markets=(KrxMarket.KOSPI,))


def test_krx_rejects_malformed_response_shapes():
    import pytest

    cases = [
        (b"not-json", "invalid JSON"),
        (b"[]", "JSON object"),
        (b"{}", "missing OutBlock_1"),
        (b'{"OutBlock_1":[1]}', "must contain JSON objects"),
    ]
    for payload, message in cases:
        client = KrxClient(
            api_key="test-key",
            transport=lambda _url, _timeout, value=payload: value,
        )
        with pytest.raises(KrxResponseError, match=message):
            client.daily_prices(date(2026, 7, 17), markets=(KrxMarket.KOSPI,))


def test_krx_transport_error_never_exposes_api_key():
    import pytest

    key = "secret-krx-key"

    def transport(url: str, _timeout: float) -> bytes:
        raise OSError(f"request failed: {url}")

    client = KrxClient(api_key=key, transport=transport)

    with pytest.raises(KrxTransportError) as captured:
        client.daily_prices(date(2026, 7, 17), markets=(KrxMarket.KOSPI,))

    rendered = "".join(
        traceback.format_exception(
            type(captured.value), captured.value, captured.value.__traceback__
        )
    )
    assert key not in rendered


def test_krx_api_error_response_is_rejected():
    import pytest

    client = KrxClient(
        api_key="test-key",
        transport=lambda _url, _timeout: (
            b'{"respMsg":"Unauthorized Key","respCode":"401"}'
        ),
    )

    with pytest.raises(KrxResponseError, match="401.*Unauthorized Key"):
        client.daily_prices(date(2026, 7, 17), markets=(KrxMarket.KOSPI,))


def test_krx_daily_prices_normalize_official_kospi_response_without_key_leak():
    captured: dict[str, object] = {}

    def transport(url: str, timeout: float) -> bytes:
        captured.update(url=url, timeout=timeout)
        return b"""{
          "OutBlock_1": [{
            "BAS_DD": "20260717",
            "ISU_CD": "005930",
            "ISU_NM": "Samsung Electronics",
            "MKT_NM": "KOSPI",
            "SECT_TP_NM": "Common",
            "TDD_CLSPRC": "71,000",
            "CMPPREVDD_PRC": "1,000",
            "FLUC_RT": "1.43",
            "TDD_OPNPRC": "70,100",
            "TDD_HGPRC": "71,500",
            "TDD_LWPRC": "69,900",
            "ACC_TRDVOL": "12,345,678",
            "ACC_TRDVAL": "876,543,210,000",
            "MKTCAP": "423,000,000,000,000",
            "LIST_SHRS": "5,969,782,550"
          }]
        }"""

    fetched_at = datetime(2026, 7, 18, 9, 0, tzinfo=ZoneInfo("Asia/Seoul"))
    client = KrxClient(
        api_key="test-key",
        transport=transport,
        clock=lambda: fetched_at,
    )

    records = client.daily_prices(date(2026, 7, 17), markets=(KrxMarket.KOSPI,))

    assert len(records) == 1
    record = records[0]
    assert record.source is EvidenceSource.KRX
    assert record.verification is VerificationStatus.OFFICIAL
    assert record.evidence_id == "krx:daily:KOSPI:20260717:005930"
    assert record.ticker == "005930"
    assert record.published_date == date(2026, 7, 17)
    assert record.fetched_at == fetched_at
    assert record.metrics["close"] == 71000
    assert record.metrics["change_rate"] == 1.43
    assert record.metrics["volume"] == 12345678
    assert "AUTH_KEY=test-key" in str(captured["url"])
    serialized = record.to_dict()
    assert "test-key" not in str(serialized)
    assert "AUTH_KEY" not in record.source_url
    assert "test-key" not in repr(client)
