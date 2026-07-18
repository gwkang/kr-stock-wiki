from datetime import datetime
from zoneinfo import ZoneInfo

from kr_stock_wiki.harness import ResearchHarness
from kr_stock_wiki.models import Candidate, Signal, SignalGroup


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

    result = ResearchHarness().run([candidate], observed, "post-market", tmp_path)

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
        ResearchHarness().run([candidate], observed, "post-market", output)
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
        ResearchHarness().run([candidate, candidate], observed, "post-market", tmp_path)


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
        .run([candidate], observed, "pre-market", tmp_path)
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

    ResearchHarness().run([candidate], observed, "post-market", tmp_path)

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
        ResearchHarness().run([candidate], observed, "pre-market", tmp_path).reports[0]
    )

    assert report.valid_until.date().isoformat() == "2026-07-27"
