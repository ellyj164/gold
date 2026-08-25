"""Multi-timeframe orchestration.

Runs every analysis engine over each timeframe and assembles one structured
"facts" object. Nothing here interprets or decides -- it only measures. The
confluence engine consumes this output, and the AI layer explains it.

Each timeframe has a defined job, per the specification:

============  ==========================================================
D1            macro context: major trend, major levels, big liquidity
H4            primary market structure and directional bias
H1            trading context: current trend, zones, setup areas
M30           intraday execution: timing entries inside the H4/D1 direction
M15           setup formation: sweeps, micro structure, entry area
M5            confirmation: BOS, rejection, retest, momentum
M1            detail: the sweep, wick or reversal that happens inside an M5
============  ==========================================================

Below M1 the broker serves no candles; :mod:`goldassist.analysis.micro`
aggregates the tick stream into five-, fifteen- and thirty-second bars and is
run separately, because it reads a live tape rather than closed history.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from ..config import TIMEFRAMES
from . import (arrays, candlesticks, events, fvg, indicators, levels, liquidity,
               orderblocks, regime, structure, volumeprofile, zones)

TIMEFRAME_ROLE = {
    "D1": "macro context",
    "H4": "primary market structure",
    "H1": "trading context",
    "M30": "intraday execution",
    "M15": "setup formation",
    "M5": "confirmation",
    "M1": "detail",
}

# Swing sensitivity per timeframe: higher timeframes use stronger fractals so
# only genuinely significant swings are labelled.
# M1 uses the strongest fractal of all: at that resolution a 2-bar swing is
# noise, and the point of reading M1 is to catch the few moves that are not.
SWING_STRENGTH = {"D1": 3, "H4": 3, "H1": 2, "M30": 2, "M15": 2, "M5": 2, "M1": 4}

# Timeframes that carry a volume profile.
#
# A profile answers "where has this market been traded lately", and "lately"
# has to mean something. 480 M15 candles is five trading days -- the week the
# market is currently in. 480 D1 candles is two years, which describes a Gold
# that no longer exists and would anchor a POC hundreds of dollars away, so
# D1 is excluded.
#
# Everything from H4 down to M5 is included, because this method is intraday
# and the profile that prices an entry is the profile of the timeframe the
# entry is timed on. An M5 profile spans about forty hours -- the last two
# sessions -- which is precisely the window a scalp is working inside, and
# excluding it would leave the fastest setups priced against a range they
# will never trade to. Each rung answers a different question and the cost is
# one histogram per *closed candle* per timeframe, not per cycle, because
# `analyse_timeframe_cached` sits in front of it.
PROFILE_TIMEFRAMES = ("H4", "H1", "M30", "M15", "M5")


def analyse_timeframe(df: pd.DataFrame, timeframe: str) -> dict:
    """Run every per-timeframe engine and return its measurements."""
    price = float(df["close"].iloc[-1])
    atr_series = indicators.atr(df)
    atr_value = float(atr_series.iloc[-1]) if not pd.isna(atr_series.iloc[-1]) else 0.0

    ind = indicators.snapshot(df)
    struct = structure.analyse(df, atr_series, strength=SWING_STRENGTH.get(timeframe, 2))
    reg = regime.detect(df, struct, ind)

    gaps = fvg.detect(df, timeframe)
    blocks = orderblocks.detect(df, timeframe, struct["events"])
    zn = zones.detect(df, timeframe)

    # Candle patterns are weighted by whether they formed at a zone or block
    # edge -- the same pattern in open space is worth far less.
    context_levels = (
        [{"price": z.top} for z in zn[-20:]]
        + [{"price": z.bottom} for z in zn[-20:]]
        + [{"price": b.top} for b in blocks[-20:]]
        + [{"price": b.bottom} for b in blocks[-20:]]
    )
    patterns = candlesticks.detect(df)

    # Where the market actually traded, as distinct from where it went. The
    # profile is only read on the timeframes whose window describes the market
    # being traded now -- a D1 profile over 480 days is a different instrument.
    profile = (volumeprofile.build(df) if timeframe in PROFILE_TIMEFRAMES else None)

    # Premium/discount and breakers, read off the swings `structure` just
    # found. Both answer "is this a good price", which no other engine here
    # asks -- they judge location against the range rather than against a
    # pattern.
    breakers = arrays.detect_breakers(df, struct["swings"])

    return {
        "timeframe": timeframe,
        "role": TIMEFRAME_ROLE.get(timeframe, ""),
        "price": round(price, 3),
        "atr": round(atr_value, 3),
        "last_candle_time": int(df.index[-1].timestamp()),
        "indicators": ind,
        "structure": struct,
        "regime": reg,
        "fvg": fvg.summarise(gaps, price, atr_value),
        "order_blocks": orderblocks.summarise(blocks, price, atr_value),
        "zones": zones.summarise(zn, price, atr_value),
        "candles": candlesticks.summarise(patterns, context_levels, atr_value * 0.3),
        "volume_profile": volumeprofile.summarise(profile, price, atr_value),
        # The raw histogram is kept separately: the chart draws it, and the
        # summary above is what every decision path reads.
        "volume_profile_raw": profile,
        "dealing_range": arrays.dealing_range(struct["swings"], price, atr_value),
        "breakers": arrays.summarise(breakers, price, atr_value),
        "bias": _timeframe_bias(ind, struct),
    }


def _timeframe_bias(ind: dict, struct: dict) -> str:
    """Directional read for one timeframe: structure leads, indicators support."""
    trend = struct.get("trend", "ranging")
    votes = [v.get("bias") for v in ind.values() if v.get("bias") in ("bullish", "bearish")]
    bull = votes.count("bullish")
    bear = votes.count("bearish")

    if trend == "bullish":
        return "bullish" if bull >= bear else "bullish (weakening)"
    if trend == "bearish":
        return "bearish" if bear >= bull else "bearish (weakening)"
    # No structural trend: fall back to a clear indicator majority, else neutral.
    if bull >= bear * 2 and bull >= 4:
        return "bullish"
    if bear >= bull * 2 and bear >= 4:
        return "bearish"
    return "neutral"


# Per-timeframe analysis, cached on the timestamp of the newest closed candle.
#
# The engines are handed *closed* candles only -- `MarketFeed.frame` drops the
# forming one -- so between two candle closes their input is byte-identical and
# so is their output. Re-running them on every five-second cycle recomputed the
# same daily analysis 17,280 times a day.
#
# This is invisible until the ladder gets deep. Adding M1 put a full engine
# suite on a timeframe that closes once a minute and was therefore being
# recomputed twelve times per candle for nothing, which measured at about a
# second of every five-second cycle. Caching removes that, and removes the same
# waste on D1 and H4, where it was always larger and simply never noticed.
_CACHE: dict[str, tuple[object, dict]] = {}

# Bounded so a long-running process cannot accumulate entries for timeframes it
# no longer reads. One entry per timeframe is all that is ever live.
_CACHE_LIMIT = 32


def analyse_timeframe_cached(df: pd.DataFrame, timeframe: str) -> dict:
    """``analyse_timeframe``, recomputed only when a new candle has closed."""
    if df is None or df.empty:
        return analyse_timeframe(df, timeframe)
    # The key is the newest closed candle's time *and* its close: a bar can be
    # revised in place by the broker after a disconnect, and a key of time
    # alone would serve the pre-revision analysis for as long as that bar
    # remained the newest.
    key = (df.index[-1], float(df["close"].iloc[-1]), len(df))
    cached = _CACHE.get(timeframe)
    if cached is not None and cached[0] == key:
        return cached[1]
    result = analyse_timeframe(df, timeframe)
    if len(_CACHE) >= _CACHE_LIMIT:
        _CACHE.clear()
    _CACHE[timeframe] = (key, result)
    return result


def reset_cache() -> None:
    """Drop every cached timeframe analysis.

    The backtester needs this: it replays history step by step, so its notion
    of "the newest closed candle" moves backwards between runs, and a cache
    keyed on that would serve one run's analysis to the next.
    """
    _CACHE.clear()


def analyse(frames: dict[str, pd.DataFrame], tick: dict | None = None) -> dict:
    """Full multi-timeframe analysis.

    ``frames`` maps timeframe -> closed candles. ``tick`` is the live quote,
    used for the headline price so the dashboard is never a candle behind.
    """
    per_tf = {tf: analyse_timeframe_cached(frames[tf], tf)
              for tf in TIMEFRAMES if tf in frames}
    if not per_tf:
        raise ValueError("No timeframe data supplied.")

    ref = per_tf.get("H1") or next(iter(per_tf.values()))
    price = float(tick["last"]) if tick and tick.get("last") else ref["price"]
    atr_value = ref["atr"]

    level_map = levels.build(frames, price, atr_value)
    liq = liquidity.analyse(
        frames, price, atr_value,
        structure_by_tf={tf: d["structure"] for tf, d in per_tf.items()},
    )
    reg = regime.consensus({tf: d["regime"] for tf, d in per_tf.items()})

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "price": round(price, 3),
        "tick": tick,
        "atr": atr_value,
        "timeframes": per_tf,
        "levels": level_map,
        "liquidity": liq,
        "regime": reg,
        "alignment": alignment(per_tf),
        # Consumed by the risk module; empty when no calendar has been supplied.
        "events": events.upcoming(),
    }


def alignment(per_tf: dict[str, dict]) -> dict:
    """How well the timeframes agree, and which one is the odd voice out.

    Weighted so that a disagreement between D1 and H4 matters far more than M5
    not yet having confirmed.
    """
    # D1 and H4 set direction; the execution timeframes describe what is
    # happening inside it and must not be able to outvote them.
    weights = {"D1": 3.0, "H4": 3.0, "H1": 2.0, "M30": 1.5, "M15": 1.2, "M5": 0.8}
    bull = bear = neutral = 0.0
    reads: dict[str, str] = {}

    for tf, data in per_tf.items():
        bias = data["bias"]
        reads[tf] = bias
        w = weights.get(tf, 1.0)
        if bias.startswith("bullish"):
            bull += w * (0.6 if "weakening" in bias else 1.0)
        elif bias.startswith("bearish"):
            bear += w * (0.6 if "weakening" in bias else 1.0)
        else:
            neutral += w

    total = bull + bear + neutral
    if total == 0:
        return {"direction": "neutral", "agreement": 0.0, "reads": reads, "conflicts": []}

    direction = "bullish" if bull > bear else "bearish" if bear > bull else "neutral"
    dominant = max(bull, bear)
    agreement = round(dominant / total, 2)

    conflicts = []
    for tf, bias in reads.items():
        if direction == "neutral":
            continue
        if not bias.startswith(direction):
            conflicts.append({
                "timeframe": tf,
                "role": TIMEFRAME_ROLE.get(tf, ""),
                "read": bias,
            })

    return {
        "direction": direction,
        "agreement": agreement,
        "reads": reads,
        "conflicts": conflicts,
        "higher_timeframe_bias": reads.get("H4") or reads.get("D1") or "neutral",
        "confirmation_timeframe_read": reads.get("M5", "neutral"),
    }
