from __future__ import annotations

import math
import re
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .collectors.calendar import KrxCalendarBundle
from .collectors.krx import KrxDailySnapshot, KrxMarket
from .collectors.krx_live import KrxLiveActivitySnapshot
from .evidence import EvidenceRecord, EvidenceSource, VerificationStatus

_KST = ZoneInfo("Asia/Seoul")
_NXT_QUOTE_URL = "https://www.nextrade.co.kr/menu/transactionStatusMain/menuList.do"
_NXT_SUMMARY_URL = "https://www.nextrade.co.kr/menu/transactionStatusDaily/menuList.do"
_MAX_NXT_AGE = timedelta(hours=2)
_MAX_KRX_AGE = timedelta(hours=12)


def _finite_number(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{field} must be a finite number")
    return result


def _watchlist(payload: object) -> tuple[tuple[str, str], ...]:
    if (
        not isinstance(payload, dict)
        or set(payload) != {"schema_version", "source", "stocks"}
        or payload.get("schema_version") != 1
        or payload.get("source") != "user-watchlist"
        or not isinstance(payload.get("stocks"), list)
    ):
        raise ValueError("invalid user watchlist envelope")
    stocks = payload["stocks"]
    if not 1 <= len(stocks) <= 20:
        raise ValueError("watchlist must contain between 1 and 20 stocks")
    result: list[tuple[str, str]] = []
    for item in stocks:
        if not isinstance(item, dict) or set(item) != {"ticker", "name"}:
            raise ValueError("invalid watchlist stock")
        ticker = item["ticker"]
        name = item["name"]
        if not isinstance(ticker, str) or not re.fullmatch(r"[0-9A-Z]{6}", ticker):
            raise ValueError("watchlist ticker must be six uppercase characters")
        if (
            not isinstance(name, str)
            or not name
            or len(name) > 100
            or any(character in name for character in "\r\n")
        ):
            raise ValueError("watchlist name is invalid")
        result.append((ticker, name))
    if len({ticker for ticker, _name in result}) != len(result):
        raise ValueError("watchlist tickers must be unique")
    return tuple(result)


def _summary_integer(record: EvidenceRecord, name: str) -> int:
    value = record.metrics.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"NXT session {name} must be a non-negative integer")
    return value


def _validate_session_summary(
    record: EvidenceRecord, business_date: date, *, historical_collection: bool = False
) -> None:
    expected_id = f"nxt:session-summary:{business_date:%Y%m%d}"
    expected_metrics = {
        "pre_session",
        "pre_instruments",
        "pre_volume",
        "pre_trading_value",
        "main_session",
        "main_instruments",
        "main_volume",
        "main_trading_value",
        "after_session",
        "after_instruments",
        "after_volume",
        "after_trading_value",
        "total_instruments",
        "total_volume",
        "total_trading_value",
        "volume_market_share",
    }
    if (
        record.ticker is not None
        or record.delay_minutes is not None
        or record.company_name != "NEXTRADE"
        or record.source_url != _NXT_SUMMARY_URL
        or record.evidence_id != expected_id
        or record.canonical_event_id != expected_id
        or record.is_correction
        or record.is_withdrawn
        or set(record.metrics) != expected_metrics
        or record.metrics.get("pre_session") != "08:00-08:50"
        or record.metrics.get("main_session") != "09:00:30-15:20"
        or record.metrics.get("after_session") != "15:40-20:00"
        or (
            not historical_collection
            and record.fetched_at.astimezone(_KST).time() < time(20, 0)
        )
    ):
        raise ValueError("invalid NXT post-market session-summary")
    for name in ("pre", "main", "after"):
        _summary_integer(record, f"{name}_instruments")
    _summary_integer(record, "total_instruments")
    volumes = [
        _summary_integer(record, f"{name}_volume") for name in ("pre", "main", "after")
    ]
    values = [
        _summary_integer(record, f"{name}_trading_value")
        for name in ("pre", "main", "after")
    ]
    if _summary_integer(record, "total_volume") != sum(volumes) or _summary_integer(
        record, "total_trading_value"
    ) != sum(values):
        raise ValueError("NXT session totals are inconsistent")
    market_share = _finite_number(
        record.metrics.get("volume_market_share"), "NXT volume_market_share"
    )
    if not 0 <= market_share <= 100:
        raise ValueError("NXT volume_market_share must be between 0 and 100")


def _nxt_quotes(
    payload: object,
    *,
    business_date,
    as_of: datetime,
    minimum_source_time: time = time(20, 20),
    require_summary: bool = True,
    maximum_age: timedelta | None = _MAX_NXT_AGE,
    historical_collection: bool = False,
) -> dict[str, EvidenceRecord]:
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != 1
        or payload.get("source") != "nxt"
        or payload.get("quote_delay_minutes") != 20
        or payload.get("date") != business_date.isoformat()
        or not isinstance(payload.get("records"), list)
    ):
        if (
            isinstance(payload, dict)
            and payload.get("date") != business_date.isoformat()
        ):
            raise ValueError("NXT snapshot date mismatch")
        if isinstance(payload, dict) and payload.get("quote_delay_minutes") != 20:
            raise ValueError("NXT snapshot delay must be 20 minutes")
        raise ValueError("invalid NXT snapshot envelope")
    try:
        collected_at = datetime.fromisoformat(str(payload["collected_at"]))
    except (KeyError, ValueError, TypeError):
        raise ValueError("invalid NXT collected_at") from None
    if collected_at.tzinfo is None or collected_at.utcoffset() is None:
        raise ValueError("NXT collected_at must include a timezone")
    if collected_at > as_of:
        raise ValueError("NXT snapshot contains future collection time")
    if maximum_age is not None and as_of - collected_at > maximum_age:
        raise ValueError("NXT snapshot collection time is not fresh")
    collected_date = collected_at.astimezone(_KST).date()
    if (historical_collection and collected_date <= business_date) or (
        not historical_collection and collected_date != business_date
    ):
        raise ValueError("NXT collected_at date mismatch")

    quotes: dict[str, EvidenceRecord] = {}
    summary_count = 0
    for raw in payload["records"]:
        record = EvidenceRecord.from_dict(raw)
        if (
            record.source is not EvidenceSource.NXT
            or record.verification is not VerificationStatus.OFFICIAL
            or record.published_date != business_date
        ):
            raise ValueError("NXT record is not official same-day evidence")
        if record.fetched_at > as_of:
            raise ValueError("NXT record contains future evidence")
        if record.fetched_at > collected_at:
            raise ValueError("NXT record time lineage is inconsistent")
        if maximum_age is not None and as_of - record.fetched_at > maximum_age:
            raise ValueError("NXT record is not fresh")
        if record.kind == "session-summary":
            if not require_summary:
                raise ValueError(
                    "NXT morning snapshot must not contain a session-summary"
                )
            summary_count += 1
            _validate_session_summary(
                record,
                business_date,
                historical_collection=historical_collection,
            )
            continue
        expected_quote_id = (
            f"nxt:price-snapshot:{business_date:%Y%m%d}:{record.ticker}"
            if record.ticker is not None
            else None
        )
        if (
            record.kind != "price-snapshot"
            or record.ticker is None
            or record.delay_minutes != 20
            or record.source_url != _NXT_QUOTE_URL
            or record.evidence_id != expected_quote_id
            or record.canonical_event_id != expected_quote_id
            or record.is_correction
            or record.is_withdrawn
        ):
            raise ValueError("invalid NXT quote record")
        if record.ticker in quotes:
            raise ValueError("NXT quote tickers must be unique")
        source_as_of_raw = record.metrics.get("source_as_of")
        if not isinstance(source_as_of_raw, str):
            raise ValueError("NXT quote source_as_of is missing")
        try:
            source_as_of = datetime.fromisoformat(source_as_of_raw)
        except ValueError:
            raise ValueError("NXT quote source_as_of is invalid") from None
        if source_as_of.tzinfo is None or source_as_of.utcoffset() is None:
            raise ValueError("NXT quote source_as_of must include a timezone")
        if source_as_of > record.fetched_at:
            raise ValueError("NXT quote time lineage is inconsistent")
        source_time = source_as_of.astimezone(_KST).timetz().replace(tzinfo=None)
        if source_time < minimum_source_time:
            raise ValueError(
                "NXT quote source_as_of must be "
                f"{minimum_source_time.strftime('%H:%M')} KST or later"
            )
        if (
            source_as_of > as_of
            or source_as_of.astimezone(_KST).date() != business_date
            or (maximum_age is not None and as_of - source_as_of > maximum_age)
        ):
            raise ValueError("NXT quote source time is not fresh same-day evidence")
        quotes[record.ticker] = record
    if require_summary and summary_count != 1:
        raise ValueError("NXT snapshot requires exactly one session-summary")
    if not require_summary and summary_count != 0:
        raise ValueError("NXT morning snapshot must not contain a session-summary")
    if not quotes:
        raise ValueError("NXT snapshot requires at least one price-snapshot")
    return quotes


def _score(change_rate: object, field: str) -> float:
    return min(100.0, abs(_finite_number(change_rate, field)) * 10.0)


def _integer_metric(record: EvidenceRecord, name: str) -> int:
    value = record.metrics.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{record.source.value} {name} must be a non-negative integer")
    return value


def build_post_market_input(
    watchlist_payload: object,
    krx_snapshot: KrxDailySnapshot,
    nxt_snapshot_payload: object,
    as_of: datetime,
) -> dict[str, Any]:
    """Build deterministic candidate input from official KRX and NXT snapshots."""
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of must include a timezone")
    as_of_kst = as_of.astimezone(_KST)
    if as_of_kst.timetz().replace(tzinfo=None) < time(20, 20):
        raise ValueError("post-market analysis requires 20:20 KST or later")
    business_date = krx_snapshot.business_date
    if as_of_kst.date() != business_date:
        raise ValueError("post-market as_of must match the KRX business date")
    if not krx_snapshot.coverage_complete:
        raise ValueError("KRX snapshot coverage must be complete")
    if (
        krx_snapshot.fetched_at > as_of
        or as_of - krx_snapshot.fetched_at > _MAX_KRX_AGE
    ):
        raise ValueError("KRX snapshot must be fresh and not future evidence")

    stocks = _watchlist(watchlist_payload)
    krx_records = {record.ticker: record for record in krx_snapshot.records}
    quotes = _nxt_quotes(nxt_snapshot_payload, business_date=business_date, as_of=as_of)
    candidates: list[dict[str, Any]] = []
    for ticker, configured_name in stocks:
        krx = krx_records.get(ticker)
        if krx is None:
            raise ValueError(f"watchlist ticker {ticker} is missing from KRX snapshot")
        if krx.company_name != configured_name:
            raise ValueError(f"watchlist name mismatch for {ticker}")
        if (
            krx.source is not EvidenceSource.KRX
            or krx.verification is not VerificationStatus.OFFICIAL
            or krx.kind != "daily-price"
            or krx.published_date != business_date
        ):
            raise ValueError("invalid official KRX daily-price evidence")
        change_rate = _finite_number(krx.metrics.get("change_rate"), "KRX change_rate")
        volume = _integer_metric(krx, "volume")
        trading_value = _integer_metric(krx, "trading_value")
        signals: list[dict[str, Any]] = [
            {
                "group": "price-volume",
                "score": _score(change_rate, "KRX change_rate"),
                "reason": (
                    f"KRX 등락률 {change_rate:+.2f}%, 거래량 {volume:,}주, "
                    f"거래대금 {trading_value:,}원"
                ),
                "source_url": krx.source_url,
                "observed_at": krx.fetched_at.isoformat(),
                "evidence_id": krx.evidence_id,
            }
        ]
        nxt = quotes.get(ticker)
        if nxt is not None:
            if nxt.company_name != krx.company_name:
                raise ValueError(f"KRX/NXT company name mismatch for {ticker}")
            nxt_change_rate = _finite_number(
                nxt.metrics.get("change_rate"), "NXT change_rate"
            )
            nxt_volume = _integer_metric(nxt, "volume")
            nxt_trading_value = _integer_metric(nxt, "trading_value")
            signals.append(
                {
                    "group": "cross-market",
                    "score": _score(nxt_change_rate, "NXT change_rate"),
                    "reason": (
                        f"NXT 20분 지연 등락률 {nxt_change_rate:+.2f}%, "
                        f"거래량 {nxt_volume:,}주, 거래대금 {nxt_trading_value:,}원"
                    ),
                    "source_url": nxt.source_url,
                    "observed_at": nxt.fetched_at.isoformat(),
                    "evidence_id": nxt.evidence_id,
                }
            )
        candidates.append(
            {
                "ticker": ticker,
                "name": krx.company_name,
                "risk_penalty": 0,
                "signals": signals,
            }
        )
    return {
        "schema_version": 1,
        "source": "official-post-market-builder",
        "as_of": as_of.isoformat(),
        "business_date": business_date.isoformat(),
        "mode": "post-market",
        "candidates": candidates,
    }


def build_pre_market_input(
    watchlist_payload: object,
    previous_krx_snapshot: KrxDailySnapshot,
    previous_nxt_snapshot_payload: object,
    calendar_bundle: KrxCalendarBundle,
    previous_business_date: date,
    as_of: datetime,
) -> dict[str, Any]:
    """Build 07:30 candidates from the exact previous official market session."""
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of must include a timezone")
    as_of_kst = as_of.astimezone(_KST)
    analysis_time = as_of_kst.timetz().replace(tzinfo=None)
    if not time(7, 0) <= analysis_time < time(8, 0):
        raise ValueError("pre-market analysis requires 07:00-08:00 KST")
    business_date = as_of_kst.date()
    if calendar_bundle.as_of != as_of:
        raise ValueError("pre-market calendar bundle must match analysis time")
    if not calendar_bundle.is_scheduled_trading_day(business_date):
        raise ValueError("pre-market analysis date is a scheduled KRX closure")
    if calendar_bundle.previous_business_date(business_date) != previous_business_date:
        raise ValueError("pre-market previous date does not match official calendar")
    if (
        not previous_krx_snapshot.coverage_complete
        or previous_krx_snapshot.business_date != previous_business_date
        or previous_business_date >= business_date
        or previous_krx_snapshot.fetched_at > as_of
    ):
        raise ValueError("invalid exact previous KRX daily snapshot")

    stocks = _watchlist(watchlist_payload)
    krx_records = {record.ticker: record for record in previous_krx_snapshot.records}
    quotes = _nxt_quotes(
        previous_nxt_snapshot_payload,
        business_date=previous_business_date,
        as_of=as_of,
        minimum_source_time=time(20, 0),
        require_summary=True,
        maximum_age=None,
        historical_collection=True,
    )
    candidates: list[dict[str, Any]] = []
    for ticker, configured_name in stocks:
        krx = krx_records.get(ticker)
        if krx is None:
            raise ValueError(f"watchlist ticker {ticker} is missing from KRX snapshot")
        if krx.company_name != configured_name:
            raise ValueError(f"watchlist name mismatch for {ticker}")
        if (
            krx.source is not EvidenceSource.KRX
            or krx.verification is not VerificationStatus.OFFICIAL
            or krx.kind != "daily-price"
            or krx.published_date != previous_business_date
        ):
            raise ValueError("invalid official previous KRX daily-price evidence")
        change_rate = _finite_number(krx.metrics.get("change_rate"), "KRX change_rate")
        volume = _integer_metric(krx, "volume")
        trading_value = _integer_metric(krx, "trading_value")
        signals: list[dict[str, Any]] = [
            {
                "group": "price-volume",
                "score": _score(change_rate, "KRX change_rate"),
                "reason": (
                    f"전 거래일 KRX 등락률 {change_rate:+.2f}%, 거래량 {volume:,}주, "
                    f"거래대금 {trading_value:,}원"
                ),
                "source_url": krx.source_url,
                "observed_at": krx.fetched_at.isoformat(),
                "evidence_id": krx.evidence_id,
            }
        ]
        nxt = quotes.get(ticker)
        if nxt is None:
            raise ValueError(f"watchlist ticker {ticker} is missing from NXT snapshot")
        if nxt.company_name != krx.company_name:
            raise ValueError(f"KRX/NXT company name mismatch for {ticker}")
        nxt_change_rate = _finite_number(
            nxt.metrics.get("change_rate"), "NXT change_rate"
        )
        nxt_volume = _integer_metric(nxt, "volume")
        nxt_trading_value = _integer_metric(nxt, "trading_value")
        signals.append(
            {
                "group": "cross-market",
                "score": _score(nxt_change_rate, "NXT change_rate"),
                "reason": (
                    f"전 거래일 NXT 20분 지연 등락률 {nxt_change_rate:+.2f}%, "
                    f"거래량 {nxt_volume:,}주, 거래대금 {nxt_trading_value:,}원"
                ),
                "source_url": nxt.source_url,
                "observed_at": nxt.fetched_at.isoformat(),
                "evidence_id": nxt.evidence_id,
            }
        )
        candidates.append(
            {
                "ticker": ticker,
                "name": krx.company_name,
                "risk_penalty": 0,
                "signals": signals,
            }
        )
    return {
        "schema_version": 1,
        "source": "official-pre-market-builder",
        "as_of": as_of.isoformat(),
        "business_date": business_date.isoformat(),
        "mode": "pre-market",
        "candidates": candidates,
    }


def build_morning_input(
    watchlist_payload: object,
    previous_krx_snapshot: KrxDailySnapshot,
    nxt_snapshot_payload: object,
    krx_live_snapshot: KrxLiveActivitySnapshot,
    calendar_bundle: KrxCalendarBundle,
    previous_business_date: date,
    as_of: datetime,
) -> dict[str, Any]:
    """Build morning candidates only after both official markets show same-day trades."""
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of must include a timezone")
    as_of_kst = as_of.astimezone(_KST)
    analysis_time = as_of_kst.timetz().replace(tzinfo=None)
    if not time(9, 20) <= analysis_time < time(12, 0):
        raise ValueError("morning analysis requires 09:20-12:00 KST")
    business_date = as_of_kst.date()
    if calendar_bundle.as_of != as_of:
        raise ValueError("morning calendar bundle must match analysis time")
    if not calendar_bundle.is_scheduled_trading_day(business_date):
        raise ValueError("morning analysis date is a scheduled KRX closure")
    if calendar_bundle.previous_business_date(business_date) != previous_business_date:
        raise ValueError("morning previous date does not match official calendar")
    if (
        krx_live_snapshot.business_date != business_date
        or krx_live_snapshot.markets != (KrxMarket.KOSPI, KrxMarket.KOSDAQ)
        or any(activity.trading_value <= 0 for activity in krx_live_snapshot.activities)
        or krx_live_snapshot.source_as_of.astimezone(_KST).date() != business_date
        or krx_live_snapshot.source_as_of.astimezone(_KST).time() < time(9, 0)
        or krx_live_snapshot.source_as_of > krx_live_snapshot.fetched_at
        or krx_live_snapshot.fetched_at > as_of
        or as_of - krx_live_snapshot.source_as_of > timedelta(minutes=10)
    ):
        raise ValueError("invalid same-day KRX live market activity")
    if (
        not previous_krx_snapshot.coverage_complete
        or previous_krx_snapshot.business_date != previous_business_date
        or previous_business_date >= business_date
        or previous_krx_snapshot.fetched_at > as_of
    ):
        raise ValueError("invalid exact previous KRX daily snapshot")

    stocks = _watchlist(watchlist_payload)
    krx_records = {record.ticker: record for record in previous_krx_snapshot.records}
    quotes = _nxt_quotes(
        nxt_snapshot_payload,
        business_date=business_date,
        as_of=as_of,
        minimum_source_time=time(9, 0),
        require_summary=False,
    )
    if not any(
        _integer_metric(record, "volume") > 0
        and _integer_metric(record, "trading_value") > 0
        for record in quotes.values()
    ):
        raise ValueError("NXT morning snapshot requires positive same-day trading")

    candidates: list[dict[str, Any]] = []
    for ticker, configured_name in stocks:
        krx = krx_records.get(ticker)
        if krx is None:
            raise ValueError(f"watchlist ticker {ticker} is missing from KRX snapshot")
        if krx.company_name != configured_name:
            raise ValueError(f"watchlist name mismatch for {ticker}")
        change_rate = _finite_number(krx.metrics.get("change_rate"), "KRX change_rate")
        volume = _integer_metric(krx, "volume")
        trading_value = _integer_metric(krx, "trading_value")
        signals: list[dict[str, Any]] = [
            {
                "group": "price-volume",
                "score": _score(change_rate, "KRX change_rate"),
                "reason": (
                    f"전 거래일 KRX 등락률 {change_rate:+.2f}%, 거래량 {volume:,}주, "
                    f"거래대금 {trading_value:,}원"
                ),
                "source_url": krx.source_url,
                "observed_at": krx.fetched_at.isoformat(),
                "evidence_id": krx.evidence_id,
            }
        ]
        nxt = quotes.get(ticker)
        if nxt is not None:
            if nxt.company_name != krx.company_name:
                raise ValueError(f"KRX/NXT company name mismatch for {ticker}")
            nxt_change_rate = _finite_number(
                nxt.metrics.get("change_rate"), "NXT change_rate"
            )
            nxt_volume = _integer_metric(nxt, "volume")
            nxt_trading_value = _integer_metric(nxt, "trading_value")
            if nxt_volume > 0 and nxt_trading_value > 0:
                signals.append(
                    {
                        "group": "cross-market",
                        "score": _score(nxt_change_rate, "NXT change_rate"),
                        "reason": (
                            f"당일 NXT 20분 지연 등락률 {nxt_change_rate:+.2f}%, "
                            f"거래량 {nxt_volume:,}주, 거래대금 {nxt_trading_value:,}원"
                        ),
                        "source_url": nxt.source_url,
                        "observed_at": nxt.fetched_at.isoformat(),
                        "evidence_id": nxt.evidence_id,
                    }
                )
        candidates.append(
            {
                "ticker": ticker,
                "name": krx.company_name,
                "risk_penalty": 0,
                "signals": signals,
            }
        )
    return {
        "schema_version": 1,
        "source": "official-morning-builder",
        "as_of": as_of.isoformat(),
        "business_date": business_date.isoformat(),
        "mode": "morning",
        "candidates": candidates,
    }
