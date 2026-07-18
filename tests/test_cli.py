import json
from pathlib import Path

from kr_stock_wiki.cli import main


def test_cli_run_generates_wiki_from_json(tmp_path: Path):
    source = tmp_path / "signals.json"
    source.write_text(
        json.dumps(
            {
                "as_of": "2026-07-20T20:30:00+09:00",
                "mode": "post-market",
                "candidates": [
                    {
                        "ticker": "005930",
                        "name": "삼성전자",
                        "signals": [
                            {
                                "group": "catalyst",
                                "score": 25,
                                "reason": "공시",
                                "source_url": "https://dart.fss.or.kr/a",
                            },
                            {
                                "group": "price-volume",
                                "score": 20,
                                "reason": "거래량",
                                "source_url": "https://data.krx.co.kr/a",
                            },
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "wiki"

    code = main(["run", "--input", str(source), "--output", str(output)])

    assert code == 0
    assert (output / "Candidates.md").exists()
    assert len(list((output / "stocks").glob("*.md"))) == 1


def test_cli_reports_malformed_json_without_traceback(tmp_path: Path, capsys):
    source = tmp_path / "bad.json"
    source.write_text("{broken", encoding="utf-8")

    code = main(["run", "--input", str(source), "--output", str(tmp_path / "wiki")])

    assert code == 2
    assert "입력 오류" in capsys.readouterr().err


def test_cli_lint_returns_nonzero_for_invalid_wiki(tmp_path: Path):
    (tmp_path / "Bad.md").write_text("# no metadata", encoding="utf-8")

    assert main(["lint", "--wiki", str(tmp_path)]) == 1
