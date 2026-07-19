from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from html.parser import HTMLParser
from http.client import HTTPException
from http.cookiejar import CookieJar
from math import isfinite
from typing import Callable
from unicodedata import normalize
from urllib.parse import urlencode
from urllib.request import (
    HTTPCookieProcessor,
    HTTPRedirectHandler,
    Request,
    build_opener,
)

_KIND_VIEWER_URL = "https://kind.krx.co.kr/common/disclsviewer.do"
_MAX_HTML_BYTES = 2 * 1024 * 1024


class KindMarketNoticeError(ValueError):
    pass


class KindMarketNoticeTransportError(KindMarketNoticeError):
    pass


class KindMarketNoticeEventType(str, Enum):
    CLOSED = "closed"
    SESSION_CHANGED = "session-changed"


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _read_exact(response, expected_url: str) -> bytes:
    if response.geturl() != expected_url:
        raise KindMarketNoticeTransportError(
            "KIND market notice response came from an untrusted endpoint"
        )
    content_type = response.headers.get("Content-Type")
    if (
        not isinstance(content_type, str)
        or content_type.split(";", 1)[0].strip().lower() != "text/html"
    ):
        raise KindMarketNoticeTransportError("KIND market notice response is not HTML")
    content_length = response.headers.get("Content-Length")
    if content_length is not None:
        try:
            declared = int(content_length)
        except ValueError as error:
            raise KindMarketNoticeTransportError(
                "KIND market notice response has invalid size"
            ) from error
        if declared < 0 or declared > _MAX_HTML_BYTES:
            raise KindMarketNoticeTransportError(
                "KIND market notice response exceeds size limit"
            )
    try:
        payload = response.read(_MAX_HTML_BYTES + 1)
    except HTTPException:
        raise KindMarketNoticeTransportError(
            "KIND market notice response read failed"
        ) from None
    if len(payload) > _MAX_HTML_BYTES:
        raise KindMarketNoticeTransportError(
            "KIND market notice response exceeds size limit"
        )
    return payload


class _SelectedDocumentParser(HTMLParser):
    def __init__(self, *, require_main_doc: bool = False) -> None:
        super().__init__(convert_charrefs=True)
        self.documents: list[tuple[str, str, bool, str]] = []
        self.invalid_document_option = False
        self._require_main_doc = require_main_doc
        self._in_main_doc = False
        self._seen_main_doc = False
        self._select_stack: list[str] = []
        self._active_number: str | None = None
        self._active_status: str | None = None
        self._active_selected = False
        self._active_placeholder = False
        self._active_title: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        normalized_attrs = [(name.lower(), value) for name, value in attrs]
        if tag == "select":
            id_attrs = [value for name, value in normalized_attrs if name == "id"]
            if len(id_attrs) > 1:
                self.invalid_document_option = True
            select_id = (
                id_attrs[0].lower() if len(id_attrs) == 1 and id_attrs[0] else ""
            )
            if select_id == "maindoc/":
                select_id = "maindoc"
                self.invalid_document_option = True
            if self._require_main_doc and self._in_main_doc:
                self.invalid_document_option = True
            if select_id == "maindoc":
                if self._seen_main_doc or self._select_stack:
                    self.invalid_document_option = True
                self._seen_main_doc = True
                self._in_main_doc = True
            self._select_stack.append(select_id)
            return
        if tag != "option" or (self._require_main_doc and not self._in_main_doc):
            return
        value_attrs = [value for name, value in normalized_attrs if name == "value"]
        selected_attrs = [
            value for name, value in normalized_attrs if name == "selected"
        ]
        if self._require_main_doc and (
            len(value_attrs) != 1 or value_attrs[0] is None or len(selected_attrs) > 1
        ):
            self.invalid_document_option = True
            return
        value = value_attrs[0] if value_attrs and value_attrs[0] is not None else ""
        is_selected = bool(selected_attrs)
        if self._require_main_doc and not value:
            if (
                is_selected
                or self._active_number is not None
                or self._active_placeholder
            ):
                self.invalid_document_option = True
                return
            self._active_placeholder = True
            self._active_title = []
            return
        if self._active_number is not None or self._active_placeholder:
            self.invalid_document_option = True
        if re.fullmatch(r"\d{14}\|[YN]", value):
            self._active_number, self._active_status = value.split("|", 1)
            self._active_selected = is_selected
            self._active_title = []
        elif value or is_selected:
            self.invalid_document_option = True

    def handle_data(self, data: str) -> None:
        if self._active_number is not None or self._active_placeholder:
            self._active_title.append(data)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "option" and self._active_placeholder:
            title = " ".join(" ".join(self._active_title).split())
            if title != "본문선택":
                self.invalid_document_option = True
            self._active_placeholder = False
            self._active_title = []
        elif tag == "option" and self._active_number is not None:
            title = " ".join(" ".join(self._active_title).split())
            if title and self._active_status is not None:
                self.documents.append(
                    (
                        self._active_number,
                        self._active_status,
                        self._active_selected,
                        title,
                    )
                )
            else:
                self.invalid_document_option = True
            self._active_number = None
            self._active_status = None
            self._active_selected = False
            self._active_title = []
        if tag != "select":
            return
        if not self._select_stack:
            if self._require_main_doc:
                self.invalid_document_option = True
            return
        select_id = self._select_stack.pop()
        if select_id != "maindoc":
            return
        if self._active_number is not None or self._active_placeholder:
            self.invalid_document_option = True
            self._active_number = None
            self._active_status = None
            self._active_selected = False
            self._active_placeholder = False
            self._active_title = []
        self._in_main_doc = False


def _document_lineage(init_html: bytes) -> tuple[str, str, tuple[str, ...]]:
    try:
        text = init_html.decode("utf-8", "strict")
    except UnicodeDecodeError as error:
        raise KindMarketNoticeTransportError(
            "KIND market notice init page is not valid UTF-8"
        ) from error
    detector = _SelectedDocumentParser(require_main_doc=False)
    detector.feed(text)
    detector.close()
    parser = _SelectedDocumentParser(require_main_doc=detector._seen_main_doc)
    parser.feed(text)
    parser.close()
    selected = [
        (number, title)
        for number, status, is_selected, title in parser.documents
        if status == "Y" and is_selected
    ]
    prior = [
        number
        for number, status, is_selected, _title in parser.documents
        if status == "N" and not is_selected
    ]
    identities = [number for number, _status, _selected, _title in parser.documents]
    if (
        parser.invalid_document_option
        or parser._active_number is not None
        or parser._active_placeholder
        or parser._in_main_doc
        or len(selected) != 1
        or len(identities) != len(set(identities))
        or len(parser.documents) != len(selected) + len(prior)
    ):
        raise KindMarketNoticeTransportError(
            "KIND market notice init page has invalid document identity"
        )
    return selected[0][0], selected[0][1], tuple(prior)


def _selected_document(init_html: bytes) -> tuple[str, str]:
    number, title, _prior = _document_lineage(init_html)
    return number, title


def _selected_document_number(init_html: bytes) -> str:
    return _selected_document(init_html)[0]


def _external_document_url(
    wrapper_html: bytes, acceptance_number: str, document_number: str
) -> str:
    try:
        text = wrapper_html.decode("utf-8", "strict")
    except UnicodeDecodeError as error:
        raise KindMarketNoticeTransportError(
            "KIND market notice wrapper is not valid UTF-8"
        ) from error
    pattern = re.compile(
        rf"(?P<quote>['\"])(?:https://kind\.krx\.co\.kr)?/external/"
        rf"(?P<year>\d{{4}})/(?P<month>\d{{2}})/(?P<day>\d{{2}})/"
        rf"(?P<suffix>\d{{6}})/(?P<doc>{re.escape(document_number)})/"
        rf"(?P<file>\d+)\.htm(?P=quote)"
    )
    paths = {
        (
            match.group("year"),
            match.group("month"),
            match.group("day"),
            match.group("suffix"),
            match.group("doc"),
            match.group("file"),
        )
        for match in pattern.finditer(text)
    }
    if len(paths) != 1:
        raise KindMarketNoticeTransportError(
            "KIND market notice wrapper has invalid document path"
        )
    year, month, day, suffix, doc_no, file_no = paths.pop()
    if (
        f"{year}{month}{day}" != acceptance_number[:8]
        or suffix != acceptance_number[-6:]
    ):
        raise KindMarketNoticeTransportError(
            "KIND market notice document path identity mismatch"
        )
    return (
        f"https://kind.krx.co.kr/external/{year}/{month}/{day}/"
        f"{suffix}/{doc_no}/{file_no}.htm"
    )


def _default_transport(acceptance_number: str, timeout: float) -> RawKindMarketNotice:
    if re.fullmatch(r"\d{14}", acceptance_number) is None:
        raise KindMarketNoticeTransportError(
            "KIND market notice acceptance number is invalid"
        )
    init_url = (
        f"{_KIND_VIEWER_URL}?"
        f"{urlencode({'method': 'searchInitInfo', 'acptNo': acceptance_number})}"
    )
    opener = build_opener(_NoRedirect, HTTPCookieProcessor(CookieJar()))
    headers = {"User-Agent": "kr-stock-wiki/1.0"}
    try:
        with opener.open(
            Request(init_url, headers=headers), timeout=timeout
        ) as response:
            init_html = _read_exact(response, init_url)
        document_number = _selected_document_number(init_html)
        body = urlencode({"method": "searchContents", "docNo": document_number}).encode(
            "ascii"
        )
        wrapper_request = Request(
            _KIND_VIEWER_URL,
            data=body,
            method="POST",
            headers={
                **headers,
                "Referer": init_url,
                "X-Requested-With": "XMLHttpRequest",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            },
        )
        with opener.open(wrapper_request, timeout=timeout) as response:
            wrapper_html = _read_exact(response, _KIND_VIEWER_URL)
        document_url = _external_document_url(
            wrapper_html, acceptance_number, document_number
        )
        with opener.open(
            Request(document_url, headers={**headers, "Referer": init_url}),
            timeout=timeout,
        ) as response:
            body_html = _read_exact(response, document_url)
    except (OSError, TimeoutError):
        raise KindMarketNoticeTransportError(
            "KIND market notice request failed"
        ) from None
    return RawKindMarketNotice(
        acceptance_number=acceptance_number,
        document_number=document_number,
        init_url=init_url,
        document_url=document_url,
        init_html=init_html,
        wrapper_html=wrapper_html,
        body_html=body_html,
    )


@dataclass(frozen=True)
class RawKindMarketNotice:
    acceptance_number: str
    document_number: str
    init_url: str
    document_url: str
    init_html: bytes
    wrapper_html: bytes
    body_html: bytes

    def __post_init__(self) -> None:
        expected_init_url = f"{_KIND_VIEWER_URL}?{urlencode({'method': 'searchInitInfo', 'acptNo': self.acceptance_number})}"
        expected_document_pattern = (
            rf"https://kind\.krx\.co\.kr/external/{self.acceptance_number[:4]}/"
            rf"{self.acceptance_number[4:6]}/{self.acceptance_number[6:8]}/"
            rf"{self.acceptance_number[-6:]}/{self.document_number}/\d+\.htm"
        )
        if (
            re.fullmatch(r"\d{14}", self.acceptance_number) is None
            or re.fullmatch(r"\d{14}", self.document_number) is None
            or self.init_url != expected_init_url
            or re.fullmatch(expected_document_pattern, self.document_url) is None
            or any(
                not isinstance(payload, bytes)
                or not payload
                or len(payload) > _MAX_HTML_BYTES
                for payload in (self.init_html, self.wrapper_html, self.body_html)
            )
        ):
            raise ValueError("invalid raw KIND market notice")


@dataclass(frozen=True)
class KindMarketNoticeEvent:
    event_type: KindMarketNoticeEventType
    effective_date: date
    markets: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.markets or len(set(self.markets)) != len(self.markets):
            raise ValueError("invalid KIND market notice event")

    def to_payload(self) -> dict[str, object]:
        return {
            "event_type": self.event_type.value,
            "effective_date": self.effective_date.isoformat(),
            "markets": list(self.markets),
        }


@dataclass(frozen=True)
class KindMarketNotice:
    acceptance_number: str
    document_number: str
    title: str
    prior_document_numbers: tuple[str, ...]
    init_url: str
    document_url: str
    fetched_at: datetime
    init_html: str
    wrapper_html: str
    body_html: str
    body_text: str
    events: tuple[KindMarketNoticeEvent, ...]

    def __post_init__(self) -> None:
        if (
            self.fetched_at.tzinfo is None
            or self.fetched_at.utcoffset() is None
            or not self.title
            or len(self.prior_document_numbers) != len(set(self.prior_document_numbers))
            or self.document_number in self.prior_document_numbers
            or any(
                re.fullmatch(r"\d{14}", number) is None
                for number in self.prior_document_numbers
            )
            or not self.init_html
            or not self.wrapper_html
            or not self.body_html
            or not self.body_text
        ):
            raise ValueError("invalid normalized KIND market notice")

    @property
    def is_correction(self) -> bool:
        return "정정" in self.title

    @property
    def structured_complete(self) -> bool:
        return bool(self.events) and all(event.markets for event in self.events)

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "source": "kind-market-notice-detail",
            "acceptance_number": self.acceptance_number,
            "document_number": self.document_number,
            "title": self.title,
            "is_correction": self.is_correction,
            "prior_document_numbers": list(self.prior_document_numbers),
            "init_url": self.init_url,
            "document_url": self.document_url,
            "collected_at": self.fetched_at.isoformat(),
            "structured_complete": self.structured_complete,
            "init_html": self.init_html,
            "wrapper_html": self.wrapper_html,
            "body_html": self.body_html,
            "body_text": self.body_text,
            "events": [event.to_payload() for event in self.events],
        }

    @classmethod
    def from_payload(cls, payload: object) -> KindMarketNotice:
        required = {
            "schema_version",
            "source",
            "acceptance_number",
            "document_number",
            "title",
            "is_correction",
            "prior_document_numbers",
            "init_url",
            "document_url",
            "collected_at",
            "structured_complete",
            "init_html",
            "wrapper_html",
            "body_html",
            "body_text",
            "events",
        }
        if (
            not isinstance(payload, dict)
            or set(payload) != required
            or type(payload.get("schema_version")) is not int
            or payload.get("schema_version") != 1
            or payload.get("source") != "kind-market-notice-detail"
            or not isinstance(payload.get("events"), list)
            or not isinstance(payload.get("prior_document_numbers"), list)
            or not all(
                isinstance(number, str)
                for number in payload.get("prior_document_numbers", [])
            )
            or not all(
                isinstance(payload.get(key), str)
                for key in (
                    "acceptance_number",
                    "document_number",
                    "title",
                    "init_url",
                    "document_url",
                    "collected_at",
                    "init_html",
                    "wrapper_html",
                    "body_html",
                    "body_text",
                )
            )
        ):
            raise ValueError("invalid KIND market notice artifact")
        raw = RawKindMarketNotice(
            acceptance_number=payload["acceptance_number"],
            document_number=payload["document_number"],
            init_url=payload["init_url"],
            document_url=payload["document_url"],
            init_html=payload["init_html"].encode(),
            wrapper_html=payload["wrapper_html"].encode(),
            body_html=payload["body_html"].encode(),
        )
        normalized = _normalize_raw(
            raw, datetime.fromisoformat(payload["collected_at"])
        )
        if (
            payload["title"] != normalized.title
            or payload["is_correction"] is not normalized.is_correction
            or payload["prior_document_numbers"]
            != list(normalized.prior_document_numbers)
            or payload["body_text"] != normalized.body_text
            or payload["structured_complete"] is not normalized.structured_complete
            or payload["events"] != [event.to_payload() for event in normalized.events]
        ):
            raise ValueError("KIND market notice artifact normalization mismatch")
        return normalized


class _VisibleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._hidden_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.lower() in {"style", "script", "noscript"}:
            self._hidden_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"style", "script", "noscript"} and self._hidden_depth:
            self._hidden_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._hidden_depth:
            self.parts.append(data)

    def text(self) -> str:
        return " ".join(" ".join(self.parts).split())


def _body_text(body_html: str) -> str:
    parser = _VisibleTextParser()
    parser.feed(body_html)
    parser.close()
    return parser.text()


def _parse_korean_date(value: str) -> date:
    match = re.search(
        r"(\d{4})\s*(?:년|\.)\s*(\d{1,2})\s*(?:월|\.)\s*(\d{1,2})\s*(?:일)?",
        value,
    )
    if match is None:
        raise KindMarketNoticeError(
            "KIND market notice has no parseable effective date"
        )
    return date(*(int(part) for part in match.groups()))


def _markets(text: str) -> tuple[str, ...]:
    text = normalize("NFKC", text)
    market_chars = r"A-Za-z0-9가-힣一-龥"
    separator = rf"[^{market_chars}]*"
    if re.search(
        r"(?:넥스트\s*(?:트\s*)?레이드|대체\s*(?:거래소|시장))"
        rf"|(?<![{market_chars}])N{separator}E{separator}X{separator}T"
        rf"{separator}R{separator}A{separator}D{separator}E(?![{market_chars}])"
        rf"|(?<![{market_chars}])N{separator}X{separator}T(?![{market_chars}])"
        rf"|(?<![{market_chars}])A{separator}T{separator}S(?![{market_chars}])",
        text,
        re.I,
    ):
        return ()
    mappings = (
        ("유가증권시장", "KOSPI"),
        ("코스닥시장", "KOSDAQ"),
        ("코넥스시장", "KONEX"),
        ("파생상품시장", "DERIVATIVES"),
    )
    supported_labels = {label for label, _code in mappings}
    generic_session_labels = {
        "대상시장",
        "정규시장",
        "시간외시장",
        "야간시장",
        "기타시장",
    }
    market_stem = r"[A-Za-z0-9가-힣一-龥.-]"
    market_tokens = {
        f"{stem}시장"
        for stem, _suffix in re.findall(
            rf"({market_stem}{{1,20}})[^{market_stem[1:-1]}]*(시장|市場)",
            text,
        )
    }
    punctuated_runs = re.findall(
        rf"(?<![{market_chars}])"
        rf"([{market_chars}](?:[^{market_chars}\s]+[{market_chars}])+)"
        rf"(?![{market_chars}])",
        text,
    )
    for run in punctuated_runs:
        canonical = re.sub(rf"[^{market_chars}]", "", run)
        if canonical.endswith("시장"):
            market_tokens.add(canonical)
        elif canonical.endswith("市場"):
            market_tokens.add(f"{canonical[:-2]}시장")
    if market_tokens - supported_labels - generic_session_labels:
        return ()
    return tuple(code for label, code in mappings if label in text)


def _events(text: str) -> tuple[KindMarketNoticeEvent, ...]:
    text = normalize("NFKC", text)
    ambiguous_semantics = re.search(
        r"(?:취소|철회|검토|계획|예정|향후|추후|참고|과거|지난\s*해|지난해|전년도|전년|작년|이전|기존|종전|이력|정정|미변경|부정|해당\s*없음|변경\s*여부|정상[^\n.!?]*개장)"
        r"|(?:변경|휴장)[^\n.!?]{0,30}(?:않|안\s*(?:함|한|하|했)|없|아니|아님)",
        text,
    )
    date_literals = re.findall(
        r"\d{4}\s*(?:년|[./-])\s*\d{1,2}\s*(?:월|[./-])\s*\d{1,2}\s*(?:일)?",
        text,
    )
    if ambiguous_semantics is not None or len(date_literals) != 1:
        return ()
    markets = _markets(text)
    if len(markets) != 1:
        return ()
    holidays = re.findall(
        r"휴장일자\s*[:：]?\s*(\d{4}\s*년\s*\d{1,2}\s*월\s*\d{1,2}\s*일)",
        text,
    )
    holiday_template = all(cue in text for cue in ("휴장안내", "휴장사유", "대상시장"))
    if holiday_template and len(holidays) == 1 and text.count("휴장일자") == 1:
        return (
            KindMarketNoticeEvent(
                event_type=KindMarketNoticeEventType.CLOSED,
                effective_date=_parse_korean_date(holidays[0]),
                markets=markets,
            ),
        )
    if "휴장일자" in text:
        return ()
    effective_dates = re.findall(
        r"시행일\s*[:：]?\s*(\d{4}\s*\.\s*\d{1,2}\s*\.\s*\d{1,2})",
        text,
    )
    positive_session_change = re.search(
        r"거래시간(?:이|을|은)?\s*(?:임시\s*)?변경(?!\s*(?:하지|없))", text
    )
    time_range = r"\d{1,2}:\d{2}\s*[~～-]\s*\d{1,2}:\d{2}"
    before_ranges = re.findall(rf"변경\s*전[^0-9\n]{{0,40}}({time_range})", text)
    after_ranges = re.findall(rf"변경\s*후[^0-9\n]{{0,40}}({time_range})", text)
    distinct_change = (
        len(before_ranges) == 1
        and len(after_ranges) == 1
        and re.sub(r"\s", "", before_ranges[0]).replace("～", "~")
        != re.sub(r"\s", "", after_ranges[0]).replace("～", "~")
    )
    session_template = all(
        cue in text for cue in ("기타시장안내", "변경 전", "변경 후", "시행일")
    )
    if (
        session_template
        and distinct_change
        and len(effective_dates) == 1
        and text.count("시행일") == 1
        and positive_session_change is not None
    ):
        return (
            KindMarketNoticeEvent(
                event_type=KindMarketNoticeEventType.SESSION_CHANGED,
                effective_date=_parse_korean_date(effective_dates[0]),
                markets=markets,
            ),
        )
    return ()


def _normalize_raw(raw: RawKindMarketNotice, fetched_at: datetime) -> KindMarketNotice:
    selected_number, title, prior_document_numbers = _document_lineage(raw.init_html)
    if selected_number != raw.document_number:
        raise KindMarketNoticeError("KIND market notice document identity mismatch")
    if (
        _external_document_url(
            raw.wrapper_html, raw.acceptance_number, raw.document_number
        )
        != raw.document_url
    ):
        raise KindMarketNoticeError("KIND market notice document URL mismatch")
    try:
        init_html = raw.init_html.decode("utf-8", "strict")
        wrapper_html = raw.wrapper_html.decode("utf-8", "strict")
        body_html = raw.body_html.decode("utf-8", "strict")
    except UnicodeDecodeError as error:
        raise KindMarketNoticeError(
            "KIND market notice HTML is not valid UTF-8"
        ) from error
    text = _body_text(body_html)
    return KindMarketNotice(
        acceptance_number=raw.acceptance_number,
        document_number=raw.document_number,
        title=title,
        prior_document_numbers=prior_document_numbers,
        init_url=raw.init_url,
        document_url=raw.document_url,
        fetched_at=fetched_at,
        init_html=init_html,
        wrapper_html=wrapper_html,
        body_html=body_html,
        body_text=text,
        events=_events(text),
    )


NoticeTransport = Callable[[str, float], RawKindMarketNotice]
Clock = Callable[[], datetime]


@dataclass
class KindMarketNoticeClient:
    transport: NoticeTransport = field(default=_default_transport, repr=False)
    clock: Clock = field(default=lambda: datetime.now().astimezone(), repr=False)
    timeout: float = 15.0

    def __post_init__(self) -> None:
        if (
            isinstance(self.timeout, bool)
            or not isinstance(self.timeout, (int, float))
            or not isfinite(float(self.timeout))
            or self.timeout <= 0
        ):
            raise ValueError("invalid KIND market notice client configuration")

    def document(self, acceptance_number: str) -> KindMarketNotice:
        raw = self.transport(acceptance_number, self.timeout)
        if raw.acceptance_number != acceptance_number:
            raise KindMarketNoticeError("KIND market notice identity mismatch")
        return _normalize_raw(raw, self.clock())
