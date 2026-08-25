"""Supply and demand zone analysis.

A demand zone is the base (consolidation or single origin candle) that price
left rapidly to the upside; a supply zone is the mirror. Zones are graded on
freshness, the strength of the departure, how many times they have been
retested, and their timeframe -- a fresh zone is treated as materially stronger
than one price has already worked through.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict

import numpy as np
import pandas as pd

from .indicators import atr

TF_WEIGHT = {"D1": 1.0, "H4": 0.9, "H1": 0.75, "M30": 0.65, "M15": 0.55,
             "M5": 0.4, "M1": 0.2}


@dataclass
class Zone:
    kind: str            # demand | supply
    timeframe: str
    top: float
    bottom: float
    created: pd.Timestamp
    created_index: int
    base_candles: int
    departure: float     # impulse leg out of the base, in ATR multiples
    tests: int = 0
    penetration: float = 0.0   # deepest retest, 0 = untouched, >=1 = traded through
    broken: bool = False
    quality: float = 0.0

    @property
    def midpoint(self) -> float:
        return (self.top + self.bottom) / 2

    @property
    def fresh(self) -> bool:
        return self.tests == 0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["created"] = int(self.created.timestamp())
        d["top"] = round(float(self.top), 3)
        d["bottom"] = round(float(self.bottom), 3)
        d["midpoint"] = round(float(self.midpoint), 3)
        d["departure"] = round(float(self.departure), 2)
        d["penetration"] = round(float(self.penetration), 2)
        d["fresh"] = self.fresh
        d["quality"] = round(self.quality, 1)
        return d


def detect(df: pd.DataFrame, timeframe: str, lookback: int = 250,
           max_base: int = 4, min_departure: float = 1.5) -> list[Zone]:
    """Find zones by locating impulse legs and taking the base that preceded them."""
    if len(df) < 40:
        return []

    a = atr(df)
    o, h, l, c = (df[x].to_numpy() for x in ("open", "high", "low", "close"))
    start = max(max_base + 1, len(df) - lookback)
    zones: list[Zone] = []
    i = start

    while i < len(df) - 1:
        atr_i = float(a.iloc[i]) if not np.isnan(a.iloc[i]) else 0.0
        if atr_i <= 0:
            i += 1
            continue

        body = abs(c[i] - o[i])
        if body / atr_i < min_departure:
            i += 1
            continue

        bullish_leg = c[i] > o[i]
        # Walk backwards over the small candles that formed the base.
        base_start = i - 1
        while (
            base_start > start
            and (i - base_start) <= max_base
            and abs(c[base_start] - o[base_start]) / atr_i < 0.7
        ):
            base_start -= 1
        base_start = max(base_start + 1, i - max_base)
        if base_start >= i:
            base_start = i - 1

        base = slice(base_start, i)
        top = float(h[base].max()) if base_start < i else float(h[i - 1])
        bottom = float(l[base].min()) if base_start < i else float(l[i - 1])
        if top <= bottom:
            i += 1
            continue

        zones.append(
            Zone(
                kind="demand" if bullish_leg else "supply",
                timeframe=timeframe,
                top=top,
                bottom=bottom,
                created=df.index[base_start],
                created_index=base_start,
                base_candles=max(1, i - base_start),
                departure=body / atr_i,
            )
        )
        i += 1

    _apply_tests(df, zones)
    for z in zones:
        z.quality = _grade(z)
    return zones


def _apply_tests(df: pd.DataFrame, zones: list[Zone]) -> None:
    """Count retests and detect zones price has closed through."""
    highs, lows, closes = df["high"].to_numpy(), df["low"].to_numpy(), df["close"].to_numpy()
    for z in zones:
        span = z.top - z.bottom
        if span <= 0:
            continue
        deepest = 0.0
        inside_prev = False
        # Start after the impulse candle so the departure itself is not a test.
        for j in range(z.created_index + z.base_candles + 1, len(df)):
            if z.kind == "demand":
                if closes[j] < z.bottom:
                    z.broken = True
                    deepest = 1.2
                    break
                penetration = (z.top - lows[j]) / span
            else:
                if closes[j] > z.top:
                    z.broken = True
                    deepest = 1.2
                    break
                penetration = (highs[j] - z.bottom) / span

            inside = penetration > 0
            if inside:
                deepest = max(deepest, min(1.0, penetration))
                # One test per visit, not one per candle spent inside.
                if not inside_prev:
                    z.tests += 1
            inside_prev = inside
        z.penetration = min(1.5, deepest)


def _grade(z: Zone) -> float:
    score = 0.0
    score += min(35.0, z.departure * 14.0)               # strength of departure
    score += 25.0 * TF_WEIGHT.get(z.timeframe, 0.5)      # timeframe
    score += 20.0 if z.fresh else max(0.0, 14.0 - z.tests * 5.0)  # freshness
    score += max(0.0, 10.0 * (1.0 - z.penetration))      # how intact it is
    # Tight bases are cleaner origins than wide, messy ones.
    score += 8.0 if z.base_candles <= 2 else 4.0
    if z.broken:
        score *= 0.25
    return float(np.clip(score, 0, 100))


def summarise(zones: list[Zone], price: float, atr_value: float) -> dict:
    active = [z for z in zones if not z.broken]
    active.sort(key=lambda z: -z.quality)

    demand = sorted([z for z in active if z.kind == "demand" and z.top < price * 1.002],
                    key=lambda z: -z.top)
    supply = sorted([z for z in active if z.kind == "supply" and z.bottom > price * 0.998],
                    key=lambda z: z.bottom)
    inside = [z for z in active if z.bottom <= price <= z.top]

    return {
        "count": len(zones),
        "active": [z.to_dict() for z in active[:12]],
        "nearest_demand": demand[0].to_dict() if demand else None,
        "nearest_supply": supply[0].to_dict() if supply else None,
        "strongest_demand": max(demand, key=lambda z: z.quality).to_dict() if demand else None,
        "strongest_supply": max(supply, key=lambda z: z.quality).to_dict() if supply else None,
        "price_inside": [z.to_dict() for z in inside],
        "location": _location(inside, price),
        "bias": _bias(inside, demand, supply, price, atr_value),
    }


def _location(inside: list[Zone], price: float) -> str:
    if not inside:
        return "between zones"
    kinds = {z.kind for z in inside}
    if kinds == {"demand"}:
        return "inside demand"
    if kinds == {"supply"}:
        return "inside supply"
    return "inside overlapping supply and demand"


def _bias(inside: list[Zone], demand: list[Zone], supply: list[Zone],
          price: float, atr_value: float) -> str:
    """Being *in* a zone is the strongest read; otherwise favour the closer edge."""
    if inside:
        best = max(inside, key=lambda z: z.quality)
        return "bullish" if best.kind == "demand" else "bearish"
    if not atr_value:
        return "neutral"
    d = (price - demand[0].top) / atr_value if demand else None
    s = (supply[0].bottom - price) / atr_value if supply else None
    if d is not None and (s is None or d < s) and d < 1.5:
        return "bullish"
    if s is not None and (d is None or s < d) and s < 1.5:
        return "bearish"
    return "neutral"
