"""The session thesis: a view that is held, not re-derived.

The engines in this application form a fresh opinion every five seconds. That
is machine behaviour. A trader forms a view at the start of the session --
"Gold is bid, I'm buying dips into H4 demand, I'm wrong below 4362" -- writes
it down, and then trades *against that view* for the rest of the day, changing
it only when the market gives them a reason.

The difference is not cosmetic. A held view produces patience, because a setup
either fits the plan or it does not. A view re-derived every tick produces the
opposite: whatever the last few candles happened to say.

So the thesis here is deliberately sticky. It is formed once per trading day
and it is revised only when *both* of these are true:

* the higher timeframes have disagreed with it continuously for at least
  ``revision_minutes``, and
* an H4 or H1 structure break has printed in the new direction since the
  thesis was formed.

Disagreement that has not yet met that bar is recorded as a *challenge*, and
shown to the trader, because "my view is being tested" is itself information.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

INTACT = "intact"
CHALLENGED = "challenged"
REVISED = "revised"


@dataclass
class Thesis:
    date: str
    direction: str                    # bullish | bearish | neutral
    conviction: str                   # strong | moderate | tentative
    statement: str
    rationale: list[str] = field(default_factory=list)
    key_levels: dict = field(default_factory=dict)
    invalidated_if: str = ""
    macro_note: str = ""
    formed_at: str = ""
    status: str = INTACT
    challenged_since: str | None = None
    challenge_direction: str | None = None
    revisions: list[dict] = field(default_factory=list)
    observations: int = 0

    def to_dict(self) -> dict:
        return {
            "date": self.date,
            "direction": self.direction,
            "conviction": self.conviction,
            "statement": self.statement,
            "rationale": self.rationale,
            "key_levels": self.key_levels,
            "invalidated_if": self.invalidated_if,
            "macro_note": self.macro_note,
            "formed_at": self.formed_at,
            "status": self.status,
            "challenged_since": self.challenged_since,
            "challenge_direction": self.challenge_direction,
            "challenged_minutes": self.challenged_minutes(),
            "revisions": self.revisions,
            "observations": self.observations,
            "age_minutes": _minutes_since(self.formed_at),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Thesis":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})

    def challenged_minutes(self) -> int:
        return _minutes_since(self.challenged_since) if self.challenged_since else 0

    def agrees_with(self, direction: str) -> bool:
        return self.direction == "neutral" or self.direction == direction


def _minutes_between(stamp: str | None, moment: datetime) -> int:
    """Minutes from ``stamp`` to ``moment``, both timezone-aware."""
    if not stamp:
        return 0
    try:
        start = datetime.fromisoformat(stamp)
    except ValueError:
        return 0
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    return max(0, int((moment - start).total_seconds() // 60))


def _minutes_since(stamp: str | None) -> int:
    if not stamp:
        return 0
    try:
        moment = datetime.fromisoformat(stamp)
    except ValueError:
        return 0
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return max(0, int((datetime.now(timezone.utc) - moment).total_seconds() // 60))


def _today(moment: datetime | None = None) -> str:
    return (moment or datetime.now(timezone.utc)).astimezone().strftime("%Y-%m-%d")


def form(analysis: dict, decision: dict, macro=None,
         moment: datetime | None = None) -> Thesis:
    """Write down the view for today, in the terms a trader would use."""
    moment = moment or datetime.now(timezone.utc)
    alignment = analysis.get("alignment") or {}
    direction = alignment.get("direction") or decision.get("direction") or "neutral"
    agreement = float(alignment.get("agreement") or 0.0)
    regime = (analysis.get("regime") or {}).get("regime", "unknown")
    price = float(analysis.get("price") or 0.0)

    conviction = ("strong" if agreement >= 0.75 else
                  "moderate" if agreement >= 0.55 else "tentative")

    rationale: list[str] = []
    reads = alignment.get("reads") or {}
    for tf in ("D1", "H4", "H1"):
        if tf in reads:
            rationale.append(f"{tf} is reading {reads[tf]}")
    rationale.append(f"the market regime is {regime}")
    conflicts = alignment.get("conflicts") or []
    if conflicts:
        names = ", ".join(c["timeframe"] for c in conflicts)
        rationale.append(f"{names} disagrees, so this view is not unanimous")

    upside, downside = _key_levels(analysis, price)
    invalidation = _thesis_invalidation(analysis, direction, price)

    macro_note = ""
    if macro is not None and getattr(macro, "available", False):
        macro_note = macro.headline
        if macro.supports == "conflicted":
            rationale.append("the dollar relationship is not confirming this move")
        elif macro.supports == direction:
            rationale.append("the dollar is moving the way this view needs")

    statement = _statement(direction, conviction, regime, upside, downside, invalidation)

    return Thesis(
        date=_today(moment),
        direction=direction,
        conviction=conviction,
        statement=statement,
        rationale=rationale,
        key_levels={"upside": upside, "downside": downside},
        invalidated_if=invalidation,
        macro_note=macro_note,
        formed_at=moment.isoformat(),
        observations=1,
    )


def _key_levels(analysis: dict, price: float) -> tuple[list[dict], list[dict]]:
    levels = (analysis.get("levels") or {}).get("levels", [])
    above = sorted([l for l in levels if l.get("price", 0) > price],
                   key=lambda l: l["price"])[:3]
    below = sorted([l for l in levels if l.get("price", 0) < price],
                   key=lambda l: -l["price"])[:3]
    trim = lambda items: [{"price": round(float(l["price"]), 2),
                           "source": l.get("source", ""),
                           "strength": l.get("strength")} for l in items]
    return trim(above), trim(below)


def _thesis_invalidation(analysis: dict, direction: str, price: float) -> str:
    """The level that would make the whole day's view wrong, not one trade."""
    h4 = ((analysis.get("timeframes") or {}).get("H4") or {}).get("structure") or {}
    if direction == "bullish" and h4.get("next_bearish_break"):
        return (f"H4 closes below {float(h4['next_bearish_break']):.2f} "
                "(the last higher low)")
    if direction == "bearish" and h4.get("next_bullish_break"):
        return (f"H4 closes above {float(h4['next_bullish_break']):.2f} "
                "(the last lower high)")
    return "H4 structure breaks the other way"


def _statement(direction, conviction, regime, upside, downside, invalidation) -> str:
    if direction == "neutral":
        return ("No directional view today. The timeframes disagree, so the plan "
                "is to wait for one side to take control rather than to pick one.")
    verb = "buying dips" if direction == "bullish" else "selling rallies"
    area = downside if direction == "bullish" else upside
    where = (f" into the {area[0]['price']:.2f} area" if area else "")
    return (f"{conviction.title()} {direction} view: {verb}{where} while the "
            f"market is in a {regime}. Wrong if {invalidation}.")


def review(thesis: Thesis, analysis: dict, decision: dict, profile,
           macro=None, moment: datetime | None = None) -> tuple[Thesis, str | None]:
    """Hold, challenge or revise the standing view. Returns it plus any event."""
    moment = moment or datetime.now(timezone.utc)
    thesis.observations += 1

    alignment = analysis.get("alignment") or {}
    current = alignment.get("direction") or "neutral"

    # Agreement, or a merely neutral read, restores the view. A trader does not
    # abandon a plan because one timeframe went flat for an hour.
    if current == thesis.direction or current == "neutral":
        if thesis.status == CHALLENGED:
            thesis.status = INTACT
            thesis.challenged_since = None
            thesis.challenge_direction = None
            return thesis, "The higher timeframes have come back in line; the view stands."
        thesis.status = INTACT if thesis.status != REVISED else thesis.status
        return thesis, None

    # Disagreement. Start the clock, or check whether it has run out.
    if thesis.challenged_since is None or thesis.challenge_direction != current:
        thesis.status = CHALLENGED
        thesis.challenged_since = moment.isoformat()
        thesis.challenge_direction = current
        return thesis, (f"The higher timeframes have turned {current}. The view is "
                        "under challenge but has not been abandoned.")

    minutes = int(getattr(profile, "thesis_revision_minutes", 30))
    # Measured against the moment under review rather than the wall clock, so a
    # replay or a test sees the same elapsed time the live loop would.
    elapsed = _minutes_between(thesis.challenged_since, moment)
    held_long_enough = elapsed >= minutes
    structure_confirms = _structure_break_since(analysis, current, thesis.formed_at)

    if not (held_long_enough and structure_confirms):
        thesis.status = CHALLENGED
        return thesis, None

    previous = thesis.direction
    fresh = form(analysis, decision, macro, moment)
    fresh.date = thesis.date
    fresh.observations = thesis.observations
    fresh.status = REVISED
    fresh.revisions = list(thesis.revisions) + [{
        "at": moment.isoformat(),
        "from": previous,
        "to": fresh.direction,
        "why": (f"{current} on the higher timeframes for {elapsed} minutes, "
                f"confirmed by {structure_confirms}"),
    }]
    return fresh, (f"View revised from {previous} to {fresh.direction}: "
                   f"{structure_confirms} after {elapsed} minutes of "
                   "disagreement.")


def _structure_break_since(analysis: dict, direction: str, formed_at: str) -> str | None:
    """An H4 or H1 BOS/CHoCH in the new direction since the view was formed."""
    try:
        formed = datetime.fromisoformat(formed_at)
    except (TypeError, ValueError):
        return None
    if formed.tzinfo is None:
        formed = formed.replace(tzinfo=timezone.utc)

    for timeframe in ("H4", "H1"):
        struct = ((analysis.get("timeframes") or {}).get(timeframe) or {}).get("structure") or {}
        for event in reversed(struct.get("events") or []):
            if event.get("direction") != direction:
                continue
            if event.get("kind") not in ("BOS", "CHoCH", "MSS"):
                continue
            when = event.get("time")
            stamp = _event_time(when)
            if stamp is None or stamp >= formed:
                return f"an {timeframe} {event['kind']}"
    return None


def _event_time(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    try:
        stamp = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def load(db, moment: datetime | None = None) -> Thesis | None:
    """Today's saved view, if one has already been written down."""
    stored = db.get_setting(f"thesis:{_today(moment)}")
    if not stored:
        return None
    try:
        return Thesis.from_dict(stored)
    except TypeError:
        return None


def save(db, thesis: Thesis) -> None:
    db.set_setting(f"thesis:{thesis.date}", thesis.to_dict())


def is_stale(thesis: Thesis | None, moment: datetime | None = None) -> bool:
    """Whether the stored view belongs to an earlier trading day."""
    if thesis is None:
        return True
    if thesis.date != _today(moment):
        return True
    # A view formed more than a day ago is not today's view whatever it says.
    return _minutes_since(thesis.formed_at) > 24 * 60
