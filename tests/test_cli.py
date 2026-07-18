import json
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from kr_stock_wiki.cli import main
from kr_stock_wiki.evidence import EvidenceRecord, EvidenceSource, VerificationStatus


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


def test_collect_dart_writes_versioned_snapshot(tmp_path: Path, monkeypatch):
    record = EvidenceRecord(
        source=EvidenceSource.DART,
        evidence_id="dart:20260718000123",
        canonical_event_id="dart:20260718000123",
        kind="disclosure",
        company_name="Example",
        title="Major Event Report",
        source_url="https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20260718000123",
        published_date=date(2026, 7, 18),
        fetched_at=datetime(2026, 7, 18, 20, 30, tzinfo=ZoneInfo("Asia/Seoul")),
        verification=VerificationStatus.OFFICIAL,
        ticker="005930",
    )

    def search(_client, begin, end, *, corp_code=None):
        assert begin == end == date(2026, 7, 18)
        assert corp_code == "00126380"
        return [record]

    monkeypatch.setenv("DART_API_KEY", "k" * 40)
    monkeypatch.setattr("kr_stock_wiki.cli.DartClient.search", search)
    output = tmp_path / "nested" / "dart.json"

    code = main(
        [
            "collect-dart",
            "--begin",
            "2026-07-18",
            "--end",
            "2026-07-18",
            "--corp-code",
            "00126380",
            "--output",
            str(output),
        ]
    )

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert code == 0
    assert payload["schema_version"] == 1
    assert datetime.fromisoformat(payload["collected_at"]).tzinfo is not None
    assert payload["records"][0]["evidence_id"] == record.evidence_id
    assert "k" * 40 not in output.read_text(encoding="utf-8")
    assert not output.with_suffix(".json.tmp").exists()


def test_collect_dart_requires_environment_key(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.delenv("DART_API_KEY", raising=False)
    output = tmp_path / "dart.json"

    code = main(
        [
            "collect-dart",
            "--begin",
            "2026-07-18",
            "--end",
            "2026-07-18",
            "--output",
            str(output),
        ]
    )

    assert code == 2
    assert "DART_API_KEY" in capsys.readouterr().err
    assert not output.exists()


def test_cli_reports_malformed_json_without_traceback(tmp_path: Path, capsys):
    source = tmp_path / "bad.json"
    source.write_text("{broken", encoding="utf-8")

    code = main(["run", "--input", str(source), "--output", str(tmp_path / "wiki")])

    assert code == 2
    assert "입력 오류" in capsys.readouterr().err


def test_cli_lint_returns_nonzero_for_invalid_wiki(tmp_path: Path):
    (tmp_path / "Bad.md").write_text("# no metadata", encoding="utf-8")

    assert main(["lint", "--wiki", str(tmp_path)]) == 1
