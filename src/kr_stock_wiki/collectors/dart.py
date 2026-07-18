from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Callable
from urllib.parse import urlencode, urlparse
from urllib.request import urlopen

from ..evidence import EvidenceRecord, EvidenceSource, VerificationStatus


Transport = Callable[[str, float], bytes]
Clock = Callable[[], datetime]
_CORRECTION_PREFIX_RE = re.compile(
    r"^\[(?:기재정정|첨부정정|첨부추가|변경등록|정정|Correction)\]\s*",
    re.IGNORECASE,
)


class DartError(ValueError):
    """Base error safe to show without exposing the authenticated request URL."""


class DartTransportError(DartError):
    pass


class DartResponseError(DartError):
    pass


def _default_transport(url: str, timeout: float) -> bytes:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != "opendart.fss.or.kr":
        raise DartTransportError("DART request blocked: untrusted endpoint")
    # URL scheme and host are allowlisted immediately above.
    with urlopen(url, timeout=timeout) as response:  # nosec B310
        return response.read()


@dataclass
class DartClient:
    api_key: str = field(repr=False)
    transport: Transport = field(default=_default_transport, repr=False)
    clock: Clock = field(default=lambda: datetime.now().astimezone(), repr=False)
    timeout: float = 15.0
    max_pages: int = 1000

    endpoint = "https://opendart.fss.or.kr/api/list.json"

    def __post_init__(self) -> None:
        if len(self.api_key) != 40 or any(char.isspace() for char in self.api_key):
            raise ValueError("DART API key must be 40 non-whitespace characters")
        if self.timeout <= 0:
            raise ValueError("timeout must be positive")
        if self.max_pages < 1:
            raise ValueError("max_pages must be positive")

    def _record(self, item: dict, fetched_at: datetime) -> EvidenceRecord:
        receipt = item["rcept_no"]
        evidence_id = f"dart:{receipt}"
        title = item["report_nm"].strip()
        is_correction = bool(_CORRECTION_PREFIX_RE.match(title)) or "정" in item.get(
            "rm", ""
        )
        return EvidenceRecord(
            source=EvidenceSource.DART,
            evidence_id=evidence_id,
            canonical_event_id=evidence_id,
            kind="disclosure",
            company_name=item["corp_name"],
            title=title,
            source_url="https://dart.fss.or.kr/dsaf001/main.do?rcpNo=" + receipt,
            published_date=datetime.strptime(item["rcept_dt"], "%Y%m%d").date(),
            fetched_at=fetched_at,
            verification=VerificationStatus.OFFICIAL,
            ticker=item.get("stock_code") or None,
            is_correction=is_correction,
            is_withdrawn="철" in item.get("rm", ""),
            raw=dict(item),
        )

    def search(
        self, begin: date, end: date, *, corp_code: str | None = None
    ) -> list[EvidenceRecord]:
        if begin > end:
            raise ValueError("begin date cannot be after end date")
        if corp_code is None:
            month_span = (end.year - begin.year) * 12 + end.month - begin.month
            if month_span > 3 or (month_span == 3 and end.day > begin.day):
                raise ValueError(
                    "market-wide DART search range cannot exceed three months"
                )
        if corp_code is not None and (len(corp_code) != 8 or not corp_code.isdigit()):
            raise ValueError("corp_code must be eight digits")

        records: list[EvidenceRecord] = []
        seen_evidence_ids: set[str] = set()
        page = 1
        while True:
            params: dict[str, str | int] = {
                "crtfc_key": self.api_key,
                "bgn_de": begin.strftime("%Y%m%d"),
                "end_de": end.strftime("%Y%m%d"),
                "sort": "date",
                "sort_mth": "desc",
                "page_no": page,
                "page_count": 100,
            }
            if corp_code is not None:
                params["corp_code"] = corp_code
            request_url = f"{self.endpoint}?{urlencode(params)}"
            try:
                raw_payload = self.transport(request_url, self.timeout)
            except (OSError, TimeoutError):
                raise DartTransportError("DART request failed") from None
            try:
                payload = json.loads(raw_payload)
            except (json.JSONDecodeError, UnicodeError) as error:
                raise DartResponseError("DART returned invalid JSON") from error
            if not isinstance(payload, dict):
                raise DartResponseError("DART response must be a JSON object")
            status = payload.get("status")
            if status == "013":
                break
            if status != "000":
                raise ValueError(f"DART API error {status}: {payload.get('message')}")
            try:
                response_page = int(payload.get("page_no", page))
            except (TypeError, ValueError) as error:
                raise DartResponseError("DART response has invalid page_no") from error
            if response_page != page:
                raise DartResponseError(
                    f"DART page mismatch: requested {page}, received {response_page}"
                )
            fetched_at = self.clock()
            for item in payload.get("list", []):
                record = self._record(item, fetched_at)
                if record.evidence_id not in seen_evidence_ids:
                    seen_evidence_ids.add(record.evidence_id)
                    records.append(record)
            total_pages = int(payload.get("total_page", 1))
            if total_pages > self.max_pages:
                raise ValueError("DART response exceeds configured max_pages")
            if page >= total_pages:
                break
            page += 1
        return records
