from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import yaml

from .agents import DEFAULT_AGENTS
from .models import Candidate, HarnessResult, StockReport
from .scanner import BalancedRanker


def add_trading_days(
    start: datetime, days: int, holidays: frozenset[date] = frozenset()
) -> datetime:
    current = start
    remaining = days
    while remaining:
        current += timedelta(days=1)
        if current.weekday() < 5 and current.date() not in holidays:
            remaining -= 1
    return current


def _markdown_text(value: str) -> str:
    escaped = value.replace("\\", "\\\\")
    for character in "[]*_<>#|`":
        escaped = escaped.replace(character, f"\\{character}")
    return escaped


def _document(metadata: dict[str, object], body: str) -> str:
    frontmatter = yaml.safe_dump(
        metadata, allow_unicode=True, sort_keys=False, default_flow_style=False
    ).strip()
    return f"---\n{frontmatter}\n---\n{body.rstrip()}\n"


def _support_page(
    title: str, observed_at: datetime, body: str, page_type: str = "summary"
) -> str:
    return _document(
        {
            "title": title,
            "created": observed_at.date().isoformat(),
            "updated": observed_at.date().isoformat(),
            "type": page_type,
            "tags": ["market", "short-term"],
            "sources": ["https://data.krx.co.kr/", "https://www.nextrade.co.kr/"],
            "as_of": observed_at.isoformat(),
            "confidence": "medium",
        },
        body,
    )


def _ensure_support_pages(output_dir: Path, observed_at: datetime) -> None:
    pages = {
        "Home.md": "# kr-stock-wiki\n\n- [[Candidates|후보종목]]\n- [[Methodology|분석 방법론]]",
        "Methodology.md": (
            "# 분석 방법론\n\n일반 후보는 서로 다른 신호 그룹이 최소 2개 필요하며 "
            "1~5거래일 안에 재검토합니다.\n\n- [[Home]]\n- [[Candidates]]"
        ),
    }
    for filename, body in pages.items():
        path = output_dir / filename
        if not path.exists():
            path.write_text(
                _support_page(Path(filename).stem, observed_at, body), encoding="utf-8"
            )


def _report_markdown(report: StockReport, mode: str) -> str:
    safe_name = _markdown_text(report.name)
    findings = "\n".join(
        f"### {_markdown_text(role)}\n\n{_markdown_text(text)}\n"
        for role, text in report.agent_findings.items()
    )
    sources = "\n".join(f"- <{source}>" for source in report.sources)
    dissent = "\n".join(f"- {_markdown_text(item)}" for item in report.dissent)
    body = f"""# {safe_name} ({report.ticker})

- **상태:** {report.status}
- **복합 신호 점수:** {report.score:g}
- **유효기간:** {report.valid_until.date().isoformat()}까지 (최대 5거래일)
- **연결:** [[Home]] · [[Methodology]]

## 사실과 해석

아래 의견은 자동 생성된 조사 결과이며 사실, 해석, 반대 의견을 분리해 검토해야 합니다.

{findings}
## 반대 의견과 무효화 조건

{dissent}

## 출처

{sources}

> 투자 권유가 아니며 원문과 최신 시장 데이터를 직접 확인해야 합니다.
"""
    return _document(
        {
            "title": f"{report.name} 초단기 리포트",
            "created": report.observed_at.date().isoformat(),
            "updated": report.observed_at.date().isoformat(),
            "type": "stock-report",
            "tags": ["stock", "short-term", mode],
            "sources": report.sources,
            "as_of": report.observed_at.isoformat(),
            "confidence": "medium",
        },
        body,
    )


class ResearchHarness:
    def __init__(
        self,
        ranker: BalancedRanker | None = None,
        holidays: set[date] | frozenset[date] | None = None,
    ):
        self.ranker = ranker or BalancedRanker(minimum_score=20)
        self.holidays = frozenset(holidays or ())

    def run(
        self,
        candidates: list[Candidate],
        observed_at: datetime,
        mode: str,
        output_dir: Path,
    ) -> HarnessResult:
        if mode not in {"pre-market", "post-market"}:
            raise ValueError("mode must be pre-market or post-market")
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ValueError("observed_at must include a timezone")
        tickers = [candidate.ticker for candidate in candidates]
        if len(tickers) != len(set(tickers)):
            raise ValueError("중복 종목코드는 허용되지 않습니다")
        output_dir.mkdir(parents=True, exist_ok=True)
        if output_dir.is_symlink() or any(
            path.is_symlink() for path in output_dir.rglob("*")
        ):
            raise ValueError("output tree cannot contain a symlink")
        output_root = output_dir.resolve()
        _ensure_support_pages(output_dir, observed_at)
        reports_dir = output_dir / "stocks"
        reports_dir.mkdir(exist_ok=True)
        if reports_dir.is_symlink() or output_root not in reports_dir.resolve().parents:
            raise ValueError("stocks path escaped output directory or is a symlink")
        reports: list[StockReport] = []
        paths: list[Path] = []
        for evaluation in self.ranker.rank(candidates, as_of=observed_at):
            candidate = evaluation.candidate
            findings = {
                agent.role: agent.analyze(candidate, evaluation)
                for agent in DEFAULT_AGENTS
            }
            report = StockReport(
                ticker=candidate.ticker,
                name=candidate.name,
                status="관심" if evaluation.final_score >= 60 else "관찰",
                observed_at=observed_at,
                valid_until=add_trading_days(observed_at, 5, self.holidays),
                score=evaluation.final_score,
                agent_findings=findings,
                dissent=[
                    findings["risk-bear"],
                    "촉매가 가격에 이미 반영되면 판단을 무효화한다.",
                ],
                sources=sorted({signal.source_url for signal in candidate.signals}),
            )
            report.markdown = _report_markdown(report, mode)
            path = reports_dir / (
                f"{candidate.ticker}-{observed_at.date().isoformat()}-{mode}.md"
            )
            if output_root not in path.resolve().parents:
                raise ValueError("report path escaped output directory")
            path.write_text(report.markdown, encoding="utf-8")
            reports.append(report)
            paths.append(path)

        links = (
            "\n".join(
                f"- [[stocks/{path.stem}|{_markdown_text(report.name)}]] — "
                f"{report.status}, {report.score:g}점"
                for report, path in zip(reports, paths)
            )
            or "- 기준을 통과한 종목 없음"
        )
        index_path = output_dir / "Candidates.md"
        index_path.write_text(
            _support_page(
                "후보종목",
                observed_at,
                f"# 후보종목\n\n- [[Home]] · [[Methodology]]\n\n{links}",
            ),
            encoding="utf-8",
        )
        return HarnessResult(reports, index_path, paths)
