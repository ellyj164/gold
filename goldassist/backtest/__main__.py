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

from ..config import AppConfig
from ..data import DataUnavailable, MT5Source
from ..decision import profile as profile_module
from ..storage import Database
from . import engine, history

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
    parser.add_argument("--no-save", action="store_true",
                        help="Print the result without writing it to the database.")
    parser.add_argument("--force", action="store_true",
                        help="Run even if a Gold Assistant instance is up. "
                             "Expect IPC failures in both processes.")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(name)s: %(message)s")

    if args.list_profiles:
        for name in sorted(profile_module.load_profiles()):
            print(f"  {name}")
        return 0

    if _other_instance_running() and not args.force:
        print("The Gold Assistant is running, and MetaTrader serves one client "
              "at a time.\nClose it and retry, or pass --force to run anyway "
              "(both will see IPC errors).")
        return 2

    config = AppConfig.load()
    profile = profile_module.get(args.profile)
    print(f"Profile: {profile.name}")

    source = MT5Source(config.mt5)
    try:
        print("Loading deep history from MetaTrader (this takes a minute)...")
        frames = history.load(source)
    except DataUnavailable as exc:
        print(f"\nCould not load history: {exc}")
        return 1

    described = history.describe(frames)
    start, end = history.usable_window(frames)
    print(f"Window: {start} -> {end}")
    for tf, info in sorted(described.items()):
        print(f"  {tf:4} {info['bars']:>7} bars  {info['days']:>6.1f} days  "
              f"{info['from'][:10]} -> {info['to'][:10]}")

    print(f"\nReplaying on {args.step_timeframe}, step={args.step}, "
          f"cap={args.max_evaluations}...")

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

    stats = engine.run(frames, profile,
                       step_timeframe=args.step_timeframe,
                       max_hold_bars=args.max_hold_bars,
                       step=args.step, progress=progress,
                       max_evaluations=args.max_evaluations)
    elapsed = time.perf_counter() - started
    print(f"\rCompleted in {elapsed:.0f}s{' ' * 30}\n")

    _report(stats)

    if not args.no_save:
        db = Database()
        run_id = db.save_backtest(profile.name, stats, stats.get("trades", []),
                                  start=str(start), end=str(end),
                                  timeframe=args.step_timeframe)
        print(f"\nSaved as backtest #{run_id}.")

    source.shutdown()
    return 0


def _report(stats: dict) -> None:
    """Print the figures, with the honesty checks beside them."""

    def show(label, key, fmt="{}"):
        value = stats.get(key)
        if value is not None:
            print(f"  {label:26} {fmt.format(value)}")

    print("RESULTS")
    for label, key, fmt in (
            ("Trades", "trades", "{}"),
            ("Wins", "wins", "{}"),
            ("Losses", "losses", "{}"),
            ("Win rate", "win_rate", "{:.1f}%"),
            ("Average R", "avg_r", "{:+.3f}"),
            ("Total R", "total_r", "{:+.2f}"),
            ("Total pips", "total_pips", "{:+.1f}"),
            ("Profit factor", "profit_factor", "{:.2f}"),
            ("Max drawdown R", "max_drawdown_r", "{:.2f}")):
        show(label, key, fmt)

    by_strategy = stats.get("by_strategy") or {}
    if by_strategy:
        print("\nBY STRATEGY")
        rows = sorted(by_strategy.items(),
                      key=lambda kv: -(kv[1].get("n") or 0))
        for name, row in rows[:15]:
            print(f"  {str(name)[:26]:26} n={row.get('n', 0):>4}  "
                  f"R={row.get('total_r', 0):+7.2f}  "
                  f"win={row.get('win_rate', 0):5.1f}%")

    print("\nHONESTY CHECKS")
    print(f"  {'Evaluations':26} {stats.get('evaluations', '?')}")
    print(f"  {'Step':26} {stats.get('step', '?')}  "
          f"(a wide stride under-fills: arming needs a confirmation seen at an "
          f"evaluation instant)")
    print(f"  {'Unfilled setups':26} {stats.get('unfilled_setups', '?')}")
    print(f"  {'Skipped steps':26} {stats.get('skipped_steps', '?')}")
    dist = stats.get("score_distribution") or {}
    if dist:
        print(f"  {'Score distribution':26} {dist}")
    waits = stats.get("wait_reasons") or {}
    if waits:
        print("\n  Why the engine waited:")
        for reason, count in list(waits.items())[:6]:
            print(f"    {count:>6}  {reason}")
    print(f"\n  {'Transaction costs':26} NOT MODELLED -- every figure above is gross")


if __name__ == "__main__":
    raise SystemExit(main())
