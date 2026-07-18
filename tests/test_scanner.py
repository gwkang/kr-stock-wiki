from datetime import datetime, timezone

from kr_stock_wiki.models import Candidate, Signal, SignalGroup
from kr_stock_wiki.scanner import BalancedRanker


def signal(group: SignalGroup, score: float, source: str | None = None) -> Signal:
    return Signal(
        group=group,
        score=score,
        reason=f"{group.value} evidence",
        source_url=source or f"https://example.com/source/{group.value}",
        observed_at=datetime(2026, 7, 18, tzinfo=timezone.utc),
    )


def test_candidate_requires_two_independent_signal_groups():
    candidate = Candidate(
        "005930",
        "삼성전자",
        [
            signal(SignalGroup.CATALYST, 20),
            signal(SignalGroup.CATALYST, 18, "https://example.com/other"),
        ],
    )

    result = BalancedRanker().evaluate(candidate)

    assert result.qualified is False
    assert "독립 신호 2개 미만" in result.reasons


def test_balanced_score_uses_group_caps_and_risk_penalty():
    candidate = Candidate(
        "005930",
        "삼성전자",
        [
            signal(SignalGroup.CATALYST, 25),
            signal(SignalGroup.PRICE_VOLUME, 20),
            signal(SignalGroup.FLOW, 15),
            signal(SignalGroup.SECTOR, 10),
            signal(SignalGroup.FRESHNESS, 10),
            signal(SignalGroup.CROSS_MARKET, 10),
            signal(SignalGroup.PROVENANCE, 10),
        ],
        risk_penalty=12,
    )

    result = BalancedRanker().evaluate(candidate)

    assert result.qualified is True
    assert result.base_score == 100
    assert result.final_score == 88


def test_same_url_is_duplicate_even_when_only_one_signal_has_evidence_id():
    observed = datetime(2026, 7, 18, tzinfo=timezone.utc)
    candidate = Candidate(
        "005930",
        "삼성전자",
        [
            Signal(
                SignalGroup.CATALYST,
                25,
                "사건",
                "https://example.com/same",
                observed,
                evidence_id="event-1",
            ),
            Signal(
                SignalGroup.FLOW, 15, "시장 반응", "https://example.com/same", observed
            ),
        ],
    )

    result = BalancedRanker().evaluate(candidate)

    assert result.qualified is False
    assert "독립 근거 2개 미만" in result.reasons


def test_same_evidence_cannot_count_as_independent_across_groups():
    candidate = Candidate(
        "005930",
        "삼성전자",
        [
            Signal(
                SignalGroup.CATALYST,
                25,
                "동일 사건",
                "https://example.com/same",
                datetime(2026, 7, 18, tzinfo=timezone.utc),
                evidence_id="event-1",
            ),
            Signal(
                SignalGroup.FLOW,
                15,
                "동일 사건의 시장 반응",
                "https://example.com/same",
                datetime(2026, 7, 18, tzinfo=timezone.utc),
                evidence_id="event-1",
            ),
        ],
    )

    result = BalancedRanker().evaluate(candidate)

    assert result.qualified is False
    assert "독립 근거 2개 미만" in result.reasons


def test_candidate_rejects_non_finite_risk_penalty():
    import pytest

    with pytest.raises(ValueError, match="유한"):
        Candidate("005930", "삼성전자", [], risk_penalty=float("nan"))


def test_signal_rejects_non_finite_score():
    import pytest

    with pytest.raises(ValueError, match="finite"):
        Signal(
            SignalGroup.CATALYST,
            float("inf"),
            "신호",
            "https://example.com/a",
            datetime(2026, 7, 18, tzinfo=timezone.utc),
        )


def test_candidate_accepts_current_alphanumeric_krx_short_code():
    candidate = Candidate("0126Z0", "삼성에피스홀딩스", [])

    assert candidate.ticker == "0126Z0"


def test_candidate_rejects_path_traversal_ticker():
    import pytest

    with pytest.raises(ValueError, match="6자리 대문자 영숫자"):
        Candidate("../../escaped", "위험종목", [])


def test_signal_rejects_untrusted_multiline_content():
    import pytest

    with pytest.raises(ValueError, match="개행"):
        signal(SignalGroup.CATALYST, 10, "https://example.com/a\n---")


def test_rank_excludes_stale_and_future_signals():
    from datetime import timedelta

    now = datetime(2026, 7, 18, tzinfo=timezone.utc)
    stale = Candidate(
        "005930",
        "삼성전자",
        [
            signal(SignalGroup.CATALYST, 25),
            signal(SignalGroup.PRICE_VOLUME, 20),
        ],
    )
    stale.signals = [
        Signal(s.group, s.score, s.reason, s.source_url, now - timedelta(days=10))
        for s in stale.signals
    ]
    future = Candidate(
        "000660",
        "SK하이닉스",
        [
            Signal(
                SignalGroup.CATALYST,
                25,
                "촉매",
                "https://example.com/a",
                now + timedelta(minutes=1),
            ),
            Signal(SignalGroup.FLOW, 15, "수급", "https://example.com/b", now),
        ],
    )

    assert BalancedRanker().evaluate(stale, as_of=now).qualified is False
    assert BalancedRanker().evaluate(future, as_of=now).qualified is False


def test_rank_limits_deep_analysis_to_five_without_padding():
    candidates = [
        Candidate(
            f"00000{i}",
            f"종목{i}",
            [
                signal(SignalGroup.CATALYST, 15 + i),
                signal(SignalGroup.PRICE_VOLUME, 12 + i),
            ],
        )
        for i in range(8)
    ]

    ranked = BalancedRanker(minimum_score=20).rank(candidates, deep_limit=5)

    assert len(ranked) == 5
    assert [item.candidate.name for item in ranked] == [
        "종목7",
        "종목6",
        "종목5",
        "종목4",
        "종목3",
    ]
