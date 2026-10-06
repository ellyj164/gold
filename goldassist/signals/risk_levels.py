"""Invalidation and targets measured from the entry zone.

The decision engine reasons from spot price: "if I were in now, where is this
wrong?". A *suggestion* is different -- it proposes entering inside a zone that
may be some distance away, so its invalidation and targets have to be measured
from that zone instead.

Getting this wrong is not cosmetic. Reusing the spot-based invalidation for a
zone-based entry produced a stop that sat on the wrong side of the entry and a
reward/risk of 1:19 -- a number that would badly mislead a trader.
"""

from __future__ import annotations

from . import opportunity


def invalidation_for_zone(analysis: dict, direction: str, zone_low: float,
                          zone_high: float) -> dict:
    """The structural level that must hold, given entry inside the zone.

    For a buy it must sit strictly below the zone; for a sell, strictly above.
    Preference order: a swing beyond the zone, then the far edge of a
    supporting area, then a volatility-based fallback.
    """
    bullish = direction == "bullish"
    atr = float(analysis.get("atr") or 0) or float(analysis["price"]) * 0.002
    tfs = analysis.get("timeframes", {})
    # A small buffer so the stop is not exactly on the level everyone watches.
    buffer = atr * 0.25
    boundary = zone_low if bullish else zone_high

    candidates: list[dict] = []

    # Swing points beyond the zone.
    for tf in ("M15", "H1", "H4"):
        struct = (tfs.get(tf) or {}).get("structure") or {}
        for key, want_low in (("last_swing_low", True), ("last_swing_high", False)):
            swing = struct.get(key)
            if not swing:
                continue
            if bullish and want_low and swing["price"] < boundary:
                candidates.append({"price": swing["price"] - buffer,
                                   "basis": f"{tf} swing low", "type": "structure"})
            if not bullish and not want_low and swing["price"] > boundary:
                candidates.append({"price": swing["price"] + buffer,
                                   "basis": f"{tf} swing high", "type": "structure"})

    # The far edge of the demand/supply area the zone sits in.
    zone_key = "nearest_demand" if bullish else "nearest_supply"
    for tf in ("H1", "M15", "H4"):
        area = ((tfs.get(tf) or {}).get("zones") or {}).get(zone_key)
        if not area:
            continue
        edge = area["bottom"] if bullish else area["top"]
        if (bullish and edge < boundary) or (not bullish and edge > boundary):
            candidates.append({
                "price": edge - buffer if bullish else edge + buffer,
                "basis": f"far edge of the {tf} {'demand' if bullish else 'supply'} zone",
                "type": "zone",
            })

    # Support/resistance beyond the zone.
    wanted = "support" if bullish else "resistance"
    for lvl in (analysis.get("levels") or {}).get("levels", []):
        if lvl["kind"] != wanted or lvl["strength"] < 50:
            continue
        if (bullish and lvl["price"] < boundary) or (not bullish and lvl["price"] > boundary):
            candidates.append({
                "price": lvl["price"] - buffer if bullish else lvl["price"] + buffer,
                "basis": f"{lvl['timeframe']} {wanted} ({lvl['source']})",
                "type": "level",
            })

    if not candidates:
        fallback = boundary - atr * 1.2 if bullish else boundary + atr * 1.2
        return {"price": round(fallback, 3), "basis": "1.2x ATR beyond the entry zone",
                "type": "volatility",
                "description": _describe(direction, fallback, "1.2x ATR beyond the entry zone")}

    # The closest valid level keeps risk tightest while staying structural.
    best = min(candidates, key=lambda c: abs(boundary - c["price"]))

    # Never allow a stop so tight that normal noise takes it out.
    minimum = atr * 0.6
    if abs(boundary - best["price"]) < minimum:
        best = {"price": boundary - minimum if bullish else boundary + minimum,
                "basis": best["basis"] + ", widened to 0.6x ATR for noise",
                "type": best["type"]}

    best["price"] = round(best["price"], 3)
    best["description"] = _describe(direction, best["price"], best["basis"])
    return best


def targets_for_zone(analysis: dict, direction: str, entry: float,
                     invalidation: float, count: int = 3,
                     minimum_pips: float = 500.0,
                     extra: list[dict] | None = None) -> list[dict]:
    """Up to ``count`` objectives ahead of the entry, nearest first.

    Drawn from the same technical sources the specification lists: liquidity,
    support/resistance, supply/demand, Fibonacci extensions and session extremes.

    ``extra`` carries objectives the detecting strategy measured itself -- the
    opposite edge of a range, a measured move out of a double top, the VWAP a
    reversion is aiming at. They are treated exactly like any other candidate:
    they must sit ahead of the entry and clear the same minimum distance.
    """
    bullish = direction == "bullish"
    atr = float(analysis.get("atr") or 0) or entry * 0.002
    risk = abs(entry - invalidation) or atr * 0.6
    # An objective must be far enough to be worth the risk taken *and* to clear
    # the day-trading minimum. The pip floor is what makes a setup eligible at
    # all; it never limits how far the targets may run.
    minimum_distance = max(atr * 0.6, risk * 0.8,
                           opportunity.to_price(minimum_pips))

    found: list[dict] = []

    def consider(price: float, basis: str) -> None:
        ahead = price > entry if bullish else price < entry
        if ahead and abs(price - entry) >= minimum_distance:
            found.append({"price": float(price), "basis": basis,
                          "pips": round(opportunity.to_pips(price - entry), 1)})

    # Objectives the strategy itself measured come first: they are the reason
    # it took the trade, so they should not be outvoted by a generic level.
    for target in (extra or []):
        try:
            consider(float(target["price"]), str(target.get("basis", "strategy objective")))
        except (TypeError, ValueError, KeyError):
            continue

    # Untapped liquidity in front of price.
    wanted_side = "buyside" if bullish else "sellside"
    for pool in (analysis.get("liquidity") or {}).get("pools", []):
        if pool["side"] == wanted_side:
            consider(pool["price"], f"{pool['timeframe']} {pool['source'].replace('_', ' ')} liquidity")

    # Opposing levels.
    opposing = "resistance" if bullish else "support"
    for lvl in (analysis.get("levels") or {}).get("levels", []):
        if lvl["kind"] == opposing and lvl["strength"] >= 45:
            consider(lvl["price"], f"{lvl['timeframe']} {opposing} ({lvl['source']})")

    # The opposing supply/demand area.
    tfs = analysis.get("timeframes", {})
    opposing_zone = "nearest_supply" if bullish else "nearest_demand"
    for tf in ("H1", "H4"):
        area = ((tfs.get(tf) or {}).get("zones") or {}).get(opposing_zone)
        if area:
            consider(area["bottom"] if bullish else area["top"],
                     f"{tf} {'supply' if bullish else 'demand'} zone")

    # Fibonacci extensions.
    fib = (analysis.get("levels") or {}).get("fibonacci") or {}
    for ratio, value in (fib.get("extensions") or {}).items():
        consider(value, f"Fibonacci {ratio} extension")

    found.sort(key=lambda t: abs(t["price"] - entry))

    # Nothing structural clears the pip floor. Before giving up on the setup,
    # test whether the move is simply *open* -- no level of consequence stands
    # in front of it within the required distance. An unobstructed run is a
    # real objective and is how most 500-pip days on Gold actually pay; it is
    # labelled as a projection so nobody mistakes it for a measured level.
    if not found:
        projected = _projection(analysis, direction, entry, atr, minimum_distance)
        if projected:
            found = [projected]
        else:
            return _below_minimum(analysis, direction, entry, atr, count)

    # Keep them meaningfully apart so TP1/TP2/TP3 are distinct objectives.
    spread: list[dict] = []
    for candidate in found:
        if any(abs(candidate["price"] - kept["price"]) < atr * 0.7 for kept in spread):
            continue
        spread.append(candidate)
        if len(spread) == count:
            break
    return spread


def _projection(analysis: dict, direction: str, entry: float, atr: float,
                distance: float) -> dict | None:
    """An objective at ``distance`` when the path to it is genuinely clear.

    Two conditions, both necessary. The distance must be reachable inside a
    normal intraday session -- 2.5x the H1 ATR is about a day's work on Gold --
    and nothing of consequence may sit in the way. A projection through the
    middle of H4 resistance is not an objective, it is a wish.
    """
    if distance > atr * 2.5:
        return None

    bullish = direction == "bullish"
    price = entry + distance if bullish else entry - distance

    opposing = "resistance" if bullish else "support"
    for lvl in (analysis.get("levels") or {}).get("levels", []):
        if lvl["kind"] != opposing or lvl["strength"] < 55:
            continue
        blocking = entry < lvl["price"] < price if bullish else price < lvl["price"] < entry
        if blocking:
            return None

    tfs = analysis.get("timeframes", {})
    blocking_zone = "nearest_supply" if bullish else "nearest_demand"
    for tf in ("H1", "H4"):
        area = ((tfs.get(tf) or {}).get("zones") or {}).get(blocking_zone)
        if not area:
            continue
        edge = area["bottom"] if bullish else area["top"]
        if (entry < edge < price) if bullish else (price < edge < entry):
            return None

    return {
        "price": float(price),
        "basis": f"open projection ({distance / atr:.1f}x ATR, no level in the way)",
        "pips": round(opportunity.to_pips(price - entry), 1),
        "projected": True,
    }


def _describe(direction: str, price: float, basis: str) -> str:
    if direction == "bullish":
        return f"a confirmed close below {price:.2f} ({basis})"
    return f"a confirmed close above {price:.2f} ({basis})"


def _below_minimum(analysis: dict, direction: str, entry: float, atr: float,
                   count: int) -> list[dict]:
    """Best objectives available when none clears the day-trading minimum."""
    bullish = direction == "bullish"
    fallback: list[dict] = []

    def consider(price: float, basis: str) -> None:
        ahead = price > entry if bullish else price < entry
        if ahead and abs(price - entry) >= atr * 0.4:
            fallback.append({"price": float(price), "basis": basis,
                             "pips": round(opportunity.to_pips(price - entry), 1),
                             "below_minimum": True})

    wanted_side = "buyside" if bullish else "sellside"
    for pool in (analysis.get("liquidity") or {}).get("pools", []):
        if pool["side"] == wanted_side:
            consider(pool["price"],
                     f"{pool['timeframe']} {pool['source'].replace('_', ' ')} liquidity")
    opposing = "resistance" if bullish else "support"
    for lvl in (analysis.get("levels") or {}).get("levels", []):
        if lvl["kind"] == opposing:
            consider(lvl["price"], f"{lvl['timeframe']} {opposing} ({lvl['source']})")

    fallback.sort(key=lambda t: abs(t["price"] - entry))
    return fallback[:count]
