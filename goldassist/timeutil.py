"""Display time, in the trader's own clock.

Everything inside the application works in UTC: the candle index, every
timestamp written to the database, every `created_at` on a setup. That is the
right internal convention and none of it changes here.

But a trader does not read UTC. They read the clock on their wall, and they
read it against the session -- "07:14, London open" means something; "05:14Z"
means nothing without arithmetic. This module is the single place that
translates between the two, so that a timestamp rendered on a signal card, in
the signal log, in an alert and in an exported record can never disagree.

The display zone is fixed rather than taken from the operating system on
purpose. The trader's method is written in GMT+2 -- their sessions, their
journal, their notes on when Gold moves -- and that must not silently change
because a laptop travelled or a machine has its clock set wrong. It is
configurable (``display_utc_offset`` in config.json) for the day that stops
being true.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

# The trader's clock: GMT+2, fixed offset, no daylight-saving shift.
#
# A fixed offset is deliberate. A named zone such as Europe/Kigali would be
# equivalent today, but a zone that observes DST would move the session
# boundaries twice a year relative to the London and New York opens the method
# is actually built around -- which is precisely the mistake this avoids.
DEFAULT_UTC_OFFSET_HOURS = 2.0

_offset_hours = DEFAULT_UTC_OFFSET_HOURS


def configure(offset_hours: float | None) -> None:
    """Set the display offset, once, at start-up."""
    global _offset_hours
    if offset_hours is None:
        return
    _offset_hours = float(offset_hours)


def display_tz() -> timezone:
    return timezone(timedelta(hours=_offset_hours))


def offset_hours() -> float:
    return _offset_hours


def offset_label() -> str:
    """``GMT+2`` / ``GMT+5:30`` / ``GMT-4`` -- how the clock is named in the UI."""
    hours = _offset_hours
    sign = "+" if hours >= 0 else "-"
    hours = abs(hours)
    whole = int(hours)
    minutes = int(round((hours - whole) * 60))
    return f"GMT{sign}{whole}" + (f":{minutes:02d}" if minutes else "")


# --- parsing ----------------------------------------------------------------


def to_utc(value) -> datetime | None:
    """Coerce whatever a caller has into an aware UTC datetime, or None.

    Accepts datetimes (aware or naive), ISO strings with or without a zone,
    and epoch seconds. A naive value is read as UTC, because every naive
    timestamp this application produces is UTC.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    else:
        text = str(value).strip().replace("Z", "+00:00")
        try:
            moment = datetime.fromisoformat(text)
        except ValueError:
            return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def to_display(value) -> datetime | None:
    """The same moment, on the trader's clock."""
    moment = to_utc(value)
    return moment.astimezone(display_tz()) if moment else None


# --- formatting -------------------------------------------------------------


def clock(value) -> str:
    """``14:37`` -- the time a signal fired, on the trader's clock."""
    moment = to_display(value)
    return moment.strftime("%H:%M") if moment else "—"


def clock_seconds(value) -> str:
    """``14:37:05`` -- for the tape, where the second matters."""
    moment = to_display(value)
    return moment.strftime("%H:%M:%S") if moment else "—"


def date(value) -> str:
    """``2026-08-20`` -- the trading date on the trader's clock.

    Note this is the *local* date, which is what belongs on a signal log. A
    signal fired at 23:30 GMT+2 is a Thursday signal to the trader even though
    UTC has not yet agreed.
    """
    moment = to_display(value)
    return moment.strftime("%Y-%m-%d") if moment else "—"


def stamp(value) -> str:
    """``2026-08-20 14:37`` -- date and time together."""
    moment = to_display(value)
    return moment.strftime("%Y-%m-%d %H:%M") if moment else "—"


def full(value) -> str:
    """``2026-08-20 14:37 GMT+2`` -- unambiguous, for exports and tooltips."""
    moment = to_display(value)
    return f"{moment.strftime('%Y-%m-%d %H:%M')} {offset_label()}" if moment else "—"


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return now_utc().isoformat()


def describe(value) -> dict:
    """Everything the interface needs about one moment, computed once.

    Returned on every signal so the browser never has to do timezone
    arithmetic of its own -- the one place a display bug would otherwise hide.
    """
    moment = to_utc(value)
    if moment is None:
        return {}
    from .trader import sessions

    read = sessions.read(moment)
    return {
        "utc": moment.isoformat(),
        "date": date(moment),
        "clock": clock(moment),
        "clock_seconds": clock_seconds(moment),
        "stamp": stamp(moment),
        "full": full(moment),
        "offset": offset_label(),
        "session": read.name,
        "session_label": read.to_dict()["label"],
        "session_quality": read.quality,
    }


def session_options() -> list[dict]:
    """The sessions a signal can be filtered by, in day order.

    Sourced from the session table itself rather than hard-coded in the
    interface, so a change to the windows cannot leave the filter offering a
    session that no signal will ever carry.
    """
    from .trader import sessions

    options = []
    for name, start, end, quality, description in sessions.WINDOWS:
        options.append({
            "name": name,
            "label": sessions.LABELS.get(name, name.title()),
            "quality": quality,
            "description": description,
            # The window on the trader's clock, which is how they will
            # recognise it -- London opening at 07:00 UTC is 09:00 to them.
            "window": f"{_hh(start)}–{_hh(end)}",
        })
    return options


def _hh(utc_hour: int) -> str:
    """A UTC window boundary, shown on the display clock."""
    shifted = (utc_hour + _offset_hours) % 24
    whole = int(shifted)
    minutes = int(round((shifted - whole) * 60))
    return f"{whole:02d}:{minutes:02d}"
