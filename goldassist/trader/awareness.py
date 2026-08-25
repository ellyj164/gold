"""Session awareness: what has already happened today.

An experienced trader carries a running count in their head -- how many setups
have come and gone, how the ones they took worked out, whether the last idea
at this level already failed. That awareness changes how they read the next
setup even though it changes nothing about the chart.

**This module never withholds a setup.** The application does not execute
trades, so there is nothing to protect the trader from by hiding analysis from
them. Every qualifying setup is produced, scored, alerted and displayed, no
matter how many have already appeared today or how the earlier ones went.

What this module contributes is context, and only context: the day's running
record, and a short note when the setup in front of the trader has a history
worth knowing about. Interpretation is theirs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone


@dataclass
class Observation:
    kind: str
    headline: str
    detail: str

    def to_dict(self) -> dict:
        return {"kind": self.kind, "headline": self.headline, "detail": self.detail}


@dataclass
class SessionRecord:
    """Today's running record. Purely informational."""

    counters: dict = field(default_factory=dict)
    observations: list[Observation] = field(default_factory=list)
    headline: str = ""

    def to_dict(self) -> dict:
        return {
            "counters": self.counters,
            "observations": [o.to_dict() for o in self.observations],
            "headline": self.headline,
        }


def day_bounds(moment: datetime | None = None) -> tuple[datetime, datetime]:
    """Start and end of the trader's local day, expressed in UTC."""
    moment = (moment or datetime.now(timezone.utc)).astimezone()
    start_local = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    return (start_local.astimezone(timezone.utc),
            (start_local + timedelta(days=1)).astimezone(timezone.utc))


def gather(db, moment: datetime | None = None) -> dict:
    """Today's counters, read from the recorded outcomes and the journal.

    Setup outcomes are recorded automatically when a tracked setup terminates,
    so the count is right whether or not the trader keeps a journal.
    """
    moment = moment or datetime.now(timezone.utc)
    start, end = day_bounds(moment)

    outcomes = db.setup_outcomes_between(start.isoformat(), end.isoformat())
    filled = [o for o in outcomes if o.get("entered")]
    wins = [o for o in filled if o.get("outcome") == "win"]
    losses = [o for o in filled if o.get("outcome") == "loss"]
    realised_r = sum(float(o.get("r_multiple") or 0.0) for o in filled)

    journal = [e for e in db.journal_entries(limit=200)
               if e.get("took_trade") and _within(e.get("created_at"), start, end)]

    last_loss_at = None
    if losses:
        last_loss_at = max(o.get("closed_at") or o.get("created_at") or ""
                           for o in losses) or None

    return {
        "setups_identified": len(outcomes),
        "setups_filled": len(filled),
        "wins": len(wins),
        "losses": len(losses),
        "consecutive_losses": _consecutive_losses(filled),
        "realised_r": round(realised_r, 2),
        "trades_journalled": len(journal),
        "last_loss_at": last_loss_at,
        "last_loss_minutes": _minutes_since(last_loss_at, moment),
        "day_start": start.isoformat(),
    }


def _within(stamp: str | None, start: datetime, end: datetime) -> bool:
    parsed = _parse(stamp)
    return bool(parsed and start <= parsed < end)


def _parse(stamp: str | None) -> datetime | None:
    if not stamp:
        return None
    try:
        parsed = datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _minutes_since(stamp: str | None, moment: datetime) -> int | None:
    parsed = _parse(stamp)
    if parsed is None:
        return None
    return max(0, int((moment - parsed).total_seconds() // 60))


def _consecutive_losses(outcomes: list[dict]) -> int:
    """Losses in an unbroken run ending at the most recent closed trade."""
    ordered = sorted(
        [o for o in outcomes if o.get("outcome") in ("win", "loss", "scratch")],
        key=lambda o: o.get("closed_at") or o.get("created_at") or "",
        reverse=True,
    )
    run = 0
    for outcome in ordered:
        if outcome["outcome"] != "loss":
            break
        run += 1
    return run


def evaluate(counters: dict, moment: datetime | None = None,
             recent_failure: dict | None = None) -> SessionRecord:
    """Turn today's counters into observations a trader would find useful."""
    moment = moment or datetime.now(timezone.utc)
    observations: list[Observation] = []

    filled = int(counters.get("setups_filled", 0))
    identified = int(counters.get("setups_identified", 0))
    realised = float(counters.get("realised_r", 0.0))
    run = int(counters.get("consecutive_losses", 0))

    if run >= 2:
        observations.append(Observation(
            kind="losing_run",
            headline=f"{run} losses in a row today",
            detail="Not a reason to skip the next setup, but worth knowing "
                   "before you size it. A losing run often says the market is "
                   "not currently offering what this method needs.",
        ))

    minutes = counters.get("last_loss_minutes")
    if minutes is not None and minutes <= 30:
        observations.append(Observation(
            kind="recent_loss",
            headline=f"Last loss was {minutes} min ago",
            detail="The trade taken immediately after a loss is statistically "
                   "the worst-executed one in most trading records. Take it if "
                   "it qualifies, but take it on the setup, not on the last one.",
        ))

    if realised <= -2.0:
        observations.append(Observation(
            kind="drawdown",
            headline=f"Down {realised:.2f}R on the day",
            detail="Worth registering before the next entry so that position "
                   "size is a decision rather than an attempt to get it back.",
        ))
    elif realised >= 2.0:
        observations.append(Observation(
            kind="good_day",
            headline=f"Up {realised:+.2f}R on the day",
            detail="A good day is also the easiest one to give back. The next "
                   "setup is worth the same scrutiny as the first.",
        ))

    if recent_failure:
        age = _minutes_since(recent_failure.get("at"), moment)
        if age is not None and age <= 120:
            low, high = recent_failure.get("low"), recent_failure.get("high")
            where = f" around {low:.2f}-{high:.2f}" if low and high else ""
            observations.append(Observation(
                kind="repeat_idea",
                headline=f"A similar {recent_failure.get('direction', '')} idea "
                         f"failed {age} min ago",
                detail=f"The previous setup{where} was invalidated. If this one "
                       "is the same idea, check what has changed on the chart "
                       "since -- if nothing has, it is a repeat rather than a "
                       "new opportunity.",
            ))

    if identified == 0:
        headline = "No setups have been tracked yet today."
    else:
        headline = (f"{identified} setup(s) tracked today, {filled} filled, "
                    f"{counters.get('wins', 0)}W/{counters.get('losses', 0)}L, "
                    f"{realised:+.2f}R.")

    return SessionRecord(counters=counters, observations=observations,
                         headline=headline)


def same_idea(direction: str, zone, previous: dict | None) -> bool:
    """Whether a new suggestion is materially the same trade as a failed one.

    Used only to attach a note. The setup is presented either way.
    """
    if not previous or zone is None:
        return False
    if previous.get("direction") != direction:
        return False
    low, high = previous.get("low"), previous.get("high")
    if low is None or high is None:
        return False
    # Overlapping entry areas in the same direction are the same idea, however
    # differently the engine has re-derived them.
    return not (zone.high < low or zone.low > high)
