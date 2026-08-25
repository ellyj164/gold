"""Signal resolution: deciding what actually happened to a setup.

The failure this module exists to fix
-------------------------------------

A setup used to sit on the dashboard indefinitely. ``COMPLETED`` required
*every* target to be hit, which on a three-target setup essentially never
happened, so a signal that reached its first objective stayed on the board as
``ACTIVE`` for the rest of the session and blocked nothing, resolved nothing
and was recorded as nothing. A stop being hit fared little better: it was
detected from spot price on a five-second poll, so any move that ran through
the level and came back between two polls was simply never seen.

Resolution here is therefore done the only way it can be done honestly:

* **From candles, not from spot.** Every M1 bar since the last check is walked,
  so a wick that touched the stop at 14:32:10 is found even though the app was
  looking at a price from 14:32:05. The live tick is applied on top as a
  partial bar, so an outcome is never a minute late either.
* **Adverse first inside a bar.** When one bar contains both the stop and the
  target there is no way to know which came first, so the stop is assumed. A
  resolver that guesses in its own favour produces a record that flatters the
  strategy and cannot be learned from.
* **Nothing counts before the fill.** A target reached while price never
  traded into the entry zone is not a win -- nobody was in the trade. The same
  setup stopping out before it filled is not a loss; it is an idea that never
  became a trade, recorded as untaken.

The result is a setup that leaves the board the moment it is decided, with a
banked outcome in pips and R, and a slot free for the next opportunity.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from . import opportunity
from .states import (COMPLETED, EXPIRED, INVALIDATED, STOPPED_OUT, TARGET_HIT,
                     TERMINAL)

# Terminal reasons, stored on the suggestion and in the outcome record.
TARGET = "target"
STOP = "stop"
BREAKEVEN = "breakeven"
EXPIRY = "expiry"
CANCELLED = "cancelled"
STRUCTURE = "structure"

# Outcome classes used by the learning layer.
WIN = "win"
LOSS = "loss"
SCRATCH = "scratch"
UNTAKEN = "untaken"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def initialise(suggestion) -> None:
    """Give a freshly built suggestion its working risk levels."""
    stop = (suggestion.invalidation or {}).get("price")
    suggestion.initial_stop = float(stop) if stop is not None else None
    suggestion.stop = suggestion.initial_stop
    suggestion.risk = None
    if suggestion.entry_zone is not None and suggestion.initial_stop is not None:
        suggestion.risk = abs(suggestion.entry_zone.preferred - suggestion.initial_stop) or None


def apply_bars(suggestion, bars, profile) -> list[str]:
    """Walk every candle newer than the last one processed. Returns events.

    ``bars`` is a DataFrame of the finest timeframe available (M1 in the live
    app). Bars are processed oldest first so the sequence of fill, targets and
    stop is the sequence that actually occurred.

    The window is sliced before it is walked rather than filtered inside the
    loop. A caller may hand over a long trailing frame every cycle -- the live
    service does, and so does the backtester -- and re-reading thousands of
    already-resolved candles per setup per tick is the difference between a
    replay that finishes and one that does not.
    """
    if bars is None or len(bars) == 0 or suggestion.status in TERMINAL:
        return []

    window = _unwalked(suggestion, bars)
    if window is None or window.empty:
        return []

    events: list[str] = []
    for row in window.itertuples():
        events += step(suggestion, float(row.high), float(row.low),
                       float(row.close), profile,
                       when=row.Index.isoformat())
        suggestion.resolved_through = int(row.Index.timestamp())
        if suggestion.status in TERMINAL:
            break
    return events


def visited_zone(suggestion, bars) -> bool:
    """Whether price traded into the entry zone in the candles not yet walked.

    Arming used to be judged on spot alone, which is fine at a five-second
    poll and wrong everywhere else: a candle that wicked into the zone and left
    again never armed the setup, so it could not fill, and it eventually
    expired as though price had never come. Reading the unwalked window instead
    means the setup arms when price *reached the entry*, not when it happened
    to still be sitting there at the moment the engine looked.
    """
    zone = suggestion.entry_zone
    if zone is None or bars is None or len(bars) == 0:
        return False
    window = _unwalked(suggestion, bars)
    if window is None or window.empty:
        return False
    return bool(((window["low"] <= zone.high) & (window["high"] >= zone.low)).any())


def apply_price(suggestion, price: float, profile,
                when: str | None = None) -> list[str]:
    """Apply the live quote as a partial bar, so nothing waits for a close."""
    if suggestion.status in TERMINAL:
        return []
    return step(suggestion, price, price, price, profile, when=when or _now())


def step(suggestion, high: float, low: float, close: float, profile,
         when: str | None = None) -> list[str]:
    """Advance one setup through one bar of price action."""
    events: list[str] = []
    when = when or _now()
    bullish = suggestion.direction == "bullish"
    zone = suggestion.entry_zone

    # -- the fill -----------------------------------------------------------
    #
    # Only an armed setup fills. Arming happens in the lifecycle when price is
    # in the zone *and* the trigger is met, which is the method's own rule: an
    # entry zone touched by price that never confirmed is not a trade.
    if not suggestion.entered:
        if suggestion.armed and zone is None:
            # No zone could be derived, so the card offers entry at market and
            # the fill is the price at the moment it confirmed.
            _fill(suggestion, close, when)
            events.append(f"Entry filled at market {close:.2f}")
        elif suggestion.armed and zone is not None and low <= zone.high and high >= zone.low:
            preferred = float(zone.preferred)
            if bullish:
                reached = low <= preferred
                fill = preferred if reached else min(max(low, zone.low), zone.high)
            else:
                reached = high >= preferred
                fill = preferred if reached else max(min(high, zone.high), zone.low)
            _fill(suggestion, fill, when)
            events.append(f"Entry filled at {fill:.2f}")
        elif suggestion.stop is not None:
            # Price ran past the invalidation without ever offering the entry.
            breached = low <= suggestion.stop if bullish else high >= suggestion.stop
            if breached:
                _close(suggestion, float(suggestion.stop), STRUCTURE, when)
                events.append("Invalidated before the entry was ever offered")
                return events
        else:
            return events

    if not suggestion.entered:
        return events

    # -- excursions ---------------------------------------------------------
    favourable = high if bullish else low
    adverse = low if bullish else high
    fill = float(suggestion.fill_price)
    gained = (favourable - fill) if bullish else (fill - favourable)
    lost = (fill - adverse) if bullish else (adverse - fill)
    if gained > 0:
        suggestion.mfe_pips = max(suggestion.mfe_pips, opportunity.to_pips(gained))
    if lost > 0:
        suggestion.mae_pips = max(suggestion.mae_pips, opportunity.to_pips(lost))

    # -- the stop, checked before the targets -------------------------------
    if suggestion.stop is not None:
        hit = adverse <= suggestion.stop if bullish else adverse >= suggestion.stop
        if hit:
            reason = BREAKEVEN if _at_breakeven(suggestion) else STOP
            _close(suggestion, float(suggestion.stop), reason, when)
            events.append("Stop hit at %.2f" % suggestion.stop if reason == STOP
                          else "Closed at breakeven %.2f" % suggestion.stop)
            return events

    # -- the targets --------------------------------------------------------
    close_index = max(1, int(getattr(profile, "close_at_target", 1)))
    for position, target in enumerate(suggestion.targets, start=1):
        if target.hit:
            continue
        reached = favourable >= target.price if bullish else favourable <= target.price
        if not reached:
            break            # targets are ordered; the rest are further away
        target.hit = True
        suggestion.targets_hit = position
        events.append(f"{target.level} reached at {target.price:.2f} "
                      f"({target.pips:.0f} pips)")

        if position >= close_index or position >= len(suggestion.targets):
            _close(suggestion, float(target.price), TARGET, when)
            return events

        # Runner: the risk comes off the trade the moment it has paid.
        if getattr(profile, "breakeven_after_target", True) and suggestion.fill_price:
            if suggestion.stop != suggestion.fill_price:
                suggestion.stop = float(suggestion.fill_price)
                events.append(f"Stop moved to breakeven at {suggestion.fill_price:.2f}")

    return events


def check_expiry(suggestion, profile, now: datetime | None = None) -> str | None:
    """Retire a setup that has run out of time. Returns an event, or None."""
    if suggestion.status in TERMINAL:
        return None
    now = now or datetime.now(timezone.utc)

    if not suggestion.entered:
        limit = float(getattr(profile, "setup_expiry_minutes", 240) or 0)
        if limit <= 0:
            return None
        age = _minutes_since(suggestion.created_at, now)
        if age is not None and age >= limit:
            _close(suggestion, None, EXPIRY, now.isoformat())
            return (f"Setup expired unfilled after {age:.0f} minutes — "
                    "price never came to the entry zone")
        return None

    limit = float(getattr(profile, "trade_expiry_minutes", 720) or 0)
    if limit <= 0:
        return None
    age = _minutes_since(suggestion.filled_at or suggestion.created_at, now)
    if age is not None and age >= limit:
        _close(suggestion, float(suggestion.price), EXPIRY, now.isoformat())
        return (f"Closed on time after {age:.0f} minutes — the trade stopped "
                "going anywhere")
    return None


def cancel(suggestion, reason: str) -> str:
    """Retire an unfilled setup whose premise has gone."""
    _close(suggestion, None, CANCELLED, _now())
    suggestion.exit_note = reason
    return f"Setup cancelled — {reason}"


# --- internals --------------------------------------------------------------


def _fill(suggestion, price: float, when: str) -> None:
    suggestion.entered = True
    suggestion.fill_price = round(float(price), 3)
    suggestion.filled_at = when
    if suggestion.stop is not None:
        suggestion.risk = abs(suggestion.fill_price - float(suggestion.stop)) or None


def _close(suggestion, exit_price: float | None, reason: str, when: str) -> None:
    """Record the terminal state, the outcome and what it was worth."""
    suggestion.exit_reason = reason
    suggestion.exit_at = when

    if not suggestion.entered:
        suggestion.outcome = UNTAKEN
        suggestion.result_pips = 0.0
        suggestion.r_multiple = 0.0
        suggestion.exit_price = round(float(exit_price), 3) if exit_price else None
        suggestion.status = EXPIRED if reason in (EXPIRY, CANCELLED) else INVALIDATED
        return

    price = float(exit_price if exit_price is not None else suggestion.price)
    suggestion.exit_price = round(price, 3)
    fill = float(suggestion.fill_price)
    signed = (price - fill) if suggestion.direction == "bullish" else (fill - price)
    suggestion.result_pips = round(opportunity.to_pips(signed) * (1 if signed >= 0 else -1), 1)

    risk = suggestion.risk or abs(fill - float(suggestion.initial_stop or fill)) or None
    suggestion.r_multiple = round(signed / risk, 2) if risk else 0.0

    if reason == TARGET:
        suggestion.outcome = WIN
        suggestion.status = (COMPLETED if all(t.hit for t in suggestion.targets)
                             else TARGET_HIT)
        return
    if reason in (STOP, BREAKEVEN):
        suggestion.status = STOPPED_OUT
        suggestion.outcome = (SCRATCH if reason == BREAKEVEN or abs(suggestion.r_multiple) < 0.2
                              else LOSS)
        return

    # Time exit: whatever it was worth at the moment it was closed.
    suggestion.status = STOPPED_OUT if suggestion.r_multiple < 0 else TARGET_HIT
    suggestion.outcome = (WIN if suggestion.r_multiple >= 0.2
                          else LOSS if suggestion.r_multiple <= -0.2 else SCRATCH)


def _at_breakeven(suggestion) -> bool:
    if suggestion.stop is None or suggestion.fill_price is None:
        return False
    return abs(float(suggestion.stop) - float(suggestion.fill_price)) < 1e-6


def _unwalked(suggestion, bars):
    """The slice of ``bars`` this setup has not seen yet.

    Sliced by timestamp rather than filtered inside a loop: callers hand over a
    long trailing frame every cycle -- the live service does, and so does the
    backtester -- and re-reading thousands of already-resolved candles per
    setup per tick is the difference between a replay that finishes and one
    that does not.
    """
    if bars is None or len(bars) == 0:
        return None
    since = suggestion.resolved_through or _created_epoch(suggestion)
    if not since:
        return bars
    cutoff = pd.Timestamp(since, unit="s", tz="UTC")
    if bars.index.tz is None:
        cutoff = cutoff.tz_localize(None)
    start = int(bars.index.searchsorted(cutoff, side="right"))
    return bars.iloc[start:] if start < len(bars) else None


def _created_epoch(suggestion) -> int:
    stamp = _parse(suggestion.created_at)
    return int(stamp.timestamp()) if stamp else 0


def _minutes_since(value: str | None, now: datetime) -> float | None:
    stamp = _parse(value)
    if stamp is None:
        return None
    return (now - stamp).total_seconds() / 60.0


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        stamp = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)
