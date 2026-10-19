"""Economic event awareness.

Gold reacts sharply to US macro releases, so the risk module wants to know when
one is imminent. The application deliberately does not predict what a release
will do -- it only warns that technical setups can behave abnormally around it.

There is no bundled calendar feed. Events are read from a user-maintained file
at ``~/.goldassist/calendar.json``, which keeps the feature honest: if you have
not supplied a calendar, the app says so rather than pretending to know.

    [
      {"time": "2026-08-20T12:30:00Z", "title": "US CPI (YoY)", "impact": "high"},
      {"time": "2026-08-21T18:00:00Z", "title": "FOMC rate decision", "impact": "high"}
    ]
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from ..config import APP_DIR

log = logging.getLogger(__name__)

CALENDAR_PATH = APP_DIR / "calendar.json"

# This week's full US calendar, written hourly by ``news.calendar_feed`` from
# ForexFactory. Merged into the verified file above when present; its absence
# changes nothing.
LIVE_CALENDAR_PATH = APP_DIR / "calendar_live.json"

_IMPACT_RANK = {"low": 0, "medium": 1, "high": 2}

# Releases Gold is most sensitive to, used to flag high impact when a supplied
# entry omits its own impact field.
HIGH_IMPACT_KEYWORDS = (
    "fomc", "interest rate", "rate decision", "cpi", "pce", "nonfarm",
    "non-farm", "payroll", "unemployment", "gdp", "powell", "fed chair",
    "jackson hole", "ppi", "retail sales",
)


def load(path=None) -> list[dict]:
    """The calendar: the verified file, plus this week's live feed.

    With an explicit ``path`` only that file is read, which keeps tests and
    any caller that names its own calendar exactly as deterministic as before.
    """
    if path is not None:
        return _read(path)
    return merge(_read(CALENDAR_PATH), _read(LIVE_CALENDAR_PATH))


def merge(verified: list[dict], live: list[dict]) -> list[dict]:
    """One entry per release moment.

    The live feed lists CPI as three rows (headline, core, y/y) at 08:30, and
    the verified file has "US CPI (October)" at the same minute. Shown as four
    events, the panel would count four releases and the "next release" line
    would flicker between them. Rows sharing a minute are folded into one
    entry: the verified title wins when there is one, the highest impact
    wins, and every row's forecast and previous are kept as ``details``.
    """
    slots: dict[str, dict] = {}
    for event in verified + live:
        key = event["time"].strftime("%Y-%m-%dT%H:%M")
        slot = slots.setdefault(key, {"time": event["time"], "impact": event["impact"],
                                      "verified": None, "feed": []})
        if _IMPACT_RANK[event["impact"]] > _IMPACT_RANK[slot["impact"]]:
            slot["impact"] = event["impact"]
        if event.get("source"):
            slot["feed"].append({k: event[k] for k in ("title", "impact", "forecast", "previous")
                                 if event.get(k) is not None})
        elif slot["verified"] is None:
            slot["verified"] = event["title"]

    merged = []
    for slot in sorted(slots.values(), key=lambda s: s["time"]):
        feed = slot["feed"]
        if slot["verified"]:
            title = slot["verified"]
        else:
            # Highest-impact rows name the moment; the rest are in details.
            ranked = sorted(feed, key=lambda d: -_IMPACT_RANK[d["impact"]])
            names = [ranked[0]["title"]] + [d["title"].removeprefix("US ") for d in ranked[1:3]]
            title = " · ".join(names) + (f" +{len(feed) - 3}" if len(feed) > 3 else "")
        event = {"time": slot["time"], "title": title, "impact": slot["impact"]}
        if len(feed) == 1:
            for extra in ("forecast", "previous"):
                if feed[0].get(extra):
                    event[extra] = feed[0][extra]
        if feed:
            event["details"] = feed
            event["source"] = "ForexFactory"
        merged.append(event)
    return merged


def _read(source) -> list[dict]:
    """One calendar file. Missing or malformed files yield nothing."""
    if not source.exists():
        return []
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("Could not read the economic calendar at %s: %s", source, exc)
        return []

    events = []
    for item in raw if isinstance(raw, list) else []:
        try:
            when = datetime.fromisoformat(str(item["time"]).replace("Z", "+00:00"))
        except (KeyError, ValueError):
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        title = str(item.get("title", "Economic release"))
        impact = str(item.get("impact", "")).lower()
        if impact not in ("high", "medium", "low"):
            impact = "high" if any(k in title.lower() for k in HIGH_IMPACT_KEYWORDS) else "medium"
        event = {"time": when, "title": title, "impact": impact}
        # The live feed's extras: carried when present, absent otherwise, so
        # an entry from the verified file looks exactly as it always did.
        for extra in ("forecast", "previous", "source"):
            if item.get(extra):
                event[extra] = str(item[extra])
        events.append(event)

    events.sort(key=lambda e: e["time"])
    return events


# How far ahead the calendar must still carry events before it is treated as
# current. Below this the file has run out rather than the market having gone
# quiet, and the two must not look the same.
#
# Thirty days, because FOMC meets roughly every six weeks and CPI monthly: a
# calendar with nothing in the next month is not a quiet month, it is a file
# that has expired. The distinction matters -- an event layer that has silently
# run out reports "no releases due", which reads as an all-clear and is the
# most dangerous thing it could say.
STALE_AFTER_DAYS = 30


def coverage(events: list[dict] | None = None, now: datetime | None = None) -> dict:
    """How far the calendar reaches, and whether that is far enough.

    ``calendar_sync`` carries FOMC and CPI dates as published tables, one year
    at a time, so the file has an end date. Without this check the application
    would cross into a year it has no dates for and simply stop warning about
    releases, with nothing on screen to say the difference between "no releases
    due" and "this file ran out in December".
    """
    events = load() if events is None else events
    now = now or datetime.now(timezone.utc)

    future = [e for e in events if e["time"] >= now]
    if not events:
        return {"available": False, "stale": True, "last_event": None,
                "days_remaining": 0,
                "warning": ("No economic calendar supplied. Run "
                            "`python -m goldassist.analysis.calendar_sync` to "
                            "generate one — until then, releases are invisible "
                            "to the risk layer.")}

    last = max(e["time"] for e in events)
    days = (last - now).total_seconds() / 86400
    if not future:
        return {"available": True, "stale": True,
                "last_event": last.isoformat(), "days_remaining": 0,
                "warning": (f"The economic calendar ended on {last:%d %b %Y} and "
                            "carries nothing ahead. Releases are not being "
                            "warned about. Regenerate it with "
                            "`python -m goldassist.analysis.calendar_sync`.")}
    if days < STALE_AFTER_DAYS:
        return {"available": True, "stale": True,
                "last_event": last.isoformat(), "days_remaining": round(days, 1),
                "warning": (f"The economic calendar runs out on {last:%d %b %Y}, "
                            f"in {days:.0f} days. Regenerate it with "
                            "`python -m goldassist.analysis.calendar_sync` "
                            "before it does.")}
    return {"available": True, "stale": False, "last_event": last.isoformat(),
            "days_remaining": round(days, 1), "warning": None}


# How far back a release still counts as "just happened". Longer than any
# profile's aftermath window, so the profile decides and this only bounds it.
LOOKBACK_HOURS = 6


def upcoming(events: list[dict] | None = None, now: datetime | None = None,
             horizon_hours: int = 24) -> dict:
    """Summarise what is coming, how close the next high-impact release is, and
    how long ago the last one printed.

    ``now`` must be the analysis clock, not the machine's. A replay of last
    March asked with the wall clock sees September's calendar: every historical
    release is invisible, and the news penalty never fires once in the whole
    backtest -- which is exactly what happened until this was threaded through.
    """
    events = load() if events is None else events
    now = now or datetime.now(timezone.utc)
    reach = coverage(events, now)

    if not events:
        return {
            "available": False,
            "note": ("No economic calendar supplied. Add one at "
                     f"{CALENDAR_PATH} to enable event warnings."),
            "next_high_impact": None,
            "last_high_impact": None,
            "upcoming": [],
            "coverage": reach,
        }

    ahead = [e for e in events if 0 <= (e["time"] - now).total_seconds() <= horizon_hours * 3600]
    high = [e for e in ahead if e["impact"] == "high"]
    behind = [e for e in events if e["impact"] == "high"
              and 0 < (now - e["time"]).total_seconds() <= LOOKBACK_HOURS * 3600]

    def render(event: dict) -> dict:
        minutes = int((event["time"] - now).total_seconds() // 60)
        out = {
            "title": event["title"],
            "impact": event["impact"],
            "time": event["time"].isoformat(),
            "minutes_until": minutes,
        }
        for extra in ("forecast", "previous", "details"):
            if event.get(extra):
                out[extra] = event[extra]
        return out

    last = None
    if behind:
        latest = behind[-1]
        last = {"title": latest["title"], "impact": latest["impact"],
                "time": latest["time"].isoformat(),
                "minutes_since": int((now - latest["time"]).total_seconds() // 60)}

    return {
        "available": True,
        # A stale calendar reports its own staleness here rather than staying
        # silent: "nothing due" and "this file ran out" must never read alike.
        "note": reach.get("warning"),
        "next_high_impact": render(high[0]) if high else None,
        "last_high_impact": last,
        "upcoming": [render(e) for e in ahead[:8]],
        "coverage": reach,
    }
