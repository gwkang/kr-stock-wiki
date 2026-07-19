from datetime import date, datetime
from http.client import IncompleteRead
from urllib.parse import parse_qs
from zoneinfo import ZoneInfo

import pytest

from kr_stock_wiki.collectors import kind_market_notices as kind_notice_module
from kr_stock_wiki.collectors.kind_market_notices import (
    KindMarketNotice,
    KindMarketNoticeClient,
    KindMarketNoticeEvent,
    KindMarketNoticeEventType,
    RawKindMarketNotice,
)


def test_kind_market_notice_normalizes_official_holiday_document():
    init_url = (
        "https://kind.krx.co.kr/common/disclsviewer.do?"
        "method=searchInitInfo&acptNo=20250520000110"
    )
    document_url = (
        "https://kind.krx.co.kr/external/2025/05/20/000110/20250520000087/99340.htm"
    )
    body_html = """<!doctype html><html><body>
      <h1>유가증권시장 휴장안내</h1>
      <table>
        <tr><th>휴장일자</th><td>2025년 6월 3일(화)</td></tr>
        <tr><th>휴장사유</th><td>임시공휴일 지정(대통령 선거일)</td></tr>
        <tr><th>대상시장</th><td>유가증권시장(주식, 채권, ETF, ETN, ELW 등)</td></tr>
      </table>
    </body></html>""".encode()
    raw = RawKindMarketNotice(
        acceptance_number="20250520000110",
        document_number="20250520000087",
        init_url=init_url,
        document_url=document_url,
        init_html=(
            "<option value='20250520000087|Y' selected>휴장안내</option>"
        ).encode(),
        wrapper_html=(
            f'<iframe src="/{document_url.split(".co.kr/")[1]}"></iframe>'
        ).encode(),
        body_html=body_html,
    )
    fetched_at = datetime(2026, 7, 20, 7, 0, tzinfo=ZoneInfo("Asia/Seoul"))

    notice = KindMarketNoticeClient(
        transport=lambda acceptance_number, _timeout: raw,
        clock=lambda: fetched_at,
    ).document("20250520000110")

    assert notice.acceptance_number == "20250520000110"
    assert notice.document_number == "20250520000087"
    assert notice.title == "휴장안내"
    assert notice.is_correction is False
    assert notice.document_url == document_url
    assert notice.fetched_at == fetched_at
    assert notice.init_html == raw.init_html.decode()
    assert notice.wrapper_html == raw.wrapper_html.decode()
    assert notice.body_html == body_html.decode()
    assert "유가증권시장 휴장안내" in notice.body_text
    assert len(notice.events) == 1
    assert notice.events[0].event_type is KindMarketNoticeEventType.CLOSED
    assert notice.events[0].effective_date == date(2025, 6, 3)
    assert notice.events[0].markets == ("KOSPI",)
    assert notice.structured_complete is True
    assert KindMarketNotice.from_payload(notice.to_payload()) == notice

    tampered = notice.to_payload()
    tampered["events"][0]["effective_date"] = "2025-06-04"
    with pytest.raises(ValueError):
        KindMarketNotice.from_payload(tampered)

    tampered_wrapper = notice.to_payload()
    tampered_wrapper["wrapper_html"] = tampered_wrapper["wrapper_html"].replace(
        "99340.htm", "99341.htm"
    )
    with pytest.raises(ValueError):
        KindMarketNotice.from_payload(tampered_wrapper)


def test_kind_market_notice_normalizes_session_change_effective_date():
    body_html = """<html><body>
      <h1>기타시장안내</h1>
      <p>대학수학능력시험일 출근시간 조정에 따라 유가증권시장의 거래시간이 임시 변경됨</p>
      <p>변경 전 정규시장 : 09:00~15:30</p>
      <p>변경 후 정규시장 : 10:00~16:30</p>
      <p>3. 시행일 : 2025. 11. 13 (목)</p>
    </body></html>""".encode()
    raw = RawKindMarketNotice(
        acceptance_number="20251030000102",
        document_number="20251030000137",
        init_url=(
            "https://kind.krx.co.kr/common/disclsviewer.do?"
            "method=searchInitInfo&acptNo=20251030000102"
        ),
        document_url=(
            "https://kind.krx.co.kr/external/2025/10/30/000102/20251030000137/99303.htm"
        ),
        init_html=b"<option value='20251030000137|Y' selected>notice</option>",
        wrapper_html=(
            b"<iframe src='/external/2025/10/30/000102/"
            b"20251030000137/99303.htm'></iframe>"
        ),
        body_html=body_html,
    )

    notice = KindMarketNoticeClient(
        transport=lambda _acceptance_number, _timeout: raw
    ).document("20251030000102")

    assert len(notice.events) == 1
    assert notice.events[0].event_type is KindMarketNoticeEventType.SESSION_CHANGED
    assert notice.events[0].effective_date == date(2025, 11, 13)
    assert notice.events[0].markets == ("KOSPI",)
    assert notice.structured_complete is True


def test_kind_market_notice_default_transport_uses_exact_document_chain(monkeypatch):
    acceptance_number = "20250520000110"
    document_number = "20250520000087"
    init_url = (
        "https://kind.krx.co.kr/common/disclsviewer.do?"
        "method=searchInitInfo&acptNo=20250520000110"
    )
    external_path = "/external/2025/05/20/000110/20250520000087/99340.htm"
    document_url = f"https://kind.krx.co.kr{external_path}"
    responses = [
        (
            init_url,
            b"<select><option value='20250520000087|Y' selected>notice</option></select>",
        ),
        (
            "https://kind.krx.co.kr/common/disclsviewer.do",
            f'<script>var path="{external_path}";</script>'.encode(),
        ),
        (document_url, b"<html><body>official document</body></html>"),
    ]
    requests = []

    class Response:
        headers = {"Content-Type": "text/html; charset=UTF-8"}

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
        def open(self, request, timeout):
            requests.append((request, timeout))
            url, body = responses.pop(0)
            return Response(url, body)

    monkeypatch.setattr(kind_notice_module, "build_opener", lambda *_args: Opener())

    raw = kind_notice_module._default_transport(acceptance_number, 15.0)

    assert raw.document_number == document_number
    assert raw.init_url == init_url
    assert raw.document_url == document_url
    assert [request.full_url for request, _timeout in requests] == [
        init_url,
        "https://kind.krx.co.kr/common/disclsviewer.do",
        document_url,
    ]
    assert requests[0][0].get_method() == "GET"
    assert requests[1][0].get_method() == "POST"
    assert parse_qs(requests[1][0].data.decode()) == {
        "method": ["searchContents"],
        "docNo": [document_number],
    }
    assert requests[2][0].get_method() == "GET"


def test_mixed_market_semantics_remain_unstructured():
    body_html = """<html><body>
      유가증권시장의 거래시간은 변경되며 코스닥시장은 변경없음.
      시행일 : 2025. 11. 13
    </body></html>""".encode()
    raw = RawKindMarketNotice(
        acceptance_number="20251030000102",
        document_number="20251030000137",
        init_url=(
            "https://kind.krx.co.kr/common/disclsviewer.do?"
            "method=searchInitInfo&acptNo=20251030000102"
        ),
        document_url=(
            "https://kind.krx.co.kr/external/2025/10/30/000102/20251030000137/99303.htm"
        ),
        init_html=b"<option value='20251030000137|Y' selected>notice</option>",
        wrapper_html=(
            b"<iframe src='/external/2025/10/30/000102/"
            b"20251030000137/99303.htm'></iframe>"
        ),
        body_html=body_html,
    )

    notice = KindMarketNoticeClient(
        transport=lambda _acceptance_number, _timeout: raw
    ).document("20251030000102")

    assert notice.events == ()
    assert notice.structured_complete is False


@pytest.mark.parametrize(
    "text",
    (
        "유가증권시장과 대체거래소 NXT 대상 휴장일자: 2025년 6월 3일",
        "유가증권시장과 부산시장 대상 휴장일자: 2025년 6월 3일",
        "유가증권시장과 부산 시장 대상 휴장일자: 2025년 6월 3일",
        "유가증권시장과 ＮＸＴ 대상 휴장일자: 2025년 6월 3일",
        "유가증권시장과 N.X.T 대상 휴장일자: 2025년 6월 3일",
        "유가증권시장과 N/X/T 대상 휴장일자: 2025년 6월 3일",
        "유가증권시장과 A·T·S 대상 휴장일자: 2025년 6월 3일",
        "유가증권시장과 대체 거래소 대상 휴장일자: 2025년 6월 3일",
        "유가증권시장과 넥스트트레이드 대상 휴장일자: 2025년 6월 3일",
        "유가증권시장과 NEXTRADE 대상 휴장일자: 2025년 6월 3일",
        "유가증권시장과 부산/시장 대상 휴장일자: 2025년 6월 3일",
        "유가증권시장과 부산////시장 대상 휴장일자: 2025년 6월 3일",
        "유가증권시장과 NYSE!!!!시장 대상 휴장일자: 2025년 6월 3일",
        "유가증권시장과 未知////市場 대상 휴장일자: 2025년 6월 3일",
        "유가증권시장과 NYSE/시장 대상 휴장일자: 2025년 6월 3일",
        "유가증권시장과 NYSE 시장 대상 휴장일자: 2025년 6월 3일",
        "유가증권시장과 부산市場 대상 휴장일자: 2025년 6월 3일",
        "유가증권시장 거래시간은 변경하지 않음. 시행일: 2025. 11. 13",
        "유가증권시장 거래시간 변경 안 함. 시행일: 2025. 11. 13",
        "유가증권시장 거래시간 변경 안 한다. 시행일: 2025. 11. 13",
        "유가증권시장 거래시간은 변경되지 않는다. 시행일: 2025. 11. 13",
        "유가증권시장 거래시간 변경은 하지 않음. 시행일: 2025. 11. 13",
        "유가증권시장 거래시간 변경 계획 없음. 시행일: 2025. 11. 13",
        "유가증권시장 거래시간 변경 계획은 없다. 시행일: 2025. 11. 13",
        "유가증권시장 거래시간 변경을 계획 중. 시행일: 2025. 11. 13",
        "유가증권시장 정정 공지: 거래시간 변경 시행일: 2025. 11. 13",
        "유가증권시장 향후 거래시간 변경 시행일: 2025. 11. 13",
        "유가증권시장 추후 거래시간 변경 시행일: 2025. 11. 13",
        "유가증권시장 지난해 거래시간 변경 시행일: 2025. 11. 13",
        "유가증권시장 작년 거래시간 변경 시행일: 2025. 11. 13",
        "유가증권시장 기존 거래시간 변경 시행일: 2025. 11. 13",
        "유가증권시장 거래시간 변경되는 것은 아님. 시행일: 2025. 11. 13",
        "유가증권시장 거래시간 변경 검토자료. 시행일: 2025. 11. 13",
        "유가증권시장 정정 전 휴장일자: 2025년 6월 3일 정정 후 정상 개장",
        "유가증권시장 휴장일자: 2025년 6월 3일 공지는 취소되었습니다",
        "유가증권시장 휴장일자: 2025년 6월 3일은 휴장하지 않음",
        "유가증권시장 휴장일자: 2025년 6월 3일 정상 개장",
        "유가증권시장 휴장일자: 2025년 6월 3일 정상적으로 개장",
        "유가증권시장 휴장일자: 2025년 6월 3일 정상적인 절차에 따라서 문제 없이 개장",
        "유가증권시장 휴장일자: 2025년 6월 3일 정상 운영 원칙에 따라서 문제 없이 개장",
        "유가증권시장 휴장일자: 2025년 6월 3일 전년도에는 휴장",
        "유가증권시장 휴장일자: 2025년 6월 3일 지난 해에는 휴장",
        "유가증권시장 휴장일자: 2025년 6월 3일 공고일: 2025-05-20",
        "유가증권시장 휴장일자: 2025년 6월 3일 공고일: 2025/05/20",
        (
            "유가증권시장 변경 전 휴장일자: 2025년 6월 3일 "
            "변경 후 휴장일자: 2025년 6월 4일"
        ),
        ("유가증권시장 휴장일자: 2025년 6월 3일 또는 2025년 6월 4일"),
        ("유가증권시장 거래시간 변경 시행일: 2025. 11. 13 / 2025. 11. 14"),
        ("유가증권시장 거래시간 변경 시행일: 2025. 11. 13 별도 시행일: 2025. 11. 14"),
    ),
)
def test_ambiguous_or_negative_market_events_remain_unstructured(text):
    assert kind_notice_module._events(text) == ()


@pytest.mark.parametrize(
    "unsupported_market",
    (
        "N.E.X.T.R.A.D.E",
        "N/E/X/T/R/A/D/E",
        "N-E-X-T-R-A-D-E",
        "Ｎ．Ｅ．Ｘ．Ｔ．Ｒ．Ａ．Ｄ．Ｅ",
        "N E X T R A D E",
        "未/知/市/場",
    ),
)
def test_official_holiday_template_rejects_obfuscated_unsupported_market(
    unsupported_market,
):
    text = (
        "유가증권시장 휴장안내 휴장일자: 2025년 6월 3일 "
        "휴장사유: 임시공휴일 대상시장: 유가증권시장 및 " + unsupported_market
    )
    assert kind_notice_module._events(text) == ()


def test_official_session_template_rejects_nonpositive_or_unchanged_hours():
    text = (
        "기타시장안내 유가증권시장 거래시간 변경 여부: 부정 "
        "변경 전 정규시장: 09:00~15:30 변경 후 정규시장: 09:00~15:30 "
        "시행일: 2025. 11. 13"
    )
    assert kind_notice_module._events(text) == ()


def test_correction_title_preserves_official_prior_document_versions():
    raw = RawKindMarketNotice(
        acceptance_number="20241031000170",
        document_number="20241031000407",
        init_url=(
            "https://kind.krx.co.kr/common/disclsviewer.do?"
            "method=searchInitInfo&acptNo=20241031000170"
        ),
        document_url=(
            "https://kind.krx.co.kr/external/2024/10/31/000170/20241031000407/70869.htm"
        ),
        init_html=(
            "<option value='20241030000001|N'>"
            "파생상품시장거래시간변경안내</option>"
            "<option value='20241031000407|Y' selected>"
            "[정정]파생상품시장거래시간변경안내</option>"
        ).encode(),
        wrapper_html=(
            b"<iframe src='/external/2024/10/31/000170/"
            b"20241031000407/70869.htm'></iframe>"
        ),
        body_html=(
            "<html><body>파생상품시장 거래시간 변경 시행일 : 2024. 11. 14</body></html>"
        ).encode(),
    )

    notice = KindMarketNoticeClient(
        transport=lambda _acceptance_number, _timeout: raw
    ).document("20241031000170")

    assert notice.title == "[정정]파생상품시장거래시간변경안내"
    assert notice.is_correction is True
    assert notice.prior_document_numbers == ("20241030000001",)
    assert KindMarketNotice.from_payload(notice.to_payload()) == notice
    tampered = notice.to_payload()
    tampered["prior_document_numbers"] = []
    with pytest.raises(ValueError):
        KindMarketNotice.from_payload(tampered)
    assert "replaces_acceptance_number" not in notice.to_payload()


def test_raw_kind_market_notice_rejects_noncanonical_document_url():
    with pytest.raises(ValueError):
        RawKindMarketNotice(
            acceptance_number="20250520000110",
            document_number="20250520000087",
            init_url=(
                "https://kind.krx.co.kr/common/disclsviewer.do?"
                "method=searchInitInfo&acptNo=20250520000110"
            ),
            document_url=(
                "https://kind.krx.co.kr/external/../2025/05/20/000110/"
                "20250520000087/99340.htm"
            ),
            init_html=b"official-init",
            wrapper_html=b"official-wrapper",
            body_html=b"official-body",
        )


def test_read_exact_rejects_redirect_and_oversized_or_invalid_lengths():
    class Response:
        def __init__(self, url, body=b"ok", headers=None):
            self.url = url
            self.body = body
            self.headers = {"Content-Type": "text/html"}
            self.headers.update(headers or {})

        def geturl(self):
            return self.url

        def read(self, _limit):
            return self.body

    expected = "https://kind.krx.co.kr/official"
    with pytest.raises(ValueError):
        kind_notice_module._read_exact(
            Response(
                expected,
                headers={"Content-Type": "application/json"},
            ),
            expected,
        )
    with pytest.raises(ValueError):
        kind_notice_module._read_exact(
            Response("https://evil.example/redirect"), expected
        )
    with pytest.raises(ValueError):
        kind_notice_module._read_exact(
            Response(expected, headers={"Content-Length": "invalid"}), expected
        )
    with pytest.raises(ValueError):
        kind_notice_module._read_exact(
            Response(
                expected,
                headers={"Content-Length": str(kind_notice_module._MAX_HTML_BYTES + 1)},
            ),
            expected,
        )
    with pytest.raises(ValueError):
        kind_notice_module._read_exact(
            Response(expected, body=b"x" * (kind_notice_module._MAX_HTML_BYTES + 1)),
            expected,
        )


def test_document_identity_parsers_reject_malformed_official_pages():
    with pytest.raises(ValueError):
        kind_notice_module._selected_document(b"\xff")
    with pytest.raises(ValueError):
        kind_notice_module._selected_document(b"<option>missing identity</option>")
    for malformed_value in (
        "20250519000001|X",
        "2025051900000|N",
        "20250519000001junk",
    ):
        with pytest.raises(ValueError):
            kind_notice_module._selected_document(
                b"<option value='20250520000087|Y' selected>current</option>"
                + f"<option value='{malformed_value}'>bad prior</option>".encode()
            )
    with pytest.raises(ValueError):
        kind_notice_module._selected_document(
            b"<option value='20250520000087|Y' selected>current</option>"
            b"<option value='20250519000001|N'></option>"
        )
    with pytest.raises(ValueError):
        kind_notice_module._selected_document(
            b"<option value='20250520000087|Y' selected>current</option>"
            b"<option value='20250519000001'>missing status</option>"
        )
    assert kind_notice_module._selected_document(
        "<select id='mainDoc'><option value=''>본문선택</option>".encode()
        + b"<option value='20250520000087|Y' selected>current</option>"
        b"</select><select id='attachedDoc'>"
        b"<option value='20250519000001'>attachment</option></select>"
    ) == ("20250520000087", "current")
    for empty_option in (
        "<option>본문선택</option>".encode(),
        "<option value>본문선택</option>".encode(),
        b"<option>bad</option>",
        b"<option value=''>bad</option>",
    ):
        with pytest.raises(ValueError):
            kind_notice_module._selected_document(
                b"<select id='mainDoc'>"
                b"<option value='20250520000087|Y' selected>current</option>"
                + empty_option
                + b"</select>"
            )
    with pytest.raises(ValueError):
        kind_notice_module._selected_document(
            b"<select id=mainDoc/>"
            b"<select id='attachedDoc'>"
            b"<option value='20250519000001|Y' selected>attached</option></select>"
        )
    for encoded_main_doc in (b"main&#68;oc", b"main&#x44;oc"):
        with pytest.raises(ValueError):
            kind_notice_module._selected_document(
                b"<select id='" + encoded_main_doc + b"'></select>"
                b"<option value='20250519000001|Y' selected>outside</option>"
            )
    with pytest.raises(ValueError):
        kind_notice_module._selected_document(
            b"<select id='mainDoc'>"
            b"<option value='20250520000087|Y' selected>current</option>"
            b"<select id='attachedDoc'>"
            b"<option value='20250519000001|N'>attachment</option></select>"
        )
    with pytest.raises(ValueError):
        kind_notice_module._selected_document(
            b"<select id='mainDoc'>"
            b"<option value='20250520000087|Y' selected>current"
            b"</select><select id='attachedDoc'>"
            b"<option value='junk'>attachment</option></select>"
        )
    with pytest.raises(ValueError):
        kind_notice_module._external_document_url(
            b"\xff", "20250520000110", "20250520000087"
        )
    with pytest.raises(ValueError):
        kind_notice_module._external_document_url(
            b"<html>missing path</html>",
            "20250520000110",
            "20250520000087",
        )
    with pytest.raises(ValueError):
        kind_notice_module._external_document_url(
            (b"external/2025/05/21/000110/20250520000087/99340.htm"),
            "20250520000110",
            "20250520000087",
        )
    for malformed_wrapper in (
        (
            b"<iframe src='https://evil.example/external/2025/05/20/000110/"
            b"20250520000087/99340.htm'></iframe>"
        ),
        (
            b"<iframe src='notexternal/2025/05/20/000110/"
            b"20250520000087/99340.htm'></iframe>"
        ),
        (
            b"<iframe src='/external/2025/05/20/000110/"
            b"20250520000087/99340.html'></iframe>"
        ),
    ):
        with pytest.raises(ValueError):
            kind_notice_module._external_document_url(
                malformed_wrapper,
                "20250520000110",
                "20250520000087",
            )


def test_default_transport_rejects_invalid_identity_and_sanitizes_network_error(
    monkeypatch,
):
    with pytest.raises(ValueError):
        kind_notice_module._default_transport("not-an-acceptance-number", 15.0)

    class FailingOpener:
        def open(self, _request, timeout):
            raise OSError("sensitive upstream detail")

    monkeypatch.setattr(
        kind_notice_module, "build_opener", lambda *_args: FailingOpener()
    )
    with pytest.raises(ValueError, match="request failed") as error:
        kind_notice_module._default_transport("20250520000110", 15.0)
    assert "sensitive" not in str(error.value)


def test_read_exact_sanitizes_truncated_http_response():
    expected_url = "https://kind.krx.co.kr/example"

    class TruncatedResponse:
        headers = {"Content-Type": "text/html"}

        def geturl(self):
            return expected_url

        def read(self, _limit):
            raise IncompleteRead(b"partial", 10)

    with pytest.raises(ValueError, match="response read failed") as error:
        kind_notice_module._read_exact(TruncatedResponse(), expected_url)
    assert "partial" not in str(error.value)


def test_artifact_schema_and_client_configuration_fail_closed():
    for timeout in (True, 0, -1, float("nan"), float("inf"), "15"):
        with pytest.raises(ValueError):
            KindMarketNoticeClient(timeout=timeout)

    with pytest.raises(ValueError):
        KindMarketNoticeEvent(
            event_type=KindMarketNoticeEventType.CLOSED,
            effective_date=date(2025, 6, 3),
            markets=("KOSPI", "KOSPI"),
        )
    with pytest.raises(ValueError):
        KindMarketNotice.from_payload({"schema_version": True})


def test_client_rejects_raw_identity_and_invalid_body_encoding():
    canonical = dict(
        document_number="20250520000087",
        init_url=(
            "https://kind.krx.co.kr/common/disclsviewer.do?"
            "method=searchInitInfo&acptNo=20250520000110"
        ),
        document_url=(
            "https://kind.krx.co.kr/external/2025/05/20/000110/20250520000087/99340.htm"
        ),
        init_html=(b"<option value='20250520000087|Y' selected>notice</option>"),
        wrapper_html=(
            b"<iframe src='/external/2025/05/20/000110/"
            b"20250520000087/99340.htm'></iframe>"
        ),
    )
    valid_raw = RawKindMarketNotice(
        acceptance_number="20250520000110",
        body_html=b"<html><body>official</body></html>",
        **canonical,
    )
    with pytest.raises(ValueError, match="identity mismatch"):
        KindMarketNoticeClient(
            transport=lambda _acceptance_number, _timeout: valid_raw
        ).document("20250520000111")

    invalid_body = RawKindMarketNotice(
        acceptance_number="20250520000110",
        body_html=b"\xff",
        **canonical,
    )
    with pytest.raises(ValueError, match="not valid UTF-8"):
        KindMarketNoticeClient(
            transport=lambda _acceptance_number, _timeout: invalid_body
        ).document("20250520000110")


def test_visible_text_excludes_script_content_and_unrecognized_event_is_empty():
    text = kind_notice_module._body_text(
        "<html><script>hidden 유가증권시장 휴장일자 2025년 6월 3일"
        "</script><body>visible 유가증권시장 안내</body></html>"
    )
    assert "hidden" not in text
    assert "visible" in text
    assert kind_notice_module._events(text) == ()
    with pytest.raises(ValueError):
        kind_notice_module._parse_korean_date("no effective date")
