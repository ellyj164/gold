"""Setup classification and entry triggers.

The specification separates three kinds of intraday opportunity -- pullback,
breakout and reversal -- and asks for a different confirmation trigger for
each. That distinction matters: a reversal must clear a higher bar than a
continuation, and a breakout must not be taken simply because price touched a
level.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict

PULLBACK = "pullback"
BREAKOUT = "breakout"
REVERSAL = "reversal"
CONTINUATION = "continuation"
RANGE = "range"

# Human labels used on the signal card.
SETUP_LABELS = {
    PULLBACK: "Bullish Pullback",
    BREAKOUT: "Breakout",
    REVERSAL: "Reversal",
    CONTINUATION: "Trend Continuation",
    RANGE: "Range Rotation",
}


@dataclass
class Trigger:
    """What must happen before the setup counts as confirmed."""

    description: str
    requirements: list[str]
    met: bool
    met_by: str | None = None
    outstanding: list[str] = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["outstanding"] = self.outstanding or []
        return d


def setup_type(analysis: dict, direction: str) -> str:
    """Classify what kind of opportunity this is."""
    tfs = analysis.get("timeframes", {})
    regime = (analysis.get("regime") or {}).get("regime", "")
    h1 = tfs.get("H1", {})
    h1_state = (h1.get("structure") or {}).get("state", "")
    alignment = analysis.get("alignment", {})
    htf_agrees = alignment.get("higher_timeframe_bias", "").startswith(direction)

    sweep = (analysis.get("liquidity") or {}).get("latest_sweep")
    recent_sweep = bool(sweep and sweep.get("direction") == direction)

    # A character change against the prior trend, backed by a sweep, is the
    # reversal case -- and the specification wants it held to a higher bar.
    for tf in ("M30", "M15", "M5"):
        last = ((tfs.get(tf) or {}).get("structure") or {}).get("last_event") or {}
        if last.get("kind") in ("CHoCH", "MSS") and last.get("direction") == direction:
            if recent_sweep or not htf_agrees:
                return REVERSAL

    if regime in ("range", "consolidation"):
        return RANGE

    if htf_agrees and "retracement" in h1_state:
        return PULLBACK

    # Price pressing into a strong level with expansion behind it.
    level_key = "nearest_resistance" if direction == "bullish" else "nearest_support"
    level = (analysis.get("levels") or {}).get(level_key)
    atr = analysis.get("atr") or 1
    if level and regime == "expansion":
        if abs(level["price"] - analysis["price"]) / atr < 1.0 and level["strength"] >= 55:
            return BREAKOUT

    if htf_agrees:
        return CONTINUATION
    return RANGE


def label(kind: str, direction: str) -> str:
    """Card label, e.g. 'Bullish Pullback' / 'Bearish Reversal'."""
    side = "Bullish" if direction == "bullish" else "Bearish"
    if kind == PULLBACK:
        return f"{side} Pullback"
    if kind == BREAKOUT:
        return f"{side} Breakout"
    if kind == REVERSAL:
        return f"{side} Reversal"
    if kind == CONTINUATION:
        return f"{side} Continuation"
    return f"{side} Range Rotation"


def trigger_for(kind: str, analysis: dict, direction: str,
                confirmation_tf: str = "M5") -> Trigger:
    """Build the confirmation requirement for this setup type, and check it.

    Requirements are evaluated against what the engines have actually seen, so
    a trigger reports itself met only on evidence -- never on price merely
    having arrived somewhere.
    """
    tfs = analysis.get("timeframes", {})
    conf = tfs.get(confirmation_tf, {})
    struct = conf.get("structure") or {}
    recent = struct.get("recent_events", []) or []
    candles = conf.get("candles") or {}

    has_bos = any(e["kind"] == "BOS" and e["direction"] == direction for e in recent)
    has_shift = any(e["kind"] in ("CHoCH", "MSS") and e["direction"] == direction
                    for e in recent)
    matching_patterns = [p for p in candles.get("patterns", [])
                         if p["direction"] == direction]
    has_rejection = any(
        p["name"] in ("hammer", "bullish pin bar", "shooting star", "inverted hammer",
                      "bullish engulfing", "bearish engulfing", "morning star",
                      "evening star", "failed breakout candle", "failed breakdown candle")
        for p in matching_patterns
    )
    has_displacement = any("displacement" in p["name"] or p["name"] == "momentum candle"
                           for p in matching_patterns)

    sweep = (analysis.get("liquidity") or {}).get("latest_sweep")
    reclaimed = bool(sweep and sweep.get("direction") == direction)

    side = "bullish" if direction == "bullish" else "bearish"

    if kind == REVERSAL:
        # Deliberately the strictest: a single rejection candle is never enough.
        requirements = [
            (f"{confirmation_tf} {side} CHoCH or MSS", has_shift),
            ("a liquidity sweep in this direction", reclaimed),
            (f"{confirmation_tf} {side} BOS confirming the shift", has_bos),
        ]
        description = (f"{confirmation_tf} {side} structure shift after a liquidity "
                       f"sweep, then a {side} BOS")
    elif kind == BREAKOUT:
        requirements = [
            (f"a {side} candle closing beyond the level", has_displacement),
            (f"{confirmation_tf} {side} BOS", has_bos),
            ("a successful retest of the broken level", has_rejection),
        ]
        description = (f"a close beyond the level, then a {confirmation_tf} {side} "
                       "BOS and a successful retest")
    elif kind == RANGE:
        requirements = [
            (f"a {side} rejection candle at the range edge", has_rejection),
            (f"{confirmation_tf} {side} BOS", has_bos),
        ]
        description = f"{side} rejection at the range edge, then a {confirmation_tf} BOS"
    else:  # pullback and continuation
        requirements = [
            (f"{confirmation_tf} {side} BOS", has_bos),
            (f"a {side} rejection or displacement candle", has_rejection or has_displacement),
        ]
        description = f"{confirmation_tf} {side} BOS and retest"

    outstanding = [text for text, ok in requirements if not ok]
    met = not outstanding
    met_by = None
    if met:
        met_by = "; ".join(text for text, _ in requirements)

    return Trigger(
        description=description,
        requirements=[text for text, _ in requirements],
        met=met,
        met_by=met_by,
        outstanding=outstanding,
    )
