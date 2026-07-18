from datetime import date, datetime
from zoneinfo import ZoneInfo

import kr_stock_wiki.collectors.news as news_module
from kr_stock_wiki.collectors.news import (
    NewsFeed,
    NewsResponseError,
    NewsTransportError,
    YonhapRssClient,
)
from kr_stock_wiki.evidence import EvidenceSource, VerificationStatus


def _feed_xml(channel: str, items: str) -> bytes:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/">
      <channel><title>{channel}</title>{items}</channel>
    </rss>""".encode()


def _item(article_id: str, title: str, published: str, description: str) -> str:
    url = f"https://www.yna.co.kr/view/{article_id}"
    return f"""
      <item>
        <title>{title}</title><link>{url}</link><guid>{url}</guid>
        <pubDate>{published}</pubDate><dc:creator>홍길동</dc:creator>
        <description>{description}</description>
      </item>"""


def test_news_default_transport_enforces_redirect_host_and_size(monkeypatch):
    import pytest

    assert (
        news_module._NoRedirect().redirect_request(
            None, None, 302, "Found", {}, "https://evil.example"
        )
        is None
    )
    with pytest.raises(NewsTransportError, match="untrusted endpoint"):
        news_module._default_transport("http://127.0.0.1/feed", 1)

    class FakeResponse:
        def __init__(self, url: str, payload: bytes, content_length: str | None = None):
            self.url = url
            self.payload = payload
            self.headers = {}
            if content_length is not None:
                self.headers["Content-Length"] = content_length

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def geturl(self):
            return self.url

        def read(self, limit: int):
            return self.payload[:limit]

    class FakeOpener:
        def __init__(self, response: FakeResponse):
            self.response = response

        def open(self, _request, timeout: float):
            assert timeout == 1
            return self.response

    def install(response: FakeResponse):
        monkeypatch.setattr(
            news_module, "build_opener", lambda _handler: FakeOpener(response)
        )

    install(FakeResponse("http://127.0.0.1/feed", b"{}"))
    with pytest.raises(NewsTransportError, match="untrusted endpoint"):
        news_module._default_transport("https://www.yna.co.kr/rss/market.xml", 1)

    install(
        FakeResponse("https://www.yna.co.kr/rss/market.xml", b"{}", "not-an-integer")
    )
    with pytest.raises(NewsTransportError, match="invalid size"):
        news_module._default_transport("https://www.yna.co.kr/rss/market.xml", 1)

    install(
        FakeResponse(
            "https://www.yna.co.kr/rss/market.xml",
            b"{}",
            str(news_module._MAX_RESPONSE_BYTES + 1),
        )
    )
    with pytest.raises(NewsTransportError, match="size limit"):
        news_module._default_transport("https://www.yna.co.kr/rss/market.xml", 1)

    install(
        FakeResponse(
            "https://www.yna.co.kr/rss/market.xml",
            b"x" * (news_module._MAX_RESPONSE_BYTES + 1),
        )
    )
    with pytest.raises(NewsTransportError, match="size limit"):
        news_module._default_transport("https://www.yna.co.kr/rss/market.xml", 1)

    install(FakeResponse("https://www.yna.co.kr/rss/market.xml", b"{}"))
    assert (
        news_module._default_transport("https://www.yna.co.kr/rss/market.xml", 1)
        == b"{}"
    )


def test_yonhap_rss_rejects_invalid_configuration_transport_and_xml():
    import pytest

    with pytest.raises(ValueError, match="timeout"):
        YonhapRssClient(timeout=0)
    with pytest.raises(ValueError, match="begin"):
        YonhapRssClient().latest(date(2026, 7, 19), date(2026, 7, 18))
    with pytest.raises(ValueError, match="at least one"):
        YonhapRssClient().latest(date(2026, 7, 18), date(2026, 7, 18), feeds=())

    failing = YonhapRssClient(
        transport=lambda _url, _timeout: (_ for _ in ()).throw(OSError())
    )
    with pytest.raises(NewsTransportError, match="request failed"):
        failing.latest(
            date(2026, 7, 18),
            date(2026, 7, 18),
            feeds=(NewsFeed.MARKET,),
        )

    malformed = [
        (b"<!DOCTYPE rss><rss/>", "invalid XML"),
        ("<?xml version='1.0'?><!DOCTYPE rss><rss/>".encode("utf-16"), "invalid XML"),
        (b"not-xml", "invalid XML"),
        (
            "<rss><channel><title>위조 피드</title></channel></rss>".encode(),
            "invalid channel",
        ),
    ]
    for payload, message in malformed:
        client = YonhapRssClient(transport=lambda _url, _timeout, value=payload: value)
        with pytest.raises(NewsResponseError, match=message):
            client.latest(
                date(2026, 7, 18),
                date(2026, 7, 18),
                feeds=(NewsFeed.MARKET,),
            )

    oversized = YonhapRssClient(
        transport=lambda _url, _timeout: b"x" * (news_module._MAX_RESPONSE_BYTES + 1)
    )
    with pytest.raises(NewsResponseError, match="size limit"):
        oversized.latest(
            date(2026, 7, 18),
            date(2026, 7, 18),
            feeds=(NewsFeed.MARKET,),
        )


def test_yonhap_rss_rejects_untrusted_or_inconsistent_article_metadata():
    import pytest

    valid = _item(
        "AKR20260718000100001",
        "기사",
        "Sat, 18 Jul 2026 09:30:00 +0900",
        "설명",
    )

    cases = [
        (valid.replace("<guid>https://", "<guid>http://"), "GUID and link differ"),
        (valid.replace("<title>기사</title>", "<title></title>"), "missing title"),
        (
            valid.replace("AKR20260718000100001", "BAD"),
            "invalid ID",
        ),
        (
            valid.replace("https://www.yna.co.kr/view/", "https://evil.example/view/"),
            "untrusted link",
        ),
        (
            valid.replace("Sat, 18 Jul 2026 09:30:00 +0900", "not-a-date"),
            "invalid pubDate",
        ),
        (
            valid.replace(
                "Sat, 18 Jul 2026 09:30:00 +0900", "Sat, 18 Jul 2026 09:30:00"
            ),
            "timezone",
        ),
    ]
    for item, message in cases:
        client = YonhapRssClient(
            transport=lambda _url, _timeout, value=item: _feed_xml(
                "연합뉴스 마켓+ 최신기사", value
            )
        )
        with pytest.raises(NewsResponseError, match=message):
            client.latest(
                date(2026, 7, 18),
                date(2026, 7, 18),
                feeds=(NewsFeed.MARKET,),
            )

    def conflicting_transport(url: str, _timeout: float) -> bytes:
        title = "첫 제목" if url.endswith("economy.xml") else "변경된 제목"
        return _feed_xml(
            "연합뉴스 최신기사",
            _item(
                "AKR20260718000100001",
                title,
                "Sat, 18 Jul 2026 09:30:00 +0900",
                "설명",
            ),
        )

    with pytest.raises(NewsResponseError, match="changed across RSS feeds"):
        YonhapRssClient(transport=conflicting_transport).latest(
            date(2026, 7, 18),
            date(2026, 7, 18),
            feeds=(NewsFeed.ECONOMY, NewsFeed.MARKET),
        )


def test_yonhap_rss_rejects_truncated_window_and_normalizes_dates_to_kst():
    import pytest

    window = "".join(
        _item(
            f"AKR20260717{index:09d}",
            f"과거 기사 {index}",
            "Fri, 17 Jul 2026 12:00:00 +0900",
            "설명",
        )
        for index in range(120)
    )
    truncated = YonhapRssClient(
        transport=lambda _url, _timeout: _feed_xml("연합뉴스 경제 최신기사", window)
    )
    with pytest.raises(NewsResponseError, match="coverage is incomplete"):
        truncated.latest(
            date(2026, 7, 17),
            date(2026, 7, 18),
            feeds=(NewsFeed.ECONOMY,),
        )

    utc_item = _item(
        "AKR20260717000100001",
        "KST 자정 이후 기사",
        "Fri, 17 Jul 2026 15:30:00 +0000",
        "설명",
    )
    records = YonhapRssClient(
        transport=lambda _url, _timeout: _feed_xml("연합뉴스 마켓+ 최신기사", utc_item)
    ).latest(
        date(2026, 7, 18),
        date(2026, 7, 18),
        feeds=(NewsFeed.MARKET,),
    )
    assert len(records) == 1
    assert records[0].published_date == date(2026, 7, 18)
    assert records[0].metrics["published_at"] == "2026-07-18T00:30:00+09:00"
    assert records[0].raw["pub_date"] == "Fri, 17 Jul 2026 15:30:00 +0000"


def test_yonhap_rss_collects_date_range_and_merges_cross_feed_duplicates():
    duplicate = _item(
        "AKR20260718000100001",
        "반도체 수출 증가",
        "Sat, 18 Jul 2026 09:30:00 +0900",
        "반도체 업황 기사",
    )
    old = _item(
        "AKR20260716000100001",
        "범위 밖 기사",
        "Thu, 16 Jul 2026 09:30:00 +0900",
        "오래된 기사",
    )

    def transport(url: str, _timeout: float) -> bytes:
        if url.endswith("economy.xml"):
            return _feed_xml("연합뉴스 경제 최신기사", duplicate + old)
        if url.endswith("market.xml"):
            return _feed_xml("연합뉴스 마켓+ 최신기사", duplicate)
        raise AssertionError(url)

    fetched_at = datetime(2026, 7, 18, 20, 30, tzinfo=ZoneInfo("Asia/Seoul"))
    records = YonhapRssClient(transport=transport, clock=lambda: fetched_at).latest(
        date(2026, 7, 18),
        date(2026, 7, 18),
        feeds=(NewsFeed.ECONOMY, NewsFeed.MARKET),
    )

    assert len(records) == 1
    record = records[0]
    assert record.source is EvidenceSource.OFFICIAL_NEWS
    assert record.verification is VerificationStatus.OFFICIAL
    assert record.evidence_id == "yonhap:AKR20260718000100001"
    assert record.canonical_event_id == record.evidence_id
    assert record.kind == "news-article"
    assert record.company_name == "연합뉴스"
    assert record.title == "반도체 수출 증가"
    assert record.published_date == date(2026, 7, 18)
    assert record.fetched_at == fetched_at
    assert record.metrics["publisher"] == "연합뉴스"
    assert record.metrics["creator"] == "홍길동"
    assert record.metrics["published_at"] == "2026-07-18T09:30:00+09:00"
    assert record.metrics["feed_categories"] == "economy,market"
    assert record.raw["description"] == "반도체 업황 기사"
