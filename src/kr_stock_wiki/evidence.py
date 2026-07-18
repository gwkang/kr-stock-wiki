from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from typing import Any
from urllib.parse import urlparse


class EvidenceSource(StrEnum):
    DART = "dart"
    KRX = "krx"
    KIND = "kind"
    NXT = "nxt"
    OFFICIAL_NEWS = "official-news"


class VerificationStatus(StrEnum):
    OFFICIAL = "official"
    UNVERIFIED = "unverified"


@dataclass(frozen=True)
class EvidenceRecord:
    source: EvidenceSource
    evidence_id: str
    canonical_event_id: str
    kind: str
    company_name: str
    title: str
    source_url: str
    published_date: date
    fetched_at: datetime
    verification: VerificationStatus
    ticker: str | None = None
    delay_minutes: int | None = None
    is_correction: bool = False
    is_withdrawn: bool = False
    metrics: dict[str, int | float | str | None] = field(
        default_factory=dict, repr=False
    )
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source.value,
            "evidence_id": self.evidence_id,
            "canonical_event_id": self.canonical_event_id,
            "kind": self.kind,
            "company_name": self.company_name,
            "title": self.title,
            "source_url": self.source_url,
            "published_date": self.published_date.isoformat(),
            "fetched_at": self.fetched_at.isoformat(),
            "verification": self.verification.value,
            "ticker": self.ticker,
            "delay_minutes": self.delay_minutes,
            "is_correction": self.is_correction,
            "is_withdrawn": self.is_withdrawn,
            "metrics": dict(self.metrics),
            "raw": dict(self.raw),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> EvidenceRecord:
        if not isinstance(payload, dict):
            raise ValueError("evidence payload must be a JSON object")
        metrics = payload.get("metrics")
        raw = payload.get("raw")
        if not isinstance(metrics, dict) or not isinstance(raw, dict):
            raise ValueError("evidence metrics and raw must be JSON objects")
        return cls(
            source=EvidenceSource(payload["source"]),
            evidence_id=payload["evidence_id"],
            canonical_event_id=payload["canonical_event_id"],
            kind=payload["kind"],
            company_name=payload["company_name"],
            title=payload["title"],
            source_url=payload["source_url"],
            published_date=date.fromisoformat(payload["published_date"]),
            fetched_at=datetime.fromisoformat(payload["fetched_at"]),
            verification=VerificationStatus(payload["verification"]),
            ticker=payload.get("ticker"),
            delay_minutes=payload.get("delay_minutes"),
            is_correction=payload.get("is_correction", False),
            is_withdrawn=payload.get("is_withdrawn", False),
            metrics=dict(metrics),
            raw=dict(raw),
        )

    def __post_init__(self) -> None:
        if self.fetched_at.tzinfo is None or self.fetched_at.utcoffset() is None:
            raise ValueError("fetched_at must include a timezone")
        parsed = urlparse(self.source_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("source_url must be a valid HTTP(S) URL")
        if self.delay_minutes is not None and (
            isinstance(self.delay_minutes, bool)
            or not isinstance(self.delay_minutes, int)
            or self.delay_minutes < 0
        ):
            raise ValueError("delay_minutes must be a non-negative integer")
        if self.ticker is not None and (
            len(self.ticker) != 6
            or not self.ticker.isascii()
            or not self.ticker.isalnum()
            or self.ticker != self.ticker.upper()
        ):
            raise ValueError(
                "ticker must be six uppercase ASCII alphanumeric characters"
            )
        for name, value in self.metrics.items():
            if not isinstance(name, str) or not name:
                raise ValueError("metric names must be non-empty strings")
            if isinstance(value, bool) or not isinstance(
                value, (int, float, str, type(None))
            ):
                raise ValueError("metric values must be JSON scalar values")
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError("numeric metric values must be finite")
