"""Top-down method compliance.

The method this application is built to support is stated plainly:

    Use D1 and H4 to find the overall market direction and the major
    support/resistance zones. Then drop to H1, M30 or M15 to time the actual
    intraday entry *in the direction of the H4/D1 bias*.

The engines already measure all six timeframes, and the confluence engine
already rewards higher-timeframe alignment. What was missing was any explicit
statement of whether a given setup actually obeys the method -- so a trader
could see, in one line, "this is a long, D1 and H4 are both bullish, you are
timing it on M15, this is with the bias".

That distinction matters most for the setups that *don't* comply. A
counter-bias trade is not forbidden; intraday reversals off a major H4 zone are
a legitimate trade and the Reversal Hunter profile exists precisely to find
them. But the trader should know they are taking one, rather than discovering
it afterwards.

This module classifies, it does not block.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..config import BIAS_TIMEFRAMES, EXECUTION_TIMEFRAMES

WITH_BIAS = "with bias"
COUNTER_BIAS = "counter-bias"
NO_BIAS = "no clear bias"


@dataclass
class TopDownRead:
    bias: str                              # bullish | bearish | neutral
    bias_agreement: str                    # aligned | split | neutral
    bias_reads: dict = field(default_factory=dict)
    zones: list[dict] = field(default_factory=list)
    execution_reads: dict = field(default_factory=dict)
    execution_timeframe: str | None = None
    compliance: str = NO_BIAS
    headline: str = ""
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "bias": self.bias,
            "bias_agreement": self.bias_agreement,
            "bias_reads": self.bias_reads,
            "bias_timeframes": list(BIAS_TIMEFRAMES),
            "execution_timeframes": list(EXECUTION_TIMEFRAMES),
            "execution_reads": self.execution_reads,
            "execution_timeframe": self.execution_timeframe,
            "zones": self.zones,
            "compliance": self.compliance,
            "headline": self.headline,
            "notes": self.notes,
        }


def _direction_of(read: str) -> str:
    if read.startswith("bullish"):
        return "bullish"
    if read.startswith("bearish"):
        return "bearish"
    return "neutral"


def bias_from(analysis: dict) -> tuple[str, str, dict]:
    """The D1/H4 direction, and how firmly the two agree."""
    timeframes = analysis.get("timeframes") or {}
    reads = {tf: (timeframes.get(tf) or {}).get("bias", "neutral")
             for tf in BIAS_TIMEFRAMES if tf in timeframes}
    directions = [_direction_of(r) for r in reads.values()]
    decided = [d for d in directions if d != "neutral"]

    if not decided:
        return "neutral", "neutral", reads
    if len(set(decided)) == 1:
        # Both higher timeframes pointing the same way, or one pointing and the
        # other flat. The first is a genuine alignment; the second is weaker.
        agreement = "aligned" if len(decided) == len(directions) else "partial"
        return decided[0], agreement, reads
    # D1 outranks H4 when they genuinely conflict: the larger picture wins, but
    # the conflict is reported rather than smoothed over.
    return reads and _direction_of(reads.get("D1", "neutral")) or "neutral", "split", reads


def major_zones(analysis: dict, price: float, limit: int = 4) -> list[dict]:
    """The major zones D1 and H4 mark, which is what step one is actually for."""
    out: list[dict] = []
    timeframes = analysis.get("timeframes") or {}
    for tf in BIAS_TIMEFRAMES:
        data = timeframes.get(tf) or {}
        for zone in ((data.get("zones") or {}).get("active") or []):
            top, bottom = zone.get("top"), zone.get("bottom")
            if top is None or bottom is None:
                continue
            out.append({
                "timeframe": tf,
                "kind": zone.get("kind", "zone"),
                "low": round(float(bottom), 2),
                "high": round(float(top), 2),
                "quality": zone.get("quality"),
                "side": "above" if (top + bottom) / 2 > price else "below",
                "distance": round(abs((top + bottom) / 2 - price), 2),
            })
    out.sort(key=lambda z: z["distance"])
    return out[:limit]


def execution_timeframe(analysis: dict, direction: str) -> tuple[str | None, dict]:
    """Which execution timeframe currently agrees with the setup direction.

    The entry is timed on the fastest execution timeframe that has already
    turned in the traded direction -- which is what "drop down to time the
    entry" means in practice.
    """
    timeframes = analysis.get("timeframes") or {}
    reads = {tf: (timeframes.get(tf) or {}).get("bias", "neutral")
             for tf in EXECUTION_TIMEFRAMES if tf in timeframes}
    for tf in reversed(EXECUTION_TIMEFRAMES):        # M15, then M30, then H1
        if _direction_of(reads.get(tf, "neutral")) == direction:
            return tf, reads
    return None, reads


def assess(analysis: dict, suggestion=None) -> TopDownRead:
    """Read the method top-down, and say whether a setup complies with it."""
    price = float(analysis.get("price") or 0.0)
    bias, agreement, reads = bias_from(analysis)
    zones = major_zones(analysis, price)

    direction = getattr(suggestion, "direction", None) or bias
    tf, execution_reads = execution_timeframe(analysis, direction)

    notes: list[str] = []
    if agreement == "split":
        notes.append("D1 and H4 disagree. Until they resolve, intraday setups "
                     "in either direction are counter to one of them.")
    elif agreement == "partial":
        notes.append("Only one of D1 and H4 has committed to a direction, so "
                     "the bias is real but not yet firm.")

    if suggestion is None:
        compliance = NO_BIAS if bias == "neutral" else WITH_BIAS
        headline = (f"D1/H4 bias is {bias}."
                    if bias != "neutral"
                    else "D1 and H4 are not pointing anywhere yet.")
        return TopDownRead(bias, agreement, reads, zones, execution_reads, tf,
                           compliance, headline, notes)

    if bias == "neutral":
        compliance = NO_BIAS
        headline = (f"This {suggestion.direction} setup has no higher-timeframe "
                    "bias behind it — D1 and H4 are undecided.")
        notes.append("With no D1/H4 direction, the method's first step has not "
                     "produced an answer. Anything taken here is being timed "
                     "without a bias to time it against.")
    elif suggestion.direction == bias:
        compliance = WITH_BIAS
        where = f" and timed on {tf}" if tf else ""
        headline = (f"With the bias: D1/H4 are {bias}, this is a "
                    f"{suggestion.direction} setup{where}.")
        if tf is None:
            notes.append("No execution timeframe has turned in this direction "
                         "yet, so the entry is early relative to the method.")
    else:
        compliance = COUNTER_BIAS
        headline = (f"Counter-bias: D1/H4 are {bias} but this is a "
                    f"{suggestion.direction} setup.")
        notes.append("Counter-bias intraday trades off a major zone are a real "
                     "trade, but they need more evidence and they usually need "
                     "to be taken smaller and managed faster.")

    return TopDownRead(bias, agreement, reads, zones, execution_reads, tf,
                       compliance, headline, notes)
