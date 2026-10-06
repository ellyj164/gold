"""Signal suggestion engine.

Turns a *strategy candidate* -- or, for the original confluence read, the
decision itself -- into a tradeable suggestion: a preferred entry zone, the
confirmation trigger, an invalidation level, laddered targets, a quality grade,
and a lifecycle status that advances as price moves.

The flow the specification asks for:

    IDENTIFY -> FIND ZONE -> WAIT -> CONFIRM -> SUGGEST -> MONITOR -> DONE

...and, added here, the half that was missing: **RESOLVE**. A suggestion is
tracked from the moment it is identified until price decides it, at which point
it leaves the board carrying a result -- target hit, stopped out, or never
filled -- rather than lingering as a permanent fixture on the dashboard.

Two scores are carried, deliberately kept apart:

``conviction``  how good an example of its own pattern this setup is, as judged
                by the strategy that found it
``context``     the confluence engine's score for that direction

``score`` blends them, weighted by how much the strategy depends on the higher
timeframes agreeing (see ``strategies.base.CONTEXT_WEIGHT``). A sweep reversal
that is a textbook example of a sweep reversal is not disqualified for
disagreeing with a daily trend it is explicitly fading.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .. import timeutil
from . import classify, entry as entry_module, opportunity, resolve, risk_levels
from .classify import Trigger
from .states import (ACTIVE, COMPLETED, CONFIRMED, DEVELOPING, EXPIRED, IN_ZONE,
                     INVALIDATED, STOPPED_OUT, TARGET_HIT, TERMINAL,
                     WAITING_FOR_CONFIRMATION, WATCH, WEAKENING, WINNING)

# Kept under the old name because the rest of the app and the tests use it.
TERMINAL_STATES = TERMINAL

# --- quality bands (specification section 11) -------------------------------

QUALITY_BANDS = (
    (90, "Very Strong"),
    (80, "Strong"),
    (70, "Good"),
    (60, "Developing"),
    (40, "Weak"),
    (0, "No Setup"),
)


def quality_of(score: float) -> str:
    for threshold, name in QUALITY_BANDS:
        if score >= threshold:
            return name
    return "No Setup"


@dataclass
class Target:
    level: str          # TP1 / TP2 / TP3
    price: float
    basis: str
    pips: float = 0.0
    reward_risk: float | None = None
    hit: bool = False
    below_minimum: bool = False

    def to_dict(self) -> dict:
        return {
            "level": self.level,
            "price": round(self.price, 3),
            "basis": self.basis,
            "pips": round(self.pips, 1),
            "reward_risk": round(self.reward_risk, 2) if self.reward_risk else None,
            "hit": self.hit,
            "below_minimum": self.below_minimum,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Target":
        return cls(level=data.get("level", ""), price=float(data["price"]),
                   basis=data.get("basis", ""),
                   pips=float(data.get("pips") or 0.0),
                   reward_risk=data.get("reward_risk"),
                   hit=bool(data.get("hit")),
                   below_minimum=bool(data.get("below_minimum")))


@dataclass
class Suggestion:
    """One tracked trade scenario, from identification to resolution."""

    id: str
    direction: str                 # bullish | bearish
    action: str                    # BUY | SELL | WAIT
    setup_type: str
    setup_label: str
    status: str
    score: float
    quality: str
    price: float
    entry_zone: entry_module.EntryZone | None
    trigger: Trigger
    invalidation: dict
    targets: list[Target]
    reward_risk: float | None
    reasons: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    opportunity: dict = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""
    peak_score: float = 0.0

    # -- when it fired, in the terms a trader reads ------------------------
    # ``created_at`` is UTC and stays that way; everything below is the same
    # instant expressed the way it will be read back -- the trader's clock and
    # the session that was running. Both are frozen at identification rather
    # than recomputed on display, because a setup found at the London open is a
    # London-open setup for the rest of its life, however long it takes to
    # resolve. Recomputing it later is how a signal quietly changes its own
    # story after the fact.
    fired_at: str = ""             # display stamp, e.g. "2026-08-20 14:37"
    fired_date: str = ""           # display date, e.g. "2026-08-20"
    fired_clock: str = ""          # display time, e.g. "14:37"
    fired_session: str = ""        # session key, e.g. "overlap"
    fired_session_label: str = ""  # "London/NY overlap"
    fired_session_quality: float = 0.0
    clock_offset: str = ""         # "GMT+2" -- so the card can say which clock

    # -- which strategy found it -------------------------------------------
    strategy: str = "confluence_setup"
    strategy_name: str = "Confluence Read"
    family: str = "trend"
    trigger_family: str = "continuation"
    alignment: str = "either"
    conviction: float = 0.0
    context_score: float = 0.0
    # The market regime when the setup was identified. Kept on the setup
    # because the regime at the moment of the idea is what the record should
    # be grouped by, not whatever the regime happens to be when it closes.
    regime_at_entry: str = ""
    # Other strategies that independently pointed at the same trade.
    confirmations: list[dict] = field(default_factory=list)
    # The confluence evidence behind the setup, as numbers: each pillar's share
    # of its maximum and each penalty's points, for this setup's direction.
    # Captured when the setup is built and again at the moment it arms, which
    # is the moment the trade was actually justified. This is the feature
    # vector the weight fit reads -- without it the backtest can report what a
    # setup was worth but never which evidence made it worth that.
    features: dict = field(default_factory=dict)
    # Other live setups already committed in the same direction. Two longs a
    # few dollars apart filling within minutes are one bet on one move, and a
    # trader sizing each at full risk is carrying double without knowing it.
    exposure: dict = field(default_factory=dict)
    # How many same-direction trades were already filled nearby when this one
    # filled. Frozen at the fill, unlike ``exposure``, so the backtest can ask
    # whether stacked trades earn what standalone ones do.
    exposure_at_fill: int | None = None
    # The score at the moment the setup armed -- the number the trade was
    # actually taken on. ``score`` keeps being re-measured after the fill, and
    # it drifts with the outcome: losing trades lose evidence as they go wrong.
    # Every record that groups trades by score must read this, or the grouping
    # measures what happened after entry and calls it predictive. Backtests #1
    # to #5 grouped on the final score and their score-band tables were
    # largely that artefact.
    entry_score: float | None = None
    # How many other strategies agreed at the moment of the fill. Frozen for
    # the same reason as ``entry_score``: ``confirmations`` keeps growing while
    # a trade is open, and winners stay open longer.
    confirmations_at_fill: int | None = None
    # Handed over by the setup book every cycle and never saved: the other
    # setups on the board, and the ATR that defines "the same move", so the
    # resolver can refuse a fill that would stack on an open trade.
    peers: list = field(default_factory=list, repr=False, compare=False)
    peer_atr: float = field(default=0.0, repr=False, compare=False)

    # -- execution state ----------------------------------------------------
    # A setup is armed once it has actually confirmed: price in the zone and
    # the trigger met. Only an armed setup can fill. Price merely touching an
    # entry zone that never confirmed is not a trade anyone took, and counting
    # it as one filled the record with trades the method forbids.
    armed: bool = False
    armed_at: str | None = None
    entered: bool = False
    fill_price: float | None = None
    filled_at: str | None = None
    stop: float | None = None
    initial_stop: float | None = None
    risk: float | None = None
    targets_hit: int = 0
    mfe_pips: float = 0.0
    mae_pips: float = 0.0
    resolved_through: int | None = None

    # -- resolution ---------------------------------------------------------
    outcome: str | None = None     # win | loss | scratch | untaken
    exit_price: float | None = None
    exit_at: str | None = None
    exit_reason: str | None = None
    exit_note: str = ""
    result_pips: float = 0.0
    r_multiple: float = 0.0

    # -- what the broker took -----------------------------------------------
    #
    # Gross and net are kept side by side deliberately. ``result_pips`` and
    # ``r_multiple`` keep their original meaning so stored history stays
    # comparable across the change; the net figures are what the statistics,
    # the expectancy layer and the reward/risk gate read, because they are what
    # a trader is actually paid. ``costs`` is the model that produced them --
    # None means costs were not modelled for this setup, which is reported
    # rather than silently treated as zero.
    costs: object | None = None
    cost_pips: float = 0.0
    net_result_pips: float = 0.0
    net_r_multiple: float = 0.0

    history: list[dict] = field(default_factory=list)

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "symbol": "XAUUSD",
            "direction": self.direction,
            "action": self.action,
            "setup_type": self.setup_type,
            "setup_label": self.setup_label,
            "status": self.status,
            "score": round(self.score, 1),
            "quality": self.quality,
            "price": round(self.price, 3),
            "entry_zone": self.entry_zone.to_dict() if self.entry_zone else None,
            "entry_rationale": self.entry_zone.rationale() if self.entry_zone else None,
            "trigger": self.trigger.to_dict(),
            "invalidation": self.invalidation,
            "targets": [t.to_dict() for t in self.targets],
            "reward_risk": round(self.reward_risk, 2) if self.reward_risk else None,
            "opportunity": self.opportunity,
            "reasons": self.reasons,
            "risks": self.risks,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "fired_at": self.fired_at,
            "fired_date": self.fired_date,
            "fired_clock": self.fired_clock,
            "fired_session": self.fired_session,
            "fired_session_label": self.fired_session_label,
            "fired_session_quality": round(self.fired_session_quality, 2),
            "clock_offset": self.clock_offset,
            # The lifecycle, already translated: the interface should never
            # have to do timezone arithmetic to label a stage change.
            "updated_clock": timeutil.clock_seconds(self.updated_at),
            "filled_clock": timeutil.clock(self.filled_at),
            "exit_clock": timeutil.clock(self.exit_at),
            "peak_score": round(self.peak_score, 1),
            "strategy": self.strategy,
            "strategy_name": self.strategy_name,
            "family": self.family,
            "alignment": self.alignment,
            "conviction": round(self.conviction, 1),
            "context_score": round(self.context_score, 1),
            "regime_at_entry": self.regime_at_entry,
            "confirmations": self.confirmations,
            "exposure": self.exposure,
            "armed": self.armed,
            "entered": self.entered,
            "fill_price": self.fill_price,
            "filled_at": self.filled_at,
            "stop": round(self.stop, 3) if self.stop is not None else None,
            "initial_stop": (round(self.initial_stop, 3)
                             if self.initial_stop is not None else None),
            "targets_hit": self.targets_hit,
            "mfe_pips": round(self.mfe_pips, 1),
            "mae_pips": round(self.mae_pips, 1),
            "outcome": self.outcome,
            "exit_price": self.exit_price,
            "exit_at": self.exit_at,
            "exit_reason": self.exit_reason,
            "exit_note": self.exit_note,
            "result_pips": round(self.result_pips, 1),
            "r_multiple": round(self.r_multiple, 2),
            "cost_pips": round(self.cost_pips, 1),
            "net_result_pips": round(self.net_result_pips, 1),
            "net_r_multiple": round(self.net_r_multiple, 2),
            "costs": self.costs.to_dict() if self.costs is not None else None,
            "history": self.history[-25:],
            "is_terminal": self.is_terminal,
            "won": self.status in WINNING,
        }

    # -- persistence --------------------------------------------------------
    #
    # Kept apart from ``to_dict``, which is the *view* model the API and the
    # interface read. A view may round, truncate and derive freely; a saved
    # setup may not, because it has to come back as the same object. The two
    # were one method for exactly as long as it took to notice that reloading
    # a setup from its card silently dropped the resolver's place in the
    # candles and rewound the trade.

    # Working state ``to_dict`` has no reason to publish, and a restored setup
    # cannot do without.
    PERSIST_EXTRA = ("resolved_through", "risk", "armed_at", "trigger_family",
                     "features", "exposure_at_fill", "entry_score",
                     "confirmations_at_fill")

    def persist(self) -> dict:
        """The setup as it must be written down to be resumed later."""
        data = self.to_dict()
        for name in self.PERSIST_EXTRA:
            data[name] = getattr(self, name)
        data["history"] = self.history
        return data

    @classmethod
    def restore(cls, data: dict) -> "Suggestion":
        """Rebuild a setup saved by :meth:`persist`.

        Raises on a card that cannot be rebuilt faithfully -- a half-restored
        setup that resolves against the wrong stop is worse than one that is
        honestly dropped, so the caller is told rather than handed a shape.
        """
        zone = data.get("entry_zone")
        suggestion = cls(
            id=data["id"],
            direction=data["direction"],
            action=data["action"],
            setup_type=data.get("setup_type", ""),
            setup_label=data.get("setup_label", ""),
            status=data["status"],
            score=float(data.get("score") or 0.0),
            quality=data.get("quality", ""),
            price=float(data.get("price") or 0.0),
            entry_zone=entry_module.EntryZone.from_dict(zone) if zone else None,
            trigger=Trigger.from_dict(data.get("trigger") or {}),
            invalidation=data.get("invalidation") or {},
            targets=[Target.from_dict(t) for t in (data.get("targets") or [])],
            reward_risk=data.get("reward_risk"),
            reasons=list(data.get("reasons") or []),
            risks=list(data.get("risks") or []),
            opportunity=data.get("opportunity") or {},
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
            peak_score=float(data.get("peak_score") or 0.0),
        )
        for name in ("fired_at", "fired_date", "fired_clock", "fired_session",
                     "fired_session_label", "clock_offset", "strategy",
                     "strategy_name", "family", "trigger_family", "alignment",
                     "regime_at_entry", "exit_reason", "exit_note", "outcome",
                     "exit_at", "armed_at", "filled_at"):
            if data.get(name) is not None:
                setattr(suggestion, name, data[name])
        suggestion.fired_session_quality = float(data.get("fired_session_quality") or 0.0)
        suggestion.conviction = float(data.get("conviction") or 0.0)
        suggestion.context_score = float(data.get("context_score") or 0.0)
        suggestion.confirmations = list(data.get("confirmations") or [])
        suggestion.features = dict(data.get("features") or {})
        suggestion.exposure = dict(data.get("exposure") or {})
        suggestion.exposure_at_fill = data.get("exposure_at_fill")
        suggestion.entry_score = data.get("entry_score")
        suggestion.confirmations_at_fill = data.get("confirmations_at_fill")
        suggestion.history = list(data.get("history") or [])
        suggestion.armed = bool(data.get("armed"))
        suggestion.entered = bool(data.get("entered"))
        suggestion.fill_price = data.get("fill_price")
        suggestion.stop = data.get("stop")
        suggestion.initial_stop = data.get("initial_stop")
        suggestion.risk = data.get("risk")
        suggestion.targets_hit = int(data.get("targets_hit") or 0)
        suggestion.mfe_pips = float(data.get("mfe_pips") or 0.0)
        suggestion.mae_pips = float(data.get("mae_pips") or 0.0)
        suggestion.resolved_through = data.get("resolved_through")
        suggestion.exit_price = data.get("exit_price")
        suggestion.result_pips = float(data.get("result_pips") or 0.0)
        suggestion.r_multiple = float(data.get("r_multiple") or 0.0)
        # The cost *model* is deliberately not restored: the card carries its
        # reading, not the object, and the book re-attaches the current one on
        # the next cycle. What is restored is what had already been measured.
        suggestion.cost_pips = float(data.get("cost_pips") or 0.0)
        suggestion.net_result_pips = float(data.get("net_result_pips") or 0.0)
        suggestion.net_r_multiple = float(data.get("net_r_multiple") or 0.0)
        return suggestion


def build(analysis: dict, decision: dict, profile, candidate=None,
          now: datetime | None = None, costs=None) -> Suggestion | None:
    """Create a suggestion from a strategy candidate, or from the decision.

    Returns None when there is not even a candidate worth tracking -- the
    module must never manufacture a setup out of nothing. In particular a setup
    whose first realistic objective cannot clear the profile's pip minimum is
    rejected here: the trader's rule is that a move too small to be worth
    taking is not a signal, however good the pattern looks.

    ``costs`` is a :class:`goldassist.costs.CostModel`, or None to leave costs
    unmodelled. When supplied it tightens the reward/risk gate below, because a
    gross ratio is not the ratio the trade actually pays.
    """
    direction = candidate.direction if candidate is not None else decision["direction"]
    price = float(analysis["price"])
    context = _context_score(decision, direction)
    conviction = (_seed_conviction(candidate.conviction) if candidate is not None
                  else context)
    score = _blend(conviction, context,
                   candidate.alignment if candidate is not None else "either")

    zone = _zone_for(analysis, direction, candidate)
    kind = candidate.trigger_family if candidate is not None else classify.setup_type(
        analysis, direction)
    trigger = classify.trigger_for(kind, analysis, direction,
                                   profile.confirmation_timeframe)

    # Invalidation and targets are measured from the entry zone, not from spot.
    # Reusing the decision engine's spot-based levels put the stop on the wrong
    # side of the entry and produced a meaningless reward/risk figure.
    invalidation = _invalidation_for(analysis, direction, zone, decision, candidate)
    if not invalidation.get("price"):
        return None
    reference = zone.preferred if zone is not None else price

    minimum_pips = getattr(profile, "min_target_pips", 500.0)
    raw_targets = risk_levels.targets_for_zone(
        analysis, direction, reference, invalidation["price"],
        minimum_pips=minimum_pips,
        extra=candidate.targets if candidate is not None else None)
    targets = [
        Target(level=f"TP{i + 1}", price=t["price"], basis=t["basis"],
               pips=t.get("pips", 0.0), below_minimum=t.get("below_minimum", False))
        for i, t in enumerate(raw_targets)
    ]
    if not targets:
        return None

    chance = opportunity.assess(reference, targets, minimum_pips)
    if not chance["qualifies"] and not getattr(profile, "allow_below_minimum", False):
        return None

    rr = _reward_risk(reference, invalidation["price"], targets[0].price)
    for target in targets:
        target.reward_risk = _reward_risk(reference, invalidation["price"], target.price)

    # Reward/risk is a gate, not a note. The objective the trade is actually
    # closed at has to pay for the risk it demands: banking 1.1R on the winners
    # while losing 1R on the losers is a losing method whatever the win rate
    # looks like. Which objective that is depends on where the profile banks.
    # The gate is applied to the *net* ratio when costs are known. A gross 1.5
    # on a 250-pip stop is a net 1.34 once a 25-pip round turn is charged, and
    # a gate that reads the gross figure passes trades the method is meant to
    # refuse. With no cost model the two are identical, so nothing changes.
    minimum_rr = float(getattr(profile, "min_reward_risk", 1.5) or 0)
    if minimum_rr:
        banked = targets[min(len(targets), max(1, int(getattr(
            profile, "close_at_target", 1)))) - 1]
        if not banked.reward_risk:
            return None
        effective_rr = banked.reward_risk
        if costs is not None and costs.available:
            net = costs.net_reward_risk(
                abs(banked.price - reference), abs(reference - invalidation["price"]))
            if net is not None:
                effective_rr = net
        if effective_rr < minimum_rr:
            return None

    stamp = (now or datetime.now(timezone.utc)).isoformat()
    suggestion = Suggestion(
        id=uuid.uuid4().hex[:12],
        direction=direction,
        action=("BUY" if direction == "bullish" else "SELL")
        if candidate is not None else decision["decision"],
        setup_type=candidate.key if candidate is not None else kind,
        setup_label=candidate.label if candidate is not None else classify.label(
            kind, direction),
        status=DEVELOPING,
        score=score,
        quality=quality_of(score),
        price=price,
        entry_zone=zone,
        trigger=trigger,
        invalidation=invalidation,
        targets=targets,
        reward_risk=rr,
        reasons=_reasons_for(decision, candidate),
        risks=[w["message"] for w in (decision.get("risk") or {}).get("warnings", [])],
        opportunity=chance,
        created_at=stamp,
        updated_at=stamp,
        peak_score=score,
        strategy=candidate.key if candidate is not None else "confluence_setup",
        strategy_name=candidate.name if candidate is not None else "Confluence Read",
        family=candidate.family if candidate is not None else "trend",
        trigger_family=kind,
        alignment=candidate.alignment if candidate is not None else "either",
        conviction=conviction,
        context_score=context,
        regime_at_entry=(analysis.get("regime") or {}).get("regime", ""),
        costs=costs,
        features=features_for(decision, direction, conviction),
    )
    _stamp_clock(suggestion, stamp)
    resolve.initialise(suggestion)
    suggestion.status = _status_for(suggestion, analysis, decision, profile)
    suggestion.history.append({"time": stamp,
                               "clock": suggestion.fired_clock,
                               "session": suggestion.fired_session_label,
                               "status": suggestion.status,
                               "score": round(score, 1), "price": round(price, 3),
                               "note": "setup identified by "
                                       + suggestion.strategy_name})
    return suggestion


def _stamp_clock(suggestion: Suggestion, when: str) -> None:
    """Freeze the trader-facing time and session onto a new setup."""
    read = timeutil.describe(when)
    suggestion.fired_at = read.get("stamp", "")
    suggestion.fired_date = read.get("date", "")
    suggestion.fired_clock = read.get("clock", "")
    suggestion.fired_session = read.get("session", "")
    suggestion.fired_session_label = read.get("session_label", "")
    suggestion.fired_session_quality = float(read.get("session_quality") or 0.0)
    suggestion.clock_offset = read.get("offset", "")


def update(suggestion: Suggestion, analysis: dict, decision: dict, profile,
           bars=None, now: datetime | None = None) -> tuple[Suggestion, list[str]]:
    """Advance a tracked suggestion. Returns it plus any stage changes.

    ``bars`` are the finest-timeframe candles available. When supplied, fills,
    targets and stops are resolved from the candles rather than from spot, so a
    move that happened between two polls is not missed.
    """
    events: list[str] = []
    price = float(analysis["price"])
    clock = now or datetime.now(timezone.utc)
    stamp = clock.isoformat()

    previous_status = suggestion.status
    previous_score = suggestion.score

    # Re-measure everything that can move.
    suggestion.price = price
    context = _context_score(decision, suggestion.direction)
    suggestion.context_score = context
    if suggestion.strategy == "confluence_setup":
        suggestion.conviction = context
    suggestion.score = _blend(suggestion.conviction, context, suggestion.alignment)
    suggestion.quality = quality_of(suggestion.score)
    suggestion.peak_score = max(suggestion.peak_score, suggestion.score)
    suggestion.trigger = classify.trigger_for(
        suggestion.trigger_family, analysis, suggestion.direction,
        profile.confirmation_timeframe)
    # Only the confluence read is re-argued from the decision each cycle. A
    # strategy setup keeps the evidence its detector gave for it -- overwriting
    # that with the engine's leading-direction reasons made a sweep reversal
    # explain itself in the words of the trend it was fading.
    if suggestion.strategy == "confluence_setup":
        suggestion.reasons = _reasons_for(decision, None) or suggestion.reasons
    suggestion.risks = [w["message"]
                        for w in (decision.get("risk") or {}).get("warnings", [])]
    reference = (suggestion.fill_price if suggestion.entered and suggestion.fill_price
                 else suggestion.entry_zone.preferred if suggestion.entry_zone
                 else suggestion.price)
    suggestion.opportunity = opportunity.assess(
        reference, suggestion.targets, getattr(profile, "min_target_pips", 500.0))
    if suggestion.entry_zone:
        zone = suggestion.entry_zone
        atr = analysis.get("atr") or 1
        zone.price_inside = zone.low <= price <= zone.high
        zone.distance_atr = 0.0 if zone.price_inside else (
            min(abs(price - zone.low), abs(price - zone.high)) / atr)

    # Did price reach the entry during the candles about to be resolved? This
    # is read before they are walked, because arming has to happen first: a
    # setup that confirms and fills inside the same window must do both.
    if suggestion.entry_zone is not None:
        suggestion.entry_zone.visited = (
            suggestion.entry_zone.price_inside
            or resolve.visited_zone(suggestion, bars))

    # Arming, before resolution: a setup that has just confirmed may fill on
    # this very tick, and one that never confirms may not fill at all.
    if not suggestion.entered and not suggestion.armed:
        if _status_for(suggestion, analysis, decision, profile) == CONFIRMED:
            suggestion.armed = True
            suggestion.armed_at = stamp
            suggestion.features = features_for(decision, suggestion.direction,
                                               suggestion.conviction)
            suggestion.entry_score = round(float(suggestion.score), 1)
            events.append("Confirmed — the entry is live: "
                          + suggestion.trigger.description)

    # Resolution: candles first (so nothing between polls is missed), then the
    # live quote as a partial bar.
    events += resolve.apply_bars(suggestion, bars, profile)
    events += resolve.apply_price(suggestion, price, profile, when=stamp)

    if suggestion.status not in TERMINAL:
        expiry = resolve.check_expiry(suggestion, profile, now=clock)
        if expiry:
            events.append(expiry)

    if suggestion.status not in TERMINAL:
        suggestion.status = _status_for(suggestion, analysis, decision, profile)
    suggestion.updated_at = stamp

    if suggestion.status != previous_status:
        events.append(f"Setup {previous_status} -> {suggestion.status}")
        suggestion.history.append({
            "time": stamp, "clock": timeutil.clock_seconds(stamp),
            "session": timeutil.describe(stamp).get("session_label", ""),
            "status": suggestion.status,
            "score": round(suggestion.score, 1), "price": round(price, 3),
            "note": _status_note(suggestion),
        })
    elif abs(suggestion.score - previous_score) >= 5:
        suggestion.history.append({
            "time": stamp, "clock": timeutil.clock_seconds(stamp),
            "session": timeutil.describe(stamp).get("session_label", ""),
            "status": suggestion.status,
            "score": round(suggestion.score, 1), "price": round(price, 3),
            "note": f"score {previous_score:.0f} -> {suggestion.score:.0f}",
        })

    return suggestion, events


def merge(suggestion: Suggestion, candidate) -> None:
    """Record a second strategy agreeing with a setup already being tracked.

    Two detectors pointing at the same area in the same direction is not a
    duplicate signal -- it is confluence, and it is the strongest argument the
    application can make. The setup keeps its identity and gains a bounded
    boost, so ten weak agreements can never manufacture a strong setup.
    """
    if any(c["key"] == candidate.key for c in suggestion.confirmations):
        return
    suggestion.confirmations.append({
        "key": candidate.key,
        "name": candidate.name,
        "family": candidate.family,
        "conviction": round(float(candidate.conviction), 1),
        "evidence": list(candidate.evidence)[:2],
    })
    # Each confirmation takes a share of the *remaining* headroom rather than
    # adding a flat amount and clamping.
    #
    # The old rule added up to six points per confirming strategy and clipped
    # at 100. With thirty-two detectors several routinely agree on one setup,
    # so conviction ratcheted to the ceiling and stuck there: a third of every
    # setup ever recorded sits at exactly 100.0, and once two setups are both
    # pinned at the ceiling the field can no longer rank them. Measured over
    # the live record, conviction carried no information at all -- the
    # better-than-median half returned -0.001R against -0.046R for the rest.
    #
    # Taking a share of the headroom is strictly increasing, orders setups by
    # how much agreement they actually attracted, and approaches 100 without
    # ever arriving, so the top of the scale stays a comparison rather than a
    # collision.
    boost = _headroom_gain(suggestion.conviction, candidate.conviction)
    suggestion.conviction += boost
    suggestion.score = min(100.0, suggestion.score + boost)
    suggestion.quality = quality_of(suggestion.score)
    suggestion.peak_score = max(suggestion.peak_score, suggestion.score)
    for line in list(candidate.evidence)[:2]:
        if line not in suggestion.reasons:
            suggestion.reasons.append(f"{candidate.name}: {line}")


# --- construction helpers ---------------------------------------------------


# Share of the remaining headroom a perfect confirmation may claim. A
# textbook agreeing strategy closes a quarter of the gap to 100; a mediocre
# one closes proportionally less.
CONFIRMATION_SHARE = 0.25

# Where a single strategy's own read tops out once it seeds a suggestion.
#
# `base.mix` legitimately returns 100 for a textbook example of a pattern, and
# that is the right answer to "how good an example of itself is this?". It is
# the wrong answer to "how convinced am I?", because it leaves no room for the
# thing that should separate two textbook setups: whether anything else
# agrees. Reserving the top of the range for corroboration is what makes the
# scale mean something.
#
# Applied here rather than inside `mix` on purpose. Every detector's
# `min_conviction` gate is checked against the raw value in
# `strategies.base.detect`, so compressing it there would quietly raise every
# gate by four percent and start withholding setups -- which this application
# is not permitted to do.
SEED_CEILING = 96.0


def _seed_conviction(value: float) -> float:
    """A detector's own conviction, compressed to leave room for agreement."""
    return max(0.0, min(100.0, float(value))) * (SEED_CEILING / 100.0)


def _headroom_gain(current: float, candidate_conviction: float) -> float:
    """How much a confirming strategy adds, given how convinced it is.

    Diminishing by construction: the first confirmation on a 70 moves it ~7,
    the fourth moves it ~3, and no finite number of them reaches 100.
    """
    headroom = max(0.0, 100.0 - current)
    quality = max(0.0, min(1.0, candidate_conviction / 100.0))
    return headroom * CONFIRMATION_SHARE * quality


def features_for(decision: dict, direction: str, conviction: float = 0.0) -> dict:
    """The confluence evidence for ``direction`` as a flat numeric record.

    Pillars are recorded as the share of their own maximum they earned (0..1)
    so a profile's weight overrides do not change what the feature means;
    penalties as the points they deducted. Every catalogue pillar is present,
    zero when it earned nothing, because an absent pillar is information and a
    missing key is not.
    """
    from ..decision.confluence import CATALOGUE

    side = (decision.get("confluence") or {}).get(direction) or {}
    record = {f"pillar:{name}": 0.0 for name in CATALOGUE}
    for item in side.get("evidence") or []:
        cap = float(item.get("max_points") or 0.0)
        if cap > 0:
            key = f"pillar:{item.get('factor')}"
            record[key] = round(min(1.0, record.get(key, 0.0)
                                    + float(item.get("points") or 0.0) / cap), 4)
    for item in side.get("penalties") or []:
        key = f"penalty:{item.get('factor')}"
        record[key] = round(record.get(key, 0.0) + float(item.get("points") or 0.0), 2)
    record["context_score"] = round(float(side.get("score") or 0.0), 1)
    record["conviction"] = round(float(conviction or 0.0), 1)
    return record


def _context_score(decision: dict, direction: str) -> float:
    if direction == decision.get("direction"):
        return float(decision.get("score", 0.0))
    side = (decision.get("confluence") or {}).get(direction) or {}
    return float(side.get("score", 0.0))


def _blend(conviction: float, context: float, alignment: str) -> float:
    from ..strategies.base import CONTEXT_WEIGHT

    weight = CONTEXT_WEIGHT.get(alignment, 0.38)
    return round(conviction * (1 - weight) + context * weight, 1)


def _zone_for(analysis: dict, direction: str, candidate):
    """The entry area: the strategy's own, or the overlap engine's."""
    if candidate is None or candidate.zone is None:
        return entry_module.build(analysis, direction)
    low, high = candidate.zone
    price = float(analysis["price"])
    atr = float(analysis.get("atr") or 0) or price * 0.002
    return entry_module.explicit(low, high, direction, price, atr,
                                 candidate.preferred, candidate.sources)


# A stop closer than this fraction of the H1 ATR to the entry is inside the
# market's own noise: it will be taken out by a spread widening or a single
# fast candle, whatever the pattern says.
MIN_STOP_ATR = 0.25


def _invalidation_for(analysis: dict, direction: str, zone, decision: dict,
                      candidate) -> dict:
    """Where this setup is wrong: the strategy's level, or a structural one."""
    if candidate is not None and candidate.stop is not None and zone is not None:
        stop = float(candidate.stop)
        basis = candidate.stop_basis or "strategy invalidation"
        price = float(analysis["price"])
        atr = float(analysis.get("atr") or 0) or price * 0.002
        edge = zone.low if direction == "bullish" else zone.high
        floor = atr * MIN_STOP_ATR
        if abs(edge - stop) < floor:
            stop = edge - floor if direction == "bullish" else edge + floor
            basis += f", widened to {MIN_STOP_ATR:.2f}x ATR so noise cannot take it"
        return {"price": round(stop, 3),
                "basis": basis,
                "type": "strategy",
                "description": _describe(direction, stop, basis)}
    if zone is not None:
        return risk_levels.invalidation_for_zone(analysis, direction, zone.low, zone.high)
    return dict(decision.get("invalidation") or {})


def _reasons_for(decision: dict, candidate) -> list[str]:
    if candidate is not None and candidate.evidence:
        return list(candidate.evidence)[:6]
    return list(decision.get("reasons", []))[:6]


def _describe(direction: str, price: float, basis: str) -> str:
    if direction == "bullish":
        return f"a confirmed close below {price:.2f} ({basis})"
    return f"a confirmed close above {price:.2f} ({basis})"


# --- the lifecycle state machine -------------------------------------------


def _status_for(suggestion: Suggestion, analysis: dict, decision: dict,
                profile) -> str:
    """The non-terminal lifecycle. Terminal states come from ``resolve``."""
    price = suggestion.price
    zone = suggestion.entry_zone

    if suggestion.entered:
        # A filled trade is decided by price alone from here. The engine
        # changing its mind does not close a position; the stop and the target
        # do, and both are handled in ``resolve``.
        return ACTIVE if suggestion.targets_hit else CONFIRMED

    # Deterioration: well off the peak, and out of the band that made it worth
    # watching in the first place.
    decayed = suggestion.peak_score - suggestion.score >= 15
    if decayed and suggestion.score < profile.min_score:
        return WEAKENING

    # Confirmation only counts once price has actually reached the entry area.
    # A trigger firing while price is still far away does not make the setup
    # tradeable -- the entry has not come to the trader yet, and treating it as
    # CONFIRMED would invite exactly the chase the specification warns against.
    if zone is not None:
        if zone.price_inside or zone.visited:
            if suggestion.trigger.met and suggestion.score >= profile.min_score:
                return CONFIRMED
            return WAITING_FOR_CONFIRMATION
        if zone.distance_atr <= 1.5:
            return WATCH
        return WATCH if suggestion.score >= profile.watch_score else DEVELOPING

    # No zone could be derived: fall back to trigger and score alone.
    if suggestion.trigger.met and suggestion.score >= profile.min_score:
        return CONFIRMED
    if suggestion.score >= profile.watch_score:
        return WATCH
    return DEVELOPING


def _status_note(suggestion: Suggestion) -> str:
    status = suggestion.status
    if status == IN_ZONE:
        return "price entered the preferred entry zone"
    if status == WAITING_FOR_CONFIRMATION:
        return "in zone; waiting for " + (suggestion.trigger.outstanding[0]
                                          if suggestion.trigger.outstanding
                                          else suggestion.trigger.description)
    if status == CONFIRMED:
        return ("entry filled at %.2f" % suggestion.fill_price if suggestion.entered
                else "confirmation detected: " + suggestion.trigger.description)
    if status == ACTIVE:
        return f"{suggestion.targets_hit} target(s) banked, running"
    if status == WEAKENING:
        return f"conditions deteriorating (peak {suggestion.peak_score:.0f})"
    if status == TARGET_HIT:
        return (f"target reached at {suggestion.exit_price} "
                f"({suggestion.result_pips:+.0f} pips, {suggestion.r_multiple:+.2f}R)")
    if status == COMPLETED:
        return (f"every target reached ({suggestion.result_pips:+.0f} pips, "
                f"{suggestion.r_multiple:+.2f}R)")
    if status == STOPPED_OUT:
        return (f"stopped at {suggestion.exit_price} "
                f"({suggestion.result_pips:+.0f} pips, {suggestion.r_multiple:+.2f}R)")
    if status == INVALIDATED:
        return "invalidation level broke before the entry filled"
    if status == EXPIRED:
        return suggestion.exit_note or "the setup ran out of time"
    if status == WATCH:
        return "approaching the entry zone"
    return "setup developing"


def _reward_risk(entry: float, invalidation: float, target: float) -> float | None:
    risk = abs(entry - invalidation)
    if risk <= 0:
        return None
    return abs(target - entry) / risk


def rr_acceptable(suggestion: Suggestion, minimum: float) -> bool:
    """Whether the nearest objective clears the profile's minimum reward/risk."""
    return bool(suggestion.reward_risk and suggestion.reward_risk >= minimum)
