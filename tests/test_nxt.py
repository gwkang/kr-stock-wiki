import json
from datetime import date, datetime
from urllib.parse import parse_qs
from zoneinfo import ZoneInfo

import kr_stock_wiki.collectors.nxt as nxt_module
from kr_stock_wiki.collectors.nxt import (
    NxtClient,
    NxtResponseError,
    NxtTransportError,
    _decimal,
    _integer,
)
from kr_stock_wiki.evidence import EvidenceSource, VerificationStatus


def _quote_row(code: str = "A000660", **overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "aggDd": "20260716",
        "isuSrdCd": code,
        "isuAbwdNm": "Example",
        "mktNm": "KOSPI",
        "curPrc": 183000,
        "contrastPrc": -25200,
        "upDownRate": -12.1,
        "oppr": 198700,
        "hgpr": 198700,
        "lwpr": 180200,
        "accTdQty": 3864042,
        "accTrval": 721865761050,
        "basePrc": "208200",
        "cptrTrdPmsnCd": "7",
        "cptrTrdPmsnCdNm": "전체",
        "trdIpsbRsn": "",
    }
    row.update(overrides)
    return row


def test_nxt_configuration_transport_json_pagination_and_numeric_guards():
    import pytest

    with pytest.raises(ValueError, match="timeout"):
        NxtClient(timeout=0)
    with pytest.raises(ValueError, match="page_size"):
        NxtClient(page_size=0)
    with pytest.raises(ValueError, match="max_pages"):
        NxtClient(max_pages=0)

    failing = NxtClient(
        transport=lambda _url, _body, _timeout: (_ for _ in ()).throw(OSError())
    )
    with pytest.raises(NxtTransportError, match="request failed"):
        failing.daily_quotes(date(2026, 7, 16))

    malformed_payloads = [
        (b"not-json", "invalid JSON"),
        (b"[]", "JSON object"),
    ]
    for payload, message in malformed_payloads:
        client = NxtClient(transport=lambda _url, _body, _timeout, value=payload: value)
        with pytest.raises(NxtResponseError, match=message):
            client.daily_quotes(date(2026, 7, 16))

    pagination_cases = [
        ({}, "invalid pagination"),
        ({"page": 2, "total": 2, "rows": []}, "page mismatch"),
        ({"page": 1, "total": 0, "rows": []}, "invalid total pages"),
        ({"page": 1, "total": 1, "rows": [1]}, "invalid rows"),
    ]
    for payload, message in pagination_cases:
        with pytest.raises(NxtResponseError, match=message):
            NxtClient._page(payload, 1, "rows")

    assert _integer(0) == 0
    assert _integer(None) is None
    assert _integer("-") is None
    assert _decimal(None) is None
    assert _decimal("-") is None
    with pytest.raises(NxtResponseError, match="finite"):
        _decimal("NaN")
    with pytest.raises(NxtResponseError, match="quote A000660.*curPrc"):
        _integer("not-a-number", field="curPrc", context="quote A000660")


def test_nxt_default_transport_blocks_untrusted_destinations_and_large_payloads(
    monkeypatch,
):
    import pytest

    with pytest.raises(NxtTransportError, match="untrusted endpoint"):
        nxt_module._default_transport("http://127.0.0.1/internal", b"", 1)

    class FakeResponse:
        def __init__(self, url: str, payload: bytes, content_length: str | None = None):
            self.url = url
            self.payload = payload
            self.headers = {}
            if content_length is not None:
                self.headers["Content-Length"] = content_length

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def geturl(self):
            return self.url

        def read(self, limit: int):
            return self.payload[:limit]

    class FakeOpener:
        def __init__(self, response: FakeResponse):
            self.response = response

        def open(self, _request, timeout: float):
            assert timeout == 1
            return self.response

    untrusted_response = FakeResponse("http://127.0.0.1/internal", b"{}")
    monkeypatch.setattr(
        nxt_module,
        "build_opener",
        lambda handler: FakeOpener(untrusted_response),
    )
    with pytest.raises(NxtTransportError, match="untrusted endpoint"):
        nxt_module._default_transport("https://www.nextrade.co.kr/endpoint", b"", 1)

    oversized = FakeResponse(
        "https://www.nextrade.co.kr/endpoint",
        b"{}",
        str(nxt_module._MAX_RESPONSE_BYTES + 1),
    )
    monkeypatch.setattr(
        nxt_module,
        "build_opener",
        lambda handler: FakeOpener(oversized),
    )
    with pytest.raises(NxtTransportError, match="size limit"):
        nxt_module._default_transport("https://www.nextrade.co.kr/endpoint", b"", 1)


def test_nxt_daily_quotes_paginate_and_normalize_official_delayed_data():
    calls: list[dict[str, list[str]]] = []

    def transport(_url: str, body: bytes, _timeout: float) -> bytes:
        params = parse_qs(body.decode())
        calls.append(params)
        page = int(params["pageIndex"][0])
        row = _quote_row(
            {1: "A000660", 2: "A0126Z0"}[page], isuAbwdNm=f"Example {page}"
        )
        return json.dumps(
            {
                "page": page,
                "total": 2,
                "records": 2,
                "setTime": "2026-07-16 20:05",
                "brdinfoTimeList": [row],
            }
        ).encode()

    fetched_at = datetime(2026, 7, 18, 9, 0, tzinfo=ZoneInfo("Asia/Seoul"))
    records = NxtClient(transport=transport, clock=lambda: fetched_at).daily_quotes(
        date(2026, 7, 16)
    )

    assert len(records) == 2
    assert records[1].ticker == "0126Z0"
    assert [params["pageIndex"] for params in calls] == [["1"], ["2"]]
    first = records[0]
    assert first.source is EvidenceSource.NXT
    assert first.verification is VerificationStatus.OFFICIAL
    assert first.evidence_id == "nxt:price-snapshot:20260716:000660"
    assert first.kind == "price-snapshot"
    assert first.ticker == "000660"
    assert first.delay_minutes == 20
    assert first.metrics["current_price"] == 183000
    assert first.metrics["change_rate"] == -12.1
    assert first.metrics["volume"] == 3864042
    assert first.metrics["available_sessions"] == "전체"
    assert first.metrics["source_as_of"] == "2026-07-16T20:05:00+09:00"
    assert first.raw["setTime"] == "2026-07-16 20:05"
    assert first.fetched_at == fetched_at


def test_nxt_accepts_stable_cumulative_prefix_pages_and_rejects_conflicts():
    import pytest

    first = _quote_row("A000660")
    second = _quote_row("A0126Z0", isuAbwdNm="Second")

    def transport(_url: str, body: bytes, _timeout: float) -> bytes:
        page = int(parse_qs(body.decode())["pageIndex"][0])
        rows = [first] if page == 1 else [first, second]
        return json.dumps(
            {
                "page": page,
                "total": 2,
                "records": 2,
                "setTime": "2026-07-16 20:05",
                "brdinfoTimeList": rows,
            }
        ).encode()

    records = NxtClient(transport=transport, page_size=1).daily_quotes(
        date(2026, 7, 16)
    )
    assert [record.ticker for record in records] == ["000660", "0126Z0"]

    def conflicting_transport(_url: str, body: bytes, _timeout: float) -> bytes:
        page = int(parse_qs(body.decode())["pageIndex"][0])
        changed_first = _quote_row("A000660", curPrc=184000)
        rows = [first] if page == 1 else [changed_first, second]
        return json.dumps(
            {
                "page": page,
                "total": 2,
                "records": 2,
                "setTime": "2026-07-16 20:05",
                "brdinfoTimeList": rows,
            }
        ).encode()

    with pytest.raises(NxtResponseError, match="000660 changed during pagination"):
        NxtClient(transport=conflicting_transport, page_size=1).daily_quotes(
            date(2026, 7, 16)
        )


def test_nxt_rejects_incomplete_quote_pagination_and_stale_reference_time():
    import pytest

    base = {
        "page": 1,
        "total": 1,
        "records": 2,
        "setTime": "2026-07-16 20:05",
        "brdinfoTimeList": [],
    }
    client = NxtClient(
        transport=lambda _url, _body, _timeout: json.dumps(base).encode()
    )
    with pytest.raises(NxtResponseError, match="record count mismatch"):
        client.daily_quotes(date(2026, 7, 16))

    stale = {**base, "records": 0, "setTime": "2026-07-15 20:05"}
    client = NxtClient(
        transport=lambda _url, _body, _timeout: json.dumps(stale).encode()
    )
    with pytest.raises(NxtResponseError, match="setTime.*business date"):
        client.daily_quotes(date(2026, 7, 16))

    malformed_time = {**base, "records": 0, "setTime": "2026-07-16-invalid"}
    client = NxtClient(
        transport=lambda _url, _body, _timeout: json.dumps(malformed_time).encode()
    )
    with pytest.raises(NxtResponseError, match="invalid setTime"):
        client.daily_quotes(date(2026, 7, 16))


def test_nxt_session_summary_rejects_ambiguous_or_malformed_rows():
    import pytest

    def client_for(payload: dict) -> NxtClient:
        return NxtClient(
            transport=lambda _url, _body, _timeout: json.dumps(payload).encode()
        )

    assert (
        client_for(
            {"page": 1, "total": 1, "records": 0, "dailyInfoList": []}
        ).session_summary(date(2026, 7, 16))
        is None
    )
    with pytest.raises(NxtResponseError, match="record count mismatch"):
        client_for(
            {"page": 1, "total": 1, "records": 1, "dailyInfoList": []}
        ).session_summary(date(2026, 7, 16))

    with pytest.raises(NxtResponseError, match="multiple pages"):
        client_for(
            {"page": 1, "total": 2, "records": 0, "dailyInfoList": []}
        ).session_summary(date(2026, 7, 16))
    with pytest.raises(NxtResponseError, match="multiple records"):
        client_for(
            {"page": 1, "total": 1, "records": 2, "dailyInfoList": [{}, {}]}
        ).session_summary(date(2026, 7, 16))
    with pytest.raises(NxtResponseError, match="missing required fields"):
        client_for(
            {
                "page": 1,
                "total": 1,
                "records": 1,
                "dailyInfoList": [{"aggDd": "20260716"}],
            }
        ).session_summary(date(2026, 7, 16))

    wrong_date_row = {
        "aggDd": "20260715",
        "preIsuCnt": 0,
        "preAccTdQty": 0,
        "preAccTrval": 0,
        "mainIsuCnt": 0,
        "mainAccTdQty": 0,
        "mainAccTrval": 0,
        "aftIsuCnt": 0,
        "aftAccTdQty": 0,
        "aftAccTrval": 0,
        "totalIsuCnt": 0,
        "totalAccTdQty": 0,
        "totalAccTrval": 0,
        "mktShr": 0,
    }
    with pytest.raises(NxtResponseError, match="business date mismatch"):
        client_for(
            {
                "page": 1,
                "total": 1,
                "records": 1,
                "dailyInfoList": [wrong_date_row],
            }
        ).session_summary(date(2026, 7, 16))

    inconsistent_totals = {
        **wrong_date_row,
        "aggDd": "20260716",
        "totalAccTdQty": 1,
        "totalAccTrval": 1,
    }
    with pytest.raises(NxtResponseError, match="session totals mismatch"):
        client_for(
            {
                "page": 1,
                "total": 1,
                "records": 1,
                "dailyInfoList": [inconsistent_totals],
            }
        ).session_summary(date(2026, 7, 16))


def test_nxt_session_summary_preserves_pre_main_after_market_totals():
    def transport(_url: str, body: bytes, _timeout: float) -> bytes:
        params = parse_qs(body.decode())
        assert params["scBeginDe"] == ["20260716"]
        assert params["scEndDe"] == ["20260716"]
        return json.dumps(
            {
                "page": 1,
                "total": 1,
                "records": 1,
                "dailyInfoList": [
                    {
                        "aggDd": "20260716",
                        "preIsuCnt": 600,
                        "preAccTdQty": 33163995,
                        "preAccTrval": 4469693466890,
                        "mainIsuCnt": 607,
                        "mainAccTdQty": 60934697,
                        "mainAccTrval": 9710661356380,
                        "aftIsuCnt": 606,
                        "aftAccTdQty": 21987461,
                        "aftAccTrval": 3665445987900,
                        "totalIsuCnt": 607,
                        "totalAccTdQty": 116086153,
                        "totalAccTrval": 17845800811170,
                        "mktShr": 11.04,
                    }
                ],
            }
        ).encode()

    record = NxtClient(transport=transport).session_summary(date(2026, 7, 16))

    assert record is not None
    assert record.evidence_id == "nxt:session-summary:20260716"
    assert record.delay_minutes is None
    assert record.metrics["pre_volume"] == 33163995
    assert record.metrics["main_volume"] == 60934697
    assert record.metrics["after_volume"] == 21987461
    assert record.metrics["pre_session"] == "08:00-08:50"
    assert record.metrics["main_session"] == "09:00:30-15:20"
    assert record.metrics["after_session"] == "15:40-20:00"
