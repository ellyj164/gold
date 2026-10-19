"""This week's full US calendar, from ForexFactory's public weekly feed.

``analysis/calendar_sync`` carries only the three release families whose dates
can be verified a year ahead -- FOMC, CPI, payrolls -- and says plainly that
PPI, PCE, GDP, retail sales, claims and the PMIs are missing. They move gold
too. This fills that gap for the current week, which is the week a trader is
trading, from the feed most retail desks already watch.

What the feed gives: every release with its scheduled time, impact (High /
Medium / Low), forecast and previous. What it does not give: the actual
print -- the weekly JSON carries no results -- and anything beyond this week.
Both are stated rather than papered over: weeks ahead still come from the
verified FOMC/CPI/payrolls file, and an actual is never invented.

The result is written to ``~/.goldassist/calendar_live.json`` in the same
shape as ``calendar.json``, so :mod:`goldassist.analysis.events` reads it on
the analysis thread as a local file -- no network call ever sits in the
five-second cycle.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from ..analysis import events
from ..fundamentals import http

log = logging.getLogger(__name__)

URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

# Hourly: the schedule changes rarely, and the feed's operators ask that it
# not be polled hard.
TTL = 3600.0

# Gold is priced in dollars and its macro drivers are American; other
# currencies' releases are left out rather than diluting the warnings.
CURRENCIES = {"USD"}

# Low-impact rows (natural gas storage, the API oil bulletin, regional
# surveys) do not move gold and would crowd the real releases out of the
# "upcoming" list, so they are left out. PPI, PCE, GDP, retail sales, claims
# and the PMIs are all Medium or High on this feed.
IMPACTS = {"High": "high", "Medium": "medium"}


def normalise(raw: list) -> list[dict]:
    """Feed rows -> calendar entries in UTC, US releases only."""
    out = []
    for row in raw if isinstance(raw, list) else []:
        if not isinstance(row, dict) or row.get("country") not in CURRENCIES:
            continue
        impact = IMPACTS.get(str(row.get("impact")))
        if impact is None:              # Low, "Holiday", "Non-Economic"
            continue
        try:
            when = datetime.fromisoformat(str(row["date"]))
        except (KeyError, ValueError):
            continue
        if when.tzinfo is None:
            continue                    # a time with no zone is not a time
        out.append({
            "time": when.astimezone(timezone.utc).isoformat(),
            "title": f"US {row.get('title', 'release')}".strip(),
            "impact": impact,
            "currency": row.get("country"),
            "forecast": (row.get("forecast") or None),
            "previous": (row.get("previous") or None),
            "source": "ForexFactory",
        })
    out.sort(key=lambda e: e["time"])
    return out


def sync(path=None) -> dict:
    """Fetch the week and write the live calendar. Never raises."""
    target = path or events.LIVE_CALENDAR_PATH
    data, fetched = http.get_json(URL, ttl=TTL)
    if data is None:
        return {"ok": False, "error": fetched.error or "no data", "written": 0}
    entries = normalise(data)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(entries, indent=1), encoding="utf-8")
        tmp.replace(target)          # atomic: the analysis thread never reads half a file
    except OSError as exc:
        log.warning("Could not write the live calendar: %s", exc)
        return {"ok": False, "error": str(exc), "written": 0}
    return {"ok": True, "written": len(entries),
            "stale": bool(fetched.error) and fetched.from_cache,
            "error": fetched.error or ""}
