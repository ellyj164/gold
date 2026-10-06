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

**It refuses to point both ways at one price.** Two setups may disagree about
where gold is going -- that is what a range is, and selling the highs down to
support while planning to buy that support is one coherent plan, not a
contradiction. What they may not do is ask for a long and a short *in the same
entry zone*: whichever way price then moves, one of them is stopped out by the
move that fills the other, so the pair cannot both be acted on and says nothing
at all. ``_contradictions`` compares built entry zones, so disjoint scenarios are
left alone and only genuine same-price conflicts are arbitrated.

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

import logging
from datetime import datetime, timedelta, timezone

from .. import strategies
from ..trader import expectancy
from . import engine as signal_engine
from . import resolve
from .engine import Suggestion
from .states import ATTENTION_ORDER, PROTECTED, TERMINAL, WINNING

log = logging.getLogger(__name__)

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

# How much better a new setup has to be before it may displace a live setup
# pointing the opposite way at the same price.
#
# Without a margin the two sides trade places every few cycles as their scores
# drift past each other, and the board flickers between "buy here" and "sell
# here" -- which is the confusion this rule exists to end, arriving one setup
# at a time instead of two at once. A setup that has already been shown to the
# trader keeps its place until something clearly better turns up.
CONFLICT_MARGIN = 6.0

# How much of the narrower entry zone two opposite setups may share before they
# are treated as the same price. Zones are built from ATR, so this scales with
# volatility on its own.
CONFLICT_TOLERANCE = 0.25

# How old a filled trade may be and still be resumed after a restart. Past
# this the broker's M1 history may not reach back to its fill, and a trade that
# cannot be walked from its fill cannot be resolved honestly.
RESTORE_FILLED_DAYS = 7

# Two same-direction setups whose entries sit within this many ATR of each
# other, or that filled within ``EXPOSURE_MINUTES`` of each other, are treated
# as one exposure to one move. Owned by the resolver, which refuses the fill.
EXPOSURE_ATR = resolve.EXPOSURE_ATR
EXPOSURE_MINUTES = resolve.EXPOSURE_MINUTES

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

    # -- resuming across a restart -------------------------------------------

    def restore(self, cards: list[dict], profile,
                now: datetime | None = None) -> list[Suggestion]:
        """Put setups saved by a previous run back on the board.

        The book lives in memory. Without this, closing the application
        stranded every live setup: the next run started with an empty board,
        nothing ever resolved them, and their log rows stayed open forever --
        including the ones that had actually filled, whose results were simply
        lost to the record.

        Setups older than the profile's own expiry window are not restored.
        They would be retired unfilled on the first cycle anyway, and doing it
        here rather than through the board keeps a restart from announcing a
        dozen stale expiries as though they had just happened. A card that
        cannot be rebuilt is skipped rather than guessed at.

        Returns what was restored, so the caller can leave those log rows alone
        and reconcile the rest.
        """
        clock = now or datetime.now(timezone.utc)
        limit = float(getattr(profile, "trade_expiry_minutes", 720) or 0) or 720.0
        restored: list[Suggestion] = []
        for card in cards:
            if len(restored) >= self.limit:
                break
            try:
                suggestion = Suggestion.restore(card)
            except Exception as exc:
                # A card too old or too damaged to rebuild is skipped by
                # design, so this is a one-line note rather than a traceback:
                # the setup is simply not resumed, and its log row is
                # reconciled like any other the book is not holding.
                log.warning("Skipping unreadable setup card %s (%s)",
                            card.get("id"), exc)
                continue
            if suggestion.status in TERMINAL or suggestion.outcome:
                continue
            age = _age_minutes(suggestion.created_at, clock)
            # A trade that had *filled* comes back whatever its age. Dropping
            # it left the log row "unresolved" -- a real trade with no result --
            # and three of the first twenty-one live fills ended that way. It
            # is resumed so the caller can walk the candles it missed and close
            # it on what price actually did, which is an observation, not an
            # invented outcome. Only an unfilled idea may be let go on age.
            if not suggestion.entered and (age is None or age > limit):
                continue
            if suggestion.entered and (age is None or age > RESTORE_FILLED_DAYS * 1440):
                continue
            if any(s.id == suggestion.id for s in self.live):
                continue
            self.live.append(suggestion)
            restored.append(suggestion)
        return restored

    # -- the per-cycle update ------------------------------------------------

    def sync(self, analysis: dict, decision: dict, profile, bars=None,
             frames: dict | None = None, edge: dict | None = None,
             now: datetime | None = None, costs=None) -> dict:
        """Advance every tracked setup and admit any new ones.

        ``bars`` are the finest candles available, used to resolve fills,
        targets and stops accurately. ``frames`` are the per-timeframe candles
        the strategies may need. ``edge`` is the trader's own record per
        strategy, which nudges conviction within a bounded range. ``costs`` is
        the transaction-cost model new setups are built and resolved against;
        None leaves costs unmodelled and every figure gross.

        ``now`` overrides the clock, which the backtester needs: a replay of
        last month must age its setups on the replay's calendar, not on the
        wall clock of the machine running it.

        Returns the events worth alerting on, the setups newly identified on
        this cycle so the caller can log them, and the ones that finished so
        the caller can record their outcomes.
        """
        clock = now or datetime.now(timezone.utc)
        # Held on the book for the duration of the cycle so ``_admit`` -- which
        # is several calls down and has no business growing another parameter
        # -- can hand it to every setup it builds.
        self._costs = costs
        # A setup restored from the database comes back without one: the model
        # is an object, and only its reading was written down. Re-attaching it
        # here means a setup that survives a restart is still resolved net.
        if costs is not None:
            for suggestion in self.live:
                if getattr(suggestion, "costs", None) is None:
                    suggestion.costs = costs
        events: list[tuple[Suggestion, str]] = []
        finished: list[Suggestion] = []
        # Setups admitted on this cycle. Reported separately from ``events``
        # because the caller has to *log* them, and deriving "this one is new"
        # from the wording of an alert message is not something a persistence
        # layer should ever be asked to do.
        fired: list[Suggestion] = []
        still_live: list[Suggestion] = []

        # What the resolver checks a fill against: every setup on the board,
        # filled or not -- one that fills earlier in this loop is open exposure
        # for the ones after it -- and the filled trades that closed recently,
        # which may still have been open at a candle this cycle walks.
        peers = list(self.live) + [s for s in self.retired if s.entered]
        atr = float(analysis.get("atr") or 0.0)

        for suggestion in self.live:
            suggestion.peers = peers
            suggestion.peer_atr = atr
            updated, changes = signal_engine.update(
                suggestion, analysis, decision, profile, bars=bars, now=clock)
            if updated.status not in TERMINAL:
                cancellation = self._premise_gone(updated, decision, profile)
                if cancellation:
                    changes.append(resolve.cancel(updated, cancellation))
            for change in changes:
                events.append((updated, change))
            if updated.status in TERMINAL:
                # A retired setup holding the board it was retired from would
                # keep every older setup alive through it, cycle after cycle.
                updated.peers = []
                finished.append(updated)
                self.retired.insert(0, updated)
                self._remember(updated, clock)
            else:
                still_live.append(updated)

        self.live = still_live
        del self.retired[HISTORY:]
        self._forget_old(clock)

        # Setups retired because a stronger setup pointing the other way took
        # their price. Collected here rather than inside ``_admit`` because
        # whether one was ever shown to the trader decides how it is recorded.
        displaced: list[Suggestion] = []

        for candidate in self._candidates(analysis, decision, profile, frames, edge):
            admitted, note = self._admit(candidate, analysis, decision, profile,
                                         clock, displaced)
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

        for gone in displaced:
            if any(f is gone for f in fired):
                # Built and beaten on the same cycle by the opposite side of
                # the same picture. It never reached the trader, so it is not
                # a setup that happened and must not be logged as one.
                fired[:] = [f for f in fired if f is not gone]
                events[:] = [e for e in events if e[0] is not gone]
                continue
            self.retired.insert(0, gone)
            self._remember(gone, clock)
            finished.append(gone)
            events.append((gone, f"Setup superseded — {gone.exit_note}"))
        del self.retired[HISTORY:]

        self._mark_exposure(analysis, clock, finished)

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
        disabled = set(getattr(profile, "disabled_strategies", ()) or ())
        if disabled:
            found = [c for c in found if c.key not in disabled]
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
               clock: datetime,
               displaced: list[Suggestion] | None = None
               ) -> tuple[Suggestion | None, str]:
        """Track a candidate, merge it into an existing setup, or drop it.

        ``displaced`` collects any live setup this candidate took the price of,
        for the caller to retire. It is optional so the backtester and the
        tests may call ``_admit`` without one.
        """
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
                                             now=clock,
                                             costs=getattr(self, "_costs", None))
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

        # Nothing may point the opposite way at the same price. Every conflict
        # is collected, not just the first: a setup that displaced one live
        # opposite and left a second standing would publish the contradiction
        # this rule exists to prevent.
        blockers = self._contradictions(suggestion)
        if any(not _supersedes(suggestion, b) for b in blockers):
            return None, "contradicted"
        for blocker in blockers:
            resolve.cancel(
                blocker,
                f"a {suggestion.action} setup at the same price scores "
                f"{suggestion.score:.0f}/100 against its {blocker.score:.0f}")
            self.live[:] = [s for s in self.live if s is not blocker]
            if displaced is not None:
                displaced.append(blocker)

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

    def _mark_exposure(self, analysis: dict, clock: datetime,
                       finished: list[Suggestion] | None = None) -> None:
        """Tell every live setup what same-direction risk is already on.

        Four longs filling between 13:30 and 13:53 on 2026-09-09 were four
        cards and one bet: three of them lost together. Nothing is hidden
        here -- each setup still appears and still alerts. The card is told how
        many *filled* trades already point the same way at a nearby price or
        time, the desk tells the trader to skip it while they are open, and the
        resolver refuses its fill if price arrives before they close.
        """
        atr = float(analysis.get("atr") or 0.0)
        # Trades that filled and closed inside this one cycle -- routine in a
        # replay, which walks several candles per step -- still held exposure
        # while they were open, and still need their at-fill count frozen.
        closed_now = [s for s in (finished or []) if s.entered]
        filled = [s for s in self.live if s.entered] + closed_now
        for suggestion in self.live + [s for s in closed_now
                                       if s.exposure_at_fill is None]:
            related = [other for other in filled
                       if other is not suggestion
                       and other.direction == suggestion.direction
                       and _same_exposure(suggestion, other, atr, clock)]
            if suggestion.entered and suggestion.exposure_at_fill is None:
                # Frozen once, on the first cycle that sees the fill, counting
                # only trades that were already on when this one filled.
                suggestion.exposure_at_fill = sum(
                    1 for other in related
                    if other.filled_at and suggestion.filled_at
                    and str(other.filled_at) <= str(suggestion.filled_at))
            if not related:
                suggestion.exposure = {}
                continue
            suggestion.exposure = {
                "same_direction_open": len(related),
                "with": [o.id for o in related],
                "note": (f"{len(related)} filled {suggestion.direction} trade"
                         f"{'s' if len(related) != 1 else ''} already on nearby — "
                         "this is added exposure to the same move, not a new bet, "
                         "so it is skipped while that trade is open"),
            }
            # On the card too, where the trader reads the risks: the desk's
            # sizing only speaks for the setup in the banner.
            if not suggestion.entered and suggestion.exposure["note"] not in suggestion.risks:
                suggestion.risks.append(suggestion.exposure["note"])

    def _contradictions(self, suggestion: Suggestion) -> list[Suggestion]:
        """Every live setup that wants the opposite trade at this one's price."""
        return [existing for existing in self.live
                if existing.direction != suggestion.direction
                and _same_price(existing, suggestion)]

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
    """How much this strategy's own record should move its conviction.

    Delegates to :mod:`expectancy`, which shrinks the estimate toward "no
    edge" by sample size. That replaces the old hard cliff at eight trades:
    a strategy with seven contributed exactly nothing and with eight
    contributed its full raw average, which is the wrong shape for evidence.
    A shrunk estimate rises smoothly as the record earns it.
    """
    points, _ = expectancy.conviction_adjustment(edge, key)
    return points


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


def _same_price(a: Suggestion, b: Suggestion) -> bool:
    """Whether two setups would have the trader in the market at one price.

    Entry zones, not stops or targets, because entries are what the trader
    acts on. Two setups whose zones are disjoint are sequential legs of a
    range -- a short from the highs down to support and a long off that
    support are the same read stated twice -- and both belong on the board.
    Zones that overlap are the same price, and opposite directions there
    cannot both be right.
    """
    if a.entry_zone is None or b.entry_zone is None:
        return False
    tolerance = CONFLICT_TOLERANCE * min(a.entry_zone.high - a.entry_zone.low,
                                         b.entry_zone.high - b.entry_zone.low)
    return not (a.entry_zone.high + tolerance < b.entry_zone.low
                or a.entry_zone.low - tolerance > b.entry_zone.high)


def _supersedes(newcomer: Suggestion, existing: Suggestion) -> bool:
    """Whether a new setup may take the price a live setup is holding.

    Three rules, in order of how much they cost to get wrong:

    A **filled** setup is never displaced. Money is at risk in a direction, and
    an application that answers that by publishing the opposite trade is worse
    than one that stays quiet.

    A setup **further along its lifecycle** is not displaced by a fresher idea.
    A setup waiting on its confirmation candle is a trade the trader may be
    about to take; a DEVELOPING one scoring two points higher is not a reason
    to pull it off the board.

    Otherwise the **higher score** wins, by a margin -- except between two
    setups built on the same cycle, which the trader has not seen yet and
    which can therefore be ranked on the evidence alone.
    """
    if existing.entered:
        return False
    if ATTENTION_ORDER.get(existing.status, 0) > ATTENTION_ORDER.get(newcomer.status, 0):
        return False
    if newcomer.created_at == existing.created_at:
        return newcomer.score > existing.score
    return newcomer.score >= existing.score + CONFLICT_MARGIN


def _same_exposure(a: Suggestion, b: Suggestion, atr: float,
                   clock: datetime) -> bool:
    """Whether a filled trade ``b`` is exposure to the same move as ``a``.

    Near in price (entries within ``EXPOSURE_ATR``) or near in time (``b``
    filled within ``EXPOSURE_MINUTES``). Either is enough: two longs ten
    dollars apart filled a minute apart are one reaction to one print.
    """
    def entry_of(s: Suggestion) -> float | None:
        if s.fill_price is not None:
            return float(s.fill_price)
        return float(s.entry_zone.preferred) if s.entry_zone is not None else None

    ea, eb = entry_of(a), entry_of(b)
    if atr > 0 and ea is not None and eb is not None and abs(ea - eb) <= EXPOSURE_ATR * atr:
        return True
    age = _age_minutes(b.filled_at, clock) if b.filled_at else None
    return age is not None and 0 <= age <= EXPOSURE_MINUTES


def _zones_overlap(a: Suggestion, b: Suggestion) -> bool:
    if a.entry_zone is None or b.entry_zone is None:
        return False
    return not (a.entry_zone.high < b.entry_zone.low
                or a.entry_zone.low > b.entry_zone.high)


def _age_minutes(created_at: str, now: datetime) -> float | None:
    try:
        stamp = datetime.fromisoformat(str(created_at))
    except (TypeError, ValueError):
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return (now - stamp).total_seconds() / 60.0


def _attention(suggestion: Suggestion) -> tuple[int, float]:
    return ATTENTION_ORDER.get(suggestion.status, 0), suggestion.score
