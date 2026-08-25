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
from dataclasses import dataclass, asdict
from datetime import datetime

import numpy as np
import pandas as pd

from ..analysis import mtf
from ..config import BACKTEST_TIMEFRAMES
from ..decision import engine as decision_engine
from ..decision.profile import StrategyProfile
from ..signals import SuggestionBook
from . import history

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

# Guard against accidentally requesting a multi-day replay: one evaluation runs
# the full multi-timeframe analysis, so the step is widened to stay under this.
DEFAULT_MAX_EVALUATIONS = 600

SESSIONS = {"asia": (0, 8), "london": (7, 16), "newyork": (12, 21)}


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
    confirmations: int = 0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["entered_at"] = self.entered_at.isoformat()
        d["exited_at"] = self.exited_at.isoformat() if self.exited_at else None
        for k in ("entry", "stop", "target", "exit_price"):
            if d[k] is not None:
                d[k] = round(float(d[k]), 3)
        d["r_multiple"] = round(self.r_multiple, 2)
        d["result_pips"] = round(self.result_pips, 1)
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
        score=float(suggestion.score),
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
    )


def run(frames: dict[str, pd.DataFrame], profile: StrategyProfile,
        step_timeframe: str = "M15", max_hold_bars: int = 96,
        step: int = 1, progress=None,
        max_evaluations: int = DEFAULT_MAX_EVALUATIONS) -> dict:
    """Replay history and collect every setup the application would have shown.

    ``step_timeframe`` is the clock: the engines are re-run on each closed
    candle of that timeframe. ``max_hold_bars`` is retained for callers that
    pass it, but how long a setup may live is now the profile's own
    ``setup_expiry_minutes`` and ``trade_expiry_minutes`` -- the same limits the
    live app applies, which is the point of replaying the real book.
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

    trades: list[BacktestTrade] = []
    evaluations = 0
    decisions_counted = {"BUY": 0, "SELL": 0, "WAIT": 0}

    total_steps = end_index - start_index
    skipped = 0
    # Why the engine declined, tallied so a zero-setup run is explainable.
    wait_reasons: dict[str, int] = {}
    scores: list[float] = []

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
    # When each live setup was identified, so holding time can be reported.
    opened_at: dict[str, int] = {}
    unfilled = 0

    # A replay walks its own clock, so start from a clean analysis cache rather
    # than whatever the live loop last computed. The cache key would keep the
    # two apart anyway; clearing makes that independence explicit instead of
    # load-bearing on a key's construction.
    mtf.reset_cache()

    for i in range(start_index, end_index, step):
        now = driver.index[i]

        sliced = _slice_frames(frames, now)
        if sliced is None:
            skipped += 1
            continue

        try:
            analysis = mtf.analyse(sliced)
            decision = decision_engine.decide(analysis, profile)
        except (ValueError, KeyError, IndexError) as exc:
            log.debug("Skipping %s: %s", now, exc)
            continue

        evaluations += 1
        decisions_counted[decision["decision"]] = decisions_counted.get(decision["decision"], 0) + 1
        scores.append(float(decision["score"]))
        if decision["decision"] == "WAIT":
            for reason in decision.get("waiting_for", []) or ["unspecified"]:
                key = _normalise_reason(reason)
                wait_reasons[key] = wait_reasons.get(key, 0) + 1

        # Everything -- new setups, arming, fills, targets, stops, expiry --
        # happens inside the book, exactly as it does live.
        try:
            result = book.sync(
                analysis, decision, profile,
                bars=_resolution_slice(resolution_frame, now),
                frames=sliced,
                now=now.to_pydatetime(),
            )
        except Exception:
            log.warning("Book step failed at %s", now, exc_info=True)
            continue

        for suggestion in book.live:
            opened_at.setdefault(suggestion.id, i)

        for suggestion in result["finished"]:
            if not suggestion.entered:
                unfilled += 1
                continue
            trades.append(_trade_from(
                suggestion,
                entered_at=_as_datetime(suggestion.filled_at, now),
                exited_at=_as_datetime(suggestion.exit_at, now),
                bars_held=i - opened_at.get(suggestion.id, i),
            ))

        if progress and evaluations % 50 == 0:
            progress(min(1.0, (i - start_index) / max(1, total_steps)))

    # Anything still open at the end of the data is a timeout, not a win: the
    # data ran out, which tells us nothing about how the trade would have gone.
    last = driver.index[min(end_index, len(driver) - 1)]
    for suggestion in book.live:
        if not suggestion.entered:
            continue
        suggestion.exit_price = suggestion.price
        suggestion.exit_reason = "data ended"
        suggestion.outcome = "scratch"
        trades.append(_trade_from(
            suggestion,
            entered_at=_as_datetime(suggestion.filled_at, last),
            exited_at=last.to_pydatetime(),
            bars_held=end_index - opened_at.get(suggestion.id, end_index),
        ))

    stats = summarise(trades, evaluations, decisions_counted)
    stats["unfilled_setups"] = unfilled
    stats["resolution_timeframe"] = resolution_tf
    stats["by_strategy"] = _group(trades, lambda t: t.strategy_name or t.strategy)
    stats["by_family"] = _group(trades, lambda t: t.family or "unclassified")
    stats["by_exit"] = _group(trades, lambda t: t.exit_reason or "unknown")
    stats["best_strategy"] = _best(stats["by_strategy"])
    stats["worst_strategy"] = _worst(stats["by_strategy"])
    stats["total_pips"] = round(sum(t.result_pips for t in trades), 1)
    stats["window"] = {"from": window_start.isoformat(), "to": window_end.isoformat()}
    stats["skipped_steps"] = skipped
    stats["wait_reasons"] = dict(
        sorted(wait_reasons.items(), key=lambda kv: -kv[1])[:10]
    )
    stats["score_distribution"] = _score_distribution(scores)
    stats["coverage"] = history.describe(frames)
    stats["step"] = step
    stats["step_timeframe"] = step_timeframe
    return {"stats": stats, "trades": [t.to_dict() for t in trades]}


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
    """
    out = {}
    for tf in BACKTEST_TIMEFRAMES:
        df = frames.get(tf)
        if df is None:
            continue
        visible = df.loc[df.index <= now]
        if len(visible) < 60:
            return None
        # Only the tail matters to the engines; slicing it here is what makes
        # a replay of thousands of steps finish in minutes rather than hours.
        out[tf] = visible.iloc[-ANALYSIS_BARS:]
    return out or None


def _resolution_slice(frame: pd.DataFrame, now: pd.Timestamp) -> pd.DataFrame | None:
    """Candles the resolver may see at this step: everything up to ``now``.

    The resolver itself skips anything it has already walked and anything older
    than the setup, so handing it a trailing window is both correct and cheap.
    """
    if frame is None or frame.empty:
        return None
    visible = frame.loc[frame.index <= now]
    if visible.empty:
        return None
    return visible.iloc[-RESOLUTION_BARS:]


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

    return {
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
    # Ranked on total R, not win rate: a high win rate on two setups is noise.
    eligible = {k: v for k, v in groups.items() if v["setups"] >= 3}
    return max(eligible, key=lambda k: eligible[k]["total_r"]) if eligible else None


def _worst(groups: dict) -> str | None:
    eligible = {k: v for k, v in groups.items() if v["setups"] >= 3}
    return min(eligible, key=lambda k: eligible[k]["total_r"]) if eligible else None
