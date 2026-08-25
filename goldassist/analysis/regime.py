"""Market regime detection.

The same evidence means different things in different conditions, so the
decision engine asks for the regime first. Regimes are derived from structure,
trend strength (ADX), volatility behaviour (ATR and Bollinger width) and where
price sits inside its recent range.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .indicators import adx, atr, bollinger

REGIMES = (
    "strong bullish trend",
    "strong bearish trend",
    "range",
    "consolidation",
    "expansion",
    "retracement",
    "potential reversal",
)


def detect(df: pd.DataFrame, structure: dict, indicators: dict) -> dict:
    """Classify the current regime for one timeframe.

    Returns the regime name, a 0-1 confidence, and the measurements behind it
    so the AI layer can explain the classification rather than assert it.
    """
    price = float(df["close"].iloc[-1])
    a = atr(df)
    atr_now = float(a.iloc[-1]) if not np.isnan(a.iloc[-1]) else 0.0
    atr_ref = float(a.tail(100).median()) if len(a.dropna()) > 20 else atr_now
    vol_ratio = (atr_now / atr_ref) if atr_ref else 1.0

    adx_df = adx(df)
    adx_now = float(adx_df["adx"].iloc[-1]) if not np.isnan(adx_df["adx"].iloc[-1]) else 0.0

    bb = bollinger(df["close"])
    width = bb["width"].dropna()
    width_now = float(width.iloc[-1]) if len(width) else 0.0
    width_pct = float((width.tail(100) < width_now).mean()) if len(width) > 20 else 0.5

    # Where price sits inside its own recent range.
    window = df.tail(40)
    hi, lo = float(window["high"].max()), float(window["low"].min())
    span = hi - lo
    position = (price - lo) / span if span > 0 else 0.5

    trend = structure.get("trend", "ranging")
    state = structure.get("state", "")
    last_event = structure.get("last_event") or {}
    squeeze = bool((indicators.get("bollinger") or {}).get("squeeze"))

    measures = {
        "adx": round(adx_now, 1),
        "volatility_ratio": round(vol_ratio, 2),
        "band_width_percentile": round(width_pct * 100, 0),
        "range_position": round(position, 2),
        "trend": trend,
        "structure_state": state,
    }

    regime, confidence, reason = _classify(
        trend, state, last_event, adx_now, vol_ratio, width_pct, position, squeeze
    )

    return {
        "regime": regime,
        "confidence": round(confidence, 2),
        "reason": reason,
        "measures": measures,
        "volatility": "high" if vol_ratio > 1.35 else "low" if vol_ratio < 0.7 else "normal",
        "atr": round(atr_now, 3),
        "atr_median": round(atr_ref, 3),
        "range_high": round(hi, 3),
        "range_low": round(lo, 3),
    }


def _classify(trend, state, last_event, adx_now, vol_ratio, width_pct, position, squeeze):
    # A very recent character change outranks everything else.
    if last_event.get("kind") in ("CHoCH", "MSS") and "reversal" in state:
        conf = 0.6 + (0.2 if last_event.get("kind") == "MSS" else 0.0)
        return ("potential reversal", conf,
                f"a {last_event['kind']} against the prior trend printed recently")

    if squeeze or (width_pct < 0.25 and vol_ratio < 0.85):
        return ("consolidation", 0.65,
                "volatility compressed into the lower quartile of its recent range")

    if vol_ratio > 1.5 and adx_now > 22:
        return ("expansion", 0.7,
                f"ATR is {vol_ratio:.1f}x its median with ADX at {adx_now:.0f}")

    if trend == "bullish" and adx_now >= 25 and position > 0.6:
        return ("strong bullish trend", min(0.95, 0.5 + adx_now / 100),
                f"bullish structure with ADX {adx_now:.0f} and price in the upper range")

    if trend == "bearish" and adx_now >= 25 and position < 0.4:
        return ("strong bearish trend", min(0.95, 0.5 + adx_now / 100),
                f"bearish structure with ADX {adx_now:.0f} and price in the lower range")

    if "retracement" in state:
        return ("retracement", 0.7, f"price is pulling back inside {trend} structure")

    if trend == "ranging" or adx_now < 18:
        return ("range", 0.6,
                f"no dominant structure and ADX at {adx_now:.0f} indicates no trend")

    # Trending but not strongly.
    return (f"strong {trend} trend" if adx_now >= 20 else "range",
            0.45,
            f"{trend} structure with moderate trend strength (ADX {adx_now:.0f})")


def consensus(by_timeframe: dict[str, dict]) -> dict:
    """Blend per-timeframe regimes into one headline read.

    Higher timeframes dominate: D1 and H4 set the environment, the lower
    timeframes describe what is happening inside it.
    """
    weights = {"D1": 3.0, "H4": 3.0, "H1": 2.0, "M30": 1.2, "M15": 1.0, "M5": 0.5}
    tally: dict[str, float] = {}
    for tf, data in by_timeframe.items():
        if not data:
            continue
        w = weights.get(tf, 1.0) * max(0.2, data.get("confidence", 0.5))
        tally[data["regime"]] = tally.get(data["regime"], 0.0) + w

    if not tally:
        return {"regime": "unknown", "confidence": 0.0, "by_timeframe": {}}

    regime = max(tally, key=tally.get)
    total = sum(tally.values())
    return {
        "regime": regime,
        "confidence": round(tally[regime] / total, 2),
        "by_timeframe": {tf: d.get("regime") for tf, d in by_timeframe.items() if d},
        "volatility": (by_timeframe.get("H1") or by_timeframe.get("M15") or {}).get("volatility", "normal"),
    }
