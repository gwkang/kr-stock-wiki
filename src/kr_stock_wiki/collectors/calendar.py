from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from http.cookiejar import CookieJar
from typing import Callable
from urllib.parse import urlencode, urlparse
from urllib.request import (
    HTTPCookieProcessor,
    HTTPRedirectHandler,
    Request,
    build_opener,
)


CALENDAR_SOURCE_URL = (
    "https://global.krx.co.kr/contents/GLB/05/0501/0501110000/GLB0501110000.jsp"
)
_BASE_URL = "https://global.krx.co.kr"
_OTP_PATH = "/contents/COM/GenerateOTP.jspx"
_DATA_PATH = "/contents/GLB/99/GLB99000001.jspx"
_CALENDAR_BLD = "GLB/05/0501/0501110000/glb0501110000_01"
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_MAX_OTP_BYTES = 1024
MINIMUM_ANNUAL_HOLIDAYS = 10

CalendarTransport = Callable[[int, float], bytes]
Clock = Callable[[], datetime]

_WEEKDAY_CODES = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")
_WEEKDAY_NAMES = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)
_REQUIRED_FIELDS = {
    "calnd_dd",
    "calnd_dd_dy",
    "dy_tp_cd",
    "kr_dy_tp",
    "holdy_eng_nm",
}


class KrxCalendarError(ValueError):
    pass


class KrxCalendarTransportError(KrxCalendarError):
    pass


class KrxCalendarResponseError(KrxCalendarError):
    pass


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _read_official_response(response, expected_url: str, limit: int) -> bytes:
    if response.geturl() != expected_url:
        raise KrxCalendarTransportError(
            "KRX market calendar response came from an untrusted endpoint"
        )
    content_length = response.headers.get("Content-Length")
    if content_length is not None:
        try:
            declared = int(content_length)
        except ValueError as error:
            raise KrxCalendarTransportError(
                "KRX market calendar response has invalid size"
            ) from error
        if declared < 0 or declared > limit:
            raise KrxCalendarTransportError(
                "KRX market calendar response exceeds size limit"
            )
    payload = response.read(limit + 1)
    if len(payload) > limit:
        raise KrxCalendarTransportError(
            "KRX market calendar response exceeds size limit"
        )
    return payload


def _default_transport(year: int, timeout: float) -> bytes:
    opener = build_opener(_NoRedirect, HTTPCookieProcessor(CookieJar()))
    user_agent = "kr-stock-wiki/1.0"
    common_headers = {
        "User-Agent": user_agent,
        "Referer": CALENDAR_SOURCE_URL,
        "X-Requested-With": "XMLHttpRequest",
    }
    page_request = Request(CALENDAR_SOURCE_URL, headers={"User-Agent": user_agent})
    with opener.open(page_request, timeout=timeout) as response:  # nosec B310
        _read_official_response(response, CALENDAR_SOURCE_URL, _MAX_RESPONSE_BYTES)

    otp_url = (
        f"{_BASE_URL}{_OTP_PATH}?{urlencode({'name': 'form', 'bld': _CALENDAR_BLD})}"
    )
    with opener.open(  # nosec B310
        Request(otp_url, headers=common_headers), timeout=timeout
    ) as response:
        otp = (
            _read_official_response(response, otp_url, _MAX_OTP_BYTES)
            .decode("ascii")
            .strip()
        )
    if not otp or len(otp) >= _MAX_OTP_BYTES or any(char.isspace() for char in otp):
        raise KrxCalendarTransportError("KRX market calendar returned invalid OTP")

    body = urlencode(
        {
            "search_bas_yy": str(year),
            "gridTp": "KRX",
            "pagePath": urlparse(CALENDAR_SOURCE_URL).path,
            "code": otp,
        }
    ).encode("ascii")
    data_url = f"{_BASE_URL}{_DATA_PATH}"
    data_request = Request(
        data_url,
        data=body,
        method="POST",
        headers={
            **common_headers,
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        },
    )
    with opener.open(data_request, timeout=timeout) as response:  # nosec B310
        return _read_official_response(response, data_url, _MAX_RESPONSE_BYTES)


@dataclass(frozen=True)
class MarketHoliday:
    day: date
    weekday_code: str
    weekday_name: str
    reason: str
    _raw: tuple[tuple[str, str], ...] = field(default=(), repr=False)

    def __post_init__(self) -> None:
        weekday = self.day.weekday()
        if (
            self.weekday_code != _WEEKDAY_CODES[weekday]
            or self.weekday_name != _WEEKDAY_NAMES[weekday]
            or not isinstance(self.reason, str)
        ):
            raise ValueError("invalid KRX market holiday")
        canonical = {
            "calnd_dd": self.day.isoformat(),
            "calnd_dd_dy": self.day.isoformat(),
            "dy_tp_cd": self.weekday_code,
            "kr_dy_tp": self.weekday_name,
            "holdy_eng_nm": self.reason,
        }
        if not self._raw:
            object.__setattr__(self, "_raw", tuple(canonical.items()))
        elif dict(self._raw) != canonical or len(self._raw) != len(canonical):
            raise ValueError("KRX market holiday raw fields do not match normalization")

    @property
    def raw(self) -> dict[str, str]:
        return dict(self._raw)

    def to_payload(self) -> dict[str, object]:
        return {
            "date": self.day.isoformat(),
            "weekday_code": self.weekday_code,
            "weekday_name": self.weekday_name,
            "reason": self.reason,
            "raw": self.raw,
        }

    @classmethod
    def from_payload(cls, payload: object) -> MarketHoliday:
        if not isinstance(payload, dict) or not isinstance(payload.get("raw"), dict):
            raise ValueError("invalid KRX market holiday record")
        raw = payload["raw"]
        if set(raw) != _REQUIRED_FIELDS or any(
            not isinstance(value, str) for value in raw.values()
        ):
            raise ValueError("invalid KRX market holiday raw record")
        return cls(
            day=date.fromisoformat(str(payload["date"])),
            weekday_code=str(payload["weekday_code"]),
            weekday_name=str(payload["weekday_name"]),
            reason=str(payload["reason"]),
            _raw=tuple(raw.items()),
        )


@dataclass(frozen=True)
class KrxMarketCalendar:
    year: int
    holidays: tuple[MarketHoliday, ...]
    fetched_at: datetime
    source_url: str = field(default=CALENDAR_SOURCE_URL)

    def __post_init__(self) -> None:
        if self.year < 2016:
            raise ValueError("KRX market calendar year must be 2016 or later")
        if self.fetched_at.tzinfo is None or self.fetched_at.utcoffset() is None:
            raise ValueError("KRX market calendar fetched_at must be timezone-aware")
        if self.source_url != CALENDAR_SOURCE_URL:
            raise ValueError("invalid official KRX market calendar source")
        days = [holiday.day for holiday in self.holidays]
        if any(day.year != self.year for day in days):
            raise ValueError("KRX market calendar contains a different year")
        if days != sorted(days) or len(days) != len(set(days)):
            raise ValueError("KRX market holidays must be sorted and unique")
        required_anchors = {date(self.year, 1, 1), date(self.year, 12, 31)}
        if len(days) < MINIMUM_ANNUAL_HOLIDAYS or not required_anchors <= set(days):
            raise ValueError("KRX market calendar failed completeness safeguards")

    @property
    def coverage_complete(self) -> bool:
        return True

    def is_scheduled_trading_day(self, day: date) -> bool:
        """Return only the annual schedule result, not live market operating status."""
        if day.year != self.year:
            raise ValueError("date is outside KRX market calendar year")
        return day.weekday() < 5 and day not in {item.day for item in self.holidays}

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "source": "krx-market-calendar",
            "source_url": self.source_url,
            "coverage_complete": True,
            "year": self.year,
            "collected_at": self.fetched_at.isoformat(),
            "holidays": [holiday.to_payload() for holiday in self.holidays],
        }

    @classmethod
    def from_payload(cls, payload: object) -> KrxMarketCalendar:
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") != 1
            or payload.get("source") != "krx-market-calendar"
            or payload.get("coverage_complete") is not True
            or not isinstance(payload.get("holidays"), list)
        ):
            raise ValueError("invalid KRX market calendar envelope")
        return cls(
            year=payload["year"],
            holidays=tuple(
                MarketHoliday.from_payload(item) for item in payload["holidays"]
            ),
            fetched_at=datetime.fromisoformat(str(payload["collected_at"])),
            source_url=str(payload["source_url"]),
        )


@dataclass
class KrxCalendarClient:
    transport: CalendarTransport = field(default=_default_transport, repr=False)
    clock: Clock = field(default=lambda: datetime.now().astimezone(), repr=False)
    timeout: float = 15.0

    def __post_init__(self) -> None:
        if self.timeout <= 0:
            raise ValueError("timeout must be positive")

    def annual_calendar(self, year: int) -> KrxMarketCalendar:
        try:
            raw_payload = self.transport(year, self.timeout)
        except (OSError, TimeoutError):
            raise KrxCalendarTransportError(
                "KRX market calendar request failed"
            ) from None
        try:
            payload = json.loads(raw_payload)
        except (json.JSONDecodeError, UnicodeError) as error:
            raise KrxCalendarResponseError(
                "KRX market calendar returned invalid JSON"
            ) from error
        if not isinstance(payload, dict) or set(payload) != {"block1"}:
            raise KrxCalendarResponseError("invalid KRX market calendar response")
        rows = payload["block1"]
        if not isinstance(rows, list) or not rows:
            raise KrxCalendarResponseError("KRX market calendar holidays are missing")
        if len(rows) < MINIMUM_ANNUAL_HOLIDAYS:
            raise KrxCalendarResponseError(
                "KRX market calendar failed minimum annual cardinality"
            )
        holidays: list[MarketHoliday] = []
        for row in rows:
            if (
                not isinstance(row, dict)
                or set(row) != _REQUIRED_FIELDS
                or any(not isinstance(row[field], str) for field in _REQUIRED_FIELDS)
            ):
                raise KrxCalendarResponseError(
                    "KRX market calendar row is missing or has invalid required fields"
                )
            if row["calnd_dd"] != row["calnd_dd_dy"]:
                raise KrxCalendarResponseError("KRX market calendar date fields differ")
            try:
                holiday = MarketHoliday(
                    day=date.fromisoformat(row["calnd_dd"]),
                    weekday_code=row["dy_tp_cd"],
                    weekday_name=row["kr_dy_tp"],
                    reason=row["holdy_eng_nm"],
                    _raw=tuple(row.items()),
                )
            except (ValueError, TypeError) as error:
                raise KrxCalendarResponseError(
                    "KRX market calendar row is invalid"
                ) from error
            if holiday.day.year != year:
                raise KrxCalendarResponseError("KRX market calendar year mismatch")
            holidays.append(holiday)
        days = [holiday.day for holiday in holidays]
        required_anchors = {date(year, 1, 1), date(year, 12, 31)}
        if (
            days != sorted(days)
            or len(days) != len(set(days))
            or not required_anchors <= set(days)
        ):
            raise KrxCalendarResponseError(
                "KRX market calendar failed ordering, uniqueness, or anchor checks"
            )
        fetched_at = self.clock()
        return KrxMarketCalendar(year, tuple(holidays), fetched_at)
