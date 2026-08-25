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


def upcoming(events: list[dict] | None = None, now: datetime | None = None,
             horizon_hours: int = 24) -> dict:
    """Summarise what is coming, and how close the next high-impact release is."""
    events = load() if events is None else events
    now = now or datetime.now(timezone.utc)

    if not events:
        return {
            "available": False,
            "note": ("No economic calendar supplied. Add one at "
                     f"{CALENDAR_PATH} to enable event warnings."),
            "next_high_impact": None,
            "upcoming": [],
        }

    ahead = [e for e in events if 0 <= (e["time"] - now).total_seconds() <= horizon_hours * 3600]
    high = [e for e in ahead if e["impact"] == "high"]

    def render(event: dict) -> dict:
        minutes = int((event["time"] - now).total_seconds() // 60)
        return {
            "title": event["title"],
            "impact": event["impact"],
            "time": event["time"].isoformat(),
            "minutes_until": minutes,
        }

    return {
        "available": True,
        "note": None,
        "next_high_impact": render(high[0]) if high else None,
        "upcoming": [render(e) for e in ahead[:8]],
    }
