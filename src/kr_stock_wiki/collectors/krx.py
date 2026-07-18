from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from typing import Callable
from urllib.parse import urlencode, urlparse
from urllib.request import urlopen

from ..evidence import EvidenceRecord, EvidenceSource, VerificationStatus


Transport = Callable[[str, float], bytes]
Clock = Callable[[], datetime]


class KrxError(ValueError):
    """Base error safe to display without exposing the authenticated URL."""


class KrxTransportError(KrxError):
    pass


class KrxResponseError(KrxError):
    pass


class KrxMarket(StrEnum):
    KOSPI = "KOSPI"
    KOSDAQ = "KOSDAQ"


_ENDPOINTS = {
    KrxMarket.KOSPI: "sto/stk_bydd_trd",
    KrxMarket.KOSDAQ: "sto/ksq_bydd_trd",
}
_BASE_URL = "https://data-dbg.krx.co.kr/svc/apis"
_REQUIRED_QUOTE_FIELDS = (
    "BAS_DD",
    "ISU_CD",
    "ISU_NM",
    "MKT_NM",
    "TDD_CLSPRC",
    "CMPPREVDD_PRC",
    "FLUC_RT",
    "TDD_OPNPRC",
    "TDD_HGPRC",
    "TDD_LWPRC",
    "ACC_TRDVOL",
    "ACC_TRDVAL",
    "MKTCAP",
    "LIST_SHRS",
)


def _default_transport(url: str, timeout: float) -> bytes:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != "data-dbg.krx.co.kr":
        raise KrxTransportError("KRX request blocked: untrusted endpoint")
    # URL scheme and host are allowlisted immediately above.
    with urlopen(url, timeout=timeout) as response:  # nosec B310
        return response.read()


def _integer(value: object) -> int | None:
    if value is None:
        return None
    text = str(value).replace(",", "").strip()
    return int(text) if text not in {"", "-"} else None


def _decimal(value: object) -> float | None:
    if value is None:
        return None
    text = str(value).replace(",", "").strip()
    return float(text) if text not in {"", "-"} else None


@dataclass
class KrxClient:
    api_key: str = field(repr=False)
    transport: Transport = field(default=_default_transport, repr=False)
    clock: Clock = field(default=lambda: datetime.now().astimezone(), repr=False)
    timeout: float = 15.0

    def __post_init__(self) -> None:
        if not self.api_key or any(char.isspace() for char in self.api_key):
            raise ValueError("KRX API key must be non-empty and contain no whitespace")
        if self.timeout <= 0:
            raise ValueError("timeout must be positive")

    def _request(self, endpoint: str, business_date: date) -> list[dict]:
        params = {
            "AUTH_KEY": self.api_key,
            "basDd": business_date.strftime("%Y%m%d"),
        }
        url = f"{_BASE_URL}/{endpoint}?{urlencode(params)}"
        try:
            raw_payload = self.transport(url, self.timeout)
        except (OSError, TimeoutError):
            raise KrxTransportError("KRX request failed") from None
        try:
            payload = json.loads(raw_payload)
        except (json.JSONDecodeError, UnicodeError) as error:
            raise KrxResponseError("KRX returned invalid JSON") from error
        if not isinstance(payload, dict):
            raise KrxResponseError("KRX response must be a JSON object")
        code = payload.get("respCode")
        message = payload.get("respMsg")
        if code not in {None, "", "0", "000", "200", 0, 200}:
            raise KrxResponseError(f"KRX API error {code}: {message}")
        records = payload.get("OutBlock_1")
        if not isinstance(records, list):
            if code or message:
                raise KrxResponseError(f"KRX API error {code}: {message}")
            raise KrxResponseError("KRX response is missing OutBlock_1")
        if not all(isinstance(item, dict) for item in records):
            raise KrxResponseError("KRX OutBlock_1 must contain JSON objects")
        return records

    def daily_prices(
        self,
        business_date: date,
        *,
        markets: tuple[KrxMarket, ...] = (KrxMarket.KOSPI, KrxMarket.KOSDAQ),
    ) -> list[EvidenceRecord]:
        if not markets:
            raise ValueError("at least one KRX market is required")
        date_text = business_date.strftime("%Y%m%d")
        evidence: list[EvidenceRecord] = []
        for market in markets:
            endpoint = _ENDPOINTS[market]
            items = self._request(endpoint, business_date)
            fetched_at = self.clock()
            source_url = f"{_BASE_URL}/{endpoint}?{urlencode({'basDd': date_text})}"
            for item in items:
                ticker_hint = str(item.get("ISU_CD", "<unknown>"))
                missing = [
                    field
                    for field in _REQUIRED_QUOTE_FIELDS
                    if field not in item or item[field] is None
                ]
                if missing:
                    raise KrxResponseError(
                        f"KRX record {ticker_hint} missing required fields: "
                        + ", ".join(missing)
                    )
                if str(item["BAS_DD"]) != date_text:
                    raise KrxResponseError(
                        f"KRX record {ticker_hint} business date mismatch"
                    )
                actual_market = str(item["MKT_NM"])
                if actual_market != market.value:
                    raise KrxResponseError(
                        f"KRX record {ticker_hint} market mismatch: requested "
                        f"{market.value}, received {actual_market}"
                    )
                ticker = str(item["ISU_CD"])
                company_name = str(item["ISU_NM"]).strip()
                if not company_name:
                    raise KrxResponseError(f"KRX record {ticker} has an empty ISU_NM")
                evidence.append(
                    EvidenceRecord(
                        source=EvidenceSource.KRX,
                        evidence_id=(f"krx:daily:{market.value}:{date_text}:{ticker}"),
                        canonical_event_id=(
                            f"krx:daily:{market.value}:{date_text}:{ticker}"
                        ),
                        kind="daily-price",
                        company_name=company_name,
                        title=f"{company_name} KRX {market.value} 일별 시세",
                        source_url=source_url,
                        published_date=business_date,
                        fetched_at=fetched_at,
                        verification=VerificationStatus.OFFICIAL,
                        ticker=ticker,
                        metrics={
                            "close": _integer(item.get("TDD_CLSPRC")),
                            "change": _integer(item.get("CMPPREVDD_PRC")),
                            "change_rate": _decimal(item.get("FLUC_RT")),
                            "open": _integer(item.get("TDD_OPNPRC")),
                            "high": _integer(item.get("TDD_HGPRC")),
                            "low": _integer(item.get("TDD_LWPRC")),
                            "volume": _integer(item.get("ACC_TRDVOL")),
                            "trading_value": _integer(item.get("ACC_TRDVAL")),
                            "market_cap": _integer(item.get("MKTCAP")),
                            "listed_shares": _integer(item.get("LIST_SHRS")),
                        },
                        raw=dict(item),
                    )
                )
        return evidence
