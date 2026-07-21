import json
from dataclasses import replace
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from kr_stock_wiki.collectors.krx_live import (
    KrxLiveClient,
    KrxLiveResponseError,
    KrxLiveTransportError,
    KrxMarket,
    _default_transport,
    _read,
)

KST = ZoneInfo("Asia/Seoul")
BUSINESS_DATE = date(2026, 7, 21)
FETCHED_AT = datetime(2026, 7, 21, 9, 25, tzinfo=KST)


def _payload(market: KrxMarket, *, trading_value: str = "100") -> bytes:
    names = ["기관(십억원)", "외국인(십억원)", "개인(십억원)"]
    return json.dumps(
        {
            "output": [
                {
                    "TRD_DD": "20260721",
                    "DD_TP": "T_DD",
                    "INVST_TP": name,
                    "ACC_BID_TRDVAL": trading_value,
                    "ACC_ASK_TRDVAL": trading_value,
                    "NETBID_TRDVAL": "0",
                }
                for name in names
            ],
            "CURRENT_DATETIME": "2026.07.21 AM 09:24:30",
        },
        ensure_ascii=False,
    ).encode()


def test_live_activity_requires_positive_same_day_trades_in_both_markets():
    client = KrxLiveClient(
        transport=lambda timeout: {
            KrxMarket.KOSPI: _payload(KrxMarket.KOSPI),
            KrxMarket.KOSDAQ: _payload(KrxMarket.KOSDAQ),
        },
        clock=lambda: FETCHED_AT,
    )

    snapshot = client.current_activity(BUSINESS_DATE)

    assert snapshot.business_date == BUSINESS_DATE
    assert snapshot.source_as_of == datetime(2026, 7, 21, 9, 24, 30, tzinfo=KST)
    assert snapshot.markets == (KrxMarket.KOSPI, KrxMarket.KOSDAQ)
    assert snapshot.total_trading_value > 0
    assert snapshot.to_payload()["source"] == "krx-live-market-activity"


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda payload: payload.update(CURRENT_DATETIME="2026.07.20 AM 09:24:30"),
            "date",
        ),
        (lambda payload: payload["output"][0].update(TRD_DD="20260720"), "date"),
        (
            lambda payload: [
                row.update(ACC_BID_TRDVAL="0", ACC_ASK_TRDVAL="0")
                for row in payload["output"]
            ],
            "positive trading value",
        ),
        (lambda payload: payload["output"].pop(), "investor categories"),
    ],
)
def test_live_activity_fails_closed_on_invalid_official_payload(mutate, message):
    payload = json.loads(_payload(KrxMarket.KOSPI))
    mutate(payload)
    client = KrxLiveClient(
        transport=lambda timeout: {
            KrxMarket.KOSPI: json.dumps(payload, ensure_ascii=False).encode(),
            KrxMarket.KOSDAQ: _payload(KrxMarket.KOSDAQ),
        },
        clock=lambda: FETCHED_AT,
    )

    with pytest.raises(KrxLiveResponseError, match=message):
        client.current_activity(BUSINESS_DATE)


def test_live_activity_rejects_source_before_krx_open():
    payload = json.loads(_payload(KrxMarket.KOSPI))
    payload["CURRENT_DATETIME"] = "2026.07.21 AM 08:59:59"
    client = KrxLiveClient(
        transport=lambda timeout: {
            KrxMarket.KOSPI: json.dumps(payload, ensure_ascii=False).encode(),
            KrxMarket.KOSDAQ: json.dumps(payload, ensure_ascii=False).encode(),
        },
        clock=lambda: FETCHED_AT,
    )

    with pytest.raises(KrxLiveResponseError, match="after the KRX open"):
        client.current_activity(BUSINESS_DATE)


def test_live_activity_payload_round_trip_is_strict():
    snapshot = KrxLiveClient(
        transport=lambda timeout: {
            KrxMarket.KOSPI: _payload(KrxMarket.KOSPI),
            KrxMarket.KOSDAQ: _payload(KrxMarket.KOSDAQ),
        },
        clock=lambda: FETCHED_AT,
    ).current_activity(BUSINESS_DATE)
    payload = snapshot.to_payload()

    assert type(snapshot).from_payload(payload) == snapshot

    payload["unexpected"] = True
    with pytest.raises(ValueError, match="envelope"):
        type(snapshot).from_payload(payload)


def test_live_activity_payload_rejects_non_string_and_market_time_tampering():
    snapshot = KrxLiveClient(
        transport=lambda timeout: {
            KrxMarket.KOSPI: _payload(KrxMarket.KOSPI),
            KrxMarket.KOSDAQ: _payload(KrxMarket.KOSDAQ),
        },
        clock=lambda: FETCHED_AT,
    ).current_activity(BUSINESS_DATE)

    for field in ("business_date", "source_as_of", "collected_at"):
        payload = snapshot.to_payload()
        payload[field] = 20260721
        with pytest.raises(ValueError, match="timestamp"):
            type(snapshot).from_payload(payload)

    payload = snapshot.to_payload()
    payload["markets"][0]["source_as_of"] = "2026-07-21T09:20:00+09:00"
    with pytest.raises(ValueError, match="timestamp lineage"):
        type(snapshot).from_payload(payload)


def test_live_activity_dataclasses_enforce_source_and_raw_invariants():
    snapshot = KrxLiveClient(
        transport=lambda timeout: {
            KrxMarket.KOSPI: _payload(KrxMarket.KOSPI),
            KrxMarket.KOSDAQ: _payload(KrxMarket.KOSDAQ),
        },
        clock=lambda: FETCHED_AT,
    ).current_activity(BUSINESS_DATE)

    with pytest.raises(ValueError, match="source URL"):
        replace(snapshot, source_url="https://evil.example/")
    with pytest.raises(ValueError, match="invalid KRX live market"):
        replace(snapshot.activities[0], market="KOSPI")
    with pytest.raises(ValueError, match="include timezone"):
        replace(snapshot.activities[0], source_as_of=FETCHED_AT.replace(tzinfo=None))
    with pytest.raises(ValueError, match="market activity"):
        replace(snapshot.activities[0], trading_value=True)
    with pytest.raises(ValueError, match="investor row"):
        rows = list(snapshot.activities[0].raw_rows)
        rows[0] = rows[0][:-1]
        replace(snapshot.activities[0], raw_rows=tuple(rows))
    with pytest.raises(ValueError, match="market activity"):
        replace(snapshot.activities[0], raw_rows=())


def test_live_activity_rejects_missing_market_and_naive_clock():
    client = KrxLiveClient(
        transport=lambda timeout: {KrxMarket.KOSPI: _payload(KrxMarket.KOSPI)},
        clock=lambda: FETCHED_AT,
    )
    with pytest.raises(KrxLiveResponseError, match="both markets"):
        client.current_activity(BUSINESS_DATE)

    client = KrxLiveClient(
        transport=lambda timeout: {
            KrxMarket.KOSPI: _payload(KrxMarket.KOSPI),
            KrxMarket.KOSDAQ: _payload(KrxMarket.KOSDAQ),
        },
        clock=lambda: FETCHED_AT.replace(tzinfo=None),
    )
    with pytest.raises(KrxLiveResponseError, match="timezone-aware"):
        client.current_activity(BUSINESS_DATE)


@pytest.mark.parametrize(
    ("kospi", "kosdaq", "clock", "message"),
    [
        (b"not-json", _payload(KrxMarket.KOSDAQ), FETCHED_AT, "invalid JSON"),
        (
            json.dumps({"output": [], "CURRENT_DATETIME": "x", "extra": 1}).encode(),
            _payload(KrxMarket.KOSDAQ),
            FETCHED_AT,
            "schema",
        ),
        (
            _payload(KrxMarket.KOSPI),
            _payload(KrxMarket.KOSDAQ).replace(b"09:24:30", b"09:21:00"),
            FETCHED_AT,
            "contemporaneous",
        ),
        (
            _payload(KrxMarket.KOSPI),
            _payload(KrxMarket.KOSDAQ),
            datetime(2026, 7, 21, 9, 24, tzinfo=KST),
            "lineage",
        ),
        (
            _payload(KrxMarket.KOSPI),
            _payload(KrxMarket.KOSDAQ),
            datetime(2026, 7, 21, 9, 31, tzinfo=KST),
            "lineage",
        ),
    ],
)
def test_live_activity_rejects_schema_and_time_lineage(kospi, kosdaq, clock, message):
    client = KrxLiveClient(
        transport=lambda timeout: {
            KrxMarket.KOSPI: kospi,
            KrxMarket.KOSDAQ: kosdaq,
        },
        clock=lambda: clock,
    )

    with pytest.raises(KrxLiveResponseError, match=message):
        client.current_activity(BUSINESS_DATE)


def test_live_activity_rejects_duplicate_investor_and_negative_value():
    payload = json.loads(_payload(KrxMarket.KOSPI))
    payload["output"][1]["INVST_TP"] = payload["output"][0]["INVST_TP"]
    client = KrxLiveClient(
        transport=lambda timeout: {
            KrxMarket.KOSPI: json.dumps(payload, ensure_ascii=False).encode(),
            KrxMarket.KOSDAQ: _payload(KrxMarket.KOSDAQ),
        },
        clock=lambda: FETCHED_AT,
    )
    with pytest.raises(KrxLiveResponseError, match="investor categories"):
        client.current_activity(BUSINESS_DATE)

    payload = json.loads(_payload(KrxMarket.KOSPI))
    payload["output"][0]["ACC_BID_TRDVAL"] = "-1"
    client = KrxLiveClient(
        transport=lambda timeout: {
            KrxMarket.KOSPI: json.dumps(payload, ensure_ascii=False).encode(),
            KrxMarket.KOSDAQ: _payload(KrxMarket.KOSDAQ),
        },
        clock=lambda: FETCHED_AT,
    )
    with pytest.raises(KrxLiveResponseError, match="non-negative"):
        client.current_activity(BUSINESS_DATE)


def test_default_transport_opens_landing_then_both_market_posts(monkeypatch):
    calls = []

    class Response:
        def __init__(self, url, payload):
            self._url = url
            self._payload = payload
            self.headers = {"Content-Length": str(len(payload))}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def geturl(self):
            return self._url

        def read(self, amount):
            return self._payload

    class Opener:
        def open(self, request, timeout):
            calls.append((request.full_url, request.data, timeout))
            if request.data is None:
                return Response(request.full_url, b"landing")
            return Response(request.full_url, b'{"output": []}')

    monkeypatch.setattr(
        "kr_stock_wiki.collectors.krx_live.build_opener", lambda *handlers: Opener()
    )

    payloads = _default_transport(7.5)

    assert set(payloads) == set(KrxMarket)
    assert calls[0][1] is None
    assert b"mktId=STK" in calls[1][1]
    assert b"mktId=KSQ" in calls[2][1]
    assert all(call[2] == 7.5 for call in calls)


def test_transport_reader_rejects_redirect_and_oversized_response():
    class Response:
        def __init__(self, url, length, payload=b"x"):
            self._url = url
            self.headers = {} if length is None else {"Content-Length": length}
            self._payload = payload

        def geturl(self):
            return self._url

        def read(self, amount):
            return self._payload

    with pytest.raises(KrxLiveTransportError, match="untrusted URL"):
        _read(Response("https://example.com/", "1"), "https://data.krx.co.kr/a", 10)
    with pytest.raises(KrxLiveTransportError, match="invalid size"):
        _read(
            Response("https://data.krx.co.kr/a", "bad"), "https://data.krx.co.kr/a", 10
        )
    with pytest.raises(KrxLiveTransportError, match="size limit"):
        _read(
            Response("https://data.krx.co.kr/a", "11"), "https://data.krx.co.kr/a", 10
        )
    with pytest.raises(KrxLiveTransportError, match="size limit"):
        _read(
            Response("https://data.krx.co.kr/a", None, b"x" * 11),
            "https://data.krx.co.kr/a",
            10,
        )
