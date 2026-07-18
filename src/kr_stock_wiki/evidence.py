from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from typing import Any
from urllib.parse import urlparse


class EvidenceSource(StrEnum):
    DART = "dart"
    KRX = "krx"
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
            "raw": dict(self.raw),
        }

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
            len(self.ticker) != 6 or not self.ticker.isdigit()
        ):
            raise ValueError("ticker must be six digits")
