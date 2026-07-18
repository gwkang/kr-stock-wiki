from datetime import date, datetime
from urllib.parse import parse_qs
from zoneinfo import ZoneInfo

from kr_stock_wiki.collectors.kind import (
    KindClient,
    KindResponseError,
)
from kr_stock_wiki.evidence import EvidenceSource, VerificationStatus


def _table(summary: str, rows: list[list[str]]) -> bytes:
    body = "".join(
        "<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows
    )
    return (
        f'<section><table summary="{summary}"><tbody>{body}</tbody></table></section>'
    ).encode()


def _identity(ticker: str = "005930", company_name: str = "삼성전자") -> bytes:
    return (
        '[{"repisusrtcd":"A'
        + ticker
        + '","repisusrtcd2":"'
        + ticker
        + '","comabbrv":"'
        + company_name
        + '","liststatcd":"Y","secugrpId":"ST"}]'
    ).encode()


def test_kind_collects_official_current_status_and_warning_periods():
    calls: list[tuple[str, dict[str, list[str]]]] = []

    def transport(url: str, body: bytes, _timeout: float) -> bytes:
        form = parse_qs(body.decode())
        calls.append((url, form))
        method = form["method"][0]
        if method == "searchCorpNameJson":
            return _identity()
        if method == "searchAdminIssueSub":
            return _table(
                "종목명, 지정일, 지정사유", [["삼성전자", "2026-07-18", "사유"]]
            )
        if method == "searchTradingHaltIssueSub":
            return _table("번호, 회사명, 지정일자, 해제일자, 사유", [])
        if form["menuIndex"] == ["2"]:
            return _table(
                "번호, 종목명, 공시일, 지정일, 해제일",
                [["1", "삼성전자", "2026-07-17", "2026-07-18", ""]],
            )
        return _table("번호, 종목명, 공시일, 지정일, 해제일", [])

    fetched_at = datetime(2026, 7, 18, 11, 30, tzinfo=ZoneInfo("Asia/Seoul"))
    records = KindClient(transport=transport, clock=lambda: fetched_at).statuses(
        ["005930"], date(2026, 7, 18)
    )

    assert len(records) == 1
    record = records[0]
    assert record.source is EvidenceSource.KIND
    assert record.verification is VerificationStatus.OFFICIAL
    assert record.ticker == "005930"
    assert record.published_date == date(2026, 7, 18)
    assert record.metrics == {
        "administrative_issue": 1,
        "trading_halt": 0,
        "investment_warning": 1,
    }
    assert len(calls) == 5
    assert calls[0][1]["searchCorpName"] == ["005930"]
    assert all(call[1]["repIsuSrtCd"] == ["005930"] for call in calls[1:])
    assert calls[3][1]["startDate"] == ["2023-07-18"]


def test_kind_warning_release_on_or_before_as_of_is_not_active():
    def transport(_url: str, body: bytes, _timeout: float) -> bytes:
        form = parse_qs(body.decode())
        if form["method"] == ["searchCorpNameJson"]:
            return _identity()
        if form["method"] == ["investattentwarnriskySub"]:
            return _table(
                "번호, 종목명, 공시일, 지정일, 해제일",
                [["1", "삼성전자", "2026-07-01", "2026-07-02", "2026-07-18"]],
            )
        summary = (
            "종목명, 지정일, 지정사유"
            if form["method"] == ["searchAdminIssueSub"]
            else "번호, 회사명, 지정일자, 해제일자, 사유"
        )
        return _table(summary, [])

    record = KindClient(
        transport=transport,
        clock=lambda: datetime(2026, 7, 18, 12, 0, tzinfo=ZoneInfo("Asia/Seoul")),
    ).statuses(["005930"], date(2026, 7, 18))[0]

    assert record.metrics["investment_warning"] == 0


def test_kind_default_transport_enforces_endpoint_and_response_size(monkeypatch):
    import pytest

    import kr_stock_wiki.collectors.kind as kind_module

    class Response:
        def __init__(
            self,
            *,
            payload: bytes = b"ok",
            final_url: str = "https://kind.krx.co.kr/investwarn/adminissue.do",
            content_length: str | None = None,
        ):
            self.payload = payload
            self.final_url = final_url
            self.headers = {}
            if content_length is not None:
                self.headers["Content-Length"] = content_length

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def geturl(self) -> str:
            return self.final_url

        def read(self, _limit: int) -> bytes:
            return self.payload

    response = Response()

    class Opener:
        def open(self, _request, timeout: float):
            assert timeout == 5
            return response

    monkeypatch.setattr(kind_module, "build_opener", lambda *_args: Opener())
    trusted = "https://kind.krx.co.kr/investwarn/adminissue.do"
    assert kind_module._default_transport(trusted, b"method=x", 5) == b"ok"

    with pytest.raises(ValueError, match="untrusted endpoint"):
        kind_module._default_transport("https://example.com/", b"", 5)

    response.final_url = "https://example.com/redirected"
    with pytest.raises(ValueError, match="untrusted endpoint"):
        kind_module._default_transport(trusted, b"", 5)

    response.final_url = trusted
    response.headers["Content-Length"] = "invalid"
    with pytest.raises(ValueError, match="invalid size"):
        kind_module._default_transport(trusted, b"", 5)

    response.headers["Content-Length"] = str(2 * 1024 * 1024 + 1)
    with pytest.raises(ValueError, match="size limit"):
        kind_module._default_transport(trusted, b"", 5)

    response.headers.clear()
    response.payload = b"x" * (2 * 1024 * 1024 + 1)
    with pytest.raises(ValueError, match="size limit"):
        kind_module._default_transport(trusted, b"", 5)


def test_kind_normalizes_official_no_result_row():
    no_result = _table("종목명, 지정일, 지정사유", [["조회된 결과값이 없습니다."]])

    def transport(_url: str, body: bytes, _timeout: float) -> bytes:
        form = parse_qs(body.decode())
        if form["method"] == ["searchCorpNameJson"]:
            return _identity()
        if form["method"] == ["searchAdminIssueSub"]:
            return no_result
        if form["method"] == ["searchTradingHaltIssueSub"]:
            return _table("번호, 회사명, 지정일자, 해제일자, 사유", [])
        return _table("번호, 종목명, 공시일, 지정일, 해제일", [])

    record = KindClient(
        transport=transport,
        clock=lambda: datetime(2026, 7, 18, 12, 0, tzinfo=ZoneInfo("Asia/Seoul")),
    ).statuses(["005930"], date(2026, 7, 18))[0]
    assert record.metrics["administrative_issue"] == 0


def test_kind_rejects_transport_encoding_warning_date_and_naive_clock():
    import pytest

    aware = datetime(2026, 7, 18, 12, 0, tzinfo=ZoneInfo("Asia/Seoul"))

    def failed_transport(_url: str, _body: bytes, _timeout: float) -> bytes:
        raise OSError("offline")

    with pytest.raises(ValueError, match="request failed"):
        KindClient(transport=failed_transport, clock=lambda: aware).statuses(
            ["005930"], date(2026, 7, 18)
        )

    def invalid_utf8(_url: str, body: bytes, _timeout: float) -> bytes:
        if parse_qs(body.decode())["method"] == ["searchCorpNameJson"]:
            return _identity()
        return b"\xff"

    with pytest.raises(KindResponseError, match="UTF-8"):
        KindClient(transport=invalid_utf8, clock=lambda: aware).statuses(
            ["005930"], date(2026, 7, 18)
        )
    with pytest.raises(ValueError, match="timezone-aware"):
        KindClient(clock=lambda: datetime(2026, 7, 18, 12, 0)).statuses(
            ["005930"], date(2026, 7, 18)
        )

    def malformed_warning(_url: str, body: bytes, _timeout: float) -> bytes:
        form = parse_qs(body.decode())
        if form["method"] == ["searchCorpNameJson"]:
            return _identity()
        if form["method"] == ["searchAdminIssueSub"]:
            return _table("종목명, 지정일, 지정사유", [])
        if form["method"] == ["searchTradingHaltIssueSub"]:
            return _table("번호, 회사명, 지정일자, 해제일자, 사유", [])
        if form["menuIndex"] == ["2"]:
            return _table(
                "번호, 종목명, 공시일, 지정일, 해제일",
                [["1", "삼성전자", "2026-07-01", "bad-date", ""]],
            )
        return _table("번호, 종목명, 공시일, 지정일, 해제일", [])

    with pytest.raises(KindResponseError, match="invalid date"):
        KindClient(transport=malformed_warning, clock=lambda: aware).statuses(
            ["005930"], date(2026, 7, 18)
        )


def test_kind_handles_february_29_three_year_window():
    calls: list[dict[str, list[str]]] = []

    def transport(_url: str, body: bytes, _timeout: float) -> bytes:
        form = parse_qs(body.decode())
        calls.append(form)
        if form["method"] == ["searchCorpNameJson"]:
            return _identity()
        if form["method"] == ["searchAdminIssueSub"]:
            return _table("종목명, 지정일, 지정사유", [])
        if form["method"] == ["searchTradingHaltIssueSub"]:
            return _table("번호, 회사명, 지정일자, 해제일자, 사유", [])
        return _table("번호, 종목명, 공시일, 지정일, 해제일", [])

    KindClient(
        transport=transport,
        clock=lambda: datetime(2028, 2, 29, 12, 0, tzinfo=ZoneInfo("Asia/Seoul")),
    ).statuses(["005930"], date(2028, 2, 29))
    assert calls[3]["startDate"] == ["2025-02-28"]


def test_kind_rejects_identity_or_status_row_for_another_company():
    import pytest

    now = datetime(2026, 7, 18, 12, 0, tzinfo=ZoneInfo("Asia/Seoul"))

    with pytest.raises(KindResponseError, match="identity"):
        KindClient(
            transport=lambda _url, _body, _timeout: _identity("000660", "SK하이닉스"),
            clock=lambda: now,
        ).statuses(["005930"], date(2026, 7, 18))

    def wrong_row(_url: str, body: bytes, _timeout: float) -> bytes:
        form = parse_qs(body.decode())
        if form["method"] == ["searchCorpNameJson"]:
            return _identity()
        if form["method"] == ["searchAdminIssueSub"]:
            return _table(
                "종목명, 지정일, 지정사유", [["삼성전자우", "2026-07-18", "사유"]]
            )
        raise AssertionError("status row mismatch must fail on first status query")

    with pytest.raises(KindResponseError, match="company does not match"):
        KindClient(transport=wrong_row, clock=lambda: now).statuses(
            ["005930"], date(2026, 7, 18)
        )


def test_kind_rejects_incomplete_or_ambiguous_html_and_oversized_payload():
    import pytest

    now = datetime(2026, 7, 18, 12, 0, tzinfo=ZoneInfo("Asia/Seoul"))
    cases = [
        (b"<html>error</html>", "expected table"),
        (_table("종목명, 지정일, 지정사유", [["a"], ["b"]]), "multiple rows"),
        (b"x" * (2 * 1024 * 1024 + 1), "size limit"),
    ]
    for payload, message in cases:

        def transport(_url: str, body: bytes, _timeout: float, value=payload) -> bytes:
            if parse_qs(body.decode())["method"] == ["searchCorpNameJson"]:
                return _identity()
            return value

        client = KindClient(
            transport=transport,
            clock=lambda: now,
        )
        with pytest.raises(KindResponseError, match=message):
            client.statuses(["005930"], date(2026, 7, 18))


def test_kind_rejects_non_current_date_duplicate_or_invalid_ticker():
    import pytest

    client = KindClient(
        transport=lambda _url, _body, _timeout: b"",
        clock=lambda: datetime(2026, 7, 18, 12, 0, tzinfo=ZoneInfo("Asia/Seoul")),
    )
    with pytest.raises(ValueError, match="current KST date"):
        client.statuses(["005930"], date(2026, 7, 17))
    with pytest.raises(ValueError, match="unique"):
        client.statuses(["005930", "005930"], date(2026, 7, 18))
    with pytest.raises(ValueError, match="ticker"):
        client.statuses(["../etc"], date(2026, 7, 18))
