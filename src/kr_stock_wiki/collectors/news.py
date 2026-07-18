from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from email.utils import parsedate_to_datetime
from enum import StrEnum
from typing import Any, Callable
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zoneinfo import ZoneInfo

from defusedxml import ElementTree as ET
from defusedxml.common import DefusedXmlException

from ..evidence import EvidenceRecord, EvidenceSource, VerificationStatus


Transport = Callable[[str, float], bytes]
Clock = Callable[[], datetime]
_BASE_URL = "https://www.yna.co.kr/rss"
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_RSS_WINDOW_SIZE = 120
_KST = ZoneInfo("Asia/Seoul")
_ARTICLE_ID = re.compile(r"AKR\d{17}")
_CREATOR_TAG = "{http://purl.org/dc/elements/1.1/}creator"


class NewsFeed(StrEnum):
    ECONOMY = "economy"
    INDUSTRY = "industry"
    MARKET = "market"


class NewsError(ValueError):
    pass


class NewsTransportError(NewsError):
    pass


class NewsResponseError(NewsError):
    pass


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _trusted_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme == "https" and parsed.hostname == "www.yna.co.kr"


def _default_transport(url: str, timeout: float) -> bytes:
    if not _trusted_url(url):
        raise NewsTransportError("news request blocked: untrusted endpoint")
    opener = build_opener(_NoRedirect)
    request = Request(url, headers={"Accept": "application/rss+xml, application/xml"})
    # Redirects are disabled and both requested and final URLs are allowlisted.
    with opener.open(request, timeout=timeout) as response:  # nosec B310
        if not _trusted_url(response.geturl()):
            raise NewsTransportError("news response blocked: untrusted endpoint")
        content_length = response.headers.get("Content-Length")
        if content_length is not None:
            try:
                declared_size = int(content_length)
            except ValueError as error:
                raise NewsTransportError("news response has invalid size") from error
            if declared_size > _MAX_RESPONSE_BYTES:
                raise NewsTransportError("news response exceeds size limit")
        payload = response.read(_MAX_RESPONSE_BYTES + 1)
        if len(payload) > _MAX_RESPONSE_BYTES:
            raise NewsTransportError("news response exceeds size limit")
        return payload


def _text(item: Any, tag: str, context: str) -> str:
    value = item.findtext(tag)
    if value is None or not value.strip():
        raise NewsResponseError(f"news {context} is missing {tag}")
    return " ".join(value.split())


def _article_id(link: str) -> str:
    parsed = urlparse(link)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "www.yna.co.kr"
        or not parsed.path.startswith("/view/")
    ):
        raise NewsResponseError("news article has an untrusted link")
    article_id = parsed.path.removeprefix("/view/").strip("/")
    if not _ARTICLE_ID.fullmatch(article_id):
        raise NewsResponseError("news article has an invalid ID")
    return article_id


def _published_at(item: Any, context: str) -> tuple[datetime, str]:
    published_text = _text(item, "pubDate", context)
    try:
        published_at = parsedate_to_datetime(published_text)
    except (TypeError, ValueError) as error:
        raise NewsResponseError(f"news {context} has an invalid pubDate") from error
    if published_at.tzinfo is None or published_at.utcoffset() is None:
        raise NewsResponseError(f"news {context} pubDate must include a timezone")
    return published_at.astimezone(_KST), published_text


@dataclass
class YonhapRssClient:
    transport: Transport = field(default=_default_transport, repr=False)
    clock: Clock = field(default=lambda: datetime.now().astimezone(), repr=False)
    timeout: float = 15.0

    def __post_init__(self) -> None:
        if self.timeout <= 0:
            raise ValueError("timeout must be positive")

    def _fetch(self, feed: NewsFeed) -> Any:
        url = f"{_BASE_URL}/{feed.value}.xml"
        try:
            payload = self.transport(url, self.timeout)
        except (OSError, TimeoutError):
            raise NewsTransportError("news RSS request failed") from None
        if not isinstance(payload, bytes):
            raise NewsResponseError("news RSS transport must return bytes")
        if len(payload) > _MAX_RESPONSE_BYTES:
            raise NewsResponseError("news RSS response exceeds size limit")
        try:
            root = ET.fromstring(
                payload,
                forbid_dtd=True,
                forbid_entities=True,
                forbid_external=True,
            )
        except (ET.ParseError, DefusedXmlException, UnicodeError) as error:
            raise NewsResponseError("news RSS returned invalid XML") from error
        channel = root.find("channel")
        if channel is None or "연합뉴스" not in (channel.findtext("title") or ""):
            raise NewsResponseError("news RSS has an invalid channel")
        return channel

    def latest(
        self,
        begin: date,
        end: date,
        *,
        feeds: tuple[NewsFeed, ...] = tuple(NewsFeed),
    ) -> list[EvidenceRecord]:
        if begin > end:
            raise ValueError("begin must not be after end")
        if not feeds:
            raise ValueError("at least one news feed is required")

        articles: dict[str, dict] = {}
        for feed in feeds:
            channel = self._fetch(feed)
            items = channel.findall("item")
            parsed_items: list[tuple[Any, str, str, datetime, str]] = []
            for item in items:
                link = _text(item, "link", "item")
                guid = _text(item, "guid", "item")
                if guid != link:
                    raise NewsResponseError("news item GUID and link differ")
                article_id = _article_id(link)
                published_at, published_text = _published_at(item, article_id)
                parsed_items.append(
                    (item, link, article_id, published_at, published_text)
                )
            if len(parsed_items) >= _RSS_WINDOW_SIZE:
                oldest = min(entry[3] for entry in parsed_items)
                if begin <= oldest.date():
                    raise NewsResponseError(
                        f"news {feed.value} RSS coverage is incomplete: "
                        f"{len(parsed_items)} items, oldest {oldest.isoformat()}"
                    )
            for item, link, article_id, published_at, published_text in parsed_items:
                if not begin <= published_at.date() <= end:
                    continue
                title = _text(item, "title", article_id)
                creator = _text(item, _CREATOR_TAG, article_id)
                description = _text(item, "description", article_id)
                identity = {
                    "title": title,
                    "link": link,
                    "published_at": published_at,
                    "published_text": published_text,
                    "creator": creator,
                    "description": description,
                }
                existing = articles.get(article_id)
                if existing is not None:
                    if existing["identity"] != identity:
                        raise NewsResponseError(
                            f"news {article_id} changed across RSS feeds"
                        )
                    existing["categories"].add(feed.value)
                    continue
                articles[article_id] = {
                    "identity": identity,
                    "categories": {feed.value},
                }

        fetched_at = self.clock()
        records: list[EvidenceRecord] = []
        ordered = sorted(
            articles.items(),
            key=lambda pair: (pair[1]["identity"]["published_at"], pair[0]),
            reverse=True,
        )
        for article_id, article in ordered:
            identity = article["identity"]
            categories = sorted(article["categories"])
            evidence_id = f"yonhap:{article_id}"
            records.append(
                EvidenceRecord(
                    source=EvidenceSource.OFFICIAL_NEWS,
                    evidence_id=evidence_id,
                    canonical_event_id=evidence_id,
                    kind="news-article",
                    company_name="연합뉴스",
                    title=identity["title"],
                    source_url=identity["link"],
                    published_date=identity["published_at"].date(),
                    fetched_at=fetched_at,
                    verification=VerificationStatus.OFFICIAL,
                    metrics={
                        "publisher": "연합뉴스",
                        "creator": identity["creator"],
                        "published_at": identity["published_at"].isoformat(),
                        "feed_categories": ",".join(categories),
                    },
                    raw={
                        "guid": identity["link"],
                        "pub_date": identity["published_text"],
                        "description": identity["description"],
                        "categories": categories,
                    },
                )
            )
        return records
