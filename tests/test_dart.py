import json
import traceback
from datetime import date, datetime
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

from kr_stock_wiki.collectors.dart import (
    DartClient,
    DartResponseError,
    DartTransportError,
)
from kr_stock_wiki.evidence import EvidenceSource, VerificationStatus


def test_default_transport_rejects_non_opendart_endpoint():
    import pytest

    client = DartClient(api_key="k" * 40)
    client.endpoint = "file:///etc/passwd"

    with pytest.raises(DartTransportError, match="untrusted endpoint"):
        client.search(date(2026, 7, 18), date(2026, 7, 18))


def test_evidence_rejects_negative_delay_metadata():
    import pytest

    with pytest.raises(ValueError, match="delay_minutes"):
        from kr_stock_wiki.evidence import EvidenceRecord

        EvidenceRecord(
            source=EvidenceSource.NXT,
            evidence_id="nxt:1",
            canonical_event_id="nxt-event:1",
            kind="trade",
            company_name="Example",
            title="NXT delayed quote",
            source_url="https://www.nextrade.co.kr/",
            published_date=date(2026, 7, 18),
            fetched_at=datetime(2026, 7, 18, tzinfo=ZoneInfo("Asia/Seoul")),
            verification=VerificationStatus.OFFICIAL,
            ticker="005930",
            delay_minutes=-20,
        )


def test_evidence_rejects_non_integer_delay_metadata():
    import pytest

    with pytest.raises(ValueError, match="delay_minutes"):
        from kr_stock_wiki.evidence import EvidenceRecord

        EvidenceRecord(
            source=EvidenceSource.NXT,
            evidence_id="nxt:1",
            canonical_event_id="nxt-event:1",
            kind="trade",
            company_name="Example",
            title="NXT delayed quote",
            source_url="https://www.nextrade.co.kr/",
            published_date=date(2026, 7, 18),
            fetched_at=datetime(2026, 7, 18, tzinfo=ZoneInfo("Asia/Seoul")),
            verification=VerificationStatus.OFFICIAL,
            ticker="005930",
            delay_minutes=1.5,
        )


def test_evidence_rejects_non_finite_numeric_metric():
    import pytest
    from kr_stock_wiki.evidence import EvidenceRecord

    with pytest.raises(ValueError, match="finite"):
        EvidenceRecord(
            source=EvidenceSource.KRX,
            evidence_id="krx:1",
            canonical_event_id="krx:1",
            kind="daily-price",
            company_name="Example",
            title="Daily price",
            source_url="https://data-dbg.krx.co.kr/svc/apis/sto/stk_bydd_trd",
            published_date=date(2026, 7, 18),
            fetched_at=datetime(2026, 7, 18, tzinfo=ZoneInfo("Asia/Seoul")),
            verification=VerificationStatus.OFFICIAL,
            ticker="005930",
            metrics={"change_rate": float("nan")},
        )


def test_dart_rejects_market_wide_range_over_three_months():
    import pytest

    client = DartClient(
        api_key="k" * 40,
        transport=lambda _url, _timeout: b'{"status":"013"}',
    )

    with pytest.raises(ValueError, match="three months"):
        client.search(date(2026, 1, 1), date(2026, 4, 2))


def test_repeated_original_filings_keep_distinct_canonical_events():
    payload = """{
      "status":"000", "message":"normal", "list":[
        {"corp_cls":"Y","corp_name":"Example","corp_code":"00126380","stock_code":"005930","report_nm":"Executive Ownership Report","rcept_no":"20260102000111","flr_nm":"A","rcept_dt":"20260102","rm":""},
        {"corp_cls":"Y","corp_name":"Example","corp_code":"00126380","stock_code":"005930","report_nm":"Executive Ownership Report","rcept_no":"20260718000123","flr_nm":"B","rcept_dt":"20260718","rm":""}
      ]
    }""".encode()
    client = DartClient(api_key="k" * 40, transport=lambda _url, _timeout: payload)

    first, second = client.search(
        date(2026, 1, 2), date(2026, 7, 18), corp_code="00126380"
    )

    assert first.canonical_event_id != second.canonical_event_id


def test_dart_rejects_repeated_first_page_response():
    import pytest

    def transport(_url: str, _timeout: float) -> bytes:
        return json.dumps(
            {
                "status": "000",
                "page_no": 1,
                "page_count": 10,
                "total_count": 20,
                "total_page": 2,
                "list": [],
            }
        ).encode()

    client = DartClient(api_key="k" * 40, transport=transport)

    with pytest.raises(DartResponseError, match="page mismatch"):
        client.search(date(2026, 7, 18), date(2026, 7, 18))


def test_dart_pagination_deduplicates_receipt_numbers():
    def transport(url: str, _timeout: float) -> bytes:
        page = int(parse_qs(urlparse(url).query)["page_no"][0])
        item = {
            "corp_cls": "Y",
            "corp_name": "Example",
            "corp_code": "00126380",
            "stock_code": "005930",
            "report_nm": "Major Event Report",
            "rcept_no": "20260718000123",
            "flr_nm": "Example",
            "rcept_dt": "20260718",
            "rm": "",
        }
        return json.dumps(
            {"status": "000", "total_page": 2, "page_no": page, "list": [item]}
        ).encode()

    records = DartClient(api_key="k" * 40, transport=transport).search(
        date(2026, 7, 18), date(2026, 7, 18)
    )

    assert [record.evidence_id for record in records] == ["dart:20260718000123"]


def test_dart_rejects_non_object_json_response():
    import pytest

    client = DartClient(api_key="k" * 40, transport=lambda _url, _timeout: b"[]")

    with pytest.raises(DartResponseError, match="JSON object"):
        client.search(date(2026, 7, 18), date(2026, 7, 18))


def test_dart_transport_error_never_exposes_api_key():
    import pytest

    key = "k" * 40

    def transport(url: str, _timeout: float) -> bytes:
        raise OSError(f"request failed: {url}")

    client = DartClient(api_key=key, transport=transport)

    with pytest.raises(DartTransportError) as captured:
        client.search(date(2026, 7, 18), date(2026, 7, 18))

    assert key not in str(captured.value)
    rendered = "".join(
        traceback.format_exception(
            type(captured.value), captured.value, captured.value.__traceback__
        )
    )
    assert key not in rendered


def test_dart_search_collects_every_page_and_filters_corporation():
    pages: list[int] = []

    def transport(url: str, _timeout: float) -> bytes:
        query = parse_qs(urlparse(url).query)
        page = int(query["page_no"][0])
        pages.append(page)
        assert query["corp_code"] == ["00126380"]
        item = {
            "corp_cls": "Y",
            "corp_name": "Example",
            "corp_code": "00126380",
            "stock_code": "005930",
            "report_nm": f"Report {page}",
            "rcept_no": f"2026071800012{page}",
            "flr_nm": "Example",
            "rcept_dt": "20260718",
            "rm": "",
        }
        return json.dumps(
            {"status": "000", "message": "normal", "total_page": 2, "list": [item]}
        ).encode()

    client = DartClient(api_key="k" * 40, transport=transport)

    records = client.search(date(2026, 7, 18), date(2026, 7, 18), corp_code="00126380")

    assert pages == [1, 2]
    assert [record.evidence_id for record in records] == [
        "dart:20260718000121",
        "dart:20260718000122",
    ]


def test_dart_recognizes_korean_correction_markers_only():
    payload = """{
      "status":"000", "message":"normal", "list":[
        {"corp_cls":"Y","corp_name":"Example","corp_code":"00126380","stock_code":"005930","report_nm":"[기재정정] 사업보고서","rcept_no":"20260718000121","flr_nm":"Example","rcept_dt":"20260718","rm":"정"},
        {"corp_cls":"Y","corp_name":"Example","corp_code":"00126380","stock_code":"005930","report_nm":"[첨부정정] 사업보고서","rcept_no":"20260718000122","flr_nm":"Example","rcept_dt":"20260718","rm":"정"},
        {"corp_cls":"Y","corp_name":"Example","corp_code":"00126380","stock_code":"005930","report_nm":"[첨부추가] 사업보고서","rcept_no":"20260718000123","flr_nm":"Example","rcept_dt":"20260718","rm":""},
        {"corp_cls":"Y","corp_name":"Example","corp_code":"00126380","stock_code":"005930","report_nm":"[변경등록] 사업보고서","rcept_no":"20260718000124","flr_nm":"Example","rcept_dt":"20260718","rm":""},
        {"corp_cls":"Y","corp_name":"Example","corp_code":"00126380","stock_code":"005930","report_nm":"[연결] 사업보고서","rcept_no":"20260718000125","flr_nm":"Example","rcept_dt":"20260718","rm":"연"}
      ]
    }""".encode()

    records = DartClient(
        api_key="k" * 40, transport=lambda _url, _timeout: payload
    ).search(date(2026, 7, 18), date(2026, 7, 18))

    assert [record.is_correction for record in records] == [
        True,
        True,
        True,
        True,
        False,
    ]


def test_dart_correction_keeps_stable_receipt_canonical_id():
    payload = """{
      "status":"000", "message":"normal", "list":[
        {"corp_cls":"Y","corp_name":"Example","corp_code":"00126380","stock_code":"005930","report_nm":"Major Event Report (Capital Increase)","rcept_no":"20260717000111","flr_nm":"Example","rcept_dt":"20260717","rm":""},
        {"corp_cls":"Y","corp_name":"Example","corp_code":"00126380","stock_code":"005930","report_nm":"[Correction] Major Event Report (Capital Increase)","rcept_no":"20260718000123","flr_nm":"Example","rcept_dt":"20260718","rm":"Correction"}
      ]
    }""".encode()
    client = DartClient(api_key="k" * 40, transport=lambda _url, _timeout: payload)

    original, correction = client.search(date(2026, 7, 17), date(2026, 7, 18))

    assert original.evidence_id != correction.evidence_id
    assert original.canonical_event_id == original.evidence_id
    assert correction.canonical_event_id == correction.evidence_id
    assert original.is_correction is False
    assert correction.is_correction is True


def test_dart_search_returns_empty_for_official_no_data_status():
    client = DartClient(
        api_key="k" * 40,
        transport=lambda _url, _timeout: b'{"status":"013","message":"no data"}',
    )

    assert client.search(date(2026, 7, 18), date(2026, 7, 18)) == []


def test_dart_search_normalizes_official_filing_without_exposing_key():
    captured: dict[str, object] = {}

    def transport(url: str, timeout: float) -> bytes:
        captured.update(url=url, timeout=timeout)
        return b"""{
          "status": "000",
          "message": "normal",
          "page_no": 1,
          "page_count": 100,
          "total_count": 1,
          "total_page": 1,
          "list": [{
            "corp_cls": "Y",
            "corp_name": "Samsung Electronics",
            "corp_code": "00126380",
            "stock_code": "005930",
            "report_nm": "Major Event Report (Capital Increase)",
            "rcept_no": "20260718000123",
            "flr_nm": "Samsung Electronics",
            "rcept_dt": "20260718",
            "rm": ""
          }]
        }"""

    fetched_at = datetime(2026, 7, 18, 21, 0, tzinfo=ZoneInfo("Asia/Seoul"))
    client = DartClient(
        api_key="k" * 40,
        transport=transport,
        clock=lambda: fetched_at,
    )

    records = client.search(date(2026, 7, 18), date(2026, 7, 18))

    assert len(records) == 1
    record = records[0]
    assert record.source is EvidenceSource.DART
    assert record.verification is VerificationStatus.OFFICIAL
    assert record.ticker == "005930"
    assert record.published_date == date(2026, 7, 18)
    assert record.fetched_at == fetched_at
    assert record.source_url.endswith("rcpNo=20260718000123")
    assert record.evidence_id == "dart:20260718000123"
    serialized = record.to_dict()
    assert serialized["source"] == "dart"
    assert serialized["published_date"] == "2026-07-18"
    assert serialized["fetched_at"] == fetched_at.isoformat()
    assert serialized["raw"]["rcept_no"] == "20260718000123"
    assert "crtfc_key" not in json.dumps(serialized)
    assert "k" * 40 not in repr(client)
    assert "crtfc_key=" + "k" * 40 in str(captured["url"])
