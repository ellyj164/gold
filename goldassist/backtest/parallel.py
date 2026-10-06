"""Running a replay across every core the machine has.

Why this exists
---------------

One evaluation runs the full multi-timeframe analysis and costs roughly two
seconds. That single fact set the whole shape of backtesting here: the
evaluation ceiling was 600, which covers about six days of market at an M15
step, and six days cannot validate 32 detectors across six sessions, four score
bands and two directions -- every cell holds single digits, and a single-digit
cell is noise wearing a number.

So the strategy library was never measured, the conviction weights were never
fitted, and the application's central claim -- that it finds the highest
probability setups -- rested on nothing. That is not an analysis gap. It is an
arithmetic one: the measuring instrument was too slow to take a reading.

How it is split
---------------

A replay is stateful. The setup book carries live setups from one step to the
next, deduplicates against what it already holds, and ages setups on the replay
clock, so steps cannot simply be handed out to workers independently -- the
result would be a book that never remembers anything.

What *can* be split is time. The history is cut into contiguous chunks, each
worker replays its own chunk with its own book, and the trades are merged. The
one thing that costs is the boundary: a chunk beginning at noon has no memory
of the setup identified at eleven, so it would both miss that trade and,
worse, admit a duplicate of it.

Both are fixed the same way. Every chunk starts ``OVERLAP_STEPS`` evaluations
*before* the range it is responsible for, purely to build book state, and then
records only trades whose entry falls inside its own range. The overlap is
wasted work by design -- it is the price of the parallelism -- and it is sized
against the profile's own setup expiry, because a setup cannot influence a
chunk it could not still have been alive for.

The merge is exact rather than approximate: :class:`~goldassist.backtest.engine.Replay`
holds raw trades and raw counters with nothing averaged yet, so merging is
concatenation and addition, and the statistics are computed once at the end
over the whole set. A parallel run and a sequential run of the same history
produce the same figures, give or take the handful of setups that span a
boundary and were legitimately attributed to one side of it.
"""

from __future__ import annotations

import logging
import os
from concurrent.futures import ProcessPoolExecutor, as_completed

import pandas as pd

from ..decision.profile import StrategyProfile
from . import engine, history

log = logging.getLogger(__name__)

# Evaluations of warm-up each chunk runs before its own range begins.
#
# Sized so a setup identified before the boundary has had time to be admitted,
# arm, fill and resolve within the warm-up -- otherwise a trade that a
# sequential run would have recorded is lost at every seam. 300 steps is 75
# hours at an M15 step, comfortably past the default 300-minute setup expiry
# and the 720-minute trade expiry.
OVERLAP_STEPS = 300

# Below this many evaluations the fork is not worth its own overhead: pickling
# the frames to each worker costs more than the replay saves.
MIN_STEPS_TO_FORK = 400

# How many times its own warm-up a chunk must be worth before it is created.
#
# The overlap is pure overhead, so a chunk has to be large enough for it to
# amortise. At 4x, a worker spends a fifth of its time warming up and
# four-fifths on work that counts -- three workers then finish a run in a
# little over half the sequential time. Splitting more finely than this loses
# to its own warm-up: eight chunks of 300 steps each with 300 steps of overlap
# is 2400 wasted evaluations to save 2100, which is slower than not forking.
MIN_CHUNK_OVERLAP_RATIO = 4


def worker_count(requested: int | None = None) -> int:
    """How many processes to use.

    One core is deliberately left to the operating system, and to the live
    analysis loop if the application is running while a backtest is launched --
    a replay that makes the terminal unresponsive is a replay nobody runs twice.
    """
    if requested and requested > 0:
        return int(requested)
    cores = os.cpu_count() or 1
    return max(1, cores - 1)


def _chunk_bounds(start_index: int, end_index: int, step: int,
                  chunks: int) -> list[tuple[int, int]]:
    """Contiguous [start, end) index ranges, one per chunk, aligned to ``step``.

    Two things this has to get right.

    **Alignment.** Every chunk must land on the same grid of evaluation
    instants a sequential run would have walked, or the boundaries silently
    shift the sampled times and the two runs are not comparable.

    **Balance.** Equal ranges are the wrong split, because they are not equal
    *work*: only the first chunk starts at the beginning of the window and so
    only the first has no warm-up to run. Cutting 900 evaluations into two
    equal halves gives one worker 450 and the other 450 plus a 300-step
    warm-up, and wall time is set by the slower -- 750 against a sequential
    900, a theoretical best of 1.2x before process startup, which measured at
    **0.86x: slower than not forking at all.**

    So the first chunk is given a larger range, by exactly the warm-up every
    other chunk must pay, and each worker then does the same total number of
    evaluations. For N chunks over S steps with a warm-up of V, that target is
    ``(S + (N-1)V) / N`` each -- 1853 apiece for a 4959-step window across
    three workers, against 4959 sequential.
    """
    total = end_index - start_index
    if chunks <= 1 or total <= 0:
        return [(start_index, end_index)]

    steps = max(1, total // step)
    if steps < chunks:
        return [(start_index, end_index)]

    # Evaluations each worker should perform, warm-up included.
    per_worker = (steps + (chunks - 1) * OVERLAP_STEPS) // chunks
    # The first chunk owns that many; the rest own that many less their warm-up.
    first_own = max(1, min(steps, per_worker))
    later_own = max(1, per_worker - OVERLAP_STEPS)

    bounds: list[tuple[int, int]] = []
    cursor = start_index
    for index in range(chunks):
        own = first_own if index == 0 else later_own
        stop = min(end_index, cursor + own * step)
        if index == chunks - 1:
            stop = end_index          # the last chunk absorbs the remainder
        if stop <= cursor:
            break
        bounds.append((cursor, stop))
        cursor = stop
        if cursor >= end_index:
            break
    if bounds and bounds[-1][1] < end_index:
        bounds[-1] = (bounds[-1][0], end_index)
    return bounds


def _run_chunk(args) -> engine.Replay:
    """One worker's share. Module-level so it can be pickled to a subprocess."""
    (frames, profile_data, step_timeframe, step, chunk_start, chunk_end,
     global_start, costs) = args

    profile = StrategyProfile.from_dict(profile_data)
    driver = frames[step_timeframe]

    # Warm the book up before the range this chunk owns, then record only what
    # begins inside it.
    #
    # The first chunk warms from nothing, because there is nothing before it:
    # ``global_start`` is where the usable window begins, and stepping back
    # past it would only feed `_slice_frames` candles it must refuse for want
    # of history, inflating the skipped count for no benefit.
    #
    # The warm-up is a whole number of steps, so a chunk lands on exactly the
    # evaluation instants a sequential run would have walked. Clamping to
    # ``global_start`` with a plain max() would break that alignment whenever
    # the clamp bit, and the two runs would silently sample different times.
    warm_steps = min(OVERLAP_STEPS, (chunk_start - global_start) // step)
    warm_start = chunk_start - warm_steps * step
    record_from = None if warm_start == chunk_start else driver.index[chunk_start]

    return engine.replay(
        frames, profile, step_timeframe, step,
        start_index=warm_start, end_index=chunk_end,
        record_from=record_from, costs=costs,
    )


def run(frames: dict[str, pd.DataFrame], profile: StrategyProfile,
        step_timeframe: str = "M15", step: int = 1,
        max_evaluations: int = engine.DEFAULT_MAX_EVALUATIONS,
        workers: int | None = None, progress=None,
        costs=None, last_days: float | None = None) -> dict:
    """Replay history across several processes and merge the result.

    Falls back to a plain sequential replay whenever forking would not pay --
    a short range, or a single worker -- so callers can use this unconditionally
    rather than choosing between two code paths.

    ``last_days`` narrows the *evaluated* range to the final N days without
    touching the history behind it. That distinction is the point: trimming the
    frames instead would leave D1 with fewer than the 260 bars every engine
    needs and the replay would refuse to start. Narrowing the range is what
    makes a step-1 run affordable -- the honest stride over a short window,
    rather than a wide stride over a long one.
    """
    start_index, end_index, step, window_start, window_end = engine.plan(
        frames, step_timeframe, step, max_evaluations)

    if last_days:
        driver = frames[step_timeframe]
        cutoff = driver.index[end_index - 1] - pd.Timedelta(days=float(last_days))
        narrowed = int(driver.index.searchsorted(cutoff))
        if narrowed > start_index:
            # Keep the new start on the same evaluation grid the plan set, so
            # the sampled instants are the ones a full run would have walked.
            offset = (narrowed - start_index) // step * step
            start_index = start_index + offset
            window_start = driver.index[start_index]

    resolution_tf = next((tf for tf in ("M1", "M5", "M15", "M30")
                          if frames.get(tf) is not None and len(frames[tf]) > 100),
                         step_timeframe)
    if costs is None:
        costs = engine.cost_model_for(frames, resolution_tf)

    n = worker_count(workers)
    total_steps = (end_index - start_index) // max(1, step)

    # Never create more chunks than the warm-up can be amortised over. A short
    # run split across every core spends more time re-warming books than it
    # saves, and would be slower than not forking at all.
    affordable = total_steps // (OVERLAP_STEPS * MIN_CHUNK_OVERLAP_RATIO)
    if workers is None:
        n = max(1, min(n, affordable))

    if n <= 1 or total_steps < MIN_STEPS_TO_FORK:
        log.info("Replaying %d evaluations sequentially "
                 "(too short to amortise the per-chunk warm-up)", total_steps)
        result = engine.replay(frames, profile, step_timeframe, step,
                               start_index=start_index, end_index=end_index,
                               progress=progress, costs=costs)
        stats = engine.compile_stats(result, frames, step_timeframe,
                                     window_start, window_end)
        stats["workers"] = 1
        return {"stats": stats, "trades": [t.to_dict() for t in result.trades]}

    bounds = _chunk_bounds(start_index, end_index, step, n)
    log.info("Replaying %d evaluations across %d workers in %d chunks",
             total_steps, n, len(bounds))

    profile_data = profile.to_dict()
    tasks = [(frames, profile_data, step_timeframe, step, lo, hi,
              start_index, costs)
             for lo, hi in bounds]

    merged = engine.Replay(step=step)
    merged.resolution_timeframe = resolution_tf
    merged.costs = costs
    done = 0

    with ProcessPoolExecutor(max_workers=n) as pool:
        futures = {pool.submit(_run_chunk, task): task for task in tasks}
        for future in as_completed(futures):
            try:
                merged.merge(future.result())
            except Exception:
                # One failed chunk is a hole in the sample, not a failed run,
                # and the hole is visible: fewer evaluations than the window
                # implies. Losing the whole replay to it would be worse.
                log.warning("A replay chunk failed and was skipped", exc_info=True)
            done += 1
            if progress:
                progress(done / len(tasks))

    # Trades arrive in whatever order the chunks finished. Every equity-curve
    # figure -- drawdown, streaks -- reads them in sequence, so they have to be
    # put back into the order they actually happened in.
    merged.trades.sort(key=lambda t: t.entered_at)

    stats = engine.compile_stats(merged, frames, step_timeframe,
                                 window_start, window_end)
    stats["workers"] = n
    stats["chunks"] = len(bounds)
    return {"stats": stats, "trades": [t.to_dict() for t in merged.trades]}
