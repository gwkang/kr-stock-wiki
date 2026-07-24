import json
from datetime import date, datetime
from urllib.error import HTTPError
from zoneinfo import ZoneInfo

import pytest

from kr_stock_wiki.collectors import kis as kis_module
from kr_stock_wiki.collectors.kis import (
    KisClient,
    KisDailySnapshot,
    KisResponseError,
    _non_negative_int,
)
from kr_stock_wiki.evidence import EvidenceRecord, EvidenceSource, VerificationStatus


_KST = ZoneInfo("Asia/Seoul")


def _valid_kis_record(*, fetched_at: datetime | None = None) -> EvidenceRecord:
    observed = fetched_at or datetime(2026, 7, 22, 7, 30, tzinfo=_KST)
    return EvidenceRecord(
        source=EvidenceSource.KIS,
        evidence_id="kis:daily:20260721:005930",
        canonical_event_id="kis:daily:20260721:005930",
        kind="daily-price",
        company_name="삼성전자",
        title="KIS daily price",
        source_url=(
            "https://openapi.koreainvestment.com:9443/"
            "uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
        ),
        published_date=date(2026, 7, 21),
        fetched_at=observed,
        verification=VerificationStatus.OFFICIAL,
        ticker="005930",
        metrics={},
        raw={},
    )


def test_kis_collects_exact_watchlist_daily_snapshot_without_secret_in_provenance():
    calls: list[tuple[str, str, dict[str, str], bytes | None]] = []

    def transport(
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes | None,
        _timeout: float,
    ) -> bytes:
        calls.append((method, url, headers, body))
        if url.endswith("/oauth2/tokenP"):
            assert method == "POST"
            assert json.loads(body or b"{}") == {
                "grant_type": "client_credentials",
                "appkey": "app-key",
                "appsecret": "app-secret",
            }
            return b'{"access_token":"access-token"}'
        assert method == "GET"
        assert headers["authorization"] == "Bearer access-token"
        assert headers["appkey"] == "app-key"
        assert headers["appsecret"] == "app-secret"
        assert headers["tr_id"] == "FHKST03010100"
        assert "app-key" not in url
        assert "app-secret" not in url
        return json.dumps(
            {
                "rt_cd": "0",
                "output1": {
                    "hts_kor_isnm": "삼성전자",
                    "stck_shrn_iscd": "005930",
                    "stck_bsop_date": "20260721",
                    "hts_avls": "423000000000000",
                    "lstn_stcn": "5969782550",
                },
                "output2": [
                    {
                        "stck_bsop_date": "20260721",
                        "stck_clpr": "71000",
                        "acml_vol": "12345678",
                        "acml_tr_pbmn": "876543210000",
                    },
                    {
                        "stck_bsop_date": "20260720",
                        "stck_clpr": "70000",
                        "acml_vol": "11111111",
                        "acml_tr_pbmn": "765432100000",
                    },
                ],
            }
        ).encode()

    snapshot = KisClient(
        app_key="app-key",
        app_secret="app-secret",
        transport=transport,
        clock=lambda: datetime(2026, 7, 22, 7, 30, tzinfo=_KST),
    ).daily_snapshot(date(2026, 7, 21), {"005930": "삼성전자"})

    assert snapshot.coverage_complete is True
    assert snapshot.requested_tickers == ("005930",)
    assert snapshot.completed_tickers == ("005930",)
    assert KisDailySnapshot.from_payload(snapshot.to_payload()) == snapshot
    record = snapshot.records[0]
    assert record.source is EvidenceSource.KIS
    assert record.evidence_id == "kis:daily:20260721:005930"
    assert record.source_url == (
        "https://openapi.koreainvestment.com:9443/"
        "uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
        "?FID_COND_MRKT_DIV_CODE=J&FID_INPUT_DATE_1=20260711"
        "&FID_INPUT_DATE_2=20260721&FID_INPUT_ISCD=005930"
        "&FID_ORG_ADJ_PRC=0&FID_PERIOD_DIV_CODE=D"
    )
    assert record.published_date == date(2026, 7, 21)
    assert record.metrics == {
        "close": 71000,
        "change": 1000,
        "change_rate": 1.43,
        "volume": 12345678,
        "trading_value": 876543210000,
        "market_cap": 423000000000000,
        "listed_shares": 5969782550,
    }
    artifact = json.dumps(snapshot.to_payload(), ensure_ascii=False)
    assert "app-key" not in artifact
    assert "app-secret" not in artifact
    assert "access-token" not in artifact
    assert len(calls) == 2


@pytest.mark.parametrize(
    ("app_key", "app_secret", "timeout"),
    [
        ("", "secret", 1),
        ("key with space", "secret", 1),
        ("key", "", 1),
        ("key", "secret", 0),
    ],
)
def test_kis_client_rejects_invalid_configuration(
    app_key: str, app_secret: str, timeout: float
):
    with pytest.raises(ValueError):
        KisClient(app_key=app_key, app_secret=app_secret, timeout=timeout)


def test_kis_rejects_invalid_token_and_daily_responses():
    responses = iter(
        [
            b"{}",
            b'{"access_token":"token"}',
            b'{"rt_cd":"1"}',
        ]
    )

    def transport(*_args: object) -> bytes:
        return next(responses)

    client = KisClient(app_key="key", app_secret="secret", transport=transport)
    with pytest.raises(KisResponseError, match="access_token"):
        client.daily_snapshot(date(2026, 7, 21), {"005930": "삼성전자"})
    with pytest.raises(KisResponseError, match="API error 1"):
        client.daily_snapshot(date(2026, 7, 21), {"005930": "삼성전자"})


def test_kis_rejects_missing_exact_history_and_invalid_numbers():
    def transport(method: str, *_args: object) -> bytes:
        if method == "POST":
            return b'{"access_token":"token"}'
        return json.dumps(
            {
                "rt_cd": "0",
                "output1": {
                    "hts_kor_isnm": "삼성전자",
                    "stck_shrn_iscd": "005930",
                    "stck_bsop_date": "20260721",
                    "hts_avls": "1",
                    "lstn_stcn": "1",
                },
                "output2": [{"stck_bsop_date": "20260721", "stck_clpr": "-1"}],
            }
        ).encode()

    client = KisClient(app_key="key", app_secret="secret", transport=transport)
    with pytest.raises(KisResponseError, match="exact daily history"):
        client.daily_snapshot(date(2026, 7, 21), {"005930": "삼성전자"})


@pytest.mark.parametrize(
    "watchlist",
    [{}, {"BAD": "삼성전자"}, {"005930": ""}],
)
def test_kis_rejects_invalid_watchlist(watchlist: dict[str, str]):
    client = KisClient(
        app_key="key", app_secret="secret", transport=lambda *_args: b"{}"
    )
    with pytest.raises(ValueError):
        client.daily_snapshot(date(2026, 7, 21), watchlist)


def test_kis_snapshot_rejects_malformed_payloads():
    with pytest.raises(ValueError, match="envelope"):
        KisDailySnapshot.from_payload({})
    payload = {
        "schema_version": 1,
        "source": "kis",
        "date": "2026-07-21",
        "collected_at": "2026-07-22T07:30:00+09:00",
        "requested_tickers": "005930",
        "completed_tickers": [],
        "records": [],
    }
    with pytest.raises(ValueError, match="coverage metadata"):
        KisDailySnapshot.from_payload(payload)


@pytest.mark.parametrize("value", [True, "bad", -1])
def test_kis_rejects_invalid_non_negative_integer(value: object):
    with pytest.raises(KisResponseError, match="non-negative integer"):
        _non_negative_int(value, "value")


@pytest.mark.parametrize("response", [b"not-json", b"[]"])
def test_kis_rejects_non_object_or_invalid_json_response(response: bytes):
    client = KisClient(
        app_key="key", app_secret="secret", transport=lambda *_args: response
    )
    with pytest.raises(KisResponseError):
        client._request("GET", "https://example.test", {}, None)


@pytest.mark.parametrize(
    "error",
    [
        OSError("network"),
        HTTPError("https://example.test", 401, "unauthorized", {}, None),
    ],
)
def test_kis_redacts_transport_failures(error: Exception):
    def transport(*_args: object) -> bytes:
        raise error

    client = KisClient(app_key="key", app_secret="secret", transport=transport)
    with pytest.raises(Exception) as raised:
        client._request("GET", "https://example.test", {}, None)
    assert "key" not in str(raised.value)
    assert "secret" not in str(raised.value)


def test_kis_rejects_bad_daily_metadata():
    client = KisClient(
        app_key="key", app_secret="secret", transport=lambda *_args: b"{}"
    )
    with pytest.raises(KisResponseError, match="daily-price output"):
        client._record(
            {}, "https://example.test", date(2026, 7, 21), "005930", "삼성전자"
        )
    payload = {
        "output1": {
            "stck_bsop_date": "20260721",
            "stck_shrn_iscd": "005930",
            "hts_kor_isnm": "삼성전자",
        },
        "output2": [
            {
                "stck_bsop_date": "20260721",
                "stck_clpr": "1",
                "acml_vol": "0",
                "acml_tr_pbmn": "0",
            },
            {
                "stck_bsop_date": "20260720",
                "stck_clpr": "0",
                "acml_vol": "0",
                "acml_tr_pbmn": "0",
            },
        ],
    }
    for field, value, message in [
        ("stck_shrn_iscd", "000001", "ticker mismatch"),
        ("hts_kor_isnm", "다른회사", "company name mismatch"),
    ]:
        changed = {**payload, "output1": {**payload["output1"], field: value}}
        with pytest.raises(KisResponseError, match=message):
            client._record(
                changed, "https://example.test", date(2026, 7, 21), "005930", "삼성전자"
            )
    with pytest.raises(KisResponseError, match="previous stck_clpr"):
        client._record(
            payload, "https://example.test", date(2026, 7, 21), "005930", "삼성전자"
        )


@pytest.mark.parametrize(
    "requested,completed,fetched",
    [
        ((), (), datetime(2026, 7, 22, 7, 30, tzinfo=_KST)),
        (("005930",), (), datetime(2026, 7, 22, 7, 30, tzinfo=_KST)),
        (("005930",), ("005930",), datetime(2026, 7, 22, 7, 30)),
    ],
)
def test_kis_snapshot_rejects_invalid_core_fields(requested, completed, fetched):
    with pytest.raises(ValueError):
        KisDailySnapshot(date(2026, 7, 21), requested, completed, (), fetched)


def test_kis_snapshot_rejects_record_count_and_timestamp_mismatch():
    fetched = datetime(2026, 7, 22, 7, 30, tzinfo=_KST)
    with pytest.raises(ValueError, match="record count"):
        KisDailySnapshot(date(2026, 7, 21), ("005930",), ("005930",), (), fetched)
    record = _valid_kis_record(fetched_at=fetched)
    with pytest.raises(ValueError, match="latest record"):
        KisDailySnapshot(
            date(2026, 7, 21),
            ("005930",),
            ("005930",),
            (record,),
            datetime(2026, 7, 22, 7, 31, tzinfo=_KST),
        )
    payload = KisDailySnapshot(
        date(2026, 7, 21), ("005930",), ("005930",), (record,), fetched
    ).to_payload()
    payload["coverage_complete"] = False
    with pytest.raises(ValueError, match="coverage_complete"):
        KisDailySnapshot.from_payload(payload)


def test_kis_default_transport_reads_response(monkeypatch):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b"ok"

    monkeypatch.setattr(kis_module, "urlopen", lambda *_args, **_kwargs: Response())
    assert (
        kis_module._default_transport("GET", "https://example.test", {}, None, 1)
        == b"ok"
    )


def test_kis_snapshot_rejects_wrong_record_identity():
    record = _valid_kis_record()
    wrong = EvidenceRecord(**{**record.__dict__, "ticker": "000001"})
    with pytest.raises(ValueError, match="records must exactly match"):
        KisDailySnapshot(
            date(2026, 7, 21),
            ("005930",),
            ("005930",),
            (wrong,),
            wrong.fetched_at,
        )
    bad_source = EvidenceRecord(
        **{**record.__dict__, "source_url": "https://example.test/daily"}
    )
    with pytest.raises(ValueError, match="invalid evidence record"):
        KisDailySnapshot(
            date(2026, 7, 21),
            ("005930",),
            ("005930",),
            (bad_source,),
            bad_source.fetched_at,
        )
