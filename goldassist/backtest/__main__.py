"""Run a deep backtest from the command line.

``backtest/history.py`` -- the loader that pulls months of candles rather than
the live window -- was written, exported, and then never called by anything.
Every backtest in the database before v1.2.0 therefore covered the ~10-day
live window and was labelled as though it covered the history. This is the
entry point that actually uses it.

    python -m goldassist.backtest --profile "Trend Continuation"
    python -m goldassist.backtest --step 4 --max-evaluations 3000
    python -m goldassist.backtest --list-profiles

**MetaTrader serves one client at a time.** Close the Gold Assistant window
before running this: with both up, the terminal returns ``-10001: IPC send
failed`` to whichever asks second, and the symptom is a wall of warnings and
a stale chart rather than a clean error. This script checks and refuses
rather than letting you discover it from the log an hour later.

Results are written to the ``backtests`` table with the window and the step
recorded, so a run can be told apart from the ones that preceded it.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timezone

from ..config import AppConfig
from ..data import DataUnavailable, MT5Source
from ..decision import profile as profile_module
from ..storage import Database
from . import engine, fit, history, parallel, rigour, stability, walkforward

log = logging.getLogger(__name__)


def _other_instance_running() -> bool:
    """Whether a Gold Assistant process already holds the MT5 connection."""
    try:
        import subprocess
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq GoldAssistant.exe", "/NH"],
            capture_output=True, text=True, timeout=15).stdout
        return "GoldAssistant.exe" in out
    except Exception:
        # Never let the guard itself stop a run; the IPC error is recoverable
        # and a false negative here only costs a clearer message.
        return False


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m goldassist.backtest",
        description="Replay months of history through the live pipeline.")
    parser.add_argument("--profile", default=None,
                        help="Strategy profile to replay (default: the active one).")
    parser.add_argument("--list-profiles", action="store_true",
                        help="List available profiles and exit.")
    parser.add_argument("--step", type=int, default=1,
                        help="Evaluate every Nth candle. 1 is the honest "
                             "setting; a wider stride under-fills badly, "
                             "because arming needs a confirmation seen at an "
                             "evaluation instant (default: 1).")
    parser.add_argument("--step-timeframe", default="M15",
                        help="The clock the replay walks (default: M15).")
    parser.add_argument("--max-evaluations", type=int,
                        default=engine.DEFAULT_MAX_EVALUATIONS,
                        help="Cap on evaluations. Raising the cap is what "
                             "keeps the stride narrow over a long window.")
    parser.add_argument("--max-hold-bars", type=int, default=96,
                        help="Bars a setup may stay open (default: 96).")
    parser.add_argument("--workers", type=int, default=None,
                        help="Processes to replay across. Defaults to one per "
                             "core bar one. Pass 1 to force a sequential run.")
    parser.add_argument("--no-save", action="store_true",
                        help="Print the result without writing it to the database.")
    parser.add_argument("--force", action="store_true",
                        help="Run even if a Gold Assistant instance is up. "
                             "Expect IPC failures in both processes.")
    parser.add_argument("--cache", action="store_true",
                        help="Replay from the cached history instead of "
                             "MetaTrader, leaving the terminal free. Falls "
                             "back to downloading when no cache exists.")
    parser.add_argument("--refresh-cache", action="store_true",
                        help="Download a fresh history and save it to the "
                             "cache, then replay from it.")
    parser.add_argument("--stability", type=int, nargs="?", const=0,
                        metavar="ID",
                        help="Analyse a stored backtest for stability across "
                             "time instead of running a new one. Pass a "
                             "backtest id, or omit it for the most recent.")
    parser.add_argument("--folds", type=int, default=4,
                        help="Folds to split a stability analysis into "
                             "(default: 4).")
    parser.add_argument("--rigour", type=int, nargs="?", const=0, metavar="ID",
                        help="Report the statistical rigour of a stored "
                             "backtest instead of running a new one: "
                             "significance at trade and month scale, "
                             "concurrency, mark-to-market drawdown and cost "
                             "sensitivity. Pass a backtest id, or omit it for "
                             "the most recent.")
    parser.add_argument("--walk-forward", type=int, nargs="?", const=0,
                        metavar="ID",
                        help="Validate a stored backtest out-of-sample: does "
                             "the score band ordering predict, and does "
                             "pruning the strategy library help? Pass a "
                             "backtest id, or omit it for the most recent.")
    parser.add_argument("--fit", type=int, nargs="?", const=0, metavar="ID",
                        help="Fit the evidence weights to a stored backtest's "
                             "outcomes, walk-forward, and report whether the fit "
                             "beats the judged score out of sample. Nothing is "
                             "applied. Needs a run made after features were "
                             "recorded.")
    parser.add_argument("--live-check", type=int, nargs="?", const=0, metavar="ID",
                        help="Compare the live trade record with what a stored "
                             "backtest produces over the same number of trades. "
                             "The acceptance test: a backtest the live record "
                             "falls outside of is not describing the live system.")
    parser.add_argument("--reserve-holdout", type=float, default=None,
                        metavar="DAYS",
                        help="Set aside the final DAYS of history as an "
                             "out-of-sample holdout. Every later run stops "
                             "before it. Can be set once.")
    parser.add_argument("--holdout", action="store_true",
                        help="Replay the reserved holdout. Allowed once: a "
                             "holdout looked at twice is just more in-sample "
                             "data.")
    parser.add_argument("--until", default=None, metavar="DATE",
                        help="Replay only history before DATE (e.g. 2026-07-06). "
                             "How a change suggested by recent data is tested on "
                             "earlier data that did not suggest it.")
    parser.add_argument("--last-days", type=float, default=None,
                        help="Evaluate only the final N days, keeping all the "
                             "history behind them. This is how a step-1 run "
                             "becomes affordable: an honest stride over a "
                             "short window beats a wide stride over a long one.")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(name)s: %(message)s")

    if args.list_profiles:
        for name in sorted(profile_module.load_profiles()):
            print(f"  {name}")
        return 0

    if args.stability is not None:
        return _stability(args.stability, args.folds)

    if args.rigour is not None:
        return _rigour(args.rigour)

    if args.walk_forward is not None:
        return _walk_forward(args.walk_forward, args.folds)

    if args.fit is not None:
        return _fit(args.fit, args.folds)

    if args.live_check is not None:
        return _live_check(args.live_check)

    config = AppConfig.load()
    profile = profile_module.get(args.profile)
    print(f"Profile: {profile.name}")

    # A cached replay touches no terminal, so the one-client rule does not
    # apply to it and neither does the guard.
    frames = None
    source = None
    point = None
    if args.cache and not args.refresh_cache:
        frames = history.load_cache()
        if frames is None:
            print("No history cache found; downloading instead.")
        else:
            print(f"Using cached history: {history.describe_cache()}")

    if frames is None:
        if _other_instance_running() and not args.force:
            print("The Gold Assistant is running, and MetaTrader serves one "
                  "client at a time.\nClose it and retry, pass --cache to "
                  "replay from a saved history, or --force to run anyway "
                  "(both will see IPC errors).")
            return 2
        source = MT5Source(config.mt5)
        try:
            print("Loading deep history from MetaTrader (this takes a minute)...")
            frames = history.load(source)
        except DataUnavailable as exc:
            print(f"\nCould not load history: {exc}")
            return 1
        # The terminal is open right here, so ask it for the point size rather
        # than inferring one, and record the history for the next run.
        try:
            point = float(source.symbol_details()["point"])
        except Exception:
            point = None
        target = history.save_cache(frames)
        print(f"Cached history to {target} — future runs can use --cache.")

    db = Database()
    frames, last_days, label = _apply_holdout(db, frames, args)
    if frames is None:
        return 2

    described = history.describe(frames)
    start, end = history.usable_window(frames)
    print(f"Window: {start} -> {end}")
    for tf, info in sorted(described.items()):
        print(f"  {tf:4} {info['bars']:>7} bars  {info['days']:>6.1f} days  "
              f"{info['from'][:10]} -> {info['to'][:10]}")

    workers = parallel.worker_count(args.workers)
    # ``point`` is set above when a terminal was open. On a cached replay there
    # is none, and the precision is inferred from the quotes rather than
    # assumed -- the spread column is in points, and a wrong point size scales
    # every cost figure by a factor of ten.
    costs = engine.cost_model_for(frames, "M5", config.costs, point=point)
    print(f"\nCosts: {costs.headline()}")
    scope = (f", last {last_days:g} days" if last_days else "")
    print(f"Replaying on {args.step_timeframe}, step={args.step}, "
          f"cap={args.max_evaluations}, workers={workers}{scope}...")

    started = time.perf_counter()

    # The engine reports a single 0..1 fraction, not (done, total).
    state = {"last": -1}

    def progress(fraction: float) -> None:
        pct = int(fraction * 100)
        if pct != state["last"]:
            state["last"] = pct
            elapsed = time.perf_counter() - started
            eta = (elapsed / fraction - elapsed) if fraction > 0.01 else 0
            sys.stdout.write(f"\r  {pct:3d}%  elapsed {elapsed:5.0f}s  "
                             f"remaining ~{eta:5.0f}s")
            sys.stdout.flush()

    result = parallel.run(frames, profile,
                          step_timeframe=args.step_timeframe,
                          step=args.step, progress=progress,
                          max_evaluations=args.max_evaluations,
                          workers=args.workers, costs=costs,
                          last_days=last_days)
    elapsed = time.perf_counter() - started
    print(f"\rCompleted in {elapsed:.0f}s{' ' * 30}\n")

    # ``run`` returns {"stats": ..., "trades": [...]}; everything below reads
    # the statistics, and passing the wrapper instead is why this report used
    # to print a bare list where the trade count belonged and nothing at all
    # where the win rate did.
    stats = result["stats"]
    trades = result["trades"]

    _report(stats)
    print(rigour.render({"monte_carlo": rigour.monte_carlo(trades)}))
    _variants_note(db, start, end)

    if args.holdout:
        db.set_setting("holdout_used", datetime.now(timezone.utc).isoformat())

    if not args.no_save:
        run_id = db.save_backtest(profile.name + label, stats, trades,
                                  start=str(start), end=str(end),
                                  timeframe=args.step_timeframe)
        print(f"\nSaved as backtest #{run_id}.")

    if source is not None:
        source.shutdown()
    return 0


def _stored_trades(backtest_id: int) -> tuple[list | None, dict]:
    """The trade list of a stored run, or the most recent one. Reports its own
    failure and returns ``None`` so each caller stays a two-line function."""
    import json

    db = Database()
    if not backtest_id:
        stored = db.backtests()
        if not stored:
            print("No stored backtests to analyse. Run one first.")
            return None, {}
        backtest_id = stored[0]["id"]

    record = db.backtest(backtest_id)
    if record is None:
        print(f"No backtest #{backtest_id}.")
        return None, {}

    trades = record.get("trades")
    if isinstance(trades, str):
        trades = json.loads(trades)
    if not trades:
        print(f"Backtest #{backtest_id} recorded no trades to analyse.")
        return None, {}

    print(f"Backtest #{record.get('id', backtest_id)} — "
          f"{record.get('profile', '?')}\n")
    return trades, record


def _stability(backtest_id: int, folds: int) -> int:
    """Read a stored backtest back and ask whether its result held over time."""
    trades, _ = _stored_trades(backtest_id)
    if trades is None:
        return 1
    reads = stability.split(trades, folds)
    print(stability.render(reads, stability.ordering(reads)))
    return 0


def _rigour(backtest_id: int) -> int:
    """Whether a stored result is distinguishable from luck, and what it cost."""
    trades, _ = _stored_trades(backtest_id)
    if trades is None:
        return 1
    print(rigour.render(rigour.assess(trades)))
    return 0


def _walk_forward(backtest_id: int, folds: int) -> int:
    """Whether a stored result would have been usable in advance."""
    trades, _ = _stored_trades(backtest_id)
    if trades is None:
        return 1
    print(walkforward.render(walkforward.assess(trades, folds)))
    return 0


def _apply_holdout(db, frames: dict, args) -> tuple[dict | None, float | None, str]:
    """Keep the reserved holdout out of every run except the one allowed to see it.

    Returns the frames to replay, the ``last_days`` to evaluate, and a label
    appended to the stored profile name so a holdout run can never be mistaken
    for an in-sample one. Frames of None means the run must not proceed.

    The holdout is the only defence this project has against its own
    iterations. Every replay that is looked at and followed by a change makes
    the history it ran on a little more in-sample; a slice that no decision was
    ever made from is the one honest test left, and it is honest exactly once.
    """
    import pandas as pd

    reserved = db.get_setting("holdout_start")
    _, end = history.usable_window(frames)

    if args.reserve_holdout is not None:
        if reserved:
            print(f"A holdout is already reserved from {reserved}. It cannot be "
                  "moved: moving it after seeing results is how it stops being one.")
            return None, None, ""
        start = pd.Timestamp(end) - pd.Timedelta(days=args.reserve_holdout)
        db.set_setting("holdout_start", start.isoformat())
        print(f"Reserved the holdout: {start} -> {end}. Every run from now on "
              "stops before it; `--holdout` replays it once.")
        reserved = start.isoformat()

    if args.holdout:
        if not reserved:
            print("No holdout is reserved. Reserve one with --reserve-holdout DAYS "
                  "before making decisions from the history.")
            return None, None, ""
        used = db.get_setting("holdout_used")
        if used:
            print(f"The holdout was already replayed on {used}. A second look "
                  "turns it into in-sample data; there is no honest way to reuse it.")
            return None, None, ""
        cutoff = pd.Timestamp(reserved)
        days = (pd.Timestamp(end) - cutoff).total_seconds() / 86400
        print(f"HOLDOUT RUN: evaluating {cutoff} -> {end} ({days:.1f} days). "
              "This is the only time this slice will be replayed.")
        return frames, days, " [holdout]"

    if reserved:
        cutoff = pd.Timestamp(reserved)
        frames = {tf: df[df.index < cutoff] for tf, df in frames.items()}
        print(f"Stopping before the reserved holdout at {cutoff}.")
    label = ""
    if getattr(args, "until", None):
        cutoff = pd.Timestamp(args.until)
        if cutoff.tzinfo is None:
            cutoff = cutoff.tz_localize("UTC")
        frames = {tf: df[df.index < cutoff] for tf, df in frames.items()}
        label = f" [until {cutoff.date()}]"
        print(f"Replaying only history before {cutoff}.")
    return frames, args.last_days, label


def _variants_note(db, start, end) -> None:
    """Say how many runs have already been made over this window.

    The best of many variants is optimistic by construction: try enough
    profiles on one history and one of them will look good by chance. The
    count is the context every result needs and none of them used to carry.
    """
    try:
        stored = db.backtests(limit=500)
    except Exception:
        return
    same = [b for b in stored
            if str(b.get("start_date", ""))[:10] == str(start)[:10]]
    n = len(same) + 1
    print(f"\n  {'Variants on this window':26} {n} (including this run)")
    if n > 3:
        print("  The best of several runs over one history is optimistic by "
              "construction. Confirm anything chosen here on the holdout.")


def _fit(backtest_id: int, folds: int) -> int:
    """Whether fitted evidence weights beat the judged score out of sample."""
    trades, _ = _stored_trades(backtest_id)
    if trades is None:
        return 1
    print(fit.render(fit.assess(trades, folds)))
    return 0


def _live_check(backtest_id: int) -> int:
    """The live record against the band a stored backtest produces."""
    from ..trader import expectancy

    trades, _ = _stored_trades(backtest_id)
    if trades is None:
        return 1
    rows = Database().setup_outcomes(limit=5000)
    clean, _, _ = expectancy.clean(rows)
    live = [float(r.get("r_multiple") or 0.0) for r in clean]
    report = rigour.forward_check(trades, live)
    print("Live record against the backtest")
    print("=" * 60)
    print(f"  {report['note']}")
    return 0


def _report(stats: dict) -> None:
    """Print the figures, with the honesty checks beside them."""

    def show(label, key, fmt="{}"):
        value = stats.get(key)
        if value is not None:
            print(f"  {label:26} {fmt.format(value)}")

    print("RESULTS  (gross is before costs, net is what the account is paid)")
    for label, key, fmt in (
            ("Trades", "total_setups", "{}"),
            ("Wins", "wins", "{}"),
            ("Losses", "losses", "{}"),
            ("Timeouts", "timeouts", "{}"),
            ("Win rate (gross)", "win_rate", "{:.1f}%"),
            ("Win rate (net)", "net_win_rate", "{:.1f}%"),
            ("Average R (gross)", "average_r", "{:+.3f}"),
            ("Average R (net)", "net_average_r", "{:+.3f}"),
            ("Total R (gross)", "total_r", "{:+.2f}"),
            ("Total R (net)", "net_total_r", "{:+.2f}"),
            ("Total pips (gross)", "total_pips", "{:+.1f}"),
            ("Total pips (net)", "net_total_pips", "{:+.1f}"),
            ("Paid in costs", "total_cost_pips", "{:.1f} pips"),
            ("Profit factor (gross)", "profit_factor", "{:.2f}"),
            ("Profit factor (net)", "net_profit_factor", "{:.2f}"),
            ("Max drawdown R (net)", "net_max_drawdown_r", "{:.2f}"),
            ("Max consecutive losses", "max_consecutive_losses", "{}")):
        show(label, key, fmt)

    by_strategy = stats.get("by_strategy") or {}
    if by_strategy:
        print("\nBY STRATEGY  (ranked on net R)")
        rows = sorted(by_strategy.items(),
                      key=lambda kv: -(kv[1].get("net_total_r") or 0))
        for name, row in rows[:20]:
            print(f"  {str(name)[:26]:26} n={row.get('setups', 0):>4}  "
                  f"netR={row.get('net_total_r', 0):+7.2f}  "
                  f"grossR={row.get('total_r', 0):+7.2f}  "
                  f"win={row.get('win_rate', 0):5.1f}%")

    for title, key in (("BY EXPOSURE AT FILL", "by_exposure"),
                       ("BY CONFIRMATIONS AT FILL", "by_confirmations_at_fill")):
        groups = stats.get(key) or {}
        if groups:
            print(f"\n{title}")
            for name, row in sorted(groups.items()):
                n = row.get("setups", 0) or 0
                net = row.get("net_total_r", 0) or 0
                print(f"  {str(name):26} n={n:>4}  netR={net:+7.2f}  "
                      f"per trade={net / n if n else 0:+.3f}  "
                      f"win={row.get('win_rate', 0):5.1f}%")

    print("\nHONESTY CHECKS")
    print(f"  {'Evaluations':26} {stats.get('evaluations', '?')}")
    print(f"  {'Workers':26} {stats.get('workers', 1)}"
          + (f" over {stats['chunks']} chunks" if stats.get("chunks") else ""))
    print(f"  {'Step':26} {stats.get('step', '?')}  "
          f"(a wide stride under-fills: arming needs a confirmation seen at an "
          f"evaluation instant)")
    print(f"  {'Unfilled setups':26} {stats.get('unfilled_setups', '?')}")
    print(f"  {'  of which stacked, skipped':26} {stats.get('skipped_stacked', '?')}")
    print(f"  {'Skipped steps':26} {stats.get('skipped_steps', '?')}")
    dist = stats.get("score_distribution") or {}
    if dist:
        print(f"  {'Score distribution':26} {dist}")
    waits = stats.get("wait_reasons") or {}
    if waits:
        print("\n  Why the engine waited:")
        for reason, count in list(waits.items())[:6]:
            print(f"    {count:>6}  {reason}")
    costs = stats.get("costs")
    if costs and costs.get("available"):
        print(f"\n  {'Transaction costs':26} {costs['headline']}")
    else:
        print(f"\n  {'Transaction costs':26} NOT MODELLED -- every figure above "
              f"is gross"
              + (f" ({costs['note']})" if costs and costs.get("note") else ""))


if __name__ == "__main__":
    raise SystemExit(main())
