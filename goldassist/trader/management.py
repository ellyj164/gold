"""Trade management: what to do after the entry.

Until now this application went quiet at the most important moment. It would
say "enter between 4383.51 and 4385.65", and then say nothing at all about the
hours that follow -- which is where most of a discretionary trader's actual
result is decided.

A human running the same trade does four things the app did not:

* moves the stop to breakeven once the trade has paid for itself,
* takes part of the position off at the first objective,
* trails the remainder behind structure rather than behind a fixed distance,
* and notices when a trade has stopped working *before* the stop is hit.

That last one matters most. A trade that has sat still for two hours, or given
back most of what it offered, is telling the trader something. A stop-loss
tells them the same thing several hundred pips later.

Nothing here places or modifies an order. Every output is a suggested action
for the trader to take in their own terminal, expressed in prices they can
read off the chart -- the application still never trades.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

# Action kinds.
MOVE_TO_BREAKEVEN = "MOVE_TO_BREAKEVEN"
TAKE_PARTIAL = "TAKE_PARTIAL"
TRAIL_STOP = "TRAIL_STOP"
TARGET_NEAR = "TARGET_NEAR"
STALLED = "STALLED"
MOMENTUM_AGAINST = "MOMENTUM_AGAINST"
GIVING_BACK = "GIVING_BACK"

# Urgency, which drives how loudly the interface says it.
INFO = "info"
CAUTION = "caution"
ACT = "act"


@dataclass
class ManagementAction:
    kind: str
    urgency: str
    headline: str
    detail: str
    level: float | None = None
    at: str = ""

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "urgency": self.urgency,
            "headline": self.headline,
            "detail": self.detail,
            "level": round(self.level, 3) if self.level is not None else None,
            "at": self.at,
        }


@dataclass
class ManagedTrade:
    """The live state of one entered setup."""

    suggestion_id: str
    direction: str
    entry: float
    initial_stop: float
    current_stop: float
    risk: float
    opened_at: str
    position_open: float = 1.0        # fraction of the original size still on
    r_now: float = 0.0
    mfe_r: float = 0.0                # best the trade ever offered
    mae_r: float = 0.0                # worst it ever went against
    breakeven_set: bool = False
    partial_taken: bool = False
    actions: list[ManagementAction] = field(default_factory=list)
    _fired: set = field(default_factory=set)

    @property
    def bullish(self) -> bool:
        return self.direction == "bullish"

    def to_dict(self) -> dict:
        return {
            "suggestion_id": self.suggestion_id,
            "direction": self.direction,
            "entry": round(self.entry, 3),
            "initial_stop": round(self.initial_stop, 3),
            "current_stop": round(self.current_stop, 3),
            "risk": round(self.risk, 3),
            "opened_at": self.opened_at,
            "position_open": round(self.position_open, 2),
            "r_now": round(self.r_now, 2),
            "mfe_r": round(self.mfe_r, 2),
            "mae_r": round(self.mae_r, 2),
            "breakeven_set": self.breakeven_set,
            "partial_taken": self.partial_taken,
            "stop_moved": abs(self.current_stop - self.initial_stop) > 1e-9,
            "actions": [a.to_dict() for a in self.actions[-8:]],
        }


def open_trade(suggestion, fill_price: float | None = None) -> ManagedTrade | None:
    """Begin managing a setup that price has traded into."""
    stop = (suggestion.invalidation or {}).get("price")
    if stop is None:
        return None
    zone = suggestion.entry_zone
    entry = float(fill_price if fill_price is not None
                  else (zone.preferred if zone else suggestion.price))
    risk = abs(entry - float(stop))
    if risk <= 0:
        return None
    now = datetime.now(timezone.utc).isoformat()
    return ManagedTrade(
        suggestion_id=suggestion.id,
        direction=suggestion.direction,
        entry=entry,
        initial_stop=float(stop),
        current_stop=float(stop),
        risk=risk,
        opened_at=now,
    )


def update(trade: ManagedTrade, suggestion, analysis: dict,
           profile) -> list[ManagementAction]:
    """Re-measure the open trade and return any newly suggested actions."""
    price = float(analysis.get("price") or suggestion.price)
    atr = float(analysis.get("atr") or 0.0) or trade.risk
    now = datetime.now(timezone.utc)

    signed = (price - trade.entry) if trade.bullish else (trade.entry - price)
    trade.r_now = signed / trade.risk
    trade.mfe_r = max(trade.mfe_r, trade.r_now)
    trade.mae_r = min(trade.mae_r, trade.r_now)

    new: list[ManagementAction] = []

    def emit(kind, urgency, headline, detail, level=None, key=None):
        marker = key or kind
        if marker in trade._fired:
            return
        trade._fired.add(marker)
        action = ManagementAction(kind, urgency, headline, detail, level,
                                  now.isoformat())
        trade.actions.append(action)
        new.append(action)

    be_at_r = float(getattr(profile, "breakeven_at_r", 1.0))
    partial_fraction = float(getattr(profile, "partial_at_tp1", 0.5))

    # --- first objective: take something off, and stop risking money -------
    tp1 = suggestion.targets[0] if suggestion.targets else None
    if tp1 is not None and tp1.hit and not trade.partial_taken:
        trade.partial_taken = True
        trade.position_open = max(0.0, 1.0 - partial_fraction)
        emit(TAKE_PARTIAL, ACT,
             f"TP1 reached — take {partial_fraction:.0%} off at {tp1.price:.2f}",
             "Banking part of the position at the first objective turns a good "
             "read into a realised result, and makes the rest of the trade free "
             "to run without you needing it to.",
             level=tp1.price)

    # --- breakeven ---------------------------------------------------------
    if not trade.breakeven_set and (trade.partial_taken or trade.r_now >= be_at_r):
        trade.breakeven_set = True
        trade.current_stop = trade.entry
        why = ("TP1 is banked" if trade.partial_taken
               else f"the trade is {trade.r_now:.1f}R in your favour")
        emit(MOVE_TO_BREAKEVEN, ACT,
             f"Move the stop to breakeven at {trade.entry:.2f}",
             f"Now that {why}, there is no reason left to risk the original "
             "amount on this idea. From here the worst case is a scratch.",
             level=trade.entry)

    # --- structure trail ---------------------------------------------------
    if trade.breakeven_set:
        trail = _structural_stop(analysis, trade, profile, atr)
        if trail is not None and _improves(trade, trail, atr * 0.25):
            trade.current_stop = trail
            emit(TRAIL_STOP, INFO,
                 f"Trail the stop to {trail:.2f}",
                 "Structure has advanced, so the level that would prove this "
                 "trade wrong has moved with it. Trailing behind structure "
                 "keeps you in the move; trailing by a fixed distance does not.",
                 level=trail, key=f"{TRAIL_STOP}:{round(trail, 1)}")

    # --- approaching the next objective ------------------------------------
    nxt = next((t for t in suggestion.targets if not t.hit), None)
    if nxt is not None:
        distance = abs(nxt.price - price)
        if distance <= atr * 0.25:
            emit(TARGET_NEAR, INFO,
                 f"{nxt.level} is close ({nxt.price:.2f})",
                 f"Price is within a quarter of an ATR of {nxt.level}. "
                 f"{nxt.basis}.",
                 level=nxt.price, key=f"{TARGET_NEAR}:{nxt.level}")

    # --- the trade has stopped working -------------------------------------
    stall_minutes = int(getattr(profile, "stall_minutes", 90))
    minutes_open = _minutes_since(trade.opened_at, now)
    if (minutes_open >= stall_minutes and trade.mfe_r < 0.5
            and trade.r_now < 0.25 and not trade.partial_taken):
        emit(STALLED, CAUTION,
             f"No progress in {minutes_open} minutes",
             f"The best this trade has offered is {trade.mfe_r:.2f}R and it is "
             f"currently {trade.r_now:+.2f}R. A setup that was going to work "
             "usually shows something by now. Consider closing it at whatever "
             "it is worth rather than waiting for the stop to decide.")

    if trade.mfe_r >= 1.0 and trade.r_now <= trade.mfe_r * 0.4 and not trade.partial_taken:
        emit(GIVING_BACK, CAUTION,
             f"Given back most of a {trade.mfe_r:.1f}R move",
             f"This trade was {trade.mfe_r:.1f}R in your favour and is now "
             f"{trade.r_now:+.2f}R. Letting a winner become a loser is the most "
             "avoidable way to lose money on a correct read.")

    # --- structure turning against the position ----------------------------
    against = _structure_against(analysis, trade, profile)
    if against and trade.r_now < 1.0:
        emit(MOMENTUM_AGAINST, CAUTION,
             f"{against} against the position",
             "The confirmation timeframe has broken structure the other way "
             "while the trade is not yet paying. The reason you entered is no "
             "longer on the chart.",
             key=f"{MOMENTUM_AGAINST}:{against}")

    return new


def _minutes_since(stamp: str, now: datetime) -> int:
    try:
        opened = datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return 0
    if opened.tzinfo is None:
        opened = opened.replace(tzinfo=timezone.utc)
    return int((now - opened).total_seconds() // 60)


def _improves(trade: ManagedTrade, level: float, minimum_step: float) -> bool:
    """A trail may only ever reduce risk, and only by a meaningful amount."""
    if trade.bullish:
        return level > trade.current_stop + minimum_step
    return level < trade.current_stop - minimum_step


def _structural_stop(analysis: dict, trade: ManagedTrade, profile,
                     atr: float) -> float | None:
    """The last swing that would have to break for the trade to be wrong."""
    timeframe = getattr(profile, "trail_timeframe", "M15")
    struct = ((analysis.get("timeframes") or {}).get(timeframe) or {}).get("structure") or {}
    swing = struct.get("last_swing_low" if trade.bullish else "last_swing_high")
    if not swing or swing.get("price") is None:
        return None
    # Sit just beyond the swing rather than exactly on it: the wick that takes
    # out the level to the tick is a routine event.
    buffer = atr * 0.15
    return float(swing["price"]) - buffer if trade.bullish else float(swing["price"]) + buffer


def _structure_against(analysis: dict, trade: ManagedTrade, profile) -> str | None:
    """A recent CHoCH or MSS on the confirmation timeframe, the other way."""
    timeframe = getattr(profile, "confirmation_timeframe", "M5")
    struct = ((analysis.get("timeframes") or {}).get(timeframe) or {}).get("structure") or {}
    event = struct.get("last_event") or {}
    if event.get("kind") not in ("CHoCH", "MSS", "BOS"):
        return None
    direction = event.get("direction")
    if not direction or direction == trade.direction:
        return None
    return f"{timeframe} {event['kind']}"


def close_trade(trade: ManagedTrade, suggestion, reason: str) -> dict:
    """Final record of how the managed trade actually went."""
    from ..signals.states import WINNING

    if suggestion.status in WINNING:
        outcome = "win"
    elif trade.r_now <= -0.9:
        outcome = "loss"
    elif trade.r_now >= 0.2 or trade.partial_taken:
        outcome = "win"
    elif trade.r_now <= -0.2:
        outcome = "loss"
    else:
        outcome = "scratch"

    # Once the stop has been moved to breakeven, the realised result cannot be
    # worse than a scratch -- the trade would have closed there.
    realised = trade.r_now
    if trade.breakeven_set and realised < 0:
        realised = 0.0
    if trade.partial_taken:
        # Half the position banked roughly 1R at TP1; the rest ran to whatever
        # the trade finished at. Approximate, and labelled as such.
        realised = 0.5 * max(trade.mfe_r, 1.0) + 0.5 * realised

    return {
        "outcome": outcome,
        "r_multiple": round(realised, 2),
        "max_favourable_r": round(trade.mfe_r, 2),
        "max_adverse_r": round(trade.mae_r, 2),
        "reason": reason,
        "partial_taken": trade.partial_taken,
        "breakeven_set": trade.breakeven_set,
    }
