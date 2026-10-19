"""Headline feeds: where gold-moving news is read from, and what is kept.

Every source here is a public RSS feed that answered this application's own
User-Agent on 2026-10-19, read through :mod:`goldassist.fundamentals.http` so
it inherits that client's rules -- cached on disk, stale-rather-than-nothing,
backed off after a failure, never raising.

**Two kinds of source.** *Focused* feeds are about gold or are official policy
(the Google News gold search, the Fed's own speeches); everything they carry is
kept. *Broad* feeds (FXStreet, InvestingLive, CNBC, MarketWatch, Investing.com)
carry the whole market, so an item is kept only when its headline touches what
moves gold: the metal itself, the Fed and rates, inflation, the dollar, jobs
and growth, and the geopolitical and trade shocks that send money to a haven.
A headline about a homebuilder's earnings is noise on a gold desk, and a
classifier shown it would still have to say so.

Deliberately absent: Kitco (its RSS is gone, 404) and Investing.com's metals
analysis feed (unchanged since August 2026 -- a stale feed reads as a quiet
market).
"""

from __future__ import annotations

import hashlib
import html
import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

from ..fundamentals import http

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Feed:
    key: str
    name: str
    url: str
    focused: bool = False      # keep everything, no relevance filter
    ttl: float = 170.0         # seconds a fetched copy is current


FEEDS: tuple[Feed, ...] = (
    # Phrases, not the bare word: "gold" alone returns jewellery prices in
    # rupees, mining features and coin-collecting tips.
    Feed("google_gold", "Google News · gold",
         "https://news.google.com/rss/search?q=%22spot+gold%22+OR+%22gold+prices%22"
         "+OR+%22gold+futures%22+OR+%22gold+rallies%22+OR+%22gold+falls%22"
         "+when:1d&hl=en-US&gl=US&ceid=US:en", focused=True),
    Feed("investinglive", "InvestingLive", "https://investinglive.com/feed/"),
    Feed("fxstreet", "FXStreet", "https://www.fxstreet.com/rss/news"),
    Feed("fed_speeches", "Federal Reserve · speeches",
         "https://www.federalreserve.gov/feeds/speeches.xml", focused=True, ttl=900),
    Feed("fed_press", "Federal Reserve · press",
         "https://www.federalreserve.gov/feeds/press_all.xml", ttl=900),
    Feed("cnbc_economy", "CNBC · economy",
         "https://www.cnbc.com/id/20910258/device/rss/rss.html"),
    Feed("cnbc_top", "CNBC · top news",
         "https://www.cnbc.com/id/100003114/device/rss/rss.html"),
    Feed("marketwatch", "MarketWatch", "https://feeds.content.dowjones.io/public/rss/mw_topstories"),
    Feed("investing_commodities", "Investing.com · commodities",
         "https://www.investing.com/rss/news_11.rss"),
    Feed("investing_economy", "Investing.com · economy",
         "https://www.investing.com/rss/news_95.rss"),
)

# Newest items kept per feed. One chatty source must not drown the others.
PER_FEED = 30

# How old a headline may be and still be news. Two days covers a weekend's
# Sunday-evening open; anything older is history the chart has already priced.
MAX_AGE = timedelta(hours=48)

# What makes a broad-feed headline relevant to gold. Word-boundary matched, so
# "rate" does not match "corporate" and "war" does not match "award".
RELEVANT = re.compile(
    r"\b("
    r"gold|xau|bullion|precious metals?|silver|"
    r"fed|fomc|powell|federal reserve|central banks?|"
    r"rate (cut|hike)s?|interest rates?|yields?|treasur(y|ies)|"
    r"inflation|cpi|pce|ppi|"
    r"dollar|dxy|greenback|usd|"
    r"jobs|payrolls?|nfp|unemployment|jobless|labor market|"
    r"gdp|recession|stimulus|shutdown|"
    r"tariffs?|trade war|sanctions?|"
    r"war|missiles?|strikes?|attack(s|ed)?|ceasefire|invasion|military|nuclear|"
    r"geopolitic\w*|safe[- ]haven|haven|crisis|"
    r"iran|israel|gaza|russia|ukraine|china|taiwan|middle east|north korea"
    r")\b",
    re.IGNORECASE,
)

# Never news for a spot-gold desk, whichever feed carries it: chart-idea
# posts (TradingView reaches Google News as "XAUUSD Chart Image by ..."),
# retail jewellery prices quoted in rupees per ten grams around Indian
# festivals, and personal-finance advice columns.
EXCLUDE = re.compile(
    r"(chart image by|\blakh\b|\btola\b|\brs\.? ?[\d,]+|₹|\bper 10 ?(gm|grams?)\b|"
    r"\bjewell?ery\b|\bdiwali\b|\bdhanteras\b|\bdussehra\b|\bakshaya\b|"
    r"\b\d{2} ?(k|carat|karat)\b|\begp\b|\bpkr\b|"
    r"\bmy (wife|husband|son|daughter|mother|father|sister|brother|partner)\b|"
    r"\bretire(ment|d)?\b)", re.IGNORECASE)
EXCLUDED_PUBLISHERS = {"TradingView"}

# The Fed's press feed is mostly bank supervision. Only monetary policy moves
# gold, so from that feed only these are kept.
FED_PRESS_POLICY = re.compile(
    r"\b(fomc|federal open market|monetary policy|statement|minutes|discount rate|"
    r"interest rate|balance sheet|beige book|projections)\b", re.IGNORECASE)

_TAGS = re.compile(r"<[^>]+>")


@dataclass
class Headline:
    id: str
    source: str
    source_name: str
    title: str
    link: str
    summary: str
    published_at: str          # UTC ISO

    def to_dict(self) -> dict:
        return dict(self.__dict__)


def _clean(text: str | None, limit: int = 400) -> str:
    text = html.unescape(_TAGS.sub(" ", text or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def _when(raw: str | None) -> datetime | None:
    if not raw:
        return None
    raw = raw.strip()
    try:
        moment = parsedate_to_datetime(raw)            # RFC 822, most feeds
    except (TypeError, ValueError):
        moment = None
    if moment is None:
        # The last is Investing.com's: no zone, and UTC (its 14:06 NAHB story
        # is the 10:00 New York release).
        for fmt in ("%b %d, %Y %H:%M GMT", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ",
                    "%Y-%m-%d %H:%M:%S"):
            try:
                moment = datetime.strptime(raw, fmt)
                break
            except ValueError:
                continue
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def headline_id(title: str, link: str) -> str:
    """Stable across feeds for the same story, so it is stored once.

    Keyed on the normalised title rather than the link: the same Reuters
    story reaches Google News and MarketWatch under different URLs.
    """
    key = re.sub(r"[^a-z0-9]+", " ", title.lower()).strip() or link
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def parse(feed: Feed, body: str, now: datetime | None = None) -> list[Headline]:
    """Items from one RSS document, filtered for age and relevance."""
    now = now or datetime.now(timezone.utc)
    try:
        root = ET.fromstring(body.encode("utf-8") if isinstance(body, str) else body)
    except ET.ParseError as exc:
        log.info("News feed %s did not parse: %s", feed.key, exc)
        return []

    out: list[Headline] = []
    for item in root.iter("item"):
        title = _clean(item.findtext("title"), 300)
        if not title:
            continue
        link = (item.findtext("link") or "").strip()
        published = _when(item.findtext("pubDate"))
        if published is None or now - published > MAX_AGE or published - now > timedelta(hours=1):
            continue

        source_name = feed.name
        if feed.key == "google_gold":
            # Google News titles end " - Publisher"; the publisher is the
            # source the trader cares about, not the aggregator.
            publisher = (item.findtext("source") or "").strip()
            if publisher:
                source_name = publisher
                if title.endswith(f" - {publisher}"):
                    title = title[: -len(publisher) - 3].strip()

        if source_name in EXCLUDED_PUBLISHERS or EXCLUDE.search(title):
            continue

        summary = _clean(item.findtext("description"))
        if summary.lower().startswith(title.lower()[:40]):
            summary = ""            # Google repeats the title as the description

        if feed.key == "fed_press" and not FED_PRESS_POLICY.search(title):
            continue
        if not feed.focused and not RELEVANT.search(f"{title} {summary[:200]}"):
            continue

        out.append(Headline(
            id=headline_id(title, link), source=feed.key, source_name=source_name,
            title=title, link=link, summary=summary,
            published_at=published.isoformat()))
    return out


def fetch_all(now: datetime | None = None) -> tuple[list[Headline], dict]:
    """Every relevant headline from every feed, deduplicated, newest first.

    Also returns per-feed health, so the panel can say "CNBC has not answered
    since 14:02" instead of quietly showing fewer headlines.
    """
    seen: dict[str, Headline] = {}
    health: dict[str, dict] = {}
    for feed in FEEDS:
        fetched = http.get(feed.url, ttl=feed.ttl)
        if not fetched.ok:
            health[feed.key] = {"name": feed.name, "ok": False, "error": fetched.error, "kept": 0}
            continue
        items = sorted(parse(feed, fetched.body, now),
                       key=lambda h: h.published_at, reverse=True)[:PER_FEED]
        health[feed.key] = {"name": feed.name, "ok": True, "kept": len(items),
                            "stale": bool(fetched.error) and fetched.from_cache,
                            "error": fetched.error or ""}
        for item in items:
            # First source wins: FEEDS is ordered fastest-and-most-specific first.
            seen.setdefault(item.id, item)
    headlines = sorted(seen.values(), key=lambda h: h.published_at, reverse=True)
    return headlines, health
