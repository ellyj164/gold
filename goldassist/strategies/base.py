"""The strategy library: one detector per way of trading Gold.

Why this package exists
-----------------------

The application used to derive at most one setup per direction. Every idea it
could ever have came out of a single classifier -- ``classify.setup_type`` --
which picked *one* label (pullback, breakout, reversal, range, continuation)
from an if-chain and threw the alternatives away. That is not how a desk works.
At any moment Gold may simultaneously offer a pullback into H1 demand, an
unmitigated M15 order block above it, a sweep of yesterday's low and an Asian
range break -- four different trades, three of which the old design could not
see because the fourth won an if-statement.

A strategy here is therefore a *detector*: it is handed the analysis and
answers one question -- "does my particular pattern exist right now, and how
convincing is it?" -- independently of every other detector. Every strategy
runs on every cycle. Nothing competes for a single slot.

Two rules hold across the whole package:

1. **A strategy measures; it never invents.** Every zone edge, stop and target
   a detector returns comes from something the analysis engines actually found
   -- a swing, a zone, a gap, a level, a session extreme, an indicator value.
   A detector that cannot find its pattern returns nothing at all.
2. **Conviction is the strategy's own read, not the market's.** It answers
   "how good an example of *this pattern* is this?" on 0-100. Broader context
   -- higher-timeframe agreement, penalties, positioning -- is applied later by
   the book, which blends conviction with the confluence score for that side.
   Keeping the two separate is what lets a mean-reversion strategy score its
   own setup honestly instead of being flattened by a trend-weighted score.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# How a strategy relates to the higher-timeframe direction. This decides how
# much of the final score comes from the confluence engine, which is built
# around trend agreement and would otherwise bury every mean-reversion idea.
WITH_TREND = "with"      # only valid in the direction D1/H4 already point
EITHER = "either"        # works with or against the higher timeframes
COUNTER = "counter"      # deliberately fades the prevailing move

# Weight given to the confluence score (the rest comes from conviction).
CONTEXT_WEIGHT = {WITH_TREND: 0.50, EITHER: 0.38, COUNTER: 0.25}

# Families group strategies for the interface and for the performance record.
SMC = "smart money"
TREND = "trend"
RANGE = "range"
PATTERN = "pattern"


@dataclass
class ZoneHint:
    """One measured area behind a candidate's entry zone."""

    kind: str
    timeframe: str
    low: float
    high: float
    quality: float
    label: str


@dataclass
class Candidate:
    """One setup a strategy has found, before the book turns it into a signal."""

    key: str                       # strategy key, e.g. "sweep_reversal"
    name: str                      # human strategy name
    family: str
    direction: str                 # bullish | bearish
    trigger_family: str            # which confirmation rules apply
    label: str                     # card label, e.g. "Bullish Sweep Reversal"
    conviction: float              # 0-100, this strategy's own read
    alignment: str = EITHER
    evidence: list[str] = field(default_factory=list)
    zone: tuple[float, float] | None = None
    preferred: float | None = None
    stop: float | None = None
    stop_basis: str = ""
    targets: list[dict] = field(default_factory=list)
    sources: list[ZoneHint] = field(default_factory=list)
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "name": self.name,
            "family": self.family,
            "direction": self.direction,
            "label": self.label,
            "conviction": round(self.conviction, 1),
            "alignment": self.alignment,
            "evidence": list(self.evidence),
            "note": self.note,
        }


@dataclass
class StrategyDef:
    key: str
    name: str
    family: str
    alignment: str
    min_conviction: float
    description: str
    detector: object


REGISTRY: dict[str, StrategyDef] = {}


def strategy(key: str, name: str, family: str, alignment: str = EITHER,
             min_conviction: float = 50.0, description: str = ""):
    """Register a detector. The function takes a Context and returns candidates."""

    def decorate(fn):
        REGISTRY[key] = StrategyDef(key=key, name=name, family=family,
                                    alignment=alignment,
                                    min_conviction=min_conviction,
                                    description=description or (fn.__doc__ or "").strip(),
                                    detector=fn)
        return fn

    return decorate


class Context:
    """Everything a detector may read, with the tedious lookups done once."""

    def __init__(self, analysis: dict, decision: dict, profile, frames: dict | None = None):
        self.analysis = analysis
        self.decision = decision or {}
        self.profile = profile
        self.frames = frames or {}
        self.price = float(analysis.get("price") or 0.0)
        self.atr = float(analysis.get("atr") or 0.0) or max(self.price * 0.002, 0.01)
        self.timeframes = analysis.get("timeframes") or {}
        self.levels = analysis.get("levels") or {}
        self.liquidity = analysis.get("liquidity") or {}
        self.regime = (analysis.get("regime") or {}).get("regime", "")
        self.alignment = analysis.get("alignment") or {}
        # The sub-minute tape. Always a dict; ``available`` is False when the
        # broker served no ticks, and detectors must treat that as "no
        # opinion" rather than as the tape disagreeing.
        self.micro = analysis.get("micro") or {}

    # -- lookups ------------------------------------------------------------

    def tf(self, name: str) -> dict:
        return self.timeframes.get(name) or {}

    def ind(self, timeframe: str, name: str) -> dict:
        return (self.tf(timeframe).get("indicators") or {}).get(name) or {}

    def struct(self, timeframe: str) -> dict:
        return self.tf(timeframe).get("structure") or {}

    def bias(self, timeframe: str) -> str:
        return self.tf(timeframe).get("bias", "neutral")

    def atr_of(self, timeframe: str) -> float:
        value = float(self.tf(timeframe).get("atr") or 0.0)
        return value or self.atr

    def frame(self, timeframe: str):
        return self.frames.get(timeframe)

    # -- context questions detectors ask constantly -------------------------

    def side_score(self, direction: str) -> float:
        """The confluence score for one side, 0-100."""
        confluence = self.decision.get("confluence") or {}
        side = confluence.get(direction) or {}
        return float(side.get("score", 0.0))

    def htf_agrees(self, direction: str) -> bool:
        return str(self.alignment.get("higher_timeframe_bias", "")).startswith(direction)

    def htf_opposes(self, direction: str) -> bool:
        bias = str(self.alignment.get("higher_timeframe_bias", ""))
        return bias.startswith(opposite(direction))

    def trend_agrees(self, timeframe: str, direction: str) -> bool:
        return self.struct(timeframe).get("trend") == direction

    def events(self, timeframe: str, direction: str, kinds: tuple[str, ...]) -> list[dict]:
        """Recent structure events on a timeframe, in one direction."""
        recent = self.struct(timeframe).get("recent_events") or []
        return [e for e in recent
                if e.get("direction") == direction and e.get("kind") in kinds]

    def patterns(self, timeframe: str, direction: str) -> list[dict]:
        candles = self.tf(timeframe).get("candles") or {}
        return [p for p in (candles.get("patterns") or [])
                if p.get("direction") == direction]

    def tape_agrees(self, direction: str) -> float | None:
        """0-1: whether the tape is pushing this way right now, or None.

        None means the tape could not be read, which is not the same as a flat
        tape (0.5). Callers must keep the distinction: a setup should never
        score lower because the feed is quiet on tick history.
        """
        from ..analysis import micro

        return micro.agrees(self.micro, direction)

    def distance_atr(self, level: float) -> float:
        return abs(self.price - float(level)) / self.atr

    def ahead(self, level: float, direction: str) -> bool:
        """Whether a price sits in front of spot for this direction."""
        return level > self.price if direction == "bullish" else level < self.price

    def behind(self, level: float, direction: str) -> bool:
        """Whether a price sits behind spot -- where a pullback entry waits."""
        return level <= self.price if direction == "bullish" else level >= self.price

    def clear_run(self, direction: str, distance: float, strength: int = 60) -> bool:
        """No strong opposing level within ``distance`` in front of price.

        Used by detectors that project a target rather than reading one off a
        level: a 50-pip objective through the middle of H4 resistance is not an
        objective, it is wishful thinking.
        """
        wanted = "resistance" if direction == "bullish" else "support"
        for level in (self.levels.get("levels") or []):
            if level.get("kind") != wanted or level.get("strength", 0) < strength:
                continue
            price = float(level["price"])
            if not self.ahead(price, direction):
                continue
            if abs(price - self.price) < distance * 0.8:
                return False
        return True


def detect_all(ctx: Context, enabled: tuple[str, ...] | None = None) -> list[Candidate]:
    """Run every registered strategy and return everything they found.

    A detector that raises is skipped and logged: one broken pattern must never
    silence the other twenty.
    """
    found: list[Candidate] = []
    for key, definition in REGISTRY.items():
        if enabled is not None and key not in enabled:
            continue
        try:
            produced = definition.detector(ctx) or []
        except Exception:
            log.warning("Strategy %s failed on this cycle", key, exc_info=True)
            continue
        for candidate in produced:
            if candidate is None:
                continue
            candidate.key = candidate.key or key
            candidate.name = candidate.name or definition.name
            candidate.family = candidate.family or definition.family
            candidate.alignment = candidate.alignment or definition.alignment
            if candidate.conviction < definition.min_conviction:
                continue
            if not _sane(candidate, ctx):
                continue
            found.append(candidate)
    found.sort(key=lambda c: -c.conviction)
    return found


def _sane(candidate: Candidate, ctx: Context) -> bool:
    """Reject anything geometrically impossible before it becomes a signal."""
    if candidate.direction not in ("bullish", "bearish"):
        return False
    if candidate.zone is not None:
        low, high = candidate.zone
        if not low or not high or high < low:
            return False
        # An entry zone must be reachable. Six ATR away is a different setup.
        if min(abs(ctx.price - low), abs(ctx.price - high)) > ctx.atr * 6:
            return False
    if candidate.stop is not None and candidate.zone is not None:
        low, high = candidate.zone
        if candidate.direction == "bullish" and candidate.stop >= low:
            return False
        if candidate.direction == "bearish" and candidate.stop <= high:
            return False
    return True


def describe_all() -> list[dict]:
    """The registry, for the interface and the settings screen."""
    return [
        {
            "key": d.key,
            "name": d.name,
            "family": d.family,
            "alignment": d.alignment,
            "min_conviction": d.min_conviction,
            "description": " ".join((d.description or "").split()),
        }
        for d in sorted(REGISTRY.values(), key=lambda s: (s.family, s.name))
    ]


# --- helpers shared by the detectors ---------------------------------------


def band(centre: float, width: float) -> tuple[float, float]:
    """A symmetrical price band of ``width`` around ``centre``."""
    return (centre - width / 2, centre + width / 2)


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def mix(*parts: tuple[float, float]) -> float:
    """Weighted 0-1 components combined into a 0-100 conviction.

    ``mix((0.4, 0.9), (0.6, 0.5))`` -> 66. Weights need not sum to one; they
    are normalised, so a detector can simply drop a component it could not
    measure rather than guessing a neutral value for it.
    """
    total_weight = sum(w for w, _ in parts if w > 0)
    if total_weight <= 0:
        return 0.0
    value = sum(w * clamp(v, 0.0, 1.0) for w, v in parts if w > 0) / total_weight
    return clamp(value * 100)


def label_for(name: str, direction: str) -> str:
    return ("Bullish " if direction == "bullish" else "Bearish ") + name


def opposite(direction: str) -> str:
    return "bearish" if direction == "bullish" else "bullish"


def stop_beyond(price: float, direction: str, buffer: float) -> float:
    """A stop placed the safe side of a level, by ``buffer``."""
    return price - buffer if direction == "bullish" else price + buffer
