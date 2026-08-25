"""The tape: what price is doing right now, below the minute.

Every other engine in this package reads closed candles, and the finest of
those is M5. That is the correct resolution for deciding *what* to trade and
leaves a real gap for deciding *when*: between two M5 closes lie five minutes
in which a level was swept, a spread blew out, and momentum turned over, none
of which any closed-candle engine can see until it is over.

This module fills that gap. It aggregates the raw tick stream into sub-minute
candles -- five, fifteen and thirty seconds -- and reduces them to the handful
of readings a trader actually takes off the tape:

**velocity**      how fast price is travelling, in pips per minute
**thrust**        whether the last seconds are pushing or stalling, -1 to +1
**acceleration**  whether that push is building or fading
**pressure**      where inside its own range price is trading, 0-1
**spread state**  whether the cost of entry has widened away from normal
**activity**      tick rate against its own recent average

Three deliberate limits, because a tape read is the easiest thing in a trading
system to over-interpret:

1. **It never produces a signal.** Nothing here returns a setup or a direction
   to trade. It qualifies setups the candle engines have already found, and it
   is offered to the interface as an observation.
2. **It is honest about being blind.** When the broker serves no tick history
   the whole reading is marked unavailable rather than being computed from a
   single quote and presented as if it meant something.
3. **It is scaled in pips, not in points.** Every figure here is comparable
   with every other number the application shows a trader.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

# XAUUSD pip convention, matching signals/opportunity.py.
PIP = 0.10

# The sub-minute ladder. Five seconds is about as fine as Gold's tick stream
# stays meaningful on a retail feed; thirty gives the slowest of the three
# enough candles to have a shape.
MICRO_TIMEFRAMES = ("S5", "S15", "S30")
RESAMPLE = {"S5": "5s", "S15": "15s", "S30": "30s"}

# How many sub-minute candles to keep per rung.
MICRO_BARS = 180

# A spread this many times its own recent median counts as widened. Gold's
# spread routinely doubles around a release, and entering into that is how a
# 50-pip target quietly becomes a 44-pip one.
#
# Measured against the window's own median rather than a fixed number, which
# has one consequence worth naming: a spread that has been wide for the whole
# fifteen-minute window reads as normal, because for that window it is. The
# alternative is hardcoding what Gold's spread "should" be, which differs by
# broker and by account type and would be wrong more often than right.
SPREAD_ALERT = 1.8


def unavailable(reason: str = "no tick history available") -> dict:
    """The reading when there is no tape to read."""
    return {
        "available": False,
        "reason": reason,
        "timeframes": {},
        "velocity_pips_min": 0.0,
        "thrust": 0.0,
        "acceleration": 0.0,
        "pressure": 0.5,
        "state": "unknown",
        "note": reason,
    }


def candles(ticks: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    """Sub-minute OHLC built from the tick stream.

    Volume is the tick count, which is the only volume a retail Gold feed has
    and the right one here anyway: on this timescale the number of quotes *is*
    the measure of activity.
    """
    if ticks is None or ticks.empty or timeframe not in RESAMPLE:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume",
                                     "spread"])
    mid = ticks["mid"]
    rule = RESAMPLE[timeframe]
    frame = pd.DataFrame({
        "open": mid.resample(rule).first(),
        "high": mid.resample(rule).max(),
        "low": mid.resample(rule).min(),
        "close": mid.resample(rule).last(),
        "volume": mid.resample(rule).count().astype(float),
        "spread": ticks["spread"].resample(rule).mean(),
    })
    # Buckets with no ticks are gaps in the tape, not zero-priced candles.
    return frame.dropna(subset=["close"]).tail(MICRO_BARS)


def read(ticks: pd.DataFrame, atr: float = 0.0) -> dict:
    """The full tape reading for one moment.

    ``atr`` is the working ATR from the candle engines, used only to say
    whether the current velocity is fast *for this market today* rather than
    fast against some fixed number.
    """
    if ticks is None or ticks.empty:
        return unavailable()
    if len(ticks) < 8:
        return unavailable("too few ticks to read the tape")

    frames = {tf: candles(ticks, tf) for tf in MICRO_TIMEFRAMES}
    fine = frames["S5"]
    if fine.empty or len(fine) < 4:
        return unavailable("not enough sub-minute candles yet")

    mid = ticks["mid"].astype(float)
    span_seconds = max(
        (ticks.index[-1] - ticks.index[0]).total_seconds(), 1.0)

    # -- velocity ----------------------------------------------------------
    # Measured over the last minute of ticks, or over everything available
    # when less than a minute has been served.
    cutoff = ticks.index[-1] - pd.Timedelta(seconds=60)
    last_minute = mid[mid.index >= cutoff]
    if len(last_minute) >= 2:
        seconds = max((last_minute.index[-1]
                       - last_minute.index[0]).total_seconds(), 1.0)
        travelled = float(last_minute.iloc[-1] - last_minute.iloc[0])
        velocity = travelled / PIP / (seconds / 60.0)
    else:
        velocity = 0.0

    # Distance actually covered versus ground travelled: a market that moved
    # 40 pips in a straight line and one that moved 40 pips back and forth to
    # end where it started are not the same tape.
    path = float(mid.diff().abs().sum()) / PIP
    net = abs(float(mid.iloc[-1] - mid.iloc[0])) / PIP
    efficiency = float(net / path) if path > 0 else 0.0

    # -- thrust and acceleration -------------------------------------------
    closes = fine["close"].astype(float)
    recent = closes.tail(6)
    earlier = closes.tail(12).head(6)
    thrust = _normalise(float(recent.iloc[-1] - recent.iloc[0]), atr)
    prior = (_normalise(float(earlier.iloc[-1] - earlier.iloc[0]), atr)
             if len(earlier) >= 2 else 0.0)
    acceleration = thrust - prior

    # -- position within the recent range ----------------------------------
    window = fine.tail(60)
    high = float(window["high"].max())
    low = float(window["low"].min())
    height = high - low
    pressure = float((float(closes.iloc[-1]) - low) / height) if height > 0 else 0.5

    # -- spread and activity ------------------------------------------------
    spreads = ticks["spread"].astype(float)
    spread_now = float(spreads.tail(20).mean())
    spread_normal = float(spreads.median()) or spread_now
    spread_ratio = spread_now / spread_normal if spread_normal > 0 else 1.0

    tick_rate = len(ticks) / (span_seconds / 60.0)          # ticks per minute
    recent_rate = (len(mid[mid.index >= cutoff])
                   / max((ticks.index[-1] - cutoff).total_seconds(), 1.0) * 60.0)
    activity = float(recent_rate / tick_rate) if tick_rate > 0 else 1.0

    state, note = _describe(velocity, thrust, efficiency, spread_ratio, activity)

    return {
        "available": True,
        "reason": "",
        "ticks": int(len(ticks)),
        "seconds_covered": round(span_seconds, 1),
        "timeframes": {
            tf: {
                "candles": int(len(frame)),
                "last": _last_candle(frame),
                "range_pips": (round(float(frame["high"].max()
                                           - frame["low"].min()) / PIP, 1)
                               if not frame.empty else 0.0),
            }
            for tf, frame in frames.items()
        },
        "velocity_pips_min": round(velocity, 1),
        "speed": _speed_label(abs(velocity), atr),
        "thrust": round(thrust, 3),
        "acceleration": round(acceleration, 3),
        "efficiency": round(efficiency, 3),
        "pressure": round(pressure, 3),
        "range_pips": round(height / PIP, 1),
        "high": round(high, 3),
        "low": round(low, 3),
        "spread_now": round(spread_now, 3),
        "spread_normal": round(spread_normal, 3),
        "spread_ratio": round(spread_ratio, 2),
        "spread_wide": bool(spread_ratio >= SPREAD_ALERT),
        "tick_rate": round(recent_rate, 1),
        "activity": round(activity, 2),
        "state": state,
        "note": note,
    }


def agrees(reading: dict, direction: str) -> float | None:
    """Whether the tape is currently pushing the way a setup wants, 0-1.

    Returns None when there is no tape to read, which callers must treat as
    "no opinion" rather than as disagreement. A setup is not worse because the
    broker did not serve tick history.
    """
    if not reading or not reading.get("available"):
        return None
    thrust = float(reading.get("thrust") or 0.0)
    aligned = thrust if direction == "bullish" else -thrust
    # -1..+1 mapped to 0..1, so a flat tape lands neutral at 0.5 rather than
    # counting against the setup.
    return max(0.0, min(1.0, 0.5 + aligned / 2))


# --- helpers ----------------------------------------------------------------


def _normalise(move: float, atr: float) -> float:
    """A price move as a -1..+1 push, scaled to the day's own volatility."""
    scale = max(atr * 0.25, PIP * 8) if atr else PIP * 8
    return float(max(-1.0, min(1.0, move / scale)))


def _speed_label(pips_per_minute: float, atr: float) -> str:
    # An ATR's worth of movement inside a minute is violent on any timeframe.
    fast = max((atr / PIP) * 0.35, 12.0) if atr else 12.0
    if pips_per_minute >= fast:
        return "fast"
    if pips_per_minute >= fast * 0.4:
        return "moving"
    if pips_per_minute >= fast * 0.12:
        return "steady"
    return "quiet"


def _describe(velocity, thrust, efficiency, spread_ratio, activity) -> tuple[str, str]:
    """A one-line read of the tape, in the words a trader would use."""
    if spread_ratio >= SPREAD_ALERT:
        return ("wide spread",
                f"spread is {spread_ratio:.1f}x its normal width — entry costs "
                "more than the measured levels suggest")
    if abs(thrust) < 0.15 and abs(velocity) < 4:
        return ("quiet", "the tape is quiet; nothing is being pushed right now")
    if efficiency < 0.25:
        return ("churning",
                "price is covering ground without going anywhere — two-way "
                "trade, not a move")
    direction = "up" if thrust > 0 else "down"
    if abs(thrust) >= 0.6:
        return (f"driving {direction}",
                f"the tape is driving {direction} at {abs(velocity):.0f} pips "
                "a minute")
    if activity >= 1.6:
        return (f"picking up {direction}",
                f"activity is rising with price leaning {direction}")
    return (f"drifting {direction}",
            f"price is drifting {direction} at {abs(velocity):.0f} pips a minute")


def _last_candle(frame: pd.DataFrame) -> dict | None:
    if frame is None or frame.empty:
        return None
    row = frame.iloc[-1]
    return {
        "time": int(frame.index[-1].timestamp()),
        "open": round(float(row["open"]), 3),
        "high": round(float(row["high"]), 3),
        "low": round(float(row["low"]), 3),
        "close": round(float(row["close"]), 3),
        "ticks": int(row["volume"]),
        "range_pips": round(float(row["high"] - row["low"]) / PIP, 1),
    }
