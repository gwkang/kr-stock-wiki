from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from html.parser import HTMLParser
from typing import Callable
from urllib.parse import urlencode, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zoneinfo import ZoneInfo

from ..evidence import EvidenceRecord, EvidenceSource, VerificationStatus


Transport = Callable[[str, bytes, float], bytes]
Clock = Callable[[], datetime]
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_HOST = "kind.krx.co.kr"
_BASE_URL = f"https://{_HOST}"
_KST = ZoneInfo("Asia/Seoul")
_TICKER = re.compile(r"[0-9A-Z]{6}")
_ADMIN_URL = f"{_BASE_URL}/investwarn/adminissue.do"
_TRADING_URL = f"{_BASE_URL}/investwarn/tradinghaltissue.do"
_WARNING_URL = f"{_BASE_URL}/investwarn/investattentwarnrisky.do"
_CORP_URL = f"{_BASE_URL}/common/searchcorpname.do"
_SOURCE_URL = f"{_ADMIN_URL}?method=searchAdminIssueList"


class KindError(ValueError):
    pass


class KindTransportError(KindError):
    pass


class KindResponseError(KindError):
    pass


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _trusted_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme == "https" and parsed.hostname == _HOST


def _default_transport(url: str, body: bytes, timeout: float) -> bytes:
    if not _trusted_url(url):
        raise KindTransportError("KIND request blocked: untrusted endpoint")
    request = Request(
        url,
        data=body,
        method="POST",
        headers={
            "Accept": "text/html, */*;q=0.8",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "User-Agent": "kr-stock-wiki/1.0",
            "X-Requested-With": "XMLHttpRequest",
        },
    )
    opener = build_opener(_NoRedirect)
    with opener.open(request, timeout=timeout) as response:  # nosec B310
        if not _trusted_url(response.geturl()):
            raise KindTransportError("KIND response blocked: untrusted endpoint")
        content_length = response.headers.get("Content-Length")
        if content_length is not None:
            try:
                declared_size = int(content_length)
            except ValueError as error:
                raise KindTransportError("KIND response has invalid size") from error
            if declared_size > _MAX_RESPONSE_BYTES:
                raise KindTransportError("KIND response exceeds size limit")
        payload = response.read(_MAX_RESPONSE_BYTES + 1)
        if len(payload) > _MAX_RESPONSE_BYTES:
            raise KindTransportError("KIND response exceeds size limit")
        return payload


class _TableParser(HTMLParser):
    def __init__(self, expected_summary: str):
        super().__init__(convert_charrefs=True)
        self.expected_summary = expected_summary
        self.matches = 0
        self.in_table = False
        self.in_tbody = False
        self.in_row = False
        self.in_cell = False
        self.cell_parts: list[str] = []
        self.row: list[str] = []
        self.rows: list[list[str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "table" and attributes.get("summary") == self.expected_summary:
            self.matches += 1
            self.in_table = True
        elif self.in_table and tag == "tbody":
            self.in_tbody = True
        elif self.in_table and self.in_tbody and tag == "tr":
            self.in_row = True
            self.row = []
        elif self.in_row and tag in {"td", "th"}:
            self.in_cell = True
            self.cell_parts = []

    def handle_data(self, data: str) -> None:
        if self.in_cell:
            self.cell_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"td", "th"} and self.in_cell:
            self.row.append(" ".join("".join(self.cell_parts).split()))
            self.in_cell = False
        elif tag == "tr" and self.in_row:
            if self.row and any(self.row):
                self.rows.append(self.row)
            self.in_row = False
        elif tag == "tbody" and self.in_tbody:
            self.in_tbody = False
        elif tag == "table" and self.in_table:
            self.in_tbody = False
            self.in_table = False


def _parse_table(payload: bytes, expected_summary: str) -> list[list[str]]:
    if not isinstance(payload, bytes):
        raise KindResponseError("KIND transport must return bytes")
    if len(payload) > _MAX_RESPONSE_BYTES:
        raise KindResponseError("KIND response exceeds size limit")
    try:
        text = payload.decode("utf-8")
    except UnicodeError as error:
        raise KindResponseError("KIND response is not valid UTF-8") from error
    parser = _TableParser(expected_summary)
    parser.feed(text)
    parser.close()
    if parser.matches != 1:
        raise KindResponseError("KIND response is missing the expected table")
    if parser.rows == [["조회된 결과값이 없습니다."]]:
        return []
    return parser.rows


def _three_years_ago(value: date) -> date:
    try:
        return value.replace(year=value.year - 3)
    except ValueError:
        return value.replace(year=value.year - 3, day=28)


def _active_warning(rows: list[list[str]], as_of: date) -> bool:
    active = False
    for row in rows:
        if len(row) != 5:
            raise KindResponseError("KIND warning row has an invalid column count")
        try:
            designated = date.fromisoformat(row[3])
            released = date.fromisoformat(row[4]) if row[4] else None
        except ValueError as error:
            raise KindResponseError("KIND warning row has an invalid date") from error
        if released is not None and released < designated:
            raise KindResponseError("KIND warning release precedes designation")
        if designated <= as_of and (released is None or released > as_of):
            active = True
    return active


@dataclass
class KindClient:
    transport: Transport = field(default=_default_transport, repr=False)
    clock: Clock = field(default=lambda: datetime.now().astimezone(), repr=False)
    timeout: float = 15.0

    def __post_init__(self) -> None:
        if self.timeout <= 0:
            raise ValueError("timeout must be positive")

    def _request(self, url: str, fields: dict[str, str]) -> bytes:
        body = urlencode(fields).encode("ascii")
        try:
            payload = self.transport(url, body, self.timeout)
        except (OSError, TimeoutError):
            raise KindTransportError("KIND request failed") from None
        if not isinstance(payload, bytes):
            raise KindResponseError("KIND transport must return bytes")
        if len(payload) > _MAX_RESPONSE_BYTES:
            raise KindResponseError("KIND response exceeds size limit")
        return payload

    def _post(
        self,
        url: str,
        fields: dict[str, str],
        expected_summary: str,
    ) -> list[list[str]]:
        return _parse_table(self._request(url, fields), expected_summary)

    def _identity(self, ticker: str) -> tuple[str, str]:
        payload = self._request(
            _CORP_URL,
            {"method": "searchCorpNameJson", "searchCorpName": ticker},
        )
        try:
            items = json.loads(payload)
        except (json.JSONDecodeError, UnicodeError) as error:
            raise KindResponseError(
                "KIND company search returned invalid JSON"
            ) from error
        if (
            not isinstance(items, list)
            or len(items) != 1
            or not isinstance(items[0], dict)
        ):
            raise KindResponseError(
                "KIND company search must return exactly one company"
            )
        item = items[0]
        prefixed_ticker = item.get("repisusrtcd")
        short_ticker = item.get("repisusrtcd2")
        company_name = item.get("comabbrv")
        if (
            not isinstance(prefixed_ticker, str)
            or prefixed_ticker != f"A{ticker}"
            or not isinstance(short_ticker, str)
            or short_ticker != ticker
            or item.get("liststatcd") != "Y"
            or item.get("secugrpId") != "ST"
            or not isinstance(company_name, str)
            or not company_name.strip()
        ):
            raise KindResponseError(
                "KIND company identity does not match requested ticker"
            )
        return short_ticker, " ".join(company_name.split())

    @staticmethod
    def _verify_company_rows(
        rows: list[list[str]], company_name: str, *, company_column: int
    ) -> None:
        for row in rows:
            if (
                len(row) <= company_column
                or " ".join(row[company_column].split()) != company_name
            ):
                raise KindResponseError("KIND status row company does not match ticker")

    def _current_rows(
        self,
        query_ticker: str,
        company_name: str,
        *,
        trading: bool,
    ) -> list[list[str]]:
        if trading:
            url = _TRADING_URL
            method = "searchTradingHaltIssueSub"
            forward = "tradinghaltissue_sub"
            summary = "번호, 회사명, 지정일자, 해제일자, 사유"
        else:
            url = _ADMIN_URL
            method = "searchAdminIssueSub"
            forward = "adminissue_sub"
            summary = "종목명, 지정일, 지정사유"
        rows = self._post(
            url,
            {
                "method": method,
                "currentPageSize": "100",
                "pageIndex": "1",
                "searchMode": "",
                "searchCodeType": "",
                "searchCorpName": company_name,
                "forward": forward,
                "paxreq": "",
                "outsvcno": "",
                "marketType": "0" if trading else "",
                "repIsuSrtCd": query_ticker,
            },
            summary,
        )
        if len(rows) > 1:
            raise KindResponseError("KIND ticker query returned multiple rows")
        self._verify_company_rows(
            rows, company_name, company_column=1 if trading else 0
        )
        return rows

    def _warning_rows(
        self,
        query_ticker: str,
        company_name: str,
        as_of: date,
        menu_index: int,
    ) -> list[list[str]]:
        forward = "invstwarnisu_sub" if menu_index == 2 else "invstriskisu_sub"
        begin = _three_years_ago(as_of)
        rows = self._post(
            _WARNING_URL,
            {
                "method": "investattentwarnriskySub",
                "currentPageSize": "3000",
                "pageIndex": "1",
                "orderMode": "4",
                "orderStat": "D",
                "searchCodeType": "",
                "searchCorpName": company_name,
                "repIsuSrtCd": query_ticker,
                "menuIndex": str(menu_index),
                "forward": forward,
                "searchFromDate": begin.isoformat(),
                "marketType": "",
                "etsIsuSrtCd": "",
                "startDate": begin.isoformat(),
                "endDate": as_of.isoformat(),
            },
            "번호, 종목명, 공시일, 지정일, 해제일",
        )
        self._verify_company_rows(rows, company_name, company_column=1)
        return rows

    def statuses(self, tickers: list[str], as_of: date) -> list[EvidenceRecord]:
        fetched_at = self.clock()
        if fetched_at.tzinfo is None or fetched_at.utcoffset() is None:
            raise ValueError("KIND clock must return a timezone-aware datetime")
        if as_of != fetched_at.astimezone(_KST).date():
            raise ValueError("KIND status date must be the current KST date")
        if not tickers or len(tickers) > 20:
            raise ValueError("KIND status requires 1 to 20 tickers")
        if len(set(tickers)) != len(tickers):
            raise ValueError("KIND tickers must be unique")
        if not all(
            isinstance(ticker, str) and _TICKER.fullmatch(ticker) for ticker in tickers
        ):
            raise ValueError(
                "KIND ticker must be six uppercase ASCII alphanumeric characters"
            )

        records: list[EvidenceRecord] = []
        for ticker in tickers:
            query_ticker, company_name = self._identity(ticker)
            administrative_rows = self._current_rows(
                query_ticker, company_name, trading=False
            )
            trading_rows = self._current_rows(query_ticker, company_name, trading=True)
            warning_rows = self._warning_rows(query_ticker, company_name, as_of, 2)
            risky_rows = self._warning_rows(query_ticker, company_name, as_of, 3)
            metrics: dict[str, int | float | str | None] = {
                "administrative_issue": int(bool(administrative_rows)),
                "trading_halt": int(bool(trading_rows)),
                "investment_warning": int(
                    _active_warning(warning_rows, as_of)
                    or _active_warning(risky_rows, as_of)
                ),
            }
            evidence_id = f"kind:listing-risk:{as_of.isoformat()}:{ticker}"
            records.append(
                EvidenceRecord(
                    source=EvidenceSource.KIND,
                    evidence_id=evidence_id,
                    canonical_event_id=evidence_id,
                    kind="listing-risk-status",
                    company_name=company_name,
                    title=f"{ticker} KRX KIND 투자유의 상태",
                    source_url=_SOURCE_URL,
                    published_date=as_of,
                    fetched_at=fetched_at,
                    verification=VerificationStatus.OFFICIAL,
                    ticker=ticker,
                    metrics=metrics,
                    raw={
                        "administrative_rows": administrative_rows,
                        "trading_rows": trading_rows,
                        "warning_rows": warning_rows,
                        "risky_rows": risky_rows,
                        "source_urls": [
                            _SOURCE_URL,
                            f"{_TRADING_URL}?method=searchTradingHaltIssueMain",
                            f"{_WARNING_URL}?method=investattentwarnriskyMain",
                        ],
                    },
                )
            )
        return records
