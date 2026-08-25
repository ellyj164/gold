"""Fair Value Gap detection.

A Fair Value Gap is a three-candle imbalance: candle 1's wick and candle 3's
wick fail to overlap, leaving a price band that traded only during candle 2's
expansion. Gold produces many of these; the useful ones are those created by
genuine displacement, so every gap is graded rather than merely listed.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict

import numpy as np
import pandas as pd

from .indicators import atr


@dataclass
class FVG:
    direction: str          # bullish | bearish
    timeframe: str
    top: float
    bottom: float
    created: pd.Timestamp
    created_index: int
    size: float
    size_atr: float
    displacement: float     # middle candle body in ATR multiples
    filled: bool = False
    fill_ratio: float = 0.0   # 0 = untouched, 1 = fully traded through
    tests: int = 0
    quality: float = 0.0      # 0..100

    @property
    def midpoint(self) -> float:
        return (self.top + self.bottom) / 2

    def to_dict(self) -> dict:
        d = asdict(self)
        d["created"] = int(self.created.timestamp())
        for k in ("top", "bottom", "size", "midpoint"):
            d[k] = round(getattr(self, k) if k == "midpoint" else d[k], 3)
        d["size_atr"] = round(self.size_atr, 2)
        d["displacement"] = round(self.displacement, 2)
        d["fill_ratio"] = round(self.fill_ratio, 2)
        d["quality"] = round(self.quality, 1)
        return d


def detect(df: pd.DataFrame, timeframe: str, lookback: int = 200,
           min_size_atr: float = 0.15) -> list[FVG]:
    """Find every unfilled-or-partially-filled FVG in the recent window."""
    if len(df) < 20:
        return []

    a = atr(df)
    highs, lows = df["high"].to_numpy(), df["low"].to_numpy()
    opens, closes = df["open"].to_numpy(), df["close"].to_numpy()
    start = max(2, len(df) - lookback)
    gaps: list[FVG] = []

    for i in range(start, len(df)):
        atr_i = float(a.iloc[i]) if not np.isnan(a.iloc[i]) else 0.0
        if atr_i <= 0:
            continue
        mid_body = abs(closes[i - 1] - opens[i - 1])
        displacement = mid_body / atr_i

        # Bullish gap: candle 3's low sits above candle 1's high.
        if lows[i] > highs[i - 2]:
            size = lows[i] - highs[i - 2]
            if size / atr_i >= min_size_atr:
                gaps.append(FVG("bullish", timeframe, float(lows[i]), float(highs[i - 2]),
                                df.index[i], i, float(size), size / atr_i, displacement))
        # Bearish gap: candle 3's high sits below candle 1's low.
        elif highs[i] < lows[i - 2]:
            size = lows[i - 2] - highs[i]
            if size / atr_i >= min_size_atr:
                gaps.append(FVG("bearish", timeframe, float(lows[i - 2]), float(highs[i]),
                                df.index[i], i, float(size), size / atr_i, displacement))

    _apply_fills(df, gaps)
    for g in gaps:
        g.quality = _grade(g)
    return gaps


def _apply_fills(df: pd.DataFrame, gaps: list[FVG]) -> None:
    """Mark how far price has since traded back into each gap."""
    highs, lows = df["high"].to_numpy(), df["low"].to_numpy()
    for g in gaps:
        span = g.top - g.bottom
        if span <= 0:
            g.filled = True
            g.fill_ratio = 1.0
            continue
        deepest = 0.0
        for j in range(g.created_index + 1, len(df)):
            if g.direction == "bullish":
                # Filled from the top down: price trading below g.top eats the gap.
                penetration = (g.top - lows[j]) / span
            else:
                penetration = (highs[j] - g.bottom) / span
            if penetration > 0:
                deepest = max(deepest, min(1.0, penetration))
                if penetration >= 0.05:
                    g.tests += 1
        g.fill_ratio = deepest
        g.filled = deepest >= 0.98


def _grade(g: FVG) -> float:
    """Score a gap 0-100 on displacement, size, freshness and how often tested."""
    score = 0.0
    score += min(40.0, g.displacement * 25.0)      # displacement is the key factor
    score += min(20.0, g.size_atr * 25.0)
    score += 20.0 * (1.0 - g.fill_ratio)           # untouched gaps are strongest
    score -= min(15.0, max(0, g.tests - 1) * 5.0)  # repeatedly probed gaps weaken
    if g.filled:
        score *= 0.25
    return float(np.clip(score, 0, 100))


def summarise(gaps: list[FVG], price: float, atr_value: float) -> dict:
    """Pick out the gaps that matter to the current price."""
    active = [g for g in gaps if not g.filled]
    active.sort(key=lambda g: -g.quality)

    def nearest(direction: str) -> dict | None:
        pool = [g for g in active if g.direction == direction]
        if not pool:
            return None
        return min(pool, key=lambda g: abs(g.midpoint - price)).to_dict()

    inside = [g for g in active if g.bottom <= price <= g.top]
    return {
        "count": len(gaps),
        "active": [g.to_dict() for g in active[:10]],
        "nearest_bullish": nearest("bullish"),
        "nearest_bearish": nearest("bearish"),
        "price_inside": [g.to_dict() for g in inside],
        "bias": _bias(active, price, atr_value),
    }


def _bias(active: list[FVG], price: float, atr_value: float) -> str:
    """Directional read: which side has the stronger nearby unfilled imbalance."""
    if not atr_value:
        return "neutral"
    near = [g for g in active if abs(g.midpoint - price) <= atr_value * 4]
    bull = sum(g.quality for g in near if g.direction == "bullish")
    bear = sum(g.quality for g in near if g.direction == "bearish")
    if bull > bear * 1.3:
        return "bullish"
    if bear > bull * 1.3:
        return "bearish"
    return "neutral"
