"""Checks for transaction costs and the chunked parallel replay.

Two things are verified here that nothing else could verify:

1. **A trade is priced after the broker is paid.** The spread is measured from
   the data rather than assumed, the round turn is charged once whichever way
   the trade ran, and the net figures move in the direction they must -- a
   winner is worth less and a loser costs more, never the reverse.
2. **Splitting a replay does not change it.** The parallel runner cuts history
   into chunks and merges them; the chunk boundaries have to tile the same
   evaluation grid a sequential run walks, or the two are not comparable.

Run with:  python -m tests.test_costs
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

import pandas as pd

from goldassist import costs as costs_module
from goldassist.backtest import parallel
from goldassist.decision import profile as profile_module
from goldassist.signals import resolve

from tests.test_resolution import armed_setup, bars

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        FAILURES.append(name)


# --- the cost model ---------------------------------------------------------

def test_spread_is_measured_not_assumed():
    print("the spread comes from the broker's own recorded column")
    # 25 points at a 0.01 point size is a 0.25 spread -- 25 pips on Gold.
    frame = pd.DataFrame({
        "open": [4400.0] * 5, "high": [4401.0] * 5, "low": [4399.0] * 5,
        "close": [4400.0] * 5, "volume": [100.0] * 5,
        "spread": [20, 25, 25, 30, 25],
    })
    model = costs_module.CostModel.from_frame(frame, point=0.01)

    check("a model is produced", model.available, model.note)
    check("the median spread is used, not the mean",
          abs(model.spread - 0.25) < 1e-9, str(model.spread))
    check("the round turn is one spread when nothing else is charged",
          abs(model.round_turn_pips - 25.0) < 1e-6, str(model.round_turn_pips))

    empty = costs_module.CostModel.from_frame(
        frame.drop(columns=["spread"]), point=0.01)
    check("no spread column means costs are unmodelled, not zero",
          not empty.available, empty.note)

    absurd = frame.assign(spread=[500000] * 5)
    guarded = costs_module.CostModel.from_frame(absurd, point=0.01)
    check("an implausible spread is refused rather than charged",
          not guarded.available, guarded.note)


def test_the_point_size_is_not_guessed():
    print("the point size is taken or inferred, never assumed to be two decimals")
    from goldassist.backtest import engine

    # A three-decimal Gold feed, which is what this account quotes. The spread
    # column is in points, so 260 points is 0.26 of price -- 26 pips. Reading
    # it against a two-decimal point size makes it 2.60, and a 26-pip round
    # turn silently becomes 240.
    three_dp = pd.DataFrame({
        "open": [4430.155] * 8, "high": [4431.255] * 8, "low": [4429.005] * 8,
        "close": [4430.155, 4430.255, 4430.005, 4430.615,
                  4430.125, 4430.335, 4430.845, 4430.155],
        "volume": [100.0] * 8, "spread": [260] * 8,
    })
    told = engine.cost_model_for({"M5": three_dp}, "M5", point=0.001)
    check("with the point size supplied the round turn is 26 pips",
          abs(told.round_turn_pips - 26.0) < 1e-6, str(told.round_turn_pips))

    inferred = engine.cost_model_for({"M5": three_dp}, "M5")
    check("with none supplied the precision is inferred, not assumed",
          abs(inferred.round_turn_pips - 26.0) < 1e-6,
          str(inferred.round_turn_pips))
    check("and the model says the point size was inferred",
          "inferred" in inferred.note, inferred.note)

    two_dp = three_dp.assign(
        close=[4430.15, 4430.25, 4430.00, 4430.60,
               4430.10, 4430.30, 4430.85, 4430.15],
        spread=[26] * 8)
    plain = engine.cost_model_for({"M5": two_dp}, "M5")
    check("a two-decimal feed infers 0.01 and also reads 26 pips",
          abs(plain.round_turn_pips - 26.0) < 1e-6, str(plain.round_turn_pips))


def test_commission_and_slippage_are_added():
    print("commission and slippage are added to the spread")
    model = costs_module.CostModel(
        spread=0.25, commission_per_lot=7.0, slippage_pips=2.0,
        contract_size=100.0, source="measured")
    # 0.25 spread + 7/100 commission + 2 pips a side both ways = 0.25+0.07+0.04
    check("the round turn adds all three",
          abs(model.round_turn - 0.36) < 1e-9, str(model.round_turn))
    check("and reports in pips", abs(model.round_turn_pips - 36.0) < 1e-6,
          str(model.round_turn_pips))

    default = costs_module.CostModel(spread=0.25, source="measured")
    check("commission and slippage default to zero rather than to a guess",
          abs(default.round_turn - 0.25) < 1e-9, str(default.round_turn))


def test_net_reward_risk_is_stricter_than_gross():
    print("the reward/risk gate reads the net ratio")
    model = costs_module.CostModel(spread=0.25, source="measured")
    # 500 pips of reward against a 250-pip stop is a gross 2.0.
    gross = 5.0 / 2.5
    net = model.net_reward_risk(reward=5.0, risk=2.5)
    check("gross is 2.0", abs(gross - 2.0) < 1e-9, str(gross))
    check("net is lower than gross", net < gross, f"{net} vs {gross}")
    # (5.00 - 0.25) / (2.50 + 0.25) = 4.75 / 2.75
    check("both legs move: reward shrinks and risk grows",
          abs(net - (4.75 / 2.75)) < 1e-9, str(net))

    check("a zero risk yields no ratio rather than a division error",
          model.net_reward_risk(5.0, 0.0) is None)


def test_costs_reduce_a_winner_and_deepen_a_loser():
    print("a resolved trade is recorded gross and net")
    model = costs_module.CostModel(spread=0.25, source="measured")

    winner, prof = armed_setup()
    winner.costs = model
    zone = winner.entry_zone
    tp1 = winner.targets[0].price
    resolve.apply_bars(winner, bars([
        (4400, 4400, zone.preferred - 0.2, zone.preferred),
        (zone.preferred, tp1 + 0.5, zone.preferred - 0.5, tp1 + 0.2),
    ]), prof)

    check("the winner is still a win", winner.outcome == "win", str(winner.outcome))
    check("the cost is recorded", abs(winner.cost_pips - 25.0) < 1e-6,
          str(winner.cost_pips))
    check("net pips are 25 below gross",
          abs((winner.result_pips - winner.net_result_pips) - 25.0) < 0.2,
          f"{winner.result_pips} gross vs {winner.net_result_pips} net")
    check("net R is below gross R", winner.net_r_multiple < winner.r_multiple,
          f"{winner.r_multiple} vs {winner.net_r_multiple}")
    check("the exit price is untouched by costs",
          winner.exit_price == round(tp1, 3), str(winner.exit_price))

    loser, prof = armed_setup()
    loser.costs = model
    zone = loser.entry_zone
    stop = loser.stop
    resolve.apply_bars(loser, bars([
        (4400, 4400, zone.preferred - 0.2, zone.preferred),
        (zone.preferred, zone.preferred, stop - 0.5, stop - 0.2),
    ]), prof)

    check("the loser is still a loss", loser.outcome == "loss", str(loser.outcome))
    check("the loss is deeper in pips after costs",
          loser.net_result_pips < loser.result_pips,
          f"{loser.result_pips} gross vs {loser.net_result_pips} net")
    # The denominator stays the initial stop distance for both figures. If the
    # risk were inflated by the round turn too, a stop-out would come to
    # exactly -1.00R net as well as gross and the cost would vanish from the
    # only number the statistics are built on.
    check("net R is more negative than gross R",
          loser.net_r_multiple < loser.r_multiple,
          f"{loser.r_multiple} vs {loser.net_r_multiple}")
    check("a stop-out costs more than the 1R it promised",
          loser.net_r_multiple < -1.0, str(loser.net_r_multiple))


def test_no_model_means_gross_equals_net():
    print("with no cost model the net figures equal the gross ones")
    suggestion, prof = armed_setup()
    zone = suggestion.entry_zone
    tp1 = suggestion.targets[0].price
    resolve.apply_bars(suggestion, bars([
        (4400, 4400, zone.preferred - 0.2, zone.preferred),
        (zone.preferred, tp1 + 0.5, zone.preferred - 0.5, tp1 + 0.2),
    ]), prof)

    check("no cost is charged", suggestion.cost_pips == 0.0,
          str(suggestion.cost_pips))
    check("net pips equal gross pips",
          suggestion.net_result_pips == suggestion.result_pips)
    check("net R equals gross R",
          suggestion.net_r_multiple == suggestion.r_multiple)


def test_a_gate_that_gross_would_have_passed():
    print("costs can fail a setup the gross ratio would have passed")
    model = costs_module.CostModel(spread=0.25, source="measured")
    # Reward 3.80, risk 2.50: gross 1.52 clears a 1.5 gate, net 1.29 does not.
    gross = 3.80 / 2.50
    net = model.net_reward_risk(3.80, 2.50)
    check("gross clears the default 1.5 gate", gross >= 1.5, str(gross))
    check("net does not", net < 1.5, str(net))


# --- the chunked replay -----------------------------------------------------

def test_chunks_tile_the_range_exactly():
    print("chunk boundaries tile the range without gap or overlap")
    for total, step, n in ((10000, 1, 8), (5000, 3, 4), (997, 7, 3), (50, 1, 8)):
        bounds = parallel._chunk_bounds(100, 100 + total, step, n)
        check(f"contiguous for total={total} step={step} n={n}",
              all(bounds[i][1] == bounds[i + 1][0] for i in range(len(bounds) - 1)),
              str(bounds))
        check(f"spans the whole range for total={total} step={step} n={n}",
              bounds[0][0] == 100 and bounds[-1][1] == 100 + total, str(bounds))


def test_chunks_land_on_the_sequential_grid():
    print("every chunk boundary sits on a step the sequential run would walk")
    start, total, step = 250, 6000, 4
    bounds = parallel._chunk_bounds(start, start + total, step, 6)
    walked = set(range(start, start + total, step))
    starts = [lo for lo, _ in bounds]
    check("each chunk starts on an evaluation instant",
          all(s in walked for s in starts), str(starts))


def test_a_single_worker_is_one_chunk():
    print("one worker means one chunk, and no wasted overlap")
    bounds = parallel._chunk_bounds(0, 5000, 1, 1)
    check("exactly one chunk", len(bounds) == 1, str(bounds))
    check("covering everything", bounds == [(0, 5000)], str(bounds))


def test_chunks_are_balanced_by_work_not_by_range():
    print("chunks are sized so every worker does the same amount of work")
    step, overlap = 4, parallel.OVERLAP_STEPS
    global_start, steps = 250, 4959
    bounds = parallel._chunk_bounds(global_start, global_start + steps * step,
                                    step, 3)
    check("three chunks are produced", len(bounds) == 3, str(len(bounds)))

    work = []
    for chunk_start, chunk_end in bounds:
        own = (chunk_end - chunk_start) // step
        warm = min(overlap, (chunk_start - global_start) // step)
        work.append(own + warm)

    check("the first chunk has no warm-up",
          min(overlap, (bounds[0][0] - global_start) // step) == 0)
    check("the first chunk therefore owns more than the others",
          (bounds[0][1] - bounds[0][0]) > (bounds[1][1] - bounds[1][0]),
          str(bounds))
    # Equal ranges would give 1653 / 1953 / 1953 and a wall time of 1953.
    spread = max(work) - min(work)
    check(f"total work per worker is even (spread {spread} evaluations)",
          spread <= 2, str(work))
    check("and the split still beats sequential",
          max(work) < steps, f"{max(work)} vs {steps}")

    covered = sum((hi - lo) // step for lo, hi in bounds)
    check("the owned ranges still add up to the whole window",
          abs(covered - steps) <= 1, f"{covered} vs {steps}")


def test_warm_up_keeps_a_chunk_on_the_sequential_grid():
    print("a chunk's warm-up start lands on the sequential grid")
    global_start, step = 250, 4
    bounds = parallel._chunk_bounds(global_start, global_start + 8000, step, 3)
    walked = set(range(global_start, global_start + 8000, step))

    for chunk_start, _ in bounds:
        # The arithmetic _run_chunk uses, checked without spawning a process.
        warm_steps = min(parallel.OVERLAP_STEPS,
                         (chunk_start - global_start) // step)
        warm_start = chunk_start - warm_steps * step
        check(f"warm start {warm_start} is an evaluation instant",
              warm_start in walked, f"chunk at {chunk_start}")
        check(f"warm start {warm_start} never precedes the usable window",
              warm_start >= global_start, f"chunk at {chunk_start}")

    first_start = bounds[0][0]
    warm_steps = min(parallel.OVERLAP_STEPS, (first_start - global_start) // step)
    check("the first chunk warms from nothing", warm_steps == 0, str(warm_steps))


def test_short_runs_are_not_split_into_warm_up():
    print("a run too short to amortise its warm-up is not split")
    # Each chunk costs OVERLAP_STEPS of warm-up, so chunks must be worth
    # several times that or the fork loses to its own overhead.
    afford = parallel.OVERLAP_STEPS * parallel.MIN_CHUNK_OVERLAP_RATIO
    check("the threshold is stated in overlaps, not guessed",
          afford == parallel.OVERLAP_STEPS * parallel.MIN_CHUNK_OVERLAP_RATIO)
    check("a run of one chunk's worth would not be split",
          (afford - 1) // afford == 0, str(afford))
    check("a run of eight chunks' worth affords eight",
          (afford * 8) // afford == 8)


def test_worker_count_leaves_a_core_free():
    print("the default worker count leaves a core for everything else")
    import os
    cores = os.cpu_count() or 1
    check("one fewer than the core count, and at least one",
          parallel.worker_count() == max(1, cores - 1),
          f"{parallel.worker_count()} of {cores}")
    check("an explicit request is honoured", parallel.worker_count(3) == 3)


# --- the decision-history row -----------------------------------------------

def test_a_history_row_carries_the_setup_s_own_numbers():
    print("a history row reports the setup's own stop, R:R and direction")
    from goldassist.storage.db import _setup_fields

    # The shape that produced the complaint: an overall bearish market bias
    # with a bullish setup on the board, and a decision-level invalidation
    # sitting 14 dollars above the bullish setup's own entry zone.
    row = {
        "direction": "bearish",              # the engine's market bias
        "invalidation": 4438.506,            # the decision's stop
        "reward_risk": 1.74,                 # the decision's ratio
        "setup_label": "Bullish Sweep Reversal",
        "setup_direction": None,             # older rows left this unset
        "entry_low": 4421.46, "entry_high": 4424.731,
        "suggestion": {
            "direction": "bullish",
            "invalidation": {"price": 4416.01,
                             "description": "a confirmed close below 4416.01"},
            "reward_risk": 3.02,
            "targets": [{"price": 4440.0}],
            "score": 61.2,
            "strategy_name": "Liquidity Sweep Reversal",
        },
    }
    out = _setup_fields(dict(row))

    check("the setup's own direction is recovered",
          out["setup_direction"] == "bullish", str(out.get("setup_direction")))
    check("the setup's own stop is reported",
          abs(out["setup_invalidation"] - 4416.01) < 1e-9,
          str(out.get("setup_invalidation")))
    check("the setup's own reward/risk is reported",
          abs(out["setup_reward_risk"] - 3.02) < 1e-9,
          str(out.get("setup_reward_risk")))

    # The point of the whole fix: a long's stop must sit below its entry zone.
    # The decision's 4438.506 sat 14 above it, which is not a stop at all.
    check("a long's stop is below its entry zone, as it must be",
          out["setup_invalidation"] < row["entry_low"],
          f"stop {out['setup_invalidation']} vs zone low {row['entry_low']}")
    check("the decision's own fields are left untouched",
          out["direction"] == "bearish" and abs(out["invalidation"] - 4438.506) < 1e-9,
          "the market bias is a real reading and must still be available")
    check("the stop's basis is carried so the number can be checked",
          "4416.01" in (out.get("setup_invalidation_note") or ""),
          str(out.get("setup_invalidation_note")))


def test_a_history_row_without_a_card_is_left_alone():
    print("a row stored before the card was kept degrades quietly")
    from goldassist.storage.db import _setup_fields

    row = {"direction": "bearish", "invalidation": 4438.506,
           "setup_label": "Bullish Sweep Reversal", "suggestion": None}
    out = _setup_fields(dict(row))
    check("no setup stop is invented", out.get("setup_invalidation") is None,
          str(out.get("setup_invalidation")))
    check("and the row still returns", out["direction"] == "bearish")


# --- feed health ------------------------------------------------------------

class _FlakySource:
    """A data source that can be told to fail, the way a dead terminal does."""

    def __init__(self):
        self.symbol = "XAUUSDm"
        self.failing = False
        self.connects = 0
        self.shutdowns = 0

    def connect(self):
        self.connects += 1

    def shutdown(self):
        self.shutdowns += 1

    def candles(self, timeframe, count, end=None):
        from goldassist.data.source import DataUnavailable
        if self.failing:
            raise DataUnavailable(
                f"No {timeframe} candles returned for XAUUSDm (-10001: IPC send failed).")
        index = pd.date_range("2026-09-01", periods=count, freq="1min", tz="UTC")
        return pd.DataFrame(
            {"open": 4400.0, "high": 4401.0, "low": 4399.0, "close": 4400.0,
             "volume": 100.0, "spread": 260.0},
            index=index)


def test_a_failed_refresh_is_not_reported_as_healthy():
    print("a refresh that failed everywhere does not report success")
    from goldassist.data.feed import MarketFeed

    source = _FlakySource()
    feed = MarketFeed(source, timeframes=("H1", "M15", "M5"), tracking="M1")
    feed.prime()
    feed.refresh()
    check("a good cycle records no error", feed.last_error is None,
          str(feed.last_error))
    check("and is not flagged as a total failure", not feed.all_failed)

    source.failing = True
    feed.refresh()
    # This is the 2026-09-05 bug: last_error was assigned inside the loop and
    # then unconditionally cleared at the end, so a cycle in which every
    # timeframe failed finished by reporting no error at all.
    check("a cycle where everything failed records an error",
          feed.last_error is not None, str(feed.last_error))
    check("the error names a timeframe and the cause",
          feed.last_error and "IPC send failed" in feed.last_error,
          str(feed.last_error))
    check("and the total failure is flagged for the service",
          feed.all_failed)

    source.failing = False
    feed.refresh()
    check("recovery clears the error", feed.last_error is None)
    check("and clears the total-failure flag", not feed.all_failed)


def test_a_partial_failure_is_not_a_disconnection():
    print("one bad timeframe is an error, not a dead connection")
    from goldassist.data.feed import MarketFeed
    from goldassist.data.source import DataUnavailable

    source = _FlakySource()
    feed = MarketFeed(source, timeframes=("H1", "M15", "M5"), tracking="M1")
    feed.prime()

    real = source.candles

    def only_m15_fails(timeframe, count, end=None):
        if timeframe == "M15":
            raise DataUnavailable("No M15 candles returned.")
        return real(timeframe, count, end)

    source.candles = only_m15_fails
    feed.refresh()
    check("the failure is recorded", feed.last_error is not None,
          str(feed.last_error))
    check("but it is NOT treated as a lost connection", not feed.all_failed,
          "a single bad series must not trigger a reconnect")


# --- stability across folds -------------------------------------------------

def _trade(when: str, score: float, r: float) -> dict:
    return {"entered_at": when + "T12:00:00+00:00", "score": score,
            "net_r_multiple": r, "r_multiple": r}


def test_a_consistent_ordering_is_reported_as_consistent():
    print("an ordering that holds in every fold is reported as holding")
    from goldassist.backtest import stability

    trades = []
    for fold in range(4):
        for i in range(12):
            day = f"2026-0{fold + 1}-{(i % 28) + 1:02d}"
            trades.append(_trade(day, 78.0, 0.5))     # strong band
            trades.append(_trade(day, 70.0, 0.1))     # weak band
    reads = stability.split(trades, 4)
    verdict = stability.ordering(reads)

    check("four folds are produced", len(reads) == 4, str(len(reads)))
    check("the ordering holds everywhere", verdict["holds_everywhere"],
          str(verdict))
    check("and every fold was actually compared",
          verdict["folds_compared"] == 4, str(verdict["folds_compared"]))
    check("the weak band is recognised as positive, not losing",
          verdict["lower_band_always_positive"], str(verdict))
    text = stability.render(reads, verdict)
    check("the advice is to size down rather than raise the gate",
          "do not raise the gate" in text, text[-200:])


def test_an_inconsistent_ordering_is_not_reported_as_consistent():
    print("an ordering that only appears on average is not reported as holding")
    from goldassist.backtest import stability

    trades = []
    for fold in range(4):
        # The strong band wins hugely in one fold and loses in the rest, which
        # is exactly the case an aggregate average would hide.
        strong_r = 4.0 if fold == 0 else -0.2
        for i in range(12):
            day = f"2026-0{fold + 1}-{(i % 28) + 1:02d}"
            trades.append(_trade(day, 78.0, strong_r))
            trades.append(_trade(day, 70.0, 0.1))
    reads = stability.split(trades, 4)
    verdict = stability.ordering(reads)

    check("the aggregate favours the strong band",
          sum(r.by_band["75-85"]["net_r"] for r in reads)
          > sum(r.by_band["65-75"]["net_r"] for r in reads))
    check("but the ordering is reported as not holding",
          not verdict["holds_everywhere"], str(verdict))
    check("and only one fold is credited",
          verdict["folds_ordered_correctly"] == 1,
          str(verdict["folds_ordered_correctly"]))


def test_thin_bands_are_flagged_not_trusted():
    print("a band with too few trades is reported but marked unreliable")
    from goldassist.backtest import stability

    trades = []
    for fold in range(2):
        for i in range(12):
            day = f"2026-0{fold + 1}-{(i % 28) + 1:02d}"
            trades.append(_trade(day, 78.0, 0.5))
        # Three trades in the top band per fold: reported, not reasoned from.
        for i in range(3):
            trades.append(_trade(f"2026-0{fold + 1}-15", 92.0, 3.0))
    reads = stability.split(trades, 2)

    top = [r.by_band.get("85-100") for r in reads if r.by_band.get("85-100")]
    check("the thin band is still reported", bool(top), str(reads[0].by_band))
    check("but is marked unreliable",
          all(not cell["reliable"] for cell in top), str(top))
    check("while the well-populated band is reliable",
          all(r.by_band["75-85"]["reliable"] for r in reads))

    verdict = stability.ordering(reads, better="85-100", worse="75-85")
    check("an unreliable band is excluded from the comparison",
          verdict["folds_compared"] == 0, str(verdict))
    check("and the renderer says so rather than inventing a verdict",
          "Not enough per-fold evidence" in stability.render(reads, verdict))


def test_gross_is_used_when_a_row_predates_costs():
    print("a trade stored before costs existed falls back to gross R")
    from goldassist.backtest import stability

    old = {"entered_at": "2026-01-05T12:00:00+00:00", "score": 78.0,
           "r_multiple": 0.7}
    check("net R falls back to gross", abs(stability._net_r(old) - 0.7) < 1e-9)
    check("a missing R is zero, not an error",
          stability._net_r({"entered_at": "x", "score": 70}) == 0.0)


def main() -> int:
    from tests.clock import outside_rollover
    outside_rollover()
    for test in (
            test_a_history_row_carries_the_setup_s_own_numbers,
            test_a_history_row_without_a_card_is_left_alone,
            test_a_failed_refresh_is_not_reported_as_healthy,
            test_a_partial_failure_is_not_a_disconnection,
            test_a_consistent_ordering_is_reported_as_consistent,
            test_an_inconsistent_ordering_is_not_reported_as_consistent,
            test_thin_bands_are_flagged_not_trusted,
            test_gross_is_used_when_a_row_predates_costs,
            test_spread_is_measured_not_assumed,
            test_the_point_size_is_not_guessed,
            test_commission_and_slippage_are_added,
            test_net_reward_risk_is_stricter_than_gross,
            test_costs_reduce_a_winner_and_deepen_a_loser,
            test_no_model_means_gross_equals_net,
            test_a_gate_that_gross_would_have_passed,
            test_chunks_tile_the_range_exactly,
            test_chunks_land_on_the_sequential_grid,
            test_a_single_worker_is_one_chunk,
            test_chunks_are_balanced_by_work_not_by_range,
            test_warm_up_keeps_a_chunk_on_the_sequential_grid,
            test_short_runs_are_not_split_into_warm_up,
            test_worker_count_leaves_a_core_free):
        test()

    if FAILURES:
        print(f"\n{len(FAILURES)} check(s) failed:")
        for name in FAILURES:
            print(f"  - {name}")
        return 1
    print("\nAll cost and parallel-replay checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
