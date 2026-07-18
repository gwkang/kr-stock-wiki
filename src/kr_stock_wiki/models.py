from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
import math
import re
from urllib.parse import urlparse


class SignalGroup(StrEnum):
    CATALYST = "catalyst"
    PRICE_VOLUME = "price-volume"
    FLOW = "flow"
    SECTOR = "sector"
    FRESHNESS = "freshness"
    CROSS_MARKET = "cross-market"
    PROVENANCE = "provenance"


@dataclass(frozen=True)
class Signal:
    group: SignalGroup
    score: float
    reason: str
    source_url: str
    observed_at: datetime
    evidence_id: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.score, bool) or not math.isfinite(self.score):
            raise ValueError("signal score must be finite")
        if not 0 <= self.score <= 100:
            raise ValueError("signal score must be between 0 and 100")
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("signal observed_at must include a timezone")
        if any(char in self.reason for char in "\r\n") or any(
            ord(char) < 32 for char in self.reason
        ):
            raise ValueError("signal reason에 개행 또는 제어문자를 사용할 수 없습니다")
        if any(char in self.source_url for char in "\r\n"):
            raise ValueError("source URL에 개행을 사용할 수 없습니다")
        parsed = urlparse(self.source_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("signal requires a valid HTTP source URL")
        if self.evidence_id is not None and (
            not self.evidence_id
            or len(self.evidence_id) > 128
            or any(char in self.evidence_id for char in "\r\n")
        ):
            raise ValueError("evidence_id는 개행 없는 1~128자여야 합니다")


@dataclass
class Candidate:
    ticker: str
    name: str
    signals: list[Signal] = field(default_factory=list)
    risk_penalty: float = 0
    hard_exclusion: str | None = None

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[0-9A-Z]{6}", self.ticker):
            raise ValueError("ticker는 6자리 대문자 영숫자여야 합니다")
        if (
            not self.name
            or len(self.name) > 100
            or any(char in self.name for char in "\r\n")
        ):
            raise ValueError("name은 개행 없는 1~100자여야 합니다")
        if isinstance(self.risk_penalty, bool) or not math.isfinite(self.risk_penalty):
            raise ValueError("risk_penalty는 유한한 숫자여야 합니다")
        if self.risk_penalty < 0:
            raise ValueError("risk_penalty는 음수일 수 없습니다")


@dataclass(frozen=True)
class Evaluation:
    candidate: Candidate
    base_score: float
    final_score: float
    qualified: bool
    reasons: tuple[str, ...]


@dataclass
class StockReport:
    ticker: str
    name: str
    status: str
    observed_at: datetime
    valid_until: datetime
    score: float
    agent_findings: dict[str, str]
    dissent: list[str]
    sources: list[str]
    markdown: str = ""


@dataclass
class HarnessResult:
    reports: list[StockReport]
    index_path: Path
    report_paths: list[Path]
