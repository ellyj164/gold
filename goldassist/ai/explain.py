"""Deterministic explanation generator.

The offline half of the specification's two-layer design: when the AI service
is unavailable, the application still explains itself. These functions read the
same facts the AI would and assemble prose from them — no model, no network.
"""

from __future__ import annotations

CONCEPTS = {
    "bos": (
        "A Break of Structure (BOS) is a candle *closing* beyond the most recent swing "
        "point in the direction of the existing trend — above the last swing high in an "
        "uptrend, below the last swing low in a downtrend. It confirms that the trend is "
        "continuing. A wick through the level without a close beyond it is not a BOS; it "
        "is a liquidity event."
    ),
    "choch": (
        "A Change of Character (CHoCH) is the first structural break *against* the "
        "prevailing trend: in an uptrend, a close below the last higher low. It is the "
        "earliest warning that control may be changing hands — a warning, not a reversal. "
        "Confirmation still has to arrive."
    ),
    "mss": (
        "A Market Structure Shift (MSS) is a Change of Character carried by an unusually "
        "large displacement candle. The size of the move is what separates it from an "
        "ordinary CHoCH: it suggests the break was driven by real participation rather "
        "than drift, which is why it carries more weight as a reversal signal."
    ),
    "liquidity": (
        "Liquidity means resting orders. Stop-losses cluster just beyond obvious swing "
        "highs and lows, so those levels act as magnets: buy-side liquidity sits above "
        "highs, sell-side liquidity below lows. Price often reaches for that liquidity "
        "before making its real move."
    ),
    "liquidity sweep": (
        "A liquidity sweep is a wick through a level where stops rest, followed by a close "
        "back inside it. It tells you price reached for those orders and was rejected. "
        "On its own it means nothing directional — it is a *location* signal. Structural "
        "confirmation in the opposite direction still has to follow before it becomes a setup."
    ),
    "fvg": (
        "A Fair Value Gap is a three-candle imbalance: the first candle's wick and the "
        "third candle's wick fail to overlap, leaving a price band that traded only during "
        "the middle candle's expansion. Gaps created by genuine displacement matter; gaps "
        "left by random drift do not."
    ),
    "order block": (
        "An Order Block is the last opposing candle before a displacement move that breaks "
        "structure — the last down-candle before an impulsive rally, or the last up-candle "
        "before an impulsive sell-off. Its quality depends on the strength of the departure, "
        "whether that departure broke structure, and whether price has already worked "
        "through it."
    ),
    "supply and demand": (
        "A demand zone is the base price left rapidly to the upside; a supply zone is the "
        "mirror. Freshness is the key variable: a zone price has not yet returned to is "
        "stronger than one already tested several times, because each test consumes the "
        "orders that made it significant."
    ),
    "regime": (
        "Market regime is the environment the market is in — trending, ranging, "
        "consolidating, expanding, retracing, or potentially reversing. It matters because "
        "the same evidence means different things in different regimes: a breakout is "
        "meaningful in an expansion and a trap in a range."
    ),
    "confluence": (
        "Confluence is agreement between independent forms of evidence — higher-timeframe "
        "direction, location at a zone, a liquidity event, structural confirmation, and "
        "supporting momentum. No single factor should ever produce a setup; the strength "
        "of a setup is how many independent things point the same way."
    ),
    "invalidation": (
        "The invalidation level is the price whose breach would disprove the setup. It is "
        "not an arbitrary distance — it is the structural point that must hold for the idea "
        "to remain valid, usually the swing low or the far edge of the zone price is "
        "reacting from."
    ),
}


def decision_narrative(decision: dict, analysis: dict) -> str:
    """Assemble the full explanation of a decision from the facts."""
    action = decision["decision"]
    direction = decision["direction"]
    score = decision["score"]
    regime = (decision.get("regime") or {}).get("regime", "unknown")
    price = decision.get("price")
    parts: list[str] = []

    # 1. The conclusion.
    if action == "WAIT":
        parts.append(
            f"The engine is holding at WAIT. The {direction} case scores {score:.0f}/100 "
            f"with Gold at {price:.2f} in a {regime} regime — there is a case, but it is "
            "not complete."
        )
    else:
        parts.append(
            f"The engine reads this as a {action}. The {direction} case scores "
            f"{score:.0f}/100 with Gold at {price:.2f} in a {regime} regime."
        )

    # 2. Timeframe picture.
    alignment = analysis.get("alignment", {})
    reads = alignment.get("reads", {})
    if reads:
        agreeing = [tf for tf, r in reads.items() if r.startswith(alignment.get("direction", ""))]
        parts.append(
            "Across timeframes: "
            + ", ".join(f"{tf} {r}" for tf, r in reads.items())
            + f". That is {len(agreeing)} of {len(reads)} pointing "
            f"{alignment.get('direction', 'neutral')}, an agreement level of "
            f"{alignment.get('agreement', 0) * 100:.0f}%."
        )

    # 3. The evidence.
    evidence = (decision.get("confluence", {}).get(direction, {}) or {}).get("evidence", [])
    if evidence:
        top = evidence[:5]
        parts.append(
            "The evidence behind that, strongest first: "
            + "; ".join(f"{e['detail']} ({e['points']:.0f} pts)" for e in top)
            + "."
        )

    # 4. The counter-case — never omitted.
    penalties = (decision.get("confluence", {}).get(direction, {}) or {}).get("penalties", [])
    if penalties:
        parts.append(
            "Against it: "
            + "; ".join(f"{p['detail']} (-{p['points']:.0f})" for p in penalties[:3])
            + "."
        )
    else:
        opposing = decision.get("opposing_score", 0)
        parts.append(
            f"The opposing case scores {opposing:.0f}/100, so the evidence is not "
            "meaningfully split."
        )

    # 4b. Where price actually is, as opposed to what pattern it made.
    #
    # The three questions a trader asks first: is this a fair price, has the
    # market been trading here, and did a level fail? The narrative could
    # answer none of them, while the scoring engine had been using all three
    # since v1.2.0.
    parts.extend(_location_sentences(analysis, direction))

    # 5. What is missing (the heart of a WAIT).
    waiting = decision.get("waiting_for") or []
    if waiting:
        parts.append(
            "What is still missing: "
            + "; ".join(waiting[:4])
            + "."
        )

    # 6. Invalidation and targets.
    inval = decision.get("invalidation") or {}
    if inval.get("description"):
        parts.append(f"The setup is invalidated by {inval['description']}.")
    targets = decision.get("targets") or []
    if targets:
        parts.append(
            "The objectives ahead are "
            + ", ".join(f"{t['price']:.2f} ({t['basis']})" for t in targets[:3])
            + "."
        )
    rr = (decision.get("risk") or {}).get("reward_risk")
    if rr:
        parts.append(f"That shapes a reward-to-risk of roughly {rr:.2f}:1 to the first objective.")

    # 7. Risk warnings.
    warnings = (decision.get("risk") or {}).get("warnings") or []
    for w in warnings[:2]:
        parts.append(w["message"])

    # 8. What is coming. A release outranks everything above it.
    event = _event_sentence(analysis)
    if event:
        parts.append(event)

    parts.append(
        "This is technical evidence, not a probability of profit, and the application does "
        "not place trades — the decision stays with you."
    )
    return "\n\n".join(parts)


def _location_sentences(analysis: dict, direction: str) -> list[str]:
    """Volume profile, premium/discount and breakers, in plain sentences.

    Reads the execution timeframes first. This method is intraday: the range
    and the value area an entry is priced against are those of the timeframe
    the entry is timed on, not the swing-scale ones.
    """
    out: list[str] = []
    frames = analysis.get("timeframes") or {}

    for tf in ("M15", "M30", "H1"):
        vp = (frames.get(tf) or {}).get("volume_profile") or {}
        if not vp.get("available"):
            continue
        line = (f"On {tf} the market has been trading around {vp['poc']:.2f}, with its "
                f"value area between {vp['val']:.2f} and {vp['vah']:.2f}. Price is "
                f"{vp.get('location', 'inside value')} — {vp.get('meaning', '')}.")
        node = vp.get("nearest_low_volume_node")
        if node is not None:
            line += (f" There is thin volume around {node:.2f}, the kind of gap price "
                     f"crosses quickly once it starts moving.")
        out.append(line)
        break

    for tf in ("M15", "M30", "H1"):
        band = (frames.get(tf) or {}).get("dealing_range") or {}
        if not band.get("available"):
            continue
        position = float(band.get("position", 0.5)) * 100
        verdict = ("a good price for a long" if band["zone"] == "discount"
                   else "a good price for a short" if band["zone"] == "premium"
                   else "neither cheap nor expensive")
        line = (f"Against the {tf} dealing range {band['low']:.2f}–{band['high']:.2f}, "
                f"price sits at {position:.0f}% of it — {band['zone']}, which is "
                f"{verdict}.")
        if band.get("favours") not in (direction, "neutral") and band["zone"] != "equilibrium":
            line += (f" That is the wrong half for a {direction} entry, which is the "
                     f"most common way a good pattern becomes a bad trade.")
        out.append(line)
        break

    for tf in ("M15", "H1", "H4"):
        breakers = (frames.get(tf) or {}).get("breakers") or {}
        if not breakers.get("available"):
            continue
        near = breakers.get("nearest_bullish" if direction == "bullish"
                            else "nearest_bearish")
        if not near:
            continue
        out.append(
            f"A {tf} breaker sits at {near['bottom']:.2f}–{near['top']:.2f} — a level that "
            f"failed and flipped side, marking where the previous move's participants were "
            f"proved wrong."
            + (" Price is inside it now." if near.get("price_inside") else ""))
        break

    return out


def _event_sentence(analysis: dict) -> str | None:
    """A release warning, when one is close enough to change what to do.

    Four hours is the horizon because that is roughly when Gold's spread and
    behaviour begin to reflect an imminent print. Beyond it the warning is
    noise; inside it, it outranks the technical read.
    """
    events = analysis.get("events") or {}
    if not events.get("available"):
        return None
    nxt = events.get("next_high_impact")
    if not nxt:
        return None
    minutes = int(nxt.get("minutes_until") or 0)
    if minutes < 0 or minutes > 240:
        return None
    when = f"{minutes} minutes" if minutes >= 1 else "moments"
    return (f"{nxt['title']} is {when} away. Technical setups behave abnormally around a "
            f"release of this size: spreads widen, levels are swept and reclaimed, and a "
            f"stop placed on structure can be taken by noise alone. That does not "
            f"invalidate the analysis above — it changes what it is worth acting on now.")


def answer_offline(question: str, decision: dict, analysis: dict) -> str:
    """Best-effort answer to a trader question without the AI layer."""
    q = question.lower()

    if any(w in q for w in ("why", "reason", "explain")):
        return decision_narrative(decision, analysis)

    if "wait" in q or "waiting" in q or "confirm" in q:
        waiting = decision.get("waiting_for") or []
        if not waiting:
            return ("Nothing is outstanding — the engine's conditions for a "
                    f"{decision['decision']} are met.")
        return "Waiting for: " + _join(waiting)

    if "invalidat" in q:
        inval = decision.get("invalidation") or {}
        return inval.get("description", "No structural invalidation level is available.")

    if "target" in q or "where" in q and "go" in q:
        targets = decision.get("targets") or []
        if not targets:
            return "No objective is far enough from price to be meaningful right now."
        return "Objectives: " + ", ".join(
            f"{t['price']:.2f} ({t['basis']})" for t in targets
        ) + "."

    if "resistance" in q:
        lvl = analysis.get("levels", {}).get("strongest_resistance")
        return (f"The strongest resistance above price is {lvl['price']:.2f} "
                f"({lvl['timeframe']} {lvl['source']}, strength {lvl['strength']:.0f}/100)."
                if lvl else "No significant resistance is mapped above price.")

    if "support" in q or "demand" in q:
        lvl = analysis.get("levels", {}).get("strongest_support")
        return (f"The strongest support below price is {lvl['price']:.2f} "
                f"({lvl['timeframe']} {lvl['source']}, strength {lvl['strength']:.0f}/100)."
                if lvl else "No significant support is mapped below price.")

    if "sweep" in q or "liquidity" in q:
        sweep = analysis.get("liquidity", {}).get("latest_sweep")
        if not sweep:
            return "No liquidity sweep has been detected in the recent window."
        return (f"The most recent sweep is a {sweep['direction']} sweep on {sweep['timeframe']} "
                f"at {sweep['level']:.2f}, with a {sweep['wick_depth_atr']:.1f} ATR wick beyond "
                f"the level"
                + (" and structure has confirmed it." if sweep.get("confirmed_by_structure")
                   else ", not yet confirmed by structure."))

    if "trend" in q or "bias" in q:
        reads = analysis.get("alignment", {}).get("reads", {})
        return "Timeframe reads: " + ", ".join(f"{tf} {r}" for tf, r in reads.items()) + "."

    if "conflict" in q:
        conflicts = analysis.get("alignment", {}).get("conflicts", [])
        if not conflicts:
            return "No timeframe is currently conflicting with the dominant direction."
        return "Conflicting: " + ", ".join(
            f"{c['timeframe']} ({c['role']}) reading {c['read']}" for c in conflicts
        ) + "."

    # Longest key first: "liquidity sweep" must win over "liquidity".
    for key in sorted(CONCEPTS, key=len, reverse=True):
        if key in q:
            return CONCEPTS[key]

    return (
        "The AI analyst is offline, so only the engine's own summary is available:\n\n"
        + decision_narrative(decision, analysis)
    )


def _join(items: list[str]) -> str:
    """Join clauses without doubling a full stop the last one already has."""
    text = "; ".join(item.rstrip(". ") for item in items)
    return text + "."


def teach_offline(concept: str) -> str:
    key = concept.lower().strip()
    for name in sorted(CONCEPTS, key=len, reverse=True):
        if name in key or key in name:
            return CONCEPTS[name]
    return (
        "That concept is not in the offline glossary. The available topics are: "
        + ", ".join(sorted(CONCEPTS)) + "."
    )
