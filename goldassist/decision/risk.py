"""Risk-awareness module.

The application never sizes or places a trade. What it does provide is the
positional context a trader needs before deciding: where the setup would be
invalidated, what the realistic targets are, the resulting reward-to-risk
shape, and explicit warnings when the technical positioning is poor.
"""

from __future__ import annotations

# A level closer than this (in ATR) is where price already is, not somewhere
# it is travelling to.
MIN_TARGET_ATR = 0.75

# XAUUSD pip convention, mirrored from goldassist.signals.opportunity so the
# risk module has no dependency on the signal layer. 1 pip = 0.01 of price,
# so 500 pips is a 5.00 ($5) move.
PIP_SIZE = 0.01


def assess(analysis: dict, direction: str) -> dict:
    """Build the risk picture for a setup in ``direction``."""
    price = analysis["price"]
    atr_v = analysis["atr"] or 1.0
    tfs = analysis["timeframes"]

    invalidation = _invalidation(analysis, direction, price, atr_v)
    targets = _targets(analysis, direction, price)

    risk = abs(price - invalidation["price"]) if invalidation else atr_v * 1.5
    reward = abs(targets[0]["price"] - price) if targets else atr_v * 2
    rr = round(reward / risk, 2) if risk > 0 else None

    warnings = _warnings(analysis, direction, price, atr_v, rr)

    return {
        "direction": direction,
        "atr": round(atr_v, 3),
        "volatility": analysis["regime"].get("volatility", "normal"),
        "invalidation": invalidation,
        "targets": targets,
        "risk_distance": round(risk, 2),
        "reward_distance": round(reward, 2),
        "reward_risk": rr,
        "warnings": warnings,
        "acceptable_positioning": not any(w["severity"] == "high" for w in warnings),
    }


def _invalidation(analysis: dict, direction: str, price: float, atr_v: float) -> dict:
    """The level whose breach would disprove the setup.

    Preference order: the structural swing that must hold, then the far edge of
    the zone price is reacting from, then an ATR-based fallback.
    """
    tfs = analysis["timeframes"]
    candidates = []

    for tf in ("M15", "H1"):
        struct = (tfs.get(tf) or {}).get("structure") or {}
        swing = struct.get("last_swing_low" if direction == "bullish" else "last_swing_high")
        if swing:
            level = swing["price"]
            valid = level < price if direction == "bullish" else level > price
            if valid:
                candidates.append({
                    "price": round(level, 3),
                    "basis": f"{tf} swing {'low' if direction == 'bullish' else 'high'}",
                    "type": "structure",
                })

    zone_key = "nearest_demand" if direction == "bullish" else "nearest_supply"
    for tf in ("H1", "M15"):
        zone = ((tfs.get(tf) or {}).get("zones") or {}).get(zone_key)
        if zone:
            edge = zone["bottom"] if direction == "bullish" else zone["top"]
            valid = edge < price if direction == "bullish" else edge > price
            if valid:
                candidates.append({
                    "price": round(edge, 3),
                    "basis": f"far edge of the {tf} {'demand' if direction == 'bullish' else 'supply'} zone",
                    "type": "zone",
                })

    if not candidates:
        fallback = price - atr_v * 1.5 if direction == "bullish" else price + atr_v * 1.5
        return {"price": round(fallback, 3), "basis": "1.5x ATR from price", "type": "volatility"}

    # Closest valid invalidation keeps risk tightest while still being structural.
    best = min(candidates, key=lambda c: abs(price - c["price"]))
    best["distance"] = round(abs(price - best["price"]), 2)
    best["distance_atr"] = round(abs(price - best["price"]) / atr_v, 2)
    best["description"] = (
        f"a confirmed close below {best['price']:.2f} ({best['basis']})"
        if direction == "bullish"
        else f"a confirmed close above {best['price']:.2f} ({best['basis']})"
    )
    return best


def _targets(analysis: dict, direction: str, price: float) -> list[dict]:
    """Realistic objectives: untapped liquidity first, then opposing levels.

    Anything price is already sitting on is not an objective -- a "target"
    half an ATR away would produce a meaningless reward-to-risk figure, so
    candidates must be at least ``MIN_TARGET_ATR`` away to qualify.
    """
    atr_v = analysis["atr"] or 1.0
    min_distance = atr_v * MIN_TARGET_ATR
    targets: list[dict] = []

    for pool in analysis["liquidity"].get("pools", []):
        ahead = pool["price"] > price if direction == "bullish" else pool["price"] < price
        wanted_side = "buyside" if direction == "bullish" else "sellside"
        if ahead and pool["side"] == wanted_side and abs(pool["price"] - price) >= min_distance:
            targets.append({
                "price": pool["price"],
                "basis": f"{pool['timeframe']} {pool['source'].replace('_', ' ')} liquidity",
                "type": "liquidity",
                "strength": pool["strength"],
                "distance": round(abs(pool["price"] - price), 2),
            })

    for lvl in analysis["levels"].get("levels", []):
        ahead = lvl["price"] > price if direction == "bullish" else lvl["price"] < price
        if ahead and lvl["strength"] >= 50 and abs(lvl["price"] - price) >= min_distance:
            targets.append({
                "price": lvl["price"],
                "basis": f"{lvl['timeframe']} {lvl['kind']} ({lvl['source']})",
                "type": "level",
                "strength": lvl["strength"],
                "distance": round(abs(lvl["price"] - price), 2),
            })

    fib = analysis["levels"].get("fibonacci") or {}
    for ratio, value in (fib.get("extensions") or {}).items():
        ahead = value > price if direction == "bullish" else value < price
        if ahead and abs(value - price) >= min_distance:
            targets.append({
                "price": value,
                "basis": f"Fibonacci {ratio} extension",
                "type": "fibonacci",
                "strength": 45,
                "distance": round(abs(value - price), 2),
            })

    for target in targets:
        target["pips"] = round(abs(target["price"] - price) / PIP_SIZE, 1)
    targets.sort(key=lambda t: t["distance"])
    # De-duplicate targets sitting at effectively the same price.
    deduped: list[dict] = []
    for t in targets:
        if deduped and abs(deduped[-1]["price"] - t["price"]) < max(0.5, price * 0.0002):
            continue
        deduped.append(t)
    return deduped[:4]


def _warnings(analysis: dict, direction: str, price: float, atr_v: float,
              rr: float | None) -> list[dict]:
    warnings: list[dict] = []
    regime = analysis["regime"]

    if regime.get("volatility") == "high":
        warnings.append({
            "severity": "medium",
            "message": "Volatility is unusually high. Expect wider swings, deeper wicks, "
                       "and stops being reached before the intended move develops.",
        })

    level_key = "nearest_resistance" if direction == "bullish" else "nearest_support"
    level = analysis["levels"].get(level_key)
    if level:
        distance_atr = abs(level["price"] - price) / atr_v
        if distance_atr < 0.75 and level["strength"] >= 60:
            warnings.append({
                "severity": "high",
                "message": f"Price is only {distance_atr:.1f} ATR below a "
                           f"{level['strength']:.0f}-strength "
                           f"{'resistance' if direction == 'bullish' else 'support'} at "
                           f"{level['price']:.2f}. Immediate entry has unfavourable positioning.",
            })

    if rr is not None and rr < 1.0:
        warnings.append({
            "severity": "high",
            "message": f"Reward-to-risk is only {rr:.2f}:1 against the nearest objective - "
                       "the technical structure does not currently justify the risk.",
        })
    elif rr is not None and rr < 1.5:
        warnings.append({
            "severity": "medium",
            "message": f"Reward-to-risk of {rr:.2f}:1 is modest. A deeper retracement into the "
                       "zone would improve it.",
        })

    h1 = analysis["timeframes"].get("H1", {})
    zone_key = "nearest_demand" if direction == "bullish" else "nearest_supply"
    zone = (h1.get("zones") or {}).get(zone_key)
    if zone:
        distance_atr = min(abs(price - zone["top"]), abs(price - zone["bottom"])) / atr_v
        if distance_atr > 3:
            warnings.append({
                "severity": "medium",
                "message": f"Price is {distance_atr:.1f} ATR away from the zone this setup is "
                           "based on. Chasing here means entering late.",
            })

    event = (analysis.get("events") or {}).get("next_high_impact")
    if event and event.get("minutes_until") is not None and 0 <= event["minutes_until"] <= 60:
        warnings.append({
            "severity": "medium" if event["minutes_until"] > 15 else "high",
            "message": f"High-impact event ({event['title']}) in {event['minutes_until']} minutes. "
                       "Technical setups can experience abnormal volatility around releases.",
        })

    return warnings
