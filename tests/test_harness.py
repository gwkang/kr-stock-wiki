from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from kr_stock_wiki.evidence import EvidenceRecord, EvidenceSource, VerificationStatus
from kr_stock_wiki.harness import ResearchHarness
from kr_stock_wiki.market_rules import ListingRisk, OperationalEvidence
from kr_stock_wiki.models import Candidate, Signal, SignalGroup


def operational_evidence(
    *candidates: Candidate, eligible: bool = True
) -> dict[str, OperationalEvidence]:
    result: dict[str, OperationalEvidence] = {}
    for candidate in candidates:
        observed = candidate.signals[0].observed_at
        analysis_date = observed.astimezone(ZoneInfo("Asia/Seoul")).date()
        price_id = f"krx:daily:KOSPI:{analysis_date:%Y%m%d}:{candidate.ticker}"
        price = EvidenceRecord(
            source=EvidenceSource.KRX,
            evidence_id=price_id,
            canonical_event_id=price_id,
            kind="daily-price",
            company_name=candidate.name,
            title="KRX 일별 시세",
            source_url="https://data-dbg.krx.co.kr/svc/apis/sto/stk_bydd_trd",
            published_date=analysis_date,
            fetched_at=observed,
            verification=VerificationStatus.OFFICIAL,
            ticker=candidate.ticker,
            metrics={
                "close": 71_000,
                "volume": 1_000_000,
                "trading_value": 100_000_000_000,
                "market_cap": 500_000_000_000,
            },
            raw={"MKT_NM": "KOSPI"},
        )
        status = None
        if eligible:
            status_id = f"kind:listing-risk:{analysis_date}:{candidate.ticker}"
            status = EvidenceRecord(
                source=EvidenceSource.KIND,
                evidence_id=status_id,
                canonical_event_id=status_id,
                kind="listing-risk-status",
                company_name=candidate.name,
                title="KIND 투자유의 상태",
                source_url="https://kind.krx.co.kr/investwarn/adminissue.do",
                published_date=analysis_date,
                fetched_at=observed,
                verification=VerificationStatus.OFFICIAL,
                ticker=candidate.ticker,
                metrics={
                    "administrative_issue": 0,
                    "trading_halt": 0,
                    "investment_warning": 0,
                },
            )
        result[candidate.ticker] = OperationalEvidence(
            candidate.ticker,
            price,
            ListingRisk(candidate.ticker, analysis_date, status),
        )
    return result


def test_harness_runs_seven_roles_and_preserves_disagreement(tmp_path):
    observed = datetime(2026, 7, 20, 20, 30, tzinfo=ZoneInfo("Asia/Seoul"))
    candidate = Candidate(
        "005930",
        "삼성전자",
        [
            Signal(
                SignalGroup.CATALYST,
                25,
                "공식 공시 촉매",
                "https://dart.fss.or.kr/example",
                observed,
            ),
            Signal(
                SignalGroup.PRICE_VOLUME,
                20,
                "거래대금 증가",
                "https://data.krx.co.kr/example",
                observed,
            ),
        ],
    )

    result = ResearchHarness().run(
        [candidate],
        observed,
        "post-market",
        tmp_path,
        operational_evidence=operational_evidence(candidate),
    )

    assert len(result.reports) == 1
    report = result.reports[0]
    assert set(report.agent_findings) == {
        "market-scanner",
        "fundamental",
        "industry",
        "valuation",
        "disclosure-event",
        "risk-bear",
        "editor-in-chief",
    }
    assert report.dissent
    assert "사실과 해석" in report.markdown
    assert "https://dart.fss.or.kr/example" in report.markdown
    assert "원문 조회가 필요" in report.agent_findings["disclosure-event"]
    assert "판정 후보" in report.agent_findings["editor-in-chief"]
    assert result.index_path.exists()
    assert (
        "[[삼성전자|stocks/005930-2026-07-20-post-market]]"
        in result.index_path.read_text(encoding="utf-8")
    )
    assert result.report_paths[0].exists()


def test_harness_rejects_symlink_inside_existing_output_tree(tmp_path):
    import pytest

    observed = datetime(2026, 7, 20, 20, 30, tzinfo=ZoneInfo("Asia/Seoul"))
    outside = tmp_path / "outside"
    outside.mkdir()
    output = tmp_path / "wiki"
    output.mkdir()
    (output / "stocks").symlink_to(outside, target_is_directory=True)
    candidate = Candidate(
        "005930",
        "삼성전자",
        [
            Signal(
                SignalGroup.CATALYST, 25, "공시", "https://dart.fss.or.kr/a", observed
            ),
            Signal(SignalGroup.FLOW, 15, "수급", "https://data.krx.co.kr/a", observed),
        ],
    )

    with pytest.raises(ValueError, match="symlink"):
        ResearchHarness().run(
            [candidate],
            observed,
            "post-market",
            output,
            operational_evidence=operational_evidence(candidate),
        )
    assert not list(outside.glob("*.md"))


def test_harness_rejects_duplicate_tickers(tmp_path):
    import pytest

    observed = datetime(2026, 7, 20, 20, 30, tzinfo=ZoneInfo("Asia/Seoul"))
    candidate = Candidate(
        "005930",
        "삼성전자",
        [
            Signal(
                SignalGroup.CATALYST, 25, "공시", "https://dart.fss.or.kr/a", observed
            ),
            Signal(SignalGroup.FLOW, 15, "수급", "https://data.krx.co.kr/a", observed),
        ],
    )

    with pytest.raises(ValueError, match="중복 종목코드"):
        ResearchHarness().run(
            [candidate, candidate],
            observed,
            "post-market",
            tmp_path,
            operational_evidence=operational_evidence(candidate),
        )


def test_harness_requires_and_applies_operational_evidence(tmp_path):
    import pytest

    observed = datetime(2026, 7, 20, 20, 30, tzinfo=ZoneInfo("Asia/Seoul"))
    candidate = Candidate(
        "005930",
        "삼성전자",
        [
            Signal(
                SignalGroup.CATALYST, 25, "공시", "https://dart.fss.or.kr/a", observed
            ),
            Signal(
                SignalGroup.PRICE_VOLUME,
                20,
                "가격",
                "https://data.krx.co.kr/a",
                observed,
            ),
        ],
    )
    with pytest.raises(ValueError, match="모든 후보"):
        ResearchHarness().run(
            [candidate],
            observed,
            "post-market",
            tmp_path / "missing",
            operational_evidence={},
        )

    result = ResearchHarness().run(
        [candidate],
        observed,
        "post-market",
        tmp_path / "excluded",
        operational_evidence=operational_evidence(candidate, eligible=False),
    )
    assert result.reports == []


def test_harness_rejects_pre_market_without_official_calendar(tmp_path):
    import pytest

    observed = datetime(2026, 7, 20, 7, 30, tzinfo=ZoneInfo("Asia/Seoul"))
    candidate = Candidate(
        "005930",
        "삼성전자",
        [
            Signal(
                SignalGroup.CATALYST, 40, "공시", "https://dart.fss.or.kr/a", observed
            )
        ],
    )

    with pytest.raises(ValueError, match="공식 KRX 당일 개장 캘린더"):
        ResearchHarness().run(
            [candidate],
            observed,
            "pre-market",
            tmp_path,
            operational_evidence=operational_evidence(candidate),
        )


def test_harness_rejects_future_operational_evidence(tmp_path):
    from dataclasses import replace

    import pytest

    observed = datetime(2026, 7, 20, 20, 30, tzinfo=ZoneInfo("Asia/Seoul"))
    candidate = Candidate(
        "005930",
        "삼성전자",
        [
            Signal(
                SignalGroup.CATALYST, 40, "공시", "https://dart.fss.or.kr/a", observed
            )
        ],
    )
    evidence = operational_evidence(candidate)[candidate.ticker]
    assert evidence.price is not None
    future = observed + timedelta(seconds=1)

    with pytest.raises(ValueError, match="KRX 가격 근거"):
        ResearchHarness().run(
            [candidate],
            observed,
            "post-market",
            tmp_path / "future-price",
            operational_evidence={
                candidate.ticker: replace(
                    evidence, price=replace(evidence.price, fetched_at=future)
                )
            },
        )

    assert evidence.listing_risk.evidence is not None
    future_risk = replace(evidence.listing_risk.evidence, fetched_at=future)
    with pytest.raises(ValueError, match="KIND 상태 근거"):
        ResearchHarness().run(
            [candidate],
            observed,
            "post-market",
            tmp_path / "future-risk",
            operational_evidence={
                candidate.ticker: replace(
                    evidence,
                    listing_risk=replace(evidence.listing_risk, evidence=future_risk),
                )
            },
        )


def test_harness_skips_exchange_holiday_when_calculating_expiry(tmp_path):
    from datetime import date

    observed = datetime(2026, 7, 16, 7, 30, tzinfo=ZoneInfo("Asia/Seoul"))
    candidate = Candidate(
        "000660",
        "SK하이닉스",
        [
            Signal(
                SignalGroup.CATALYST, 25, "실적", "https://dart.fss.or.kr/a", observed
            ),
            Signal(SignalGroup.FLOW, 15, "수급", "https://data.krx.co.kr/a", observed),
        ],
    )

    report = (
        ResearchHarness(holidays={date(2026, 7, 17)})
        .run(
            [candidate],
            observed,
            "post-market",
            tmp_path,
            operational_evidence=operational_evidence(candidate),
        )
        .reports[0]
    )

    assert report.valid_until.date().isoformat() == "2026-07-24"


def test_generated_wiki_is_self_contained_and_lint_clean(tmp_path):
    from kr_stock_wiki.wiki_lint import lint_wiki

    observed = datetime(2026, 7, 20, 20, 30, tzinfo=ZoneInfo("Asia/Seoul"))
    candidate = Candidate(
        "005930",
        "삼성전자",
        [
            Signal(
                SignalGroup.CATALYST, 25, "공시", "https://dart.fss.or.kr/a", observed
            ),
            Signal(SignalGroup.FLOW, 15, "수급", "https://data.krx.co.kr/a", observed),
        ],
    )

    ResearchHarness().run(
        [candidate],
        observed,
        "post-market",
        tmp_path,
        operational_evidence=operational_evidence(candidate),
    )

    assert (tmp_path / "Home.md").exists()
    assert (tmp_path / "Methodology.md").exists()
    assert lint_wiki(tmp_path) == []


def test_harness_marks_report_expiry_at_five_trading_days(tmp_path):
    observed = datetime(2026, 7, 20, 7, 30, tzinfo=ZoneInfo("Asia/Seoul"))
    candidate = Candidate(
        "000660",
        "SK하이닉스",
        [
            Signal(
                SignalGroup.CATALYST,
                25,
                "실적 촉매",
                "https://dart.fss.or.kr/a",
                observed,
            ),
            Signal(
                SignalGroup.FLOW, 15, "기관 수급", "https://data.krx.co.kr/a", observed
            ),
        ],
    )

    report = (
        ResearchHarness()
        .run(
            [candidate],
            observed,
            "post-market",
            tmp_path,
            operational_evidence=operational_evidence(candidate),
        )
        .reports[0]
    )

    assert report.valid_until.date().isoformat() == "2026-07-27"
