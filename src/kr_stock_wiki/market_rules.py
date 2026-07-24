from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from enum import StrEnum
from urllib.parse import urlparse

from .collectors.krx import (
    MINIMUM_DAILY_RECORDS,
    KrxDailySnapshot,
    KrxMarket,
)
from .evidence import EvidenceRecord, EvidenceSource, VerificationStatus
from .models import Candidate


class MarketDayStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class MarketDayDecision:
    business_date: date
    status: MarketDayStatus
    reason: str
    markets: tuple[str, ...] = ()


class TradingDayGate:
    def __init__(
        self,
        required_markets: tuple[KrxMarket, ...] = (
            KrxMarket.KOSPI,
            KrxMarket.KOSDAQ,
        ),
        minimum_record_counts: tuple[
            tuple[KrxMarket, int], ...
        ] = MINIMUM_DAILY_RECORDS,
    ):
        if not required_markets or len(set(required_markets)) != len(required_markets):
            raise ValueError("required_markets must be non-empty and unique")
        if not all(isinstance(market, KrxMarket) for market in required_markets):
            raise ValueError("required_markets must contain KRX markets")
        minimums = dict(minimum_record_counts)
        if (
            len(minimums) != len(minimum_record_counts)
            or not set(required_markets) <= set(minimums)
            or any(
                isinstance(value, bool) or not isinstance(value, int) or value <= 0
                for value in minimums.values()
            )
        ):
            raise ValueError("minimum record counts must cover required markets")
        self.required_markets = required_markets
        self.minimum_record_counts = minimums

    def assess(
        self,
        business_date: date,
        snapshot: KrxDailySnapshot | None,
    ) -> MarketDayDecision:
        if business_date.weekday() >= 5:
            if snapshot is not None and snapshot.records:
                raise ValueError("weekend cannot contain KRX daily-price records")
            return MarketDayDecision(business_date, MarketDayStatus.CLOSED, "주말 휴장")

        if snapshot is None:
            return MarketDayDecision(
                business_date,
                MarketDayStatus.UNKNOWN,
                "공식 KRX 스냅샷 없음: 휴장과 수집 실패를 구분할 수 없음",
            )
        if snapshot.business_date != business_date:
            raise ValueError("KRX snapshot business date does not match requested date")

        required = set(self.required_markets)
        completed = set(snapshot.completed_markets)
        missing = sorted(market.value for market in required - completed)
        if not snapshot.coverage_complete or missing:
            return MarketDayDecision(
                business_date,
                MarketDayStatus.UNKNOWN,
                "공식 KRX 시장 스냅샷 누락: " + ", ".join(missing),
                tuple(sorted(market.value for market in completed & required)),
            )
        counts = snapshot.counts
        empty = sorted(
            market.value
            for market in self.required_markets
            if counts.get(market, 0) == 0
        )
        observed = tuple(
            sorted(
                market.value
                for market in self.required_markets
                if counts.get(market, 0) > 0
            )
        )
        if empty:
            return MarketDayDecision(
                business_date,
                MarketDayStatus.UNKNOWN,
                "공식 KRX 완전 스냅샷이나 거래 레코드 없음: " + ", ".join(empty),
                observed,
            )
        below_minimum = sorted(
            f"{market.value}={counts[market]}<{self.minimum_record_counts[market]}"
            for market in self.required_markets
            if counts[market] < self.minimum_record_counts[market]
        )
        if below_minimum:
            return MarketDayDecision(
                business_date,
                MarketDayStatus.UNKNOWN,
                "공식 KRX 시장 cardinality 하한 미달: " + ", ".join(below_minimum),
                observed,
            )
        return MarketDayDecision(
            business_date,
            MarketDayStatus.OPEN,
            "공식 KRX 양 시장 완전 스냅샷 확인",
            observed,
        )


@dataclass(frozen=True)
class ListingRisk:
    ticker: str
    as_of: date
    evidence: EvidenceRecord | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.ticker, str)
            or len(self.ticker) != 6
            or not self.ticker.isascii()
            or not self.ticker.isalnum()
            or self.ticker != self.ticker.upper()
        ):
            raise ValueError("listing risk ticker must be a valid six-character code")
        if self.evidence is None:
            return
        parsed = urlparse(self.evidence.source_url)
        if (
            self.evidence.source is not EvidenceSource.KIND
            or self.evidence.verification is not VerificationStatus.OFFICIAL
            or self.evidence.kind != "listing-risk-status"
            or parsed.scheme != "https"
            or parsed.hostname != "kind.krx.co.kr"
        ):
            raise ValueError("listing risk evidence must be official KIND status")
        if self.evidence.ticker != self.ticker:
            raise ValueError("listing risk evidence ticker mismatch")
        if self.evidence.published_date != self.as_of:
            raise ValueError("listing risk evidence as-of date mismatch")
        for name in (
            "administrative_issue",
            "trading_halt",
            "investment_warning",
        ):
            value = self.evidence.metrics.get(name)
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value not in {0, 1}
            ):
                raise ValueError(f"listing risk metric {name} must be integer 0 or 1")

    def _status(self, name: str) -> bool | None:
        if self.evidence is None:
            return None
        return self.evidence.metrics[name] == 1

    @property
    def administrative_issue(self) -> bool | None:
        return self._status("administrative_issue")

    @property
    def trading_halt(self) -> bool | None:
        return self._status("trading_halt")

    @property
    def investment_warning(self) -> bool | None:
        return self._status("investment_warning")


@dataclass(frozen=True)
class OperationalEvidence:
    ticker: str
    price: EvidenceRecord | None
    listing_risk: ListingRisk

    def __post_init__(self) -> None:
        if (
            not isinstance(self.ticker, str)
            or len(self.ticker) != 6
            or not self.ticker.isascii()
            or not self.ticker.isalnum()
            or self.ticker != self.ticker.upper()
        ):
            raise ValueError(
                "operational evidence ticker must be six uppercase characters"
            )
        if self.price is not None and self.price.ticker != self.ticker:
            raise ValueError("operational price evidence ticker mismatch")
        if self.listing_risk.ticker != self.ticker:
            raise ValueError("operational listing risk ticker mismatch")


@dataclass(frozen=True)
class OperationalDecision:
    ticker: str
    eligible: bool
    reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.ticker, str)
            or len(self.ticker) != 6
            or not self.ticker.isascii()
            or not self.ticker.isalnum()
            or self.ticker != self.ticker.upper()
        ):
            raise ValueError("operational decision ticker must be valid")
        if not isinstance(self.eligible, bool):
            raise ValueError("operational decision eligible must be boolean")
        if not isinstance(self.reasons, tuple) or any(
            not isinstance(reason, str) or not reason.strip() for reason in self.reasons
        ):
            raise ValueError("operational decision reasons must be non-empty strings")
        if self.eligible == bool(self.reasons):
            raise ValueError(
                "eligible decisions must have no reasons and exclusions need reasons"
            )


def apply_operational_decision(
    candidate: Candidate, decision: OperationalDecision
) -> Candidate:
    if candidate.ticker != decision.ticker:
        raise ValueError("candidate and operational decision ticker must match")
    if decision.eligible:
        return replace(candidate, signals=list(candidate.signals))
    exclusion = "운영 필터: " + "; ".join(decision.reasons)
    if candidate.hard_exclusion:
        exclusion = f"{candidate.hard_exclusion}; {exclusion}"
    return replace(
        candidate,
        signals=list(candidate.signals),
        hard_exclusion=exclusion,
    )


class OperationalFilter:
    def __init__(
        self,
        *,
        minimum_close: int = 1_000,
        minimum_volume: int = 100_000,
        minimum_trading_value: int = 5_000_000_000,
        minimum_market_cap: int = 100_000_000_000,
    ):
        thresholds = {
            "minimum_close": minimum_close,
            "minimum_volume": minimum_volume,
            "minimum_trading_value": minimum_trading_value,
            "minimum_market_cap": minimum_market_cap,
        }
        for name, value in thresholds.items():
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        self.minimum_close = minimum_close
        self.minimum_volume = minimum_volume
        self.minimum_trading_value = minimum_trading_value
        self.minimum_market_cap = minimum_market_cap

    @staticmethod
    def _metric(record: EvidenceRecord, name: str) -> int:
        value = record.metrics.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"KRX metric {name} must be a non-negative integer")
        return value

    def evaluate(
        self,
        record: EvidenceRecord,
        listing_risk: ListingRisk,
        *,
        analysis_date: date | None = None,
    ) -> OperationalDecision:
        parsed = urlparse(record.source_url)
        market = record.raw.get("MKT_NM")
        expected_paths = {
            "KOSPI": "/svc/apis/sto/stk_bydd_trd",
            "KOSDAQ": "/svc/apis/sto/ksq_bydd_trd",
        }
        krx_valid = (
            record.source is EvidenceSource.KRX
            and parsed.hostname == "data-dbg.krx.co.kr"
            and market in expected_paths
            and parsed.path == expected_paths[market]
        )
        kis_valid = (
            record.source is EvidenceSource.KIS
            and parsed.hostname == "openapi.koreainvestment.com"
            and parsed.port == 9443
            and parsed.path
            == "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
        )
        if (
            record.kind != "daily-price"
            or record.verification is not VerificationStatus.OFFICIAL
            or record.ticker is None
            or parsed.scheme != "https"
            or not (krx_valid or kis_valid)
        ):
            raise ValueError(
                "operational filter requires official KRX daily-price or KIS daily-price"
            )

        if listing_risk.ticker != record.ticker:
            raise ValueError("listing risk and KRX price ticker must match")
        expected_status_date = analysis_date or record.published_date
        if listing_risk.as_of != expected_status_date:
            raise ValueError("listing risk as-of date must match analysis date")

        close = self._metric(record, "close")
        volume = self._metric(record, "volume")
        trading_value = self._metric(record, "trading_value")
        market_cap = self._metric(record, "market_cap")
        reasons: list[str] = []
        if close < self.minimum_close:
            reasons.append(f"종가 {self.minimum_close:,}원 미만")
        if volume < self.minimum_volume:
            reasons.append(f"거래량 {self.minimum_volume:,}주 미만")
        if trading_value < self.minimum_trading_value:
            reasons.append(f"거래대금 {self.minimum_trading_value:,}원 미만")
        if market_cap < self.minimum_market_cap:
            reasons.append(f"시가총액 {self.minimum_market_cap:,}원 미만")

        risk_fields = (
            (listing_risk.administrative_issue, "관리종목"),
            (listing_risk.trading_halt, "거래정지"),
            (listing_risk.investment_warning, "투자경고"),
        )
        for value, label in risk_fields:
            if value is None:
                reasons.append(f"{label} 여부 미확인")
            elif value:
                reasons.append(label)
        return OperationalDecision(record.ticker, not reasons, tuple(reasons))
