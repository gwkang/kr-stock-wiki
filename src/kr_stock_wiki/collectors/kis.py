from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

from ..evidence import EvidenceRecord, EvidenceSource, VerificationStatus

_BASE_URL = "https://openapi.koreainvestment.com:9443"
_DAILY_PATH = "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
_TOKEN_PATH = "/oauth2/tokenP"
_DAILY_TR_ID = "FHKST03010100"

Transport = Callable[[str, str, dict[str, str], bytes | None, float], bytes]
Clock = Callable[[], datetime]


class KisError(RuntimeError):
    """Base error for safe KIS collector failures."""


class KisTransportError(KisError):
    """KIS transport failed without exposing response content."""


class KisResponseError(KisError):
    """KIS returned a malformed or semantically invalid response."""


def _default_transport(
    method: str,
    url: str,
    headers: dict[str, str],
    body: bytes | None,
    timeout: float,
) -> bytes:
    request = Request(url, data=body, headers=headers, method=method)
    with urlopen(request, timeout=timeout) as response:
        return response.read()


def _non_negative_int(value: object, field_name: str) -> int:
    if isinstance(value, bool):
        raise KisResponseError(f"KIS {field_name} must be a non-negative integer")
    try:
        parsed = int(str(value).replace(",", ""))
    except (TypeError, ValueError):
        raise KisResponseError(
            f"KIS {field_name} must be a non-negative integer"
        ) from None
    if parsed < 0:
        raise KisResponseError(f"KIS {field_name} must be a non-negative integer")
    return parsed


@dataclass(frozen=True)
class KisDailySnapshot:
    business_date: date
    requested_tickers: tuple[str, ...]
    completed_tickers: tuple[str, ...]
    records: tuple[EvidenceRecord, ...]
    fetched_at: datetime

    def __post_init__(self) -> None:
        if not self.requested_tickers or len(set(self.requested_tickers)) != len(
            self.requested_tickers
        ):
            raise ValueError("requested KIS tickers must be non-empty and unique")
        if set(self.completed_tickers) != set(self.requested_tickers):
            raise ValueError(
                "completed KIS tickers must exactly match requested tickers"
            )
        if self.fetched_at.tzinfo is None or self.fetched_at.utcoffset() is None:
            raise ValueError("KIS snapshot fetched_at must be timezone-aware")
        if len(self.records) != len(self.requested_tickers):
            raise ValueError("KIS snapshot record count must match requested tickers")
        records = {record.ticker: record for record in self.records}
        if set(records) != set(self.requested_tickers):
            raise ValueError(
                "KIS snapshot records must exactly match requested tickers"
            )
        for record in self.records:
            parsed = urlparse(record.source_url)
            if (
                record.source is not EvidenceSource.KIS
                or record.verification is not VerificationStatus.OFFICIAL
                or record.kind != "daily-price"
                or record.ticker is None
                or record.published_date != self.business_date
                or parsed.scheme != "https"
                or parsed.hostname != "openapi.koreainvestment.com"
                or parsed.port != 9443
                or parsed.path != _DAILY_PATH
            ):
                raise ValueError("KIS snapshot contains an invalid evidence record")
        if max(record.fetched_at for record in self.records) != self.fetched_at:
            raise ValueError("KIS snapshot fetched_at must match latest record")

    @property
    def coverage_complete(self) -> bool:
        return set(self.completed_tickers) == set(self.requested_tickers)

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "source": "kis",
            "collected_at": self.fetched_at.isoformat(),
            "date": self.business_date.isoformat(),
            "coverage_complete": self.coverage_complete,
            "requested_tickers": list(self.requested_tickers),
            "completed_tickers": list(self.completed_tickers),
            "records": [record.to_dict() for record in self.records],
        }

    @classmethod
    def from_payload(cls, payload: dict[str, object]) -> KisDailySnapshot:
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") != 1
            or payload.get("source") != "kis"
        ):
            raise ValueError("invalid KIS snapshot envelope")
        requested = payload.get("requested_tickers")
        completed = payload.get("completed_tickers")
        records = payload.get("records")
        if (
            not isinstance(requested, list)
            or not isinstance(completed, list)
            or not isinstance(records, list)
        ):
            raise ValueError("invalid KIS snapshot coverage metadata")
        snapshot = cls(
            business_date=date.fromisoformat(str(payload["date"])),
            requested_tickers=tuple(str(ticker) for ticker in requested),
            completed_tickers=tuple(str(ticker) for ticker in completed),
            records=tuple(EvidenceRecord.from_dict(record) for record in records),
            fetched_at=datetime.fromisoformat(str(payload["collected_at"])),
        )
        if payload.get("coverage_complete") is not snapshot.coverage_complete:
            raise ValueError("KIS snapshot coverage_complete is inconsistent")
        return snapshot


@dataclass
class KisClient:
    app_key: str = field(repr=False)
    app_secret: str = field(repr=False)
    transport: Transport = field(default=_default_transport, repr=False)
    clock: Clock = field(default=lambda: datetime.now().astimezone(), repr=False)
    timeout: float = 15.0

    def __post_init__(self) -> None:
        for name, value in (
            ("KIS app key", self.app_key),
            ("KIS app secret", self.app_secret),
        ):
            if not value or any(char.isspace() for char in value):
                raise ValueError(f"{name} must be non-empty and contain no whitespace")
        if self.timeout <= 0:
            raise ValueError("timeout must be positive")

    def _request(
        self, method: str, url: str, headers: dict[str, str], body: bytes | None
    ) -> dict[str, Any]:
        try:
            raw = self.transport(method, url, headers, body, self.timeout)
        except HTTPError as error:
            raise KisTransportError(f"KIS HTTP {error.code}") from None
        except (OSError, TimeoutError):
            raise KisTransportError("KIS request failed") from None
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, UnicodeError):
            raise KisResponseError("KIS returned invalid JSON") from None
        if not isinstance(payload, dict):
            raise KisResponseError("KIS response must be a JSON object")
        return payload

    def _token(self) -> str:
        payload = self._request(
            "POST",
            f"{_BASE_URL}{_TOKEN_PATH}",
            {"content-type": "application/json", "accept": "text/plain"},
            json.dumps(
                {
                    "grant_type": "client_credentials",
                    "appkey": self.app_key,
                    "appsecret": self.app_secret,
                }
            ).encode(),
        )
        token = payload.get("access_token")
        if (
            not isinstance(token, str)
            or not token
            or any(char.isspace() for char in token)
        ):
            raise KisResponseError("KIS token response is missing access_token")
        return token

    def _daily_payload(
        self, ticker: str, business_date: date, token: str
    ) -> tuple[dict[str, Any], str]:
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_DATE_1": (business_date - timedelta(days=10)).strftime("%Y%m%d"),
            "FID_INPUT_DATE_2": business_date.strftime("%Y%m%d"),
            "FID_INPUT_ISCD": ticker,
            "FID_ORG_ADJ_PRC": "0",
            "FID_PERIOD_DIV_CODE": "D",
        }
        query = urlencode(sorted(params.items()))
        source_url = f"{_BASE_URL}{_DAILY_PATH}?{query}"
        payload = self._request(
            "GET",
            source_url,
            {
                "authorization": f"Bearer {token}",
                "appkey": self.app_key,
                "appsecret": self.app_secret,
                "tr_id": _DAILY_TR_ID,
                "custtype": "P",
            },
            None,
        )
        if payload.get("rt_cd") != "0":
            raise KisResponseError(f"KIS API error {payload.get('rt_cd', '<missing>')}")
        return payload, source_url

    def daily_snapshot(
        self, business_date: date, watchlist: Mapping[str, str]
    ) -> KisDailySnapshot:
        requested = tuple(watchlist)
        if not requested or len(set(requested)) != len(requested):
            raise ValueError("KIS watchlist tickers must be non-empty and unique")
        if any(
            len(ticker) != 6 or not ticker.isascii() or not ticker.isalnum()
            for ticker in requested
        ):
            raise ValueError(
                "KIS watchlist tickers must be six ASCII alphanumeric characters"
            )
        for ticker in requested:
            expected_name = watchlist[ticker]
            if not isinstance(expected_name, str) or not expected_name.strip():
                raise ValueError("KIS watchlist names must be non-empty strings")
        token = self._token()
        records: list[EvidenceRecord] = []
        for ticker in requested:
            expected_name = watchlist[ticker]
            payload, source_url = self._daily_payload(ticker, business_date, token)
            records.append(
                self._record(
                    payload, source_url, business_date, ticker, expected_name.strip()
                )
            )
        fetched_at = self.clock()
        records = [
            EvidenceRecord(**{**record.__dict__, "fetched_at": fetched_at})
            for record in records
        ]
        return KisDailySnapshot(
            business_date=business_date,
            requested_tickers=requested,
            completed_tickers=requested,
            records=tuple(records),
            fetched_at=fetched_at,
        )

    def _record(
        self,
        payload: dict[str, Any],
        source_url: str,
        business_date: date,
        ticker: str,
        expected_name: str,
    ) -> EvidenceRecord:
        output1 = payload.get("output1")
        output2 = payload.get("output2")
        if not isinstance(output1, dict) or not isinstance(output2, list):
            raise KisResponseError("KIS response is missing daily-price output")
        date_text = business_date.strftime("%Y%m%d")
        if str(output1.get("stck_shrn_iscd")) != ticker:
            raise KisResponseError("KIS daily metadata ticker mismatch")
        name = str(output1.get("hts_kor_isnm", "")).strip()
        if name != expected_name:
            raise KisResponseError("KIS daily metadata company name mismatch")
        rows = [row for row in output2 if isinstance(row, dict)]
        current = next(
            (row for row in rows if str(row.get("stck_bsop_date")) == date_text), None
        )
        older = sorted(
            (row for row in rows if str(row.get("stck_bsop_date", "")) < date_text),
            key=lambda row: str(row["stck_bsop_date"]),
            reverse=True,
        )
        if current is None or not older:
            raise KisResponseError("KIS response is missing exact daily history")
        close = _non_negative_int(current.get("stck_clpr"), "stck_clpr")
        previous_close = _non_negative_int(
            older[0].get("stck_clpr"), "previous stck_clpr"
        )
        if previous_close == 0:
            raise KisResponseError("KIS previous stck_clpr must be positive")
        change = close - previous_close
        return EvidenceRecord(
            source=EvidenceSource.KIS,
            evidence_id=f"kis:daily:{date_text}:{ticker}",
            canonical_event_id=f"kis:daily:{date_text}:{ticker}",
            kind="daily-price",
            company_name=name,
            title=f"{name} KIS 국내주식 일별 시세",
            source_url=source_url,
            published_date=business_date,
            fetched_at=self.clock(),
            verification=VerificationStatus.OFFICIAL,
            ticker=ticker,
            metrics={
                "close": close,
                "change": change,
                "change_rate": round(change / previous_close * 100, 2),
                "volume": _non_negative_int(current.get("acml_vol"), "acml_vol"),
                "trading_value": _non_negative_int(
                    current.get("acml_tr_pbmn"), "acml_tr_pbmn"
                ),
                "market_cap": _non_negative_int(output1.get("hts_avls"), "hts_avls"),
                "listed_shares": _non_negative_int(
                    output1.get("lstn_stcn"), "lstn_stcn"
                ),
            },
            raw={
                "market": "J",
                "metadata_date": date_text,
                "price_date": date_text,
            },
        )
