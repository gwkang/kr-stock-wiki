import json
import traceback
from datetime import date, datetime
from zoneinfo import ZoneInfo

from kr_stock_wiki.collectors.krx import (
    KrxClient,
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


def test_krx_requests_both_official_market_endpoints_for_empty_trading_day():
    calls: list[str] = []

    def transport(url: str, _timeout: float) -> bytes:
        calls.append(url)
        return b'{"OutBlock_1": []}'

    records = KrxClient(api_key="secret-krx-key", transport=transport).daily_prices(
        date(2026, 7, 18)
    )

    assert records == []
    assert "/sto/stk_bydd_trd?" in calls[0]
    assert "/sto/ksq_bydd_trd?" in calls[1]
    assert all("basDd=20260718" in url for url in calls)


def test_krx_rejects_business_date_mismatch():
    import pytest

    payload = json.dumps({"OutBlock_1": [_official_row(BAS_DD="20260716")]}).encode()
    client = KrxClient(
        api_key="secret-krx-key", transport=lambda _url, _timeout: payload
    )

    with pytest.raises(KrxResponseError, match="business date mismatch"):
        client.daily_prices(date(2026, 7, 17), markets=(KrxMarket.KOSPI,))


def test_krx_preserves_numeric_zero_values():
    payload = json.dumps(
        {"OutBlock_1": [_official_row(CMPPREVDD_PRC=0, FLUC_RT=0.0, ACC_TRDVOL=0)]}
    ).encode()
    client = KrxClient(
        api_key="secret-krx-key", transport=lambda _url, _timeout: payload
    )

    record = client.daily_prices(date(2026, 7, 17), markets=(KrxMarket.KOSPI,))[0]

    assert record.metrics["change"] == 0
    assert record.metrics["change_rate"] == 0.0
    assert record.metrics["volume"] == 0


def test_krx_rejects_missing_required_quote_field():
    import pytest

    row = _official_row()
    del row["TDD_CLSPRC"]
    payload = json.dumps({"OutBlock_1": [row]}).encode()
    client = KrxClient(
        api_key="secret-krx-key", transport=lambda _url, _timeout: payload
    )

    with pytest.raises(KrxResponseError, match="005930.*TDD_CLSPRC"):
        client.daily_prices(date(2026, 7, 17), markets=(KrxMarket.KOSPI,))


def test_krx_rejects_market_mismatch():
    import pytest

    payload = json.dumps({"OutBlock_1": [_official_row(MKT_NM="KOSDAQ")]}).encode()
    client = KrxClient(
        api_key="secret-krx-key", transport=lambda _url, _timeout: payload
    )

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
    client = KrxClient(
        api_key="secret-krx-key", transport=lambda _url, _timeout: payload
    )

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
            api_key="secret-krx-key",
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
        api_key="secret-krx-key",
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
        api_key="secret-krx-key",
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
    assert "AUTH_KEY=secret-krx-key" in str(captured["url"])
    serialized = record.to_dict()
    assert "secret-krx-key" not in str(serialized)
    assert "AUTH_KEY" not in record.source_url
    assert "secret-krx-key" not in repr(client)
