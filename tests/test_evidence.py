from dataclasses import replace
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from kr_stock_wiki.evidence import (
    EvidenceRecord,
    EvidenceSource,
    VerificationStatus,
)


def _record() -> EvidenceRecord:
    return EvidenceRecord(
        source=EvidenceSource.NXT,
        evidence_id="nxt:price-snapshot:20260721:005930",
        canonical_event_id="nxt:price-snapshot:20260721:005930",
        kind="price-snapshot",
        company_name="삼성전자",
        title="NXT current price",
        source_url=(
            "https://www.nextrade.co.kr/menu/transactionStatusMain/menuList.do"
        ),
        published_date=date(2026, 7, 21),
        fetched_at=datetime(2026, 7, 21, 9, 25, tzinfo=ZoneInfo("Asia/Seoul")),
        verification=VerificationStatus.OFFICIAL,
        ticker="005930",
    )


@pytest.mark.parametrize("invalid", [0, None, "", []])
@pytest.mark.parametrize("field", ["is_correction", "is_withdrawn"])
def test_evidence_rejects_non_boolean_correction_flags(field, invalid):
    record = _record()

    with pytest.raises(ValueError, match="flags must be booleans"):
        replace(record, **{field: invalid})

    payload = record.to_dict()
    payload[field] = invalid
    with pytest.raises(ValueError, match="flags must be booleans"):
        EvidenceRecord.from_dict(payload)
