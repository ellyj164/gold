"""Market structure engine.

Turns raw candles into the structural vocabulary the rest of the app reasons
with: swing points, HH/HL/LH/LL sequences, Break of Structure, Change of
Character / Market Structure Shift, and the resulting trend state.

The convention used throughout: a *break* requires a candle **close** beyond
the reference swing, not merely a wick through it. A wick through a swing
without a close beyond it is treated as a liquidity event instead (see
:mod:`goldassist.analysis.liquidity`).
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Literal

import numpy as np
import pandas as pd

SwingKind = Literal["high", "low"]
Label = Literal["HH", "HL", "LH", "LL"]
Trend = Literal["bullish", "bearish", "ranging"]


@dataclass
class Swing:
    index: int
    time: pd.Timestamp
    price: float
    kind: SwingKind
    label: str | None = None  # HH / HL / LH / LL

    def to_dict(self) -> dict:
        d = asdict(self)
        d["time"] = int(self.time.timestamp())
        return d


@dataclass
class StructureEvent:
    """A BOS, CHoCH or MSS produced by a candle closing beyond a swing."""

    kind: Literal["BOS", "CHoCH", "MSS"]
    direction: Literal["bullish", "bearish"]
    time: pd.Timestamp
    price: float           # the swing level that was broken
    close: float           # the close that broke it
    broken_swing_time: pd.Timestamp
    displacement: float    # size of the breaking candle in ATR multiples

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "direction": self.direction,
            "time": int(self.time.timestamp()),
            "price": round(float(self.price), 3),
            "close": round(float(self.close), 3),
            "broken_swing_time": int(self.broken_swing_time.timestamp()),
            "displacement": round(float(self.displacement), 2),
        }


def find_swings(df: pd.DataFrame, strength: int = 2) -> list[Swing]:
    """Fractal swing detection.

    A swing high is a candle whose high is the highest within ``strength``
    candles either side; a swing low is the mirror image. Larger ``strength``
    yields fewer, more significant swings.
    """
    highs, lows = df["high"].to_numpy(), df["low"].to_numpy()
    times = df.index
    swings: list[Swing] = []

    for i in range(strength, len(df) - strength):
        window = slice(i - strength, i + strength + 1)
        left = slice(i - strength, i)
        # A tie must not disqualify the swing: equal highs and equal lows are
        # exactly what the liquidity engine looks for. When the extreme repeats
        # inside the window, the first occurrence is taken as the swing.
        if highs[i] == highs[window].max() and not (highs[left] >= highs[i]).any():
            swings.append(Swing(i, times[i], float(highs[i]), "high"))
        elif lows[i] == lows[window].min() and not (lows[left] <= lows[i]).any():
            swings.append(Swing(i, times[i], float(lows[i]), "low"))

    return _alternate(swings)


def _alternate(swings: list[Swing]) -> list[Swing]:
    """Force a strictly alternating high/low sequence.

    Consecutive swings of the same kind are collapsed onto the more extreme of
    the two, which is what a trader reading the chart would treat as the swing.
    """
    cleaned: list[Swing] = []
    for s in swings:
        if cleaned and cleaned[-1].kind == s.kind:
            prev = cleaned[-1]
            better = (s.price > prev.price) if s.kind == "high" else (s.price < prev.price)
            if better:
                cleaned[-1] = s
            continue
        cleaned.append(s)
    return cleaned


def label_swings(swings: list[Swing]) -> list[Swing]:
    """Attach HH/HL/LH/LL labels by comparing each swing to the previous of its kind."""
    last_high: Swing | None = None
    last_low: Swing | None = None
    for s in swings:
        if s.kind == "high":
            if last_high is not None:
                s.label = "HH" if s.price > last_high.price else "LH"
            last_high = s
        else:
            if last_low is not None:
                s.label = "HL" if s.price > last_low.price else "LL"
            last_low = s
    return swings


def detect_events(df: pd.DataFrame, swings: list[Swing], atr_series: pd.Series) -> list[StructureEvent]:
    """Walk the candles and record every structural break.

    Trend state is carried forward as we go, so a break *with* the prevailing
    trend is a BOS while the first break *against* it is a CHoCH. A CHoCH
    backed by an unusually large displacement candle is upgraded to MSS, which
    is the higher-conviction reversal signal.
    """
    if not swings:
        return []

    closes = df["close"].to_numpy()
    times = df.index
    atr_vals = atr_series.reindex(df.index).to_numpy()

    events: list[StructureEvent] = []
    trend: Trend = "ranging"
    # Most recent confirmed swing of each kind that has not yet been broken.
    pending_high: Swing | None = None
    pending_low: Swing | None = None
    swing_pos = 0

    for i in range(len(df)):
        # Activate swings only once the candle that confirms them has passed.
        while swing_pos < len(swings) and swings[swing_pos].index < i:
            s = swings[swing_pos]
            if s.kind == "high":
                pending_high = s
            else:
                pending_low = s
            swing_pos += 1

        atr_i = atr_vals[i] if not np.isnan(atr_vals[i]) else 0.0
        body = abs(closes[i] - df["open"].to_numpy()[i])
        displacement = body / atr_i if atr_i else 0.0

        if pending_high is not None and closes[i] > pending_high.price:
            kind = "BOS" if trend == "bullish" else ("MSS" if displacement >= 1.2 else "CHoCH")
            events.append(
                StructureEvent(kind, "bullish", times[i], pending_high.price, float(closes[i]),
                               pending_high.time, displacement)
            )
            trend = "bullish"
            pending_high = None

        elif pending_low is not None and closes[i] < pending_low.price:
            kind = "BOS" if trend == "bearish" else ("MSS" if displacement >= 1.2 else "CHoCH")
            events.append(
                StructureEvent(kind, "bearish", times[i], pending_low.price, float(closes[i]),
                               pending_low.time, displacement)
            )
            trend = "bearish"
            pending_low = None

    return events


def _trend_from_labels(swings: list[Swing], last_event: StructureEvent | None = None) -> Trend:
    """Read trend from the last labelled swings, with structure as tiebreaker."""
    labels = [s.label for s in swings if s.label][-4:]
    bulls = sum(1 for l in labels if l in ("HH", "HL"))
    bears = sum(1 for l in labels if l in ("LH", "LL"))
    if bulls >= 3:
        return "bullish"
    if bears >= 3:
        return "bearish"
    # A mixed label sequence is common mid-retracement. The last confirmed
    # break still tells us which side is in control, so prefer it over
    # declaring the market rangebound.
    if last_event is not None and last_event.kind in ("BOS", "MSS"):
        return last_event.direction
    if len(labels) >= 2 and labels[-1] in ("HH", "HL") and labels[-2] in ("HH", "HL"):
        return "bullish"
    if len(labels) >= 2 and labels[-1] in ("LH", "LL") and labels[-2] in ("LH", "LL"):
        return "bearish"
    return "ranging"


def analyse(df: pd.DataFrame, atr_series: pd.Series, strength: int = 2) -> dict:
    """Full structural read of one timeframe."""
    swings = label_swings(find_swings(df, strength))
    events = detect_events(df, swings, atr_series)

    last_event = events[-1] if events else None
    trend = _trend_from_labels(swings, last_event)
    recent_events = [e for e in events if e.time >= df.index[-30]] if len(df) > 30 else events

    highs = [s for s in swings if s.kind == "high"]
    lows = [s for s in swings if s.kind == "low"]
    last_high = highs[-1] if highs else None
    last_low = lows[-1] if lows else None

    price = float(df["close"].iloc[-1])
    state = _state(df, price, last_high, last_low, trend, last_event)

    return {
        "trend": trend,
        "state": state,
        "swings": [s.to_dict() for s in swings[-40:]],
        "events": [e.to_dict() for e in events[-20:]],
        "recent_events": [e.to_dict() for e in recent_events],
        "last_event": last_event.to_dict() if last_event else None,
        "last_swing_high": last_high.to_dict() if last_high else None,
        "last_swing_low": last_low.to_dict() if last_low else None,
        # The levels a trader would watch for the next break.
        "next_bullish_break": round(last_high.price, 3) if last_high else None,
        "next_bearish_break": round(last_low.price, 3) if last_low else None,
        "labels": [s.label for s in swings[-6:] if s.label],
    }


def _state(
    df: pd.DataFrame,
    price: float,
    last_high: Swing | None,
    last_low: Swing | None,
    trend: Trend,
    last_event: StructureEvent | None,
) -> str:
    """Describe what price is doing *within* the structure right now."""
    if last_high is None or last_low is None:
        return "insufficient structure"

    span = last_high.price - last_low.price
    if span <= 0:
        return "undefined"
    position = (price - last_low.price) / span  # 0 at swing low, 1 at swing high

    if last_event and last_event.kind in ("CHoCH", "MSS"):
        recent = df.index[-1] - last_event.time <= (df.index[-1] - df.index[-10])
        if recent:
            return f"potential reversal ({last_event.kind.lower()} {last_event.direction})"

    if trend == "bullish":
        if position > 0.9:
            return "expansion into new highs"
        if position < 0.5:
            return "retracement within bullish structure"
        return "bullish continuation"
    if trend == "bearish":
        if position < 0.1:
            return "expansion into new lows"
        if position > 0.5:
            return "retracement within bearish structure"
        return "bearish continuation"
    return "ranging between swing high and swing low"
