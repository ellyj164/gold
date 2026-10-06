"""The statistics that decide whether a backtest result means anything.

``engine.summarise`` reports what happened: win rate, profit factor, expectancy,
drawdown. Those are the right numbers and they share a blind spot -- none of
them can distinguish a real edge from a lucky sample, and none of them describe
what the *account* experienced as opposed to what the trade list did.

This module adds the four readings that close that gap. They were identified by
auditing the engine's own stored results against the standard an institutional
research desk would apply, and each one exists because the figure beside it was
misleading without it.

**Significance.** An expectancy of +0.35R over 738 trades sounds decisive and a
t-statistic tells you whether it is. The catch is that trades overlap: this
system runs up to five positions at once, so consecutive trades are not
independent draws and the naive t-statistic on trade-level returns overstates
its own confidence. So the test is also run on **monthly** totals, where the
clustering has been aggregated away, and the monthly figure is the one to
believe when they disagree.

**Mark-to-market drawdown.** ``max_drawdown_r`` is computed on a trade-close
sequence: it asks what the cumulative sum of results did as each trade was
booked. That is not what the account did. Two positions open together are two
positions of risk, and an equity curve marked as trades *close* cannot see the
moment both were underwater simultaneously. The concurrency-aware figure is
always the larger one, and it is the one that describes the experience.

**Concurrency.** How many positions the method actually wanted open at once. It
is reported because every R-based figure in the summary silently assumes one
unit of risk per trade, and that assumption is only harmless if the positions
do not overlap. They do.

**Cost sensitivity.** Slippage is not modelled -- it cannot be observed from a
candle feed -- so instead of guessing a number this reports how much the result
would change at several. On a method with wide structural stops the answer
turns out to be "almost nothing", and knowing that is worth more than a guessed
constant would be.

Nothing here re-runs a replay. It reads the trade list a run already produced,
so it is free and can be pointed at any stored result.
"""

from __future__ import annotations

import math
import statistics as stats
from collections import defaultdict
from datetime import datetime

# XAUUSD pip convention, matching costs.PIP.
PIP = 0.01

# Trades below this and no statistical claim is worth making at all.
MIN_TRADES = 20

# |t| above this is conventional 95% significance, two-sided.
T_SIGNIFICANT = 1.96

# Slippage levels the sensitivity table reports, in pips per side.
SLIPPAGE_LEVELS = (0.5, 1.0, 2.0, 3.0)

# Months below this many and the monthly test is not run: twelve monthly
# observations is already a small sample and six is not a sample at all.
MIN_MONTHS = 6


def _net_r(trade: dict) -> float:
    value = trade.get("net_r_multiple")
    if value is None:
        value = trade.get("r_multiple", 0.0)
    return float(value or 0.0)


def _parse(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except (ValueError, TypeError):
        return None


def significance(trades: list[dict]) -> dict:
    """Is the mean result distinguishable from zero, at trade and month scale.

    Both are reported because they answer the same question with different
    assumptions, and the difference between them is itself the finding: when
    the trade-level test is confident and the monthly one is not, the
    confidence was coming from overlapping positions rather than from evidence.
    """
    values = [_net_r(t) for t in trades]
    n = len(values)
    if n < MIN_TRADES:
        return {"available": False,
                "note": f"{n} trades is too few to test; {MIN_TRADES} is the floor."}

    mean = stats.mean(values)
    deviation = stats.pstdev(values)
    t_trade = (mean / (deviation / math.sqrt(n))) if deviation > 0 else None

    monthly = defaultdict(float)
    for trade in trades:
        when = _parse(trade.get("entered_at"))
        if when is not None:
            monthly[f"{when.year:04d}-{when.month:02d}"] += _net_r(trade)
    months = [monthly[k] for k in sorted(monthly)]

    t_month = sharpe = None
    if len(months) >= MIN_MONTHS:
        month_sd = stats.pstdev(months)
        if month_sd > 0:
            t_month = stats.mean(months) / (month_sd / math.sqrt(len(months)))
            # Annualised from monthly totals, risk-free rate zero. Reported
            # with the same caveat it always deserves: an R-denominated Sharpe
            # assumes constant risk per trade and no compounding, so it is a
            # comparison figure and not a claim about an account.
            sharpe = stats.mean(months) / month_sd * math.sqrt(12)

    verdict = _significance_verdict(t_trade, t_month, len(months))

    return {
        "available": True,
        "trades": n,
        "mean_r": round(mean, 4),
        "stdev_r": round(deviation, 3),
        "t_statistic": round(t_trade, 2) if t_trade is not None else None,
        "significant": bool(t_trade is not None and abs(t_trade) >= T_SIGNIFICANT),
        "months": len(months),
        "monthly_t_statistic": round(t_month, 2) if t_month is not None else None,
        "monthly_significant": bool(t_month is not None and abs(t_month) >= T_SIGNIFICANT),
        "positive_months": sum(1 for m in months if m > 0),
        "monthly_returns_r": [round(m, 2) for m in months],
        "annualised_sharpe": round(sharpe, 2) if sharpe is not None else None,
        "verdict": verdict,
    }


def _significance_verdict(t_trade, t_month, months: int) -> str:
    if t_trade is None:
        return "The result has no variance to test against."
    if t_month is None:
        return (f"Trade-level t = {t_trade:+.2f}. There are only {months} months "
                "of results, too few to check whether that confidence survives "
                "the fact that positions overlap. Treat it as provisional.")
    trade_ok = abs(t_trade) >= T_SIGNIFICANT
    month_ok = abs(t_month) >= T_SIGNIFICANT
    if trade_ok and month_ok:
        return (f"Trade-level t = {t_trade:+.2f} and monthly t = {t_month:+.2f}: "
                "the edge survives aggregation, so it is not an artefact of "
                "overlapping positions.")
    if trade_ok and not month_ok:
        return (f"Trade-level t = {t_trade:+.2f} looks decisive but monthly "
                f"t = {t_month:+.2f} does not. The trade-level confidence is "
                "coming from overlapping positions being counted as independent. "
                "Believe the monthly figure.")
    if month_ok:
        return (f"Monthly t = {t_month:+.2f} clears significance even though "
                f"trade-level t = {t_trade:+.2f} does not — the edge is in the "
                "consistency across months rather than in any single trade.")
    return (f"Neither test clears significance (trade t = {t_trade:+.2f}, monthly "
            f"t = {t_month:+.2f}). This sample cannot distinguish the result "
            "from luck.")


def concurrency(trades: list[dict]) -> dict:
    """How many positions the method wanted open at once, and for how long.

    Every R figure in the summary assumes one unit of risk per trade. That is
    only harmless when trades do not overlap, so this measures whether they do.
    """
    events: list[tuple[datetime, int]] = []
    for trade in trades:
        entered, exited = _parse(trade.get("entered_at")), _parse(trade.get("exited_at"))
        if entered is None or exited is None or exited < entered:
            continue
        events.append((entered, 1))
        events.append((exited, -1))
    if not events:
        return {"available": False, "note": "No trade had both an entry and an exit."}

    # Exits before entries at the same instant, so a position that closes as
    # another opens is not counted as two.
    events.sort(key=lambda e: (e[0], e[1]))

    current = peak = 0
    hours: dict[int, float] = defaultdict(float)
    previous = None
    for when, delta in events:
        if previous is not None and current > 0:
            hours[current] += (when - previous).total_seconds() / 3600.0
        current += delta
        peak = max(peak, current)
        previous = when

    total = sum(hours.values())
    average = (sum(k * v for k, v in hours.items()) / total) if total else 0.0

    return {
        "available": True,
        "peak": peak,
        "average_while_open": round(average, 2),
        "hours_by_level": {str(k): round(v, 1) for k, v in sorted(hours.items())},
        "share_multiple": round(
            sum(v for k, v in hours.items() if k > 1) / total, 3) if total else 0.0,
        "note": (
            f"Peak {peak} positions open at once, averaging "
            f"{average:.2f} while in the market. Every R figure assumes one unit "
            f"of risk per trade, so at the peak the method was asking for {peak}R "
            "of simultaneous exposure."),
    }


def drawdown(trades: list[dict]) -> dict:
    """Drawdown as the account experienced it, not as the trade list did.

    Two curves are built. The **booked** one is the cumulative sum of results
    in exit order -- what ``engine.summarise`` reports. The **mark-to-market**
    one revalues every open position at each event, so a period with three
    positions all underwater shows the full excursion rather than only what was
    realised when they eventually closed.

    Marking uses each trade's own recorded worst excursion where the resolver
    stored one, and falls back to its final result where it did not. That makes
    the figure a conservative lower bound on the true mark-to-market drawdown
    rather than an estimate of it, which is the right direction for a number
    whose whole purpose is to stop a drawdown being underestimated.
    """
    resolved = [t for t in trades if _parse(t.get("exited_at")) is not None]
    if not resolved:
        return {"available": False, "note": "No resolved trades to measure."}

    booked = _booked_drawdown(resolved)
    marked = _marked_drawdown(resolved)

    return {
        "available": True,
        "booked_r": round(booked["depth"], 2),
        "booked_longest_days": booked["days"],
        "mark_to_market_r": round(marked, 2),
        "note": (
            f"Booked drawdown is {booked['depth']:.2f}R and the longest time "
            f"under water was {booked['days']} days. Marked to market — "
            f"revaluing positions while they were still open — it reached "
            f"{marked:.2f}R. The second figure is what the account went "
            "through; the first is what the trade list shows afterwards."),
    }


def _booked_drawdown(trades: list[dict]) -> dict:
    ordered = sorted(trades, key=lambda t: _parse(t["exited_at"]))
    equity = peak = depth = 0.0
    peak_at = None
    longest = 0
    for trade in ordered:
        equity += _net_r(trade)
        when = _parse(trade["exited_at"])
        if equity >= peak:
            peak, peak_at = equity, when
        else:
            depth = max(depth, peak - equity)
            if peak_at is not None:
                longest = max(longest, (when - peak_at).days)
    return {"depth": depth, "days": longest}


def _marked_drawdown(trades: list[dict]) -> float:
    """Peak-to-trough on a curve that revalues open positions.

    Each trade contributes its worst excursion from the moment it opens until
    it closes, at which point it contributes its actual result. The excursion
    comes from the stored MAE where the resolver recorded one; a trade with no
    MAE contributes min(result, 0), which understates rather than invents.
    """
    events: list[tuple[datetime, str, dict]] = []
    for trade in trades:
        entered, exited = _parse(trade.get("entered_at")), _parse(trade["exited_at"])
        if entered is None:
            continue
        events.append((entered, "open", trade))
        events.append((exited, "close", trade))
    if not events:
        return 0.0
    events.sort(key=lambda e: (e[0], 0 if e[1] == "close" else 1))

    closed = 0.0
    open_trades: list[dict] = []
    peak = worst = 0.0

    for _, kind, trade in events:
        if kind == "open":
            open_trades.append(trade)
        else:
            if trade in open_trades:
                open_trades.remove(trade)
            closed += _net_r(trade)

        floating = sum(_worst_case(t) for t in open_trades)
        equity = closed + floating
        peak = max(peak, closed)
        worst = max(worst, peak - equity)
    return worst


def _worst_case(trade: dict) -> float:
    """The worst this open position was worth, in R. Never positive."""
    mae = trade.get("mae_pips")
    risk = None
    try:
        entry, stop = float(trade["entry"]), float(trade["stop"])
        risk = abs(entry - stop)
    except (KeyError, TypeError, ValueError):
        risk = None
    if mae is not None and risk:
        return -abs(float(mae) * PIP / risk)
    return min(0.0, _net_r(trade))


def cost_sensitivity(trades: list[dict]) -> dict:
    """How much the result changes at several unmodelled slippage levels.

    Reported instead of a guessed slippage constant. The point is not to pick
    the right number -- no candle feed can observe it -- but to show whether
    the answer depends on it. Where the stops are wide, it does not.
    """
    usable = []
    for trade in trades:
        try:
            risk = abs(float(trade["entry"]) - float(trade["stop"]))
        except (KeyError, TypeError, ValueError):
            continue
        if risk > 0:
            usable.append((_net_r(trade), risk))
    if not usable:
        return {"available": False, "note": "No trade carried a usable stop distance."}

    baseline = sum(r for r, _ in usable)
    rows = []
    for pips in SLIPPAGE_LEVELS:
        cost = pips * 2 * PIP
        total = sum(r - cost / risk for r, risk in usable)
        rows.append({
            "pips_per_side": pips,
            "total_r": round(total, 2),
            "average_r": round(total / len(usable), 4),
            "cost_r": round(baseline - total, 2),
        })

    heaviest = rows[-1]
    share = ((baseline - heaviest["total_r"]) / baseline * 100) if baseline else 0.0
    return {
        "available": True,
        "baseline_total_r": round(baseline, 2),
        "levels": rows,
        "note": (
            f"At {SLIPPAGE_LEVELS[-1]:.0f} pips of slippage per side the result "
            f"falls from {baseline:.1f}R to {heaviest['total_r']:.1f}R — "
            f"{share:.1f}% of the edge. "
            + ("Slippage is not a material risk to this method's result; the "
               "stops are wide enough to absorb it."
               if share < 10 else
               "Slippage matters materially here and should be measured on the "
               "account rather than left unmodelled.")),
    }


# Resamples used by the Monte Carlo readings. Fixed seed so the same stored
# run always prints the same band -- a report that changes on every reading
# invites choosing the reading that looks best.
MONTE_CARLO_RUNS = 5000
MONTE_CARLO_SEED = 20260912
# Below this many live trades the forward check says its band is too narrow.
CLUSTER_CAVEAT_TRADES = 50


def _percentile(sorted_values: list[float], share: float) -> float:
    if not sorted_values:
        return 0.0
    index = min(len(sorted_values) - 1, max(0, int(round(share * (len(sorted_values) - 1)))))
    return sorted_values[index]


def monte_carlo(trades: list[dict], runs: int = MONTE_CARLO_RUNS,
                seed: int = MONTE_CARLO_SEED) -> dict:
    """The drawdown and losing-streak the same trades could have produced in
    another order.

    The booked drawdown is one draw from a distribution: the order the trades
    happened to arrive in. Shuffling the sequence many times shows how much of
    that figure was the edge and how much was the luck of the ordering, and
    the 95th percentile is the drawdown to plan for rather than the one that
    happened. The live account will see its own ordering, not this backtest's.
    """
    import random

    values = [_net_r(t) for t in trades]
    if len(values) < MIN_TRADES:
        return {"available": False,
                "note": f"{len(values)} trades is too few to resample."}

    rng = random.Random(seed)
    depths: list[float] = []
    streaks: list[int] = []
    sequence = list(values)
    for _ in range(runs):
        rng.shuffle(sequence)
        equity = peak = depth = 0.0
        streak = longest = 0
        for value in sequence:
            equity += value
            peak = max(peak, equity)
            depth = max(depth, peak - equity)
            streak = streak + 1 if value < 0 else 0
            longest = max(longest, streak)
        depths.append(depth)
        streaks.append(longest)
    depths.sort()
    streaks.sort()

    return {
        "available": True,
        "runs": runs,
        "drawdown_r": {"p50": round(_percentile(depths, 0.50), 2),
                       "p95": round(_percentile(depths, 0.95), 2),
                       "p99": round(_percentile(depths, 0.99), 2)},
        "losing_streak": {"p50": _percentile(streaks, 0.50),
                          "p95": _percentile(streaks, 0.95),
                          "p99": _percentile(streaks, 0.99)},
        "note": (
            f"Reshuffled {runs} times, the same trades produce a median booked "
            f"drawdown of {_percentile(depths, 0.50):.1f}R and a 95th percentile "
            f"of {_percentile(depths, 0.95):.1f}R, with a 95th-percentile losing "
            f"streak of {_percentile(streaks, 0.95):.0f}. Plan for the 95th "
            "percentile; the historical ordering was one draw."),
    }


def forward_check(trades: list[dict], live_values: list[float],
                  runs: int = MONTE_CARLO_RUNS, seed: int = MONTE_CARLO_SEED) -> dict:
    """Whether a live record is something this backtest could have produced.

    Draws ``len(live_values)`` trades at random from the backtest, many times,
    and asks where the live total falls in that distribution. Below the 5th
    percentile the backtest is not describing the live system -- a simulator
    that is more optimistic than the market, or a market that has changed --
    and no amount of backtest statistics should be trusted until the reason is
    found. This is the acceptance test every other figure answers to.

    Gross R on both sides: the live outcome record stores gross results, and
    comparing a net backtest with a gross live record would flatter the live
    side by exactly the costs.
    """
    import random

    pool = [float(t.get("r_multiple") or 0.0) for t in trades]
    n = len(live_values)
    if len(pool) < MIN_TRADES or n == 0:
        return {"available": False,
                "note": f"Need {MIN_TRADES}+ backtest trades and at least one live "
                        f"trade (have {len(pool)} and {n})."}

    rng = random.Random(seed)
    totals = sorted(sum(rng.choice(pool) for _ in range(n)) for _ in range(runs))
    live_total = sum(live_values)
    below = sum(1 for t in totals if t <= live_total) / runs
    live_wins = sum(1 for v in live_values if v > 0)
    back_wins = sum(1 for v in pool if v > 0) / len(pool)

    if below < 0.05:
        verdict = ("OUTSIDE the backtest's band: the live result is worse than "
                   f"{(1 - below) * 100:.0f}% of what the backtest produces over {n} "
                   "trades. Treat the backtest as optimistic until the gap is explained.")
    elif below < 0.20:
        verdict = ("At the weak edge of the backtest's band. Not yet proof of a "
                   "broken simulator, but the direction to watch.")
    else:
        verdict = "Inside the backtest's band; the live record is consistent with it."
    if n < CLUSTER_CAVEAT_TRADES:
        # Live trades arrive in clusters -- several setups filling on one move
        # -- and a cluster is closer to one draw than to several. Resampling
        # them as independent makes the band too narrow, so a small live
        # record reads as more decisive than it is.
        verdict += (f" With only {n} live trades, many of them clustered on the "
                    "same moves, this band is narrower than it should be: read "
                    "it as a warning, not a verdict.")

    return {
        "available": True,
        "live_trades": n,
        "live_total_r": round(live_total, 2),
        "live_win_rate": round(live_wins / n * 100, 1),
        "backtest_win_rate": round(back_wins * 100, 1),
        "band_r": {"p5": round(_percentile(totals, 0.05), 2),
                   "p50": round(_percentile(totals, 0.50), 2),
                   "p95": round(_percentile(totals, 0.95), 2)},
        "percentile": round(below * 100, 1),
        "verdict": verdict,
        "note": (f"{n} live trades totalled {live_total:+.2f}R against a backtest "
                 f"band of {_percentile(totals, 0.05):+.1f}R to "
                 f"{_percentile(totals, 0.95):+.1f}R (5th-95th) for the same "
                 f"number of trades — the {below * 100:.0f}th percentile. {verdict}"),
    }


def assess(trades: list[dict]) -> dict:
    """Every reading in this module, for one finished run."""
    return {
        "significance": significance(trades),
        "concurrency": concurrency(trades),
        "drawdown": drawdown(trades),
        "monte_carlo": monte_carlo(trades),
        "cost_sensitivity": cost_sensitivity(trades),
    }


def render(report: dict) -> str:
    """The assessment as the text a person actually reads."""
    lines: list[str] = ["", "Statistical rigour", "=" * 60]

    sig = report.get("significance") or {}
    if "significance" not in report:
        pass
    elif sig.get("available"):
        lines.append(
            f"  mean {sig['mean_r']:+.4f}R over {sig['trades']} trades "
            f"(sd {sig['stdev_r']:.2f})")
        lines.append(f"  trade-level t   {sig['t_statistic']:+.2f}"
                     f"   {'significant' if sig['significant'] else 'NOT significant'}")
        if sig.get("monthly_t_statistic") is not None:
            lines.append(
                f"  monthly t       {sig['monthly_t_statistic']:+.2f}"
                f"   {'significant' if sig['monthly_significant'] else 'NOT significant'}"
                f"   ({sig['positive_months']}/{sig['months']} months positive)")
        if sig.get("annualised_sharpe") is not None:
            lines.append(f"  annualised Sharpe (R-denominated) {sig['annualised_sharpe']:.2f}")
        lines.append(f"  {sig['verdict']}")
    else:
        lines.append(f"  {sig.get('note', 'unavailable')}")

    for key, title in (("concurrency", "Concurrency"),
                       ("drawdown", "Drawdown"),
                       ("monte_carlo", "Monte Carlo (reshuffled order)"),
                       ("cost_sensitivity", "Cost sensitivity")):
        if key not in report:
            continue
        section = report.get(key) or {}
        lines.append("")
        lines.append(f"{title}")
        lines.append("-" * 60)
        lines.append(f"  {section.get('note', 'unavailable')}")

    return "\n".join(lines)
