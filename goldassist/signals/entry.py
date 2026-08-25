"""Entry zone derivation.

The specification is explicit that the module should suggest a price *area* to
watch rather than pretend one exact price is correct. A zone is built by
overlapping the independent technical areas the analysis engines already found
-- demand/supply, order blocks, Fair Value Gaps, support/resistance and the
Fibonacci retracement band -- and keeping the region where the most of them
agree.

Nothing here invents a price. Every edge of every zone comes from a measured
area, and each contributing source is named so the AI layer (and the trader)
can see why the zone is where it is.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict


@dataclass
class ZoneSource:
    """One technical area that contributed to the entry zone."""

    kind: str          # demand | supply | order block | FVG | support | resistance | fibonacci
    timeframe: str
    low: float
    high: float
    quality: float     # 0-100, how good the engine judged this area
    label: str

    def to_dict(self) -> dict:
        d = asdict(self)
        d["low"] = round(self.low, 3)
        d["high"] = round(self.high, 3)
        d["quality"] = round(self.quality, 1)
        return d


@dataclass
class EntryZone:
    low: float
    high: float
    preferred: float          # the single best price inside the zone
    confluence_count: int     # how many independent areas overlap here
    sources: list[ZoneSource] = field(default_factory=list)
    distance_atr: float = 0.0
    price_inside: bool = False
    # Whether price traded into the zone during the candles just resolved,
    # even if it has since left. Spot alone answers "is price here now?"; a
    # setup arms on "did price reach the entry?", which is a different and
    # more useful question -- see signals.resolve.visited_zone.
    visited: bool = False

    def to_dict(self) -> dict:
        return {
            "low": round(self.low, 3),
            "high": round(self.high, 3),
            "preferred": round(self.preferred, 3),
            "confluence_count": self.confluence_count,
            "distance_atr": round(self.distance_atr, 2),
            "price_inside": self.price_inside,
            "visited": self.visited,
            "sources": [s.to_dict() for s in self.sources],
            "label": self.label(),
        }

    def label(self) -> str:
        return f"{self.low:.2f}-{self.high:.2f}"

    def rationale(self) -> str:
        """Plain-language reason the zone sits where it does."""
        if not self.sources:
            return "derived from current volatility around price"
        parts = [f"{s.timeframe} {s.kind}" for s in self.sources[:3]]
        joined = ", ".join(parts[:-1]) + (" and " + parts[-1] if len(parts) > 1 else parts[0])
        if self.confluence_count > 1:
            return f"{joined} overlap here"
        return f"the {joined}"


def build(analysis: dict, direction: str, max_reach_atr: float = 6.0) -> EntryZone | None:
    """Find the best area to enter from, for ``direction``.

    For a bullish setup that means the strongest confluence at or below price;
    for a bearish setup, at or above it. Areas further than ``max_reach_atr``
    from price are ignored -- they describe a different setup, not this one.
    """
    price = float(analysis["price"])
    atr = float(analysis.get("atr") or 0) or price * 0.002
    candidates = _candidates(analysis, direction, price, atr, max_reach_atr)
    if not candidates:
        return None

    best = _best_overlap(candidates)
    if best is None:
        return None

    low, high, members = best
    # A zone thinner than a fraction of ATR is not tradeable; widen it around
    # its midpoint so it reflects real execution tolerance.
    minimum_width = atr * 0.25
    if high - low < minimum_width:
        mid = (high + low) / 2
        low, high = mid - minimum_width / 2, mid + minimum_width / 2

    inside = low <= price <= high
    distance = 0.0 if inside else (min(abs(price - low), abs(price - high)) / atr)

    return EntryZone(
        low=low,
        high=high,
        preferred=_preferred_price(low, high, direction, members),
        confluence_count=len(members),
        sources=sorted(members, key=lambda s: -s.quality),
        distance_atr=distance,
        price_inside=inside,
    )


def explicit(low: float, high: float, direction: str, price: float, atr: float,
             preferred: float | None = None, sources=None) -> EntryZone:
    """A zone a strategy measured itself, rather than one derived by overlap.

    The overlap engine above answers "where do all the technical areas agree?".
    A strategy detector often knows something narrower and better: the order
    block it found, the swept level it wants reclaimed, the range edge. This
    wraps those bounds in the same object so the rest of the pipeline -- status,
    resolution, the interface -- cannot tell the difference.
    """
    low, high = float(min(low, high)), float(max(low, high))
    minimum_width = atr * 0.15
    if high - low < minimum_width:
        mid = (high + low) / 2
        low, high = mid - minimum_width / 2, mid + minimum_width / 2

    members = [
        ZoneSource(kind=s.kind, timeframe=s.timeframe, low=float(s.low),
                   high=float(s.high), quality=float(s.quality), label=s.label)
        for s in (sources or [])
    ]
    inside = low <= price <= high
    distance = 0.0 if inside else (min(abs(price - low), abs(price - high)) / atr)
    if preferred is None or not (low <= preferred <= high):
        preferred = _preferred_price(low, high, direction, members)

    return EntryZone(
        low=low,
        high=high,
        preferred=float(preferred),
        confluence_count=max(1, len(members)),
        sources=members,
        distance_atr=distance,
        price_inside=inside,
    )


def _candidates(analysis: dict, direction: str, price: float, atr: float,
                max_reach_atr: float) -> list[ZoneSource]:
    """Collect every technical area that could host an entry."""
    bullish = direction == "bullish"
    tfs = analysis.get("timeframes", {})
    out: list[ZoneSource] = []

    def usable(low: float, high: float) -> bool:
        """A buy area must not sit above price, a sell area not below it."""
        if bullish and low > price + atr * 0.35:
            return False
        if not bullish and high < price - atr * 0.35:
            return False
        edge = min(abs(price - low), abs(price - high))
        return edge <= atr * max_reach_atr

    # Supply / demand zones.
    zone_key = "nearest_demand" if bullish else "nearest_supply"
    for tf in ("H4", "H1", "M15"):
        z = (tfs.get(tf, {}).get("zones") or {}).get(zone_key)
        if z and usable(z["bottom"], z["top"]):
            out.append(ZoneSource(
                "demand" if bullish else "supply", tf, z["bottom"], z["top"],
                z["quality"],
                f"{tf} {'demand' if bullish else 'supply'}"
                + (" (fresh)" if z.get("fresh") else ""),
            ))

    # Order blocks and Fair Value Gaps.
    item_key = "nearest_bullish" if bullish else "nearest_bearish"
    for summary_key, kind in (("order_blocks", "order block"), ("fvg", "FVG")):
        for tf in ("H4", "H1", "M30", "M15", "M5"):
            item = (tfs.get(tf, {}).get(summary_key) or {}).get(item_key)
            if item and usable(item["bottom"], item["top"]):
                out.append(ZoneSource(kind, tf, item["bottom"], item["top"],
                                      item["quality"], f"{tf} {kind}"))

    # Support / resistance: a level is a line, so give it a tolerance band.
    levels = analysis.get("levels", {}).get("levels", [])
    wanted = "support" if bullish else "resistance"
    for lvl in levels:
        if lvl["kind"] != wanted or lvl["strength"] < 45:
            continue
        band = atr * 0.2
        if usable(lvl["price"] - band, lvl["price"] + band):
            out.append(ZoneSource(wanted, lvl["timeframe"], lvl["price"] - band,
                                  lvl["price"] + band, lvl["strength"],
                                  f"{lvl['timeframe']} {wanted} ({lvl['source']})"))

    # The Fibonacci optimal-entry band, when it points the right way.
    fib = analysis.get("levels", {}).get("fibonacci") or {}
    band = fib.get("optimal_entry_zone")
    if band and len(band) == 2 and fib.get("direction") == direction:
        if usable(band[0], band[1]):
            out.append(ZoneSource("fibonacci", "H4", band[0], band[1], 50.0,
                                  "H4 Fibonacci 0.618-0.786 band"))

    return out


def _best_overlap(candidates: list[ZoneSource]) -> tuple[float, float, list[ZoneSource]] | None:
    """Find the price band where the most (and best) areas agree.

    Every candidate edge is tested as a slice point; the slice covered by the
    largest number of areas wins, with total quality breaking ties.
    """
    if not candidates:
        return None

    edges = sorted({e for c in candidates for e in (c.low, c.high)})
    best_band = None
    best_key = (0, 0.0)

    for i in range(len(edges) - 1):
        low, high = edges[i], edges[i + 1]
        if high <= low:
            continue
        mid = (low + high) / 2
        members = [c for c in candidates if c.low <= mid <= c.high]
        if not members:
            continue
        key = (len(members), sum(c.quality for c in members))
        if key > best_key:
            best_key = key
            best_band = (low, high, members)

    if best_band is not None:
        return best_band

    # No overlap at all: fall back to the single strongest area.
    best = max(candidates, key=lambda c: c.quality)
    return (best.low, best.high, [best])


def _preferred_price(low: float, high: float, direction: str,
                     members: list[ZoneSource]) -> float:
    """The single best fill inside the zone.

    Biased toward the far edge -- the deeper side of a demand zone for a buy --
    because that is where risk is smallest relative to the invalidation level.
    """
    if not members:
        return (low + high) / 2
    return low + (high - low) * (0.35 if direction == "bullish" else 0.65)
