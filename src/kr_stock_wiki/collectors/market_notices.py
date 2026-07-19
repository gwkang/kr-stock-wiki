from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from http.cookiejar import CookieJar
from math import ceil, isfinite
from typing import Callable
from urllib.parse import urlencode
from urllib.request import (
    HTTPCookieProcessor,
    HTTPRedirectHandler,
    Request,
    build_opener,
)

NOTICE_SOURCE_URL = "https://data.krx.co.kr/contents/MMC/NOTI/noti/MMCNOTI001.cmd"
_NOTICE_DATA_URL = "https://data.krx.co.kr/contents/MMC/NOTI/noti/MMCNOTI001_D1.cmd"
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_MAX_TOTAL_COUNT = 100_000
_MAX_PAGES = 1_000
_REQUIRED_FIELDS = {
    "CUR_PAGE",
    "ROW_NUMBER",
    "TOTAL_COUNT",
    "MKT_NM",
    "TITLE",
    "DEP_NM",
    "ATTACH_FILE_INFO",
    "REG_DT",
    "CM_BBS_ID",
    "BBS_SEQ",
    "CONTN_TP_CD",
}

NoticeTransport = Callable[[int, int, date, date, float], bytes]
Clock = Callable[[], datetime]


class KrxMarketNoticeError(ValueError):
    pass


class KrxMarketNoticeTransportError(KrxMarketNoticeError):
    pass


class KrxMarketNoticeResponseError(KrxMarketNoticeError):
    pass


def _unique_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise KrxMarketNoticeResponseError(
                f"KRX market notice response has duplicate JSON key: {key}"
            )
        result[key] = value
    return result


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _read_response(response) -> bytes:
    if response.geturl() != _NOTICE_DATA_URL:
        raise KrxMarketNoticeTransportError(
            "KRX market notice response came from an untrusted endpoint"
        )
    content_length = response.headers.get("Content-Length")
    if content_length is not None:
        try:
            declared = int(content_length)
        except ValueError as error:
            raise KrxMarketNoticeTransportError(
                "KRX market notice response has invalid size"
            ) from error
        if declared < 0 or declared > _MAX_RESPONSE_BYTES:
            raise KrxMarketNoticeTransportError(
                "KRX market notice response exceeds size limit"
            )
    payload = response.read(_MAX_RESPONSE_BYTES + 1)
    if len(payload) > _MAX_RESPONSE_BYTES:
        raise KrxMarketNoticeTransportError(
            "KRX market notice response exceeds size limit"
        )
    return payload


def _default_transport(
    page: int, page_size: int, begin: date, end: date, timeout: float
) -> bytes:
    body = urlencode(
        {
            "curPage": str(page),
            "pageSize": str(page_size),
            "mktId": "ALL",
            "condTp": "2",
            "titleContn": "",
            "strtDd": begin.strftime("%Y%m%d"),
            "endDd": end.strftime("%Y%m%d"),
            "boardId": "",
        }
    ).encode("ascii")
    request = Request(
        _NOTICE_DATA_URL,
        data=body,
        method="POST",
        headers={
            "User-Agent": "kr-stock-wiki/1.0",
            "Referer": NOTICE_SOURCE_URL,
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        },
    )
    opener = build_opener(_NoRedirect, HTTPCookieProcessor(CookieJar()))
    with opener.open(request, timeout=timeout) as response:  # nosec B310
        return _read_response(response)


@dataclass(frozen=True)
class KrxMarketNotice:
    row_number: int
    notice_id: str
    registered_date: date
    market_name: str
    title: str
    department: str
    content_type: str
    board_id: str
    attachment_info: str
    _raw: tuple[tuple[str, str], ...] = field(repr=False)

    def __post_init__(self) -> None:
        raw = dict(self._raw)
        if (
            isinstance(self.row_number, bool)
            or not isinstance(self.row_number, int)
            or self.row_number <= 0
            or not isinstance(self.notice_id, str)
            or not self.notice_id
            or not self.title
            or set(raw) < _REQUIRED_FIELDS
            or len(raw) != len(self._raw)
            or any(not isinstance(value, str) for value in raw.values())
            or raw["ROW_NUMBER"] != str(self.row_number)
            or raw["BBS_SEQ"] != self.notice_id
            or raw["REG_DT"] != self.registered_date.isoformat()
            or raw["MKT_NM"] != self.market_name
            or raw["TITLE"] != self.title
            or raw["DEP_NM"] != self.department
            or raw["CONTN_TP_CD"] != self.content_type
            or raw["CM_BBS_ID"] != self.board_id
            or raw["ATTACH_FILE_INFO"] != self.attachment_info
        ):
            raise ValueError("invalid KRX market notice")

    @property
    def raw(self) -> dict[str, str]:
        return dict(self._raw)

    def to_payload(self) -> dict[str, object]:
        return {
            "row_number": self.row_number,
            "notice_id": self.notice_id,
            "registered_date": self.registered_date.isoformat(),
            "market_name": self.market_name,
            "title": self.title,
            "department": self.department,
            "content_type": self.content_type,
            "board_id": self.board_id,
            "attachment_info": self.attachment_info,
            "raw": self.raw,
        }

    @classmethod
    def from_payload(cls, payload: object) -> KrxMarketNotice:
        if not isinstance(payload, dict) or not isinstance(payload.get("raw"), dict):
            raise ValueError("invalid KRX market notice record")
        raw = payload["raw"]
        if any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in raw.items()
        ):
            raise ValueError("invalid KRX market notice raw record")
        return cls(
            row_number=payload["row_number"],
            notice_id=payload["notice_id"],
            registered_date=date.fromisoformat(payload["registered_date"]),
            market_name=payload["market_name"],
            title=payload["title"],
            department=payload["department"],
            content_type=payload["content_type"],
            board_id=payload["board_id"],
            attachment_info=payload["attachment_info"],
            _raw=tuple(raw.items()),
        )


@dataclass(frozen=True)
class KrxMarketNoticeSnapshot:
    begin: date
    end: date
    fetched_at: datetime
    total_count: int
    completed_pages: int
    page_size: int
    notices: tuple[KrxMarketNotice, ...]
    source_url: str = NOTICE_SOURCE_URL

    def __post_init__(self) -> None:
        if self.begin > self.end or (self.end - self.begin).days > 366:
            raise ValueError("invalid KRX market notice date range")
        if self.fetched_at.tzinfo is None or self.fetched_at.utcoffset() is None:
            raise ValueError("KRX market notice fetched_at must be timezone-aware")
        if self.source_url != NOTICE_SOURCE_URL:
            raise ValueError("invalid official KRX market notice source")
        expected_pages = (
            ceil(self.total_count / self.page_size)
            if isinstance(self.total_count, int)
            and not isinstance(self.total_count, bool)
            and isinstance(self.page_size, int)
            and not isinstance(self.page_size, bool)
            and self.page_size > 0
            else 0
        )
        if (
            isinstance(self.completed_pages, bool)
            or not isinstance(self.completed_pages, int)
            or isinstance(self.page_size, bool)
            or not isinstance(self.page_size, int)
            or not 1 <= self.page_size <= 100
            or isinstance(self.total_count, bool)
            or not isinstance(self.total_count, int)
            or not 0 < self.total_count <= _MAX_TOTAL_COUNT
            or not 1 <= expected_pages <= _MAX_PAGES
            or self.completed_pages != expected_pages
            or len(self.notices) != self.total_count
            or [notice.row_number for notice in self.notices]
            != list(range(self.total_count, 0, -1))
            or len({notice.notice_id for notice in self.notices}) != self.total_count
            or any(
                notice.raw["TOTAL_COUNT"] != str(self.total_count)
                or notice.raw["CUR_PAGE"] != str(index // self.page_size + 1)
                for index, notice in enumerate(self.notices)
            )
            or any(
                not self.begin <= notice.registered_date <= self.end
                for notice in self.notices
            )
        ):
            raise ValueError("KRX market notice snapshot failed completeness checks")

    @property
    def coverage_complete(self) -> bool:
        return True

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "source": "krx-market-notices",
            "source_url": self.source_url,
            "coverage_complete": True,
            "begin": self.begin.isoformat(),
            "end": self.end.isoformat(),
            "collected_at": self.fetched_at.isoformat(),
            "total_count": self.total_count,
            "completed_pages": self.completed_pages,
            "page_size": self.page_size,
            "records": [notice.to_payload() for notice in self.notices],
        }

    @classmethod
    def from_payload(cls, payload: object) -> KrxMarketNoticeSnapshot:
        if (
            not isinstance(payload, dict)
            or type(payload.get("schema_version")) is not int
            or payload.get("schema_version") != 1
            or payload.get("source") != "krx-market-notices"
            or payload.get("coverage_complete") is not True
            or not isinstance(payload.get("records"), list)
        ):
            raise ValueError("invalid KRX market notice envelope")
        return cls(
            begin=date.fromisoformat(payload["begin"]),
            end=date.fromisoformat(payload["end"]),
            fetched_at=datetime.fromisoformat(payload["collected_at"]),
            total_count=payload["total_count"],
            completed_pages=payload["completed_pages"],
            page_size=payload["page_size"],
            notices=tuple(
                KrxMarketNotice.from_payload(row) for row in payload["records"]
            ),
            source_url=payload["source_url"],
        )


@dataclass
class KrxMarketNoticeClient:
    transport: NoticeTransport = field(default=_default_transport, repr=False)
    clock: Clock = field(default=lambda: datetime.now().astimezone(), repr=False)
    page_size: int = 100
    timeout: float = 15.0

    def __post_init__(self) -> None:
        if (
            isinstance(self.page_size, bool)
            or not isinstance(self.page_size, int)
            or not 1 <= self.page_size <= 100
            or isinstance(self.timeout, bool)
            or not isinstance(self.timeout, (int, float))
            or not isfinite(float(self.timeout))
            or self.timeout <= 0
        ):
            raise ValueError("invalid KRX market notice client configuration")

    def _page(self, page: int, begin: date, end: date) -> list[dict[str, str]]:
        try:
            raw_payload = self.transport(page, self.page_size, begin, end, self.timeout)
        except (OSError, TimeoutError):
            raise KrxMarketNoticeTransportError(
                "KRX market notice request failed"
            ) from None
        if not isinstance(raw_payload, bytes):
            raise KrxMarketNoticeResponseError(
                "KRX market notice response must be bytes"
            )
        if len(raw_payload) > _MAX_RESPONSE_BYTES:
            raise KrxMarketNoticeResponseError(
                "KRX market notice response exceeds size limit"
            )
        try:
            payload = json.loads(raw_payload, object_pairs_hook=_unique_json_object)
        except (json.JSONDecodeError, UnicodeError) as error:
            raise KrxMarketNoticeResponseError(
                "KRX market notice returned invalid JSON"
            ) from error
        if (
            not isinstance(payload, dict)
            or set(payload) != {"controller", "dir", "cmd", "output"}
            or payload.get("controller") != "noti"
            or payload.get("dir") != "contents/MMC/NOTI"
            or payload.get("cmd") != "MMCNOTI001_D1"
            or not isinstance(payload["output"], dict)
            or set(payload["output"]) != {"OutBlock_1"}
            or not isinstance(payload["output"]["OutBlock_1"], list)
        ):
            raise KrxMarketNoticeResponseError(
                "invalid KRX market notice response envelope"
            )
        rows = payload["output"]["OutBlock_1"]
        if not rows:
            raise KrxMarketNoticeResponseError("KRX market notice page is empty")
        if any(
            not isinstance(row, dict)
            or not _REQUIRED_FIELDS <= set(row)
            or any(not isinstance(value, str) for value in row.values())
            for row in rows
        ):
            raise KrxMarketNoticeResponseError("invalid KRX market notice row")
        return rows

    def notices(self, begin: date, end: date) -> KrxMarketNoticeSnapshot:
        if begin > end or (end - begin).days > 366:
            raise ValueError("KRX market notice range must be at most 367 days")
        pages: list[list[dict[str, str]]] = [self._page(1, begin, end)]
        try:
            total_count = int(pages[0][0]["TOTAL_COUNT"])
        except ValueError as error:
            raise KrxMarketNoticeResponseError(
                "KRX market notice total count is invalid"
            ) from error
        if not 0 < total_count <= _MAX_TOTAL_COUNT:
            raise KrxMarketNoticeResponseError(
                "KRX market notice total count is outside safety limits"
            )
        page_count = ceil(total_count / self.page_size)
        if page_count > _MAX_PAGES:
            raise KrxMarketNoticeResponseError(
                "KRX market notice page count is outside safety limits"
            )
        for page in range(2, page_count + 1):
            pages.append(self._page(page, begin, end))

        rows = [row for page in pages for row in page]
        expected_numbers = list(range(total_count, 0, -1))
        notices: list[KrxMarketNotice] = []
        for index, row in enumerate(rows):
            expected_page = index // self.page_size + 1
            try:
                row_number = int(row["ROW_NUMBER"])
                row_total = int(row["TOTAL_COUNT"])
                row_page = int(row["CUR_PAGE"])
                notice = KrxMarketNotice(
                    row_number=row_number,
                    notice_id=row["BBS_SEQ"],
                    registered_date=date.fromisoformat(row["REG_DT"]),
                    market_name=row["MKT_NM"],
                    title=row["TITLE"],
                    department=row["DEP_NM"],
                    content_type=row["CONTN_TP_CD"],
                    board_id=row["CM_BBS_ID"],
                    attachment_info=row["ATTACH_FILE_INFO"],
                    _raw=tuple(row.items()),
                )
            except (TypeError, ValueError) as error:
                raise KrxMarketNoticeResponseError(
                    "KRX market notice row normalization failed"
                ) from error
            if (
                row_total != total_count
                or row_page != expected_page
                or index >= len(expected_numbers)
                or row_number != expected_numbers[index]
            ):
                raise KrxMarketNoticeResponseError(
                    "KRX market notice pagination is incomplete or inconsistent"
                )
            notices.append(notice)

        try:
            return KrxMarketNoticeSnapshot(
                begin=begin,
                end=end,
                fetched_at=self.clock(),
                total_count=total_count,
                completed_pages=len(pages),
                page_size=self.page_size,
                notices=tuple(notices),
            )
        except ValueError as error:
            raise KrxMarketNoticeResponseError(
                "KRX market notice snapshot failed completeness checks"
            ) from error
