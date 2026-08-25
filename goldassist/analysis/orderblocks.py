"""Order Block detection.

An Order Block is the last opposing candle before a displacement move that
breaks structure. Bullish OB = the last down-candle before an impulsive rally;
bearish OB = the last up-candle before an impulsive sell-off.

Each block is ranked on the factors the specification lists: timeframe, the
strength of the departure, whether it caused a structural break, freshness,
mitigation, and its relationship with any FVG left behind.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict

import numpy as np
import pandas as pd

from .indicators import atr


@dataclass
class OrderBlock:
    direction: str        # bullish | bearish
    timeframe: str
    top: float
    bottom: float
    created: pd.Timestamp
    created_index: int
    displacement: float   # size of the impulse leg in ATR multiples
    caused_bos: bool
    has_fvg: bool
    mitigated: bool = False
    mitigation_ratio: float = 0.0
    tests: int = 0
    quality: float = 0.0

    @property
    def midpoint(self) -> float:
        return (self.top + self.bottom) / 2

    def to_dict(self) -> dict:
        d = asdict(self)
        d["created"] = int(self.created.timestamp())
        d["top"] = round(self.top, 3)
        d["bottom"] = round(self.bottom, 3)
        d["midpoint"] = round(self.midpoint, 3)
        d["displacement"] = round(self.displacement, 2)
        d["mitigation_ratio"] = round(self.mitigation_ratio, 2)
        d["quality"] = round(self.quality, 1)
        return d


def detect(df: pd.DataFrame, timeframe: str, structure_events: list[dict] | None = None,
           lookback: int = 200, impulse_bars: int = 4,
           min_displacement: float = 1.2) -> list[OrderBlock]:
    """Find order blocks in the recent window.

    ``structure_events`` are the BOS/CHoCH/MSS dicts from
    :func:`goldassist.analysis.structure.analyse`; a block whose impulse broke
    structure is materially stronger than one that did not.
    """
    if len(df) < 30:
        return []

    a = atr(df)
    o, h, l, c = (df[x].to_numpy() for x in ("open", "high", "low", "close"))
    break_times = {e["time"] for e in (structure_events or [])}
    times = df.index
    start = max(1, len(df) - lookback)
    blocks: list[OrderBlock] = []

    for i in range(start, len(df) - impulse_bars):
        atr_i = float(a.iloc[i]) if not np.isnan(a.iloc[i]) else 0.0
        if atr_i <= 0:
            continue

        # Measure the leg that follows this candle.
        window = slice(i + 1, i + 1 + impulse_bars)
        up_move = (h[window].max() - c[i]) / atr_i
        down_move = (c[i] - l[window].min()) / atr_i

        is_down_candle = c[i] < o[i]
        is_up_candle = c[i] > o[i]

        if is_down_candle and up_move >= min_displacement:
            caused = any(int(times[j].timestamp()) in break_times for j in range(i + 1, i + 1 + impulse_bars))
            # A gap between this candle's high and the candle two ahead marks
            # true imbalance out of the block.
            has_gap = i + 2 < len(df) and l[i + 2] > h[i]
            blocks.append(OrderBlock("bullish", timeframe, float(h[i]), float(l[i]),
                                     times[i], i, up_move, caused, has_gap))
        elif is_up_candle and down_move >= min_displacement:
            caused = any(int(times[j].timestamp()) in break_times for j in range(i + 1, i + 1 + impulse_bars))
            has_gap = i + 2 < len(df) and h[i + 2] < l[i]
            blocks.append(OrderBlock("bearish", timeframe, float(h[i]), float(l[i]),
                                     times[i], i, down_move, caused, has_gap))

    _apply_mitigation(df, blocks)
    for b in blocks:
        b.quality = _grade(b, timeframe)
    return blocks


def _apply_mitigation(df: pd.DataFrame, blocks: list[OrderBlock]) -> None:
    """Track how deeply price has returned into each block since creation."""
    highs, lows, closes = df["high"].to_numpy(), df["low"].to_numpy(), df["close"].to_numpy()
    for b in blocks:
        span = b.top - b.bottom
        if span <= 0:
            continue
        deepest = 0.0
        for j in range(b.created_index + 2, len(df)):
            if b.direction == "bullish":
                penetration = (b.top - lows[j]) / span
                # A close below the block invalidates it outright.
                if closes[j] < b.bottom:
                    deepest = 1.2
                    break
            else:
                penetration = (highs[j] - b.bottom) / span
                if closes[j] > b.top:
                    deepest = 1.2
                    break
            if penetration > 0:
                deepest = max(deepest, penetration)
                if penetration >= 0.1:
                    b.tests += 1
        b.mitigation_ratio = min(1.5, deepest)
        # Fully traded through (or closed beyond) counts as mitigated.
        b.mitigated = deepest >= 0.95


def _grade(b: OrderBlock, timeframe: str) -> float:
    tf_weight = {"D1": 1.0, "H4": 0.9, "H1": 0.75, "M30": 0.65, "M15": 0.55,
                 "M5": 0.4}.get(timeframe, 0.5)
    score = 0.0
    score += min(35.0, b.displacement * 14.0)   # strength of departure
    score += 20.0 if b.caused_bos else 0.0      # structural significance
    score += 12.0 if b.has_fvg else 0.0         # imbalance left behind
    score += 20.0 * tf_weight                   # timeframe
    score += 13.0 * max(0.0, 1.0 - b.mitigation_ratio)  # freshness
    score -= min(12.0, max(0, b.tests - 1) * 4.0)
    if b.mitigated:
        score *= 0.3
    return float(np.clip(score, 0, 100))


def summarise(blocks: list[OrderBlock], price: float, atr_value: float) -> dict:
    active = [b for b in blocks if not b.mitigated]
    active.sort(key=lambda b: -b.quality)

    def nearest(direction: str) -> dict | None:
        pool = [b for b in active if b.direction == direction]
        if not pool:
            return None
        return min(pool, key=lambda b: abs(b.midpoint - price)).to_dict()

    inside = [b for b in active if b.bottom <= price <= b.top]
    near = [b for b in active if atr_value and abs(b.midpoint - price) <= atr_value * 4]
    bull = sum(b.quality for b in near if b.direction == "bullish")
    bear = sum(b.quality for b in near if b.direction == "bearish")

    return {
        "count": len(blocks),
        "active": [b.to_dict() for b in active[:10]],
        "nearest_bullish": nearest("bullish"),
        "nearest_bearish": nearest("bearish"),
        "price_inside": [b.to_dict() for b in inside],
        "bias": "bullish" if bull > bear * 1.3 else "bearish" if bear > bull * 1.3 else "neutral",
    }
