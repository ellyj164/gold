"""Session structure: the reference levels an intraday day is actually built on.

Everything else in ``analysis`` finds levels by looking at price shape --
swings, zones, imbalances, blocks. This module finds them by looking at the
*clock*, and for an intraday trader that is at least as productive, because a
handful of prices are known in advance every single day and are traded by
everyone:

**Prior day high, low and close.** The three most-watched prices in any
instrument. They are where yesterday's argument was settled, and price returns
to test them constantly. An intraday trader who does not have these marked is
trading blind through the most reliable reaction points on the chart.

**The overnight (Asia) range.** Gold's Asian session is usually quiet and
range-bound, and the high and low it leaves behind become the liquidity that
London runs. "Sweep the Asian high, reverse into London" is not a theory, it is
a description of a large share of Gold's mornings.

**The initial balance.** The first hour of a session. Its high and low bound
the auction's opening agreement, and whether price *extends* beyond them --
range extension -- is the single most useful early read on whether today is a
trend day or a rotation. On a trend day the IB breaks early and does not come
back; on a normal day price rotates inside it and both edges get faded.

**How much range is left.** Gold has a characteristic daily range. Once
seventy per cent of it has been spent by mid-session, the odds of a further
clean directional leg fall sharply and the odds of rotation rise. This is the
reading that stops an intraday trader taking a breakout at the exact moment the
day has run out of fuel -- and, in the other direction, tells them there is
room when a move looks late but is not.

Why this matters for a trader who takes many trades a day
---------------------------------------------------------

Each of these is a reference that gets tested repeatedly within one session,
and each test is a decision point in both directions. A single day typically
offers the Asian high, the Asian low, the prior close, both IB edges and the
day's VWAP -- six known prices before any pattern has been detected at all. The
point of this module is that those prices are available at the open, not
discovered afterwards.

Nothing here produces a signal. It produces the levels and the day's shape, and
the strategy layer decides what to do at them.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

# Session bounds in UTC, matching ``analysis/vwap`` and the backtest's own map.
ASIA = (0, 7)
LONDON = (7, 12)
NEWYORK = (12, 21)

# The initial balance is conventionally the first hour of the session.
INITIAL_BALANCE_MINUTES = 60

# The opening range: the first fifteen minutes, which is where the day's first
# tradeable break tends to come from.
OPENING_RANGE_MINUTES = 15

# Days of history used to characterise a normal daily range. Twenty sessions is
# a month and tracks a regime change without being dominated by one wild day.
RANGE_LOOKBACK = 20

# Share of the typical daily range spent, above which the day is called
# extended and a further clean leg is less likely.
RANGE_SPENT_HIGH = 0.70
RANGE_SPENT_LOW = 0.35

# How close price must come to a reference to count as testing it, as a share
# of the day's typical range.
TEST_TOLERANCE = 0.04


def _session_slice(frame: pd.DataFrame, day: pd.Timestamp,
                   bounds: tuple[int, int]) -> pd.DataFrame:
    start = day + pd.Timedelta(hours=bounds[0])
    end = day + pd.Timedelta(hours=bounds[1])
    return frame[(frame.index >= start) & (frame.index < end)]


def _range_of(window: pd.DataFrame) -> dict | None:
    if window is None or len(window) == 0:
        return None
    high = float(window["high"].max())
    low = float(window["low"].min())
    return {
        "high": round(high, 3),
        "low": round(low, 3),
        "mid": round((high + low) / 2, 3),
        "width": round(high - low, 3),
        "bars": int(len(window)),
    }


def _typical_daily_range(daily: pd.DataFrame) -> float | None:
    """The range a normal Gold day covers, as a median rather than a mean.

    Median because a single release day can double a mean and would then make
    every ordinary day look like it had plenty of room left.
    """
    if daily is None or len(daily) < 5:
        return None
    spans = (daily["high"] - daily["low"]).tail(RANGE_LOOKBACK).dropna()
    if spans.empty:
        return None
    return float(spans.median())


def analyse(frames: dict[str, pd.DataFrame],
            now: pd.Timestamp | None = None) -> dict:
    """The day's structural references and what shape the session is taking."""
    intraday = None
    for timeframe in ("M5", "M15", "M1", "M30"):
        candidate = frames.get(timeframe)
        if candidate is not None and len(candidate) > 50:
            intraday, source = candidate, timeframe
            break
    if intraday is None:
        return {"available": False,
                "note": "No intraday frame available for session structure."}

    now = pd.Timestamp(now) if now is not None else intraday.index[-1]
    today = now.normalize()
    price = float(intraday["close"].iloc[-1])

    daily = frames.get("D1")
    typical = _typical_daily_range(daily)

    out: dict = {
        "available": True,
        "timeframe": source,
        "price": round(price, 3),
        "typical_daily_range": round(typical, 3) if typical else None,
    }

    out["prior_day"] = _prior_day(daily, today)
    out["asia"] = _range_of(_session_slice(intraday, today, ASIA))
    out["today"] = _range_of(intraday[intraday.index >= today])
    out["initial_balance"] = _initial_balance(intraday, today, now)
    out["opening_range"] = _opening_range(intraday, today, now)
    out["range_used"] = _range_used(out["today"], typical)
    out["extension"] = _extension(out["initial_balance"], out["today"], price)
    out["day_type"] = _day_type(out, price)
    out["levels"] = _levels(out)
    out["tests"] = _tests(out, price, typical)
    out["observations"] = _observations(out, price)
    return out


def _prior_day(daily: pd.DataFrame | None, today: pd.Timestamp) -> dict | None:
    """Yesterday's high, low and close -- the three most-watched prices."""
    if daily is None or len(daily) < 2:
        return None
    previous = daily[daily.index < today]
    if len(previous) == 0:
        return None
    row = previous.iloc[-1]
    return {
        "date": previous.index[-1].date().isoformat(),
        "high": round(float(row["high"]), 3),
        "low": round(float(row["low"]), 3),
        "close": round(float(row["close"]), 3),
        "mid": round((float(row["high"]) + float(row["low"])) / 2, 3),
        "width": round(float(row["high"]) - float(row["low"]), 3),
    }


def _active_session(now: pd.Timestamp) -> tuple[str, int]:
    hour = now.hour
    if NEWYORK[0] <= hour < NEWYORK[1]:
        return "newyork", NEWYORK[0]
    if LONDON[0] <= hour < LONDON[1]:
        return "london", LONDON[0]
    if ASIA[0] <= hour < ASIA[1]:
        return "asia", ASIA[0]
    return "after hours", NEWYORK[0]


def _initial_balance(frame: pd.DataFrame, today: pd.Timestamp,
                     now: pd.Timestamp) -> dict | None:
    """The first hour of the session currently in progress.

    Anchored to the live session rather than to the calendar day, because the
    IB that matters to a trader at 13:00 UTC is New York's, not the one Asia
    left behind thirteen hours ago.
    """
    name, open_hour = _active_session(now)
    start = today + pd.Timedelta(hours=open_hour)
    end = start + pd.Timedelta(minutes=INITIAL_BALANCE_MINUTES)
    window = frame[(frame.index >= start) & (frame.index < end)]
    result = _range_of(window)
    if result is None:
        return None
    result["session"] = name
    result["complete"] = bool(now >= end)
    return result


def _opening_range(frame: pd.DataFrame, today: pd.Timestamp,
                   now: pd.Timestamp) -> dict | None:
    name, open_hour = _active_session(now)
    start = today + pd.Timedelta(hours=open_hour)
    end = start + pd.Timedelta(minutes=OPENING_RANGE_MINUTES)
    result = _range_of(frame[(frame.index >= start) & (frame.index < end)])
    if result is None:
        return None
    result["session"] = name
    result["complete"] = bool(now >= end)
    return result


def _range_used(today: dict | None, typical: float | None) -> dict | None:
    """How much of a normal day's range has already been spent.

    The reading that decides whether a late breakout has room to travel. A day
    that has already covered its usual range does not usually manage another
    clean leg, and a day that has covered a third of it by the New York open
    has plenty left.
    """
    if today is None or not typical:
        return None
    fraction = today["width"] / typical if typical else 0.0
    if fraction >= RANGE_SPENT_HIGH:
        verdict = "extended"
        note = (f"Today has already covered {fraction * 100:.0f}% of a typical "
                "daily range. A further clean directional leg is less likely "
                "than rotation, so favour fading the edges over chasing a break.")
    elif fraction <= RANGE_SPENT_LOW:
        verdict = "room"
        note = (f"Today has covered only {fraction * 100:.0f}% of a typical daily "
                "range. There is room for a directional move, which favours "
                "breakouts and continuation over fading.")
    else:
        verdict = "normal"
        note = (f"Today has covered {fraction * 100:.0f}% of a typical daily "
                "range — an ordinary session with room for one more leg.")
    return {"fraction": round(fraction, 2), "verdict": verdict,
            "spent": today["width"], "typical": round(typical, 3), "note": note}


def _extension(ib: dict | None, today: dict | None, price: float) -> dict | None:
    """Whether price has broken out of the initial balance, and which way.

    Range extension is the earliest reliable signal of a trend day. Both-sided
    extension is the opposite -- a day that broke both ways is a day that
    trusted neither, and it is the classic rotation profile.
    """
    if ib is None or today is None:
        return None
    above = today["high"] > ib["high"]
    below = today["low"] < ib["low"]

    if above and below:
        direction, note = "both", (
            "Price has extended beyond both initial-balance edges. A day that "
            "broke both ways is a rotation, not a trend — both edges are fade "
            "candidates rather than breakout levels.")
    elif above:
        direction, note = "upward", (
            f"Price has extended above the initial balance ({ib['high']:.2f}). "
            "That is the earliest signal of a trend day, and the IB high now "
            "acts as support on a pullback.")
    elif below:
        direction, note = "downward", (
            f"Price has extended below the initial balance ({ib['low']:.2f}). "
            "That is the earliest signal of a trend day, and the IB low now "
            "acts as resistance on a bounce.")
    else:
        direction, note = "none", (
            f"Price is still inside the initial balance ({ib['low']:.2f} to "
            f"{ib['high']:.2f}). Until one edge gives way, both are fade "
            "candidates and a break of either is the day's first real trade.")

    return {"direction": direction, "inside": not (above or below),
            "ib_high": ib["high"], "ib_low": ib["low"], "note": note}


def _day_type(out: dict, price: float) -> dict:
    """The auction's shape, in the vocabulary a profile trader uses."""
    extension = out.get("extension") or {}
    used = out.get("range_used") or {}
    direction = extension.get("direction", "none")
    verdict = used.get("verdict", "normal")

    if direction in ("upward", "downward") and verdict in ("room", "normal"):
        kind = f"trend day ({'up' if direction == 'upward' else 'down'})"
        guidance = ("Trade with the extension. Pullbacks into the initial "
                    "balance edge and VWAP are entries; fading the highs on a "
                    "trend day is the most expensive trade available.")
    elif direction == "both":
        kind = "rotation"
        guidance = ("Both edges have been rejected. Trade the range: fade the "
                    "extremes back toward the middle and treat breakouts with "
                    "suspicion until one holds.")
    elif direction == "none":
        kind = "balanced — undecided"
        guidance = ("The auction has not chosen. The first sustained break of "
                    "the initial balance is the day's first genuine trade, in "
                    "whichever direction it comes.")
    else:
        kind = "extended trend"
        guidance = ("Extended and directional. The move is real but late — bank "
                    "the first objective rather than holding for a runner.")

    return {"kind": kind, "guidance": guidance}


def _levels(out: dict) -> list[dict]:
    """Every clock-derived reference, as a flat list the UI can plot."""
    levels: list[dict] = []

    prior = out.get("prior_day")
    if prior:
        levels += [
            {"name": "Prior day high", "price": prior["high"], "kind": "resistance",
             "source": "prior session"},
            {"name": "Prior day low", "price": prior["low"], "kind": "support",
             "source": "prior session"},
            {"name": "Prior day close", "price": prior["close"], "kind": "pivot",
             "source": "prior session"},
        ]

    asia = out.get("asia")
    if asia:
        levels += [
            {"name": "Asia high", "price": asia["high"], "kind": "liquidity",
             "source": "overnight range"},
            {"name": "Asia low", "price": asia["low"], "kind": "liquidity",
             "source": "overnight range"},
        ]

    ib = out.get("initial_balance")
    if ib:
        label = ib.get("session", "session").title()
        levels += [
            {"name": f"{label} IB high", "price": ib["high"], "kind": "breakout",
             "source": "initial balance"},
            {"name": f"{label} IB low", "price": ib["low"], "kind": "breakout",
             "source": "initial balance"},
        ]

    opening = out.get("opening_range")
    if opening:
        levels += [
            {"name": "Opening range high", "price": opening["high"],
             "kind": "breakout", "source": "opening range"},
            {"name": "Opening range low", "price": opening["low"],
             "kind": "breakout", "source": "opening range"},
        ]

    return levels


def _tests(out: dict, price: float, typical: float | None) -> list[dict]:
    """Which references price is sitting on right now.

    This is the reading that turns a list of levels into a reason to look at
    the chart: an intraday trader wants to know not that the Asian high exists
    but that price is at it now.
    """
    if not typical:
        return []
    tolerance = typical * TEST_TOLERANCE
    out_tests = []
    for level in out.get("levels", []):
        distance = price - level["price"]
        if abs(distance) <= tolerance:
            out_tests.append({
                "name": level["name"],
                "price": level["price"],
                "distance": round(distance, 3),
                "side": "above" if distance > 0 else "below",
                "kind": level["kind"],
            })
    return out_tests


def _observations(out: dict, price: float) -> list[str]:
    """The session read, in the sentences a trader would say out loud."""
    notes: list[str] = []

    day_type = out.get("day_type") or {}
    if day_type:
        notes.append(f"Today reads as a {day_type['kind']}. {day_type['guidance']}")

    extension = out.get("extension")
    if extension:
        notes.append(extension["note"])

    used = out.get("range_used")
    if used:
        notes.append(used["note"])

    asia = out.get("asia")
    today = out.get("today")
    if asia and today:
        if today["high"] > asia["high"] and today["low"] < asia["low"]:
            notes.append(
                f"Both sides of the Asian range ({asia['low']:.2f}–{asia['high']:.2f}) "
                "have been taken. The overnight liquidity is gone, so the next "
                "move has to be built on something else.")
        elif today["high"] > asia["high"]:
            notes.append(
                f"The Asian high ({asia['high']:.2f}) has been swept and the low "
                f"({asia['low']:.2f}) has not. If price fails back inside, that "
                "sweep is the classic London reversal; if it holds above, it is "
                "the breakout.")
        elif today["low"] < asia["low"]:
            notes.append(
                f"The Asian low ({asia['low']:.2f}) has been swept and the high "
                f"({asia['high']:.2f}) has not — the mirror of the London "
                "reversal setup, and the untouched high above is the target.")

    tests = out.get("tests") or []
    for test in tests:
        notes.append(
            f"Price is testing the {test['name'].lower()} at {test['price']:.2f} "
            f"right now ({test['distance']:+.2f} away).")

    prior = out.get("prior_day")
    if prior and not tests:
        nearest = min(
            (("prior day high", prior["high"]), ("prior day low", prior["low"]),
             ("prior day close", prior["close"])),
            key=lambda item: abs(price - item[1]))
        notes.append(
            f"The nearest prior-session reference is the {nearest[0]} at "
            f"{nearest[1]:.2f}, {price - nearest[1]:+.2f} away.")

    return notes
