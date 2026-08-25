"""Support and resistance engine.

Produces a single ranked list of price levels drawn from several sources
(swing clusters, previous day/week extremes, session extremes, pivots) and
scores each one on the factors the specification calls for: number of
reactions, source timeframe, recency, rejection strength, volume, and distance
from current price.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict, field

import numpy as np
import pandas as pd

from .indicators import atr
from .structure import find_swings

# Weight given to a level by the timeframe it came from. Higher timeframe
# levels hold more significance and therefore score higher.
# M1 is deliberately the lowest rung by a wide margin: it is read for detail,
# and a level found only on M1 should barely register against one the daily
# has respected for a week.
TF_WEIGHT = {"D1": 1.0, "H4": 0.8, "H1": 0.6, "M30": 0.5, "M15": 0.4,
             "M5": 0.25, "M1": 0.12}

# Trading sessions in UTC. Gold behaves differently in each, and session
# extremes are both reference levels and liquidity targets.
SESSIONS = {
    "asia": (0, 8),
    "london": (7, 16),
    "newyork": (12, 21),
}


@dataclass
class Level:
    price: float
    kind: str            # "support" | "resistance"
    source: str          # e.g. "swing", "PDH", "session_high:london"
    timeframe: str
    reactions: int = 1
    last_touch: pd.Timestamp | None = None
    rejection_strength: float = 0.0   # average ATR-normalised rejection wick
    volume_factor: float = 1.0
    strength: float = 0.0             # final 0..100 score
    distance_atr: float | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["price"] = round(self.price, 3)
        d["strength"] = round(self.strength, 1)
        d["rejection_strength"] = round(self.rejection_strength, 2)
        d["last_touch"] = int(self.last_touch.timestamp()) if self.last_touch is not None else None
        d["distance_atr"] = round(self.distance_atr, 2) if self.distance_atr is not None else None
        return d


# --- level sources ---------------------------------------------------------


def swing_levels(df: pd.DataFrame, timeframe: str, strength: int = 3,
                 tolerance_atr: float = 0.35) -> list[Level]:
    """Cluster swing points into levels, counting repeat reactions."""
    if len(df) < 40:
        return []
    a = atr(df)
    tol = float(a.iloc[-1] or 0) * tolerance_atr
    if tol <= 0:
        tol = float(df["close"].iloc[-1]) * 0.0008

    swings = find_swings(df, strength)
    clusters: list[Level] = []

    for s in swings:
        kind = "resistance" if s.kind == "high" else "support"
        candle = df.iloc[s.index]
        wick = (candle["high"] - max(candle["open"], candle["close"])) if s.kind == "high" \
            else (min(candle["open"], candle["close"]) - candle["low"])
        atr_at = float(a.iloc[s.index]) if not np.isnan(a.iloc[s.index]) else tol
        rejection = float(wick / atr_at) if atr_at else 0.0

        match = next(
            (c for c in clusters if c.kind == kind and abs(c.price - s.price) <= tol), None
        )
        if match:
            # Average the cluster price, weighted toward the most-tested area.
            match.price = (match.price * match.reactions + s.price) / (match.reactions + 1)
            match.reactions += 1
            match.last_touch = s.time
            match.rejection_strength = max(match.rejection_strength, rejection)
        else:
            clusters.append(
                Level(price=float(s.price), kind=kind, source="swing", timeframe=timeframe,
                      last_touch=s.time, rejection_strength=rejection)
            )

    return clusters


def reference_levels(d1: pd.DataFrame, intraday: pd.DataFrame) -> list[Level]:
    """Previous day/week extremes and today's session extremes."""
    out: list[Level] = []

    if len(d1) >= 2:
        prev = d1.iloc[-2]
        out.append(Level(float(prev["high"]), "resistance", "PDH", "D1",
                         last_touch=d1.index[-2], notes=["previous day high"]))
        out.append(Level(float(prev["low"]), "support", "PDL", "D1",
                         last_touch=d1.index[-2], notes=["previous day low"]))

    weekly = d1.resample("W").agg({"high": "max", "low": "min"})
    if len(weekly) >= 2:
        pw = weekly.iloc[-2]
        out.append(Level(float(pw["high"]), "resistance", "PWH", "D1",
                         last_touch=weekly.index[-2], notes=["previous week high"]))
        out.append(Level(float(pw["low"]), "support", "PWL", "D1",
                         last_touch=weekly.index[-2], notes=["previous week low"]))

    if len(intraday):
        today = intraday[intraday.index.date == intraday.index[-1].date()]
        for name, (start, end) in SESSIONS.items():
            block = today[(today.index.hour >= start) & (today.index.hour < end)]
            if block.empty:
                continue
            out.append(Level(float(block["high"].max()), "resistance", f"session_high:{name}",
                             "H1", last_touch=block["high"].idxmax(),
                             notes=[f"{name} session high"]))
            out.append(Level(float(block["low"].min()), "support", f"session_low:{name}",
                             "H1", last_touch=block["low"].idxmin(),
                             notes=[f"{name} session low"]))

    return out


def pivot_points(d1: pd.DataFrame) -> dict:
    """Classic floor-trader pivots from the previous completed day."""
    if len(d1) < 2:
        return {}
    prev = d1.iloc[-2]
    h, l, c = float(prev["high"]), float(prev["low"]), float(prev["close"])
    p = (h + l + c) / 3
    rng = h - l
    return {
        "P": round(p, 3),
        "R1": round(2 * p - l, 3), "S1": round(2 * p - h, 3),
        "R2": round(p + rng, 3), "S2": round(p - rng, 3),
        "R3": round(h + 2 * (p - l), 3), "S3": round(l - 2 * (h - p), 3),
    }


def fibonacci(swing_high: float, swing_low: float, direction: str) -> dict:
    """Retracements and extensions of the most recent impulse leg.

    ``direction`` is the direction of the impulse: "bullish" measures from low
    to high (retracements pull back down), "bearish" the reverse.
    """
    span = swing_high - swing_low
    if span <= 0:
        return {}
    retr_ratios = (0.236, 0.382, 0.5, 0.618, 0.705, 0.786)
    ext_ratios = (1.272, 1.414, 1.618, 2.0)
    if direction == "bullish":
        retr = {f"{r:.3f}": round(swing_high - span * r, 3) for r in retr_ratios}
        ext = {f"{r:.3f}": round(swing_low + span * r, 3) for r in ext_ratios}
    else:
        retr = {f"{r:.3f}": round(swing_low + span * r, 3) for r in retr_ratios}
        ext = {f"{r:.3f}": round(swing_high - span * r, 3) for r in ext_ratios}
    golden = retr.get("0.618"), retr.get("0.705")
    return {
        "direction": direction,
        "anchor_high": round(swing_high, 3),
        "anchor_low": round(swing_low, 3),
        "retracements": retr,
        "extensions": ext,
        # The 0.618-0.786 band is where continuation entries are usually sought.
        "optimal_entry_zone": sorted([retr["0.618"], retr["0.786"]]) if golden[0] else None,
    }


# --- scoring and assembly --------------------------------------------------


def score_levels(levels: list[Level], price: float, atr_value: float,
                 now: pd.Timestamp, volume_context: dict | None = None) -> list[Level]:
    """Assign a 0-100 strength score to each level."""
    for lvl in levels:
        score = 0.0
        # Repeat reactions are the strongest single evidence of significance.
        score += min(30.0, lvl.reactions * 12.0)
        # Higher timeframe origin.
        score += 25.0 * TF_WEIGHT.get(lvl.timeframe, 0.3)
        # Named reference levels (PDH/PDL/PWH/PWL) carry inherent weight.
        if lvl.source in ("PDH", "PDL", "PWH", "PWL"):
            score += 12.0
        elif lvl.source.startswith("session_"):
            score += 6.0
        # Rejection quality.
        score += min(15.0, lvl.rejection_strength * 12.0)
        # Recency: levels touched long ago decay.
        if lvl.last_touch is not None:
            age_days = max(0.0, (now - lvl.last_touch).total_seconds() / 86400)
            score += max(0.0, 12.0 - age_days * 0.6)
        # Volume confirmation where available.
        score += min(8.0, (lvl.volume_factor - 1.0) * 8.0) if lvl.volume_factor > 1 else 0.0

        if atr_value:
            lvl.distance_atr = abs(price - lvl.price) / atr_value
            # Levels far from price matter less to the current decision.
            if lvl.distance_atr > 8:
                score *= 0.6
            elif lvl.distance_atr > 4:
                score *= 0.85

        lvl.strength = float(np.clip(score, 0, 100))
    return levels


def merge(levels: list[Level], tolerance: float) -> list[Level]:
    """Collapse near-identical levels coming from different sources."""
    merged: list[Level] = []
    for lvl in sorted(levels, key=lambda x: x.price):
        prev = merged[-1] if merged else None
        if prev and prev.kind == lvl.kind and abs(prev.price - lvl.price) <= tolerance:
            keep, drop = (prev, lvl) if prev.strength >= lvl.strength else (lvl, prev)
            keep.reactions += drop.reactions
            keep.notes = list(dict.fromkeys(keep.notes + drop.notes + [f"also {drop.source}"]))
            keep.rejection_strength = max(keep.rejection_strength, drop.rejection_strength)
            # Cap the merge bonus: repeated merges used to compound a level
            # all the way to a misleading 100/100.
            keep.strength = min(92.0, max(keep.strength, drop.strength) + 4)
            merged[-1] = keep
            continue
        merged.append(lvl)
    return merged


def build(frames: dict[str, pd.DataFrame], price: float, atr_value: float,
          limit: int = 14) -> dict:
    """Build the ranked level map from every available timeframe."""
    levels: list[Level] = []
    for tf in ("D1", "H4", "H1", "M15"):
        df = frames.get(tf)
        if df is None or df.empty:
            continue
        levels.extend(swing_levels(df, tf, strength=3 if tf in ("D1", "H4") else 2))

    d1 = frames.get("D1")
    h1 = frames.get("H1")
    if d1 is not None and h1 is not None:
        levels.extend(reference_levels(d1, h1))

    now = max(df.index[-1] for df in frames.values() if len(df))
    levels = score_levels(levels, price, atr_value, now)
    # A level's role follows current price: anything above acts as resistance,
    # anything below as support, regardless of which side it formed on. Done
    # before merging so a flipped level and a same-price level of the new role
    # collapse into one.
    for lvl in levels:
        role = "resistance" if lvl.price > price else "support"
        if role != lvl.kind:
            lvl.notes = list(dict.fromkeys(lvl.notes + [f"broken {lvl.kind}, now acting as {role}"]))
            lvl.kind = role
    levels = merge(levels, tolerance=max(atr_value * 0.3, price * 0.0004))
    levels.sort(key=lambda x: -x.strength)
    top = levels[:limit]

    above = sorted([l for l in top if l.price > price], key=lambda x: x.price)
    below = sorted([l for l in top if l.price <= price], key=lambda x: -x.price)

    fib = {}
    h4 = frames.get("H4")
    if h4 is not None and len(h4) > 40:
        swings = find_swings(h4, 3)
        highs = [s for s in swings if s.kind == "high"]
        lows = [s for s in swings if s.kind == "low"]
        if highs and lows:
            last_high, last_low = highs[-1], lows[-1]
            direction = "bullish" if last_low.index > last_high.index else "bearish"
            fib = fibonacci(last_high.price, last_low.price, direction)

    return {
        "levels": [l.to_dict() for l in top],
        "nearest_resistance": above[0].to_dict() if above else None,
        "nearest_support": below[0].to_dict() if below else None,
        "strongest_resistance": max((l for l in top if l.price > price),
                                    key=lambda x: x.strength, default=None).to_dict()
        if any(l.price > price for l in top) else None,
        "strongest_support": max((l for l in top if l.price <= price),
                                 key=lambda x: x.strength, default=None).to_dict()
        if any(l.price <= price for l in top) else None,
        "pivots": pivot_points(d1) if d1 is not None else {},
        "fibonacci": fib,
    }
