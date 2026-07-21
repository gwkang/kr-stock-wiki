import json
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from kr_stock_wiki.cli import (
    _load_candidates,
    _load_krx_snapshot,
    _load_listing_risks,
    _operational_evidence,
    _verify_official_candidate_input,
    main,
)
from kr_stock_wiki.collectors.calendar import (
    CALENDAR_SOURCE_URL,
    KrxCalendarBundle,
    KrxMarketCalendar,
    MarketHoliday,
)
from kr_stock_wiki.collectors.kind_market_notices import (
    KindMarketNotice,
    KindMarketNoticeEvent,
    KindMarketNoticeEventType,
)
from kr_stock_wiki.collectors.market_notices import (
    KrxMarketNotice,
    KrxMarketNoticeSnapshot,
)
from kr_stock_wiki.collectors.krx import KrxDailySnapshot, KrxMarket
from kr_stock_wiki.collectors.krx_live import (
    KrxLiveActivitySnapshot,
    KrxLiveMarketActivity,
)
from kr_stock_wiki.evidence import EvidenceRecord, EvidenceSource, VerificationStatus


def _test_calendar(as_of: datetime) -> KrxMarketCalendar:
    year = as_of.astimezone(ZoneInfo("Asia/Seoul")).year
    days = [date(year, 1, day) for day in range(1, 10)] + [date(year, 12, 31)]
    return KrxMarketCalendar(
        year=year,
        holidays=tuple(
            MarketHoliday(day, day.strftime("%a").upper(), day.strftime("%A"), "test")
            for day in days
        ),
        fetched_at=as_of - timedelta(minutes=1),
    )


def write_calendar(tmp_path: Path, as_of: datetime) -> Path:
    snapshot = _test_calendar(as_of)
    path = tmp_path / f"calendar-{snapshot.year}.json"
    path.write_text(json.dumps(snapshot.to_payload()), encoding="utf-8")
    return path


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
                + f"?basDd={business_date:%Y%m%d}"
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
        source_url=(
            "https://kind.krx.co.kr/investwarn/adminissue.do"
            "?method=searchAdminIssueList"
        ),
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


@pytest.mark.parametrize(
    ("field", "value"),
    [("schema_version", 999), ("source", "tampered")],
)
def test_candidate_loader_rejects_untrusted_envelope(
    tmp_path: Path, field: str, value: object
):
    payload = {
        "schema_version": 1,
        "source": "manual-research-input",
        "as_of": "2026-07-20T20:30:00+09:00",
        "business_date": "2026-07-20",
        "mode": "post-market",
        "candidates": [],
    }
    payload[field] = value
    source = tmp_path / "signals.json"
    source.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="candidate input envelope"):
        _load_candidates(source)


@pytest.mark.parametrize("missing_field", ["observed_at", "evidence_id"])
def test_candidate_loader_rejects_official_pre_market_signal_without_provenance(
    tmp_path: Path, missing_field: str
):
    signal = {
        "group": "price-volume",
        "score": 25,
        "reason": "전 거래일 KRX",
        "source_url": "https://data-dbg.krx.co.kr/svc/apis/sto/stk_bydd_trd",
        "observed_at": "2026-07-20T20:45:00+09:00",
        "evidence_id": "krx:daily:KOSPI:20260720:005930",
    }
    signal.pop(missing_field)
    source = tmp_path / "signals.json"
    source.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source": "official-pre-market-builder",
                "as_of": "2026-07-21T07:30:00+09:00",
                "business_date": "2026-07-21",
                "mode": "pre-market",
                "candidates": [
                    {
                        "ticker": "005930",
                        "name": "삼성전자",
                        "risk_penalty": 0,
                        "signals": [signal],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="candidate signal schema"):
        _load_candidates(source)


def test_cli_run_generates_wiki_from_json(tmp_path: Path):
    source = tmp_path / "signals.json"
    observed = datetime(2026, 7, 20, 20, 30, tzinfo=ZoneInfo("Asia/Seoul"))
    source.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source": "manual-research-input",
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
    calendar = write_calendar(tmp_path, observed)

    code = main(
        [
            "run",
            "--input",
            str(source),
            "--krx-snapshot",
            str(krx_snapshot),
            "--calendar",
            str(calendar),
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
            "--calendar",
            str(tmp_path / "missing-calendar.json"),
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


def test_collect_kind_market_notice_writes_verified_detail_snapshot(
    tmp_path: Path, monkeypatch
):
    notice = KindMarketNotice(
        acceptance_number="20250520000110",
        document_number="20250520000087",
        title="휴장안내",
        prior_document_numbers=(),
        init_url=(
            "https://kind.krx.co.kr/common/disclsviewer.do?"
            "method=searchInitInfo&acptNo=20250520000110"
        ),
        document_url=(
            "https://kind.krx.co.kr/external/2025/05/20/000110/20250520000087/99340.htm"
        ),
        fetched_at=datetime(2026, 7, 20, 7, 0, tzinfo=ZoneInfo("Asia/Seoul")),
        init_html=("<option value='20250520000087|Y' selected>휴장안내</option>"),
        wrapper_html=(
            "<iframe src='/external/2025/05/20/000110/"
            "20250520000087/99340.htm'></iframe>"
        ),
        body_html=("<html><body>유가증권시장 휴장일자 2025년 6월 3일</body></html>"),
        body_text="유가증권시장 휴장일자 2025년 6월 3일",
        events=(
            KindMarketNoticeEvent(
                event_type=KindMarketNoticeEventType.CLOSED,
                effective_date=date(2025, 6, 3),
                markets=("KOSPI",),
            ),
        ),
    )
    monkeypatch.setattr(
        "kr_stock_wiki.cli.KindMarketNoticeClient.document",
        lambda _client, acceptance_number: (
            notice if acceptance_number == "20250520000110" else None
        ),
    )
    output = tmp_path / "kind-market-notice.json"

    code = main(
        [
            "collect-kind-market-notice",
            "--acceptance-number",
            "20250520000110",
            "--output",
            str(output),
        ]
    )

    assert code == 0
    assert json.loads(output.read_text(encoding="utf-8")) == notice.to_payload()


def test_collect_kind_market_notice_failure_preserves_existing_snapshot(
    tmp_path: Path, monkeypatch
):
    output = tmp_path / "kind-market-notice.json"
    output.write_text("trusted-previous-snapshot", encoding="utf-8")

    def fail(*_args):
        raise ValueError("partial KIND document chain")

    monkeypatch.setattr("kr_stock_wiki.cli.KindMarketNoticeClient.document", fail)
    code = main(
        [
            "collect-kind-market-notice",
            "--acceptance-number",
            "20250520000110",
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
                "schema_version": 1,
                "source": "manual-research-input",
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
            "--calendar",
            str(tmp_path / "missing-calendar.json"),
            "--kind-status",
            str(tmp_path / "missing-kind.json"),
            "--output",
            str(tmp_path / "wiki"),
        ]
    )

    assert code == 2
    assert "official pre-market candidate input" in capsys.readouterr().err
    assert not (tmp_path / "wiki").exists()


def test_cli_lint_returns_nonzero_for_invalid_wiki(tmp_path: Path):
    (tmp_path / "Bad.md").write_text("# no metadata", encoding="utf-8")

    assert main(["lint", "--wiki", str(tmp_path)]) == 1


def test_build_daily_input_writes_official_post_market_candidates(tmp_path: Path):
    krx_path, _kind_path = write_operational_snapshots(
        tmp_path,
        business_date=date(2026, 7, 20),
        analysis_date=date(2026, 7, 20),
    )
    krx_payload = json.loads(krx_path.read_text(encoding="utf-8"))
    krx_payload["records"][0]["metrics"]["change_rate"] = 2.5
    krx_path.write_text(json.dumps(krx_payload), encoding="utf-8")
    watchlist = tmp_path / "watchlist.json"
    watchlist.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source": "user-watchlist",
                "stocks": [{"ticker": "005930", "name": "005930"}],
            }
        ),
        encoding="utf-8",
    )
    nxt = tmp_path / "nxt.json"
    nxt_quote = EvidenceRecord(
        source=EvidenceSource.NXT,
        evidence_id="nxt:price-snapshot:20260720:005930",
        canonical_event_id="nxt:price-snapshot:20260720:005930",
        kind="price-snapshot",
        ticker="005930",
        company_name="005930",
        title="005930 NXT 20분 지연 시세",
        source_url="https://www.nextrade.co.kr/menu/transactionStatusMain/menuList.do",
        published_date=date(2026, 7, 20),
        fetched_at=datetime(2026, 7, 20, 20, 29, tzinfo=ZoneInfo("Asia/Seoul")),
        verification=VerificationStatus.OFFICIAL,
        delay_minutes=20,
        metrics={
            "current_price": 72000,
            "change_rate": 2.0,
            "volume": 2000000,
            "trading_value": 144000000000,
            "source_as_of": "2026-07-20T20:20:00+09:00",
        },
    )
    nxt_summary = EvidenceRecord(
        source=EvidenceSource.NXT,
        evidence_id="nxt:session-summary:20260720",
        canonical_event_id="nxt:session-summary:20260720",
        kind="session-summary",
        company_name="NEXTRADE",
        title="NXT 2026-07-20 세션별 거래 현황",
        source_url="https://www.nextrade.co.kr/menu/transactionStatusDaily/menuList.do",
        published_date=date(2026, 7, 20),
        fetched_at=datetime(2026, 7, 20, 20, 30, tzinfo=ZoneInfo("Asia/Seoul")),
        verification=VerificationStatus.OFFICIAL,
        metrics={
            "pre_session": "08:00-08:50",
            "pre_instruments": 100,
            "pre_volume": 10,
            "pre_trading_value": 100,
            "main_session": "09:00:30-15:20",
            "main_instruments": 200,
            "main_volume": 20,
            "main_trading_value": 200,
            "after_session": "15:40-20:00",
            "after_instruments": 150,
            "after_volume": 30,
            "after_trading_value": 300,
            "total_instruments": 250,
            "total_volume": 60,
            "total_trading_value": 600,
            "volume_market_share": 12.3,
        },
    )
    nxt.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source": "nxt",
                "collected_at": "2026-07-20T20:30:00+09:00",
                "date": "2026-07-20",
                "quote_delay_minutes": 20,
                "records": [nxt_quote.to_dict(), nxt_summary.to_dict()],
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "daily-input.json"

    code = main(
        [
            "build-daily-input",
            "--watchlist",
            str(watchlist),
            "--krx-snapshot",
            str(krx_path),
            "--nxt-snapshot",
            str(nxt),
            "--as-of",
            "2026-07-20T20:45:00+09:00",
            "--output",
            str(output),
        ]
    )

    assert code == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["source"] == "official-post-market-builder"
    assert payload["candidates"][0]["ticker"] == "005930"
    assert payload["candidates"][0]["signals"][0]["score"] == 25.0
    _verify_official_candidate_input(
        output,
        watchlist_path=watchlist,
        nxt_snapshot_path=nxt,
        krx_snapshot=_load_krx_snapshot(krx_path),
        calendar_bundle=KrxCalendarBundle(
            (
                _test_calendar(
                    datetime(2026, 7, 20, 20, 45, tzinfo=ZoneInfo("Asia/Seoul"))
                ),
            ),
            datetime(2026, 7, 20, 20, 45, tzinfo=ZoneInfo("Asia/Seoul")),
        ),
        observed=datetime(2026, 7, 20, 20, 45, tzinfo=ZoneInfo("Asia/Seoul")),
    )

    payload["candidates"][0]["signals"][0]["score"] = 100
    output.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="does not match source snapshots"):
        _verify_official_candidate_input(
            output,
            watchlist_path=watchlist,
            nxt_snapshot_path=nxt,
            krx_snapshot=_load_krx_snapshot(krx_path),
            calendar_bundle=KrxCalendarBundle(
                (
                    _test_calendar(
                        datetime(2026, 7, 20, 20, 45, tzinfo=ZoneInfo("Asia/Seoul"))
                    ),
                ),
                datetime(2026, 7, 20, 20, 45, tzinfo=ZoneInfo("Asia/Seoul")),
            ),
            observed=datetime(2026, 7, 20, 20, 45, tzinfo=ZoneInfo("Asia/Seoul")),
        )


def test_build_daily_input_preserves_existing_output_on_validation_failure(
    tmp_path: Path,
):
    output = tmp_path / "daily-input.json"
    output.write_text('{"preserved": true}\n', encoding="utf-8")
    invalid = tmp_path / "invalid.json"
    invalid.write_text("{}", encoding="utf-8")

    code = main(
        [
            "build-daily-input",
            "--watchlist",
            str(invalid),
            "--krx-snapshot",
            str(invalid),
            "--nxt-snapshot",
            str(invalid),
            "--as-of",
            "2026-07-20T20:45:00+09:00",
            "--output",
            str(output),
        ]
    )

    assert code == 2
    assert json.loads(output.read_text(encoding="utf-8")) == {"preserved": True}


def test_collect_krx_live_writes_same_day_market_activity(tmp_path: Path, monkeypatch):
    observed = datetime(2026, 7, 21, 9, 25, tzinfo=ZoneInfo("Asia/Seoul"))
    source_as_of = datetime(2026, 7, 21, 9, 24, tzinfo=ZoneInfo("Asia/Seoul"))
    raw_rows = tuple(
        tuple(
            {
                "TRD_DD": "20260721",
                "DD_TP": "T_DD",
                "INVST_TP": investor,
                "ACC_BID_TRDVAL": "100",
                "ACC_ASK_TRDVAL": "100",
                "NETBID_TRDVAL": "0",
            }.items()
        )
        for investor in ("기관(십억원)", "외국인(십억원)", "개인(십억원)")
    )
    snapshot = KrxLiveActivitySnapshot(
        business_date=date(2026, 7, 21),
        source_as_of=source_as_of,
        fetched_at=observed,
        activities=tuple(
            KrxLiveMarketActivity(market, source_as_of, 600, raw_rows)
            for market in (KrxMarket.KOSPI, KrxMarket.KOSDAQ)
        ),
    )
    monkeypatch.setattr(
        "kr_stock_wiki.cli.KrxLiveClient.current_activity",
        lambda _client, business_date: (
            snapshot if business_date == date(2026, 7, 21) else None
        ),
    )
    output = tmp_path / "krx-live.json"

    code = main(
        [
            "collect-krx-live",
            "--date",
            "2026-07-21",
            "--output",
            str(output),
        ]
    )

    assert code == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["source"] == "krx-live-market-activity"
    assert payload["business_date"] == "2026-07-21"


def test_candidate_loader_accepts_official_pre_market_analysis_date(tmp_path: Path):
    source = tmp_path / "pre-market.json"
    payload = {
        "schema_version": 1,
        "source": "official-pre-market-builder",
        "as_of": "2026-07-21T07:30:00+09:00",
        "business_date": "2026-07-21",
        "mode": "pre-market",
        "candidates": [],
    }
    source.write_text(json.dumps(payload), encoding="utf-8")

    _observed, business_date, mode, source_name, _candidates = _load_candidates(source)

    assert business_date == date(2026, 7, 21)
    assert mode == "pre-market"
    assert source_name == "official-pre-market-builder"


def test_candidate_loader_accepts_only_same_day_official_morning_envelope(
    tmp_path: Path,
):
    source = tmp_path / "morning.json"
    payload = {
        "schema_version": 1,
        "source": "official-morning-builder",
        "as_of": "2026-07-21T09:25:00+09:00",
        "business_date": "2026-07-21",
        "mode": "morning",
        "candidates": [],
    }
    source.write_text(json.dumps(payload), encoding="utf-8")

    _observed, business_date, mode, source_name, _candidates = _load_candidates(source)

    assert business_date == date(2026, 7, 21)
    assert mode == "morning"
    assert source_name == "official-morning-builder"

    payload["source"] = "manual-research-input"
    source.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="requires official morning"):
        _load_candidates(source)


def test_build_morning_input_cli_writes_canonical_official_artifact(tmp_path: Path):
    kst = ZoneInfo("Asia/Seoul")
    krx_path, _kind_path = write_operational_snapshots(
        tmp_path,
        business_date=date(2026, 7, 20),
        analysis_date=date(2026, 7, 21),
    )
    krx_payload = json.loads(krx_path.read_text(encoding="utf-8"))
    krx_payload["records"][0]["metrics"]["change_rate"] = 2.5
    krx_path.write_text(json.dumps(krx_payload), encoding="utf-8")

    watchlist = tmp_path / "watchlist.json"
    watchlist.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source": "user-watchlist",
                "stocks": [{"ticker": "005930", "name": "005930"}],
            }
        ),
        encoding="utf-8",
    )
    nxt_id = "nxt:price-snapshot:20260721:005930"
    nxt_record = EvidenceRecord(
        source=EvidenceSource.NXT,
        evidence_id=nxt_id,
        canonical_event_id=nxt_id,
        kind="price-snapshot",
        company_name="005930",
        title="NXT current price",
        source_url="https://www.nextrade.co.kr/menu/transactionStatusMain/menuList.do",
        published_date=date(2026, 7, 21),
        fetched_at=datetime(2026, 7, 21, 9, 25, tzinfo=kst),
        verification=VerificationStatus.OFFICIAL,
        ticker="005930",
        delay_minutes=20,
        metrics={
            "market": "KOSPI",
            "current_price": 72_000,
            "change_rate": 1.5,
            "volume": 150_000,
            "trading_value": 10_800_000_000,
            "source_as_of": "2026-07-21T09:00:00+09:00",
        },
    )
    nxt_path = tmp_path / "nxt.json"
    nxt_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source": "nxt",
                "collected_at": "2026-07-21T09:25:00+09:00",
                "date": "2026-07-21",
                "quote_delay_minutes": 20,
                "records": [nxt_record.to_dict()],
            }
        ),
        encoding="utf-8",
    )
    raw_rows = tuple(
        tuple(
            {
                "TRD_DD": "20260721",
                "DD_TP": "T_DD",
                "INVST_TP": investor,
                "ACC_BID_TRDVAL": "100",
                "ACC_ASK_TRDVAL": "100",
                "NETBID_TRDVAL": "0",
            }.items()
        )
        for investor in ("기관(십억원)", "외국인(십억원)", "개인(십억원)")
    )
    live = KrxLiveActivitySnapshot(
        date(2026, 7, 21),
        datetime(2026, 7, 21, 9, 24, tzinfo=kst),
        datetime(2026, 7, 21, 9, 25, tzinfo=kst),
        tuple(
            KrxLiveMarketActivity(
                market, datetime(2026, 7, 21, 9, 24, tzinfo=kst), 600, raw_rows
            )
            for market in (KrxMarket.KOSPI, KrxMarket.KOSDAQ)
        ),
    )
    live_path = tmp_path / "krx-live.json"
    live_path.write_text(json.dumps(live.to_payload()), encoding="utf-8")
    observed = datetime(2026, 7, 21, 9, 25, tzinfo=kst)
    calendar = write_calendar(tmp_path, observed)
    output = tmp_path / "morning.json"

    code = main(
        [
            "build-morning-input",
            "--watchlist",
            str(watchlist),
            "--krx-snapshot",
            str(krx_path),
            "--nxt-snapshot",
            str(nxt_path),
            "--krx-live-snapshot",
            str(live_path),
            "--calendar",
            str(calendar),
            "--previous-business-date",
            "2026-07-20",
            "--as-of",
            "2026-07-21T09:25:00+09:00",
            "--output",
            str(output),
        ]
    )

    assert code == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["source"] == "official-morning-builder"
    assert payload["candidates"][0]["ticker"] == "005930"


def test_build_pre_market_input_cli_writes_previous_session_artifact(tmp_path: Path):
    kst = ZoneInfo("Asia/Seoul")
    previous = date(2026, 7, 20)
    observed = datetime(2026, 7, 21, 7, 30, tzinfo=kst)
    nxt_fetched = observed - timedelta(minutes=1)
    krx_path, kind_path = write_operational_snapshots(
        tmp_path, business_date=previous, analysis_date=observed.date()
    )
    kind_payload = json.loads(kind_path.read_text(encoding="utf-8"))
    kind_payload["collected_at"] = observed.isoformat()
    kind_payload["records"][0]["fetched_at"] = observed.isoformat()
    kind_payload["records"][0]["company_name"] = "005930"
    kind_path.write_text(json.dumps(kind_payload), encoding="utf-8")
    krx_payload = json.loads(krx_path.read_text(encoding="utf-8"))
    krx_payload["records"][0]["metrics"]["change_rate"] = 2.5
    krx_path.write_text(json.dumps(krx_payload), encoding="utf-8")
    watchlist = tmp_path / "watchlist-pre.json"
    watchlist.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source": "user-watchlist",
                "stocks": [{"ticker": "005930", "name": "005930"}],
            }
        ),
        encoding="utf-8",
    )
    quote_id = "nxt:price-snapshot:20260720:005930"
    quote = EvidenceRecord(
        source=EvidenceSource.NXT,
        evidence_id=quote_id,
        canonical_event_id=quote_id,
        kind="price-snapshot",
        company_name="005930",
        title="NXT previous close",
        source_url="https://www.nextrade.co.kr/menu/transactionStatusMain/menuList.do",
        published_date=previous,
        fetched_at=nxt_fetched,
        verification=VerificationStatus.OFFICIAL,
        ticker="005930",
        delay_minutes=20,
        metrics={
            "change_rate": 1.5,
            "volume": 150_000,
            "trading_value": 10_800_000_000,
            "source_as_of": "2026-07-20T20:05:00+09:00",
        },
    )
    summary_id = "nxt:session-summary:20260720"
    summary = EvidenceRecord(
        source=EvidenceSource.NXT,
        evidence_id=summary_id,
        canonical_event_id=summary_id,
        kind="session-summary",
        company_name="NEXTRADE",
        title="NXT session summary",
        source_url="https://www.nextrade.co.kr/menu/transactionStatusDaily/menuList.do",
        published_date=previous,
        fetched_at=nxt_fetched,
        verification=VerificationStatus.OFFICIAL,
        metrics={
            "pre_session": "08:00-08:50",
            "pre_instruments": 1,
            "pre_volume": 1,
            "pre_trading_value": 1,
            "main_session": "09:00:30-15:20",
            "main_instruments": 1,
            "main_volume": 1,
            "main_trading_value": 1,
            "after_session": "15:40-20:00",
            "after_instruments": 1,
            "after_volume": 1,
            "after_trading_value": 1,
            "total_instruments": 1,
            "total_volume": 3,
            "total_trading_value": 3,
            "volume_market_share": 1.0,
        },
    )
    nxt_path = tmp_path / "nxt-previous.json"
    nxt_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source": "nxt",
                "collected_at": nxt_fetched.isoformat(),
                "date": previous.isoformat(),
                "quote_delay_minutes": 20,
                "records": [summary.to_dict(), quote.to_dict()],
            }
        ),
        encoding="utf-8",
    )
    calendar = write_calendar(tmp_path, observed)
    output = tmp_path / "pre-market.json"

    code = main(
        [
            "build-pre-market-input",
            "--watchlist",
            str(watchlist),
            "--krx-snapshot",
            str(krx_path),
            "--nxt-snapshot",
            str(nxt_path),
            "--calendar",
            str(calendar),
            "--previous-business-date",
            previous.isoformat(),
            "--as-of",
            observed.isoformat(),
            "--output",
            str(output),
        ]
    )

    assert code == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["source"] == "official-pre-market-builder"
    assert payload["business_date"] == "2026-07-21"
    assert payload["candidates"][0]["signals"][1]["evidence_id"] == quote_id

    wiki = tmp_path / "wiki-pre-market"
    run_code = main(
        [
            "run",
            "--input",
            str(output),
            "--watchlist",
            str(watchlist),
            "--krx-snapshot",
            str(krx_path),
            "--nxt-snapshot",
            str(nxt_path),
            "--calendar",
            str(calendar),
            "--previous-business-date",
            previous.isoformat(),
            "--kind-status",
            str(kind_path),
            "--output",
            str(wiki),
        ]
    )

    assert run_code == 0
    assert (wiki / "Home.md").exists()
