"""Market data sources.

The only production source is MetaTrader 5, which supplies XAUUSD candles for
every analysed timeframe plus live ticks. Everything downstream depends only on
the :class:`MarketDataSource` interface, so an alternative feed can be dropped
in without touching the analysis engines.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Protocol

import pandas as pd

from ..config import MT5Config

log = logging.getLogger(__name__)

# Candle columns every source must return, indexed by UTC open time.
OHLC_COLUMNS = ["open", "high", "low", "close", "volume", "spread"]


class DataUnavailable(RuntimeError):
    """Raised when the market data source cannot serve a request."""


class MarketDataSource(Protocol):
    symbol: str

    def connect(self) -> None: ...
    def shutdown(self) -> None: ...
    def candles(self, timeframe: str, count: int, end: datetime | None = None) -> pd.DataFrame: ...
    def tick(self) -> dict: ...
    def ticks(self, seconds: int) -> pd.DataFrame: ...
    def symbol_details(self) -> dict: ...


class MT5Source:
    """Reads XAUUSD data from a running MetaTrader 5 terminal."""

    def __init__(self, config: MT5Config):
        self.config = config
        self.symbol: str = ""
        self._mt5 = None
        self._lock = threading.RLock()
        self._connected = False
        # Set once the application is closing. ``mt5.initialize`` starts the
        # terminal when it is not running, so any connect attempt after this
        # point would reopen MT5 behind a window the trader has already closed.
        self._closed = False
        # Terminal processes that appeared during our own initialize calls,
        # i.e. the ones this application opened and should close again.
        self.launched_pids: set[int] = set()

    # -- connection ---------------------------------------------------------

    def close_for_good(self) -> None:
        """Disconnect and refuse every later connect, including reconnects."""
        # Set outside the lock: a reconnect can hold it for seconds inside
        # initialize, and the flag must take effect for the next attempt anyway.
        self._closed = True
        self.shutdown()

    def connect(self) -> None:
        with self._lock:
            if self._connected:
                return
            if self._closed:
                raise DataUnavailable("The application is closing.")
            try:
                import MetaTrader5 as mt5  # imported lazily: Windows-only package
            except ImportError as exc:  # pragma: no cover - environment dependent
                raise DataUnavailable(
                    "The MetaTrader5 package is not installed. Run: pip install MetaTrader5"
                ) from exc
            self._mt5 = mt5

            kwargs = {}
            if self.config.terminal_path:
                kwargs["path"] = self.config.terminal_path
            if self.config.login:
                kwargs.update(
                    login=self.config.login,
                    password=self.config.password or "",
                    server=self.config.server or "",
                )
            from . import terminal

            before = terminal.running()
            initialized = mt5.initialize(**kwargs)
            self.launched_pids |= terminal.running() - before
            if not initialized:
                code, msg = mt5.last_error()
                raise DataUnavailable(
                    f"Could not connect to the MetaTrader 5 terminal ({code}: {msg}). "
                    "Open MT5, log into your account, and enable Tools > Options > "
                    "Expert Advisors > Allow algorithmic trading."
                )
            self.symbol = self._resolve_symbol()
            self._connected = True
            info = mt5.account_info()
            log.info(
                "MT5 connected: symbol=%s account=%s server=%s",
                self.symbol,
                getattr(info, "login", "?"),
                getattr(info, "server", "?"),
            )

    def _resolve_symbol(self) -> str:
        """Find the broker's name for Gold and make sure it is selected."""
        mt5 = self._mt5
        for name in self.config.symbol_candidates:
            if mt5.symbol_info(name) is not None:
                mt5.symbol_select(name, True)
                return name
        # Fall back to scanning every symbol the broker exposes.
        for sym in mt5.symbols_get() or []:
            if "XAU" in sym.name.upper() and "USD" in sym.name.upper():
                mt5.symbol_select(sym.name, True)
                return sym.name
        raise DataUnavailable(
            "No XAUUSD symbol found in this MT5 account. Check Market Watch, or set "
            "GOLDASSIST_SYMBOL to the exact symbol name your broker uses."
        )

    def shutdown(self) -> None:
        with self._lock:
            if self._mt5 is not None and self._connected:
                self._mt5.shutdown()
            self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    # -- data ---------------------------------------------------------------

    def _tf_const(self, timeframe: str):
        mt5 = self._mt5
        mapping = {
            "M1": mt5.TIMEFRAME_M1,
            "M5": mt5.TIMEFRAME_M5,
            "M15": mt5.TIMEFRAME_M15,
            "M30": mt5.TIMEFRAME_M30,
            "H1": mt5.TIMEFRAME_H1,
            "H4": mt5.TIMEFRAME_H4,
            "D1": mt5.TIMEFRAME_D1,
            "W1": mt5.TIMEFRAME_W1,
            "MN1": mt5.TIMEFRAME_MN1,
        }
        if timeframe not in mapping:
            raise ValueError(f"Unsupported timeframe: {timeframe}")
        return mapping[timeframe]

    def candles(self, timeframe: str, count: int, end: datetime | None = None) -> pd.DataFrame:
        self.connect()
        with self._lock:
            tf = self._tf_const(timeframe)
            if end is None:
                rates = self._mt5.copy_rates_from_pos(self.symbol, tf, 0, count)
            else:
                rates = self._mt5.copy_rates_from(self.symbol, tf, end, count)
        if rates is None or len(rates) == 0:
            code, msg = self._mt5.last_error()
            raise DataUnavailable(
                f"No {timeframe} candles returned for {self.symbol} ({code}: {msg})."
            )
        return _frame_from_rates(rates)

    def find_symbol(self, candidates) -> str | None:
        """Resolve the broker's name for a *secondary* instrument, or None.

        Used for context symbols such as a dollar index. Unlike the Gold
        resolver this never raises: context is a bonus, and an account that
        does not carry the symbol must still run normally.
        """
        try:
            self.connect()
        except DataUnavailable:
            return None
        with self._lock:
            for name in candidates:
                try:
                    if self._mt5.symbol_info(name) is not None:
                        self._mt5.symbol_select(name, True)
                        return name
                except Exception:  # pragma: no cover - terminal dependent
                    continue
            # Brokers prefix and suffix symbols freely (USDXm, DXY.cash), so
            # fall back to matching against everything on offer.
            wanted = {c.upper() for c in candidates}
            for sym in self._mt5.symbols_get() or []:
                upper = sym.name.upper()
                if any(w in upper for w in wanted):
                    self._mt5.symbol_select(sym.name, True)
                    return sym.name
        return None

    def candles_for(self, symbol: str, timeframe: str, count: int) -> pd.DataFrame:
        """Candles for a symbol other than the traded one."""
        self.connect()
        with self._lock:
            rates = self._mt5.copy_rates_from_pos(
                symbol, self._tf_const(timeframe), 0, count)
        if rates is None or len(rates) == 0:
            raise DataUnavailable(f"No {timeframe} candles returned for {symbol}.")
        return _frame_from_rates(rates)

    def tick(self) -> dict:
        self.connect()
        with self._lock:
            t = self._mt5.symbol_info_tick(self.symbol)
        if t is None:
            raise DataUnavailable(f"No tick available for {self.symbol}.")
        return {
            "time": datetime.fromtimestamp(t.time, tz=timezone.utc),
            "bid": t.bid,
            "ask": t.ask,
            "last": t.last or t.bid,
            "spread": round(t.ask - t.bid, 3),
        }

    def ticks(self, seconds: int = 900) -> pd.DataFrame:
        """Raw ticks for the last ``seconds``, indexed by UTC time.

        The finest candle MetaTrader will serve is M1, so anything below a
        minute has to be built from the tick stream. This is what makes a
        "second by second" read possible at all: the sub-minute bars, the tape
        velocity and the spread reading are all aggregations of this frame.

        Returns an empty frame rather than raising when the broker serves no
        tick history. Sub-minute detail is a refinement -- an account that
        cannot supply it must still run everything else normally.
        """
        self.connect()
        start = datetime.now(timezone.utc) - timedelta(seconds=max(seconds, 60))
        with self._lock:
            try:
                # COPY_TICKS_ALL includes both quote and trade ticks; Gold on
                # most retail feeds only ever produces the former.
                raw = self._mt5.copy_ticks_from(
                    self.symbol, start, 200000, self._mt5.COPY_TICKS_ALL)
            except Exception:
                log.debug("Tick history unavailable for %s", self.symbol,
                          exc_info=True)
                return _empty_ticks()
        if raw is None or len(raw) == 0:
            return _empty_ticks()

        df = pd.DataFrame(raw)
        # time_msc is milliseconds and is the only field fine enough to
        # separate ticks inside the same second, which is exactly the
        # resolution this frame exists to provide.
        if "time_msc" in df.columns:
            df["time"] = pd.to_datetime(df["time_msc"], unit="ms", utc=True)
        else:
            df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
        df = df.set_index("time").sort_index()
        for column in ("bid", "ask"):
            if column not in df.columns:
                return _empty_ticks()
        df["mid"] = (df["bid"].astype(float) + df["ask"].astype(float)) / 2
        df["spread"] = df["ask"].astype(float) - df["bid"].astype(float)
        return df[["bid", "ask", "mid", "spread"]].astype(float)

    def symbol_details(self) -> dict:
        self.connect()
        with self._lock:
            info = self._mt5.symbol_info(self.symbol)
        if info is None:
            raise DataUnavailable(f"Symbol {self.symbol} is not available.")
        return {
            "symbol": info.name,
            "description": info.description,
            "digits": info.digits,
            "point": info.point,
            "contract_size": info.trade_contract_size,
            "tick_value": info.trade_tick_value,
            "volume_min": info.volume_min,
            "volume_step": info.volume_step,
        }


def _empty_ticks() -> pd.DataFrame:
    """The shape callers expect when no tick history is available."""
    return pd.DataFrame(columns=["bid", "ask", "mid", "spread"],
                        index=pd.DatetimeIndex([], tz="UTC", name="time"),
                        dtype=float)


def _frame_from_rates(rates) -> pd.DataFrame:
    """Convert an MT5 rates array into the canonical OHLC frame."""
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df = df.set_index("time")
    df["volume"] = df.get("real_volume", 0)
    # Gold rarely carries real exchange volume; tick volume is the usable proxy.
    if "tick_volume" in df.columns:
        df["volume"] = df["volume"].where(df["volume"] > 0, df["tick_volume"])
    if "spread" not in df.columns:
        df["spread"] = 0
    return df[OHLC_COLUMNS].astype(float)
