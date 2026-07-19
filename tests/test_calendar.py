import json
from datetime import date, datetime
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import kr_stock_wiki.collectors.calendar as calendar_module
import pytest
from kr_stock_wiki.collectors.calendar import (
    KrxCalendarClient,
    KrxCalendarResponseError,
    KrxCalendarTransportError,
    KrxMarketCalendar,
)


def _official_payload(*rows: dict[str, str]) -> bytes:
    return json.dumps({"block1": list(rows)}).encode()


def _holiday(day: str, code: str, weekday: str, reason: str) -> dict[str, str]:
    return {
        "calnd_dd": day,
        "calnd_dd_dy": day,
        "dy_tp_cd": code,
        "kr_dy_tp": weekday,
        "holdy_eng_nm": reason,
    }


def _annual_rows() -> tuple[dict[str, str], ...]:
    return (
        _holiday("2026-01-01", "THU", "Thursday", "New Year's Day"),
        _holiday("2026-02-16", "MON", "Monday", "Lunar New Year"),
        _holiday("2026-02-17", "TUE", "Tuesday", "Lunar New Year"),
        _holiday("2026-02-18", "WED", "Wednesday", "Lunar New Year"),
        _holiday("2026-03-02", "MON", "Monday", "Substitution Holiday"),
        _holiday("2026-05-01", "FRI", "Friday", "Labor Day"),
        _holiday("2026-05-05", "TUE", "Tuesday", "Children's Day"),
        _holiday("2026-05-25", "MON", "Monday", "Substitution Holiday"),
        _holiday("2026-06-03", "WED", "Wednesday", "Temporary Holiday"),
        _holiday("2026-07-17", "FRI", "Friday", ""),
        _holiday("2026-12-31", "THU", "Thursday", "End of Year Holiday"),
    )


def test_calendar_normalizes_official_annual_holidays_and_round_trips():
    fetched_at = datetime(2026, 7, 20, 7, 0, tzinfo=ZoneInfo("Asia/Seoul"))
    calls: list[tuple[int, float]] = []

    def transport(year: int, timeout: float) -> bytes:
        calls.append((year, timeout))
        return _official_payload(*_annual_rows())

    snapshot = KrxCalendarClient(
        transport=transport, clock=lambda: fetched_at
    ).annual_calendar(2026)

    assert calls == [(2026, 15.0)]
    assert snapshot.year == 2026
    assert snapshot.coverage_complete is True
    assert snapshot.fetched_at == fetched_at
    assert len(snapshot.holidays) == 11
    assert snapshot.holidays[0].day == date(2026, 1, 1)
    assert snapshot.holidays[-1].day == date(2026, 12, 31)
    assert snapshot.holidays[-2].reason == ""
    assert snapshot.holidays[-2].raw == {
        "calnd_dd": "2026-07-17",
        "calnd_dd_dy": "2026-07-17",
        "dy_tp_cd": "FRI",
        "kr_dy_tp": "Friday",
        "holdy_eng_nm": "",
    }
    assert snapshot.is_scheduled_trading_day(date(2026, 7, 20)) is True
    assert snapshot.is_scheduled_trading_day(date(2026, 1, 1)) is False
    assert snapshot.is_scheduled_trading_day(date(2026, 7, 18)) is False
    assert KrxMarketCalendar.from_payload(snapshot.to_payload()) == snapshot


def test_calendar_rejects_partial_annual_holiday_response():
    payload = _official_payload(
        _holiday("2026-01-01", "THU", "Thursday", "New Year's Day"),
        _holiday("2026-12-31", "THU", "Thursday", "End of Year Holiday"),
    )
    client = KrxCalendarClient(
        transport=lambda _year, _timeout: payload,
        clock=lambda: datetime(2026, 7, 20, 7, 0, tzinfo=ZoneInfo("Asia/Seoul")),
    )

    with pytest.raises(KrxCalendarResponseError, match="minimum annual cardinality"):
        client.annual_calendar(2026)


def test_calendar_rejects_duplicate_non_string_and_missing_anchor_rows():
    base = list(_annual_rows())
    malformed_payloads = []

    duplicate = [dict(row) for row in base]
    duplicate[1] = dict(duplicate[0])
    malformed_payloads.append(_official_payload(*duplicate))

    non_string = [dict(row) for row in base]
    non_string[1]["holdy_eng_nm"] = None
    malformed_payloads.append(_official_payload(*non_string))

    missing_anchor = [dict(row) for row in base]
    missing_anchor[-1] = _holiday("2026-12-25", "FRI", "Friday", "Christmas Day")
    malformed_payloads.append(_official_payload(*missing_anchor))

    for payload in malformed_payloads:
        client = KrxCalendarClient(
            transport=lambda _year, _timeout, payload=payload: payload,
            clock=lambda: datetime(2026, 7, 20, 7, 0, tzinfo=ZoneInfo("Asia/Seoul")),
        )
        with pytest.raises(KrxCalendarResponseError):
            client.annual_calendar(2026)


def test_default_transport_uses_official_page_otp_and_json_contract(monkeypatch):
    payload = _official_payload(*_annual_rows())
    responses = [
        b"<html>official page</html>",
        b"opaque-one-time-code",
        payload,
    ]
    requests = []

    class Response:
        def __init__(self, url: str, body: bytes):
            self.url = url
            self.body = body
            self.headers = {"Content-Length": str(len(body))}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def geturl(self) -> str:
            return self.url

        def read(self, limit: int = -1) -> bytes:
            return self.body if limit < 0 else self.body[:limit]

    class Opener:
        def open(self, request, timeout: float):
            requests.append((request, timeout))
            body = responses.pop(0)
            return Response(request.full_url, body)

    monkeypatch.setattr(calendar_module, "build_opener", lambda *_handlers: Opener())
    fetched_at = datetime(2026, 7, 20, 7, 0, tzinfo=ZoneInfo("Asia/Seoul"))

    snapshot = KrxCalendarClient(clock=lambda: fetched_at).annual_calendar(2026)

    assert len(snapshot.holidays) == 11
    assert [timeout for _request, timeout in requests] == [15.0, 15.0, 15.0]
    assert requests[0][0].full_url == calendar_module.CALENDAR_SOURCE_URL
    otp_query = parse_qs(urlparse(requests[1][0].full_url).query)
    assert otp_query == {
        "name": ["form"],
        "bld": ["GLB/05/0501/0501110000/glb0501110000_01"],
    }
    form = parse_qs(requests[2][0].data.decode())
    assert form == {
        "search_bas_yy": ["2026"],
        "gridTp": ["KRX"],
        "pagePath": [urlparse(calendar_module.CALENDAR_SOURCE_URL).path],
        "code": ["opaque-one-time-code"],
    }
    assert "opaque-one-time-code" not in snapshot.source_url


def test_default_transport_rejects_redirect_oversize_and_invalid_otp(monkeypatch):
    class Response:
        def __init__(self, url: str, body: bytes, declared: str | None = None):
            self.url = url
            self.body = body
            self.headers = {
                "Content-Length": declared if declared is not None else str(len(body))
            }

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def geturl(self) -> str:
            return self.url

        def read(self, limit: int = -1) -> bytes:
            return self.body if limit < 0 else self.body[:limit]

    class Opener:
        def __init__(self, responses):
            self.responses = list(responses)

        def open(self, _request, timeout: float):
            assert timeout == 15.0
            return self.responses.pop(0)

    cases = (
        [Response("https://example.com/redirect", b"redirected")],
        [Response(f"{calendar_module.CALENDAR_SOURCE_URL}?unexpected=1", b"page")],
        [Response(calendar_module.CALENDAR_SOURCE_URL, b"x", str(3 * 1024 * 1024))],
        [
            Response(calendar_module.CALENDAR_SOURCE_URL, b"page"),
            Response(
                "https://global.krx.co.kr/contents/COM/GenerateOTP.jspx?"
                "name=form&bld=GLB%2F05%2F0501%2F0501110000%2Fglb0501110000_01",
                b"invalid otp",
            ),
        ],
    )
    for responses in cases:
        monkeypatch.setattr(
            calendar_module,
            "build_opener",
            lambda *_handlers, responses=responses: Opener(responses),
        )
        with pytest.raises(KrxCalendarTransportError):
            KrxCalendarClient().annual_calendar(2026)


def test_calendar_sanitizes_transport_and_malformed_response_failures():
    def failed_transport(_year: int, _timeout: float) -> bytes:
        raise OSError("network detail")

    with pytest.raises(KrxCalendarTransportError, match="request failed"):
        KrxCalendarClient(transport=failed_transport).annual_calendar(2026)

    malformed_payloads = (
        b"<html>access denied</html>",
        json.dumps([]).encode(),
        json.dumps({"block1": []}).encode(),
    )
    for payload in malformed_payloads:
        with pytest.raises(KrxCalendarResponseError):
            KrxCalendarClient(
                transport=lambda _year, _timeout, payload=payload: payload
            ).annual_calendar(2026)

    mismatched = [dict(row) for row in _annual_rows()]
    mismatched[1]["calnd_dd_dy"] = "2026-02-17"
    with pytest.raises(KrxCalendarResponseError, match="date fields differ"):
        KrxCalendarClient(
            transport=lambda _year, _timeout: _official_payload(*mismatched)
        ).annual_calendar(2026)


def test_calendar_artifact_rejects_non_exact_source_url():
    fetched_at = datetime(2026, 7, 20, 7, 0, tzinfo=ZoneInfo("Asia/Seoul"))
    snapshot = KrxCalendarClient(
        transport=lambda _year, _timeout: _official_payload(*_annual_rows()),
        clock=lambda: fetched_at,
    ).annual_calendar(2026)
    payload = snapshot.to_payload()
    payload["source_url"] = (
        "https://user:secret@global.krx.co.kr:444"
        "/contents/GLB/05/0501/0501110000/GLB0501110000.jsp"
    )

    with pytest.raises(ValueError, match="official KRX"):
        KrxMarketCalendar.from_payload(payload)


def test_calendar_artifact_rejects_raw_normalization_tampering():
    fetched_at = datetime(2026, 7, 20, 7, 0, tzinfo=ZoneInfo("Asia/Seoul"))
    snapshot = KrxCalendarClient(
        transport=lambda _year, _timeout: _official_payload(*_annual_rows()),
        clock=lambda: fetched_at,
    ).annual_calendar(2026)
    payload = snapshot.to_payload()
    payload["holidays"][0]["raw"]["holdy_eng_nm"] = "tampered"

    with pytest.raises(ValueError, match="raw fields"):
        KrxMarketCalendar.from_payload(payload)
