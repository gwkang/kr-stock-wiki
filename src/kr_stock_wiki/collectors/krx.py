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


MINIMUM_DAILY_RECORDS: tuple[tuple[KrxMarket, int], ...] = (
    (KrxMarket.KOSPI, 500),
    (KrxMarket.KOSDAQ, 1_000),
)


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


@dataclass(frozen=True)
class KrxDailySnapshot:
    business_date: date
    requested_markets: tuple[KrxMarket, ...]
    completed_markets: tuple[KrxMarket, ...]
    record_counts: tuple[tuple[KrxMarket, int], ...]
    records: tuple[EvidenceRecord, ...]
    fetched_at: datetime

    def __post_init__(self) -> None:
        if not self.requested_markets or len(set(self.requested_markets)) != len(
            self.requested_markets
        ):
            raise ValueError("requested KRX markets must be non-empty and unique")
        if len(set(self.completed_markets)) != len(self.completed_markets):
            raise ValueError("completed KRX markets must be unique")
        if not set(self.completed_markets) <= set(self.requested_markets):
            raise ValueError("completed KRX markets must have been requested")
        if self.fetched_at.tzinfo is None or self.fetched_at.utcoffset() is None:
            raise ValueError("KRX snapshot fetched_at must be timezone-aware")
        counts = self.counts
        if set(counts) != set(self.completed_markets):
            raise ValueError("record counts must cover exactly the completed markets")
        if any(isinstance(count, bool) or count < 0 for count in counts.values()):
            raise ValueError("KRX record counts must be non-negative integers")
        actual = {market: 0 for market in self.completed_markets}
        tickers: set[str] = set()
        for record in self.records:
            if (
                record.source is not EvidenceSource.KRX
                or record.verification is not VerificationStatus.OFFICIAL
                or record.kind != "daily-price"
                or record.published_date != self.business_date
                or record.ticker is None
            ):
                raise ValueError("KRX snapshot contains an invalid evidence record")
            if record.ticker in tickers:
                raise ValueError("KRX snapshot contains duplicate tickers")
            tickers.add(record.ticker)
            try:
                market = KrxMarket(str(record.raw["MKT_NM"]))
            except (KeyError, ValueError, TypeError):
                raise ValueError("KRX snapshot record has an invalid market") from None
            parsed = urlparse(record.source_url)
            if (
                parsed.scheme != "https"
                or parsed.hostname != "data-dbg.krx.co.kr"
                or parsed.path != f"/svc/apis/{_ENDPOINTS[market]}"
            ):
                raise ValueError("KRX snapshot record has an invalid official endpoint")
            if market not in actual:
                raise ValueError("KRX snapshot record belongs to an incomplete market")
            actual[market] += 1
        if counts != actual:
            raise ValueError("KRX snapshot record counts do not match records")
        if (
            self.records
            and max(record.fetched_at for record in self.records) != self.fetched_at
        ):
            raise ValueError("KRX snapshot fetched_at must match latest record")

    @property
    def counts(self) -> dict[KrxMarket, int]:
        counts: dict[KrxMarket, int] = {}
        for market, count in self.record_counts:
            if market in counts:
                raise ValueError("record counts must have unique markets")
            if not isinstance(count, int):
                raise ValueError("KRX record counts must be integers")
            counts[market] = count
        return counts

    @property
    def coverage_complete(self) -> bool:
        return set(self.completed_markets) == set(self.requested_markets)

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "source": "krx",
            "collected_at": self.fetched_at.isoformat(),
            "date": self.business_date.isoformat(),
            "coverage_complete": self.coverage_complete,
            "requested_markets": [market.value for market in self.requested_markets],
            "completed_markets": [market.value for market in self.completed_markets],
            "record_counts": {
                market.value: count for market, count in self.record_counts
            },
            "records": [record.to_dict() for record in self.records],
        }

    @classmethod
    def from_payload(cls, payload: dict[str, object]) -> KrxDailySnapshot:
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") != 1
            or payload.get("source") != "krx"
        ):
            raise ValueError("invalid KRX snapshot envelope")
        requested_raw = payload.get("requested_markets")
        completed_raw = payload.get("completed_markets")
        counts_raw = payload.get("record_counts")
        records_raw = payload.get("records")
        if (
            not isinstance(requested_raw, list)
            or not isinstance(completed_raw, list)
            or not isinstance(records_raw, list)
            or not isinstance(counts_raw, dict)
        ):
            raise ValueError("invalid KRX snapshot coverage metadata")
        snapshot = cls(
            business_date=date.fromisoformat(str(payload["date"])),
            requested_markets=tuple(KrxMarket(str(value)) for value in requested_raw),
            completed_markets=tuple(KrxMarket(str(value)) for value in completed_raw),
            record_counts=tuple(
                (KrxMarket(str(market)), count) for market, count in counts_raw.items()
            ),
            records=tuple(EvidenceRecord.from_dict(record) for record in records_raw),
            fetched_at=datetime.fromisoformat(str(payload["collected_at"])),
        )
        if payload.get("coverage_complete") is not snapshot.coverage_complete:
            raise ValueError("KRX snapshot coverage_complete is inconsistent")
        return snapshot


@dataclass
class KrxClient:
    api_key: str = field(repr=False)
    transport: Transport = field(default=_default_transport, repr=False)
    clock: Clock = field(default=lambda: datetime.now().astimezone(), repr=False)
    timeout: float = 15.0
    minimum_record_counts: tuple[tuple[KrxMarket, int], ...] = MINIMUM_DAILY_RECORDS

    def __post_init__(self) -> None:
        if not self.api_key or any(char.isspace() for char in self.api_key):
            raise ValueError("KRX API key must be non-empty and contain no whitespace")
        if self.timeout <= 0:
            raise ValueError("timeout must be positive")
        minimums = dict(self.minimum_record_counts)
        if (
            len(minimums) != len(self.minimum_record_counts)
            or set(minimums) != set(KrxMarket)
            or any(
                isinstance(value, bool) or not isinstance(value, int) or value <= 0
                for value in minimums.values()
            )
        ):
            raise ValueError("KRX minimum record counts must cover every market")

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

    def daily_snapshot(
        self,
        business_date: date,
        *,
        markets: tuple[KrxMarket, ...] = (KrxMarket.KOSPI, KrxMarket.KOSDAQ),
    ) -> KrxDailySnapshot:
        requested = tuple(markets)
        started_at = self.clock()
        records = tuple(self.daily_prices(business_date, markets=requested))
        counts = tuple(
            (
                market,
                sum(
                    1 for record in records if record.raw.get("MKT_NM") == market.value
                ),
            )
            for market in requested
        )
        minimums = dict(self.minimum_record_counts)
        partial = [
            f"{market.value}={count}<{minimums[market]}"
            for market, count in counts
            if 0 < count < minimums[market]
        ]
        if partial:
            raise KrxResponseError(
                "KRX response failed minimum market cardinality: " + ", ".join(partial)
            )
        fetched_at = max(
            (record.fetched_at for record in records),
            default=started_at,
        )
        return KrxDailySnapshot(
            business_date=business_date,
            requested_markets=requested,
            completed_markets=requested,
            record_counts=counts,
            records=records,
            fetched_at=fetched_at,
        )

    def daily_prices(
        self,
        business_date: date,
        *,
        markets: tuple[KrxMarket, ...] = (KrxMarket.KOSPI, KrxMarket.KOSDAQ),
    ) -> list[EvidenceRecord]:
        if not markets:
            raise ValueError("at least one KRX market is required")
        if len(set(markets)) != len(markets):
            raise ValueError("KRX markets must be unique")
        if not all(isinstance(market, KrxMarket) for market in markets):
            raise ValueError("unsupported KRX market")
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
