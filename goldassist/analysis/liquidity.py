"""Liquidity analysis.

Identifies where resting orders are likely to sit (buy-side liquidity above
highs, sell-side liquidity below lows) and detects when price has *taken* that
liquidity -- a wick beyond the level followed by a close back inside it.

A sweep is never treated as a reversal on its own. It is a location signal: it
tells the confluence engine that price reached for liquidity and rejected, at
which point structural confirmation still has to arrive.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict

import numpy as np
import pandas as pd

from .indicators import atr
from .structure import find_swings


@dataclass
class LiquidityPool:
    side: str            # buyside (above price highs) | sellside (below lows)
    price: float
    timeframe: str
    source: str          # equal_highs | equal_lows | swing | PDH | PDL | session
    touches: int
    last_touch: pd.Timestamp
    taken: bool = False
    strength: float = 0.0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["price"] = round(float(self.price), 3)
        d["last_touch"] = int(self.last_touch.timestamp())
        d["strength"] = round(self.strength, 1)
        return d


@dataclass
class LiquiditySweep:
    direction: str       # bullish (sell-side swept, closed back up) | bearish
    timeframe: str
    level: float
    time: pd.Timestamp
    index: int
    wick_depth_atr: float      # how far beyond the level price reached
    reclaimed_bars: int        # candles taken to close back inside
    confirmed_by_structure: bool = False
    strength: float = 0.0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["level"] = round(float(self.level), 3)
        d["time"] = int(self.time.timestamp())
        d["wick_depth_atr"] = round(float(self.wick_depth_atr), 2)
        d["reclaimed_bars"] = int(self.reclaimed_bars)
        d["index"] = int(self.index)
        d["strength"] = round(self.strength, 1)
        return d


def equal_levels(df: pd.DataFrame, timeframe: str, tolerance_atr: float = 0.12,
                 swing_strength: int = 2) -> list[LiquidityPool]:
    """Cluster swing extremes that sit at effectively the same price.

    Equal highs and equal lows are the clearest liquidity magnets: multiple
    rejections from one price imply stops resting just beyond it.
    """
    if len(df) < 40:
        return []
    a = atr(df)
    tol = float(a.iloc[-1] or 0) * tolerance_atr
    if tol <= 0:
        return []

    swings = find_swings(df, swing_strength)
    pools: list[LiquidityPool] = []

    for kind, side, source in (("high", "buyside", "equal_highs"), ("low", "sellside", "equal_lows")):
        points = [s for s in swings if s.kind == kind]
        used: set[int] = set()
        for i, s in enumerate(points):
            if i in used:
                continue
            cluster = [s]
            for j in range(i + 1, len(points)):
                if j in used:
                    continue
                if abs(points[j].price - s.price) <= tol:
                    cluster.append(points[j])
                    used.add(j)
            level = float(np.mean([c.price for c in cluster]))
            pools.append(
                LiquidityPool(
                    side=side,
                    price=level,
                    timeframe=timeframe,
                    source=source if len(cluster) > 1 else "swing",
                    touches=len(cluster),
                    last_touch=max(c.time for c in cluster),
                )
            )
    return pools


def reference_pools(d1: pd.DataFrame, h1: pd.DataFrame) -> list[LiquidityPool]:
    """Previous day / session extremes, which are standing liquidity targets."""
    pools: list[LiquidityPool] = []
    if len(d1) >= 2:
        prev = d1.iloc[-2]
        pools.append(LiquidityPool("buyside", float(prev["high"]), "D1", "PDH", 1, d1.index[-2]))
        pools.append(LiquidityPool("sellside", float(prev["low"]), "D1", "PDL", 1, d1.index[-2]))
    if len(h1):
        today = h1[h1.index.date == h1.index[-1].date()]
        if len(today):
            pools.append(LiquidityPool("buyside", float(today["high"].max()), "H1",
                                       "session", 1, today["high"].idxmax()))
            pools.append(LiquidityPool("sellside", float(today["low"].min()), "H1",
                                       "session", 1, today["low"].idxmin()))
    return pools


def detect_sweeps(df: pd.DataFrame, timeframe: str, pools: list[LiquidityPool],
                  lookback: int = 40, max_reclaim_bars: int = 3,
                  min_wick_atr: float = 0.1) -> list[LiquiditySweep]:
    """Find recent candles that wicked through a pool and closed back inside.

    ``max_reclaim_bars`` allows the reclaim to complete over a couple of
    candles, which is common on Gold where the sweep candle itself can close
    beyond the level before price snaps back.
    """
    if len(df) < 20 or not pools:
        return []

    a = atr(df)
    highs, lows, closes = df["high"].to_numpy(), df["low"].to_numpy(), df["close"].to_numpy()
    start = max(1, len(df) - lookback)
    sweeps: list[LiquiditySweep] = []

    for i in range(start, len(df)):
        atr_i = float(a.iloc[i]) if not np.isnan(a.iloc[i]) else 0.0
        if atr_i <= 0:
            continue
        for pool in pools:
            # Only levels that existed before this candle can be swept.
            if pool.last_touch >= df.index[i]:
                continue

            if pool.side == "sellside" and lows[i] < pool.price:
                depth = (pool.price - lows[i]) / atr_i
                if depth < min_wick_atr:
                    continue
                reclaim = _bars_to_reclaim(closes, i, pool.price, above=True, limit=max_reclaim_bars)
                if reclaim is not None:
                    sweeps.append(LiquiditySweep("bullish", timeframe, pool.price, df.index[i],
                                                 i, depth, reclaim))
                    pool.taken = True

            elif pool.side == "buyside" and highs[i] > pool.price:
                depth = (highs[i] - pool.price) / atr_i
                if depth < min_wick_atr:
                    continue
                reclaim = _bars_to_reclaim(closes, i, pool.price, above=False, limit=max_reclaim_bars)
                if reclaim is not None:
                    sweeps.append(LiquiditySweep("bearish", timeframe, pool.price, df.index[i],
                                                 i, depth, reclaim))
                    pool.taken = True

    for s in sweeps:
        s.strength = _grade_sweep(s)
    return sweeps


def _bars_to_reclaim(closes: np.ndarray, i: int, level: float, above: bool,
                     limit: int) -> int | None:
    """Candles needed to close back on the correct side of ``level``."""
    for k in range(0, limit + 1):
        j = i + k
        if j >= len(closes):
            return None
        if (closes[j] > level) if above else (closes[j] < level):
            return k
    return None


def _grade_sweep(s: LiquiditySweep) -> float:
    score = 40.0
    score += min(30.0, s.wick_depth_atr * 40.0)      # a decisive raid scores higher
    score += max(0.0, 20.0 - s.reclaimed_bars * 7.0)  # fast reclaim is stronger
    if s.confirmed_by_structure:
        score += 20.0
    return float(np.clip(score, 0, 100))


def _grade_pool(p: LiquidityPool, price: float, atr_value: float) -> float:
    tf_weight = {"D1": 1.0, "H4": 0.85, "H1": 0.7, "M30": 0.6, "M15": 0.5,
                 "M5": 0.35}.get(p.timeframe, 0.5)
    score = 25.0 * tf_weight
    score += min(30.0, p.touches * 14.0)             # equal highs/lows are magnets
    if p.source in ("PDH", "PDL"):
        score += 20.0
    elif p.source == "session":
        score += 10.0
    elif p.source in ("equal_highs", "equal_lows"):
        score += 15.0
    if atr_value:
        distance = abs(p.price - price) / atr_value
        score += max(0.0, 15.0 - distance * 2.0)     # nearby liquidity matters more
    if p.taken:
        score *= 0.4
    return float(np.clip(score, 0, 100))


def analyse(frames: dict[str, pd.DataFrame], price: float, atr_value: float,
            structure_by_tf: dict[str, dict] | None = None) -> dict:
    """Build the full liquidity picture across timeframes."""
    pools: list[LiquidityPool] = []
    sweeps: list[LiquiditySweep] = []

    for tf in ("H4", "H1", "M30", "M15", "M5"):
        df = frames.get(tf)
        if df is None or df.empty:
            continue
        tf_pools = equal_levels(df, tf)
        pools.extend(tf_pools)
        sweeps.extend(detect_sweeps(df, tf, tf_pools))

    d1, h1 = frames.get("D1"), frames.get("H1")
    if d1 is not None and h1 is not None:
        ref = reference_pools(d1, h1)
        pools.extend(ref)
        m15 = frames.get("M15")
        if m15 is not None:
            sweeps.extend(detect_sweeps(m15, "M15", ref))

    # A sweep gains conviction when structure shifted the same way shortly after.
    if structure_by_tf:
        for s in sweeps:
            events = (structure_by_tf.get(s.timeframe) or {}).get("events", [])
            s.confirmed_by_structure = any(
                e["direction"] == s.direction
                and e["time"] >= int(s.time.timestamp())
                and e["kind"] in ("CHoCH", "MSS", "BOS")
                for e in events
            )
            s.strength = _grade_sweep(s)

    for p in pools:
        p.strength = _grade_pool(p, price, atr_value)

    untaken = [p for p in pools if not p.taken]
    buyside = sorted([p for p in untaken if p.price > price], key=lambda p: p.price)
    sellside = sorted([p for p in untaken if p.price < price], key=lambda p: -p.price)
    sweeps.sort(key=lambda s: s.time, reverse=True)
    recent = sweeps[:6]

    return {
        "pools": [p.to_dict() for p in sorted(untaken, key=lambda p: -p.strength)[:12]],
        "nearest_buyside": buyside[0].to_dict() if buyside else None,
        "nearest_sellside": sellside[0].to_dict() if sellside else None,
        "sweeps": [s.to_dict() for s in recent],
        "latest_sweep": recent[0].to_dict() if recent else None,
        "bias": _bias(recent),
        "target": _draw_on_liquidity(buyside, sellside, price),
    }


def _bias(sweeps: list[LiquiditySweep]) -> str:
    """Direction implied by the most recent, strongest sweeps."""
    if not sweeps:
        return "neutral"
    bull = sum(s.strength for s in sweeps[:3] if s.direction == "bullish")
    bear = sum(s.strength for s in sweeps[:3] if s.direction == "bearish")
    if bull > bear * 1.2:
        return "bullish"
    if bear > bull * 1.2:
        return "bearish"
    return "neutral"


def _draw_on_liquidity(buyside: list[LiquidityPool], sellside: list[LiquidityPool],
                       price: float) -> dict | None:
    """The strongest untapped pool price is most likely reaching for."""
    candidates = [p for p in (buyside[:3] + sellside[:3])]
    if not candidates:
        return None
    best = max(candidates, key=lambda p: p.strength)
    return {
        "side": best.side,
        "price": round(float(best.price), 3),
        "distance": round(abs(best.price - price), 3),
        "source": best.source,
        "timeframe": best.timeframe,
        "strength": round(best.strength, 1),
    }
