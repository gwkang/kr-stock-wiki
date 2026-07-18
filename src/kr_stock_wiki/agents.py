from __future__ import annotations

from dataclasses import dataclass

from .models import Candidate, Evaluation


@dataclass(frozen=True)
class Agent:
    role: str

    def analyze(self, candidate: Candidate, evaluation: Evaluation) -> str:
        evidence = "; ".join(signal.reason for signal in candidate.signals)
        templates = {
            "market-scanner": f"복합 신호 점수 {evaluation.final_score:g}; {evidence}",
            "fundamental": "초단기 촉매가 재무 안정성과 충돌하는지 추가 확인이 필요하다.",
            "industry": "동종 종목과 업종 상대강도를 비교해야 신호의 확산 여부를 판단할 수 있다.",
            "valuation": "단기 급등 시 밸류에이션 부담이 반전 위험을 높일 수 있다.",
            "disclosure-event": "제공된 출처 URL과 발표 시각의 원문 조회가 필요하며, 조회 전에는 이벤트를 검증된 사실로 확정하지 않는다.",
            "risk-bear": "가격 반영 완료, 유동성 부족, 반대 공시 가능성을 우선 점검해야 한다.",
            "editor-in-chief": "사실과 해석을 분리하고 반대 의견을 보존한 판정 후보이며, 검증된 데이터만 최종 상태에 반영한다.",
        }
        return templates[self.role]


DEFAULT_AGENTS = tuple(
    Agent(role)
    for role in (
        "market-scanner",
        "fundamental",
        "industry",
        "valuation",
        "disclosure-event",
        "risk-bear",
        "editor-in-chief",
    )
)
