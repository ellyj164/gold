"""Historical data loading for backtests.

The live feed keeps only the window the dashboard needs, and that window is not
the same length on every timeframe — M5 at 2000 bars covers about ten days
while M15 at 2000 bars covers a month. Replaying against those frames silently
limits the test to the shortest timeframe's span.

This module pulls a deliberately deep, balanced history instead, and reports
the window that is actually usable across *all* timeframes.
"""

from __future__ import annotations

import logging

import pandas as pd

from ..config import BACKTEST_TIMEFRAMES
from ..data.source import DataUnavailable, MarketDataSource

log = logging.getLogger(__name__)

# MetaTrader 5 rejects a single copy_rates request above roughly this many
# bars with "Invalid params", so requests are capped and then stepped down.
MT5_MAX_BARS = 65000

# Bars requested per timeframe for a backtest. Chosen so every timeframe spans
# a comparable period rather than a comparable bar count.
BACKTEST_BARS = {
    "D1": 700,
    "H4": 3000,
    "H1": 12000,
    "M30": 22000,
    "M15": 40000,
    "M5": 60000,
}

# Each engine needs this many bars of its own timeframe before its output is
# meaningful (200-period EMA plus ATR median headroom).
MIN_BARS = 260


def load(source: MarketDataSource, bars: dict[str, int] | None = None) -> dict[str, pd.DataFrame]:
    """Download a deep history for every analysed timeframe."""
    requested = bars or BACKTEST_BARS
    frames: dict[str, pd.DataFrame] = {}
    missing: list[str] = []

    for tf in BACKTEST_TIMEFRAMES:
        want = min(requested.get(tf, 5000), MT5_MAX_BARS)
        df = None
        # Step down on failure: the terminal may hold less history than asked
        # for, and an over-large request is rejected outright.
        for attempt in (want, want // 2, want // 4, 5000):
            try:
                df = source.candles(tf, attempt)
                break
            except DataUnavailable as exc:
                log.debug("%s history request for %d bars failed: %s", tf, attempt, exc)
        if df is None:
            missing.append(tf)
            continue
        frames[tf] = df
        log.info("Loaded %s: %d bars (%s -> %s)", tf, len(df), df.index[0], df.index[-1])

    if missing:
        # Dropping a timeframe silently would invalidate the whole test: the
        # confirmation timeframe missing means no setup can ever confirm.
        raise DataUnavailable(
            "Could not load history for: " + ", ".join(missing)
            + ". Open those timeframes' charts in MT5 and scroll back so the "
              "terminal downloads the data, then retry."
        )
    return frames


def usable_window(frames: dict[str, pd.DataFrame]) -> tuple[pd.Timestamp, pd.Timestamp]:
    """The span over which *every* timeframe has enough history to analyse.

    The start is the latest of each timeframe's ``MIN_BARS``-th candle: before
    that point at least one engine would be running on too little data.
    """
    if not frames:
        raise ValueError("No historical data was loaded.")

    starts, ends = [], []
    for tf, df in frames.items():
        if len(df) < MIN_BARS:
            raise ValueError(
                f"{tf} has only {len(df)} bars; at least {MIN_BARS} are needed. "
                "Open the MT5 chart for that timeframe and scroll back to let the "
                "terminal download more history."
            )
        starts.append(df.index[MIN_BARS - 1])
        ends.append(df.index[-1])

    start, end = max(starts), min(ends)
    if start >= end:
        raise ValueError(
            "The timeframes do not overlap enough to backtest. The shortest history is "
            f"{min(len(df) for df in frames.values())} bars; load more history in MT5 "
            "(open each timeframe's chart and scroll back)."
        )
    return start, end


def describe(frames: dict[str, pd.DataFrame]) -> dict:
    """Coverage report, so a thin backtest is diagnosable rather than mysterious."""
    out = {}
    for tf, df in frames.items():
        span_days = (df.index[-1] - df.index[0]).total_seconds() / 86400
        out[tf] = {
            "bars": len(df),
            "from": df.index[0].isoformat(),
            "to": df.index[-1].isoformat(),
            "days": round(span_days, 1),
        }
    return out
