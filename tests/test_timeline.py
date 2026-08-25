"""Checks for the trader's clock, the signal log, and the sub-minute tape.

These three are one feature: a signal is only useful if you can say *when* it
fired, in the time zone you actually trade in, and go back and find it. Each
assertion below pins one behaviour that would otherwise be invisible until a
signal was already mislabelled in the record.

Run with:  python -m tests.test_timeline
"""

from __future__ import annotations

import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from goldassist import timeutil
from goldassist.analysis import micro
from goldassist.config import BACKTEST_TIMEFRAMES, MICRO_TIMEFRAMES, TIMEFRAMES
from goldassist.storage import Database

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        FAILURES.append(name)


# --- the clock --------------------------------------------------------------


def test_clock() -> None:
    print("\nThe trader's clock")

    # 12:37 UTC is 14:37 on a GMT+2 clock. The whole feature is this line
    # being true everywhere the application shows a time.
    moment = "2026-08-20T12:37:05+00:00"
    check("UTC is shown on the display clock", timeutil.clock(moment) == "14:37",
          timeutil.clock(moment))
    check("seconds are available for the tape",
          timeutil.clock_seconds(moment) == "14:37:05", timeutil.clock_seconds(moment))
    check("the offset is named", timeutil.offset_label() == "GMT+2",
          timeutil.offset_label())
    check("a full stamp names the zone",
          timeutil.full(moment) == "2026-08-20 14:37 GMT+2", timeutil.full(moment))

    # A timestamp with no zone marker is UTC, because everything this
    # application stores is. Reading it as local time would shift it twice.
    check("a naive timestamp is read as UTC",
          timeutil.clock("2026-08-20T12:37:05") == "14:37",
          timeutil.clock("2026-08-20T12:37:05"))
    check("a Z suffix is understood",
          timeutil.clock("2026-08-20T12:37:05Z") == "14:37")
    check("epoch seconds are understood",
          timeutil.clock(datetime(2026, 8, 20, 12, 37, 5,
                                  tzinfo=timezone.utc).timestamp()) == "14:37")
    check("a missing timestamp does not crash", timeutil.clock(None) == "—")
    check("nonsense does not crash", timeutil.clock("not a date") == "—")

    # The trading *date* is the local one. A signal at 23:30 GMT+2 is a
    # Thursday signal to the trader even though UTC has not agreed yet.
    late = "2026-08-20T22:30:00+00:00"          # 00:30 on the 21st, GMT+2
    check("the date follows the trader's clock, not UTC",
          timeutil.date(late) == "2026-08-21", timeutil.date(late))

    described = timeutil.describe(moment)
    check("describe carries the session", described.get("session") == "overlap",
          described.get("session"))
    check("describe carries a readable session label",
          described.get("session_label") == "London/NY overlap",
          described.get("session_label"))
    check("describe keeps the UTC original", described.get("utc") == moment)

    options = timeutil.session_options()
    check("every session is offered as a filter", len(options) == 6, len(options))
    check("session windows are shown on the display clock",
          any(o["name"] == "london" and o["window"].startswith("09:") for o in options),
          [o["window"] for o in options])


# --- the signal log ---------------------------------------------------------


def _suggestion(uid: str, when: str, strategy: str = "range_breakout",
                direction: str = "bullish") -> dict:
    read = timeutil.describe(when)
    return {
        "id": uid,
        "created_at": when,
        "fired_at": read["stamp"],
        "fired_date": read["date"],
        "fired_clock": read["clock"],
        "clock_offset": read["offset"],
        "fired_session": read["session"],
        "fired_session_label": read["session_label"],
        "fired_session_quality": read["session_quality"],
        "strategy": strategy,
        "strategy_name": strategy.replace("_", " ").title(),
        "family": "range",
        "direction": direction,
        "action": "BUY" if direction == "bullish" else "SELL",
        "setup_label": "Test Setup",
        "status": "DEVELOPING",
        "score": 71.0,
        "conviction": 68.0,
        "context_score": 60.0,
        "quality": "Good",
        "regime_at_entry": "trending",
        "price": 4381.5,
        "entry_zone": {"low": 4378.0, "high": 4382.0, "preferred": 4380.0},
        "initial_stop": 4370.0,
        "invalidation": {"price": 4370.0},
        "targets": [{"price": 4392.0}, {"price": 4405.0}],
        "reward_risk": 1.9,
        "opportunity": {"first_target_pips": 120.0, "max_pips": 250.0,
                        "classification": "Strong"},
        "trigger": {"description": "M15 close above 4382"},
        "reasons": ["a", "b"],
        "confirmations": [],
    }


def test_signal_log() -> None:
    print("\nThe signal log")
    # ignore_cleanup_errors because SQLite's WAL sidecars can outlive the
    # close on Windows; the database itself is closed explicitly below.
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        db = Database(Path(tmp) / "log.db")

        first = _suggestion("aaa", "2026-08-20T12:37:05+00:00")
        row_id = db.record_fired_signal(first)
        check("a fired signal is logged", bool(row_id))

        # The book re-offers the same tracked setup every cycle. A log that
        # grew a row each time would be a log of polling, not of trading.
        check("logging the same signal again is a no-op",
              db.record_fired_signal(first) == row_id)

        db.record_fired_signal(_suggestion("bbb", "2026-08-20T05:10:00+00:00",
                                           "sweep_reversal", "bearish"))

        rows = db.fired_signals()
        check("both signals are in the log", len(rows) == 2, len(rows))
        check("newest first", rows[0]["uid"] == "aaa", [r["uid"] for r in rows])

        row = rows[0]
        check("the log stores the display time", row["fired_clock"] == "14:37",
              row["fired_clock"])
        check("the log stores the display date", row["fired_date"] == "2026-08-20",
              row["fired_date"])
        check("the log names the clock", row["clock_offset"] == "GMT+2")
        check("the log stores the session", row["session"] == "overlap",
              row["session"])
        check("the log flattens the entry zone",
              row["entry_low"] == 4378.0 and row["entry_high"] == 4382.0)
        check("the log flattens the target ladder",
              row["tp1"] == 4392.0 and row["tp2"] == 4405.0)
        check("the log records the pips on offer",
              row["first_target_pips"] == 120.0)
        check("a fresh signal has not closed", row["closed_at"] is None)

        # The early-morning signal fired in the Asian session, not the overlap.
        asian = [r for r in rows if r["uid"] == "bbb"][0]
        check("each signal keeps its own session", asian["session"] == "asia",
              asian["session"])
        check("each signal keeps its own clock", asian["fired_clock"] == "07:10",
              asian["fired_clock"])

        # -- progress lands on the same row --------------------------------
        closed = dict(first)
        closed.update(status="TARGET_HIT", filled_at="2026-08-20T13:02:00+00:00",
                      fill_price=4380.5, exit_at="2026-08-20T14:10:00+00:00",
                      outcome="win", exit_price=4392.0, exit_reason="target",
                      result_pips=115.0, r_multiple=2.1)
        db.update_fired_signal(closed)

        after = [r for r in db.fired_signals() if r["uid"] == "aaa"][0]
        check("the result lands on the original row", after["outcome"] == "win")
        check("the row still says when it fired", after["fired_clock"] == "14:37",
              after["fired_clock"])
        check("the close is stored on the display clock",
              after["closed_local"] == "2026-08-20 16:10", after["closed_local"])
        check("still one row per signal", len(db.fired_signals()) == 2)

        # -- filters --------------------------------------------------------
        check("filter by direction",
              [r["uid"] for r in db.fired_signals(direction="bearish")] == ["bbb"])
        check("filter by session",
              [r["uid"] for r in db.fired_signals(session="asia")] == ["bbb"])
        check("filter by strategy",
              [r["uid"] for r in db.fired_signals(strategy="range_breakout")]
              == ["aaa"])
        check("filter by result",
              [r["uid"] for r in db.fired_signals(outcome="win")] == ["aaa"])
        check("'still running' means nothing has closed it",
              [r["uid"] for r in db.fired_signals(outcome="open")] == ["bbb"])
        check("filter by trading day",
              len(db.fired_signals(date="2026-08-20")) == 2)

        # -- summary --------------------------------------------------------
        summary = db.fired_summary()
        check("the summary counts what fired", summary["fired"] == 2, summary)
        check("the summary counts each side",
              summary["buys"] == 1 and summary["sells"] == 1)
        check("the summary counts what is still open", summary["open"] == 1)
        check("the summary totals the pips", summary["pips"] == 115.0)
        check("the summary breaks down by session",
              len(summary["by_session"]) == 2, summary["by_session"])
        check("the summary breaks down by strategy",
              len(summary["by_strategy"]) == 2, summary["by_strategy"])

        dates = db.fired_dates()
        check("trading days are listed for the picker",
              dates and dates[0]["date"] == "2026-08-20" and dates[0]["n"] == 2,
              dates)

        db.close()


# --- the tape ---------------------------------------------------------------


def _ticks(seconds: int = 600, drift: float = 0.0, spread: float = 0.30,
           rate: int = 4, widen_last: float | None = None) -> pd.DataFrame:
    """A synthetic tick stream.

    ``drift`` is the total move across the whole window. Note that `thrust` is
    measured over roughly the last thirty seconds, so a drift spread thinly
    across ten minutes is *supposed* to be swamped by noise there -- a test
    that wants a pushing tape has to push recently.

    ``widen_last`` sets the spread for the final tenth of the window only.
    A permanently wide spread is not a widened one: the reading is relative to
    the window's own median, deliberately, so it works on any broker.
    """
    n = seconds * rate
    rng = np.random.default_rng(5)
    end = datetime.now(timezone.utc)
    index = pd.date_range(end=end, periods=n, freq=f"{1000 // rate}ms", tz="UTC")
    mid = 4340.0 + rng.normal(0, 0.02, n).cumsum() + np.linspace(0, drift, n)
    spreads = np.full(n, spread)
    if widen_last is not None:
        spreads[-max(n // 10, 2):] = widen_last
    return pd.DataFrame({"bid": mid - spreads / 2, "ask": mid + spreads / 2,
                         "mid": mid, "spread": spreads}, index=index)


def test_tape() -> None:
    print("\nThe tape")

    check("M1 is analysed", "M1" in TIMEFRAMES, TIMEFRAMES)
    check("the tape ladder is declared",
          MICRO_TIMEFRAMES == ("S5", "S15", "S30"), MICRO_TIMEFRAMES)
    # A replay needs every timeframe to span the same period, and M1 does not.
    check("backtesting leaves M1 out", "M1" not in BACKTEST_TIMEFRAMES,
          BACKTEST_TIMEFRAMES)

    reading = micro.read(_ticks(drift=0.0), atr=4.0)
    check("the tape reads as available", reading["available"], reading.get("reason"))
    check("every sub-minute rung has candles",
          all(reading["timeframes"][tf]["candles"] > 0 for tf in MICRO_TIMEFRAMES),
          {tf: reading["timeframes"][tf]["candles"] for tf in MICRO_TIMEFRAMES})
    check("the finest rung has the most candles",
          reading["timeframes"]["S5"]["candles"]
          > reading["timeframes"]["S30"]["candles"])
    check("the spread is measured", reading["spread_now"] > 0)
    check("a normal spread is not flagged", not reading["spread_wide"],
          reading["spread_ratio"])

    # A blind tape and a flat one look identical in numbers and mean opposite
    # things, so an empty stream must say so rather than reporting zeroes.
    blind = micro.read(pd.DataFrame(), atr=4.0)
    check("no ticks reads as unavailable", not blind["available"])
    check("no ticks explains itself", bool(blind["reason"]), blind)
    check("an unavailable tape has no opinion",
          micro.agrees(blind, "bullish") is None)

    # Direction. A tape pushing up should favour a bullish setup and disfavour
    # a bearish one, symmetrically around 0.5.
    rising = micro.read(_ticks(seconds=120, drift=9.0), atr=4.0)
    up = micro.agrees(rising, "bullish")
    down = micro.agrees(rising, "bearish")
    check("a rising tape favours a buy", up is not None and up > 0.5, up)
    check("the two sides are symmetrical",
          up is not None and abs((up + down) - 1.0) < 1e-9, (up, down))
    check("a rising tape reports positive velocity",
          rising["velocity_pips_min"] > 0, rising["velocity_pips_min"])
    check("the tape describes itself in words", bool(rising["note"]), rising["note"])

    # A widened spread is the one condition that overrides everything else in
    # the description: entry costs more than the measured levels suggest.
    wide = micro.read(_ticks(seconds=120, drift=9.0, widen_last=1.60), atr=4.0)
    check("a widened spread is flagged", wide["spread_wide"], wide["spread_ratio"])
    check("a widened spread leads the description",
          wide["state"] == "wide spread", wide["state"])

    # ...and a spread that has been wide for the whole window is normal *for
    # this window*. Measuring it any other way would need a hardcoded idea of
    # what Gold's spread should be, which is broker-specific and wrong more
    # often than it is right.
    always_wide = micro.read(_ticks(seconds=120, spread=1.60), atr=4.0)
    check("a permanently wide spread is not a widening",
          not always_wide["spread_wide"], always_wide["spread_ratio"])

    check("too few ticks reads as unavailable",
          not micro.read(_ticks(seconds=1, rate=1), atr=4.0)["available"])


# --- the analysis cache -----------------------------------------------------


def _frame(closes, freq="1h"):
    closes = np.asarray(closes, dtype=float)
    opens = np.r_[closes[0], closes[:-1]]
    index = pd.date_range(end=datetime(2026, 8, 20, tzinfo=timezone.utc),
                          periods=len(closes), freq=freq, tz="UTC")
    return pd.DataFrame(
        {"open": opens, "high": np.maximum(opens, closes) + 0.5,
         "low": np.minimum(opens, closes) - 0.5, "close": closes,
         "volume": 1000.0, "spread": 22.0}, index=index)


def test_analysis_cache() -> None:
    """The cache must never serve an analysis that no longer matches the bars.

    It is keyed on the newest closed candle, which is safe only because the
    engines are handed closed candles exclusively. These checks pin the two
    ways that could go wrong: a new candle arriving, and an existing candle
    being revised in place after a reconnect.
    """
    print("\nThe analysis cache")
    from goldassist.analysis import mtf

    rng = np.random.default_rng(9)
    closes = list(4300 + rng.normal(0, 1.0, 400).cumsum() * 0.3)

    mtf.reset_cache()
    first = mtf.analyse_timeframe_cached(_frame(closes), "H1")
    again = mtf.analyse_timeframe_cached(_frame(closes), "H1")
    check("identical candles reuse the cached analysis", again is first)

    # A new candle must produce a new analysis, not the cached one.
    extended = mtf.analyse_timeframe_cached(_frame(closes + [closes[-1] + 9.0]), "H1")
    check("a new candle invalidates the cache", extended is not first)
    check("the new analysis reflects the new candle",
          extended["price"] != first["price"],
          (extended["price"], first["price"]))

    # A broker can revise the newest bar in place after a disconnect. Keying
    # on time alone would keep serving the pre-revision analysis.
    revised = list(closes)
    revised[-1] = revised[-1] + 12.0
    changed = mtf.analyse_timeframe_cached(_frame(revised), "H1")
    check("a revised close invalidates the cache", changed["price"] != first["price"],
          (changed["price"], first["price"]))

    # Each timeframe caches independently.
    mtf.reset_cache()
    h1 = mtf.analyse_timeframe_cached(_frame(closes), "H1")
    m1 = mtf.analyse_timeframe_cached(_frame(closes, "1min"), "M1")
    check("timeframes do not share a cache entry", h1 is not m1)
    check("each timeframe still reuses its own",
          mtf.analyse_timeframe_cached(_frame(closes), "H1") is h1)

    mtf.reset_cache()
    check("reset clears the cache",
          mtf.analyse_timeframe_cached(_frame(closes), "H1") is not h1)


def main() -> int:
    test_clock()
    test_signal_log()
    test_tape()
    test_analysis_cache()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) failed: {', '.join(FAILURES)}")
        return 1
    print("All timeline, signal-log and tape checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
