import json
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from kr_stock_wiki.cli import (
    _load_krx_snapshot,
    _load_listing_risks,
    _operational_evidence,
    main,
)
from kr_stock_wiki.collectors.calendar import (
    CALENDAR_SOURCE_URL,
    KrxMarketCalendar,
    MarketHoliday,
)
from kr_stock_wiki.collectors.market_notices import (
    KrxMarketNotice,
    KrxMarketNoticeSnapshot,
)
from kr_stock_wiki.collectors.krx import KrxDailySnapshot, KrxMarket
from kr_stock_wiki.evidence import EvidenceRecord, EvidenceSource, VerificationStatus


def write_operational_snapshots(
    tmp_path: Path,
    *,
    business_date: date,
    analysis_date: date,
    administrative_issue: int = 0,
) -> tuple[Path, Path]:
    fetched_at = datetime(2026, 7, 20, 20, 30, tzinfo=ZoneInfo("Asia/Seoul"))

    def price(ticker: str, market: KrxMarket) -> EvidenceRecord:
        evidence_id = (
            f"krx:daily:{market.value}:{business_date.strftime('%Y%m%d')}:{ticker}"
        )
        return EvidenceRecord(
            source=EvidenceSource.KRX,
            evidence_id=evidence_id,
            canonical_event_id=evidence_id,
            kind="daily-price",
            company_name=ticker,
            title="KRX 일별 시세",
            source_url=(
                "https://data-dbg.krx.co.kr/svc/apis/sto/"
                + ("stk_bydd_trd" if market is KrxMarket.KOSPI else "ksq_bydd_trd")
            ),
            published_date=business_date,
            fetched_at=fetched_at,
            verification=VerificationStatus.OFFICIAL,
            ticker=ticker,
            metrics={
                "close": 71_000,
                "volume": 1_000_000,
                "trading_value": 100_000_000_000,
                "market_cap": 500_000_000_000,
            },
            raw={"MKT_NM": market.value},
        )

    prices = (
        price("005930", KrxMarket.KOSPI),
        *(price(f"{600000 + index:06d}", KrxMarket.KOSPI) for index in range(499)),
        price("247540", KrxMarket.KOSDAQ),
        *(price(f"{700000 + index:06d}", KrxMarket.KOSDAQ) for index in range(999)),
    )
    krx = KrxDailySnapshot(
        business_date=business_date,
        requested_markets=(KrxMarket.KOSPI, KrxMarket.KOSDAQ),
        completed_markets=(KrxMarket.KOSPI, KrxMarket.KOSDAQ),
        record_counts=((KrxMarket.KOSPI, 500), (KrxMarket.KOSDAQ, 1_000)),
        records=prices,
        fetched_at=fetched_at,
    )
    krx_path = tmp_path / "krx-snapshot.json"
    krx_path.write_text(json.dumps(krx.to_payload()), encoding="utf-8")

    status_id = f"kind:listing-risk:{analysis_date.isoformat()}:005930"
    status = EvidenceRecord(
        source=EvidenceSource.KIND,
        evidence_id=status_id,
        canonical_event_id=status_id,
        kind="listing-risk-status",
        company_name="삼성전자",
        title="KRX KIND 투자유의 상태",
        source_url="https://kind.krx.co.kr/investwarn/adminissue.do",
        published_date=analysis_date,
        fetched_at=fetched_at,
        verification=VerificationStatus.OFFICIAL,
        ticker="005930",
        metrics={
            "administrative_issue": administrative_issue,
            "trading_halt": 0,
            "investment_warning": 0,
        },
    )
    kind_path = tmp_path / "kind-status.json"
    kind_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source": "kind",
                "coverage_complete": True,
                "collected_at": fetched_at.isoformat(),
                "date": analysis_date.isoformat(),
                "requested_tickers": ["005930"],
                "completed_tickers": ["005930"],
                "records": [status.to_dict()],
            }
        ),
        encoding="utf-8",
    )
    return krx_path, kind_path


def test_operational_snapshots_reject_lookahead_timestamps(tmp_path: Path):
    import pytest

    business_date = date(2026, 7, 20)
    observed = datetime(2026, 7, 20, 20, 30, tzinfo=ZoneInfo("Asia/Seoul"))
    krx_path, kind_path = write_operational_snapshots(
        tmp_path,
        business_date=business_date,
        analysis_date=business_date,
    )

    kind_payload = json.loads(kind_path.read_text(encoding="utf-8"))
    future = datetime(2026, 7, 20, 20, 30, 1, tzinfo=ZoneInfo("Asia/Seoul"))
    kind_payload["collected_at"] = future.isoformat()
    kind_payload["records"][0]["fetched_at"] = future.isoformat()
    kind_path.write_text(json.dumps(kind_payload), encoding="utf-8")
    with pytest.raises(ValueError, match="within one hour before analysis"):
        _load_listing_risks(
            kind_path,
            analysis_time=observed,
            candidate_tickers={"005930"},
        )

    krx_payload = json.loads(krx_path.read_text(encoding="utf-8"))
    krx_payload["collected_at"] = future.isoformat()
    for record in krx_payload["records"]:
        record["fetched_at"] = future.isoformat()
    krx_path.write_text(json.dumps(krx_payload), encoding="utf-8")
    with pytest.raises(ValueError, match="within 12 hours before analysis"):
        _operational_evidence(
            [],
            observed=observed,
            business_date=business_date,
            krx_snapshot=_load_krx_snapshot(krx_path),
            listing_risks={},
        )


def test_cli_run_generates_wiki_from_json(tmp_path: Path):
    source = tmp_path / "signals.json"
    source.write_text(
        json.dumps(
            {
                "as_of": "2026-07-20T20:30:00+09:00",
                "business_date": "2026-07-20",
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
    krx_snapshot, kind_status = write_operational_snapshots(
        tmp_path,
        business_date=date(2026, 7, 20),
        analysis_date=date(2026, 7, 20),
    )

    code = main(
        [
            "run",
            "--input",
            str(source),
            "--krx-snapshot",
            str(krx_snapshot),
            "--kind-status",
            str(kind_status),
            "--output",
            str(output),
        ]
    )

    assert code == 0
    assert (output / "Candidates.md").exists()
    assert len(list((output / "stocks").glob("*.md"))) == 1


def test_collect_news_writes_official_rss_snapshot(tmp_path: Path, monkeypatch):
    record = EvidenceRecord(
        source=EvidenceSource.OFFICIAL_NEWS,
        evidence_id="yonhap:AKR20260718000100001",
        canonical_event_id="yonhap:AKR20260718000100001",
        kind="news-article",
        company_name="연합뉴스",
        title="반도체 수출 증가",
        source_url="https://www.yna.co.kr/view/AKR20260718000100001",
        published_date=date(2026, 7, 18),
        fetched_at=datetime(2026, 7, 18, 20, 30, tzinfo=ZoneInfo("Asia/Seoul")),
        verification=VerificationStatus.OFFICIAL,
        metrics={"feed_categories": "economy,market"},
    )
    monkeypatch.setattr(
        "kr_stock_wiki.cli.YonhapRssClient.latest",
        lambda _client, begin, end: (
            [record] if (begin, end) == (date(2026, 7, 18), date(2026, 7, 18)) else []
        ),
    )
    output = tmp_path / "news.json"

    code = main(
        [
            "collect-news",
            "--begin",
            "2026-07-18",
            "--end",
            "2026-07-18",
            "--output",
            str(output),
        ]
    )

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert code == 0
    assert payload["schema_version"] == 1
    assert payload["source"] == "official-news"
    assert payload["coverage_complete"] is True
    assert payload["feeds"] == ["economy", "industry", "market"]
    assert payload["records"] == [record.to_dict()]


def test_collect_nxt_writes_delayed_quotes_and_session_summary(
    tmp_path: Path, monkeypatch
):
    fetched_at = datetime(2026, 7, 18, 9, 0, tzinfo=ZoneInfo("Asia/Seoul"))
    quote = EvidenceRecord(
        source=EvidenceSource.NXT,
        evidence_id="nxt:price-snapshot:20260716:000660",
        canonical_event_id="nxt:price-snapshot:20260716:000660",
        kind="price-snapshot",
        company_name="SK hynix",
        title="SK hynix NXT daily price",
        source_url=(
            "https://www.nextrade.co.kr/menu/transactionStatusMain/menuList.do"
        ),
        published_date=date(2026, 7, 16),
        fetched_at=fetched_at,
        verification=VerificationStatus.OFFICIAL,
        ticker="000660",
        delay_minutes=20,
        metrics={"current_price": 183000},
    )
    summary = EvidenceRecord(
        source=EvidenceSource.NXT,
        evidence_id="nxt:session-summary:20260716",
        canonical_event_id="nxt:session-summary:20260716",
        kind="session-summary",
        company_name="NEXTRADE",
        title="NXT session summary",
        source_url=(
            "https://www.nextrade.co.kr/menu/transactionStatusDaily/menuList.do"
        ),
        published_date=date(2026, 7, 16),
        fetched_at=fetched_at,
        verification=VerificationStatus.OFFICIAL,
        metrics={"pre_volume": 1, "main_volume": 2, "after_volume": 3},
    )
    monkeypatch.setattr(
        "kr_stock_wiki.cli.NxtClient.daily_quotes",
        lambda _client, business_date: (
            [quote] if business_date == date(2026, 7, 16) else []
        ),
    )
    monkeypatch.setattr(
        "kr_stock_wiki.cli.NxtClient.session_summary",
        lambda _client, _business_date: summary,
    )
    output = tmp_path / "nxt.json"

    code = main(
        [
            "collect-nxt",
            "--date",
            "2026-07-16",
            "--output",
            str(output),
        ]
    )

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert code == 0
    assert payload["schema_version"] == 1
    assert payload["source"] == "nxt"
    assert payload["quote_delay_minutes"] == 20
    assert "delay_minutes" not in payload
    assert len(payload["records"]) == 2
    assert {record["kind"] for record in payload["records"]} == {
        "price-snapshot",
        "session-summary",
    }


def test_collect_kind_writes_complete_official_status_snapshot(
    tmp_path: Path, monkeypatch
):
    _krx_path, fixture_path = write_operational_snapshots(
        tmp_path,
        business_date=date(2026, 7, 18),
        analysis_date=date(2026, 7, 18),
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    record = EvidenceRecord.from_dict(fixture["records"][0])
    monkeypatch.setattr(
        "kr_stock_wiki.cli.KindClient.statuses",
        lambda _client, tickers, as_of: (
            [record] if tickers == ["005930"] and as_of == date(2026, 7, 18) else []
        ),
    )
    output = tmp_path / "kind-output.json"

    code = main(
        [
            "collect-kind",
            "--date",
            "2026-07-18",
            "--ticker",
            "005930",
            "--output",
            str(output),
        ]
    )

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert code == 0
    assert payload["source"] == "kind"
    assert payload["coverage_complete"] is True
    assert payload["requested_tickers"] == ["005930"]
    assert payload["completed_tickers"] == ["005930"]
    assert payload["records"] == [record.to_dict()]


def test_collect_krx_writes_versioned_snapshot(tmp_path: Path, monkeypatch):
    record = EvidenceRecord(
        source=EvidenceSource.KRX,
        evidence_id="krx:daily:KOSPI:20260717:005930",
        canonical_event_id="krx:daily:KOSPI:20260717:005930",
        kind="daily-price",
        company_name="Samsung Electronics",
        title="Samsung Electronics KRX KOSPI daily price",
        source_url=(
            "https://data-dbg.krx.co.kr/svc/apis/sto/stk_bydd_trd?basDd=20260717"
        ),
        published_date=date(2026, 7, 17),
        fetched_at=datetime(2026, 7, 18, 9, 0, tzinfo=ZoneInfo("Asia/Seoul")),
        verification=VerificationStatus.OFFICIAL,
        ticker="005930",
        metrics={"close": 71000, "volume": 12345678},
        raw={"MKT_NM": "KOSPI"},
    )
    fetched_at = datetime(2026, 7, 18, 9, 0, tzinfo=ZoneInfo("Asia/Seoul"))
    snapshot = KrxDailySnapshot(
        business_date=date(2026, 7, 17),
        requested_markets=(KrxMarket.KOSPI, KrxMarket.KOSDAQ),
        completed_markets=(KrxMarket.KOSPI, KrxMarket.KOSDAQ),
        record_counts=((KrxMarket.KOSPI, 1), (KrxMarket.KOSDAQ, 0)),
        records=(record,),
        fetched_at=fetched_at,
    )

    monkeypatch.setenv("KRX_API_KEY", "secret-krx-key")
    monkeypatch.setattr(
        "kr_stock_wiki.cli.KrxClient.daily_snapshot",
        lambda _client, business_date: (
            snapshot if business_date == date(2026, 7, 17) else None
        ),
    )
    output = tmp_path / "nested" / "krx.json"

    code = main(
        [
            "collect-krx",
            "--date",
            "2026-07-17",
            "--output",
            str(output),
        ]
    )

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert code == 0
    assert payload["schema_version"] == 1
    assert payload["coverage_complete"] is True
    assert payload["requested_markets"] == ["KOSPI", "KOSDAQ"]
    assert payload["completed_markets"] == ["KOSPI", "KOSDAQ"]
    assert payload["record_counts"] == {"KOSPI": 1, "KOSDAQ": 0}
    assert payload["records"][0]["metrics"]["close"] == 71000
    assert "secret-krx-key" not in output.read_text(encoding="utf-8")


def test_collect_krx_requires_environment_key(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.delenv("KRX_API_KEY", raising=False)
    output = tmp_path / "krx.json"

    code = main(
        [
            "collect-krx",
            "--date",
            "2026-07-17",
            "--output",
            str(output),
        ]
    )

    assert code == 2
    assert "KRX_API_KEY" in capsys.readouterr().err
    assert not output.exists()


def test_collect_dart_replaces_output_symlink_without_touching_target(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("DART_API_KEY", "k" * 40)
    monkeypatch.setattr(
        "kr_stock_wiki.cli.DartClient.search", lambda *_args, **_kwargs: []
    )
    victim = tmp_path / "victim.txt"
    victim.write_text("unchanged", encoding="utf-8")
    output = tmp_path / "dart.json"
    output.symlink_to(victim)

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

    assert code == 0
    assert victim.read_text(encoding="utf-8") == "unchanged"
    assert not output.is_symlink()
    assert json.loads(output.read_text(encoding="utf-8"))["records"] == []


def test_collect_dart_cleans_unique_temporary_file_on_replace_failure(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("DART_API_KEY", "k" * 40)
    monkeypatch.setattr(
        "kr_stock_wiki.cli.DartClient.search", lambda *_args, **_kwargs: []
    )
    monkeypatch.setattr(
        "kr_stock_wiki.cli.os.replace",
        lambda *_args: (_ for _ in ()).throw(OSError("replace failed")),
    )
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
    assert not output.exists()
    assert list(tmp_path.iterdir()) == []


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

    code = main(
        [
            "run",
            "--input",
            str(source),
            "--krx-snapshot",
            str(tmp_path / "missing-krx.json"),
            "--kind-status",
            str(tmp_path / "missing-kind.json"),
            "--output",
            str(tmp_path / "wiki"),
        ]
    )

    assert code == 2
    assert "입력 오류" in capsys.readouterr().err


def test_collect_calendar_writes_official_snapshot(tmp_path: Path, monkeypatch):
    fetched_at = datetime(2026, 7, 20, 7, 0, tzinfo=ZoneInfo("Asia/Seoul"))
    days = (
        date(2026, 1, 1),
        date(2026, 2, 16),
        date(2026, 2, 17),
        date(2026, 2, 18),
        date(2026, 3, 2),
        date(2026, 5, 1),
        date(2026, 5, 5),
        date(2026, 5, 25),
        date(2026, 6, 3),
        date(2026, 7, 17),
        date(2026, 12, 31),
    )
    snapshot = KrxMarketCalendar(
        year=2026,
        holidays=tuple(
            MarketHoliday(day, day.strftime("%a").upper(), day.strftime("%A"), "")
            for day in days
        ),
        fetched_at=fetched_at,
    )
    monkeypatch.setattr(
        "kr_stock_wiki.cli.KrxCalendarClient.annual_calendar",
        lambda _client, year: snapshot if year == 2026 else None,
    )
    output = tmp_path / "calendar.json"

    code = main(
        [
            "collect-calendar",
            "--year",
            "2026",
            "--output",
            str(output),
        ]
    )

    assert code == 0
    assert json.loads(output.read_text(encoding="utf-8")) == snapshot.to_payload()
    assert snapshot.source_url == CALENDAR_SOURCE_URL


def test_collect_market_notices_writes_complete_snapshot(tmp_path: Path, monkeypatch):
    fetched_at = datetime(2026, 7, 20, 7, 0, tzinfo=ZoneInfo("Asia/Seoul"))
    raw = {
        "CUR_PAGE": "1",
        "ROW_NUMBER": "1",
        "TOTAL_COUNT": "1",
        "MKT_NM": "파생상품",
        "TITLE": "거래시간 변경 안내",
        "DEP_NM": "시장운영팀",
        "ATTACH_FILE_INFO": "",
        "REG_DT": "2026-07-18",
        "CM_BBS_ID": "0000",
        "BBS_SEQ": "20260718000103",
        "CONTN_TP_CD": "DRV",
    }
    snapshot = KrxMarketNoticeSnapshot(
        begin=date(2026, 7, 1),
        end=date(2026, 7, 20),
        fetched_at=fetched_at,
        total_count=1,
        completed_pages=1,
        page_size=100,
        notices=(
            KrxMarketNotice(
                row_number=1,
                notice_id=raw["BBS_SEQ"],
                registered_date=date(2026, 7, 18),
                market_name=raw["MKT_NM"],
                title=raw["TITLE"],
                department=raw["DEP_NM"],
                content_type=raw["CONTN_TP_CD"],
                board_id=raw["CM_BBS_ID"],
                attachment_info=raw["ATTACH_FILE_INFO"],
                _raw=tuple(raw.items()),
            ),
        ),
    )
    monkeypatch.setattr(
        "kr_stock_wiki.cli.KrxMarketNoticeClient.notices",
        lambda _client, begin, end: (
            snapshot if (begin, end) == (date(2026, 7, 1), date(2026, 7, 20)) else None
        ),
    )
    output = tmp_path / "market-notices.json"

    code = main(
        [
            "collect-market-notices",
            "--begin",
            "2026-07-01",
            "--end",
            "2026-07-20",
            "--output",
            str(output),
        ]
    )

    assert code == 0
    assert json.loads(output.read_text(encoding="utf-8")) == snapshot.to_payload()


def test_collect_market_notices_failure_preserves_existing_snapshot(
    tmp_path: Path, monkeypatch
):
    output = tmp_path / "market-notices.json"
    output.write_text("trusted-previous-snapshot", encoding="utf-8")

    def fail(*_args):
        raise ValueError("partial upstream response")

    monkeypatch.setattr("kr_stock_wiki.cli.KrxMarketNoticeClient.notices", fail)
    code = main(
        [
            "collect-market-notices",
            "--begin",
            "2026-07-01",
            "--end",
            "2026-07-20",
            "--output",
            str(output),
        ]
    )

    assert code == 2
    assert output.read_text(encoding="utf-8") == "trusted-previous-snapshot"


def test_cli_run_fails_closed_without_official_same_day_operating_status(
    tmp_path: Path, capsys
):
    source = tmp_path / "pre-market.json"
    source.write_text(
        json.dumps(
            {
                "as_of": "2026-07-20T07:30:00+09:00",
                "business_date": "2026-07-17",
                "mode": "pre-market",
                "candidates": [],
            }
        ),
        encoding="utf-8",
    )

    code = main(
        [
            "run",
            "--input",
            str(source),
            "--krx-snapshot",
            str(tmp_path / "missing-krx.json"),
            "--kind-status",
            str(tmp_path / "missing-kind.json"),
            "--output",
            str(tmp_path / "wiki"),
        ]
    )

    assert code == 2
    assert "공식 KRX 당일 운영상태" in capsys.readouterr().err
    assert not (tmp_path / "wiki").exists()


def test_cli_lint_returns_nonzero_for_invalid_wiki(tmp_path: Path):
    (tmp_path / "Bad.md").write_text("# no metadata", encoding="utf-8")

    assert main(["lint", "--wiki", str(tmp_path)]) == 1
