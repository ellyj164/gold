"""Confluence engine.

Combines every piece of technical evidence into one setup score per direction.

The scoring rule that matters: **no single factor can produce a setup**. The
catalogue below tops out at 100 points, and the largest single contribution is
18, so a tradeable score requires several independent forms of agreement --
higher-timeframe direction, location, a liquidity event, structural
confirmation, and supporting momentum.

Penalties are applied separately and can veto an otherwise attractive score,
which is how "everything is bullish but price sits under major resistance"
correctly becomes WAIT rather than BUY.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict

from ..config import EXECUTION_TIMEFRAMES
from .profile import StrategyProfile

# The catalogue mirrors the six forms of evidence the specification names as
# the ingredients of a high-quality setup: higher-timeframe direction, price
# location, a liquidity event, structural confirmation, price-action
# confirmation, and momentum.
#
# "Location" is deliberately one pillar rather than three. Supply/demand zones,
# order blocks and Fair Value Gaps are three ways of measuring the same thing —
# that price has arrived somewhere significant — and scoring them separately
# meant a genuinely good setup only ever collected a fraction of the available
# points. Measured over ten months of Gold, the split version could not reach
# 70/100 even once; the pillar version scores what a trader would actually call
# a strong location.
#
# name -> (max points, human label, required-condition keys it can satisfy)
CATALOGUE = {
    "htf_alignment":     (18, "Higher-timeframe direction", ("higher_timeframe_alignment",)),
    "h1_structure":      (12, "Trading-timeframe structure", ()),
    "location":          (20, "Price location", ("zone_location", "fvg_or_orderblock",
                                                 "value_area_location")),
    "liquidity_sweep":   (14, "Liquidity sweep", ("liquidity_event",)),
    "structure_confirm": (18, "Structure confirmation", ("structure_confirmation",)),
    "candles":           (8,  "Price-action confirmation", ("candle_confirmation",)),
    "momentum":          (10, "Momentum & trend indicators", ()),
}

MAX_SCORE = sum(v[0] for v in CATALOGUE.values())  # 100


@dataclass
class Evidence:
    factor: str
    label: str
    direction: str        # bullish | bearish
    points: float
    max_points: float
    detail: str
    conditions: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        d = asdict(self)
        d["points"] = round(self.points, 1)
        d["conditions"] = list(self.conditions)
        return d


@dataclass
class Penalty:
    factor: str
    label: str
    points: float         # positive number, subtracted from the score
    detail: str
    severity: str         # "warning" | "critical" (critical blocks entry)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["points"] = round(self.points, 1)
        return d


def evaluate(analysis: dict, profile: StrategyProfile) -> dict:
    """Score both directions and return the full evidence breakdown."""
    result = {}
    for direction in ("bullish", "bearish"):
        evidence = _gather(analysis, direction, profile)
        penalties = _penalties(analysis, direction, profile)
        raw = sum(e.points for e in evidence)
        deduction = sum(p.points for p in penalties)
        score = max(0.0, min(100.0, raw - deduction))
        satisfied = _conditions_met(evidence, profile)

        result[direction] = {
            "direction": direction,
            "score": round(score, 1),
            "raw_score": round(raw, 1),
            "deductions": round(deduction, 1),
            "evidence": [e.to_dict() for e in sorted(evidence, key=lambda x: -x.points)],
            "penalties": [p.to_dict() for p in sorted(penalties, key=lambda x: -x.points)],
            "conditions": satisfied,
            "missing_conditions": [c for c, ok in satisfied.items() if not ok],
            "has_critical_penalty": any(p.severity == "critical" for p in penalties),
        }

    winner = max(result.values(), key=lambda r: r["score"])
    loser = min(result.values(), key=lambda r: r["score"])
    result["leading"] = winner["direction"]
    result["separation"] = round(winner["score"] - loser["score"], 1)
    result["max_score"] = MAX_SCORE
    return result


# --- evidence gathering ----------------------------------------------------


def _gather(analysis: dict, direction: str, profile: StrategyProfile) -> list[Evidence]:
    tfs = analysis["timeframes"]
    evidence: list[Evidence] = []

    def add(factor: str, fraction: float, detail: str,
            conditions: tuple[str, ...] | None = None) -> None:
        """Award ``fraction`` (0..1) of a factor's maximum points."""
        if fraction <= 0:
            return
        cap, label, default_conditions = CATALOGUE[factor]
        weight = profile.weight_overrides.get(factor, 1.0)
        points = min(cap * weight, cap * weight * fraction)
        evidence.append(Evidence(factor, label, direction, points, cap * weight, detail,
                                 default_conditions if conditions is None else conditions))

    opposite = "bearish" if direction == "bullish" else "bullish"

    # 1. Higher-timeframe direction (D1 + H4 by default).
    bias_tfs = [tf for tf in profile.bias_timeframes if tf in tfs]
    agreeing = [tf for tf in bias_tfs if tfs[tf]["bias"].startswith(direction)]
    if agreeing:
        # Full credit only when every bias timeframe agrees cleanly.
        clean = [tf for tf in agreeing if "weakening" not in tfs[tf]["bias"]]
        fraction = len(agreeing) / len(bias_tfs) * (0.75 + 0.25 * (len(clean) / len(agreeing)))
        add("htf_alignment", fraction,
            f"{' and '.join(agreeing)} {direction}" + ("" if len(clean) == len(agreeing) else " (weakening)"))

    # 2. Structure on the trading timeframe.
    h1 = tfs.get("H1")
    if h1 and h1["structure"]["trend"] == direction:
        state = h1["structure"]["state"]
        # A retracement inside the trend is the ideal entry context; an
        # already-extended expansion leg is worth less.
        fraction = 1.0 if "retracement" in state else 0.6 if "expansion" in state else 0.85
        add("h1_structure", fraction, f"H1 {direction} structure - {state}")

    # 3. Location: has price arrived somewhere significant?
    #
    # Zones, order blocks and FVGs are three measurements of the same idea, so
    # they are scored as one pillar: the best component carries it, and a
    # second agreeing component adds a bounded confirmation bonus rather than
    # its own full score.
    components = _location_components(analysis, tfs, direction)
    if components:
        components.sort(key=lambda c: -c["fraction"])
        best = components[0]
        fraction = best["fraction"]
        if len(components) > 1:
            fraction = min(1.0, fraction + components[1]["fraction"] * 0.35)

        satisfied: list[str] = []
        for component in components[:2]:
            if component["fraction"] >= 0.4:
                satisfied.extend(component["conditions"])
        detail = best["detail"]
        if len(components) > 1 and components[1]["fraction"] >= 0.3:
            detail += "; also " + components[1]["detail"]
        add("location", fraction, detail, tuple(dict.fromkeys(satisfied)))

    # 4. Liquidity sweep in this direction.
    sweeps = analysis["liquidity"].get("sweeps", [])
    matching = [s for s in sweeps if s["direction"] == direction]
    if matching:
        best = max(matching, key=lambda s: s["strength"])
        # Recency matters: a sweep from an hour ago is stale on M5.
        fraction = best["strength"] / 100
        confirm = " and structure confirmed it" if best.get("confirmed_by_structure") else ""
        add("liquidity_sweep", fraction,
            f"{best['timeframe']} {'sell-side' if direction == 'bullish' else 'buy-side'} "
            f"sweep at {best['level']:.2f} ({best['wick_depth_atr']:.1f} ATR wick){confirm}")

    # 5. Structure confirmation on the confirmation timeframe.
    conf_tf = profile.confirmation_timeframe
    conf = tfs.get(conf_tf)
    if conf:
        last = conf["structure"].get("last_event")
        recent_events = conf["structure"].get("recent_events", [])
        matching_events = [e for e in recent_events if e["direction"] == direction]
        if matching_events:
            best = max(matching_events, key=lambda e: e["displacement"])
            weight = {"MSS": 1.0, "BOS": 0.9, "CHoCH": 0.75}.get(best["kind"], 0.6)
            weight *= min(1.0, 0.6 + best["displacement"] / 3)
            # Only the most recent event counts fully; older ones are partial.
            if last and last["time"] != best["time"]:
                weight *= 0.7
            add("structure_confirm", weight,
                f"{conf_tf} {best['kind']} {direction} at {best['price']:.2f} "
                f"({best['displacement']:.1f}x ATR)")

    # 5. Momentum and trend indicators, read on the trading timeframe.
    #
    # One pillar, because these are supporting evidence: the specification is
    # explicit that counting indicators must not drive the decision.
    if h1:
        ind = h1["indicators"]
        momentum_keys = ("rsi", "macd", "stochastic", "cci", "williams_r", "roc", "adx")
        trend_keys = ("ema", "supertrend", "vwap", "obv")
        agree = [k for k in momentum_keys if ind.get(k, {}).get("bias") == direction]
        against = [k for k in momentum_keys if ind.get(k, {}).get("bias") == opposite]
        trend_agree = [k for k in trend_keys if ind.get(k, {}).get("bias") == direction]

        momentum_net = max(0, len(agree) - len(against)) / len(momentum_keys)
        trend_share = len(trend_agree) / len(trend_keys)
        fraction = momentum_net * 0.65 + trend_share * 0.35
        if fraction > 0:
            names = (agree + trend_agree)[:4]
            add("momentum", fraction,
                str(len(agree)) + "/" + str(len(momentum_keys)) + " momentum and "
                + str(len(trend_agree)) + "/" + str(len(trend_keys))
                + " trend indicators " + direction
                + ((" (" + ", ".join(names) + ")") if names else ""))

    # 9. Candlestick confirmation, valued only when it forms at a level.
    for tf in (conf_tf, "M15"):
        candles = tfs.get(tf, {}).get("candles") or {}
        if candles.get("bias") != direction:
            continue
        # The summary's top pattern is the heaviest overall, which may be the
        # opposing one. Only a pattern in this direction is evidence for it.
        matching = [p for p in candles.get("patterns", []) if p["direction"] == direction]
        if not matching:
            continue
        top = max(matching, key=lambda p: p["weight"])
        fraction = min(1.0, abs(candles["score"]))
        if top.get("at_level"):
            fraction = min(1.0, fraction * 1.5)
        add("candles", fraction,
            f"{tf} {top['name']} - {top['note']}"
            + (" at a key level" if top.get("at_level") else ""))
        break

    return evidence


def _location_components(analysis: dict, tfs: dict, direction: str) -> list[dict]:
    """Score each way of measuring "price is somewhere significant".

    Returns one entry per kind found (zone, order block, FVG) with a 0..1
    fraction combining how close price is and how good the area is.
    """
    price = analysis["price"]
    atr_v = analysis["atr"] or 1.0
    out: list[dict] = []

    def score_area(area: dict, reach_atr: float) -> float:
        inside = area["bottom"] <= price <= area["top"]
        distance = 0.0 if inside else min(
            abs(price - area["top"]), abs(price - area["bottom"])
        ) / atr_v
        if not inside and distance > reach_atr:
            return 0.0
        proximity = 1.0 if inside else max(0.25, 1 - distance / reach_atr)
        return proximity * (area["quality"] / 100)

    # Supply/demand zone.
    zone_key = "nearest_demand" if direction == "bullish" else "nearest_supply"
    best_zone = None
    for tf in ("H4", "H1", "M15"):
        z = (tfs.get(tf, {}).get("zones") or {}).get(zone_key)
        if z and (best_zone is None or z["quality"] > best_zone["quality"]):
            best_zone = dict(z, timeframe=tf)
    if best_zone:
        fraction = score_area(best_zone, 1.5)
        if fraction > 0:
            inside = best_zone["bottom"] <= price <= best_zone["top"]
            fresh = " fresh" if best_zone.get("fresh") else " (%d prior tests)" % best_zone["tests"]
            out.append({
                "fraction": fraction,
                "conditions": ("zone_location",),
                "detail": "%s %s %s%s, quality %.0f/100" % (
                    "inside" if inside else "approaching",
                    best_zone["timeframe"],
                    "demand" if direction == "bullish" else "supply",
                    fresh,
                    best_zone["quality"],
                ),
            })

    # Order block and Fair Value Gap.
    key = "nearest_bullish" if direction == "bullish" else "nearest_bearish"
    for summary_key, label in (("order_blocks", "order block"), ("fvg", "FVG")):
        best = None
        for tf in ("H4", "H1", "M30", "M15", "M5"):
            item = (tfs.get(tf, {}).get(summary_key) or {}).get(key)
            if item and (best is None or item["quality"] > best["quality"]):
                best = dict(item, timeframe=tf)
        if not best:
            continue
        fraction = score_area(best, 2.0)
        if fraction > 0:
            inside = best["bottom"] <= price <= best["top"]
            out.append({
                "fraction": fraction,
                "conditions": ("fvg_or_orderblock",),
                "detail": "%s %s %s %.2f-%.2f, quality %.0f/100%s" % (
                    best["timeframe"], direction, label,
                    best["bottom"], best["top"], best["quality"],
                    " (price inside)" if inside else "",
                ),
            })

    # Volume profile: the value area and its shelves.
    #
    # The other three components all read price *shape* -- a zone, a block, a
    # gap are all drawn from where price went. This one reads participation:
    # the value-area edge is the boundary the market itself defended, and the
    # POC is the price it keeps coming back to. Neither is visible to any
    # other engine here, which is why the pillar was blind to it.
    vp_component = _value_area_component(tfs, direction, price, atr_v)
    if vp_component:
        out.append(vp_component)

    return out


# How far from a profile reference price still counts as "at" it, in ATR. The
# same reach the zone components use for an area edge, since that is what a
# value-area boundary behaves like.
VALUE_REACH_ATR = 0.6


def _value_area_component(tfs: dict, direction: str, price: float,
                          atr_v: float) -> dict | None:
    """Score price against the volume profile, best timeframe wins.

    A long wants to be at the *low* side of value or below it -- bought where
    the market considers price cheap. A short wants the high side. Sitting in
    the middle of the value area is the one location a profile actively argues
    against, and it scores nothing rather than a little.
    """
    best: tuple[float, str] | None = None

    # Finest first, and the execution timeframes lead.
    #
    # An intraday entry is priced against the session's value area, not the
    # week's: being above the H4 value area says nothing useful about a trade
    # that will be over in two hours, while being at the M15 value-area low
    # says a great deal. H4 is kept as the last resort so a setup still gets a
    # location read when the intraday profiles are unavailable, but it no
    # longer outranks the timeframe the entry is actually timed on.
    for tf in (*reversed(EXECUTION_TIMEFRAMES), "H4"):
        vp = (tfs.get(tf, {}) or {}).get("volume_profile") or {}
        if not vp.get("available"):
            continue

        edge = vp["val"] if direction == "bullish" else vp["vah"]
        poc = vp["poc"]
        reach = atr_v * VALUE_REACH_ATR

        # Extended beyond the far side of value: the mean-reversion case, and
        # the strongest thing a profile has to say about location.
        beyond = (direction == "bullish" and price < vp["val"]) or \
                 (direction == "bearish" and price > vp["vah"])
        at_edge = abs(price - edge) <= reach
        at_poc = abs(price - poc) <= reach

        if beyond:
            # The further outside value, the better the reversion case, up to
            # a point -- past ~2 ATR it is a trend that has left value behind,
            # not a stretched rubber band, so the score stops climbing.
            excursion = abs(price - edge) / atr_v if atr_v else 0.0
            fraction = min(0.95, 0.55 + excursion * 0.2)
            detail = (f"{tf} price {excursion:.1f} ATR "
                      f"{'below' if direction == 'bullish' else 'above'} the value area "
                      f"({vp['val']:.2f}-{vp['vah']:.2f}), stretched away from fair value")
        elif at_edge:
            fraction = 0.75
            detail = (f"{tf} at the value-area "
                      f"{'low' if direction == 'bullish' else 'high'} "
                      f"{edge:.2f}, the boundary the market defended")
        elif at_poc:
            # The POC is a magnet, not a turning point. It supports a
            # continuation entry and argues against a reversal one.
            fraction = 0.45
            detail = f"{tf} at the point of control {poc:.2f}, the window's busiest price"
        else:
            continue

        node = vp.get("nearest_high_volume_node")
        if node is not None and abs(price - node) <= reach:
            fraction = min(1.0, fraction + 0.15)
            detail += f"; on the {node:.2f} volume shelf"

        if best is None or fraction > best[0]:
            best = (fraction, detail)

    if best is None:
        return None
    return {"fraction": best[0], "conditions": ("value_area_location",),
            "detail": best[1]}


# --- penalties -------------------------------------------------------------


def _penalties(analysis: dict, direction: str, profile: StrategyProfile) -> list[Penalty]:
    out: list[Penalty] = []
    price = analysis["price"]
    atr_v = analysis["atr"] or 1
    tfs = analysis["timeframes"]

    # Opposing level immediately in the way.
    key = "nearest_resistance" if direction == "bullish" else "nearest_support"
    level = analysis["levels"].get(key)
    if level:
        distance = abs(level["price"] - price) / atr_v
        if distance < 2.0:
            severity = "critical" if distance < 0.6 and level["strength"] >= 65 else "warning"
            points = min(14.0, (2.0 - distance) * 6 * (level["strength"] / 100) * 1.6)
            out.append(Penalty(
                "opposing_level",
                f"{'Resistance' if direction == 'bullish' else 'Support'} in the way",
                points,
                f"{level['strength']:.0f}-strength {level['kind']} at {level['price']:.2f}, "
                f"{distance:.1f} ATR away",
                severity,
            ))

    # Higher-timeframe conflict.
    alignment = analysis["alignment"]
    htf_conflicts = [c for c in alignment["conflicts"] if c["timeframe"] in ("D1", "H4")]
    if htf_conflicts and alignment["direction"] != direction:
        out.append(Penalty(
            "htf_conflict", "Higher-timeframe conflict", 15.0,
            f"{', '.join(c['timeframe'] for c in htf_conflicts)} do not support a {direction} setup",
            "critical",
        ))
    elif htf_conflicts:
        out.append(Penalty(
            "htf_conflict", "Timeframe disagreement", 7.0,
            f"{', '.join(c['timeframe'] + ' ' + c['read'] for c in htf_conflicts)}",
            "warning",
        ))

    # Buying at a premium, or selling at a discount.
    #
    # The cheapest mistake in the book and the one the score could not see: a
    # textbook bullish setup at 90% of the dealing range scored exactly as
    # well as the same setup at 20%, because every location engine measured
    # price against a *pattern* and none measured it against the range. The
    # pattern can be perfect and the price still bad.
    #
    # Read on the *execution* timeframes, finest first, and never on H4.
    #
    # This method is intraday: D1 and H4 set direction, and H1, M30 and M15
    # are where an entry is actually timed. The range an intraday entry sits
    # in is the range of the timeframe it was timed on, so that is the range
    # its price has to be judged against. Pricing an M15 entry against the H4
    # swing range would mark half of every trending day as "premium" and
    # penalise exactly the intraday continuations this application exists to
    # find -- the aim is to catch every opportunity, not to hold out for the
    # swing-scale discount that may not come back for two days.
    #
    # H4 is excluded rather than merely deprioritised because a higher
    # timeframe that disagrees with the trade is *already* penalised, by
    # `htf_conflict` above. Reading it here too would charge one disagreement
    # twice.
    #
    # A range too tight to mean anything is refused inside `dealing_range`
    # itself, against that timeframe's own ATR -- so the guard travels down
    # the ladder with the timeframe instead of needing a whitelist here.
    for tf in reversed(EXECUTION_TIMEFRAMES):
        band = (tfs.get(tf, {}) or {}).get("dealing_range") or {}
        if not band.get("available") or band.get("zone") == "equilibrium":
            continue
        if band.get("favours") in (direction, "neutral"):
            continue

        # Premium and discount are mean-reversion ideas, and they do not
        # describe a trending market.
        #
        # A market trending up spends nearly all of its time in the upper half
        # of its own most recent range -- that is what an uptrend *is*. On the
        # finest execution timeframe that is close to permanent, so charging a
        # penalty for it would tax every intraday continuation and quietly
        # suppress the largest class of setup this method looks for. The
        # concept applies to a pullback or a reversal being bought at a bad
        # price; it does not apply to a breakout, which takes price at a new
        # high on purpose.
        #
        # So: when this timeframe's own structure is trending with the trade,
        # being high in the range is the thesis rather than a fault, and the
        # penalty is skipped.
        trend = ((tfs.get(tf, {}) or {}).get("structure") or {}).get("trend")
        if trend == direction:
            continue
        # How deep into the wrong half, 0..1, so the penalty scales with how
        # bad the price is rather than switching on at the midpoint.
        position = float(band.get("position", 0.5))
        depth = (position - 0.5) * 2 if direction == "bullish" else (0.5 - position) * 2
        if depth <= 0:
            continue
        out.append(Penalty(
            "premium_discount",
            "Buying at a premium" if direction == "bullish" else "Selling at a discount",
            min(9.0, depth * 11.0),
            f"{tf} price is {position * 100:.0f}% of the "
            f"{band['low']:.2f}-{band['high']:.2f} range, the wrong half for a "
            f"{direction} entry (equilibrium {band['equilibrium']:.2f})",
            "critical" if depth > 0.6 else "warning",
        ))
        break

    # Volatility extremes make entries unreliable on Gold.
    h1 = tfs.get("H1", {})
    atr_info = (h1.get("indicators") or {}).get("atr", {})
    if atr_info.get("elevated"):
        out.append(Penalty(
            "volatility", "Elevated volatility", 5.0,
            f"H1 {atr_info.get('note', 'ATR well above its median')} - wider stops and faster reversals",
            "warning",
        ))

    # Setup already extended: price far from the zone it should have entered at.
    zone_key = "nearest_demand" if direction == "bullish" else "nearest_supply"
    zone = (h1.get("zones") or {}).get(zone_key)
    if zone:
        distance = min(abs(price - zone["top"]), abs(price - zone["bottom"])) / atr_v
        if distance > 3.0:
            out.append(Penalty(
                "extended", "Setup is extended", min(10.0, (distance - 3) * 3),
                f"price is {distance:.1f} ATR from the nearest H1 "
                f"{'demand' if direction == 'bullish' else 'supply'} - poor entry location",
                "warning",
            ))

    # Regime mismatch: trend-following evidence inside a range regime.
    regime = analysis["regime"].get("regime", "")
    if regime in ("range", "consolidation"):
        out.append(Penalty(
            "regime", "Non-trending regime", 6.0,
            f"market regime is {regime}; continuation setups are less reliable here",
            "warning",
        ))
    elif regime == "potential reversal" and alignment["direction"] != direction:
        out.append(Penalty(
            "regime", "Reversal risk", 8.0,
            "structure suggests the prevailing move may be turning against this setup",
            "warning",
        ))

    # Economic events, when the calendar module has supplied them.
    event = (analysis.get("events") or {}).get("next_high_impact")
    if event and event.get("minutes_until") is not None:
        if 0 <= event["minutes_until"] <= profile.news_blackout_minutes:
            out.append(Penalty(
                "news", "High-impact news imminent", 8.0,
                f"{event['title']} in {event['minutes_until']} minutes - expect abnormal volatility",
                "warning",
            ))

    # ...and the release that has just printed. The first hour after CPI or
    # payrolls is where Gold whipsaws through levels the setup was built on;
    # the look-ahead window alone left that hour entirely unguarded.
    released = (analysis.get("events") or {}).get("last_high_impact")
    aftermath = getattr(profile, "news_aftermath_minutes", 60) or 0
    if released and released.get("minutes_since") is not None:
        if 0 <= released["minutes_since"] <= aftermath:
            out.append(Penalty(
                "news_aftermath", "High-impact release just printed", 8.0,
                f"{released['title']} {released['minutes_since']} minutes ago - "
                "levels are being tested by the reaction, not by the market's structure",
                "warning",
            ))

    return out


def _conditions_met(evidence: list[Evidence], profile: StrategyProfile) -> dict[str, bool]:
    """Which of the profile's required conditions this direction satisfies."""
    earned: set[str] = set()
    for e in evidence:
        # Partial credit does not satisfy a requirement; the factor must have
        # earned at least half its available points.
        if e.conditions and e.points >= e.max_points * 0.5:
            earned.update(e.conditions)
    return {c: (c in earned) for c in profile.required_conditions}
