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

# Releases Gold is most sensitive to, used to flag high impact when a supplied
# entry omits its own impact field.
HIGH_IMPACT_KEYWORDS = (
    "fomc", "interest rate", "rate decision", "cpi", "pce", "nonfarm",
    "non-farm", "payroll", "unemployment", "gdp", "powell", "fed chair",
    "jackson hole", "ppi", "retail sales",
)


def load(path=None) -> list[dict]:
    """Read the user's calendar file. Missing or malformed files yield nothing."""
    source = path or CALENDAR_PATH
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
        events.append({"time": when, "title": title, "impact": impact})

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
        return {
            "title": event["title"],
            "impact": event["impact"],
            "time": event["time"].isoformat(),
            "minutes_until": minutes,
        }

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
