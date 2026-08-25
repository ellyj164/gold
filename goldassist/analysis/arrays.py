"""Dealing ranges, premium and discount, and breaker blocks.

Two ideas that the order-block and zone engines cannot express, both of which
answer the same question from different directions: *is this a good price, or
merely a good pattern?*

**The dealing range and its equilibrium.** Take the swing high and swing low
that bound the move price is currently working inside. The midpoint is
equilibrium; above it is premium, below it is discount. The discipline is
blunt and it is the one most retail traders break: buy in discount, sell in
premium, and treat a long taken at a premium as a worse version of the same
idea however good the candle looked. The application had no way to say this --
a textbook bullish order block sitting at 90% of the range scored exactly as
well as one sitting at 20%.

Inside that frame sits the **optimal trade entry**: the 62-79% retracement
band of the leg, where a pullback is deep enough to be a discount and shallow
enough that the leg is probably still alive.

**Breaker blocks.** An order block is a level that *held*. A breaker is one
that *failed* -- and failure is information the original engine throws away.
The sequence for a bullish breaker is: price makes a low, rallies to a high,
then makes a lower low that sweeps the first (taking the stops beneath it),
and then rallies back through that intervening high. The candles of the failed
rally, which were resistance on the way down, become support on the way back
up. It is the same price acting with the opposite sign, and it is one of the
more reliable structures in the vocabulary precisely because it marks where
the previous move's participants were proved wrong.

Both read from what :mod:`structure` already measured. Neither invents a swing.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict

import pandas as pd

BULLISH = "bullish"
BEARISH = "bearish"

# The retracement band that counts as an optimal entry. The classical numbers
# are the 0.62 and 0.79 Fibonacci levels; the exact edges matter less than
# that the band sits in the deeper half of the leg.
OTE_LOW = 0.62
OTE_HIGH = 0.79

# How far past equilibrium price must be before the read is worth stating.
# Within this fraction of the range it is simply "at equilibrium", and saying
# anything stronger would be reading noise off a midpoint.
EQUILIBRIUM_BAND = 0.05

# A dealing range narrower than this many ATR is not a range, it is chop, and
# its midpoint means nothing.
MIN_RANGE_ATR = 1.5


@dataclass
class Breaker:
    direction: str
    top: float
    bottom: float
    formed: pd.Timestamp
    swept_level: float      # the high or low whose stops were taken
    broken_level: float     # the level whose break confirmed the breaker
    tested: bool = False

    @property
    def midpoint(self) -> float:
        return (self.top + self.bottom) / 2

    def to_dict(self) -> dict:
        d = asdict(self)
        d["formed"] = int(self.formed.timestamp())
        for key in ("top", "bottom", "swept_level", "broken_level"):
            d[key] = round(float(d[key]), 3)
        d["midpoint"] = round(self.midpoint, 3)
        return d


def dealing_range(swings: list[dict], price: float, atr: float) -> dict:
    """Where price sits inside the range its recent swings define.

    ``swings`` is :func:`structure.analyse`'s own output, so this adds a
    reading rather than a second opinion about where the swings are.
    """
    highs = [s for s in swings if s.get("kind") == "high"]
    lows = [s for s in swings if s.get("kind") == "low"]
    if not highs or not lows:
        return {"available": False, "note": "No completed swing range yet."}

    top = max(float(s["price"]) for s in highs[-4:])
    bottom = min(float(s["price"]) for s in lows[-4:])
    span = top - bottom
    if span <= 0 or (atr > 0 and span < atr * MIN_RANGE_ATR):
        return {"available": False,
                "note": "The recent swings are too tight to define a range."}

    position = (price - bottom) / span
    equilibrium = (top + bottom) / 2

    if position > 0.5 + EQUILIBRIUM_BAND:
        zone = "premium"
        meaning = ("price is in the premium half of its range -- a good place to "
                   "sell, an expensive place to buy")
        favours = BEARISH
    elif position < 0.5 - EQUILIBRIUM_BAND:
        zone = "discount"
        meaning = ("price is in the discount half of its range -- a good place to "
                   "buy, a cheap place to sell")
        favours = BULLISH
    else:
        zone = "equilibrium"
        meaning = "price is at equilibrium, which favours neither side"
        favours = "neutral"

    # The OTE band, measured from each end: a long wants the deep retracement
    # of an up-leg, a short the deep retracement of a down-leg.
    long_ote = (bottom + span * (1 - OTE_HIGH), bottom + span * (1 - OTE_LOW))
    short_ote = (bottom + span * OTE_LOW, bottom + span * OTE_HIGH)

    return {
        "available": True,
        "high": round(top, 3),
        "low": round(bottom, 3),
        "equilibrium": round(equilibrium, 3),
        "span": round(span, 3),
        "position": round(position, 3),
        "zone": zone,
        "favours": favours,
        "meaning": meaning,
        "ote_long": [round(long_ote[0], 3), round(long_ote[1], 3)],
        "ote_short": [round(short_ote[0], 3), round(short_ote[1], 3)],
        "in_ote_long": long_ote[0] <= price <= long_ote[1],
        "in_ote_short": short_ote[0] <= price <= short_ote[1],
    }


def detect_breakers(df: pd.DataFrame, swings: list[dict],
                    limit: int = 6) -> list[Breaker]:
    """Order blocks that failed and flipped sign.

    Walks the swing sequence looking for the sweep-then-break pattern. Reads
    swings as dictionaries so it consumes :func:`structure.analyse` output
    directly.
    """
    # Three swings is the whole pattern -- low, high, lower low -- so three is
    # the minimum, not four. Demanding four made the detector silent on every
    # series that contained exactly one breaker and nothing else.
    if df is None or len(df) < 20 or len(swings) < 3:
        return []

    found: list[Breaker] = []
    ordered = sorted(swings, key=lambda s: int(s["index"]))

    for i in range(len(ordered) - 2):
        first, middle, third = ordered[i], ordered[i + 1], ordered[i + 2]

        # Bullish breaker: low, high, *lower* low. The second low sweeps the
        # first; the intervening high is the level that must then break.
        if (first.get("kind") == "low" and middle.get("kind") == "high"
                and third.get("kind") == "low"
                and float(third["price"]) < float(first["price"])):
            breaker = _build(df, middle, third, BULLISH,
                             swept=float(first["price"]))
            if breaker is not None:
                found.append(breaker)

        # Bearish breaker: the mirror -- high, low, *higher* high.
        elif (first.get("kind") == "high" and middle.get("kind") == "low"
                and third.get("kind") == "high"
                and float(third["price"]) > float(first["price"])):
            breaker = _build(df, middle, third, BEARISH,
                             swept=float(first["price"]))
            if breaker is not None:
                found.append(breaker)

    return found[-limit:]


def _build(df: pd.DataFrame, pivot: dict, sweep: dict, direction: str,
           swept: float) -> Breaker | None:
    """Turn a confirmed sweep-and-break into a breaker, or return None.

    The breaker only exists once price has actually traded back through the
    pivot: until then this is a sweep that has not been confirmed, and
    publishing it would be the trendline-break mistake again -- a level drawn
    from a move that has not happened.
    """
    pivot_index = int(pivot["index"])
    sweep_index = int(sweep["index"])
    if sweep_index <= pivot_index or sweep_index >= len(df) - 1:
        return None

    pivot_price = float(pivot["price"])
    after = df.iloc[sweep_index + 1:]
    if after.empty:
        return None

    if direction == BULLISH:
        if float(after["high"].max()) <= pivot_price:
            return None            # the pivot high never broke: not yet a breaker
        leg = df.iloc[pivot_index:sweep_index + 1]
        # The candles that failed -- the up-closes of the rally that could not
        # hold -- are the block. Falling back to the leg's own body range
        # keeps a breaker rather than dropping it when every candle was down.
        failed = leg[leg["close"] > leg["open"]]
        source = failed if not failed.empty else leg
        top = float(source[["open", "close"]].max().max())
        bottom = float(source["low"].min())
    else:
        if float(after["low"].min()) >= pivot_price:
            return None
        leg = df.iloc[pivot_index:sweep_index + 1]
        failed = leg[leg["close"] < leg["open"]]
        source = failed if not failed.empty else leg
        top = float(source["high"].max())
        bottom = float(source[["open", "close"]].min().min())

    if top <= bottom:
        return None

    tested = bool(((after["low"] <= top) & (after["high"] >= bottom)).any())
    return Breaker(direction=direction, top=top, bottom=bottom,
                   formed=df.index[sweep_index], swept_level=swept,
                   broken_level=pivot_price, tested=tested)


def summarise(breakers: list[Breaker], price: float, atr: float) -> dict:
    """The nearest breaker on each side, and whether price is in one."""
    if not breakers:
        return {"available": False, "count": 0,
                "nearest_bullish": None, "nearest_bearish": None}

    reach = atr * 2 if atr > 0 else None

    def nearest(direction: str):
        candidates = [b for b in breakers if b.direction == direction]
        if not candidates:
            return None
        best = min(candidates, key=lambda b: abs(price - b.midpoint))
        if reach is not None and abs(price - best.midpoint) > reach * 2:
            return None
        entry = best.to_dict()
        entry["price_inside"] = best.bottom <= price <= best.top
        entry["distance"] = round(abs(price - best.midpoint), 3)
        return entry

    return {
        "available": True,
        "count": len(breakers),
        "nearest_bullish": nearest(BULLISH),
        "nearest_bearish": nearest(BEARISH),
    }
