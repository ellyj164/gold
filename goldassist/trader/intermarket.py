"""What else is moving, and whether Gold's move is corroborated by it.

:mod:`goldassist.trader.macro` answers "what is the dollar doing". That is the
single most useful piece of context, and for a long time it was the only one.
It is not enough on its own, because the dollar cannot distinguish the two
regimes that matter most to a Gold trader:

* **Monetary Gold** -- moving on rates and real yields. Silver keeps up or
  leads, the gold/silver ratio falls, and the move has industrial demand and
  liquidity behind it. These moves trend.
* **Fear Gold** -- moving on risk aversion. Silver lags badly, the ratio
  *rises*, equities fall, and Gold is being bought as insurance. These moves
  are sharp and they retrace hard when the scare passes.

Both look identical on a Gold chart, and a top-down method that cannot tell
them apart will hold a fear rally expecting a trend.

Three readings, each optional and each silent when its instrument is absent:

**Gold/silver ratio**
    The cleanest single regime tell available from a retail terminal. Falling
    ratio = broad precious-metals bid = trend. Rising ratio while Gold rises =
    haven bid only = fade risk.

**Yields**
    Gold pays no coupon, so it competes with government debt: yields up is a
    headwind, yields down a tailwind. Real yields are the true driver and no
    retail broker carries TIPS, so a nominal 10-year proxy is used where the
    broker has one, and the module says so rather than implying otherwise.

**Risk appetite**
    An equity index, read only for the divergence case: Gold and stocks rising
    together is liquidity, Gold rising while stocks fall is fear.

Every instrument here is *secondary*. A missing one costs a reading and
nothing else -- there is no fallback that invents a number, and the absent
ones are named so the interface can say what it could not see.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import numpy as np

log = logging.getLogger(__name__)

# Broker naming is chaotic; `find_symbol` also substring-matches, which is what
# resolves the `m` suffix this account uses (XAGUSDm, US500m).
SILVER_CANDIDATES = ("XAGUSD", "SILVER", "XAGUSD.", "AGUSD")
YIELD_CANDIDATES = ("US10Y", "USTNOTE", "TNX", "US10YT", "UST10Y", "USB10YUSD",
                    "US10YR", "TNOTE")
EQUITY_CANDIDATES = ("US500", "SPX500", "SP500", "SPX", "US30", "USTEC", "NAS100")

# Context does not need re-reading on a five-second cycle; these are daily
# instruments and the readings are daily.
CACHE_MINUTES = 10

# Daily candles pulled per instrument. Enough for a 20-day correlation and a
# month of ratio history, and small enough to cost nothing.
HISTORY_DAYS = 60

# The gold/silver ratio moves slowly. A day's change is noise; five days is
# where a regime shift becomes visible.
RATIO_WINDOW = 5

# How large a five-day ratio move has to be before it means anything, in
# percent. Below this the ratio is drifting, not telling you something.
RATIO_SIGNIFICANT = 1.5

# Yield move over five days, in percent of the index, that counts as a real
# push rather than chop.
YIELD_SIGNIFICANT = 3.0


@dataclass
class Reading:
    """One context instrument, resolved or not."""
    name: str
    available: bool
    symbol: str | None = None
    value: float | None = None
    change_5d_pct: float | None = None
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "available": self.available,
            "symbol": self.symbol,
            "value": round(self.value, 4) if self.value is not None else None,
            "change_5d_pct": (round(self.change_5d_pct, 2)
                              if self.change_5d_pct is not None else None),
            "note": self.note,
        }


@dataclass
class IntermarketRead:
    available: bool
    regime: str = "unknown"            # monetary | fear | liquidity | mixed | unknown
    headline: str = ""
    supports: str = "neutral"          # bullish | bearish | neutral | conflicted
    readings: list[Reading] = field(default_factory=list)
    cautions: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "available": self.available,
            "regime": self.regime,
            "headline": self.headline,
            "supports": self.supports,
            "readings": [r.to_dict() for r in self.readings],
            "cautions": self.cautions,
            "missing": self.missing,
        }


class IntermarketContext:
    """Reads and caches the instruments Gold is priced against."""

    def __init__(self, source):
        self.source = source
        self._symbols: dict[str, str | None] = {}
        self._resolved = False
        self._cached: IntermarketRead | None = None
        self._cached_at: datetime | None = None

    def _resolve(self) -> None:
        """Find each context symbol once and remember the answer, including
        the absences -- a broker that has no bond symbol will never grow one
        mid-session, and retrying costs a terminal round-trip per cycle."""
        self._resolved = True
        finder = getattr(self.source, "find_symbol", None)
        if finder is None:
            return
        for key, candidates in (("silver", SILVER_CANDIDATES),
                                ("yield", YIELD_CANDIDATES),
                                ("equity", EQUITY_CANDIDATES)):
            try:
                name = finder(candidates)
            except Exception:
                log.warning("Could not resolve a %s symbol", key, exc_info=True)
                name = None
            self._symbols[key] = name
            if name:
                log.info("Intermarket %s context using %s", key, name)
            else:
                log.info("Intermarket %s context unavailable on this broker", key)

    def read(self, gold_daily) -> IntermarketRead:
        now = datetime.now(timezone.utc)
        if (self._cached is not None and self._cached_at is not None
                and now - self._cached_at < timedelta(minutes=CACHE_MINUTES)):
            return self._cached
        if not self._resolved:
            self._resolve()
        read = self._measure(gold_daily)
        self._cached, self._cached_at = read, now
        return read

    def _closes(self, key: str) -> np.ndarray | None:
        symbol = self._symbols.get(key)
        if not symbol:
            return None
        try:
            df = self.source.candles_for(symbol, "D1", HISTORY_DAYS)
        except Exception as exc:
            log.debug("%s candles unavailable: %s", symbol, exc)
            return None
        if df is None or len(df) < RATIO_WINDOW + 2:
            return None
        return df["close"].to_numpy(dtype=float)

    def _measure(self, gold_daily) -> IntermarketRead:
        readings: list[Reading] = []
        missing: list[str] = []
        cautions: list[str] = []

        gold = (gold_daily["close"].to_numpy(dtype=float)
                if gold_daily is not None and len(gold_daily) > RATIO_WINDOW else None)

        ratio_change = _ratio_reading(self._closes("silver"), gold,
                                      self._symbols.get("silver"), readings, missing)
        yield_change = _yield_reading(self._closes("yield"),
                                      self._symbols.get("yield"), readings, missing)
        equity_change = _equity_reading(self._closes("equity"),
                                        self._symbols.get("equity"), readings, missing)

        if not any(r.available for r in readings):
            return IntermarketRead(
                available=False,
                headline="No intermarket context available from this broker.",
                readings=readings,
                missing=missing,
                cautions=["Add silver, a bond proxy or an equity index to "
                          "MetaTrader's Market Watch to enable this."],
            )

        gold_change = None
        if gold is not None:
            gold_change = (gold[-1] / gold[-1 - RATIO_WINDOW] - 1) * 100

        regime, headline, supports, more = _interpret(
            gold_change, ratio_change, yield_change, equity_change)
        cautions.extend(more)

        return IntermarketRead(
            available=True,
            regime=regime,
            headline=headline,
            supports=supports,
            readings=readings,
            cautions=cautions,
            missing=missing,
        )


def _pct_change(closes: np.ndarray, window: int = RATIO_WINDOW) -> float | None:
    if closes is None or len(closes) <= window:
        return None
    previous = closes[-1 - window]
    if previous == 0:
        return None
    return float((closes[-1] / previous - 1) * 100)


def _ratio_reading(silver, gold, symbol, readings, missing) -> float | None:
    """Gold priced in silver, and which way that is moving."""
    if silver is None or gold is None:
        missing.append("gold/silver ratio")
        readings.append(Reading("gold/silver ratio", False, symbol,
                                note="Silver unavailable, so the metals-regime "
                                     "read is not possible."))
        return None

    span = min(len(silver), len(gold))
    silver, gold = silver[-span:], gold[-span:]
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = np.divide(gold, silver, out=np.full(span, np.nan), where=silver != 0)
    ratio = ratio[np.isfinite(ratio)]
    if len(ratio) <= RATIO_WINDOW:
        missing.append("gold/silver ratio")
        readings.append(Reading("gold/silver ratio", False, symbol,
                                note="Not enough overlapping history."))
        return None

    change = _pct_change(ratio)
    current = float(ratio[-1])
    if change is None:
        note = f"ratio {current:.1f}"
    elif change <= -RATIO_SIGNIFICANT:
        note = (f"ratio {current:.1f}, down {abs(change):.1f}% in {RATIO_WINDOW} days -- "
                "silver outpacing Gold, a broad metals bid")
    elif change >= RATIO_SIGNIFICANT:
        note = (f"ratio {current:.1f}, up {change:.1f}% in {RATIO_WINDOW} days -- "
                "Gold outpacing silver, a haven bid rather than a metals one")
    else:
        note = f"ratio {current:.1f}, flat over {RATIO_WINDOW} days"

    readings.append(Reading("gold/silver ratio", True, symbol, current, change, note))
    return change


def _yield_reading(closes, symbol, readings, missing) -> float | None:
    """A nominal yield proxy, named honestly as a proxy."""
    if closes is None:
        missing.append("yields")
        readings.append(Reading(
            "yields", False, symbol,
            note="No bond or yield instrument on this broker. Real yields are "
                 "Gold's dominant driver and this read is therefore blind to it."))
        return None

    change = _pct_change(closes)
    current = float(closes[-1])
    if change is None:
        note = f"{current:.3f}"
    elif change >= YIELD_SIGNIFICANT:
        note = (f"{current:.3f}, up {change:.1f}% in {RATIO_WINDOW} days -- "
                "a rising-yield headwind for Gold")
    elif change <= -YIELD_SIGNIFICANT:
        note = (f"{current:.3f}, down {abs(change):.1f}% in {RATIO_WINDOW} days -- "
                "a falling-yield tailwind for Gold")
    else:
        note = f"{current:.3f}, little changed over {RATIO_WINDOW} days"
    note += " (nominal proxy; real yields are not available from a retail feed)"

    readings.append(Reading("yields", True, symbol, current, change, note))
    return change


def _equity_reading(closes, symbol, readings, missing) -> float | None:
    if closes is None:
        missing.append("risk appetite")
        readings.append(Reading("risk appetite", False, symbol,
                                note="No equity index available."))
        return None

    change = _pct_change(closes)
    current = float(closes[-1])
    note = (f"{current:.1f}, {change:+.1f}% over {RATIO_WINDOW} days"
            if change is not None else f"{current:.1f}")
    readings.append(Reading("risk appetite", True, symbol, current, change, note))
    return change


def _interpret(gold, ratio, yields, equity) -> tuple[str, str, str, list[str]]:
    """Name the regime, in the words a trader would use.

    Deliberately conservative: with only some of the instruments present the
    answer is "mixed", not a confident regime built from one reading.
    """
    cautions: list[str] = []
    rising = gold is not None and gold > 0.3
    falling = gold is not None and gold < -0.3

    # The headline case: what is silver saying about a Gold move?
    if rising and ratio is not None:
        if ratio <= -RATIO_SIGNIFICANT:
            regime = "monetary"
            headline = ("Gold is rising with silver leading -- a broad precious-metals "
                        "bid, the kind of move that trends.")
            supports = "bullish"
        elif ratio >= RATIO_SIGNIFICANT:
            regime = "fear"
            headline = ("Gold is rising while silver lags badly -- a haven bid, not a "
                        "metals bid. These moves retrace hard when the scare passes.")
            supports = "conflicted"
            cautions.append("Haven-driven rally: treat upside targets as less durable "
                            "and be quicker to bank at the first objective.")
        else:
            regime = "mixed"
            headline = "Gold is rising and the metals ratio is flat -- no regime signal."
            supports = "neutral"
    elif falling and ratio is not None:
        if ratio >= RATIO_SIGNIFICANT:
            regime = "monetary"
            headline = ("Gold is falling faster than silver is -- broad metals selling "
                        "rather than a Gold-specific move.")
            supports = "bearish"
        else:
            regime = "mixed"
            headline = "Gold is falling with silver holding up -- a Gold-specific move."
            supports = "neutral"
    else:
        regime = "mixed"
        headline = "No decisive Gold move to corroborate."
        supports = "neutral"

    # Yields modify rather than lead: they can veto a bullish read.
    if yields is not None:
        if yields >= YIELD_SIGNIFICANT and supports == "bullish":
            supports = "conflicted"
            cautions.append("Yields are rising into this Gold rally -- the usual "
                            "relationship is inverted, which tends not to last.")
        elif yields >= YIELD_SIGNIFICANT:
            headline += " Yields are rising, a headwind."
        elif yields <= -YIELD_SIGNIFICANT:
            headline += " Yields are falling, a tailwind."

    # Equities only speak to the fear case.
    if equity is not None and rising and equity < -1.0:
        cautions.append("Equities are falling while Gold rises -- risk-off. Confirms a "
                        "haven bid rather than a monetary one.")
        if regime == "mixed":
            regime = "fear"
    elif equity is not None and rising and equity > 1.0:
        if regime == "mixed":
            regime = "liquidity"
        headline += " Equities are rising too -- liquidity, not fear."

    return regime, headline, supports, cautions
