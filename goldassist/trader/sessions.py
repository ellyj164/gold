"""Trading sessions, from a Gold day trader's point of view.

The engines elsewhere use sessions only as a source of reference levels. A
human trader uses them for something else entirely: deciding *whether this is
a sensible hour to be looking for a trade at all*.

Gold does not distribute its movement evenly through the day. The London/New
York overlap carries most of the real directional business; the Asian session
is thin and mean-reverting; the hours around the broker rollover are close to
untradeable. A trader who takes the same setup in all three is really trading
three different instruments.

Windows are UTC, which is what the candle index uses.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timezone

# (name, start_hour, end_hour, quality, description)
#
# ``quality`` is a 0-1 multiplier on conviction, not a probability. It encodes
# how much a discretionary Gold trader would trust a setup found at that hour.
# The overlap is the reference point at 1.0; everything else is measured
# against it.
WINDOWS = (
    ("rollover",     21, 23, 0.10, "broker rollover — spreads widen, liquidity thins"),
    ("asia",         23,  7, 0.35, "Asian session — thin, range-bound, prone to fading moves"),
    ("london",        7, 12, 0.85, "London session — the first genuine directional window"),
    ("overlap",      12, 16, 1.00, "London/New York overlap — deepest liquidity of the day"),
    ("newyork",      16, 19, 0.60, "New York afternoon — trends persist but participation falls"),
    ("late_newyork", 19, 21, 0.35, "late New York — drifting, position-squaring"),
)

# Narrower windows worth naming in their own right: they change how a setup
# should be read even inside an otherwise good session.
SUB_WINDOWS = (
    ("london_open", time(7, 0),  time(9, 0),
     "London open — expect a liquidity sweep of the Asian range before the real move"),
    ("data_window", time(12, 30), time(14, 0),
     "US data window — releases land here; moves can reverse violently"),
    ("ny_open",     time(13, 30), time(15, 0),
     "New York equity open — the highest-volume hours for Gold"),
)

WEEKEND_CLOSE = 4, 21   # Friday 21:00 UTC
WEEKEND_OPEN = 6, 22    # Sunday 22:00 UTC


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


def _in_window(hour: int, start: int, end: int) -> bool:
    # Windows that cross midnight (the Asian session) wrap around.
    return start <= hour < end if start < end else hour >= start or hour < end


def window_for(moment: datetime) -> tuple[str, float, str]:
    hour = moment.hour
    for name, start, end, quality, description in WINDOWS:
        if _in_window(hour, start, end):
            return name, quality, description
    return "asia", 0.35, "outside the named windows"


def market_open(moment: datetime) -> bool:
    """Whether the Gold market is trading at this moment.

    Gold closes Friday evening and reopens Sunday evening. Standing aside is
    not a decision the app should have to make when there is nothing to trade.
    """
    weekday, hour = moment.weekday(), moment.hour
    if weekday == 5:                                     # Saturday
        return False
    if weekday == WEEKEND_CLOSE[0] and hour >= WEEKEND_CLOSE[1]:
        return False
    if weekday == WEEKEND_OPEN[0] and hour < WEEKEND_OPEN[1]:
        return False
    return True


def minutes_to_next(moment: datetime) -> tuple[int, str]:
    """How long the current window has left, and what follows it."""
    current, _, _ = window_for(moment)
    boundaries = sorted({start for _, start, _, _, _ in WINDOWS})
    minute_of_day = moment.hour * 60 + moment.minute
    for start in boundaries:
        if start * 60 > minute_of_day:
            nxt, _, _ = window_for(moment.replace(hour=start, minute=1))
            return start * 60 - minute_of_day, nxt
    # Past the last boundary of the day: the next window opens tomorrow.
    first = boundaries[0]
    nxt, _, _ = window_for(moment.replace(hour=first, minute=1))
    return (24 * 60 - minute_of_day) + first * 60, nxt


def read(moment: datetime | None = None) -> SessionRead:
    """The full session picture for one moment in time."""
    moment = (moment or datetime.now(timezone.utc)).astimezone(timezone.utc)
    is_open = market_open(moment)
    name, quality, description = window_for(moment)
    if not is_open:
        name, quality, description = "closed", 0.0, "Gold is closed for the weekend"

    clock = moment.time()
    subs = [
        note for key, start, end, note in SUB_WINDOWS
        if is_open and start <= clock < end
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
