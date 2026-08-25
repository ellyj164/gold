"""Checks for the analysis and measurement layers added in v1.2.0.

Covers the volume profile, the premium/discount and breaker arrays, the
expanded candlestick vocabulary, the economic-calendar generator, the cleaned
expectancy record and the backtester's causality fix.

One property is pinned harder than the rest, because the project has been
caught by it twice:

    **Every new detector must be shown to fire.**

A detector that never fires is the default outcome, not the exception. Three
have shipped silent in this codebase already -- `trendline_break` fitted its
line through the candles that printed the break, and two more were mis-scaled
the same way. Each pattern below therefore gets a purpose-built series that
contains it, and the test asserts the detector finds it. A pattern with no
such series here has not been shown to work.

Run with:  python -m tests.test_analysis_v12
"""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from goldassist.analysis import (arrays, calendar_sync, candlesticks, events,
                                 indicators, structure, volumeprofile)
from goldassist.config import TIMEFRAME_MINUTES
from goldassist.trader import expectancy

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        FAILURES.append(name)


# --- fixtures ---------------------------------------------------------------

def bar(o: float, h: float, l: float, c: float, volume: float = 100.0) -> dict:
    return {"open": o, "high": h, "low": l, "close": c,
            "volume": volume, "spread": 1.0}


def up(a: float, b: float) -> dict:
    return bar(a, b + 0.5, a - 0.5, b)


def down(a: float, b: float) -> dict:
    return bar(a, a + 0.5, b - 0.5, b)


def frame(rows: list[dict], freq: str = "15min") -> pd.DataFrame:
    return pd.DataFrame(
        rows, index=pd.date_range("2026-01-01", periods=len(rows), freq=freq, tz="UTC"))


def quiet(n: int = 40) -> list[dict]:
    """Flat candles, enough of them for ATR to be defined."""
    return [bar(100, 100.6, 99.4, 100.05) for _ in range(n)]


# --- volume profile ---------------------------------------------------------

def profiled_market() -> pd.DataFrame:
    """Heavy trade at 2000, a thin gap at 2020, heavy again at 2040."""
    rng = np.random.default_rng(7)
    rows = []
    for centre, volume, count in ((2000, 900, 160), (2020, 80, 25), (2040, 700, 120)):
        for _ in range(count):
            o = centre + rng.normal(0, 0.8)
            c = centre + rng.normal(0, 0.8)
            rows.append(bar(o, max(o, c) + abs(rng.normal(0, 0.4)),
                            min(o, c) - abs(rng.normal(0, 0.4)), c, volume))
    return frame(rows)


def test_volume_profile():
    print("volume profile")
    profile = volumeprofile.build(profiled_market())
    check("a profile is produced", profile is not None)

    check("the POC lands on the busiest price", abs(profile["poc"] - 2000) < 3,
          str(profile["poc"]))
    check("the value area holds about 70% of volume",
          68 <= profile["value_area_volume_pct"] <= 76,
          str(profile["value_area_volume_pct"]))
    check("both shelves are found as high-volume nodes",
          any(abs(n - 2000) < 4 for n in profile["high_volume_nodes"])
          and any(abs(n - 2040) < 4 for n in profile["high_volume_nodes"]),
          str(profile["high_volume_nodes"]))

    # The distinction that makes an LVN mean anything: an interior gap is one
    # price crossed quickly, a tail is one it never reached at all.
    check("the interior gap is a low-volume node",
          any(2014 < n < 2026 for n in profile["low_volume_nodes"]),
          str(profile["low_volume_nodes"]))
    check("distribution tails are not reported as low-volume nodes",
          not any(n < profile["val"] - 1 for n in profile["low_volume_nodes"]),
          str(profile["low_volume_nodes"]))

    check("volume is labelled as tick volume", profile["volume_kind"] == "tick")

    above = volumeprofile.summarise(profile, 2060.0, atr=3.0)
    inside = volumeprofile.summarise(profile, 2000.0, atr=3.0)
    check("price above the value area reads as above value",
          above["location"] == "above value", above["location"])
    check("price at the POC is flagged", inside["at_poc"])

    blocked = volumeprofile.path_is_clear(profile, 2010, 2050)
    check("a shelf between price and target is reported",
          blocked["verdict"] in ("congested", "blocked"), blocked["verdict"])

    check("no profile is produced without volume",
          volumeprofile.build(frame([bar(100, 101, 99, 100, 0) for _ in range(60)]))
          is None)
    check("no profile is produced from too little data",
          volumeprofile.build(frame(quiet(10))) is None)


# --- premium / discount and breakers ---------------------------------------

def bullish_breaker_series() -> list[dict]:
    """Low, failed rally, lower low that sweeps it, then a break back through."""
    rows = [up(112 - i * 0.5, 112 - (i + 1) * 0.5) for i in range(20)]
    rows += [down(102, 100), down(100.5, 100.0)]              # the swing low
    rows += [up(100, 104), up(104, 108), up(108, 110)]        # the rally that fails
    rows += [down(110, 106), down(106, 102), down(102, 98)]   # sweeps the low
    rows += [up(98, 103), up(103, 108), up(108, 113), up(113, 116)]   # breaks back
    rows += [up(116, 116.5), up(116.5, 117)]
    return rows


def bearish_breaker_series() -> list[dict]:
    rows = [down(100 + i * 0.5, 100 + (i + 1) * 0.5) for i in range(20)]
    rows += [up(108, 110), up(109.5, 110.0)]
    rows += [down(110, 106), down(106, 102), down(102, 100)]
    rows += [up(100, 104), up(104, 108), up(108, 112)]
    rows += [down(112, 107), down(107, 102), down(102, 97),
             down(97, 94), down(94, 93.5), down(93.5, 93)]
    return rows


def test_breakers_fire():
    print("breaker blocks")
    for label, rows, expected in (("bullish", bullish_breaker_series(), "bullish"),
                                  ("bearish", bearish_breaker_series(), "bearish")):
        df = frame(rows)
        swings = structure.analyse(df, indicators.atr(df), strength=1)["swings"]
        found = arrays.detect_breakers(df, swings)
        check(f"a {label} breaker is detected",
              any(b.direction == expected for b in found),
              f"{len(found)} found: {[b.direction for b in found]}")

    # Three swings *is* the pattern. Requiring four made this silent on every
    # series containing exactly one breaker, which is most of them.
    df = frame(bullish_breaker_series())
    swings = structure.analyse(df, indicators.atr(df), strength=1)["swings"]
    check("three swings are enough to form a breaker", len(swings) >= 3 and
          len(arrays.detect_breakers(df, swings)) >= 1, f"{len(swings)} swings")

    check("no breaker without swings", arrays.detect_breakers(frame(quiet(40)), []) == [])


def test_dealing_range():
    print("premium and discount")
    df = frame(bullish_breaker_series())
    swings = structure.analyse(df, indicators.atr(df), strength=1)["swings"]
    atr_v = float(indicators.atr(df).iloc[-1])

    band = arrays.dealing_range(swings, 116.0, atr_v)
    check("a dealing range is produced", band["available"])
    check("the top of the range reads as premium", band["zone"] == "premium",
          band["zone"])
    check("premium favours the sell side", band["favours"] == "bearish")

    low = arrays.dealing_range(swings, band["low"] + band["span"] * 0.1, atr_v)
    check("the bottom of the range reads as discount", low["zone"] == "discount",
          low["zone"])

    middle = arrays.dealing_range(swings, band["equilibrium"], atr_v)
    check("the midpoint reads as equilibrium", middle["zone"] == "equilibrium",
          middle["zone"])
    check("equilibrium favours neither side", middle["favours"] == "neutral")

    check("an optimal-entry band is published",
          band["ote_long"][0] < band["ote_long"][1] <= band["equilibrium"],
          str(band["ote_long"]))
    check("a range too tight to mean anything is refused",
          not arrays.dealing_range(
              [{"kind": "high", "price": 100.1, "index": 1},
               {"kind": "low", "price": 100.0, "index": 2}], 100.0, 5.0)["available"])


# --- candlestick vocabulary -------------------------------------------------

PATTERN_SERIES = {
    "bullish harami":       [bar(101, 101.2, 98.6, 98.8), bar(99.4, 99.8, 99.2, 99.6)],
    "bearish harami":       [bar(99, 101.4, 98.8, 101.2), bar(100.4, 100.8, 100.2, 100.3)],
    "piercing line":        [bar(101, 101.2, 98.8, 99.0), bar(98.6, 100.4, 98.5, 100.3)],
    "dark cloud cover":     [bar(99, 101.2, 98.8, 101.0), bar(101.5, 101.6, 99.6, 99.7)],
    "tweezer bottom":       [bar(100, 100.4, 98.5, 99.9), bar(99.9, 100.3, 98.52, 100.1)],
    "tweezer top":          [bar(100, 101.5, 99.6, 100.1), bar(100.1, 101.48, 99.7, 99.9)],
    "bullish marubozu":     [bar(99, 100.55, 98.98, 100.5)],
    "spinning top":         [bar(100, 101.2, 98.8, 100.1)],
    "three white soldiers": [bar(99, 100.1, 98.9, 100.0), bar(100.0, 101.1, 99.9, 101.0),
                             bar(101.0, 102.1, 100.9, 102.0)],
    "three black crows":    [bar(101, 101.1, 99.9, 100.0), bar(100.0, 100.1, 98.9, 99.0),
                             bar(99.0, 99.1, 97.9, 98.0)],
    "three inside up":      [bar(101, 101.2, 98.6, 98.8), bar(99.4, 99.8, 99.2, 99.6),
                             bar(99.7, 101.4, 99.6, 101.3)],
    "three inside down":    [bar(99, 101.4, 98.8, 101.2), bar(100.4, 100.8, 100.2, 100.3),
                             bar(100.2, 100.3, 98.7, 98.8)],
    # The patterns that already existed, pinned here too so a change to the
    # shared geometry cannot silence them unnoticed.
    "bullish engulfing":    [bar(101, 101.2, 99.8, 99.9), bar(99.7, 101.6, 99.6, 101.4)],
    "morning star":         [bar(101, 101.2, 98.6, 98.8), bar(98.8, 99.1, 98.5, 98.9),
                             bar(99.0, 100.6, 98.9, 100.4)],
}


def test_every_candle_pattern_fires():
    print("candlestick vocabulary")
    for name, tail in PATTERN_SERIES.items():
        df = frame(quiet(40) + tail)
        found = {p.name for p in candlesticks.detect(df, lookback=len(tail) + 1)}
        check(f"'{name}' fires on a series built to contain it", name in found,
              f"got {sorted(found)}")


# --- economic calendar ------------------------------------------------------

def test_calendar_generation():
    print("economic calendar")
    generated = calendar_sync.generate(2026)
    check("events are generated for 2026", len(generated) >= 30, str(len(generated)))

    titles = [e["title"] for e in generated]
    check("payrolls are included", any("payroll" in t.lower() for t in titles))
    check("CPI is included", any("CPI" in t for t in titles))
    check("FOMC is included", any("FOMC" in t for t in titles))
    check("every generated event is high impact",
          all(e["impact"] == "high" for e in generated))

    # The first-Friday rule, against dates from a published schedule. If this
    # ever fails the rule has drifted and the payroll dates are fiction.
    for month, day in ((9, 4), (10, 2), (11, 6)):
        check(f"payrolls for 2026-{month:02d} land on the published date",
              calendar_sync.first_friday(2026, month).day == day,
              str(calendar_sync.first_friday(2026, month)))

    # 08:30 Eastern is 12:30 UTC in October and 13:30 UTC in November. Getting
    # this wrong warns an hour late for half the year.
    def payroll_hour(month: int) -> int:
        stamp = next(e["time"] for e in generated
                     if "payroll" in e["title"].lower()
                     and f"-{month:02d}-" in e["time"])
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).hour

    check("a payroll release in EDT converts to 12:30 UTC", payroll_hour(10) == 12,
          str(payroll_hour(10)))
    check("a payroll release in EST converts to 13:30 UTC", payroll_hour(11) == 13,
          str(payroll_hour(11)))

    # The generated file must be loadable by the layer that reads it, and
    # writing twice must not double it.
    target = Path(tempfile.mkdtemp(prefix="goldassist-cal-")) / "calendar.json"
    first, _ = calendar_sync.write([2026], target)
    second, _ = calendar_sync.write([2026], target)
    check("regenerating is idempotent", first == second, f"{first} then {second}")

    loaded = events.load(target)
    check("the event layer can read what was generated", len(loaded) == first,
          f"{len(loaded)} of {first}")
    check("loaded events carry timezone-aware times",
          all(e["time"].tzinfo is not None for e in loaded))

    hand_added = json.loads(target.read_text(encoding="utf-8"))
    hand_added.append({"time": "2026-07-01T12:00:00Z", "title": "Hand-added",
                       "impact": "medium"})
    target.write_text(json.dumps(hand_added), encoding="utf-8")
    calendar_sync.write([2026], target)
    check("a hand-added event survives regeneration",
          any(e.get("title") == "Hand-added"
              for e in json.loads(target.read_text(encoding="utf-8"))))

    upcoming = events.upcoming(loaded, datetime(2026, 9, 3, tzinfo=timezone.utc),
                               horizon_hours=48)
    check("an imminent release is reported", upcoming["available"]
          and upcoming["next_high_impact"] is not None,
          str(upcoming.get("next_high_impact")))


# --- the cleaned record -----------------------------------------------------

def outcome(**kwargs) -> dict:
    base = {
        "strategy": "round_number", "direction": "bullish", "entry": 4400.0,
        "fill_price": 4400.0, "outcome": "win", "r_multiple": 1.0,
        "filled_at": datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc).isoformat(),
        "created_at": datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc).isoformat(),
        "session": "overlap", "entered": 1,
    }
    base.update(kwargs)
    return base


def test_record_cleaning():
    print("expectancy: cleaning the record")

    # The exact shape of the two poisoned rows in the live database.
    phantom = outcome(entered=1, fill_price=None, r_multiple=3.54, outcome="win",
                      strategy=None)
    kept, cut = expectancy.filled([outcome(), phantom])
    check("a row with no fill price is not a trade", len(kept) == 1 and cut == 1,
          f"kept {len(kept)}, cut {cut}")

    # The double-instance signature: same idea, seconds apart.
    base_time = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
    copies = [outcome(filled_at=(base_time + timedelta(seconds=s)).isoformat())
              for s in (0, 3, 7)]
    unique, removed = expectancy.deduplicate(copies)
    check("copies seconds apart collapse to one trade",
          len(unique) == 1 and removed == 2, f"{len(unique)} kept, {removed} removed")

    # A genuine re-entry at the same level, hours later, must survive. Without
    # the time window this function silently deletes real trades.
    spread_out = [outcome(filled_at=(base_time + timedelta(hours=h)).isoformat())
                  for h in (0, 3, 7)]
    unique, removed = expectancy.deduplicate(spread_out)
    check("re-entries hours apart are kept as separate trades",
          len(unique) == 3 and removed == 0, f"{len(unique)} kept, {removed} removed")


def test_shrinkage():
    print("expectancy: shrinkage")
    check("a thin sample is pulled hard toward zero",
          abs(expectancy.shrink(1.0, 2)) < 0.1, str(expectancy.shrink(1.0, 2)))
    check("a large sample keeps most of its estimate",
          expectancy.shrink(1.0, 500) > 0.9, str(expectancy.shrink(1.0, 500)))
    check("shrinkage is monotonic in sample size",
          expectancy.shrink(1.0, 5) < expectancy.shrink(1.0, 50)
          < expectancy.shrink(1.0, 500))
    check("no sample means no estimate", expectancy.shrink(1.0, 0) == 0.0)

    # Direction must be preserved: shrinking a loss toward zero must not turn
    # it into a gain.
    check("a losing record stays negative after shrinkage",
          expectancy.shrink(-1.0, 3) < 0)


def test_report_and_adjustment():
    print("expectancy: the report")
    base_time = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
    rows = [outcome(entry=4400.0 + i, fill_price=4400.0 + i,
                    filled_at=(base_time + timedelta(hours=i)).isoformat(),
                    outcome="win" if i % 2 else "loss",
                    r_multiple=1.5 if i % 2 else -1.0)
            for i in range(12)]
    known = {"round_number": "Round Number Reaction", "never_ran": "Never Ran"}
    report = expectancy.build(rows, known)

    check("all distinct trades are counted", report.overall.n == 12,
          str(report.overall.n))
    check("a detector that never produced a setup is named as silent",
          "never_ran" in report.silent, str(report.silent))
    check("a detector that has traded is not called silent",
          "round_number" not in report.silent)
    check("the headline states the sample needed to trust the average",
          "trades to show" in report.headline, report.headline)

    edge = expectancy.strategy_edge(report)
    points, why = expectancy.conviction_adjustment(edge, "round_number")
    check("a measured record moves conviction", abs(points) > 0.1, str(points))
    check("the adjustment explains itself", "recorded trade" in why, why)
    check("the adjustment is bounded", abs(points) <= expectancy.EDGE_LIMIT)

    none_points, none_why = expectancy.conviction_adjustment(edge, "never_ran")
    check("an unrecorded strategy is not penalised", none_points == 0.0)
    check("an unrecorded strategy says so", "no record" in none_why, none_why)

    # Standing rule: context sizes a setup, it never hides one. Nothing this
    # module returns may be a gate.
    check("nothing in the report can suppress a setup",
          not any(key in report.to_dict()
                  for key in ("blocked", "suppressed", "allowed", "gate", "veto")),
          str(sorted(report.to_dict())))


def test_context_multiplier_is_bounded():
    print("expectancy: measured timing")
    base_time = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
    rows = [outcome(entry=4400.0 + i, fill_price=4400.0 + i,
                    filled_at=(base_time + timedelta(hours=i)).isoformat(),
                    session="overlap", outcome="win", r_multiple=3.0)
            for i in range(20)]
    report = expectancy.build(rows, {})
    multiplier, notes = expectancy.context_multiplier(report, "overlap", None)
    check("a strong session record raises the multiplier", multiplier > 1.0,
          str(multiplier))
    check("even an extreme record cannot move sizing more than a tenth",
          multiplier <= 1.10 + 1e-9, str(multiplier))
    check("the multiplier explains itself", bool(notes), str(notes))

    flat, _ = expectancy.context_multiplier(report, "asia", None)
    check("a session with no record leaves sizing alone", flat == 1.0, str(flat))


# --- premium/discount is an intraday read -----------------------------------

def _range_analysis(position: float, trend: str, timeframe: str = "M15") -> dict:
    zone = ("premium" if position > 0.55
            else "discount" if position < 0.45 else "equilibrium")
    favours = ("bearish" if position > 0.55
               else "bullish" if position < 0.45 else "neutral")
    return {
        "price": 2090.0, "atr": 5.0,
        "timeframes": {timeframe: {
            "dealing_range": {"available": True, "zone": zone, "favours": favours,
                              "position": position, "low": 2000.0, "high": 2100.0,
                              "equilibrium": 2050.0},
            "structure": {"trend": trend},
        }},
        "levels": {}, "alignment": {"conflicts": [], "direction": "bullish"},
        "liquidity": {"sweeps": []}, "regime": {},
    }


def _premium_penalty(analysis: dict, direction: str = "bullish"):
    from goldassist.decision import confluence, profile as profile_module
    found = confluence._penalties(analysis, direction, profile_module.get(None))
    return next((p for p in found if p.factor == "premium_discount"), None)


def test_premium_discount_is_intraday():
    print("premium/discount: an intraday read, not a swing one")
    from goldassist.config import EXECUTION_TIMEFRAMES

    # The method is intraday: entries are timed on H1, M30 and M15, so the
    # range an entry is priced against is the range of the timeframe it was
    # timed on. Reading this on H4 would let a swing-scale range veto an
    # intraday trade, and would mark half of every trending day unbuyable.
    check("the read is scoped to the execution timeframes",
          set(EXECUTION_TIMEFRAMES) == {"H1", "M30", "M15"},
          str(EXECUTION_TIMEFRAMES))
    check("an H4 range alone raises no premium penalty",
          _premium_penalty(_range_analysis(0.92, "ranging", "H4")) is None)

    # The one that matters most: a trending intraday market lives at the top
    # of its own range. Penalising that would tax every continuation and
    # suppress the largest class of setup this application looks for.
    check("a continuation in an uptrend is not penalised for being high",
          _premium_penalty(_range_analysis(0.92, "bullish")) is None)

    # But a genuinely bad price still is.
    ranging = _premium_penalty(_range_analysis(0.92, "ranging"))
    check("buying the top of a *range* is penalised", ranging is not None)
    counter = _premium_penalty(_range_analysis(0.92, "bearish"))
    check("buying the top of a range in a downtrend is penalised",
          counter is not None)

    check("buying the discount is never penalised",
          _premium_penalty(_range_analysis(0.10, "ranging")) is None)
    check("equilibrium is not penalised",
          _premium_penalty(_range_analysis(0.50, "ranging")) is None)

    # It sizes; it must never gate. A penalty is points off a score, and the
    # setup is still built, still shown, still alerted.
    if ranging is not None:
        check("the penalty is bounded", ranging.points <= 9.0 + 1e-9,
              str(ranging.points))

    # Deeper into the wrong half must cost more than barely into it.
    shallow = _premium_penalty(_range_analysis(0.60, "ranging"))
    deep = _premium_penalty(_range_analysis(0.98, "ranging"))
    check("the penalty scales with how bad the price is",
          shallow is not None and deep is not None and deep.points > shallow.points,
          f"{shallow.points if shallow else None} vs {deep.points if deep else None}")


# --- causality --------------------------------------------------------------

def test_backtest_causality():
    print("backtest causality")
    from goldassist.backtest.engine import _slice_frames, _resolution_slice

    # A day's worth of M15, plus the D1 candle that contains it. MetaTrader
    # labels that D1 candle 00:00 -- the trap this test exists for.
    # Starts the day before, so at least 60 M15 candles have closed by noon --
    # the minimum `_slice_frames` needs before it will serve a timeframe.
    m15 = pd.DataFrame(
        [bar(100, 101, 99, 100.5) for _ in range(300)],
        index=pd.date_range("2025-12-31", periods=300, freq="15min", tz="UTC"))
    d1 = pd.DataFrame(
        [bar(100, 130, 70, 120) for _ in range(80)],
        index=pd.date_range("2025-10-15", periods=80, freq="D", tz="UTC"))
    frames = {"D1": d1, "M15": m15}

    noon = pd.Timestamp("2026-01-01 12:00", tz="UTC")
    sliced = _slice_frames(frames, noon)
    check("a slice is produced", sliced is not None)
    if sliced is not None:
        newest_d1 = sliced["D1"].index[-1]
        closes_at = newest_d1 + pd.Timedelta(minutes=TIMEFRAME_MINUTES["D1"])
        check("the forming daily candle is excluded", closes_at <= noon,
              f"newest D1 {newest_d1} closes at {closes_at}, now {noon}")
        check("the day being traded is not visible to the engines",
              newest_d1 < pd.Timestamp("2026-01-01", tz="UTC"), str(newest_d1))

        newest_m15 = sliced["M15"].index[-1]
        check("the forming 15-minute candle is excluded",
              newest_m15 + pd.Timedelta(minutes=15) <= noon, str(newest_m15))

    # The resolver must not fill on a bar that has not begun.
    resolution = _resolution_slice(m15, noon, "M15")
    check("the resolver sees no candle opening at or after the clock",
          resolution is not None and resolution.index[-1] < noon,
          str(resolution.index[-1]) if resolution is not None else "None")


# --- entry point ------------------------------------------------------------

def main() -> int:
    test_volume_profile()
    test_breakers_fire()
    test_dealing_range()
    test_every_candle_pattern_fires()
    test_calendar_generation()
    test_record_cleaning()
    test_shrinkage()
    test_report_and_adjustment()
    test_context_multiplier_is_bounded()
    test_premium_discount_is_intraday()
    test_backtest_causality()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) failed:")
        for name in FAILURES:
            print(f"  - {name}")
        return 1
    print("All v1.2 analysis and measurement checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
