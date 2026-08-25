"""Checks for the signal suggestion module.

The lifecycle cannot be verified by waiting for the market to cooperate, so
these tests drive a real suggestion through synthetic price moves and assert
that each stage in the specification is reached for the right reason.

Run with:  python -m tests.test_signals
"""

from __future__ import annotations

import sys
from dataclasses import replace

from goldassist import signals
from goldassist.decision import profile as profile_module
from goldassist.signals import classify, engine as signal_engine

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        FAILURES.append(name)


# --- fixtures --------------------------------------------------------------

def make_analysis(price: float, direction: str = "bullish") -> dict:
    """A minimal but realistic analysis object centred on ``price``."""
    atr = 10.0
    demand_low, demand_high = price - 22, price - 14
    return {
        "price": price,
        "atr": atr,
        "regime": {"regime": "strong bullish trend", "volatility": "normal"},
        "alignment": {
            "direction": direction,
            "agreement": 0.9,
            "higher_timeframe_bias": direction,
            "reads": {"D1": direction, "H4": direction, "H1": direction,
                      "M15": direction, "M5": direction},
            "conflicts": [],
        },
        "levels": {
            "levels": [
                {"price": price - 30, "kind": "support", "timeframe": "H1",
                 "source": "swing", "strength": 70},
                {"price": price + 40, "kind": "resistance", "timeframe": "H4",
                 "source": "swing", "strength": 75},
            ],
            "nearest_resistance": {"price": price + 40, "kind": "resistance",
                                   "timeframe": "H4", "source": "swing", "strength": 75},
            "nearest_support": {"price": price - 30, "kind": "support",
                                "timeframe": "H1", "source": "swing", "strength": 70},
            "fibonacci": {},
        },
        "liquidity": {
            "pools": [
                {"price": price + 25, "side": "buyside", "timeframe": "H1",
                 "source": "equal_highs", "strength": 70},
                {"price": price + 55, "side": "buyside", "timeframe": "H4",
                 "source": "PDH", "strength": 80},
            ],
            "sweeps": [], "latest_sweep": None, "bias": "neutral", "target": None,
        },
        "timeframes": {
            tf: {
                "bias": direction,
                "atr": atr,
                "structure": {
                    "trend": direction,
                    "state": "retracement within bullish structure",
                    "last_event": {"kind": "BOS", "direction": direction},
                    "recent_events": [{"kind": "BOS", "direction": direction,
                                       "displacement": 1.4, "price": price,
                                       "time": 0}],
                    "last_swing_low": {"price": demand_low - 8},
                    "last_swing_high": {"price": price + 30},
                },
                "zones": {
                    "nearest_demand": {"bottom": demand_low, "top": demand_high,
                                       "quality": 80, "fresh": True, "tests": 0},
                    "nearest_supply": None,
                    "bias": direction, "location": "between zones",
                },
                "order_blocks": {
                    "nearest_bullish": {"bottom": demand_low + 2, "top": demand_high + 1,
                                        "quality": 75},
                    "nearest_bearish": None, "bias": direction, "count": 3,
                },
                "fvg": {
                    "nearest_bullish": {"bottom": demand_low + 3, "top": demand_high,
                                        "quality": 60},
                    "nearest_bearish": None, "bias": direction, "count": 2,
                },
                "candles": {
                    "bias": direction,
                    "patterns": [{"name": "bullish engulfing", "direction": direction,
                                  "weight": 0.8, "note": "engulfs prior candle",
                                  "at_level": True}],
                    "latest": None, "score": 0.8,
                },
                "indicators": {},
                "regime": {"regime": "strong bullish trend"},
            }
            for tf in ("D1", "H4", "H1", "M15", "M5")
        },
        "events": {"available": False, "next_high_impact": None},
    }


def make_decision(price: float, score: float, direction: str = "bullish") -> dict:
    return {
        "decision": "BUY" if direction == "bullish" else "SELL",
        "direction": direction,
        "score": score,
        "price": price,
        "regime": {"regime": "strong bullish trend"},
        "reasons": ["D1 and H4 bullish", "price at H1 demand"],
        "waiting_for": [],
        "invalidation": {"price": price - 30, "basis": "H1 swing low"},
        "targets": [{"price": price + 25, "basis": "H1 equal highs liquidity"}],
        "risk": {"warnings": [], "reward_risk": 2.0, "atr": 10.0},
    }


# --- tests -----------------------------------------------------------------

def test_entry_zone():
    print("entry zone")
    analysis = make_analysis(4400.0)
    zone = signals.entry.build(analysis, "bullish")
    check("a zone is produced", zone is not None)
    if not zone:
        return
    check("zone sits below price for a buy", zone.high <= analysis["price"],
          f"{zone.low}-{zone.high} vs {analysis['price']}")
    check("zone has width", zone.high > zone.low)
    check("preferred price is inside the zone", zone.low <= zone.preferred <= zone.high)
    check("multiple areas confluence", zone.confluence_count >= 2,
          str(zone.confluence_count))
    check("sources are named", all(s.label for s in zone.sources))
    check("rationale is human-readable", "overlap" in zone.rationale()
          or "the " in zone.rationale())


def test_invalidation_side():
    print("invalidation placement")
    analysis = make_analysis(4400.0)
    zone = signals.entry.build(analysis, "bullish")
    inval = signals.risk_levels.invalidation_for_zone(
        analysis, "bullish", zone.low, zone.high)
    check("invalidation is below a buy zone", inval["price"] < zone.low,
          f"inval {inval['price']} vs zone low {zone.low}")
    check("invalidation has a stated basis", bool(inval.get("basis")))

    bear = make_analysis(4400.0, "bearish")
    # Mirror the fixture so a sell zone sits above price.
    for tf in bear["timeframes"].values():
        tf["zones"]["nearest_supply"] = {"bottom": 4414.0, "top": 4422.0,
                                         "quality": 80, "fresh": True, "tests": 0}
        tf["zones"]["nearest_demand"] = None
        tf["order_blocks"]["nearest_bearish"] = {"bottom": 4415.0, "top": 4421.0,
                                                 "quality": 70}
        tf["order_blocks"]["nearest_bullish"] = None
        tf["fvg"]["nearest_bearish"] = {"bottom": 4416.0, "top": 4420.0, "quality": 60}
        tf["fvg"]["nearest_bullish"] = None
        tf["structure"]["last_swing_high"] = {"price": 4432.0}
    sell_zone = signals.entry.build(bear, "bearish")
    if sell_zone:
        sell_inval = signals.risk_levels.invalidation_for_zone(
            bear, "bearish", sell_zone.low, sell_zone.high)
        check("invalidation is above a sell zone", sell_inval["price"] > sell_zone.high,
              f"inval {sell_inval['price']} vs zone high {sell_zone.high}")
    else:
        check("invalidation is above a sell zone", False, "no sell zone built")


def test_quality_bands():
    print("quality bands")
    cases = [(95, "Very Strong"), (85, "Strong"), (75, "Good"),
             (65, "Developing"), (45, "Weak"), (20, "No Setup")]
    for score, expected in cases:
        check(f"score {score} -> {expected}",
              signals.quality_of(score) == expected, signals.quality_of(score))


def test_setup_classification():
    print("setup classification")
    analysis = make_analysis(4400.0)
    kind = classify.setup_type(analysis, "bullish")
    check("retracement in an aligned trend is a pullback",
          kind == classify.PULLBACK, kind)

    # A sweep plus a character change should read as a reversal.
    reversal = make_analysis(4400.0)
    reversal["liquidity"]["latest_sweep"] = {"direction": "bullish", "timeframe": "M15",
                                             "level": 4380.0}
    for tf in ("M15", "M5"):
        reversal["timeframes"][tf]["structure"]["last_event"] = {
            "kind": "MSS", "direction": "bullish"}
    check("sweep plus MSS reads as a reversal",
          classify.setup_type(reversal, "bullish") == classify.REVERSAL,
          classify.setup_type(reversal, "bullish"))

    ranging = make_analysis(4400.0)
    ranging["regime"]["regime"] = "range"
    check("a range regime reads as range rotation",
          classify.setup_type(ranging, "bullish") == classify.RANGE)


def test_trigger_strictness():
    print("entry triggers")
    analysis = make_analysis(4400.0)
    pullback = classify.trigger_for(classify.PULLBACK, analysis, "bullish")
    reversal = classify.trigger_for(classify.REVERSAL, analysis, "bullish")
    check("a reversal demands more than a pullback",
          len(reversal.requirements) > len(pullback.requirements),
          f"{len(reversal.requirements)} vs {len(pullback.requirements)}")
    check("a reversal without a sweep is not met", not reversal.met)
    check("unmet triggers list what is outstanding", len(reversal.outstanding) > 0)


def test_lifecycle():
    print("signal lifecycle")
    prof = profile_module.get("Default XAUUSD")
    price = 4400.0
    analysis = make_analysis(price)
    decision = make_decision(price, 72.0)

    suggestion = signals.build(analysis, decision, prof)
    check("a suggestion is built", suggestion is not None)
    if not suggestion:
        return

    zone = suggestion.entry_zone
    check("starts away from the zone as WATCH or DEVELOPING",
          suggestion.status in (signals.WATCH, signals.DEVELOPING), suggestion.status)
    check("targets are laddered TP1..",
          [t.level for t in suggestion.targets][:1] == ["TP1"],
          str([t.level for t in suggestion.targets]))
    check("reward/risk is measured from the zone, not spot",
          suggestion.reward_risk is not None and 0.2 < suggestion.reward_risk < 20,
          str(suggestion.reward_risk))

    # Price walks down into the zone.
    in_zone_price = (zone.low + zone.high) / 2
    analysis2 = make_analysis(in_zone_price)
    # Strip the confirmation so the setup must wait for it.
    for tf in analysis2["timeframes"].values():
        tf["structure"]["recent_events"] = []
        tf["candles"]["patterns"] = []
    decision2 = make_decision(in_zone_price, 72.0)
    suggestion, events = signals.update(suggestion, analysis2, decision2, prof)
    check("entering the zone without confirmation waits",
          suggestion.status == signals.WAITING_FOR_CONFIRMATION, suggestion.status)
    check("the stage change is reported", any("->" in e for e in events), str(events))

    # Confirmation arrives.
    analysis3 = make_analysis(in_zone_price)
    decision3 = make_decision(in_zone_price, 82.0)
    suggestion, events = signals.update(suggestion, analysis3, decision3, prof)
    check("confirmation moves it to CONFIRMED",
          suggestion.status == signals.CONFIRMED, suggestion.status)
    check("quality re-grades with the score", suggestion.quality == "Strong",
          suggestion.quality)

    # Price runs to the first target. With the default profile that banks the
    # trade: the signal is decided, so it leaves the board carrying a result
    # rather than sitting on the dashboard indefinitely.
    tp1 = suggestion.targets[0].price
    analysis4 = make_analysis(tp1 + 1)
    decision4 = make_decision(tp1 + 1, 82.0)
    suggestion, events = signals.update(suggestion, analysis4, decision4, prof)
    check("reaching TP1 is reported", any("TP1" in e for e in events), str(events))
    check("reaching the first objective resolves the setup",
          suggestion.status == signals.TARGET_HIT, suggestion.status)
    check("the win is banked in pips and R",
          suggestion.outcome == "win" and suggestion.result_pips >= 50,
          f"{suggestion.outcome} {suggestion.result_pips}")
    check("a resolved setup is terminal", suggestion.status in signals.TERMINAL_STATES)

    # A separate scenario: price breaks the invalidation.
    fresh = signals.build(make_analysis(price), make_decision(price, 72.0), prof)
    broken = fresh.invalidation["price"] - 2
    fresh, events = signals.update(fresh, make_analysis(broken),
                                   make_decision(broken, 60.0), prof)
    check("breaching invalidation ends the setup",
          fresh.status == signals.INVALIDATED, fresh.status)
    check("INVALIDATED is terminal", fresh.status in signals.TERMINAL_STATES)


def test_weakening():
    print("deterioration")
    prof = profile_module.get("Default XAUUSD")
    price = 4400.0
    suggestion = signals.build(make_analysis(price), make_decision(price, 85.0), prof)
    check("peak score is recorded", suggestion.peak_score >= 85.0)
    suggestion, _ = signals.update(suggestion, make_analysis(price),
                                   make_decision(price, 55.0), prof)
    check("a large fall from peak reads as WEAKENING",
          suggestion.status == signals.WEAKENING, suggestion.status)


def test_serialisation():
    print("serialisation")
    import json
    from goldassist.jsonsafe import safe

    prof = profile_module.get("Default XAUUSD")
    suggestion = signals.build(make_analysis(4400.0), make_decision(4400.0, 75.0), prof)
    payload = suggestion.to_dict()
    json.dumps(safe(payload))
    check("suggestion serialises to JSON", True)
    for field in ("id", "direction", "setup_label", "status", "score", "quality",
                  "entry_zone", "trigger", "invalidation", "targets", "reward_risk"):
        check(f"card field '{field}' present", field in payload)




def test_pip_opportunity():
    print("50-pip opportunity rule")
    from goldassist.signals import opportunity as op

    check("50 pips is 5.00 of Gold price", op.to_price(50) == 5.0, str(op.to_price(50)))
    check("5.00 of price is 50 pips", op.to_pips(5.0) == 50.0, str(op.to_pips(5.0)))

    for pips, expected in ((49, "Below minimum"), (50, "Good"), (99, "Good"),
                           (100, "Strong"), (199, "Strong"), (200, "Major")):
        check(f"{pips} pips grades as {expected}",
              op.classify(pips)[0] == expected, op.classify(pips)[0])

    # The worked example from the specification.
    spec = op.assess(4400.0, [{"price": 4405.0}, {"price": 4415.0}, {"price": 4430.0}], 50.0)
    check("spec example qualifies", spec["qualifies"])
    check("spec example reports the first objective at 50 pips",
          spec["first_target_pips"] == 50.0, str(spec["first_target_pips"]))
    check("spec example is never capped at 50 pips",
          spec["max_pips"] == 300.0, str(spec["max_pips"]))
    check("spec example grades on the full potential",
          spec["classification"] == "Major", spec["classification"])

    short = op.assess(4400.0, [{"price": 4402.0}], 50.0)
    check("a 20-pip move does not qualify", not short["qualifies"])
    check("the shortfall is reported", short["shortfall_pips"] == 30.0,
          str(short["shortfall_pips"]))

    # Availability alone must not create a signal.
    prof = profile_module.get("Default XAUUSD")
    check("profile carries a configurable pip minimum",
          getattr(prof, "min_target_pips", None) == 50.0)


def test_targets_respect_pip_floor():
    print("targets honour the pip floor")
    from goldassist.signals import risk_levels, opportunity as op

    analysis = make_analysis(4400.0)
    zone = signals.entry.build(analysis, "bullish")
    inval = risk_levels.invalidation_for_zone(analysis, "bullish", zone.low, zone.high)
    targets = risk_levels.targets_for_zone(analysis, "bullish", zone.preferred,
                                           inval["price"], minimum_pips=50.0)
    check("targets are returned", len(targets) > 0)
    if targets:
        first = op.to_pips(targets[0]["price"] - zone.preferred)
        check("the first objective clears 50 pips", first >= 50.0, f"{first:.1f} pips")
        check("each target reports its pip distance",
              all("pips" in t for t in targets))
        check("larger targets are not truncated at the minimum",
              max(t["pips"] for t in targets) >= first)

    prof = profile_module.get("Default XAUUSD")
    suggestion = signals.build(analysis, make_decision(4400.0, 75.0), prof)
    check("the suggestion carries an opportunity grade",
          bool(suggestion.opportunity.get("classification")))
    check("the card reports pips per target",
          all(t.to_dict()["pips"] >= 0 for t in suggestion.targets))

def main() -> int:
    for test in (test_pip_opportunity, test_targets_respect_pip_floor, test_entry_zone, test_invalidation_side, test_quality_bands,
                 test_setup_classification, test_trigger_strictness,
                 test_lifecycle, test_weakening, test_serialisation):
        test()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) failed: {', '.join(FAILURES)}")
        return 1
    print("All signal checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
