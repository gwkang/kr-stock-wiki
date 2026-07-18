from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Callable
from urllib.parse import urlencode, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zoneinfo import ZoneInfo

from ..evidence import EvidenceRecord, EvidenceSource, VerificationStatus


Transport = Callable[[str, bytes, float], bytes]
Clock = Callable[[], datetime]
_BASE_URL = "https://www.nextrade.co.kr"
_QUOTE_ENDPOINT = "/brdinfoTime/brdinfoTimeList.do"
_SUMMARY_ENDPOINT = "/dailyInfo/dailyInfoList.do"
_QUOTE_SOURCE_URL = f"{_BASE_URL}/menu/transactionStatusMain/menuList.do"
_SUMMARY_SOURCE_URL = f"{_BASE_URL}/menu/transactionStatusDaily/menuList.do"
_DELAY_MINUTES = 20
_MAX_RESPONSE_BYTES = 10 * 1024 * 1024
_KST = ZoneInfo("Asia/Seoul")
_QUOTE_FIELDS = (
    "aggDd",
    "isuSrdCd",
    "isuAbwdNm",
    "mktNm",
    "curPrc",
    "contrastPrc",
    "upDownRate",
    "oppr",
    "hgpr",
    "lwpr",
    "accTdQty",
    "accTrval",
    "basePrc",
    "cptrTrdPmsnCd",
    "cptrTrdPmsnCdNm",
    "trdIpsbRsn",
)
_SUMMARY_FIELDS = (
    "aggDd",
    "preIsuCnt",
    "preAccTdQty",
    "preAccTrval",
    "mainIsuCnt",
    "mainAccTdQty",
    "mainAccTrval",
    "aftIsuCnt",
    "aftAccTdQty",
    "aftAccTrval",
    "totalIsuCnt",
    "totalAccTdQty",
    "totalAccTrval",
    "mktShr",
)


class NxtError(ValueError):
    pass


class NxtTransportError(NxtError):
    pass


class NxtResponseError(NxtError):
    pass


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _trusted_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme == "https" and parsed.hostname == "www.nextrade.co.kr"


def _default_transport(url: str, body: bytes, timeout: float) -> bytes:
    if not _trusted_url(url):
        raise NxtTransportError("NXT request blocked: untrusted endpoint")
    request = Request(
        url,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    opener = build_opener(_NoRedirect)
    # Redirects are disabled and both the requested and final URL are allowlisted.
    with opener.open(request, timeout=timeout) as response:  # nosec B310
        if not _trusted_url(response.geturl()):
            raise NxtTransportError("NXT response blocked: untrusted endpoint")
        content_length = response.headers.get("Content-Length")
        if content_length is not None:
            try:
                declared_size = int(content_length)
            except ValueError as error:
                raise NxtTransportError("NXT response has invalid size") from error
            if declared_size > _MAX_RESPONSE_BYTES:
                raise NxtTransportError("NXT response exceeds size limit")
        payload = response.read(_MAX_RESPONSE_BYTES + 1)
        if len(payload) > _MAX_RESPONSE_BYTES:
            raise NxtTransportError("NXT response exceeds size limit")
        return payload


def _integer(
    value: object, *, field: str = "value", context: str = "response"
) -> int | None:
    if value is None:
        return None
    text = str(value).replace(",", "").strip()
    if text in {"", "-"}:
        return None
    try:
        return int(text)
    except ValueError as error:
        raise NxtResponseError(
            f"NXT {context} field {field} must be an integer"
        ) from error


def _decimal(
    value: object, *, field: str = "value", context: str = "response"
) -> float | None:
    if value is None:
        return None
    text = str(value).replace(",", "").strip()
    if text in {"", "-"}:
        return None
    try:
        number = float(text)
    except ValueError as error:
        raise NxtResponseError(
            f"NXT {context} field {field} must be numeric"
        ) from error
    if not math.isfinite(number):
        raise NxtResponseError(f"NXT {context} field {field} must be finite")
    return number


def _required_integer(item: dict, field: str, context: str) -> int:
    number = _integer(item[field], field=field, context=context)
    if number is None:
        raise NxtResponseError(f"NXT {context} field {field} must not be empty")
    return number


def _require_fields(item: dict, fields: tuple[str, ...], context: str) -> None:
    missing = [field for field in fields if field not in item or item[field] is None]
    if missing:
        raise NxtResponseError(
            f"NXT {context} missing required fields: " + ", ".join(missing)
        )


@dataclass
class NxtClient:
    transport: Transport = field(default=_default_transport, repr=False)
    clock: Clock = field(default=lambda: datetime.now().astimezone(), repr=False)
    timeout: float = 15.0
    page_size: int = 100
    max_pages: int = 100

    def __post_init__(self) -> None:
        if self.timeout <= 0:
            raise ValueError("timeout must be positive")
        if self.page_size < 1:
            raise ValueError("page_size must be positive")
        if self.max_pages < 1:
            raise ValueError("max_pages must be positive")

    def _post(self, path: str, params: dict[str, str | int]) -> dict:
        url = f"{_BASE_URL}{path}"
        body = urlencode(params).encode()
        try:
            raw_payload = self.transport(url, body, self.timeout)
        except (OSError, TimeoutError):
            raise NxtTransportError("NXT request failed") from None
        try:
            payload = json.loads(raw_payload)
        except (json.JSONDecodeError, UnicodeError) as error:
            raise NxtResponseError("NXT returned invalid JSON") from error
        if not isinstance(payload, dict):
            raise NxtResponseError("NXT response must be a JSON object")
        return payload

    @staticmethod
    def _page(
        payload: dict, requested_page: int, list_key: str
    ) -> tuple[list[dict], int]:
        try:
            response_page = int(payload["page"])
            total_pages = int(payload["total"])
        except (KeyError, TypeError, ValueError) as error:
            raise NxtResponseError("NXT response has invalid pagination") from error
        if response_page != requested_page:
            raise NxtResponseError(
                f"NXT page mismatch: requested {requested_page}, received {response_page}"
            )
        if total_pages < 1:
            raise NxtResponseError("NXT response has invalid total pages")
        rows = payload.get(list_key)
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise NxtResponseError(f"NXT response has invalid {list_key}")
        return rows, total_pages

    def daily_quotes(self, business_date: date) -> list[EvidenceRecord]:
        date_text = business_date.strftime("%Y%m%d")
        records: list[EvidenceRecord] = []
        seen_items: dict[str, dict] = {}
        expected_record_count: int | None = None
        source_as_of: datetime | None = None
        page = 1
        while True:
            payload = self._post(
                _QUOTE_ENDPOINT,
                {
                    "pageIndex": page,
                    "pageUnit": self.page_size,
                    "scAggDd": date_text,
                    "scMktId": "",
                    "searchKeyword": "",
                },
            )
            rows, total_pages = self._page(payload, page, "brdinfoTimeList")
            if total_pages > self.max_pages:
                raise NxtResponseError("NXT response exceeds configured max_pages")
            set_time = payload.get("setTime")
            if not isinstance(set_time, str) or not set_time:
                raise NxtResponseError("NXT response is missing setTime")
            try:
                parsed_set_time = datetime.strptime(set_time, "%Y-%m-%d %H:%M").replace(
                    tzinfo=_KST
                )
            except ValueError as error:
                raise NxtResponseError("NXT response has invalid setTime") from error
            if parsed_set_time.date() != business_date:
                raise NxtResponseError("NXT setTime business date mismatch")
            if source_as_of is None:
                source_as_of = parsed_set_time
            elif source_as_of != parsed_set_time:
                raise NxtResponseError("NXT setTime changed during pagination")
            try:
                response_record_count = int(payload["records"])
            except (KeyError, TypeError, ValueError) as error:
                raise NxtResponseError(
                    "NXT response has invalid record count"
                ) from error
            if response_record_count < 0:
                raise NxtResponseError("NXT response has invalid record count")
            if expected_record_count is None:
                expected_record_count = response_record_count
            elif expected_record_count != response_record_count:
                raise NxtResponseError("NXT record count changed during pagination")
            fetched_at = self.clock()
            for item in rows:
                ticker_hint = str(item.get("isuSrdCd", "<unknown>"))
                _require_fields(item, _QUOTE_FIELDS, f"quote {ticker_hint}")
                if str(item["aggDd"]) != date_text:
                    raise NxtResponseError(
                        f"NXT quote {ticker_hint} business date mismatch"
                    )
                short_code = str(item["isuSrdCd"])
                if len(short_code) != 7 or not short_code.startswith("A"):
                    raise NxtResponseError(f"NXT quote has invalid ticker {short_code}")
                ticker = short_code[1:]
                if (
                    not ticker.isascii()
                    or not ticker.isalnum()
                    or ticker != ticker.upper()
                ):
                    raise NxtResponseError(f"NXT quote has invalid ticker {short_code}")
                market = str(item["mktNm"])
                if market not in {"KOSPI", "KOSDAQ"}:
                    raise NxtResponseError(
                        f"NXT quote {ticker} has invalid market {market}"
                    )
                company_name = str(item["isuAbwdNm"]).strip()
                if not company_name:
                    raise NxtResponseError(f"NXT quote {ticker} has an empty name")
                previous_item = seen_items.get(ticker)
                if previous_item is not None:
                    if previous_item != item:
                        raise NxtResponseError(
                            f"NXT quote {ticker} changed during pagination"
                        )
                    continue
                seen_items[ticker] = dict(item)
                context = f"quote {short_code}"
                evidence_id = f"nxt:price-snapshot:{date_text}:{ticker}"
                records.append(
                    EvidenceRecord(
                        source=EvidenceSource.NXT,
                        evidence_id=evidence_id,
                        canonical_event_id=evidence_id,
                        kind="price-snapshot",
                        company_name=company_name,
                        title=f"{company_name} NXT 현재가 스냅샷",
                        source_url=_QUOTE_SOURCE_URL,
                        published_date=business_date,
                        fetched_at=fetched_at,
                        verification=VerificationStatus.OFFICIAL,
                        ticker=ticker,
                        delay_minutes=_DELAY_MINUTES,
                        metrics={
                            "market": market,
                            "current_price": _integer(
                                item["curPrc"], field="curPrc", context=context
                            ),
                            "change": _integer(
                                item["contrastPrc"],
                                field="contrastPrc",
                                context=context,
                            ),
                            "change_rate": _decimal(
                                item["upDownRate"], field="upDownRate", context=context
                            ),
                            "open": _integer(
                                item["oppr"], field="oppr", context=context
                            ),
                            "high": _integer(
                                item["hgpr"], field="hgpr", context=context
                            ),
                            "low": _integer(
                                item["lwpr"], field="lwpr", context=context
                            ),
                            "volume": _integer(
                                item["accTdQty"], field="accTdQty", context=context
                            ),
                            "trading_value": _integer(
                                item["accTrval"], field="accTrval", context=context
                            ),
                            "base_price": _integer(
                                item["basePrc"], field="basePrc", context=context
                            ),
                            "available_session_code": str(item["cptrTrdPmsnCd"]),
                            "available_sessions": str(item["cptrTrdPmsnCdNm"]),
                            "unavailable_reason": str(item["trdIpsbRsn"]),
                            "source_as_of": parsed_set_time.isoformat(),
                        },
                        raw={**item, "setTime": set_time},
                    )
                )
            if page >= total_pages:
                break
            page += 1
        if expected_record_count is None or len(records) != expected_record_count:
            raise NxtResponseError(
                "NXT record count mismatch: "
                f"expected {expected_record_count}, collected {len(records)}"
            )
        return records

    def session_summary(self, business_date: date) -> EvidenceRecord | None:
        date_text = business_date.strftime("%Y%m%d")
        payload = self._post(
            _SUMMARY_ENDPOINT,
            {
                "pageIndex": 1,
                "pageUnit": self.page_size,
                "scBeginDe": date_text,
                "scEndDe": date_text,
            },
        )
        rows, total_pages = self._page(payload, 1, "dailyInfoList")
        if total_pages > 1:
            raise NxtResponseError("NXT single-day summary returned multiple pages")
        try:
            response_record_count = int(payload["records"])
        except (KeyError, TypeError, ValueError) as error:
            raise NxtResponseError("NXT summary has invalid record count") from error
        if response_record_count != len(rows):
            raise NxtResponseError(
                "NXT summary record count mismatch: "
                f"expected {response_record_count}, received {len(rows)}"
            )
        if response_record_count not in {0, 1}:
            raise NxtResponseError("NXT single-day summary returned multiple records")
        if not rows:
            return None
        item = rows[0]
        _require_fields(item, _SUMMARY_FIELDS, f"session summary {date_text}")
        if str(item["aggDd"]) != date_text:
            raise NxtResponseError("NXT session summary business date mismatch")
        context = f"session summary {date_text}"
        pre_volume = _required_integer(item, "preAccTdQty", context)
        main_volume = _required_integer(item, "mainAccTdQty", context)
        after_volume = _required_integer(item, "aftAccTdQty", context)
        total_volume = _required_integer(item, "totalAccTdQty", context)
        pre_value = _required_integer(item, "preAccTrval", context)
        main_value = _required_integer(item, "mainAccTrval", context)
        after_value = _required_integer(item, "aftAccTrval", context)
        total_value = _required_integer(item, "totalAccTrval", context)
        if total_volume != pre_volume + main_volume + after_volume or total_value != (
            pre_value + main_value + after_value
        ):
            raise NxtResponseError(f"NXT {context} session totals mismatch")
        evidence_id = f"nxt:session-summary:{date_text}"
        return EvidenceRecord(
            source=EvidenceSource.NXT,
            evidence_id=evidence_id,
            canonical_event_id=evidence_id,
            kind="session-summary",
            company_name="NEXTRADE",
            title=f"NXT {business_date.isoformat()} 세션별 거래 현황",
            source_url=_SUMMARY_SOURCE_URL,
            published_date=business_date,
            fetched_at=self.clock(),
            verification=VerificationStatus.OFFICIAL,
            metrics={
                "pre_session": "08:00-08:50",
                "pre_instruments": _required_integer(item, "preIsuCnt", context),
                "pre_volume": pre_volume,
                "pre_trading_value": pre_value,
                "main_session": "09:00:30-15:20",
                "main_instruments": _required_integer(item, "mainIsuCnt", context),
                "main_volume": main_volume,
                "main_trading_value": main_value,
                "after_session": "15:40-20:00",
                "after_instruments": _required_integer(item, "aftIsuCnt", context),
                "after_volume": after_volume,
                "after_trading_value": after_value,
                "total_instruments": _required_integer(item, "totalIsuCnt", context),
                "total_volume": total_volume,
                "total_trading_value": total_value,
                "volume_market_share": _decimal(
                    item["mktShr"], field="mktShr", context=context
                ),
            },
            raw=dict(item),
        )
