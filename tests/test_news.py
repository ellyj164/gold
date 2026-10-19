"""Checks for the news layer: feeds, tagging, the merged calendar, the desk.

No network anywhere: feeds are parsed from inline RSS, the calendar from an
inline ForexFactory payload, and the desk is run with its fetchers replaced.

**Only gold-relevant headlines are kept from broad feeds**, and the noise
that a gold search drags in -- chart-idea posts, rupee jewellery prices,
personal-finance columns -- is dropped wherever it comes from.

**The keyword fallback knows another economy's CPI is not US CPI.**

**One calendar entry per release moment.** CPI's three feed rows and the
verified "US CPI" at the same minute become one entry, highest impact kept.

**A model's malformed answer lands its valid half and nothing invented.**

**Breaking news alerts once, and only when fresh.** A restart's backlog of
two days must not fire a wall of alerts.

Run with:  python -m tests.test_news
"""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from goldassist.ai import client as ai_client
from goldassist.analysis import events
from goldassist.news import calendar_feed, desk as news_desk, feeds, impact
from goldassist.storage import Database

FAILURES: list[str] = []

NOW = datetime(2026, 10, 19, 16, 0, tzinfo=timezone.utc)


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        FAILURES.append(name)


def rss(items: list[tuple]) -> str:
    """(title, minutes_ago, extra_xml) -> an RSS document."""
    body = []
    for title, minutes, extra in items:
        when = (NOW - timedelta(minutes=minutes)).strftime("%a, %d %b %Y %H:%M:%S GMT")
        body.append(f"<item><title><![CDATA[{title}]]></title><link>https://x.test/{abs(hash(title))}</link>"
                    f"<pubDate>{when}</pubDate>{extra}</item>")
    return f"<?xml version='1.0'?><rss><channel>{''.join(body)}</channel></rss>"


BROAD = feeds.Feed("broad", "Broad wire", "https://x.test/broad")
GOOGLE = next(f for f in feeds.FEEDS if f.key == "google_gold")
FED_PRESS = next(f for f in feeds.FEEDS if f.key == "fed_press")


def test_relevance_and_noise():
    print("feeds: relevance and noise")
    kept = [h.title for h in feeds.parse(BROAD, rss([
        ("Fed's Waller says more rate cuts are likely", 10, ""),
        ("Gold rallies as Treasury yields slide", 20, ""),
        ("Homebuilder earnings beat expectations", 30, ""),
        ("I receive 10 calls a day offering loans at 8% interest", 40, ""),
        ("My wife says we can't afford to retire", 50, ""),
        ("Old story about the dollar", 60 * 50, ""),        # older than 48h
    ]), NOW)]
    check("a Fed headline is kept", "Fed's Waller says more rate cuts are likely" in kept, str(kept))
    check("a gold headline is kept", "Gold rallies as Treasury yields slide" in kept)
    check("an unrelated earnings story is dropped", "Homebuilder earnings beat expectations" not in kept)
    check("'interest' on a loan is not monetary policy",
          not any("8% interest" in t for t in kept))
    check("personal-finance columns are dropped", not any("my wife" in t.lower() for t in kept))
    check("stories older than two days are dropped", "Old story about the dollar" not in kept)

    google = feeds.parse(GOOGLE, rss([
        ("Spot gold climbs to $4,170 - Reuters", 5, "<source>Reuters</source>"),
        ("FX:XAUUSD Chart Image by Some_Trader - TradingView", 6, "<source>TradingView</source>"),
        ("Gold climbs Rs 400 to Rs 1.5 lakh per 10 gm - Deccan Herald", 7, "<source>Deccan Herald</source>"),
    ]), NOW)
    check("a focused feed keeps its gold story", len(google) == 1, str([h.title for h in google]))
    check("the publisher replaces the aggregator as the source",
          google and google[0].source_name == "Reuters")
    check("and is stripped from the title", google and google[0].title == "Spot gold climbs to $4,170")

    fed = [h.title for h in feeds.parse(FED_PRESS, rss([
        ("Federal Reserve issues FOMC statement", 5, ""),
        ("Federal Reserve Board announces termination of enforcement action", 6, ""),
    ]), NOW)]
    check("the Fed's policy statements are kept", fed == ["Federal Reserve issues FOMC statement"], str(fed))

    a = feeds.headline_id("Gold rallies as yields slide", "https://a")
    b = feeds.headline_id("Gold Rallies as Yields Slide!", "https://b")
    check("the same story from two feeds has one id", a == b)


def test_keyword_fallback():
    print("impact: the keyword fallback")
    cases = {
        "Canada September CPI 3.1% y/y vs 3.2% expected": ("low", "neutral"),
        "US CPI rises 0.4% m/m, hotter than expected": ("high", "bearish"),
        "Fed cuts rates by 25 bp, Powell signals more easing": ("high", "bullish"),
        "Gold falls as dollar strengthens, yields rise": ("medium", "bearish"),
        "Euro: Bullish tone persists against US Dollar": ("low", "neutral"),
    }
    for title, (want_impact, want_bias) in cases.items():
        got = impact.classify(title)
        check(f"{title[:45]} -> {want_impact}/{want_bias}",
              (got["impact"], got["bias"]) == (want_impact, want_bias), str(got))
    check("it always says it is a keyword tag",
          impact.classify("anything")["classified_by"] == "keywords")


def test_calendar_merge():
    print("calendar: feed rows fold into one entry per moment")
    raw = [
        {"title": "CPI m/m", "country": "USD", "date": "2026-11-10T08:30:00-05:00",
         "impact": "High", "forecast": "0.3%", "previous": "0.4%"},
        {"title": "Core CPI m/m", "country": "USD", "date": "2026-11-10T08:30:00-05:00",
         "impact": "High", "forecast": "0.3%", "previous": "0.3%"},
        {"title": "Natural Gas Storage", "country": "USD", "date": "2026-11-12T10:30:00-05:00",
         "impact": "Low", "forecast": "", "previous": "67B"},
        {"title": "German ZEW", "country": "EUR", "date": "2026-11-10T05:00:00-05:00",
         "impact": "High", "forecast": "", "previous": ""},
        {"title": "Unemployment Claims", "country": "USD", "date": "2026-11-12T08:30:00-05:00",
         "impact": "Medium", "forecast": "200K", "previous": "199K"},
    ]
    live = calendar_feed.normalise(raw)
    check("only US releases are kept", all(e["currency"] == "USD" for e in live))
    check("low-impact rows are left out", not any("Natural Gas" in e["title"] for e in live))
    check("times are converted to UTC", live[0]["time"] == "2026-11-10T13:30:00+00:00", live[0]["time"])

    folder = Path(tempfile.mkdtemp())
    verified_path, live_path = folder / "calendar.json", folder / "live.json"
    verified_path.write_text(json.dumps([
        {"time": "2026-11-10T13:30:00Z", "title": "US CPI (October)", "impact": "high"}]))
    live_path.write_text(json.dumps(live))
    merged = events.merge(events._read(verified_path), events._read(live_path))
    cpi = [e for e in merged if e["time"].day == 10]
    check("CPI is one entry, not three", len(cpi) == 1, str(cpi))
    check("the verified title wins", cpi and cpi[0]["title"] == "US CPI (October)")
    check("both feed rows are kept as details", cpi and len(cpi[0].get("details", [])) == 2)
    claims = [e for e in merged if "Claims" in e["title"]]
    check("a feed-only release carries its forecast",
          claims and claims[0].get("forecast") == "200K", str(claims))
    check("an explicit path still reads only that file",
          len(events.load(verified_path)) == 1)


def test_model_readings_are_validated():
    print("classifier: a model's answer is validated item by item")
    text = ('Sure! ```json\n[{"id": "a", "bias": "bullish", "impact": "high", "reason": "war"},'
            ' {"id": "b", "bias": "sideways", "impact": "high"},'
            ' {"id": "zzz", "bias": "bearish", "impact": "low"}]```')
    got = ai_client._parse_readings(text, {"a", "b"})
    check("the valid reading is kept", [r["id"] for r in got] == ["a"], str(got))
    check("a reading with an invented value is dropped", all(r["id"] != "b" for r in got))
    check("an id that was never sent is dropped", all(r["id"] != "zzz" for r in got))
    check("prose with no array yields nothing", ai_client._parse_readings("no idea", {"a"}) == [])


def headline(title: str, minutes_ago: int, now: datetime) -> feeds.Headline:
    return feeds.Headline(id=feeds.headline_id(title, ""), source="broad",
                          source_name="Wire", title=title, link="https://x.test",
                          summary="", published_at=(now - timedelta(minutes=minutes_ago)).isoformat())


def test_desk_cycle():
    print("desk: tag, store, alert once and only when fresh")
    db = Database(Path(tempfile.mkdtemp()) / "news.db")
    now = datetime.now(timezone.utc)
    stories = [
        headline("Fed cuts rates in surprise move", 10, now),           # fresh, high
        headline("War breaks out, oil and gold spike", 600, now),       # high but 10h old
        headline("Dollar edges lower ahead of data", 20, now),
    ]
    original_fetch, original_sync = news_desk.feeds.fetch_all, news_desk.calendar_feed.sync
    news_desk.feeds.fetch_all = lambda now=None: (stories, {"broad": {"name": "Wire", "ok": True, "kept": 3}})
    news_desk.calendar_feed.sync = lambda path=None: {"ok": True, "written": 0}
    calls = []

    def model(items):
        calls.append(len(items))
        return ([{"id": i["id"], "bias": "bullish", "impact": "high" if "Fed" in i["title"] else "low",
                  "reason": "model reading"} for i in items], "nvidia/test-model")

    alerts = []
    try:
        desk = news_desk.NewsDesk(db, model, on_breaking=alerts.append)
        state = desk.cycle()
        desk.cycle()
    finally:
        news_desk.feeds.fetch_all, news_desk.calendar_feed.sync = original_fetch, original_sync

    rows = {r["title"]: r for r in db.recent_news(hours=24)}
    check("every story is stored once", len(rows) == 3, str(len(rows)))
    check("the model's reading replaces the keyword tag",
          rows["Fed cuts rates in surprise move"]["classified_by"] == "nvidia/test-model")
    check("the fresh high-impact story alerts", [a["title"] for a in alerts] ==
          ["Fed cuts rates in surprise move"], str([a["title"] for a in alerts]))
    check("the old one does not, however big", all("War" not in a["title"] for a in alerts))
    check("a second cycle does not alert again", len(alerts) == 1)
    check("stories already read by the model are not re-sent", calls == [3], str(calls))
    check("the state names the model", state["classifier"] == "nvidia/test-model")
    check("high impact is listed first", state["items"][0]["impact"] == "high")

    # With no model, keyword tags stay and say so.
    db2 = Database(Path(tempfile.mkdtemp()) / "news.db")
    news_desk.feeds.fetch_all = lambda now=None: (stories, {})
    news_desk.calendar_feed.sync = lambda path=None: {"ok": True, "written": 0}
    try:
        state2 = news_desk.NewsDesk(db2, lambda items: ([], None)).cycle()
    finally:
        news_desk.feeds.fetch_all, news_desk.calendar_feed.sync = original_fetch, original_sync
    check("offline, stories are keyword-tagged",
          all(r["classified_by"] == "keywords" for r in db2.recent_news(hours=24)))
    check("and the state says no model tagged them", state2["classifier"] is None)


def test_news_reaches_the_analyst():
    print("analyst: headlines are in the facts")
    digest = ai_client.news_digest({"items": [
        {"published_at": "2026-10-19T15:00:00+00:00", "source_name": "Reuters",
         "title": "Fed's Powell signals patience", "bias": "bearish", "impact": "high",
         "reason": "hawkish repricing", "link": "https://x"}],
        "balance": {"bullish": 0, "bearish": 1, "neutral": 0}})
    check("the headline is there", digest["headlines"][0]["title"] == "Fed's Powell signals patience")
    check("links are not sent to the model", "link" not in digest["headlines"][0])
    check("it says it is context, not a signal", "not a signal" in digest["note"])
    check("no news is no block, not an empty one", ai_client.news_digest({"items": []}) is None)


def main() -> int:
    for test in (test_relevance_and_noise, test_keyword_fallback, test_calendar_merge,
                 test_model_readings_are_validated, test_desk_cycle,
                 test_news_reaches_the_analyst):
        test()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) failed:")
        for name in FAILURES:
            print(f"  - {name}")
        return 1
    print("All news checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
