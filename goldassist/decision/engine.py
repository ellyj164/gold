"""BUY / SELL / WAIT decision engine.

Turns the confluence scores into one decision, and -- just as importantly --
states what would confirm the setup and what would invalidate it.

WAIT is a first-class outcome, not a failure to decide. The engine returns WAIT
whenever evidence is insufficient, conflicting, gated by an unmet profile
requirement, or positioned poorly, and it always says specifically what is
missing.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from . import risk
from .confluence import evaluate
from .profile import StrategyProfile

BUY = "BUY"
SELL = "SELL"
WAIT = "WAIT"

CONDITION_LABELS = {
    "higher_timeframe_alignment": "higher-timeframe direction agreeing",
    "liquidity_event": "a liquidity sweep",
    "zone_location": "price reacting from a supply/demand zone",
    "fvg_or_orderblock": "a Fair Value Gap or Order Block in the direction",
    "structure_confirmation": "a confirmed structure break on the confirmation timeframe",
    "candle_confirmation": "a rejection or displacement candle",
}


def decide(analysis: dict, profile: StrategyProfile) -> dict:
    """Produce the full decision object for the current market state."""
    confluence = evaluate(analysis, profile)
    leading = confluence["leading"]
    best = confluence[leading]
    other = confluence["bearish" if leading == "bullish" else "bullish"]

    risk_view = risk.assess(analysis, leading)
    decision, reasons, waiting_for = _resolve(analysis, profile, best, other, risk_view)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "decision": decision,
        "direction": leading,
        "score": best["score"],
        "opposing_score": other["score"],
        "separation": confluence["separation"],
        "profile": profile.name,
        "regime": analysis["regime"],
        "price": analysis["price"],
        "confluence": confluence,
        "risk": risk_view,
        "reasons": reasons,
        "waiting_for": waiting_for,
        "invalidation": risk_view["invalidation"],
        "targets": risk_view["targets"],
        "confirmation_checklist": _checklist(analysis, profile, best),
        "alignment": analysis["alignment"],
        "headline": _headline(decision, leading, best["score"], analysis),
    }


def _resolve(analysis, profile, best, other, risk_view):
    """Apply the decision rules in priority order."""
    score = best["score"]
    direction = best["direction"]
    action = BUY if direction == "bullish" else SELL
    reasons = _top_reasons(best)
    waiting: list[str] = []

    # 1. Not enough evidence at all.
    if score < profile.watch_score:
        waiting.append(
            f"the setup score to reach at least {profile.min_score} (currently {score:.0f})"
        )
        return WAIT, reasons, waiting

    # 2. A required condition of the active profile is unmet.
    missing = best["missing_conditions"]
    if missing:
        waiting.extend(CONDITION_LABELS.get(m, m) for m in missing)

    # 3. A critical penalty blocks entry regardless of score.
    critical = [p for p in best["penalties"] if p["severity"] == "critical"]
    if critical:
        waiting.extend(p["detail"] for p in critical)

    # 4. Both directions scoring similarly means the market is genuinely unclear.
    if best["score"] - other["score"] < 12 and other["score"] > 45:
        waiting.append(
            f"the conflict to resolve - bullish and bearish evidence are close "
            f"({best['score']:.0f} vs {other['score']:.0f})"
        )

    # 5. Positioning must be acceptable even when the evidence is strong.
    if not risk_view["acceptable_positioning"]:
        waiting.extend(w["message"] for w in risk_view["warnings"] if w["severity"] == "high")

    # 6. Reward/risk must clear the profile's configured minimum. A setup can
    #    be technically excellent and still not worth the risk it demands.
    rr = risk_view.get("reward_risk")
    minimum_rr = getattr(profile, "min_reward_risk", 1.5)
    if rr is not None and rr < minimum_rr:
        waiting.append(
            f"better reward/risk - the nearest objective offers {rr:.2f}:1, "
            f"the profile requires {minimum_rr:.1f}:1"
        )

    # 7. The move must be big enough to be worth an intraday trade at all.
    #    Having the distance available does not make a signal -- but not having
    #    it definitely prevents one.
    minimum_pips = getattr(profile, "min_target_pips", 500.0)
    targets = risk_view.get("targets") or []
    if targets and minimum_pips:
        first_pips = targets[0].get("pips")
        if first_pips is not None and first_pips < minimum_pips:
            waiting.append(
                f"a bigger opportunity - the first objective is only "
                f"{first_pips:.0f} pips away, below the {minimum_pips:.0f}-pip "
                f"minimum for an intraday trade"
            )

    # 8. Score threshold.
    if score < profile.min_score:
        waiting.append(
            f"stronger confluence - score is {score:.0f}, the profile requires {profile.min_score}"
        )

    if waiting:
        return WAIT, reasons, _dedupe(waiting)
    return action, reasons, []


def _top_reasons(best: dict, limit: int = 6) -> list[str]:
    reasons = [e["detail"] for e in best["evidence"][:limit]]
    # Surface the largest penalty too, so the reasoning is not one-sided.
    if best["penalties"]:
        top = best["penalties"][0]
        reasons.append(f"counterpoint: {top['detail']}")
    return reasons


def _checklist(analysis: dict, profile: StrategyProfile, best: dict) -> list[dict]:
    """What is satisfied and what is still outstanding, as a visible checklist."""
    conditions = best["conditions"]
    items = [
        {
            "condition": CONDITION_LABELS.get(name, name),
            "met": met,
            "required": True,
        }
        for name, met in conditions.items()
    ]

    # Confirmation-timeframe status is always shown, required or not.
    conf_tf = profile.confirmation_timeframe
    conf = analysis["timeframes"].get(conf_tf, {})
    conf_read = conf.get("bias", "unknown")
    items.append({
        "condition": f"{conf_tf} reading {best['direction']}",
        "met": conf_read.startswith(best["direction"]),
        "required": False,
        "detail": f"{conf_tf} is currently {conf_read}",
    })
    return items


def _headline(decision: str, direction: str, score: float, analysis: dict) -> str:
    regime = analysis["regime"].get("regime", "unknown")
    if decision == WAIT:
        return (f"WAIT - {direction} evidence at {score:.0f}/100 in a {regime} regime, "
                "but confirmation is incomplete")
    return f"{decision} - {direction} setup scoring {score:.0f}/100 in a {regime} regime"


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        key = item.lower().strip()
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out


def _reason_key(reason: str) -> str:
    """Identity of a waiting reason, ignoring the live numbers inside it."""
    return re.sub(r"[-+]?\d[\d,.]*", "#", reason).strip().lower()


def diff(previous: dict | None, current: dict) -> dict | None:
    """Describe what changed between two decisions, for the update feed.

    Returns ``None`` when nothing material changed, so the UI only reports
    genuine developments rather than every refresh tick.
    """
    if previous is None:
        return {
            "type": "initial",
            "message": f"Analysis started: {current['decision']} "
                       f"({current['direction']}, score {current['score']:.0f})",
            "decision": current["decision"],
            "score": current["score"],
        }

    changes: list[str] = []
    if previous["decision"] != current["decision"]:
        changes.append(f"Decision changed from {previous['decision']} to {current['decision']}")
    score_delta = current["score"] - previous["score"]
    if abs(score_delta) >= 5:
        changes.append(
            f"Setup score changed from {previous['score']:.0f} to {current['score']:.0f}"
        )
    if previous["direction"] != current["direction"]:
        changes.append(
            f"Leading direction flipped from {previous['direction']} to {current['direction']}"
        )
    prev_regime = (previous.get("regime") or {}).get("regime")
    cur_regime = (current.get("regime") or {}).get("regime")
    if prev_regime != cur_regime:
        changes.append(f"Market regime changed from {prev_regime} to {cur_regime}")

    # Newly satisfied or newly lost requirements are the most useful updates.
    #
    # Compared on a normalised form: the raw strings embed live numbers
    # ("0.1 ATR away", "score is 54"), so a raw comparison reported a change on
    # almost every tick and buried the real developments.
    prev_missing = {_reason_key(r): r for r in previous.get("waiting_for", [])}
    cur_missing = {_reason_key(r): r for r in current.get("waiting_for", [])}
    for key in prev_missing.keys() - cur_missing.keys():
        changes.append(f"Resolved: {prev_missing[key]}")
    for key in cur_missing.keys() - prev_missing.keys():
        changes.append(f"Now waiting for: {cur_missing[key]}")

    if not changes:
        return None

    return {
        "type": "update",
        "message": " | ".join(changes),
        "changes": changes,
        "decision": current["decision"],
        "score": current["score"],
        "previous_decision": previous["decision"],
        "previous_score": previous["score"],
    }
