from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from http.cookiejar import CookieJar
from typing import Callable
from urllib.parse import urlencode, urlparse
from urllib.request import (
    HTTPCookieProcessor,
    HTTPRedirectHandler,
    Request,
    build_opener,
)
from zoneinfo import ZoneInfo

from .krx import KrxMarket

KST = ZoneInfo("Asia/Seoul")
SOURCE_URL = "https://data.krx.co.kr/contents/MDC/MAIN/main/index.cmd"
_BASE_URL = "https://data.krx.co.kr"
_DATA_URL = f"{_BASE_URL}/comm/bldAttendant/getJsonData.cmd"
_BLD = "dbms/MDC/MAIN/MDCMAIN00103"
_LANDING_MAX_BYTES = 2 * 1024 * 1024
_DATA_MAX_BYTES = 512 * 1024
_EXPECTED_FIELDS = {
    "TRD_DD",
    "DD_TP",
    "INVST_TP",
    "ACC_BID_TRDVAL",
    "ACC_ASK_TRDVAL",
    "NETBID_TRDVAL",
}
_EXPECTED_INVESTORS = {"기관(십억원)", "외국인(십억원)", "개인(십억원)"}
_MARKET_IDS = {KrxMarket.KOSPI: "STK", KrxMarket.KOSDAQ: "KSQ"}
_DATETIME_PATTERN = re.compile(
    r"^(\d{4})\.(\d{2})\.(\d{2}) (AM|PM) (\d{2}):(\d{2}):(\d{2})$"
)

LiveTransport = Callable[[float], dict[KrxMarket, bytes]]
Clock = Callable[[], datetime]


class KrxLiveError(ValueError):
    pass


class KrxLiveTransportError(KrxLiveError):
    pass


class KrxLiveResponseError(KrxLiveError):
    pass


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _trusted(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme == "https" and parsed.hostname == "data.krx.co.kr"


def _read(response, expected_url: str, maximum_bytes: int) -> bytes:
    if response.geturl() != expected_url or not _trusted(response.geturl()):
        raise KrxLiveTransportError("KRX live response came from an untrusted URL")
    declared = response.headers.get("Content-Length")
    if declared is not None:
        try:
            size = int(declared)
        except ValueError as error:
            raise KrxLiveTransportError("KRX live response has invalid size") from error
        if size < 0 or size > maximum_bytes:
            raise KrxLiveTransportError("KRX live response exceeds size limit")
    payload = response.read(maximum_bytes + 1)
    if len(payload) > maximum_bytes:
        raise KrxLiveTransportError("KRX live response exceeds size limit")
    return payload


def _default_transport(timeout: float) -> dict[KrxMarket, bytes]:
    opener = build_opener(_NoRedirect, HTTPCookieProcessor(CookieJar()))
    user_agent = "kr-stock-wiki/1.0"
    with opener.open(  # nosec B310
        Request(SOURCE_URL, headers={"User-Agent": user_agent}), timeout=timeout
    ) as response:
        _read(response, SOURCE_URL, _LANDING_MAX_BYTES)
    common_headers = {
        "User-Agent": user_agent,
        "Referer": SOURCE_URL,
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Accept": "application/json, text/javascript, */*; q=0.01",
    }
    result: dict[KrxMarket, bytes] = {}
    for market, market_id in _MARKET_IDS.items():
        body = urlencode({"bld": _BLD, "mktId": market_id}).encode("ascii")
        request = Request(
            _DATA_URL,
            data=body,
            headers=common_headers,
            method="POST",
        )
        with opener.open(request, timeout=timeout) as response:  # nosec B310
            result[market] = _read(response, _DATA_URL, _DATA_MAX_BYTES)
    return result


def _parse_source_time(value: object) -> datetime:
    if not isinstance(value, str):
        raise KrxLiveResponseError("KRX live source datetime is missing")
    match = _DATETIME_PATTERN.fullmatch(value)
    if match is None:
        raise KrxLiveResponseError("KRX live source datetime is invalid")
    year, month, day, period, hour, minute, second = match.groups()
    hour_number = int(hour)
    if not 1 <= hour_number <= 12:
        raise KrxLiveResponseError("KRX live source datetime is invalid")
    if period == "AM":
        hour_number %= 12
    else:
        hour_number = hour_number % 12 + 12
    try:
        return datetime(
            int(year),
            int(month),
            int(day),
            hour_number,
            int(minute),
            int(second),
            tzinfo=KST,
        )
    except ValueError as error:
        raise KrxLiveResponseError("KRX live source datetime is invalid") from error


def _integer(value: object, field_name: str) -> int:
    if isinstance(value, bool):
        raise KrxLiveResponseError(f"KRX live {field_name} must be an integer")
    try:
        return int(str(value).replace(",", ""))
    except (TypeError, ValueError) as error:
        raise KrxLiveResponseError(
            f"KRX live {field_name} must be an integer"
        ) from error


@dataclass(frozen=True)
class KrxLiveMarketActivity:
    market: KrxMarket
    source_as_of: datetime
    trading_value: int
    raw_rows: tuple[tuple[tuple[str, str], ...], ...]

    def __post_init__(self) -> None:
        if not isinstance(self.market, KrxMarket):
            raise ValueError("invalid KRX live market")
        if (
            not isinstance(self.source_as_of, datetime)
            or self.source_as_of.tzinfo is None
            or self.source_as_of.utcoffset() is None
        ):
            raise ValueError("KRX live market source_as_of must include timezone")
        if (
            isinstance(self.trading_value, bool)
            or not isinstance(self.trading_value, int)
            or self.trading_value <= 0
            or not isinstance(self.raw_rows, tuple)
            or len(self.raw_rows) != len(_EXPECTED_INVESTORS)
        ):
            raise ValueError("invalid KRX live market activity")
        expected_date = self.source_as_of.astimezone(KST).strftime("%Y%m%d")
        investors: set[str] = set()
        computed_total = 0
        for frozen_row in self.raw_rows:
            if not isinstance(frozen_row, tuple):
                raise ValueError("invalid KRX live investor row")
            row = dict(frozen_row)
            if (
                len(row) != len(frozen_row)
                or set(row) != _EXPECTED_FIELDS
                or any(not isinstance(value, str) for value in row.values())
                or row["TRD_DD"] != expected_date
                or row["DD_TP"] != "T_DD"
            ):
                raise ValueError("invalid KRX live investor row")
            investors.add(row["INVST_TP"])
            bid = _integer(row["ACC_BID_TRDVAL"], "bid trading value")
            ask = _integer(row["ACC_ASK_TRDVAL"], "ask trading value")
            _integer(row["NETBID_TRDVAL"], "net trading value")
            if bid < 0 or ask < 0:
                raise ValueError("invalid KRX live trading value")
            computed_total += bid + ask
        if investors != _EXPECTED_INVESTORS or computed_total != self.trading_value:
            raise ValueError("invalid KRX live activity totals")

    @property
    def raw(self) -> list[dict[str, str]]:
        return [dict(row) for row in self.raw_rows]


@dataclass(frozen=True)
class KrxLiveActivitySnapshot:
    business_date: date
    source_as_of: datetime
    fetched_at: datetime
    activities: tuple[KrxLiveMarketActivity, ...]
    source_url: str = SOURCE_URL

    def __post_init__(self) -> None:
        if self.source_url != SOURCE_URL:
            raise ValueError("invalid KRX live source URL")
        if (
            not isinstance(self.business_date, date)
            or isinstance(self.business_date, datetime)
            or any(
                not isinstance(value, datetime)
                or value.tzinfo is None
                or value.utcoffset() is None
                for value in (self.source_as_of, self.fetched_at)
            )
            or not isinstance(self.activities, tuple)
            or tuple(item.market for item in self.activities)
            != (KrxMarket.KOSPI, KrxMarket.KOSDAQ)
        ):
            raise ValueError("invalid KRX live snapshot")
        source_times = [item.source_as_of for item in self.activities]
        if (
            any(
                value.astimezone(KST).date() != self.business_date
                or value.astimezone(KST).time() < time(9, 0)
                for value in source_times
            )
            or max(source_times) - min(source_times) > timedelta(minutes=2)
            or self.source_as_of != max(source_times)
            or self.source_as_of > self.fetched_at
            or self.fetched_at - min(source_times) > timedelta(minutes=5)
        ):
            raise ValueError("invalid KRX live timestamp lineage")

    @property
    def markets(self) -> tuple[KrxMarket, ...]:
        return tuple(item.market for item in self.activities)

    @property
    def total_trading_value(self) -> int:
        return sum(item.trading_value for item in self.activities)

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "source": "krx-live-market-activity",
            "source_url": self.source_url,
            "business_date": self.business_date.isoformat(),
            "source_as_of": self.source_as_of.isoformat(),
            "collected_at": self.fetched_at.isoformat(),
            "markets": [
                {
                    "market": item.market.value,
                    "source_as_of": item.source_as_of.isoformat(),
                    "trading_value": item.trading_value,
                    "raw": item.raw,
                }
                for item in self.activities
            ],
        }

    @classmethod
    def from_payload(cls, payload: object) -> KrxLiveActivitySnapshot:
        expected_fields = {
            "schema_version",
            "source",
            "source_url",
            "business_date",
            "source_as_of",
            "collected_at",
            "markets",
        }
        if (
            not isinstance(payload, dict)
            or set(payload) != expected_fields
            or payload.get("schema_version") != 1
            or payload.get("source") != "krx-live-market-activity"
            or payload.get("source_url") != SOURCE_URL
            or not isinstance(payload.get("markets"), list)
        ):
            raise ValueError("invalid KRX live snapshot envelope")
        if any(
            not isinstance(payload.get(field), str)
            for field in ("business_date", "source_as_of", "collected_at")
        ):
            raise ValueError("invalid KRX live snapshot timestamp")
        try:
            business_date = date.fromisoformat(payload["business_date"])
            source_as_of = datetime.fromisoformat(payload["source_as_of"])
            fetched_at = datetime.fromisoformat(payload["collected_at"])
        except (ValueError, TypeError) as error:
            raise ValueError("invalid KRX live snapshot timestamp") from error
        if any(
            value.tzinfo is None or value.utcoffset() is None
            for value in (source_as_of, fetched_at)
        ):
            raise ValueError("KRX live snapshot timestamps must include timezone")
        activities: list[KrxLiveMarketActivity] = []
        for raw_activity in payload["markets"]:
            if (
                not isinstance(raw_activity, dict)
                or set(raw_activity)
                != {"market", "source_as_of", "trading_value", "raw"}
                or not isinstance(raw_activity.get("market"), str)
                or not isinstance(raw_activity.get("source_as_of"), str)
                or isinstance(raw_activity.get("trading_value"), bool)
                or not isinstance(raw_activity.get("trading_value"), int)
                or raw_activity["trading_value"] <= 0
                or not isinstance(raw_activity.get("raw"), list)
            ):
                raise ValueError("invalid KRX live market activity")
            try:
                market = KrxMarket(raw_activity["market"])
                activity_source_as_of = datetime.fromisoformat(
                    raw_activity["source_as_of"]
                )
            except ValueError:
                raise ValueError("invalid KRX live market") from None
            if (
                activity_source_as_of.tzinfo is None
                or activity_source_as_of.utcoffset() is None
            ):
                raise ValueError("invalid KRX live market timestamp")
            rows = raw_activity["raw"]
            if len(rows) != len(_EXPECTED_INVESTORS):
                raise ValueError("invalid KRX live investor categories")
            investors: set[str] = set()
            computed_total = 0
            normalized_rows: list[tuple[tuple[str, str], ...]] = []
            for row in rows:
                if (
                    not isinstance(row, dict)
                    or set(row) != _EXPECTED_FIELDS
                    or any(not isinstance(value, str) for value in row.values())
                    or row["TRD_DD"] != business_date.strftime("%Y%m%d")
                    or row["DD_TP"] != "T_DD"
                ):
                    raise ValueError("invalid KRX live investor row")
                investors.add(row["INVST_TP"])
                bid = _integer(row["ACC_BID_TRDVAL"], "bid trading value")
                ask = _integer(row["ACC_ASK_TRDVAL"], "ask trading value")
                _integer(row["NETBID_TRDVAL"], "net trading value")
                if bid < 0 or ask < 0:
                    raise ValueError("invalid KRX live trading value")
                computed_total += bid + ask
                normalized_rows.append(tuple(row.items()))
            if (
                investors != _EXPECTED_INVESTORS
                or computed_total != raw_activity["trading_value"]
            ):
                raise ValueError("invalid KRX live activity totals")
            activities.append(
                KrxLiveMarketActivity(
                    market,
                    activity_source_as_of,
                    computed_total,
                    tuple(normalized_rows),
                )
            )
        if tuple(item.market for item in activities) != (
            KrxMarket.KOSPI,
            KrxMarket.KOSDAQ,
        ):
            raise ValueError("KRX live snapshot must cover both markets in order")
        if (
            source_as_of.astimezone(KST).date() != business_date
            or source_as_of.astimezone(KST).time() < time(9, 0)
            or source_as_of > fetched_at
            or fetched_at - source_as_of > timedelta(minutes=5)
        ):
            raise ValueError("invalid KRX live timestamp lineage")
        return cls(
            business_date,
            source_as_of,
            fetched_at,
            tuple(activities),
            SOURCE_URL,
        )


@dataclass
class KrxLiveClient:
    transport: LiveTransport = field(default=_default_transport, repr=False)
    clock: Clock = field(default=lambda: datetime.now().astimezone(), repr=False)
    timeout: float = 15.0

    def __post_init__(self) -> None:
        if self.timeout <= 0:
            raise ValueError("timeout must be positive")

    def current_activity(self, business_date: date) -> KrxLiveActivitySnapshot:
        try:
            payloads = self.transport(self.timeout)
        except (OSError, TimeoutError):
            raise KrxLiveTransportError("KRX live request failed") from None
        if set(payloads) != set(KrxMarket):
            raise KrxLiveResponseError("KRX live response must cover both markets")
        fetched_at = self.clock()
        if fetched_at.tzinfo is None or fetched_at.utcoffset() is None:
            raise KrxLiveResponseError("KRX live fetched_at must be timezone-aware")
        fetched_at = fetched_at.astimezone(KST)
        activities: list[KrxLiveMarketActivity] = []
        source_times: list[datetime] = []
        expected_date = business_date.strftime("%Y%m%d")
        for market in KrxMarket:
            try:
                payload = json.loads(payloads[market])
            except (json.JSONDecodeError, UnicodeError) as error:
                raise KrxLiveResponseError(
                    "KRX live response is invalid JSON"
                ) from error
            if not isinstance(payload, dict) or set(payload) != {
                "output",
                "CURRENT_DATETIME",
            }:
                raise KrxLiveResponseError("KRX live response schema is invalid")
            source_time = _parse_source_time(payload["CURRENT_DATETIME"])
            if source_time.date() != business_date:
                raise KrxLiveResponseError("KRX live source date mismatch")
            if source_time.timetz().replace(tzinfo=None) < time(9, 0):
                raise KrxLiveResponseError(
                    "KRX live source must be observed after the KRX open"
                )
            rows = payload["output"]
            if not isinstance(rows, list) or len(rows) != len(_EXPECTED_INVESTORS):
                raise KrxLiveResponseError(
                    "KRX live response must contain all investor categories"
                )
            investors: set[str] = set()
            total_value = 0
            normalized_rows: list[tuple[tuple[str, str], ...]] = []
            for row in rows:
                if (
                    not isinstance(row, dict)
                    or set(row) != _EXPECTED_FIELDS
                    or any(not isinstance(value, str) for value in row.values())
                ):
                    raise KrxLiveResponseError(
                        "KRX live investor row schema is invalid"
                    )
                if row["TRD_DD"] != expected_date:
                    raise KrxLiveResponseError("KRX live trading date mismatch")
                if row["DD_TP"] != "T_DD":
                    raise KrxLiveResponseError("KRX live day type is invalid")
                investors.add(row["INVST_TP"])
                bid = _integer(row["ACC_BID_TRDVAL"], "bid trading value")
                ask = _integer(row["ACC_ASK_TRDVAL"], "ask trading value")
                _integer(row["NETBID_TRDVAL"], "net trading value")
                if bid < 0 or ask < 0:
                    raise KrxLiveResponseError(
                        "KRX live trading values must be non-negative"
                    )
                total_value += bid + ask
                normalized_rows.append(tuple(row.items()))
            if investors != _EXPECTED_INVESTORS:
                raise KrxLiveResponseError(
                    "KRX live response has invalid investor categories"
                )
            if total_value <= 0:
                raise KrxLiveResponseError(
                    "KRX live response must have positive trading value"
                )
            source_times.append(source_time)
            activities.append(
                KrxLiveMarketActivity(
                    market, source_time, total_value, tuple(normalized_rows)
                )
            )
        if max(source_times) - min(source_times) > timedelta(minutes=2):
            raise KrxLiveResponseError(
                "KRX live market snapshots are not contemporaneous"
            )
        source_as_of = max(source_times)
        if source_as_of > fetched_at or fetched_at - source_as_of > timedelta(
            minutes=5
        ):
            raise KrxLiveResponseError("KRX live timestamp lineage is invalid")
        return KrxLiveActivitySnapshot(
            business_date,
            source_as_of,
            fetched_at,
            tuple(activities),
        )
