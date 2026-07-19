import json
from datetime import date, datetime
from urllib.parse import parse_qs
from zoneinfo import ZoneInfo

import pytest

from kr_stock_wiki.collectors import market_notices as market_notices_module
from kr_stock_wiki.collectors.market_notices import (
    KrxMarketNoticeClient,
    KrxMarketNoticeResponseError,
    KrxMarketNoticeSnapshot,
    KrxMarketNoticeTransportError,
)


def _row(number: int, notice_id: str, registered: str, title: str) -> dict[str, str]:
    return {
        "CUR_PAGE": "1",
        "ROW_NUMBER": str(number),
        "TOTAL_COUNT": "3",
        "MKT_NM": "파생상품",
        "TITLE": title,
        "DEP_NM": "시장운영팀",
        "ATTACH_FILE_INFO": "",
        "REG_DT": registered,
        "CM_BBS_ID": "0000",
        "BBS_SEQ": notice_id,
        "CONTN_TP_CD": "DRV",
    }


def _payload(*rows: dict[str, str]) -> bytes:
    return json.dumps(
        {
            "controller": "noti",
            "dir": "contents/MMC/NOTI",
            "cmd": "MMCNOTI001_D1",
            "output": {"OutBlock_1": list(rows)},
        }
    ).encode()


def test_market_notice_collector_completes_pagination_and_round_trips():
    pages = {
        1: _payload(
            _row(3, "20260718000103", "2026-07-18", "거래시간 변경 안내"),
            _row(2, "20260717000102", "2026-07-17", "시장 휴장 안내"),
        ),
        2: _payload(_row(1, "20260716000101", "2026-07-16", "시장 운영 안내")),
    }
    calls: list[tuple[int, int, date, date, float]] = []

    def transport(page: int, page_size: int, begin: date, end: date, timeout: float):
        calls.append((page, page_size, begin, end, timeout))
        payload = json.loads(pages[page])
        for row in payload["output"]["OutBlock_1"]:
            row["CUR_PAGE"] = str(page)
        return json.dumps(payload).encode()

    fetched_at = datetime(2026, 7, 20, 7, 0, tzinfo=ZoneInfo("Asia/Seoul"))
    snapshot = KrxMarketNoticeClient(
        transport=transport,
        clock=lambda: fetched_at,
        page_size=2,
    ).notices(date(2026, 7, 16), date(2026, 7, 20))

    assert [call[0] for call in calls] == [1, 2]
    assert snapshot.total_count == 3
    assert snapshot.completed_pages == 2
    assert snapshot.coverage_complete is True
    assert snapshot.fetched_at == fetched_at
    assert [notice.row_number for notice in snapshot.notices] == [3, 2, 1]
    assert snapshot.notices[0].raw["TITLE"] == "거래시간 변경 안내"
    assert KrxMarketNoticeSnapshot.from_payload(snapshot.to_payload()) == snapshot

    tampered = snapshot.to_payload()
    tampered["records"][0]["raw"]["TOTAL_COUNT"] = "999"
    with pytest.raises(ValueError):
        KrxMarketNoticeSnapshot.from_payload(tampered)


def test_market_notice_collector_rejects_partial_and_conflicting_pages():
    first = _payload(
        _row(3, "20260718000103", "2026-07-18", "거래시간 변경 안내"),
        _row(2, "20260717000102", "2026-07-17", "시장 휴장 안내"),
    )
    partial = _payload()
    duplicate = _payload(_row(1, "20260717000102", "2026-07-17", "변조된 중복 공지"))

    for second in (partial, duplicate):

        def transport(page, _page_size, _begin, _end, _timeout, second=second):
            payload = json.loads(first if page == 1 else second)
            for row in payload["output"]["OutBlock_1"]:
                row["CUR_PAGE"] = str(page)
            return json.dumps(payload).encode()

        with pytest.raises(KrxMarketNoticeResponseError):
            KrxMarketNoticeClient(transport=transport, page_size=2).notices(
                date(2026, 7, 16), date(2026, 7, 20)
            )


def test_market_notice_collector_rejects_unsafe_counts_and_boolean_config():
    row = _row(100_001, "20260718000103", "2026-07-18", "시장 운영 안내")
    row["TOTAL_COUNT"] = "100001"

    def transport(page, _page_size, _begin, _end, _timeout):
        assert page == 1
        return _payload(row)

    with pytest.raises(KrxMarketNoticeResponseError, match="safety limits"):
        KrxMarketNoticeClient(transport=transport).notices(
            date(2026, 7, 16), date(2026, 7, 20)
        )
    with pytest.raises(ValueError):
        KrxMarketNoticeClient(page_size=True)
    with pytest.raises(ValueError):
        KrxMarketNoticeClient(timeout=True)
    for timeout in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError):
            KrxMarketNoticeClient(timeout=timeout)


def test_market_notice_rejects_duplicate_json_keys_and_boolean_schema_version():
    duplicate_keys = b"""{
      "controller":"noti","dir":"contents/MMC/NOTI","cmd":"MMCNOTI001_D1",
      "output":{"OutBlock_1":[{
        "CUR_PAGE":"1","ROW_NUMBER":"1",
        "TOTAL_COUNT":"999","TOTAL_COUNT":"1",
        "MKT_NM":"stock","TITLE":"evil","TITLE":"good",
        "DEP_NM":"ops","ATTACH_FILE_INFO":"","REG_DT":"2026-07-18",
        "CM_BBS_ID":"0000","BBS_SEQ":"20260718000103","CONTN_TP_CD":"DRV"
      }]}
    }"""
    with pytest.raises(KrxMarketNoticeResponseError, match="duplicate JSON key"):
        KrxMarketNoticeClient(transport=lambda *_args: duplicate_keys).notices(
            date(2026, 7, 1), date(2026, 7, 20)
        )

    row = _row(1, "20260718000103", "2026-07-18", "시장 운영 안내")
    row["TOTAL_COUNT"] = "1"
    snapshot = KrxMarketNoticeClient(transport=lambda *_args: _payload(row)).notices(
        date(2026, 7, 1), date(2026, 7, 20)
    )
    artifact = snapshot.to_payload()
    artifact["schema_version"] = True
    with pytest.raises(ValueError, match="envelope"):
        KrxMarketNoticeSnapshot.from_payload(artifact)


def test_market_notice_default_transport_uses_exact_public_contract(monkeypatch):
    requests = []

    class Response:
        headers = {}

        def __init__(self, url, body):
            self.url = url
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def geturl(self):
            return self.url

        def read(self, _limit):
            return self.body

    class Opener:
        def __init__(self, response_url):
            self.response_url = response_url

        def open(self, request, timeout):
            requests.append((request, timeout))
            return Response(self.response_url, b"official-json")

    monkeypatch.setattr(
        market_notices_module,
        "build_opener",
        lambda *_handlers: Opener(market_notices_module._NOTICE_DATA_URL),
    )
    result = market_notices_module._default_transport(
        2, 100, date(2026, 7, 1), date(2026, 7, 20), 15.0
    )

    assert result == b"official-json"
    request, timeout = requests[0]
    assert request.full_url == market_notices_module._NOTICE_DATA_URL
    assert request.method == "POST"
    assert timeout == 15.0
    assert parse_qs(request.data.decode(), keep_blank_values=True) == {
        "curPage": ["2"],
        "pageSize": ["100"],
        "mktId": ["ALL"],
        "condTp": ["2"],
        "titleContn": [""],
        "strtDd": ["20260701"],
        "endDd": ["20260720"],
        "boardId": [""],
    }

    monkeypatch.setattr(
        market_notices_module,
        "build_opener",
        lambda *_handlers: Opener(
            f"{market_notices_module._NOTICE_DATA_URL}?unexpected=1"
        ),
    )
    with pytest.raises(KrxMarketNoticeTransportError):
        market_notices_module._default_transport(
            1, 100, date(2026, 7, 1), date(2026, 7, 20), 15.0
        )


def test_market_notice_collector_rejects_malformed_transport_and_payloads():
    begin = date(2026, 7, 1)
    end = date(2026, 7, 20)

    def raises(*_args):
        raise OSError("upstream details")

    invalid_payloads = (
        "not-bytes",
        b"x" * (2 * 1024 * 1024 + 1),
        b"{",
        b"{}",
        json.dumps(
            {
                "controller": "noti",
                "dir": "contents/MMC/NOTI",
                "cmd": "MMCNOTI001_D1",
                "output": {"OutBlock_1": ["not-a-row"]},
            }
        ).encode(),
    )
    with pytest.raises(KrxMarketNoticeTransportError):
        KrxMarketNoticeClient(transport=raises).notices(begin, end)
    for payload in invalid_payloads:
        with pytest.raises(KrxMarketNoticeResponseError):
            KrxMarketNoticeClient(transport=lambda *_args, p=payload: p).notices(
                begin, end
            )

    bad_total = _row(1, "20260718000103", "2026-07-18", "시장 운영 안내")
    bad_total["TOTAL_COUNT"] = "not-an-integer"
    with pytest.raises(KrxMarketNoticeResponseError, match="total count"):
        KrxMarketNoticeClient(transport=lambda *_args: _payload(bad_total)).notices(
            begin, end
        )

    bad_date = _row(1, "20260718000103", "not-a-date", "시장 운영 안내")
    bad_date["TOTAL_COUNT"] = "1"
    with pytest.raises(KrxMarketNoticeResponseError, match="normalization"):
        KrxMarketNoticeClient(transport=lambda *_args: _payload(bad_date)).notices(
            begin, end
        )

    with pytest.raises(ValueError, match="367 days"):
        KrxMarketNoticeClient().notices(end, begin)


def test_market_notice_response_size_headers_are_fail_closed():
    class Response:
        def __init__(self, content_length, body=b"ok"):
            self.headers = {"Content-Length": content_length}
            self.body = body

        def geturl(self):
            return market_notices_module._NOTICE_DATA_URL

        def read(self, _limit):
            return self.body

    for content_length in ("invalid", "-1", str(2 * 1024 * 1024 + 1)):
        with pytest.raises(KrxMarketNoticeTransportError):
            market_notices_module._read_response(Response(content_length))
    with pytest.raises(KrxMarketNoticeTransportError):
        market_notices_module._read_response(
            Response("1", b"x" * (2 * 1024 * 1024 + 1))
        )
