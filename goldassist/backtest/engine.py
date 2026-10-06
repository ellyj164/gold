"""Backtesting engine.

Replays historical Gold data through the *same* pipeline the live application
runs -- analysis, decision, the whole strategy library, the setup book and the
resolver -- so what is measured is the application's own behaviour rather than
a simplified stand-in for it.

That distinction used to be the biggest hole in this project. The backtester
entered at spot the moment the decision engine said BUY, which meant it tested
neither the entry zones, nor the confirmation triggers, nor the lifecycle, nor
the trade management -- and, because it had no deduplication, one persistent
signal was opened again on every qualifying candle and counted as twenty
separate losses. The live app never behaved like that. Now the replay drives
``SuggestionBook`` on a replay clock and reads the results the resolver
produces, so a backtest trade is a trade the application would actually have
shown, with the entry it would have proposed and the exit it would have called.

The replay is strictly causal: at each step the engines see only candles that
had closed at that moment, and setups are resolved forward against the finest
timeframe available.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, asdict, field
from datetime import datetime

import numpy as np
import pandas as pd

from ..analysis import mtf
from ..config import BACKTEST_TIMEFRAMES, TIMEFRAME_MINUTES
from ..decision import engine as decision_engine
from ..decision.profile import StrategyProfile
from ..signals import SuggestionBook
from . import history, rigour

log = logging.getLogger(__name__)

# Minimum history each timeframe needs before its engines produce sane output
# (the 200-period EMA and the ATR median are the binding constraints).
WARMUP_BARS = 250

# Bars of each timeframe handed to the engines per step. The deepest lookback
# any engine uses is the 200-period EMA, so more than this changes nothing but
# costs time — and a replay recomputes everything on every single step.
ANALYSIS_BARS = 420

# Candles of the resolution timeframe handed to the resolver each step. It only
# walks bars it has not seen and that post-date the setup, so this is a bound on
# how far behind the replay may fall, not a lookback.
RESOLUTION_BARS = 4000

# Ceiling on how many times the engines are re-run in one replay.
#
# This used to be 600, which was not a guard but a wall. One evaluation runs
# the full multi-timeframe analysis, so 600 of them at an M15 step covers about
# six days of market -- and six days cannot validate 32 detectors across six
# sessions and five score bands, because every cell holds single digits. The
# step was silently widened to fit, which under-fills: setups arm on a
# confirmation seen at an evaluation instant, and sampling every third hour
# misses most of them.
#
# Raised to a figure that can actually measure something. A single core runs
# roughly 2000 evaluations in an hour, so this is an overnight run sequentially
# and well under an hour across eight cores -- see :mod:`goldassist.backtest.parallel`,
# which is the reason the ceiling can move at all.
DEFAULT_MAX_EVALUATIONS = 20000

SESSIONS = {"asia": (0, 8), "london": (7, 16), "newyork": (12, 21)}

# The resolver's entry rule, recorded on every stored run. See
# ``signals.resolve._entry_fill``.
FILL_MODEL = "first-in-zone"
# Which score the bands group on. Runs without this key grouped on the score at
# close, which drifts with the outcome.
SCORE_BASIS = "entry"


@dataclass
class BacktestTrade:
    entered_at: datetime
    direction: str
    decision: str
    entry: float
    stop: float
    target: float
    score: float
    regime: str
    session: str
    exited_at: datetime | None = None
    exit_price: float | None = None
    result: str = "open"        # win | loss | timeout | open
    r_multiple: float = 0.0
    bars_held: int = 0
    # Which strategy produced it, and how it ended, straight from the resolver.
    strategy: str = ""
    strategy_name: str = ""
    family: str = ""
    exit_reason: str = ""
    result_pips: float = 0.0
    # Agreeing strategies when the trade closed. Grows while a trade is open,
    # and more for winners, so nothing should group on it.
    confirmations: int = 0
    # Agreeing strategies when it filled -- the count the trader could see.
    confirmations_at_fill: int | None = None
    # What the broker took, and the same trade after it was paid.
    cost_pips: float = 0.0
    net_r_multiple: float = 0.0
    net_result_pips: float = 0.0
    # How far the trade went against and in favour before it resolved. The
    # resolver tracks both on every bar and nothing was reading them back.
    # MAE is what makes a mark-to-market drawdown possible at all -- without it
    # an open losing position is invisible until it closes. MFE says whether
    # the targets are placed where the trade actually reaches.
    mae_pips: float = 0.0
    mfe_pips: float = 0.0
    # The confluence evidence at the moment the setup armed, as numbers. What
    # ``backtest.fit`` regresses the outcome on to replace judged weights with
    # measured ones.
    features: dict = field(default_factory=dict)
    # Same-direction trades already filled nearby when this one filled. Lets a
    # run ask whether stacked trades earn what standalone ones do.
    exposure_at_fill: int | None = None
    # The score when the trade closed. Kept only to show how far it drifted;
    # nothing groups on it.
    final_score: float | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["entered_at"] = self.entered_at.isoformat()
        d["exited_at"] = self.exited_at.isoformat() if self.exited_at else None
        for k in ("entry", "stop", "target", "exit_price"):
            if d[k] is not None:
                d[k] = round(float(d[k]), 3)
        d["r_multiple"] = round(self.r_multiple, 2)
        d["result_pips"] = round(self.result_pips, 1)
        d["mae_pips"] = round(self.mae_pips, 1)
        d["mfe_pips"] = round(self.mfe_pips, 1)
        d["cost_pips"] = round(self.cost_pips, 1)
        d["net_r_multiple"] = round(self.net_r_multiple, 2)
        d["net_result_pips"] = round(self.net_result_pips, 1)
        return d


def _trade_from(suggestion, entered_at: datetime, exited_at: datetime,
                bars_held: int) -> BacktestTrade:
    """Turn a resolved suggestion into the backtest's own trade record."""
    outcome = suggestion.outcome or "scratch"
    return BacktestTrade(
        entered_at=entered_at,
        direction=suggestion.direction,
        decision="BUY" if suggestion.direction == "bullish" else "SELL",
        entry=float(suggestion.fill_price),
        stop=float(suggestion.initial_stop if suggestion.initial_stop is not None
                   else suggestion.stop),
        target=float(suggestion.targets[0].price) if suggestion.targets else 0.0,
        # The score the trade was taken on, not the one it ended with: the
        # latter drifts with the outcome and turns every score-band table into
        # a measurement of what happened after entry.
        score=float(suggestion.entry_score if getattr(suggestion, "entry_score", None)
                    is not None else suggestion.score),
        final_score=float(suggestion.score),
        regime=str(suggestion.regime_at_entry or "unknown"),
        session=_session_of(pd.Timestamp(entered_at)),
        exited_at=exited_at,
        exit_price=float(suggestion.exit_price) if suggestion.exit_price else None,
        result="win" if outcome == "win" else "loss" if outcome == "loss" else "timeout",
        r_multiple=float(suggestion.r_multiple),
        bars_held=bars_held,
        strategy=suggestion.strategy,
        strategy_name=suggestion.strategy_name,
        family=suggestion.family,
        exit_reason=suggestion.exit_reason or "",
        result_pips=float(suggestion.result_pips),
        confirmations=len(suggestion.confirmations),
        confirmations_at_fill=getattr(suggestion, "confirmations_at_fill", None),
        cost_pips=float(getattr(suggestion, "cost_pips", 0.0)),
        net_r_multiple=float(getattr(suggestion, "net_r_multiple", 0.0)
                             or suggestion.r_multiple),
        net_result_pips=float(getattr(suggestion, "net_result_pips", 0.0)
                              or suggestion.result_pips),
        mae_pips=float(getattr(suggestion, "mae_pips", 0.0) or 0.0),
        mfe_pips=float(getattr(suggestion, "mfe_pips", 0.0) or 0.0),
        features=dict(getattr(suggestion, "features", None) or {}),
        exposure_at_fill=getattr(suggestion, "exposure_at_fill", None),
    )


@dataclass
class Replay:
    """Everything one pass over the data produced, before it is summarised.

    Kept separate from the finished statistics so a replay can be *split*: a
    parallel run hands each worker a slice of the same history and merges these
    objects, which only works because nothing here has been averaged yet.
    """

    trades: list = field(default_factory=list)
    evaluations: int = 0
    decisions: dict = field(default_factory=lambda: {"BUY": 0, "SELL": 0, "WAIT": 0})
    wait_reasons: dict = field(default_factory=dict)
    scores: list = field(default_factory=list)
    unfilled: int = 0
    # Of those, setups refused at the fill because they would have stacked on
    # an open same-direction trade (``StrategyProfile.skip_stacked``).
    skipped_stacked: int = 0
    skipped: int = 0
    step: int = 1
    resolution_timeframe: str = ""
    costs: object | None = None

    def merge(self, other: "Replay") -> None:
        self.trades.extend(other.trades)
        self.evaluations += other.evaluations
        self.unfilled += other.unfilled
        self.skipped_stacked += other.skipped_stacked
        self.skipped += other.skipped
        self.scores.extend(other.scores)
        for key, count in other.decisions.items():
            self.decisions[key] = self.decisions.get(key, 0) + count
        for key, count in other.wait_reasons.items():
            self.wait_reasons[key] = self.wait_reasons.get(key, 0) + count
        self.step = other.step or self.step
        self.resolution_timeframe = self.resolution_timeframe or other.resolution_timeframe
        self.costs = self.costs or other.costs


def plan(frames: dict[str, pd.DataFrame], step_timeframe: str, step: int,
         max_evaluations: int) -> tuple[int, int, int, pd.Timestamp, pd.Timestamp]:
    """Work out the index range and stride a replay will actually walk.

    Split out of :func:`run` so a parallel run can compute the same range once
    and hand contiguous slices of it to its workers, rather than each worker
    re-deriving a window and disagreeing about it at the edges.
    """
    driver = frames.get(step_timeframe)
    if driver is None or len(driver) < WARMUP_BARS + 50:
        raise ValueError(
            f"Not enough {step_timeframe} history to backtest "
            f"({len(driver) if driver is not None else 0} bars, need "
            f"{WARMUP_BARS + 50})."
        )

    # Begin only where every timeframe has enough history; otherwise the replay
    # silently skips most of its range because one timeframe is too short.
    window_start, window_end = history.usable_window(frames)
    start_index = int(driver.index.searchsorted(window_start))
    start_index = max(start_index, WARMUP_BARS)
    end_index = int(driver.index.searchsorted(window_end, side="right"))
    if end_index - start_index < 20:
        raise ValueError(
            "The overlapping history across timeframes is too short to backtest. "
            f"Usable window: {window_start} to {window_end}."
        )

    # Widen the step rather than run for hours; the effective step is
    # reported so the sampling density is never hidden from the reader.
    planned = max(1, (end_index - start_index) // max(1, step))
    if planned > max_evaluations:
        step = max(step, (end_index - start_index) // max_evaluations + 1)

    return start_index, end_index, step, window_start, window_end


def cost_model_for(frames: dict[str, pd.DataFrame], resolution_tf: str,
                   config=None, point: float | None = None):
    """The cost model a replay charges, measured from the data itself.

    The spread comes from MetaTrader's own per-candle ``spread`` column on the
    timeframe the fills are resolved against, so a backtest pays what the
    broker actually charged on those bars -- including the rollover hour, where
    it doubles. Commission and slippage are account facts no feed exposes, so
    they come from configuration and default to zero rather than being invented.

    ``point`` is the symbol's tick size and is **not optional in practice**:
    MetaTrader records the spread in points, so converting it to a price needs
    the point size, and getting it wrong scales every cost by a power of ten.
    XAUUSDm on this account quotes to three decimals (point 0.001), and
    assuming the two-decimal default turned a real 26-pip round turn into 240.
    Pass ``source.symbol_details()["point"]``; when it is genuinely unavailable
    the point is inferred from the quoted precision of the candles rather than
    assumed, and the model says which happened.
    """
    from ..costs import CostModel

    frame = frames.get(resolution_tf)
    if config is None:
        try:
            from ..config import AppConfig
            config = AppConfig.load().costs
        except Exception:
            config = None
    if config is not None and not getattr(config, "enabled", True):
        return CostModel.disabled("cost modelling is switched off in config.json")

    inferred = False
    if point is None:
        point = _infer_point(frame)
        inferred = True

    model = CostModel.from_frame(
        frame,
        point=point,
        commission_per_lot=float(getattr(config, "commission_per_lot", 0.0) or 0.0),
        slippage_pips=float(getattr(config, "slippage_pips", 0.0) or 0.0),
    )
    if inferred and model.available:
        model.note += f"; point size {point:g} inferred from quoted precision"
    return model


def _infer_point(frame) -> float:
    """The symbol's tick size, read off the precision of its own quotes.

    A fallback for callers with no terminal to ask. Gold is quoted to two or
    three decimals depending on the account, and the difference is a factor of
    ten in every cost figure -- too large to guess at, and too easy to observe
    to need to.
    """
    if frame is None or len(frame) == 0:
        return 0.01
    sample = frame["close"].tail(500)
    decimals = 0
    for value in sample:
        text = f"{float(value):.6f}".rstrip("0")
        decimals = max(decimals, len(text.split(".")[1]) if "." in text else 0)
        if decimals >= 3:
            break
    return 10.0 ** -max(2, min(3, decimals))


def replay(frames: dict[str, pd.DataFrame], profile: StrategyProfile,
           step_timeframe: str = "M15", step: int = 1,
           start_index: int = 0, end_index: int | None = None,
           record_from: pd.Timestamp | None = None,
           progress=None, costs=None) -> Replay:
    """Walk one contiguous slice of history and collect what the book produced.

    ``record_from`` exists for the parallel runner. A chunk is started early --
    before the range it is responsible for -- so its book has the same state a
    sequential run would have reached by then; trades whose entry falls before
    that instant belong to the previous chunk and are dropped here rather than
    being counted twice.
    """
    driver = frames[step_timeframe]
    end_index = len(driver) if end_index is None else end_index

    out = Replay(step=step)
    # The book is the application's own. Running the real one is the whole
    # point: its deduplication, its merging and its lifecycle are part of what
    # is being measured.
    book = SuggestionBook()
    # Setups are resolved against the finest timeframe in the data, which is
    # what the live app does with M1. Falling back to the driver keeps a replay
    # possible on a partial dataset, with coarser fills.
    resolution_tf = next((tf for tf in ("M1", "M5", "M15", "M30")
                          if frames.get(tf) is not None and len(frames[tf]) > 100),
                         step_timeframe)
    resolution_frame = frames.get(resolution_tf, driver)
    out.resolution_timeframe = resolution_tf
    out.costs = costs
    # When each live setup was identified, so holding time can be reported.
    opened_at: dict[str, int] = {}
    total_steps = end_index - start_index

    def keep(entered_at) -> bool:
        """Whether this trade belongs to the slice this pass is responsible for."""
        if record_from is None:
            return True
        return pd.Timestamp(entered_at) >= record_from

    # A replay walks its own clock, so start from a clean analysis cache rather
    # than whatever the live loop last computed. The cache key would keep the
    # two apart anyway; clearing makes that independence explicit instead of
    # load-bearing on a key's construction.
    mtf.reset_cache()

    for i in range(start_index, end_index, step):
        now = driver.index[i]

        sliced = _slice_frames(frames, now)
        if sliced is None:
            out.skipped += 1
            continue

        try:
            analysis = mtf.analyse(sliced)
            decision = decision_engine.decide(analysis, profile)
        except (ValueError, KeyError, IndexError) as exc:
            log.debug("Skipping %s: %s", now, exc)
            continue

        counted = record_from is None or now >= record_from
        if counted:
            out.evaluations += 1
            out.decisions[decision["decision"]] = out.decisions.get(
                decision["decision"], 0) + 1
            out.scores.append(float(decision["score"]))
            if decision["decision"] == "WAIT":
                for reason in decision.get("waiting_for", []) or ["unspecified"]:
                    key = _normalise_reason(reason)
                    out.wait_reasons[key] = out.wait_reasons.get(key, 0) + 1

        # Everything -- new setups, arming, fills, targets, stops, expiry --
        # happens inside the book, exactly as it does live.
        try:
            result = book.sync(
                analysis, decision, profile,
                bars=_resolution_slice(resolution_frame, now, resolution_tf),
                frames=sliced,
                now=now.to_pydatetime(),
                costs=costs,
            )
        except Exception:
            log.warning("Book step failed at %s", now, exc_info=True)
            continue

        for suggestion in book.live:
            opened_at.setdefault(suggestion.id, i)

        for suggestion in result["finished"]:
            entered_at = _as_datetime(suggestion.filled_at, now)
            if not suggestion.entered:
                if counted:
                    out.unfilled += 1
                    if suggestion.exit_reason == "skipped":
                        out.skipped_stacked += 1
                continue
            if not keep(entered_at):
                continue
            out.trades.append(_trade_from(
                suggestion,
                entered_at=entered_at,
                exited_at=_as_datetime(suggestion.exit_at, now),
                bars_held=i - opened_at.get(suggestion.id, i),
            ))

        if progress and out.evaluations % 50 == 0:
            progress(min(1.0, (i - start_index) / max(1, total_steps)))

    # Anything still open at the end of the data is a timeout, not a win: the
    # data ran out, which tells us nothing about how the trade would have gone.
    last = driver.index[min(end_index, len(driver) - 1)]
    for suggestion in book.live:
        if not suggestion.entered:
            continue
        entered_at = _as_datetime(suggestion.filled_at, last)
        if not keep(entered_at):
            continue
        suggestion.exit_price = suggestion.price
        suggestion.exit_reason = "data ended"
        suggestion.outcome = "scratch"
        out.trades.append(_trade_from(
            suggestion,
            entered_at=entered_at,
            exited_at=last.to_pydatetime(),
            bars_held=end_index - opened_at.get(suggestion.id, end_index),
        ))

    return out


def compile_stats(result: Replay, frames: dict[str, pd.DataFrame],
                  step_timeframe: str, window_start, window_end) -> dict:
    """Turn a finished (or merged) replay into the reported statistics."""
    trades = result.trades
    stats = summarise(trades, result.evaluations, result.decisions)
    stats["unfilled_setups"] = result.unfilled
    stats["skipped_stacked"] = result.skipped_stacked
    stats["resolution_timeframe"] = result.resolution_timeframe
    stats["by_strategy"] = _group(trades, lambda t: t.strategy_name or t.strategy)
    stats["by_family"] = _group(trades, lambda t: t.family or "unclassified")
    stats["by_exit"] = _group(trades, lambda t: t.exit_reason or "unknown")
    # Which fill rule produced these trades. Runs before this key existed
    # filled at the best price of the bar and are optimistic by construction;
    # anything reading a stored run's figures back needs to be able to tell.
    stats["fill_model"] = FILL_MODEL
    stats["score_basis"] = SCORE_BASIS
    # Standalone trades against trades stacked on an open same-direction
    # position. If the stacked ones earn materially less, full size on each is
    # the wrong advice -- and this is the table that says so.
    stats["by_exposure"] = _group(
        trades, lambda t: ("unknown" if t.exposure_at_fill is None
                           else "standalone" if t.exposure_at_fill == 0
                           else "stacked"))
    # Agreement at the fill. The closing count looked strongly predictive
    # (+0.27R at five or more against +0.02R below, Mar-Sep 2026) but is
    # contaminated by agreement that arrived after entry; this is the honest
    # version of that table, and the one any sizing rule has to be built on.
    stats["by_confirmations_at_fill"] = _group(
        trades, lambda t: ("unknown" if t.confirmations_at_fill is None
                           else "0-2" if t.confirmations_at_fill <= 2
                           else "3-4" if t.confirmations_at_fill <= 4
                           else "5+"))
    stats["best_strategy"] = _best(stats["by_strategy"])
    stats["worst_strategy"] = _worst(stats["by_strategy"])
    stats["total_pips"] = round(sum(t.result_pips for t in trades), 1)
    stats["net_total_pips"] = round(sum(t.net_result_pips for t in trades), 1)
    stats["total_cost_pips"] = round(sum(t.cost_pips for t in trades), 1)
    stats["costs"] = result.costs.to_dict() if result.costs is not None else None
    stats["window"] = {"from": window_start.isoformat(), "to": window_end.isoformat()}
    stats["skipped_steps"] = result.skipped
    stats["wait_reasons"] = dict(
        sorted(result.wait_reasons.items(), key=lambda kv: -kv[1])[:10]
    )
    stats["score_distribution"] = _score_distribution(result.scores)
    stats["coverage"] = history.describe(frames)
    stats["step"] = result.step
    stats["step_timeframe"] = step_timeframe
    # Whether any of the above is distinguishable from luck, and what the
    # account actually went through as opposed to what the trade list shows.
    # Computed from the trade records rather than during the replay, so it
    # costs nothing and can be re-run on any stored result.
    stats["rigour"] = rigour.assess([t.to_dict() for t in trades])
    return stats


def run(frames: dict[str, pd.DataFrame], profile: StrategyProfile,
        step_timeframe: str = "M15", max_hold_bars: int = 96,
        step: int = 1, progress=None,
        max_evaluations: int = DEFAULT_MAX_EVALUATIONS,
        costs=None) -> dict:
    """Replay history and collect every setup the application would have shown.

    ``step_timeframe`` is the clock: the engines are re-run on each closed
    candle of that timeframe. ``max_hold_bars`` is retained for callers that
    pass it, but how long a setup may live is now the profile's own
    ``setup_expiry_minutes`` and ``trade_expiry_minutes`` -- the same limits the
    live app applies, which is the point of replaying the real book.
    """
    start_index, end_index, step, window_start, window_end = plan(
        frames, step_timeframe, step, max_evaluations)

    resolution_tf = next((tf for tf in ("M1", "M5", "M15", "M30")
                          if frames.get(tf) is not None and len(frames[tf]) > 100),
                         step_timeframe)
    if costs is None:
        costs = cost_model_for(frames, resolution_tf)

    result = replay(frames, profile, step_timeframe, step,
                    start_index=start_index, end_index=end_index,
                    progress=progress, costs=costs)
    stats = compile_stats(result, frames, step_timeframe, window_start, window_end)
    return {"stats": stats, "trades": [t.to_dict() for t in result.trades]}


def _normalise_reason(reason: str) -> str:
    """Collapse a waiting reason to its stable prefix.

    The raw strings embed live numbers ("score is 54, the profile requires 70"),
    which would otherwise make every occurrence a unique bucket.
    """
    for marker in (" - ", " (currently", " - score is", ","):
        if marker in reason:
            reason = reason.split(marker)[0]
    return reason.strip()[:90]


def _score_distribution(scores: list[float]) -> dict:
    """How close the engine got, so a strict threshold is visible not guessed."""
    if not scores:
        return {}
    arr = np.array(scores)
    return {
        "evaluations": len(scores),
        "mean": round(float(arr.mean()), 1),
        "median": round(float(np.median(arr)), 1),
        "p90": round(float(np.percentile(arr, 90)), 1),
        "max": round(float(arr.max()), 1),
        "at_or_above_60": int((arr >= 60).sum()),
        "at_or_above_70": int((arr >= 70).sum()),
        "at_or_above_80": int((arr >= 80).sum()),
    }


def _slice_frames(frames: dict[str, pd.DataFrame], now: pd.Timestamp) -> dict | None:
    """Return each timeframe truncated to candles that had closed by ``now``.

    This is what keeps the replay honest: no engine ever sees a candle from the
    future, and higher timeframes only advance when their own candle closes.

    The test is on the candle's *close* time, not its index. MetaTrader labels
    a candle with the moment it opened, so at 12:00 the D1 candle indexed
    00:00 is still forming and its OHLC covers the entire day -- half of it
    unwritten. Filtering on the index (either `<= now` or `< now`) admits that
    candle and leaks up to a full timeframe of future data into every engine,
    which is exactly the bug this function was written to prevent and did not.
    """
    out = {}
    for tf in BACKTEST_TIMEFRAMES:
        df = frames.get(tf)
        if df is None:
            continue
        # Found by binary search, not by a boolean mask.
        #
        # `df.index + duration <= now` is the correct test and the expensive
        # way to ask it: it builds a whole new DatetimeIndex and a whole
        # boolean array over every candle, on every timeframe, on every step.
        # With M5 at 60,000 bars that is hundreds of thousands of element
        # comparisons per evaluation, and it is why a replay got slower the
        # more history was loaded into it -- exactly backwards.
        #
        # The index is sorted and the duration is constant, so the condition
        # rearranges to `index <= now - duration` and `searchsorted` answers it
        # in log time. Same candles, same boundary, no allocation.
        cutoff = now - pd.Timedelta(minutes=TIMEFRAME_MINUTES.get(tf, 1))
        end = int(df.index.searchsorted(cutoff, side="right"))
        if end < 60:
            return None
        # Only the tail matters to the engines; slicing it here is what makes
        # a replay of thousands of steps finish in minutes rather than hours.
        out[tf] = df.iloc[max(0, end - ANALYSIS_BARS):end]
    return out or None


def _resolution_slice(frame: pd.DataFrame, now: pd.Timestamp,
                      timeframe: str) -> pd.DataFrame | None:
    """Candles the resolver may see at this step: those closed by ``now``.

    The resolver itself skips anything it has already walked and anything older
    than the setup, so handing it a trailing window is both correct and cheap.

    Same open-time trap as :func:`_slice_frames`, and worse here: the candle
    indexed exactly ``now`` has not begun, so admitting it lets the resolver
    fill an entry or hit a stop on a bar that does not exist yet. Live
    resolution reads ``tracking_frame(closed_only=True)``, which drops the
    forming bar; this is the replay's equivalent.
    """
    if frame is None or frame.empty:
        return None
    # Same binary search as `_slice_frames`, for the same reason: this frame is
    # the finest one in the data and therefore the longest.
    cutoff = now - pd.Timedelta(minutes=TIMEFRAME_MINUTES.get(timeframe, 1))
    end = int(frame.index.searchsorted(cutoff, side="right"))
    if end == 0:
        return None
    return frame.iloc[max(0, end - RESOLUTION_BARS):end]


def _as_datetime(value, fallback: pd.Timestamp) -> datetime:
    """An ISO stamp from the resolver, or the replay clock if it is missing."""
    if value:
        try:
            return datetime.fromisoformat(str(value))
        except ValueError:
            pass
    return fallback.to_pydatetime()


def _session_of(ts: pd.Timestamp) -> str:
    hour = ts.hour
    for name, (start, end) in SESSIONS.items():
        if start <= hour < end:
            return name
    return "offhours"


def summarise(trades: list[BacktestTrade], evaluations: int,
              decisions: dict[str, int]) -> dict:
    """Compute the performance statistics the specification lists."""
    resolved = [t for t in trades if t.result in ("win", "loss", "timeout")]
    wins = [t for t in resolved if t.result == "win"]
    losses = [t for t in resolved if t.result == "loss"]
    timeouts = [t for t in resolved if t.result == "timeout"]

    r_values = [t.r_multiple for t in resolved]
    gross_win = sum(r for r in r_values if r > 0)
    gross_loss = abs(sum(r for r in r_values if r < 0))

    equity = np.cumsum(r_values) if r_values else np.array([0.0])
    peak = np.maximum.accumulate(equity)
    drawdown = float((peak - equity).max()) if len(equity) else 0.0

    # The same statistics after the broker has been paid.
    #
    # Reported beside the gross ones rather than instead of them, because the
    # difference between the two is itself the finding: a method whose gross
    # expectancy is +0.15R and whose net expectancy is -0.02R does not have a
    # small edge, it has none, and only showing both makes that visible.
    net_values = [t.net_r_multiple for t in resolved]
    net_win = sum(r for r in net_values if r > 0)
    net_loss = abs(sum(r for r in net_values if r < 0))
    net_equity = np.cumsum(net_values) if net_values else np.array([0.0])
    net_peak = np.maximum.accumulate(net_equity)
    net_drawdown = float((net_peak - net_equity).max()) if len(net_equity) else 0.0
    net_wins = [t for t in resolved if t.net_r_multiple > 0]

    return {
        "net_win_rate": round(len(net_wins) / len(resolved) * 100, 1) if resolved else None,
        "net_average_r": round(float(np.mean(net_values)), 3) if net_values else None,
        "net_expectancy_r": round(float(np.mean(net_values)), 3) if net_values else None,
        "net_profit_factor": round(net_win / net_loss, 2) if net_loss > 0 else None,
        "net_total_r": round(float(sum(net_values)), 2) if net_values else 0.0,
        "net_max_drawdown_r": round(net_drawdown, 2),
        "average_cost_pips": round(float(np.mean([t.cost_pips for t in resolved])), 1)
        if resolved else None,
        "evaluations": evaluations,
        "decisions": decisions,
        "total_setups": len(resolved),
        "wins": len(wins),
        "losses": len(losses),
        "timeouts": len(timeouts),
        "win_rate": round(len(wins) / len(resolved) * 100, 1) if resolved else None,
        "average_r": round(float(np.mean(r_values)), 3) if r_values else None,
        "expectancy_r": round(float(np.mean(r_values)), 3) if r_values else None,
        "average_reward_risk": round(
            float(np.mean([abs(t.target - t.entry) / abs(t.entry - t.stop)
                           for t in resolved if t.entry != t.stop])), 2
        ) if resolved else None,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
        "total_r": round(float(sum(r_values)), 2) if r_values else 0.0,
        "max_drawdown_r": round(drawdown, 2),
        "max_consecutive_wins": _streak(resolved, "win"),
        "max_consecutive_losses": _streak(resolved, "loss"),
        "average_bars_held": round(float(np.mean([t.bars_held for t in resolved])), 1)
        if resolved else None,
        "by_session": _group(resolved, lambda t: t.session),
        "by_regime": _group(resolved, lambda t: t.regime),
        "by_direction": _group(resolved, lambda t: t.direction),
        "by_score_band": _group(resolved, lambda t: _score_band(t.score)),
        "best_session": _best(_group(resolved, lambda t: t.session)),
        "worst_session": _worst(_group(resolved, lambda t: t.session)),
        "best_regime": _best(_group(resolved, lambda t: t.regime)),
        "worst_regime": _worst(_group(resolved, lambda t: t.regime)),
    }


def _streak(trades: list[BacktestTrade], result: str) -> int:
    best = current = 0
    for t in trades:
        if t.result == result:
            current += 1
            best = max(best, current)
        else:
            current = 0
    return best


def _group(trades: list[BacktestTrade], key) -> dict:
    buckets: dict[str, list[BacktestTrade]] = {}
    for t in trades:
        buckets.setdefault(key(t), []).append(t)
    out = {}
    for name, items in buckets.items():
        wins = sum(1 for t in items if t.result == "win")
        out[name] = {
            "setups": len(items),
            "wins": wins,
            "win_rate": round(wins / len(items) * 100, 1),
            "total_r": round(sum(t.r_multiple for t in items), 2),
            "average_r": round(float(np.mean([t.r_multiple for t in items])), 3),
            "net_total_r": round(sum(t.net_r_multiple for t in items), 2),
            "net_average_r": round(float(np.mean([t.net_r_multiple for t in items])), 3),
        }
    return out


def _score_band(score: float) -> str:
    if score >= 85:
        return "85-100"
    if score >= 75:
        return "75-85"
    if score >= 65:
        return "65-75"
    return "below 65"


def _best(groups: dict) -> str | None:
    # Ranked on net total R, not win rate: a high win rate on two setups is
    # noise, and a gross ranking flatters whichever strategy takes the most
    # trades -- costs are charged per trade, so frequency is exactly what the
    # gross figure fails to penalise.
    eligible = {k: v for k, v in groups.items() if v["setups"] >= 3}
    return max(eligible, key=lambda k: eligible[k]["net_total_r"]) if eligible else None


def _worst(groups: dict) -> str | None:
    eligible = {k: v for k, v in groups.items() if v["setups"] >= 3}
    return min(eligible, key=lambda k: eligible[k]["net_total_r"]) if eligible else None
