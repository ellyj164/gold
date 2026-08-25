"""Dollar context for Gold.

Gold does not move in a vacuum, and a trader with any experience has at least
a rough answer to "why is it moving?" before they take a position. The single
most useful piece of that context is the dollar: Gold is priced in it, and the
two normally move against each other.

This module reads a dollar index from the same MetaTrader terminal the Gold
data comes from, falling back to EURUSD inverted when the broker does not
carry an index symbol. It reports what the dollar is doing, whether Gold is
behaving normally against it, and -- most usefully -- when it is *not*.

The divergence cases are the interesting ones. Gold rising while the dollar
also rises is a different market from Gold rising on dollar weakness: the move
is real, but the usual tailwind is missing, and it tends to be less durable.
A trader notices that. Until now this application could not.

When no dollar symbol is available the module says so and contributes nothing.
It never fabricates a reading.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import numpy as np

log = logging.getLogger(__name__)

# Broker naming for a dollar index varies wildly; EURUSD is the universal
# fallback and is inverted to behave like one.
INDEX_CANDIDATES = ("USDX", "DXY", "USDIDX", "USDOLLAR", "DX", "USIDX", "US_DOLLAR_INDEX")
FALLBACK_PAIR = "EURUSD"

# The dollar does not need re-reading every five seconds.
CACHE_MINUTES = 5


@dataclass
class MacroRead:
    available: bool
    symbol: str | None = None
    inverted: bool = False
    dollar_change_pct: float | None = None
    dollar_5d_pct: float | None = None
    gold_change_pct: float | None = None
    correlation: float | None = None
    relationship: str = ""
    headline: str = ""
    supports: str = "neutral"          # bullish | bearish | neutral | conflicted
    note: str = ""
    cautions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "available": self.available,
            "symbol": self.symbol,
            "inverted": self.inverted,
            "dollar_change_pct": (round(self.dollar_change_pct, 2)
                                  if self.dollar_change_pct is not None else None),
            "dollar_5d_pct": (round(self.dollar_5d_pct, 2)
                              if self.dollar_5d_pct is not None else None),
            "gold_change_pct": (round(self.gold_change_pct, 2)
                                if self.gold_change_pct is not None else None),
            "correlation": (round(self.correlation, 2)
                            if self.correlation is not None else None),
            "relationship": self.relationship,
            "headline": self.headline,
            "supports": self.supports,
            "note": self.note,
            "cautions": self.cautions,
        }


class DollarContext:
    """Reads and caches the dollar picture from the MetaTrader terminal."""

    def __init__(self, source):
        self.source = source
        self._symbol: str | None = None
        self._inverted = False
        self._resolved = False
        self._cached: MacroRead | None = None
        self._cached_at: datetime | None = None

    def _resolve(self) -> None:
        """Find a usable dollar symbol once, and remember the answer."""
        self._resolved = True
        finder = getattr(self.source, "find_symbol", None)
        if finder is None:
            return
        try:
            name = finder(INDEX_CANDIDATES)
            if name:
                self._symbol, self._inverted = name, False
                log.info("Dollar context using index symbol %s", name)
                return
            name = finder((FALLBACK_PAIR,))
            if name:
                self._symbol, self._inverted = name, True
                log.info("Dollar context using %s inverted (no index symbol)", name)
        except Exception:
            log.warning("Could not resolve a dollar symbol", exc_info=True)

    def read(self, gold_daily) -> MacroRead:
        now = datetime.now(timezone.utc)
        if (self._cached is not None and self._cached_at is not None
                and now - self._cached_at < timedelta(minutes=CACHE_MINUTES)):
            return self._cached

        if not self._resolved:
            self._resolve()

        read = self._measure(gold_daily)
        self._cached, self._cached_at = read, now
        return read

    def _measure(self, gold_daily) -> MacroRead:
        if not self._symbol:
            return MacroRead(
                available=False,
                headline="No dollar index available from this broker, so Gold is "
                         "being read on its own chart alone.",
                note="Add a dollar index or EURUSD to MetaTrader's Market Watch "
                     "to enable this.",
            )
        try:
            df = self.source.candles_for(self._symbol, "D1", 40)
        except Exception as exc:
            log.warning("Dollar candles unavailable: %s", exc)
            return MacroRead(available=False, symbol=self._symbol,
                             headline="Dollar data is temporarily unavailable.")

        if df is None or len(df) < 7:
            return MacroRead(available=False, symbol=self._symbol,
                             headline="Not enough dollar history to read context.")

        closes = df["close"].to_numpy(dtype=float)
        if self._inverted:
            closes = 1.0 / closes

        change = (closes[-1] / closes[-2] - 1) * 100
        five_day = (closes[-1] / closes[-6] - 1) * 100

        gold_change = correlation = None
        if gold_daily is not None and len(gold_daily) >= 7:
            gold_closes = gold_daily["close"].to_numpy(dtype=float)
            gold_change = (gold_closes[-1] / gold_closes[-2] - 1) * 100
            span = min(len(closes), len(gold_closes), 21)
            if span >= 8:
                dollar_returns = np.diff(closes[-span:]) / closes[-span:-1]
                gold_returns = np.diff(gold_closes[-span:]) / gold_closes[-span:-1]
                if dollar_returns.std() > 0 and gold_returns.std() > 0:
                    correlation = float(np.corrcoef(dollar_returns, gold_returns)[0, 1])

        relationship, headline, supports, cautions = _interpret(
            change, five_day, gold_change, correlation)

        return MacroRead(
            available=True,
            symbol=self._symbol,
            inverted=self._inverted,
            dollar_change_pct=change,
            dollar_5d_pct=five_day,
            gold_change_pct=gold_change,
            correlation=correlation,
            relationship=relationship,
            headline=headline,
            supports=supports,
            note=(f"Dollar proxied by {self._symbol} inverted."
                  if self._inverted else f"Dollar read from {self._symbol}."),
            cautions=cautions,
        )


def _interpret(dollar, five_day, gold, correlation):
    """Turn the numbers into the sentence a trader would actually say."""
    cautions: list[str] = []
    trend = ("stronger" if five_day > 0.3 else "weaker" if five_day < -0.3
             else "flat")
    base = (f"The dollar is {dollar:+.2f}% today and {five_day:+.2f}% over five "
            f"days ({trend} on the week).")

    if gold is None:
        return "unknown", base, "neutral", cautions

    # The threshold keeps a flat session from being read as a divergence.
    quiet = abs(dollar) < 0.15 and abs(gold) < 0.15
    if quiet:
        return ("quiet",
                base + " Neither market has done much today.",
                "neutral", cautions)

    dollar_up, gold_up = dollar > 0, gold > 0

    if not dollar_up and gold_up:
        return ("textbook inverse",
                base + f" Gold is {gold:+.2f}% on that dollar weakness — the "
                       "normal relationship, and it supports the upside.",
                "bullish", cautions)

    if dollar_up and not gold_up:
        return ("textbook inverse",
                base + f" Gold is {gold:+.2f}% under that dollar strength — the "
                       "normal relationship, and it supports the downside.",
                "bearish", cautions)

    if dollar_up and gold_up:
        cautions.append(
            "Gold is rising while the dollar rises. The move is real but the "
            "usual tailwind is absent, so it tends to be less durable than the "
            "chart alone suggests.")
        return ("divergent — both higher",
                base + f" Gold is {gold:+.2f}% *despite* that, which usually "
                       "means safe-haven or real-yield buying rather than "
                       "dollar weakness.",
                "conflicted", cautions)

    cautions.append(
        "Gold is falling while the dollar falls too. Gold-specific selling is "
        "in control, so dollar weakness is not going to rescue a long.")
    return ("divergent — both lower",
            base + f" Gold is {gold:+.2f}% anyway, which is a warning for anyone "
                   "buying purely on a soft dollar.",
            "conflicted", cautions)
