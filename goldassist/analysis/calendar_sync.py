"""Build the economic calendar :mod:`goldassist.analysis.events` reads.

``events.py`` has always been able to warn about releases and has never once
done it, because it reads a file the user was expected to write by hand and
nobody ever did. An event layer that is switched off by default is an event
layer that does not exist, and Gold is the most release-sensitive instrument
on the board -- an FOMC decision moves it further in ninety seconds than a
good technical setup targets in a day.

This module generates that file from dates that can be checked, and refuses
to generate anything else.

Three release families are included, and only three:

``FOMC``
    Rate decisions, from the Federal Reserve's own published meeting calendar
    (federalreserve.gov/monetarypolicy/fomccalendars.htm). The decision lands
    on the *second* day of each two-day meeting at 14:00 US Eastern.

``CPI``
    From the BLS release schedule, at 08:30 US Eastern. These dates are not
    derivable from a rule -- they are published per year and have to be
    carried as data.

``Employment Situation`` (nonfarm payrolls)
    The one family that *is* a rule: the first Friday of the month, 08:30 US
    Eastern. Verified against the published 2026 dates for September (the
    4th), October (the 2nd) and November (the 6th) before being trusted.

Deliberately absent: PPI, retail sales, GDP, PCE and the rest. They move Gold
too, but their dates are neither rule-derivable nor to hand, and a calendar
that quietly guesses is worse than one that admits a gap -- a missing warning
is a known unknown, a wrong one is a trap. Add them by hand to the generated
file, or extend ``CPI_RELEASES`` when the schedule is published.

Times are converted from US Eastern to UTC through the real timezone database,
so the daylight-saving shift is handled rather than approximated. That shift
is worth an hour, and an hour is the difference between a warning that fires
before a release and one that fires after it.

Run it with::

    python -m goldassist.analysis.calendar_sync            # current year on
    python -m goldassist.analysis.calendar_sync --year 2026
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from ..config import APP_DIR

log = logging.getLogger(__name__)

EASTERN = ZoneInfo("America/New_York")
CALENDAR_PATH = APP_DIR / "calendar.json"

# FOMC decision days: the closing day of each meeting, at 14:00 Eastern.
# Source: the Federal Reserve's published calendar. Meetings marked with a
# Summary of Economic Projections carry a press conference and move Gold more,
# which is recorded so the note can say so.
FOMC_2026 = [
    (date(2026, 1, 28), False),
    (date(2026, 3, 18), True),
    (date(2026, 4, 29), False),
    (date(2026, 6, 17), True),
    (date(2026, 7, 29), False),
    (date(2026, 9, 16), True),
    (date(2026, 10, 28), False),
    (date(2026, 12, 9), True),
]

# CPI release days at 08:30 Eastern, keyed by the month whose data is being
# reported. Source: the BLS release schedule, cross-checked against a second
# published calendar for September, October and December.
CPI_RELEASES_2026 = [
    (date(2026, 1, 13), "December 2025"),
    (date(2026, 2, 13), "January"),
    (date(2026, 3, 11), "February"),
    (date(2026, 4, 10), "March"),
    (date(2026, 5, 12), "April"),
    (date(2026, 6, 10), "May"),
    (date(2026, 7, 14), "June"),
    (date(2026, 8, 12), "July"),
    (date(2026, 9, 11), "August"),
    (date(2026, 10, 14), "September"),
    (date(2026, 11, 10), "October"),
    (date(2026, 12, 10), "November"),
]

FOMC_BY_YEAR = {2026: FOMC_2026}
CPI_BY_YEAR = {2026: CPI_RELEASES_2026}

RELEASE_TIME = time(8, 30)      # BLS releases, Eastern
DECISION_TIME = time(14, 0)     # FOMC statement, Eastern


def _utc(day: date, at: time) -> str:
    """An Eastern wall-clock moment as a UTC ISO stamp.

    Going through the timezone database rather than a fixed offset is the
    whole point: 08:30 Eastern is 12:30 UTC in October and 13:30 UTC in
    November, and a calendar that gets that wrong warns an hour late twice a
    year.
    """
    local = datetime.combine(day, at, tzinfo=EASTERN)
    return local.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def first_friday(year: int, month: int) -> date:
    """The first Friday of a month -- when nonfarm payrolls are released."""
    day = date(year, month, 1)
    # weekday(): Monday is 0, Friday is 4.
    return day + timedelta(days=(4 - day.weekday()) % 7)


def generate(year: int) -> list[dict]:
    """Every event this module is willing to vouch for, for one year."""
    events: list[dict] = []

    for day, has_projections in FOMC_BY_YEAR.get(year, []):
        extra = (" with the Summary of Economic Projections and a press conference"
                 if has_projections else "")
        events.append({
            "time": _utc(day, DECISION_TIME),
            "title": "FOMC rate decision",
            "impact": "high",
            "note": f"Federal Reserve policy statement at 14:00 ET{extra}.",
        })

    for day, reference in CPI_BY_YEAR.get(year, []):
        events.append({
            "time": _utc(day, RELEASE_TIME),
            "title": f"US CPI ({reference})",
            "impact": "high",
            "note": "Bureau of Labor Statistics, 08:30 ET.",
        })

    for month in range(1, 13):
        day = first_friday(year, month)
        events.append({
            "time": _utc(day, RELEASE_TIME),
            "title": "US Employment Situation (nonfarm payrolls)",
            "impact": "high",
            "note": "Bureau of Labor Statistics, 08:30 ET. First Friday of the month.",
        })

    events.sort(key=lambda e: e["time"])
    return events


def write(years: list[int], path=None) -> tuple[int, object]:
    """Write the calendar file, merging with anything already there.

    Hand-added entries survive a regeneration: this is a seed and a top-up,
    not an owner of the file. Identity is (time, title), so re-running is
    idempotent and a corrected official date adds rather than replaces -- if a
    release moves, delete the stale line by hand.
    """
    target = path or CALENDAR_PATH
    existing: list[dict] = []
    if target.exists():
        try:
            loaded = json.loads(target.read_text(encoding="utf-8"))
            existing = loaded if isinstance(loaded, list) else []
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("Existing calendar at %s unreadable, starting fresh: %s",
                        target, exc)

    merged = {(e.get("time"), e.get("title")): e for e in existing}
    for year in years:
        for event in generate(year):
            merged.setdefault((event["time"], event["title"]), event)

    out = sorted(merged.values(), key=lambda e: str(e.get("time")))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(out, indent=2), encoding="utf-8")
    return len(out), target


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate the economic calendar the event layer reads.")
    parser.add_argument("--year", type=int, action="append", dest="years",
                        help="Year to generate (repeatable). Defaults to the "
                             "current year and the next.")
    parser.add_argument("--path", default=None, help="Write somewhere other "
                                                     "than the app directory.")
    args = parser.parse_args(argv)

    this_year = datetime.now(timezone.utc).year
    years = args.years or [this_year, this_year + 1]

    known = sorted(set(FOMC_BY_YEAR) | set(CPI_BY_YEAR))
    for year in years:
        if year not in FOMC_BY_YEAR and year not in CPI_BY_YEAR:
            print(f"  note: no published FOMC or CPI dates carried for {year} "
                  f"(have: {', '.join(map(str, known))}). Payrolls are still "
                  f"generated from the first-Friday rule.")

    from pathlib import Path
    count, target = write(years, Path(args.path) if args.path else None)
    print(f"Wrote {count} events to {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
