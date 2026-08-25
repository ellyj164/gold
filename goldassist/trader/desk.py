"""The trading desk: everything a human does around the analysis.

The analysis engines answer "what is the market doing?" and the signals module
answers "where would I get in?". Neither answers the questions that separate a
profitable discretionary trader from an accurate one:

* Does this obey my top-down method?       -> :mod:`topdown`
* What is the market's character today?    -> :mod:`character`
* What has already happened this session?  -> :mod:`awareness`
* What is my view, and is it still intact? -> :mod:`thesis`
* Has this kind of setup worked for me?    -> :mod:`memory`
* What do I do now that I am in?           -> :mod:`management`
* Why is Gold moving?                      -> :mod:`macro`

This module runs all six on every cycle and folds them into one answer: a
conviction level, a suggested fraction of normal risk, and the reasoning behind
both.

**Nothing here withholds a setup.** The application never executes a trade, so
there is no risk to manage by hiding analysis from the trader. Every setup that
qualifies technically is built, scored, alerted and displayed, however many
have already appeared today and however the earlier ones went. What the desk
adds is context sitting alongside the setup -- and, when the trader is already
in a position, the management steps the application used to go silent about.

The technical score remains exactly what it was: the weight of technical
evidence. Conviction is a separate number beside it, and the only thing it
drives is a suggested size. It never edits, gates or invents a signal.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from . import (awareness, character, intermarket as intermarket_module,
               macro as macro_module, management, sessions, topdown)
from . import thesis as thesis_module
from .memory import Memory, score_band
from ..signals import states

log = logging.getLogger(__name__)

# How conviction maps onto position size. A human does not trade every setup
# the same size; these are the three sizes most traders actually use. This is a
# suggestion about sizing, never about whether to take the trade.
RISK_BANDS = (
    (0.85, 1.0, "full risk"),
    (0.60, 0.5, "half risk"),
    (0.0, 0.25, "quarter risk"),
)

# The memory report is recomputed on this cadence rather than every tick.
MEMORY_REFRESH_SECONDS = 120


class TradingDesk:
    """Composes the behavioural layer around one analysis service."""

    def __init__(self, db, source=None):
        self.db = db
        self.memory = Memory(db)
        self.dollar = macro_module.DollarContext(source) if source is not None else None
        # The dollar answers "is Gold moving"; this answers "why, and will it
        # last". Kept separate because the two degrade independently -- a
        # broker can carry a dollar index and no silver, or the reverse.
        self.intermarket = (intermarket_module.IntermarketContext(source)
                            if source is not None else None)

        self.thesis: thesis_module.Thesis | None = None
        # One managed trade per entered setup, keyed by suggestion id. An
        # intraday trader can easily have two positions running at once, so a
        # single slot here would have recreated the bottleneck the setup book
        # exists to remove.
        self.trades: dict[str, management.ManagedTrade] = {}
        self.state: dict = {}

        # The last setup that failed, so a repeat of the same idea can be
        # pointed out -- not blocked.
        self._last_failure: dict | None = None
        self._memory_report = None
        self._memory_at: datetime | None = None
        self._events: list[str] = []

    # -- the per-cycle assessment ------------------------------------------

    def assess(self, analysis: dict, decision: dict, book, frames: dict,
               profile) -> dict:
        """Run the whole behavioural layer for one analysis cycle.

        ``book`` is the live setup book. The desk reads the whole of it -- it
        manages every entered setup -- but the contextual reads (top-down
        compliance, the trader's own record) describe the setup currently
        deserving attention.
        """
        now = datetime.now(timezone.utc)
        self._events = []
        live = list(getattr(book, "live", []) or [])
        suggestion = getattr(book, "primary", None)

        session = sessions.read(now)
        day = character.assess(analysis, frames, session)
        method = topdown.assess(analysis, suggestion)
        macro = self._macro(frames)
        cross = self._intermarket(frames)
        view = self._thesis(analysis, decision, macro, profile, now)
        record = self._session_record(suggestion, now)
        recall = self._memory_verdict(suggestion, session, day, analysis)
        trade = self._manage_all(live, suggestion, analysis, profile)

        verdict = self._verdict(suggestion, session, day, view, recall, macro,
                                method, cross)

        self.state = {
            "session": session.to_dict(),
            "character": day.to_dict(),
            "topdown": method.to_dict(),
            "record": record.to_dict(),
            "thesis": view.to_dict() if view else None,
            "macro": macro.to_dict() if macro else None,
            "intermarket": cross.to_dict() if cross else None,
            "memory": recall,
            "management": trade,
            "verdict": verdict,
            "events": list(self._events),
            "generated_at": now.isoformat(),
        }
        return self.state

    # -- component steps ----------------------------------------------------

    def _macro(self, frames: dict):
        if self.dollar is None:
            return None
        try:
            return self.dollar.read(frames.get("D1") if frames else None)
        except Exception:
            log.warning("Dollar context failed", exc_info=True)
            return None

    def _intermarket(self, frames: dict):
        if self.intermarket is None:
            return None
        try:
            return self.intermarket.read(frames.get("D1") if frames else None)
        except Exception:
            log.warning("Intermarket context failed", exc_info=True)
            return None

    def _thesis(self, analysis: dict, decision: dict, macro, profile, now):
        if self.thesis is None:
            self.thesis = thesis_module.load(self.db, now)

        if thesis_module.is_stale(self.thesis, now):
            self.thesis = thesis_module.form(analysis, decision, macro, now)
            thesis_module.save(self.db, self.thesis)
            self._events.append(f"View for the session: {self.thesis.statement}")
            return self.thesis

        self.thesis, event = thesis_module.review(
            self.thesis, analysis, decision, profile, macro, now)
        if event:
            self._events.append(event)
            thesis_module.save(self.db, self.thesis)
        return self.thesis

    def _session_record(self, suggestion, now) -> awareness.SessionRecord:
        try:
            counters = awareness.gather(self.db, now)
        except Exception:
            log.warning("Could not read today's trading record", exc_info=True)
            counters = {}

        # The repeat-idea note only applies when the current setup really is
        # the failed one again.
        failure = None
        if suggestion is not None and awareness.same_idea(
                suggestion.direction, suggestion.entry_zone, self._last_failure):
            failure = self._last_failure
        return awareness.evaluate(counters, now, failure)

    def _memory_verdict(self, suggestion, session, day, analysis) -> dict:
        report = self._memory_report_cached()
        if suggestion is None:
            return {"headline": report.headline, "warnings": [], "support": [],
                    "matches": [], "conviction": 1.0, "report": report.to_dict()}

        verdict = self.memory.verdict_on({
            "strategy": suggestion.strategy,
            "family": suggestion.family,
            "setup_type": suggestion.setup_type,
            "session": session.name,
            "regime": (analysis.get("regime") or {}).get("regime"),
            "direction": suggestion.direction,
            "score": suggestion.score,
            "score_band": score_band(suggestion.score),
            "day_verdict": day.verdict,
        }, report)
        verdict["report"] = report.to_dict()
        return verdict

    def _memory_report_cached(self):
        now = datetime.now(timezone.utc)
        stale = (self._memory_at is None
                 or (now - self._memory_at).total_seconds() > MEMORY_REFRESH_SECONDS)
        if stale or self._memory_report is None:
            try:
                self._memory_report = self.memory.report()
            except Exception:
                log.warning("Could not read the outcome history", exc_info=True)
                from .memory import MemoryReport
                self._memory_report = MemoryReport(
                    total=0, overall=None,
                    headline="Outcome history is unavailable.")
            self._memory_at = now
        return self._memory_report

    def _manage_all(self, live: list, primary, analysis: dict,
                    profile) -> dict:
        """Open, advance or retire a managed trade for every entered setup."""
        active_ids = {s.id for s in live}
        for stale in [sid for sid in self.trades if sid not in active_ids]:
            self.trades.pop(stale, None)

        managed: list[dict] = []
        for suggestion in live:
            state = self._manage_one(suggestion, analysis, profile)
            if state is not None:
                managed.append(state)

        primary_state = None
        if primary is not None and primary.id in self.trades:
            primary_state = self.trades[primary.id].to_dict()

        open_risk = sum(t.risk * t.position_open for t in self.trades.values())
        return {
            "open": managed,
            "primary": primary_state,
            "count": len(managed),
            "open_risk": round(open_risk, 3),
        }

    def _manage_one(self, suggestion, analysis: dict, profile) -> dict | None:
        if not suggestion.entered:
            existing = self.trades.get(suggestion.id)
            return existing.to_dict() if existing else None

        trade = self.trades.get(suggestion.id)
        if trade is None:
            trade = management.open_trade(suggestion)
            if trade is None:
                return None
            self.trades[suggestion.id] = trade
            self._events.append(
                f"{suggestion.setup_label} filled around {trade.entry:.2f}; stop "
                f"{trade.initial_stop:.2f} ({trade.risk:.2f} risk).")

        try:
            actions = management.update(trade, suggestion, analysis, profile)
        except Exception:
            log.warning("Trade management step failed", exc_info=True)
            actions = []
        for action in actions:
            self._events.append(f"{suggestion.setup_label}: {action.headline}")
        return trade.to_dict()

    # -- the composed answer -------------------------------------------------

    def _verdict(self, suggestion, session, day, view, recall, macro,
                 method, cross=None) -> dict:
        """Fold every input into one conviction and one suggested size.

        Conviction is advice about *how much*, never about *whether*. A setup
        with low conviction is still a setup, still shown, still alerted.
        """
        factors: list[dict] = []
        conviction = 1.0

        def apply(name, value, note):
            nonlocal conviction
            conviction *= value
            factors.append({"factor": name, "multiplier": round(value, 2),
                            "note": note})

        apply("day character", day.conviction, day.headline)

        if suggestion is not None:
            apply("own record", float(recall.get("conviction", 1.0)),
                  recall.get("headline", ""))

            # The top-down method is the trader's stated approach, so a setup
            # that runs against the D1/H4 bias is worth less even when its own
            # technical evidence is strong. It is still a setup, still shown.
            if method.compliance == topdown.COUNTER_BIAS:
                apply("top-down method", 0.75, method.headline)
            elif method.compliance == topdown.NO_BIAS:
                apply("top-down method", 0.85, method.headline)

            if view is not None and not view.agrees_with(suggestion.direction):
                apply("session view", 0.8,
                      f"This {suggestion.direction} setup trades against today's "
                      f"{view.direction} view — which can be the right trade, but "
                      "it is a counter-trend one.")
            elif view is not None and view.status == thesis_module.CHALLENGED:
                apply("session view", 0.9,
                      "Today's view is under challenge, so the higher timeframes "
                      "are less settled than the score alone suggests.")

            if macro is not None and macro.available:
                if macro.supports == "conflicted":
                    apply("dollar context", 0.9, macro.headline)
                elif macro.supports == suggestion.direction:
                    apply("dollar context", 1.05, macro.headline)

            # The metals regime. A haven-driven rally is the case this exists
            # to catch: the Gold chart cannot distinguish it from a monetary
            # one, and it is the less durable of the two.
            if cross is not None and cross.available:
                if cross.regime == "fear" and suggestion.direction == "bullish":
                    apply("metals regime", 0.85, cross.headline)
                elif cross.supports == "conflicted":
                    apply("metals regime", 0.9, cross.headline)
                elif cross.supports == suggestion.direction:
                    apply("metals regime", 1.05, cross.headline)

        conviction = max(0.05, min(1.3, conviction))
        risk_fraction, risk_label = _risk_for(conviction)

        notes = list(method.notes)
        if not session.market_open:
            notes.append("Gold is closed, so any level shown is last week's.")
        elif day.verdict == character.STAND_ASIDE:
            notes.append("Day conditions are poor — setups still qualify, but "
                         "they are worth less than the same setup on a clean day.")

        return {
            "market_open": session.market_open,
            "conviction": round(conviction, 2),
            "risk_fraction": risk_fraction,
            "risk_label": risk_label,
            "factors": factors,
            "notes": notes,
            "headline": _headline(conviction, risk_label, day, session),
        }

    # -- lifecycle hooks the service calls ----------------------------------

    def on_terminated(self, suggestion, analysis: dict) -> dict | None:
        """Record how a finished setup actually went.

        This is the write half of the feedback loop: without it the memory
        module would have nothing to read back.
        """
        session = sessions.read()
        # The resolver owns the result. It walked the candles and knows the
        # fill, the exit and what the trade was worth; the management layer
        # only ever had spot prices and a heuristic. Where a managed trade
        # exists it still contributes its own excursion measurements.
        risk = suggestion.risk or 1.0
        outcome = {
            "outcome": suggestion.outcome or "untaken",
            "r_multiple": suggestion.r_multiple,
            "max_favourable_r": round(suggestion.mfe_pips * 0.10 / risk, 2)
            if suggestion.entered else 0.0,
            "max_adverse_r": round(-suggestion.mae_pips * 0.10 / risk, 2)
            if suggestion.entered else 0.0,
            "reason": suggestion.exit_reason or suggestion.status,
        }
        trade = self.trades.pop(suggestion.id, None)
        if trade is not None:
            managed = management.close_trade(trade, suggestion, suggestion.status)
            outcome["max_favourable_r"] = managed.get("max_favourable_r",
                                                      outcome["max_favourable_r"])
            outcome["max_adverse_r"] = managed.get("max_adverse_r",
                                                   outcome["max_adverse_r"])
            # A setup terminated without going through the resolver -- forced
            # by a caller, or closed before any candle was walked -- still has
            # to be classified from something.
            if suggestion.outcome is None:
                outcome["outcome"] = managed.get("outcome", "scratch")
                outcome["r_multiple"] = managed.get("r_multiple", 0.0)
        elif suggestion.outcome is None and suggestion.entered:
            outcome["outcome"] = ("win" if suggestion.status in states.WINNING
                                  else "loss" if suggestion.status in states.LOSING
                                  else "scratch")

        record = {
            "signal_uid": suggestion.id,
            "created_at": suggestion.created_at,
            "closed_at": datetime.now(timezone.utc).isoformat(),
            "direction": suggestion.direction,
            "setup_type": suggestion.setup_type,
            "session": session.name,
            "regime": (analysis.get("regime") or {}).get("regime"),
            "score": suggestion.score,
            "quality": suggestion.quality,
            "entry": (suggestion.entry_zone.preferred if suggestion.entry_zone
                      else suggestion.price),
            "invalidation": (suggestion.invalidation or {}).get("price"),
            "tp1": suggestion.targets[0].price if suggestion.targets else None,
            "entered": 1 if suggestion.entered else 0,
            "day_verdict": (self.state.get("character") or {}).get("verdict"),
            "notes": suggestion.status,
            "strategy": suggestion.strategy,
            "strategy_name": suggestion.strategy_name,
            "family": suggestion.family,
            "trigger_family": suggestion.trigger_family,
            "alignment": suggestion.alignment,
            "conviction": suggestion.conviction,
            "context_score": suggestion.context_score,
            "confirmations": [c.get("key") for c in suggestion.confirmations],
            "fill_price": suggestion.fill_price,
            "filled_at": suggestion.filled_at,
            "exit_price": suggestion.exit_price,
            "exit_reason": suggestion.exit_reason,
            "result_pips": suggestion.result_pips,
            "targets_hit": suggestion.targets_hit,
            "mfe_pips": round(suggestion.mfe_pips, 1),
            "mae_pips": round(suggestion.mae_pips, 1),
            "opportunity_pips": (suggestion.opportunity or {}).get("first_target_pips"),
            **outcome,
        }
        try:
            self.db.record_setup_outcome(record)
        except Exception:
            log.warning("Could not record the setup outcome", exc_info=True)

        # Force the memory to be re-read on the next cycle so the trader sees
        # the result of the setup that just finished.
        self._memory_at = None

        if suggestion.outcome == "loss" and suggestion.entry_zone is not None:
            self._last_failure = {
                "direction": suggestion.direction,
                "low": suggestion.entry_zone.low,
                "high": suggestion.entry_zone.high,
                "at": datetime.now(timezone.utc).isoformat(),
            }

        return record


def _risk_for(conviction: float) -> tuple[float, str]:
    for floor, fraction, label in RISK_BANDS:
        if conviction >= floor:
            return fraction, label
    return 0.25, "quarter risk"


def _headline(conviction, risk_label, day, session) -> str:
    if not session.market_open:
        return "Market closed — nothing to do until Gold reopens."
    return (f"{day.verdict.title()} conditions — conviction {conviction:.2f}, "
            f"{risk_label} if a setup confirms. "
            f"{session.to_dict()['label']}.")
