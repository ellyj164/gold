"""Volume profile: where the market actually traded, not merely where it went.

Every other location engine in this package reads price *shape* -- a swing, a
zone, an imbalance, a block. None of them read participation. Two candles with
identical highs and lows are identical to `structure`, `zones` and `fvg` even
when one of them carries eight times the activity of the other, and it is the
busy one that leaves a level price returns to.

This module reduces a window of candles to a histogram of volume by price and
reports the three readings a profile trader actually uses:

**POC** (point of control)
    The single busiest price in the window. The market's own idea of fair
    value. Price is drawn back to it far more often than to any swing level.

**Value area**
    The contiguous band around the POC holding ``VALUE_AREA`` of the volume,
    conventionally 70%. Its edges (VAH and VAL) are the boundaries the market
    itself defended, and they behave like support and resistance without
    anyone having drawn them.

**High and low volume nodes**
    HVNs are shelves price moves slowly through; LVNs are the gaps it crosses
    quickly. An LVN between spot and a target is a reason to expect the move
    to travel; an HVN in the way is a reason to expect it to stall.

Distribution over each candle's range is the standard approximation: MetaTrader
serves no intra-candle volume, so a candle's volume is spread evenly across the
price buckets its range touches. This is exact for a one-bucket candle and
converges as the timeframe falls, which is why the profile is read on the
finest timeframe with enough history rather than on the daily.

The volume is broker **tick volume** -- the number of price changes, not
contracts. For Gold on a retail feed it tracks real activity closely enough for
the *shape* of the distribution to be meaningful, which is all this module
claims. It reports the distinction rather than hiding it.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

# Share of total volume that defines the value area. 70% is the convention
# inherited from market profile, where it is one standard deviation of a
# normal distribution.
VALUE_AREA = 0.70

# Buckets the window is divided into. Enough that a node is narrower than a
# typical stop, few enough that each still holds meaningful volume: at Gold's
# usual daily range this puts a bucket at roughly 20-40 cents.
DEFAULT_BUCKETS = 80

# A node counts as high-volume above this multiple of the mean bucket, and as
# low-volume below the second. Both are read off the profile's own mean, so
# they scale with the window rather than needing a per-instrument constant.
HVN_RATIO = 1.5
LVN_RATIO = 0.4

# Candles the profile reads. Long enough to hold a real distribution, short
# enough that it describes the market being traded now rather than last month.
DEFAULT_LOOKBACK = 480


def build(df: pd.DataFrame, buckets: int = DEFAULT_BUCKETS,
          lookback: int = DEFAULT_LOOKBACK) -> dict | None:
    """Histogram a window of candles into a volume-by-price profile.

    Returns ``None`` when there is not enough data to say anything, which the
    caller must treat as "no profile" rather than as an empty one.
    """
    if df is None or len(df) < 30:
        return None

    window = df.iloc[-lookback:] if len(df) > lookback else df
    highs = window["high"].to_numpy(dtype=float)
    lows = window["low"].to_numpy(dtype=float)
    volumes = window["volume"].to_numpy(dtype=float)

    top, bottom = float(highs.max()), float(lows.min())
    if not np.isfinite(top) or not np.isfinite(bottom) or top <= bottom:
        return None

    # Volume can arrive as zeros on a broker that does not report it. A flat
    # profile is worse than none: it would put the POC at an arbitrary bucket
    # and every level built on it would be noise wearing a number.
    total_volume = float(volumes.sum())
    if total_volume <= 0:
        return None

    edges = np.linspace(bottom, top, buckets + 1)
    centres = (edges[:-1] + edges[1:]) / 2
    width = float(edges[1] - edges[0])
    histogram = np.zeros(buckets, dtype=float)

    # Spread each candle's volume evenly over the buckets its range covers.
    # `searchsorted` gives the first and last bucket the range touches; the
    # clip keeps a candle whose high equals the window top inside the array.
    first = np.clip(np.searchsorted(edges, lows, side="right") - 1, 0, buckets - 1)
    last = np.clip(np.searchsorted(edges, highs, side="right") - 1, 0, buckets - 1)
    for i in range(len(window)):
        lo, hi = first[i], last[i]
        spread = hi - lo + 1
        histogram[lo:hi + 1] += volumes[i] / spread

    poc_index = int(histogram.argmax())
    val_index, vah_index = _value_area(histogram, poc_index, total_volume)

    mean_bucket = float(histogram.mean())
    hvn = [round(float(centres[i]), 3) for i in _peaks(histogram, mean_bucket * HVN_RATIO)]
    lvn = [round(float(centres[i]), 3)
           for i in _valleys(histogram, mean_bucket * LVN_RATIO, mean_bucket)]

    return {
        "poc": round(float(centres[poc_index]), 3),
        "vah": round(float(centres[vah_index]), 3),
        "val": round(float(centres[val_index]), 3),
        "bucket_width": round(width, 4),
        "top": round(top, 3),
        "bottom": round(bottom, 3),
        "bars": int(len(window)),
        "total_volume": round(total_volume, 1),
        "value_area_volume_pct": round(
            float(histogram[val_index:vah_index + 1].sum()) / total_volume * 100, 1),
        "high_volume_nodes": hvn[:8],
        "low_volume_nodes": _thin(lvn, width * 2)[:8],
        # Named so no consumer can mistake tick counts for traded contracts.
        "volume_kind": "tick",
        "histogram": [
            {"price": round(float(centres[i]), 3),
             "volume": round(float(histogram[i]), 1)}
            for i in range(buckets)
        ],
    }


def _value_area(histogram: np.ndarray, poc_index: int,
                total_volume: float) -> tuple[int, int]:
    """Grow a band out from the POC until it holds ``VALUE_AREA`` of volume.

    The standard construction: at each step take whichever neighbouring bucket
    -- above or below -- is busier, so the band follows the distribution rather
    than expanding symmetrically through a thin side.
    """
    target = total_volume * VALUE_AREA
    low = high = poc_index
    captured = float(histogram[poc_index])
    limit = len(histogram) - 1

    while captured < target and (low > 0 or high < limit):
        below = float(histogram[low - 1]) if low > 0 else -1.0
        above = float(histogram[high + 1]) if high < limit else -1.0
        if above >= below:
            high += 1
            captured += above
        else:
            low -= 1
            captured += below
    return low, high


def _peaks(histogram: np.ndarray, threshold: float) -> list[int]:
    """Local maxima above ``threshold``, busiest first.

    Only genuine peaks qualify: a bucket on the flank of the POC can clear the
    threshold while describing no shelf of its own.
    """
    found = []
    for i in range(1, len(histogram) - 1):
        if (histogram[i] >= threshold
                and histogram[i] >= histogram[i - 1]
                and histogram[i] >= histogram[i + 1]):
            found.append(i)
    return sorted(found, key=lambda i: -histogram[i])


def _valleys(histogram: np.ndarray, threshold: float, mean_bucket: float) -> list[int]:
    """Thin buckets with genuine trade on *both* sides of them.

    The distinction that makes an LVN tradeable: a bucket at the edge of the
    distribution is thin because the market never went there, and price
    reaching it is news. A bucket with busy prices above *and* below is thin
    because the market went through it and refused to stay -- that is the gap
    price crosses quickly, and the only one worth calling a low-volume node.

    Without this the tails of every profile are reported as LVNs, and the two
    have opposite meanings for a target sitting beyond them.
    """
    found = []
    shelf = mean_bucket * 0.8
    for i in range(1, len(histogram) - 1):
        if histogram[i] <= 0 or histogram[i] >= threshold:
            continue
        if histogram[:i].max(initial=0.0) >= shelf and histogram[i + 1:].max(initial=0.0) >= shelf:
            found.append(i)
    return found


def _thin(prices: list[float], min_gap: float) -> list[float]:
    """Collapse runs of adjacent prices to one representative each.

    Low-volume buckets arrive in contiguous stretches; eight *adjacent* ones
    describe a single gap, not eight of them.
    """
    out: list[float] = []
    for price in sorted(prices):
        if not out or price - out[-1] >= min_gap:
            out.append(price)
    return out


def summarise(profile: dict | None, price: float, atr: float) -> dict:
    """Read the profile from where price actually is.

    This is the part the confluence engine consumes: not the histogram, but
    what standing at ``price`` means relative to it.
    """
    if not profile:
        return {"available": False, "note": "No volume profile: insufficient or "
                                            "unreported volume for this timeframe."}

    poc, vah, val = profile["poc"], profile["vah"], profile["val"]
    reach = atr if atr > 0 else profile["bucket_width"] * 4

    if price > vah:
        location = "above value"
        meaning = ("trading above the value area -- accepted higher, or "
                   "extended and liable to be drawn back into it")
    elif price < val:
        location = "below value"
        meaning = ("trading below the value area -- accepted lower, or "
                   "extended and liable to be drawn back up into it")
    else:
        location = "inside value"
        meaning = "inside the value area, where the market considers price fair"

    distance_to_poc = abs(price - poc)
    at_poc = distance_to_poc <= reach * 0.35
    nearest_lvn = _nearest(profile["low_volume_nodes"], price)
    nearest_hvn = _nearest(profile["high_volume_nodes"], price)

    return {
        "available": True,
        "volume_kind": profile["volume_kind"],
        "poc": poc,
        "vah": vah,
        "val": val,
        "location": location,
        "meaning": meaning,
        "at_poc": at_poc,
        "distance_to_poc": round(distance_to_poc, 3),
        "distance_to_poc_atr": round(distance_to_poc / reach, 2) if reach else None,
        # An edge is only tradeable while price is near it.
        "at_value_edge": (abs(price - vah) <= reach * 0.35
                          or abs(price - val) <= reach * 0.35),
        "nearest_high_volume_node": nearest_hvn,
        "nearest_low_volume_node": nearest_lvn,
        "value_area_volume_pct": profile["value_area_volume_pct"],
        "bars": profile["bars"],
    }


def _nearest(prices: list[float], price: float) -> float | None:
    return min(prices, key=lambda p: abs(p - price)) if prices else None


def path_is_clear(profile: dict | None, start: float, target: float) -> dict:
    """Whether a move from ``start`` to ``target`` runs through a shelf.

    The practical question a profile answers that no other engine here can:
    *is there anything in the way?* An HVN between price and its objective is
    where a move stalls, and a target set beyond one is a target that needs the
    market to grind through the busiest price in the window to reach it.
    """
    if not profile:
        return {"available": False}

    lo, hi = (start, target) if target > start else (target, start)
    blocking = [p for p in profile["high_volume_nodes"] if lo < p < hi]
    assisting = [p for p in profile["low_volume_nodes"] if lo < p < hi]
    poc_between = lo < profile["poc"] < hi

    if poc_between:
        verdict, note = "blocked", ("the point of control sits between price and "
                                    "the target -- the busiest price in the window "
                                    "has to be traded through")
    elif blocking:
        verdict, note = "congested", (f"{len(blocking)} high-volume shelf/shelves in "
                                      f"the way, nearest at {min(blocking, key=lambda p: abs(p - start))}")
    elif assisting:
        verdict, note = "clear", ("thin volume between price and the target -- the "
                                  "kind of gap price crosses quickly")
    else:
        verdict, note = "neutral", "no notable volume structure between the two"

    return {
        "available": True,
        "verdict": verdict,
        "note": note,
        "blocking_nodes": blocking,
        "thin_nodes": assisting,
        "poc_between": poc_between,
    }
