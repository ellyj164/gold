"""The setup book: every setup currently in play, and what happened to the rest.

The service used to track a single suggestion. That was the wrong shape for an
intraday trader: a live setup that had not yet resolved *blocked* the creation
of any new one, so a WATCH setup sitting at 4380 could quietly suppress a
confirmed setup at 4395 for the rest of the morning. The cap on trades per day
was never a rule anyone wrote down -- it was an accident of the data structure.

The book now does three jobs.

**It asks every strategy, every cycle.** Candidates come from the strategy
library, not from a single classifier, so a pullback, an order block, a sweep
and a range break can all be on the board at once. Nothing competes for a slot.

**It merges agreement instead of duplicating it.** Two detectors pointing at
the same area in the same direction produce one setup with both arguments
attached and a slightly higher score -- which is what confluence means.

**It clears decided setups out of the way.** A setup that reaches its target or
its stop is resolved by :mod:`goldassist.signals.resolve`, retired with a
result in pips and R, and removed from the board. That is what frees the next
opportunity: the board shows what is live, and the history shows what happened.

The one restraint on re-admission is worth naming because it looks like a cap
and is not one. When a setup resolves, the *identical* idea -- same strategy,
same direction, same price area -- is not re-created for a short window unless
the evidence for it has actually improved. Without that, a stopped-out setup is
rebuilt five seconds later from the same unchanged inputs, and the book fills
with the same loss over and over. It is deduplication in time, exactly as
``_same_idea`` is deduplication in space. Nothing limits how many *different*
setups a day may produce, and nothing limits how many may run at once beyond
what the interface can legibly show.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .. import strategies
from . import engine as signal_engine
from . import resolve
from .engine import Suggestion
from .states import ATTENTION_ORDER, PROTECTED, TERMINAL, WINNING

# How many setups may be tracked at once. High enough that a busy session is
# never truncated in practice, bounded so the interface stays readable.
DEFAULT_LIMIT = 16

# How many finished setups to keep for the session summary.
HISTORY = 60

# How long an identical resolved idea waits before it may be re-admitted on
# unchanged evidence. See the module docstring: this is deduplication, not a
# limit on trading.
REPEAT_MINUTES = 20
# ...unless the strategy's own conviction has improved by at least this much,
# which means the market said something new.
REPEAT_OVERRIDE = 8.0

# Sample size below which the performance record is not allowed to move a score.
EDGE_MIN_SAMPLE = 8
# Most a strategy's own record may add to or take from its conviction.
EDGE_LIMIT = 6.0

# Most the live tape may add to or take from a candidate's conviction.
#
# Kept small on purpose. The tape says what price is doing in the last thirty
# seconds, which is worth something for timing and almost nothing for
# direction: a strategy that has measured an H4 order block should not be
# talked out of it by a minute of drift. It breaks ties; it does not decide.
TAPE_LIMIT = 4.0


class SuggestionBook:
    """Every setup currently being tracked, plus the ones that have finished."""

    def __init__(self, limit: int = DEFAULT_LIMIT):
        self.limit = limit
        self.live: list[Suggestion] = []
        self.retired: list[Suggestion] = []
        self._recent: list[dict] = []

    # -- accessors ----------------------------------------------------------

    @property
    def primary(self) -> Suggestion | None:
        """The setup that most deserves the trader's attention right now.

        Ranked by how far along the lifecycle it is first and by score second,
        so a confirmed 72 outranks a developing 84 -- the confirmed one is the
        trade that is actually available.
        """
        if not self.live:
            return None
        return max(self.live, key=_attention)

    def to_dict(self) -> dict:
        primary = self.primary
        return {
            "primary": primary.to_dict() if primary else None,
            "live": [s.to_dict() for s in sorted(self.live, key=_attention,
                                                 reverse=True)],
            "retired": [s.to_dict() for s in self.retired[:20]],
            "counts": self.counts(),
            "results": self.results(),
            "limit": self.limit,
        }

    def counts(self) -> dict:
        by_status: dict[str, int] = {}
        by_strategy: dict[str, int] = {}
        for suggestion in self.live:
            by_status[suggestion.status] = by_status.get(suggestion.status, 0) + 1
            by_strategy[suggestion.strategy] = by_strategy.get(suggestion.strategy, 0) + 1
        return {
            "live": len(self.live),
            "retired": len(self.retired),
            "by_status": by_status,
            "by_strategy": by_strategy,
            "confirmed": by_status.get("CONFIRMED", 0) + by_status.get("ACTIVE", 0),
            "entered": sum(1 for s in self.live if s.entered),
        }

    def results(self) -> dict:
        """What the resolved setups in this session were actually worth."""
        taken = [s for s in self.retired if s.entered]
        wins = [s for s in taken if s.status in WINNING]
        losses = [s for s in taken if s.outcome == "loss"]
        scratches = [s for s in taken if s.outcome == "scratch"]
        decided = len(wins) + len(losses)
        return {
            "resolved": len(self.retired),
            "taken": len(taken),
            "untaken": len(self.retired) - len(taken),
            "wins": len(wins),
            "losses": len(losses),
            "scratches": len(scratches),
            "pips": round(sum(s.result_pips for s in taken), 1),
            "r": round(sum(s.r_multiple for s in taken), 2),
            "win_rate": round(len(wins) / decided * 100, 1) if decided else None,
        }

    def find(self, suggestion_id: str) -> Suggestion | None:
        for suggestion in self.live + self.retired:
            if suggestion.id == suggestion_id:
                return suggestion
        return None

    # -- the per-cycle update ------------------------------------------------

    def sync(self, analysis: dict, decision: dict, profile, bars=None,
             frames: dict | None = None, edge: dict | None = None,
             now: datetime | None = None) -> dict:
        """Advance every tracked setup and admit any new ones.

        ``bars`` are the finest candles available, used to resolve fills,
        targets and stops accurately. ``frames`` are the per-timeframe candles
        the strategies may need. ``edge`` is the trader's own record per
        strategy, which nudges conviction within a bounded range.

        ``now`` overrides the clock, which the backtester needs: a replay of
        last month must age its setups on the replay's calendar, not on the
        wall clock of the machine running it.

        Returns the events worth alerting on, the setups newly identified on
        this cycle so the caller can log them, and the ones that finished so
        the caller can record their outcomes.
        """
        clock = now or datetime.now(timezone.utc)
        events: list[tuple[Suggestion, str]] = []
        finished: list[Suggestion] = []
        # Setups admitted on this cycle. Reported separately from ``events``
        # because the caller has to *log* them, and deriving "this one is new"
        # from the wording of an alert message is not something a persistence
        # layer should ever be asked to do.
        fired: list[Suggestion] = []
        still_live: list[Suggestion] = []

        for suggestion in self.live:
            updated, changes = signal_engine.update(
                suggestion, analysis, decision, profile, bars=bars, now=clock)
            if updated.status not in TERMINAL:
                cancellation = self._premise_gone(updated, decision, profile)
                if cancellation:
                    changes.append(resolve.cancel(updated, cancellation))
            for change in changes:
                events.append((updated, change))
            if updated.status in TERMINAL:
                finished.append(updated)
                self.retired.insert(0, updated)
                self._remember(updated, clock)
            else:
                still_live.append(updated)

        self.live = still_live
        del self.retired[HISTORY:]
        self._forget_old(clock)

        for candidate in self._candidates(analysis, decision, profile, frames, edge):
            admitted, note = self._admit(candidate, analysis, decision, profile, clock)
            if admitted is not None and note == "new":
                fired.append(admitted)
                events.append((admitted,
                               f"New {admitted.setup_label} setup identified by "
                               f"{admitted.strategy_name} "
                               f"({admitted.score:.0f}/100, {admitted.quality})"))
            elif admitted is not None and note == "merged":
                events.append((admitted,
                               f"{candidate.name} agrees with this setup — "
                               f"score now {admitted.score:.0f}/100"))

        return {"events": events, "finished": finished, "fired": fired,
                "live": list(self.live)}

    # -- candidate generation ------------------------------------------------

    def _candidates(self, analysis: dict, decision: dict, profile,
                    frames: dict | None, edge: dict | None) -> list:
        """Everything every enabled strategy can see right now."""
        context = strategies.Context(analysis, decision, profile, frames)
        enabled = getattr(profile, "enabled_strategies", None)
        found = strategies.detect_all(context,
                                      tuple(enabled) if enabled else None)
        for candidate in found:
            adjustment = 0.0
            if edge:
                adjustment += _edge_for(edge, candidate.key)
            adjustment += _tape_for(context, candidate)
            if adjustment:
                candidate.conviction = max(
                    0.0, min(100.0, candidate.conviction + adjustment))
        return found

    def _admit(self, candidate, analysis: dict, decision: dict, profile,
               clock: datetime) -> tuple[Suggestion | None, str]:
        """Track a candidate, merge it into an existing setup, or drop it."""
        # Same strategy, same area, already live: the same idea re-derived.
        for existing in self.live:
            if existing.strategy == candidate.key and _overlaps(existing, candidate):
                return existing, "duplicate"

        # A different strategy pointing at the same trade is confluence.
        for existing in self.live:
            if _overlaps(existing, candidate) and existing.direction == candidate.direction:
                before = existing.score
                signal_engine.merge(existing, candidate)
                return (existing, "merged") if existing.score > before else (existing, "duplicate")

        if self._too_soon(candidate, clock):
            return None, "repeat"

        try:
            suggestion = signal_engine.build(analysis, decision, profile, candidate,
                                             now=clock)
        except Exception:
            suggestion = None
        if suggestion is None:
            return None, "unbuildable"
        if suggestion.score < profile.watch_score:
            return None, "below watch"

        # Merge against what the built zone actually turned out to be: two
        # strategies can name different areas and still resolve to one trade.
        for existing in self.live:
            if existing.direction == suggestion.direction and _zones_overlap(existing, suggestion):
                if existing.strategy == suggestion.strategy:
                    return existing, "duplicate"
                before = existing.score
                signal_engine.merge(existing, candidate)
                return (existing, "merged") if existing.score > before else (existing, "duplicate")

        if len(self.live) < self.limit:
            self.live.append(suggestion)
            return suggestion, "new"

        # Full: displace the weakest setup nobody could be acting on yet.
        displaceable = [s for s in self.live
                        if s.status not in PROTECTED and not s.entered]
        if not displaceable:
            return None, "full"
        weakest = min(displaceable, key=lambda s: s.score)
        if weakest.score >= suggestion.score:
            return None, "full"
        self.live.remove(weakest)
        self.live.append(suggestion)
        return suggestion, "new"

    # -- retirement bookkeeping ---------------------------------------------

    def _premise_gone(self, suggestion: Suggestion, decision: dict, profile) -> str | None:
        """Whether an *unfilled* setup still has a reason to exist.

        A filled trade is never retired this way. Once money is at risk the
        stop and the target decide the outcome; an engine changing its mind
        does not close a position, and pretending otherwise produced outcome
        records that were neither wins nor losses.
        """
        if suggestion.entered:
            return None
        if suggestion.status == "WEAKENING" and suggestion.score < profile.watch_score * 0.8:
            return (f"conditions collapsed to {suggestion.score:.0f}/100 from a peak "
                    f"of {suggestion.peak_score:.0f}")
        return None

    def _remember(self, suggestion: Suggestion, clock: datetime) -> None:
        """Note a resolved idea so an unchanged copy is not rebuilt at once."""
        if suggestion.entry_zone is None:
            return
        self._recent.append({
            "strategy": suggestion.strategy,
            "direction": suggestion.direction,
            "low": suggestion.entry_zone.low,
            "high": suggestion.entry_zone.high,
            "conviction": suggestion.conviction,
            "until": clock + timedelta(minutes=REPEAT_MINUTES),
        })

    def _forget_old(self, clock: datetime) -> None:
        self._recent = [r for r in self._recent if r["until"] > clock]

    def _too_soon(self, candidate, clock: datetime) -> bool:
        if candidate.zone is None:
            return False
        low, high = candidate.zone
        for recent in self._recent:
            if recent["strategy"] != candidate.key:
                continue
            if recent["direction"] != candidate.direction:
                continue
            if high < recent["low"] or low > recent["high"]:
                continue
            if candidate.conviction >= recent["conviction"] + REPEAT_OVERRIDE:
                return False      # the evidence genuinely improved
            return True
        return False


# --- helpers ----------------------------------------------------------------


def _edge_for(edge: dict, key: str) -> float:
    """How much this strategy's own record should move its conviction."""
    record = (edge or {}).get(key) or {}
    if int(record.get("n", 0)) < EDGE_MIN_SAMPLE:
        return 0.0
    adjustment = float(record.get("avg_r", 0.0)) * 8.0
    return max(-EDGE_LIMIT, min(EDGE_LIMIT, adjustment))


def _tape_for(context, candidate) -> float:
    """How much the live tape should move this candidate's conviction.

    Returns 0.0 when there is no tape, so an account whose broker serves no
    tick history scores exactly as it did before this existed.
    """
    agreement = context.tape_agrees(candidate.direction)
    if agreement is None:
        return 0.0
    # 0.5 is a flat tape and must be worth nothing either way.
    return (agreement - 0.5) * 2 * TAPE_LIMIT


def _overlaps(existing: Suggestion, candidate) -> bool:
    """Whether a candidate describes the area a tracked setup already covers."""
    if existing.direction != candidate.direction:
        return False
    if candidate.zone is None or existing.entry_zone is None:
        return existing.strategy == candidate.key
    low, high = candidate.zone
    return not (existing.entry_zone.high < low or existing.entry_zone.low > high)


def _zones_overlap(a: Suggestion, b: Suggestion) -> bool:
    if a.entry_zone is None or b.entry_zone is None:
        return False
    return not (a.entry_zone.high < b.entry_zone.low
                or a.entry_zone.low > b.entry_zone.high)


def _attention(suggestion: Suggestion) -> tuple[int, float]:
    return ATTENTION_ORDER.get(suggestion.status, 0), suggestion.score
