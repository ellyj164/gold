"""Candlestick analysis engine.

Detects the pattern vocabulary from the specification, but never scores a
pattern in isolation: every detection carries the context needed to judge it
(where it formed, how large it was relative to ATR, whether it rejected a
level). The confluence engine only rewards patterns that appear at a
meaningful location.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict

import numpy as np
import pandas as pd

from .indicators import atr

BULLISH = "bullish"
BEARISH = "bearish"
NEUTRAL = "neutral"


@dataclass
class CandlePattern:
    name: str
    direction: str
    time: pd.Timestamp
    index: int
    high: float
    low: float
    close: float
    strength: float  # 0..1, driven by body size relative to ATR and wick quality
    note: str

    def to_dict(self) -> dict:
        d = asdict(self)
        d["time"] = int(self.time.timestamp())
        for k in ("high", "low", "close"):
            d[k] = round(d[k], 3)
        d["strength"] = round(d["strength"], 2)
        return d


def _metrics(df: pd.DataFrame) -> pd.DataFrame:
    """Per-candle geometry used by every detector."""
    m = pd.DataFrame(index=df.index)
    m["body"] = (df["close"] - df["open"]).abs()
    m["range"] = (df["high"] - df["low"]).replace(0, np.nan)
    m["upper_wick"] = df["high"] - df[["open", "close"]].max(axis=1)
    m["lower_wick"] = df[["open", "close"]].min(axis=1) - df["low"]
    m["bull"] = df["close"] > df["open"]
    m["body_pct"] = m["body"] / m["range"]
    m["upper_pct"] = m["upper_wick"] / m["range"]
    m["lower_pct"] = m["lower_wick"] / m["range"]
    m["atr"] = atr(df)
    m["body_atr"] = m["body"] / m["atr"].replace(0, np.nan)
    m["range_atr"] = m["range"] / m["atr"].replace(0, np.nan)
    return m


def detect(df: pd.DataFrame, lookback: int = 12) -> list[CandlePattern]:
    """Scan the last ``lookback`` closed candles for patterns."""
    if len(df) < 30:
        return []

    m = _metrics(df)
    o, h, l, c = (df[x].to_numpy() for x in ("open", "high", "low", "close"))
    found: list[CandlePattern] = []
    start = max(3, len(df) - lookback)

    for i in range(start, len(df)):
        t = df.index[i]
        body_atr = _f(m["body_atr"].iloc[i])
        range_atr = _f(m["range_atr"].iloc[i])
        body_pct = _f(m["body_pct"].iloc[i])
        upper = _f(m["upper_pct"].iloc[i])
        lower = _f(m["lower_pct"].iloc[i])
        bull = bool(m["bull"].iloc[i])

        def add(name: str, direction: str, strength: float, note: str) -> None:
            found.append(
                CandlePattern(name, direction, t, i, float(h[i]), float(l[i]), float(c[i]),
                              min(1.0, max(0.0, strength)), note)
            )

        # --- engulfing --------------------------------------------------
        prev_body = _f(m["body"].iloc[i - 1])
        if prev_body and body_atr:
            if bull and c[i - 1] < o[i - 1] and c[i] >= o[i - 1] and o[i] <= c[i - 1]:
                add("bullish engulfing", BULLISH, 0.45 + min(0.4, body_atr / 2),
                    f"body engulfs the prior bearish candle ({body_atr:.1f}x ATR)")
            elif not bull and c[i - 1] > o[i - 1] and c[i] <= o[i - 1] and o[i] >= c[i - 1]:
                add("bearish engulfing", BEARISH, 0.45 + min(0.4, body_atr / 2),
                    f"body engulfs the prior bullish candle ({body_atr:.1f}x ATR)")

        # --- single-candle rejection shapes -----------------------------
        if body_pct and body_pct < 0.35 and lower and lower > 0.55 and upper < 0.2:
            name = "hammer" if bull else "bullish pin bar"
            add(name, BULLISH, 0.4 + min(0.4, (range_atr or 0) / 2.5),
                f"long lower wick ({lower * 100:.0f}% of range) rejecting lower prices")
        if body_pct and body_pct < 0.35 and upper and upper > 0.55 and lower < 0.2:
            name = "shooting star" if not bull else "inverted hammer"
            direction = BEARISH if name == "shooting star" else BULLISH
            add(name, direction, 0.4 + min(0.4, (range_atr or 0) / 2.5),
                f"long upper wick ({upper * 100:.0f}% of range) rejecting higher prices")
        if body_pct is not None and body_pct < 0.1:
            add("doji", NEUTRAL, 0.25, "open and close nearly equal - indecision")

        # --- inside bar --------------------------------------------------
        if h[i] < h[i - 1] and l[i] > l[i - 1]:
            add("inside bar", NEUTRAL, 0.3, "compression inside the prior candle range")

        # --- displacement / momentum ------------------------------------
        if body_atr and body_atr >= 1.3 and body_pct and body_pct > 0.6:
            direction = BULLISH if bull else BEARISH
            add(f"strong {'bullish' if bull else 'bearish'} displacement", direction,
                0.6 + min(0.35, (body_atr - 1.3) / 3),
                f"{body_atr:.1f}x ATR body with {body_pct * 100:.0f}% body fill")
        elif body_atr and body_atr >= 0.9 and body_pct and body_pct > 0.55:
            direction = BULLISH if bull else BEARISH
            add("momentum candle", direction, 0.45, f"{body_atr:.1f}x ATR directional body")

        # --- exhaustion ---------------------------------------------------
        if range_atr and range_atr >= 2.0 and body_pct and body_pct < 0.4:
            direction = BEARISH if upper > lower else BULLISH
            add("exhaustion candle", direction, 0.5,
                f"{range_atr:.1f}x ATR range but small body - momentum failing")

        # --- harami -------------------------------------------------------
        # The mirror of engulfing: a small candle contained inside the prior
        # body. Engulfing says the new side overwhelmed the old one; harami
        # says the old side simply stopped, which is the earlier and weaker
        # of the two warnings and is scored accordingly.
        prev_top = max(o[i - 1], c[i - 1])
        prev_bottom = min(o[i - 1], c[i - 1])
        prev_body_atr = _f(m["body_atr"].iloc[i - 1]) or 0
        if (prev_body_atr >= 0.8 and body_atr and body_atr < prev_body_atr * 0.6
                and max(o[i], c[i]) <= prev_top and min(o[i], c[i]) >= prev_bottom):
            if c[i - 1] < o[i - 1] and bull:
                add("bullish harami", BULLISH, 0.4,
                    "small body inside a large bearish one - selling has stalled")
            elif c[i - 1] > o[i - 1] and not bull:
                add("bearish harami", BEARISH, 0.4,
                    "small body inside a large bullish one - buying has stalled")

        # --- piercing line and dark cloud cover ---------------------------
        # Half-measures of engulfing: the close reclaims more than half of the
        # prior body without covering it entirely.
        prev_body = _f(m["body"].iloc[i - 1]) or 0
        if prev_body and prev_body_atr >= 0.7:
            midpoint = (o[i - 1] + c[i - 1]) / 2
            if (c[i - 1] < o[i - 1] and bull
                    and o[i] < l[i - 1] and midpoint < c[i] < o[i - 1]):
                add("piercing line", BULLISH, 0.5,
                    "opened below the prior low and closed back above its midpoint")
            elif (c[i - 1] > o[i - 1] and not bull
                    and o[i] > h[i - 1] and o[i - 1] < c[i] < midpoint):
                add("dark cloud cover", BEARISH, 0.5,
                    "opened above the prior high and closed back below its midpoint")

        # --- tweezers -----------------------------------------------------
        # Two candles rejecting from the same price. The tolerance is a
        # fraction of ATR rather than an exact match, because an exact tick
        # match is a coincidence and "the same level twice" is the pattern.
        tick = (_f(m["atr"].iloc[i]) or 0) * 0.08
        if tick > 0 and i >= 1:
            if abs(l[i] - l[i - 1]) <= tick and lower and lower > 0.35:
                add("tweezer bottom", BULLISH, 0.45,
                    "two candles rejecting from the same low")
            if abs(h[i] - h[i - 1]) <= tick and upper and upper > 0.35:
                add("tweezer top", BEARISH, 0.45,
                    "two candles rejecting from the same high")

        # --- marubozu and spinning top ------------------------------------
        if body_pct and body_pct > 0.9 and body_atr and body_atr >= 0.8:
            add(f"{'bullish' if bull else 'bearish'} marubozu",
                BULLISH if bull else BEARISH, 0.55,
                "almost no wick - one side controlled the entire candle")
        if (body_pct and body_pct < 0.3 and upper and lower
                and upper > 0.3 and lower > 0.3):
            add("spinning top", NEUTRAL, 0.25,
                "small body between two wicks - balance, not direction")

        # --- three-candle stars -------------------------------------------
        if i >= 2:
            first_bear = c[i - 2] < o[i - 2]
            first_bull = c[i - 2] > o[i - 2]
            mid_small = _f(m["body_atr"].iloc[i - 1]) or 0
            mid_small = mid_small < 0.5
            midpoint_prev = (o[i - 2] + c[i - 2]) / 2
            if first_bear and mid_small and bull and c[i] > midpoint_prev:
                add("morning star", BULLISH, 0.6, "three-candle bullish reversal formation")
            if first_bull and mid_small and not bull and c[i] < midpoint_prev:
                add("evening star", BEARISH, 0.6, "three-candle bearish reversal formation")

        # --- three soldiers and three crows -------------------------------
        # Three real bodies marching the same way, each closing beyond the
        # last. The strongest continuation formation in the classical
        # vocabulary and the one most often confused with exhaustion, so the
        # note says which it is only when the bodies are not shrinking.
        if i >= 2:
            bodies = [_f(m["body_atr"].iloc[j]) or 0 for j in (i - 2, i - 1, i)]
            fills = [_f(m["body_pct"].iloc[j]) or 0 for j in (i - 2, i - 1, i)]
            if all(b >= 0.5 for b in bodies) and all(f > 0.55 for f in fills):
                rising = all(c[j] > o[j] for j in (i - 2, i - 1, i)) and \
                    c[i] > c[i - 1] > c[i - 2]
                falling = all(c[j] < o[j] for j in (i - 2, i - 1, i)) and \
                    c[i] < c[i - 1] < c[i - 2]
                fading = bodies[2] < bodies[0] * 0.6
                if rising:
                    add("three white soldiers", BULLISH, 0.5 if fading else 0.65,
                        "three strong bullish closes in a row"
                        + (" but the bodies are shrinking - buying is tiring"
                           if fading else ""))
                elif falling:
                    add("three black crows", BEARISH, 0.5 if fading else 0.65,
                        "three strong bearish closes in a row"
                        + (" but the bodies are shrinking - selling is tiring"
                           if fading else ""))

        # --- three inside up / down ---------------------------------------
        # A harami that then confirmed. The confirmation is the whole point:
        # a harami alone is a stall, a harami followed through is a turn.
        if i >= 2:
            two_top = max(o[i - 2], c[i - 2])
            two_bottom = min(o[i - 2], c[i - 2])
            inside = (max(o[i - 1], c[i - 1]) <= two_top
                      and min(o[i - 1], c[i - 1]) >= two_bottom)
            if inside and (_f(m["body_atr"].iloc[i - 2]) or 0) >= 0.8:
                if c[i - 2] < o[i - 2] and bull and c[i] > two_top:
                    add("three inside up", BULLISH, 0.6,
                        "a bullish harami confirmed by a close above it")
                elif c[i - 2] > o[i - 2] and not bull and c[i] < two_bottom:
                    add("three inside down", BEARISH, 0.6,
                        "a bearish harami confirmed by a close below it")

        # --- breakout and failed breakout ---------------------------------
        window_high = h[max(0, i - 20):i].max() if i > 0 else h[i]
        window_low = l[max(0, i - 20):i].min() if i > 0 else l[i]
        if c[i] > window_high and body_pct and body_pct > 0.5:
            add("breakout candle", BULLISH, 0.5, "closed above the 20-candle high")
        elif h[i] > window_high and c[i] < window_high:
            add("failed breakout candle", BEARISH, 0.55,
                "wicked above the 20-candle high but closed back inside")
        if c[i] < window_low and body_pct and body_pct > 0.5:
            add("breakdown candle", BEARISH, 0.5, "closed below the 20-candle low")
        elif l[i] < window_low and c[i] > window_low:
            add("failed breakdown candle", BULLISH, 0.55,
                "wicked below the 20-candle low but closed back inside")

    return found


def summarise(patterns: list[CandlePattern], levels: list[dict] | None = None,
              tolerance: float = 0.0) -> dict:
    """Reduce detections to a directional read, weighting recency and location.

    ``levels`` are significant prices (support/resistance, zone edges). A
    pattern formed within ``tolerance`` of one of them is worth materially more
    than the same pattern in open space -- the distinction the spec insists on.
    """
    if not patterns:
        return {"bias": NEUTRAL, "score": 0.0, "patterns": [], "latest": None}

    latest_index = max(p.index for p in patterns)
    scored: list[tuple[CandlePattern, float]] = []
    for p in patterns:
        if p.direction == NEUTRAL:
            weight = 0.0
        else:
            # Recency decay: a pattern 6 candles back carries about half the weight.
            age = latest_index - p.index
            weight = p.strength * (0.9 ** age)
            if levels and tolerance and _near_level(p, levels, tolerance):
                weight *= 1.8
        scored.append((p, weight))

    bull = sum(w for p, w in scored if p.direction == BULLISH)
    bear = sum(w for p, w in scored if p.direction == BEARISH)
    net = bull - bear
    bias = BULLISH if net > 0.25 else BEARISH if net < -0.25 else NEUTRAL

    enriched = []
    for p, w in sorted(scored, key=lambda x: (-x[1], -x[0].index))[:10]:
        d = p.to_dict()
        d["weight"] = round(w, 3)
        d["at_level"] = bool(levels and tolerance and _near_level(p, levels, tolerance))
        enriched.append(d)

    return {
        "bias": bias,
        "score": round(net, 3),
        "bullish_weight": round(bull, 3),
        "bearish_weight": round(bear, 3),
        "patterns": enriched,
        "latest": max(patterns, key=lambda p: p.index).to_dict(),
    }


def _near_level(p: CandlePattern, levels: list[dict], tolerance: float) -> bool:
    for lvl in levels:
        price = lvl.get("price")
        if price is None:
            continue
        if p.low - tolerance <= price <= p.high + tolerance:
            return True
    return False


def _f(value) -> float | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return None if np.isnan(f) else f
