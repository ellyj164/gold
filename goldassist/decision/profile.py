"""Strategy profiles.

A profile encodes the trader's own methodology: which timeframes set bias,
which confirm, which conditions are mandatory, and how strict the decision
thresholds are. The confluence engine scores Gold *according to the active
profile* rather than to a single hard-coded strategy.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, asdict, field

from ..config import APP_DIR

PROFILE_PATH = APP_DIR / "profiles.json"

# Conditions a profile can require before a BUY/SELL is allowed. Anything not
# required still contributes to the score; requirements only gate the decision.
CONDITION_KEYS = (
    "higher_timeframe_alignment",
    "liquidity_event",
    "zone_location",
    "fvg_or_orderblock",
    "structure_confirmation",
    "candle_confirmation",
)


@dataclass
class StrategyProfile:
    name: str = "Default XAUUSD"
    bias_timeframes: tuple[str, ...] = ("D1", "H4")
    setup_timeframes: tuple[str, ...] = ("H1", "M30", "M15")
    confirmation_timeframe: str = "M5"
    required_conditions: tuple[str, ...] = (
        "higher_timeframe_alignment",
        "structure_confirmation",
    )
    # Score a setup must reach before the engine will call BUY or SELL.
    min_score: int = 70
    # Below this, the setup is not even worth watching.
    watch_score: int = 50
    # A setup whose nearest objective does not clear this reward-to-risk is
    # downgraded to WAIT, however good the technical evidence looks.
    min_reward_risk: float = 1.5
    # Only setups scoring at least this much raise an alert. Lower than
    # ``min_score`` on purpose: a strategy setup worth watching is worth
    # hearing about while there is still time to get to the entry zone.
    min_alert_score: int = 60
    # The smallest move worth taking intraday. 50 pips on XAUUSD is 5.00 of
    # price. This is a floor for the *first* objective, never a cap: larger
    # targets are always reported when the structure supports them.
    min_target_pips: float = 50.0
    # Weight multipliers, keyed by evidence factor name. 1.0 = catalogue default.
    weight_overrides: dict = field(default_factory=dict)
    # Skip setups when a high-impact economic release is within this many minutes.
    news_blackout_minutes: int = 30

    # --- the strategy library ----------------------------------------------
    # Which detectors may produce setups. None means every registered strategy,
    # which is the point of having a library: a profile that runs one pattern
    # sees one pattern's worth of opportunity.
    enabled_strategies: tuple[str, ...] | None = None
    # A setup whose first objective cannot clear ``min_target_pips`` is not
    # built at all. Set True only to inspect setups the pip rule rejects.
    allow_below_minimum: bool = False

    # --- resolution --------------------------------------------------------
    # Which target closes the signal and books the result. 1 = bank at the
    # first objective, which is the 50-pip rule the method is built on; 2 or 3
    # let a runner go, with the stop moved to breakeven behind it.
    close_at_target: int = 1
    # Move the stop to the fill price once a target has been banked.
    breakeven_after_target: bool = True
    # An unfilled setup is retired after this long: price never came, and a
    # stale zone on the board is worse than no zone.
    setup_expiry_minutes: int = 300
    # A filled trade that has neither hit its stop nor its target by now is
    # closed at market. A day trade held overnight is a different trade.
    trade_expiry_minutes: int = 720

    # --- trade management --------------------------------------------------
    # Move the stop to breakeven once the trade is this many R in favour.
    breakeven_at_r: float = 1.0
    # Fraction of the position to bank at the first objective.
    partial_at_tp1: float = 0.5
    # Timeframe whose swings the trailing stop follows.
    trail_timeframe: str = "M15"
    # Flag a trade that has gone nowhere for this long after entry.
    stall_minutes: int = 90

    # --- session view ------------------------------------------------------
    # How long the higher timeframes must disagree before the day's view is
    # revised. A view abandoned on the first contrary candle is not a view.
    thesis_revision_minutes: int = 30

    def to_dict(self) -> dict:
        d = asdict(self)
        for k in ("bias_timeframes", "setup_timeframes", "required_conditions"):
            d[k] = list(getattr(self, k))
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "StrategyProfile":
        base = asdict(cls())
        base.update({k: v for k, v in data.items() if k in base})
        for k in ("bias_timeframes", "setup_timeframes", "required_conditions"):
            base[k] = tuple(base[k])
        return cls(**base)


# Ready-made profiles covering the common ways of trading Gold.
BUILTIN_PROFILES = {
    "Default XAUUSD": StrategyProfile(),
    "Strict Smart Money": StrategyProfile(
        name="Strict Smart Money",
        required_conditions=(
            "higher_timeframe_alignment",
            "liquidity_event",
            "zone_location",
            "fvg_or_orderblock",
            "structure_confirmation",
        ),
        min_score=78,
        watch_score=55,
        min_reward_risk=2.0,
        min_alert_score=78,
    ),
    "Trend Continuation": StrategyProfile(
        name="Trend Continuation",
        required_conditions=("higher_timeframe_alignment", "structure_confirmation"),
        enabled_strategies=(
            "confluence_setup", "ema_pullback", "supertrend_ride", "vwap_pullback",
            "momentum_ride", "bos_retest", "zone_reaction", "ob_mitigation",
            "fvg_fill", "liquidity_draw", "fib_pocket", "range_breakout",
            "asian_range_break", "squeeze_expansion",
            # Continuation patterns: a flag, a triangle resolving with the
            # trend, an opening-range break, a compression expanding, and the
            # rail of a channel that is already going this way.
            "flag_continuation", "triangle_break", "opening_range_break",
            "compression_break", "channel_ride", "trendline_break",
        ),
        min_score=68,
        min_reward_risk=1.5,
        weight_overrides={"htf_alignment": 1.2, "trend_indicators": 1.3, "liquidity_sweep": 0.7},
        # Continuation trading rides winners further, so the stop is trailed
        # on a slower timeframe.
        trail_timeframe="H1",
        breakeven_at_r=1.2,
    ),
    "Reversal Hunter": StrategyProfile(
        name="Reversal Hunter",
        bias_timeframes=("H4", "H1"),
        setup_timeframes=("H1", "M30", "M15"),
        required_conditions=("liquidity_event", "structure_confirmation", "zone_location"),
        enabled_strategies=(
            "confluence_setup", "sweep_reversal", "divergence", "double_extreme",
            "range_fade", "vwap_reversion", "pattern_at_level", "round_number",
            "prev_day_level", "pivot_reaction", "zone_reaction", "ob_mitigation",
            # Reversal patterns: the three-peak top, an exhaustion climax into
            # a level, and a gap running back to the close it left behind.
            "head_shoulders", "climax_reversal", "gap_fill",
        ),
        min_score=75,
        min_reward_risk=2.0,
        weight_overrides={"htf_alignment": 0.5, "liquidity_sweep": 1.4, "candles": 1.4},
        # Reversals fail more often than they work, so this profile banks
        # earlier and protects the entry sooner.
        breakeven_at_r=0.8,
        partial_at_tp1=0.6,
        # A reversal trader is deliberately fighting the standing view, so the
        # view must not be allowed to hold their setups back too hard.
        thesis_revision_minutes=20,
    ),
    # Every strategy, a low watch threshold and a bank at the first objective:
    # the configuration that surfaces the most 50-pip opportunities per day.
    "Maximum Coverage": StrategyProfile(
        name="Maximum Coverage",
        required_conditions=(),
        min_score=62,
        watch_score=45,
        min_reward_risk=1.2,
        min_alert_score=55,
        close_at_target=1,
        setup_expiry_minutes=240,
    ),
    # The opposite trade-off: fewer setups, each one held for a runner.
    "High Conviction": StrategyProfile(
        name="High Conviction",
        required_conditions=("higher_timeframe_alignment", "structure_confirmation"),
        min_score=78,
        watch_score=60,
        min_reward_risk=2.0,
        min_alert_score=70,
        close_at_target=2,
        breakeven_after_target=True,
    ),
}


def load_profiles() -> dict[str, StrategyProfile]:
    profiles = dict(BUILTIN_PROFILES)
    if PROFILE_PATH.exists():
        try:
            saved = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return profiles
        for name, data in saved.items():
            profiles[name] = StrategyProfile.from_dict(data)
    return profiles


def save_profile(profile: StrategyProfile) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    saved = {}
    if PROFILE_PATH.exists():
        try:
            saved = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            saved = {}
    saved[profile.name] = profile.to_dict()
    PROFILE_PATH.write_text(json.dumps(saved, indent=2), encoding="utf-8")


def get(name: str | None) -> StrategyProfile:
    profiles = load_profiles()
    if name and name in profiles:
        return profiles[name]
    return profiles["Default XAUUSD"]
