"""Trading sessions, from a Gold day trader's point of view.

The engines elsewhere use sessions only as a source of reference levels. A
human trader uses them for something else entirely: deciding *whether this is
a sensible hour to be looking for a trade at all*.

Gold does not distribute its movement evenly through the day. The London/New
York overlap carries most of the real directional business; the Asian session
is thin and mean-reverting; the hours around the broker rollover are close to
untradeable. A trader who takes the same setup in all three is really trading
three different instruments.

Windows are anchored to the market each one belongs to, not to a fixed UTC
hour, and converted through the timezone database for the moment being read.

That distinction is worth an hour twice a year, and an hour is the whole
window. London and New York observe daylight saving on different dates from
each other and Tokyo observes none at all, so there is no fixed UTC offset that
describes all three. Hard-coding UTC hours calibrates the sessions for one half
of the year and leaves them wrong for the other: with summer constants, the New
York equity open in January falls at 14:30 UTC while the window that is meant
to name it still sits at 13:30-15:00, so half of the highest-volume hour of the
Gold day is labelled as something else -- and the "US data window" stops
covering the 08:30 Eastern releases it exists for.

``analysis/calendar_sync`` already resolved exactly this for economic releases;
these windows now use the same mechanism, and the two agree by construction.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

TOKYO = ZoneInfo("Asia/Tokyo")
LONDON = ZoneInfo("Europe/London")
NEWYORK = ZoneInfo("America/New_York")

# (name, market, local start hour, local start minute, quality, description)
#
# Each window starts at a wall-clock hour *in the market it belongs to* and
# runs until the next one opens. The list is in chronological order through the
# trading day, beginning with the Asian open.
#
# ``quality`` is a 0-1 multiplier on conviction, not a probability. It encodes
# how much a discretionary Gold trader would trust a setup found at that hour.
# The overlap is the reference point at 1.0; everything else is measured
# against it.
#
# These figures are reasoned rather than measured. `trader/expectancy.py` now
# measures what each session has actually paid this system and applies it as a
# separate, sample-shrunk multiplier, so this table is the prior and that is
# the evidence.
WINDOWS = (
    ("asia",         TOKYO,    8, 0, 0.35,
     "Asian session — thin, range-bound, prone to fading moves"),
    ("london",       LONDON,   8, 0, 0.85,
     "London session — the first genuine directional window"),
    ("overlap",      NEWYORK,  8, 0, 1.00,
     "London/New York overlap — deepest liquidity of the day"),
    ("newyork",      NEWYORK, 12, 0, 0.60,
     "New York afternoon — trends persist but participation falls"),
    ("late_newyork", NEWYORK, 15, 0, 0.35,
     "late New York — drifting, position-squaring"),
    ("rollover",     NEWYORK, 17, 0, 0.10,
     "broker rollover — spreads widen, liquidity thins"),
)

# Narrower windows worth naming in their own right: they change how a setup
# should be read even inside an otherwise good session.
#
# ``data_window`` is anchored to 08:30 Eastern because that is when the BLS
# releases land -- the same instant `analysis/calendar_sync` writes into the
# calendar. Anchoring it to a UTC hour instead is how it drifted off them.
SUB_WINDOWS = (
    ("london_open", LONDON,  time(8, 0),  time(10, 0),
     "London open — expect a liquidity sweep of the Asian range before the real move"),
    ("data_window", NEWYORK, time(8, 30), time(10, 0),
     "US data window — releases land here; moves can reverse violently"),
    ("ny_open",     NEWYORK, time(9, 30), time(11, 0),
     "New York equity open — the highest-volume hours for Gold"),
)

# The weekly close and reopen, in New York wall-clock time: Gold stops trading
# at the Friday rollover and comes back on Sunday evening.
WEEKEND_CLOSE_LOCAL = 4, time(17, 0)   # Friday 17:00 New York
WEEKEND_OPEN_LOCAL = 6, time(18, 0)    # Sunday 18:00 New York


def _utc_hour(moment: datetime, zone: ZoneInfo, at: time) -> float:
    """A local wall-clock time expressed as that day's UTC hour-of-day.

    Returned as a float so a half-hour boundary (the 08:30 Eastern releases)
    survives the conversion.
    """
    local = moment.astimezone(zone)
    stamp = datetime.combine(local.date(), at, tzinfo=zone).astimezone(timezone.utc)
    return stamp.hour + stamp.minute / 60.0


@dataclass
class SessionRead:
    name: str
    quality: float
    description: str
    sub_windows: list[str]
    notes: list[str]
    minutes_to_next: int
    next_session: str
    market_open: bool
    utc_time: str
    local_time: str

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "label": LABELS.get(self.name, self.name.title()),
            "quality": round(self.quality, 2),
            "description": self.description,
            "sub_windows": self.sub_windows,
            "notes": self.notes,
            "minutes_to_next": self.minutes_to_next,
            "next_session": LABELS.get(self.next_session, self.next_session),
            "market_open": self.market_open,
            "utc_time": self.utc_time,
            "local_time": self.local_time,
        }


LABELS = {
    "rollover": "Rollover",
    "asia": "Asian session",
    "london": "London",
    "overlap": "London/NY overlap",
    "newyork": "New York",
    "late_newyork": "Late New York",
    "closed": "Market closed",
}


def _in_window(hour: float, start: float, end: float) -> bool:
    # Windows that cross midnight (the Asian session) wrap around.
    return start <= hour < end if start < end else hour >= start or hour < end


def boundaries(moment: datetime) -> list[tuple[str, float, float, float, str]]:
    """The day's windows as UTC hour ranges, resolved for this date.

    Each window runs from its own market's opening hour until the next
    window's, so the six of them always tile the whole day however the
    daylight-saving offsets happen to fall.
    """
    starts = [(name, _utc_hour(moment, zone, time(hour, minute)), quality, note)
              for name, zone, hour, minute, quality, note in WINDOWS]
    out = []
    for i, (name, start, quality, note) in enumerate(starts):
        end = starts[(i + 1) % len(starts)][1]
        out.append((name, start, end, quality, note))
    return out


def window_for(moment: datetime) -> tuple[str, float, str]:
    hour = moment.hour + moment.minute / 60.0
    for name, start, end, quality, description in boundaries(moment):
        if _in_window(hour, start, end):
            return name, quality, description
    return "asia", 0.35, "outside the named windows"


def market_open(moment: datetime) -> bool:
    """Whether the Gold market is trading at this moment.

    Gold closes Friday evening and reopens Sunday evening. Standing aside is
    not a decision the app should have to make when there is nothing to trade.
    """
    weekday = moment.weekday()
    hour = moment.hour + moment.minute / 60.0
    if weekday == 5:                                     # Saturday
        return False
    close_day, close_at = WEEKEND_CLOSE_LOCAL
    open_day, open_at = WEEKEND_OPEN_LOCAL
    if weekday == close_day and hour >= _utc_hour(moment, NEWYORK, close_at):
        return False
    if weekday == open_day and hour < _utc_hour(moment, NEWYORK, open_at):
        return False
    return True


def minutes_to_next(moment: datetime) -> tuple[int, str]:
    """How long the current window has left, and what follows it."""
    today = boundaries(moment)
    edges = sorted({start for _, start, _, _, _ in today})
    minute_of_day = moment.hour * 60 + moment.minute
    for start in edges:
        start_minute = int(round(start * 60))
        if start_minute > minute_of_day:
            nxt, _, _ = window_for(moment + timedelta(
                minutes=start_minute - minute_of_day + 1))
            return start_minute - minute_of_day, nxt
    # Past the last boundary of the day: the next window opens tomorrow.
    first = int(round(edges[0] * 60))
    remaining = (24 * 60 - minute_of_day) + first
    nxt, _, _ = window_for(moment + timedelta(minutes=remaining + 1))
    return remaining, nxt


def read(moment: datetime | None = None) -> SessionRead:
    """The full session picture for one moment in time."""
    moment = (moment or datetime.now(timezone.utc)).astimezone(timezone.utc)
    is_open = market_open(moment)
    name, quality, description = window_for(moment)
    if not is_open:
        name, quality, description = "closed", 0.0, "Gold is closed for the weekend"

    hour = moment.hour + moment.minute / 60.0
    subs = [
        note for key, zone, start, end, note in SUB_WINDOWS
        if is_open and _in_window(hour, _utc_hour(moment, zone, start),
                                  _utc_hour(moment, zone, end))
    ]

    notes: list[str] = []
    if is_open:
        if quality <= 0.35:
            notes.append("Low-quality hours: a setup here needs more evidence, "
                         "not the same evidence.")
        if name == "asia":
            notes.append("Asian ranges are usually swept, not respected — "
                         "breakouts in this session fail more often than they run.")
        if name == "rollover":
            notes.append("Spreads widen through rollover; measured reward/risk "
                         "flatters the real trade.")
        if any("data window" in s for s in subs):
            notes.append("A scheduled release can invalidate technical structure "
                         "in seconds. Standing aside through it is normal.")

    remaining, nxt = minutes_to_next(moment)
    return SessionRead(
        name=name,
        quality=quality,
        description=description,
        sub_windows=subs,
        notes=notes,
        minutes_to_next=remaining,
        next_session=nxt,
        market_open=is_open,
        utc_time=moment.strftime("%H:%M UTC"),
        local_time=moment.astimezone().strftime("%H:%M %Z"),
    )
