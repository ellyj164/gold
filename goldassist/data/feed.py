"""In-memory multi-timeframe candle cache.

The analysis engines want a stable window of history per timeframe. The feed
holds that window, refreshes it cheaply (only the tail is re-requested), and
reports when a new candle has closed so the rest of the app knows to re-analyse.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime

import pandas as pd

from ..config import (HISTORY_BARS, TICK_WINDOW_SECONDS, TIMEFRAMES,
                      TRACKING_TIMEFRAME)
from .source import DataUnavailable, MarketDataSource

log = logging.getLogger(__name__)

# Bars pulled on an incremental refresh. Generous enough to repair any gap
# caused by a short disconnect without re-downloading the whole window.
REFRESH_BARS = 50

# The tracking timeframe is refreshed harder: it exists to catch the wick that
# touched a stop between two polls, so a gap in it costs an outcome.
TRACKING_REFRESH_BARS = 120


class MarketFeed:
    """Cached candles for the analysis timeframes, plus a tracking timeframe.

    ``tracking`` (M1 by default) is fetched and cached like any other, but is
    kept out of :meth:`frames` so no analysis engine ever sees it. It is there
    for :mod:`goldassist.signals.resolve`, which needs the finest candles
    available to decide whether a target or a stop was actually touched.
    """

    def __init__(self, source: MarketDataSource, timeframes=TIMEFRAMES,
                 tracking: str | None = TRACKING_TIMEFRAME):
        self.source = source
        self.timeframes = tuple(timeframes)
        # The tracking timeframe is kept whether or not it is also analysed.
        # It used to be dropped when it appeared in ``timeframes``, on the
        # assumption the two sets were disjoint; now that M1 is analysed as
        # well as tracked, that assumption would silently disable bar-accurate
        # outcome resolution -- the thing tracking exists for.
        self.tracking = tracking or None
        self._all = self.timeframes + (
            (self.tracking,) if self.tracking and self.tracking not in self.timeframes
            else ())
        self._ticks = None
        self._frames: dict[str, pd.DataFrame] = {}
        self._last_closed: dict[str, pd.Timestamp] = {}
        self._lock = threading.RLock()
        self.last_error: str | None = None
        self.last_update: datetime | None = None
        # True when the most recent refresh failed on *every* timeframe, which
        # means the terminal connection is gone rather than one series being
        # briefly unavailable. The service reconnects on this.
        self.all_failed = False

    # -- loading ------------------------------------------------------------

    def prime(self) -> None:
        """Download the full history window for every timeframe."""
        for tf in self._all:
            try:
                df = self.source.candles(tf, HISTORY_BARS.get(tf, 1000))
            except DataUnavailable:
                if tf in self.timeframes:
                    raise
                # The tracking timeframe is a refinement, not a requirement:
                # without it resolution falls back to the live quote.
                log.warning("No %s candles available; outcome resolution will "
                            "use the live quote alone", tf)
                continue
            with self._lock:
                self._frames[tf] = df
                self._last_closed[tf] = df.index[-2] if len(df) > 1 else df.index[-1]
        self.last_update = datetime.now()

    def refresh(self) -> dict[str, bool]:
        """Pull recent bars for each timeframe.

        Returns a map of timeframe -> whether a new candle closed since the
        previous refresh.

        A failure here is recorded rather than raised, because one bad
        timeframe must not kill the analysis loop -- but it is recorded
        *properly*. The previous version assigned ``last_error`` inside the
        loop and then unconditionally cleared it at the end, so a cycle in
        which every single timeframe failed finished by reporting no error at
        all. That is exactly what happened on 2026-09-05: seven consecutive
        "IPC send failed" warnings per cycle, and an interface that went on
        saying the feed was healthy while the chart quietly froze.
        """
        closed: dict[str, bool] = {}
        failures: list[str] = []
        for tf in self._all:
            try:
                tail = self.source.candles(
                    tf, TRACKING_REFRESH_BARS if tf == self.tracking else REFRESH_BARS)
            except DataUnavailable as exc:
                failures.append(f"{tf}: {exc}")
                log.warning("Refresh failed for %s: %s", tf, exc)
                closed[tf] = False
                continue
            with self._lock:
                base = self._frames.get(tf)
                if base is None:
                    merged = tail
                else:
                    merged = pd.concat([base[~base.index.isin(tail.index)], tail])
                    merged = merged.sort_index().tail(HISTORY_BARS.get(tf, 1000))
                self._frames[tf] = merged
                # The final row is the still-forming candle; the one before it
                # is the most recently closed candle.
                newest_closed = merged.index[-2] if len(merged) > 1 else merged.index[-1]
                closed[tf] = newest_closed != self._last_closed.get(tf)
                self._last_closed[tf] = newest_closed

        # Every timeframe failing is a dead connection, not a slow one, and it
        # is the case the terminal produces when MetaTrader is closed or has
        # handed its IPC channel to another client. Naming it separately lets
        # the service reconnect instead of retrying a socket that is gone.
        self.all_failed = len(failures) == len(self._all)
        if failures:
            self.last_error = "; ".join(failures[:3])
            if len(failures) > 3:
                self.last_error += f" (and {len(failures) - 3} more)"
        else:
            self.last_error = None
            self.last_update = datetime.now()
        return closed

    # -- access -------------------------------------------------------------

    def frame(self, timeframe: str, closed_only: bool = True) -> pd.DataFrame:
        """Return the cached candles for a timeframe.

        ``closed_only`` drops the forming candle, which is what every analysis
        engine should use: structure and patterns are only meaningful once a
        candle has closed.
        """
        with self._lock:
            df = self._frames.get(timeframe)
        if df is None or df.empty:
            raise DataUnavailable(f"No cached {timeframe} data yet.")
        return df.iloc[:-1].copy() if closed_only and len(df) > 1 else df.copy()

    def forming_candle(self, timeframe: str) -> pd.Series | None:
        with self._lock:
            df = self._frames.get(timeframe)
        return df.iloc[-1].copy() if df is not None and len(df) else None

    def frames(self, closed_only: bool = True) -> dict[str, pd.DataFrame]:
        return {tf: self.frame(tf, closed_only) for tf in self.timeframes if tf in self._frames}

    def tracking_frame(self, closed_only: bool = True):
        """The finest candles available, for outcome resolution.

        Returns None rather than raising: a missing M1 series degrades
        resolution to the live quote, which is a smaller problem than a
        cycle that dies.
        """
        if not self.tracking:
            return None
        try:
            return self.frame(self.tracking, closed_only)
        except DataUnavailable:
            return None

    # -- the tape -----------------------------------------------------------

    def refresh_ticks(self, seconds: int = TICK_WINDOW_SECONDS):
        """Pull recent raw ticks, for the sub-minute aggregation.

        Never raises. A feed that cannot serve tick history costs the tape
        reading and nothing else, so a failure here must not take an analysis
        cycle down with it.
        """
        getter = getattr(self.source, "ticks", None)
        if getter is None:
            return None
        try:
            frame = getter(seconds)
        except Exception:
            log.debug("Tick refresh failed", exc_info=True)
            return None
        with self._lock:
            self._ticks = frame
        return frame

    def ticks(self):
        """The most recently pulled tick frame, or None."""
        with self._lock:
            return None if self._ticks is None else self._ticks.copy()

    @property
    def ready(self) -> bool:
        return bool(self._frames) and all(tf in self._frames for tf in self.timeframes)
