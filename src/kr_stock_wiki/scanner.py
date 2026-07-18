from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .models import Candidate, Evaluation, SignalGroup


DEFAULT_CAPS: dict[SignalGroup, float] = {
    SignalGroup.CATALYST: 25,
    SignalGroup.PRICE_VOLUME: 20,
    SignalGroup.FLOW: 15,
    SignalGroup.SECTOR: 10,
    SignalGroup.FRESHNESS: 10,
    SignalGroup.CROSS_MARKET: 10,
    SignalGroup.PROVENANCE: 10,
}


class BalancedRanker:
    def __init__(
        self,
        minimum_score: float = 40,
        caps: dict[SignalGroup, float] | None = None,
        maximum_signal_age: timedelta = timedelta(days=5),
    ):
        self.minimum_score = minimum_score
        self.caps = caps or DEFAULT_CAPS
        self.maximum_signal_age = maximum_signal_age

    def _independent_evidence_count(self, candidate: Candidate) -> int:
        signals = candidate.signals
        parents = list(range(len(signals)))

        def find(index: int) -> int:
            while parents[index] != index:
                parents[index] = parents[parents[index]]
                index = parents[index]
            return index

        def union(left: int, right: int) -> None:
            left_root, right_root = find(left), find(right)
            if left_root != right_root:
                parents[right_root] = left_root

        for left, first in enumerate(signals):
            for right in range(left + 1, len(signals)):
                second = signals[right]
                same_url = first.source_url == second.source_url
                same_event = bool(
                    first.evidence_id
                    and second.evidence_id
                    and first.evidence_id == second.evidence_id
                )
                if same_url or same_event:
                    union(left, right)
        return len({find(index) for index in range(len(signals))})

    def evaluate(
        self, candidate: Candidate, as_of: datetime | None = None
    ) -> Evaluation:
        groups = {signal.group for signal in candidate.signals}
        evidence_count = self._independent_evidence_count(candidate)
        best = {
            group: min(
                self.caps[group],
                max(
                    (s.score for s in candidate.signals if s.group == group),
                    default=0,
                ),
            )
            for group in self.caps
        }
        base = sum(best.values())
        final = max(0, base - max(0, candidate.risk_penalty))
        reasons: list[str] = []
        if len(groups) < 2:
            reasons.append("독립 신호 2개 미만")
        if evidence_count < 2:
            reasons.append("독립 근거 2개 미만")
        if final < self.minimum_score:
            reasons.append(f"최소 점수 {self.minimum_score:g} 미달")
        if candidate.hard_exclusion:
            reasons.append(f"강제 제외: {candidate.hard_exclusion}")
        if as_of is not None:
            if as_of.tzinfo is None or as_of.utcoffset() is None:
                raise ValueError("as_of must include a timezone")
            reference = as_of.astimezone(timezone.utc)
            for signal in candidate.signals:
                observed = signal.observed_at.astimezone(timezone.utc)
                if observed > reference:
                    reasons.append("미래 시점 신호")
                    break
                if reference - observed > self.maximum_signal_age:
                    reasons.append("신호 최대 허용 연령 초과")
                    break
        return Evaluation(candidate, base, final, not reasons, tuple(reasons))

    def rank(
        self,
        candidates: list[Candidate],
        candidate_limit: int = 20,
        deep_limit: int = 5,
        as_of: datetime | None = None,
    ) -> list[Evaluation]:
        evaluated = [self.evaluate(candidate, as_of=as_of) for candidate in candidates]
        qualified = [item for item in evaluated if item.qualified]
        qualified.sort(key=lambda item: (-item.final_score, item.candidate.ticker))
        return qualified[: min(candidate_limit, deep_limit)]
