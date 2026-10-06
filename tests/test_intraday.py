"""Checks for the intraday layer: VWAP, session structure, persistence, fills.

Five things are verified here that nothing else could verify:

1. **A bar that closed before a setup armed cannot fill it.** This is the
   retroactive-fill guard, and it is the most consequential correctness fix in
   this layer: without it a replay fills at prices that were only available
   before the evidence justifying the entry existed, and every figure
   downstream is flattered in proportion to how coarsely the replay steps.
2. **Anchored VWAP is anchored.** The average is taken from the anchor forward,
   not over a rolling window, and the bands are the volume-weighted deviation
   about that average rather than a Bollinger band in disguise.
3. **The session read is derived from the clock, not from price shape.** Prior
   day, Asia range, initial balance and range extension are computed from
   timestamps, so they must be right at a session boundary.
4. **The statistical estimators agree with constructed series.** A trending
   series must read as persistent and an alternating one as mean-reverting,
   or the regime read is decoration.
5. **The regime never silences a detector.** Conviction is scaled; a setup is
   never withheld because the statistics disagree with it.

Run with:  python -m tests.test_intraday
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from goldassist.analysis import auction, persistence, vwap
from goldassist.decision import profile as profile_module
from goldassist.signals import resolve
from goldassist.strategies import base as strategy_base

from tests.test_resolution import armed_setup, bars

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        FAILURES.append(name)


def frame(rows, start: datetime, minutes: int = 5) -> pd.DataFrame:
    """A candle frame from (open, high, low, close) tuples, volume 100 each."""
    index = pd.DatetimeIndex(
        [start + timedelta(minutes=minutes * i) for i in range(len(rows))],
        tz="UTC", name="time")
    return pd.DataFrame(
        {"open": [r[0] for r in rows], "high": [r[1] for r in rows],
         "low": [r[2] for r in rows], "close": [r[3] for r in rows],
         "volume": [100.0] * len(rows), "spread": [25] * len(rows)},
        index=index)


def flat_frame(price: float, count: int, start: datetime,
               minutes: int = 5) -> pd.DataFrame:
    return frame([(price, price + 0.5, price - 0.5, price)] * count,
                 start, minutes)


# --- the retroactive-fill guard ---------------------------------------------

def test_a_bar_before_arming_cannot_fill():
    print("a candle that closed before the setup armed does not fill it")
    suggestion, prof = armed_setup()
    zone = suggestion.entry_zone
    # Armed *after* the bars that follow, so every one of them predates it.
    suggestion.armed_at = (datetime.now(timezone.utc)
                           + timedelta(hours=2)).isoformat()

    resolve.apply_bars(suggestion, bars([
        (4400, 4400, zone.preferred - 1.0, zone.preferred),
    ]), prof)
    check("the setup did not fill on a bar that predates the confirmation",
          not suggestion.entered, str(suggestion.fill_price))


def test_a_bar_after_arming_fills_normally():
    print("a candle that closed after the setup armed fills it")
    suggestion, prof = armed_setup()
    zone = suggestion.entry_zone
    start = datetime.now(timezone.utc)
    suggestion.armed_at = start.isoformat()

    resolve.apply_bars(suggestion, bars([
        (4400, 4400, zone.preferred - 1.0, zone.preferred),
    ], start=start + timedelta(minutes=5)), prof)
    check("the setup filled", suggestion.entered, suggestion.status)
    check("at the preferred price inside the zone",
          suggestion.fill_price == round(zone.preferred, 3),
          str(suggestion.fill_price))


def test_an_unarmed_setup_can_still_be_invalidated_early():
    print("a bar before arming may still invalidate, it simply may not fill")
    suggestion, prof = armed_setup()
    suggestion.armed_at = (datetime.now(timezone.utc)
                           + timedelta(hours=2)).isoformat()
    stop = float(suggestion.stop)

    resolve.apply_bars(suggestion, bars([
        (4400, 4400, stop - 2.0, stop - 1.0),
    ]), prof)
    check("the idea was invalidated", suggestion.status in resolve.TERMINAL,
          suggestion.status)
    check("and recorded as untaken, not as a loss",
          suggestion.outcome == resolve.UNTAKEN, suggestion.outcome)


def test_a_missing_arming_time_degrades_to_the_old_behaviour():
    print("a setup restored without an arming time still fills")
    suggestion, prof = armed_setup()
    suggestion.armed_at = None
    zone = suggestion.entry_zone

    resolve.apply_bars(suggestion, bars([
        (4400, 4400, zone.preferred - 1.0, zone.preferred),
    ]), prof)
    check("an unknown arming instant does not silence the setup forever",
          suggestion.entered, str(suggestion.fill_price))


def test_the_live_quote_always_fills():
    print("the live quote is a price on offer now, so it fills regardless")
    suggestion, prof = armed_setup()
    suggestion.armed_at = datetime.now(timezone.utc).isoformat()
    resolve.apply_price(suggestion, float(suggestion.entry_zone.preferred), prof)
    check("a live confirmation fills on the same cycle", suggestion.entered)


# --- anchored VWAP ----------------------------------------------------------

def test_vwap_is_anchored_not_rolling():
    print("the average runs from the anchor forward")
    start = datetime(2026, 9, 3, 0, 0, tzinfo=timezone.utc)
    # A day of 4000 followed by a day of 4100.
    rows = [(4000, 4000, 4000, 4000)] * 288 + [(4100, 4100, 4100, 4100)] * 288
    df = frame(rows, start)
    anchor = datetime(2026, 9, 4, 0, 0, tzinfo=timezone.utc)

    whole = vwap.anchored(df)
    today = vwap.anchored(df, pd.Timestamp(anchor))
    check("the whole-frame average sits between the two days",
          4000 < whole["vwap"] < 4100, str(whole["vwap"]))
    check("the anchored average sees only the second day",
          abs(today["vwap"] - 4100) < 1e-6, str(today["vwap"]))
    check("a flat anchored series has no deviation",
          today["deviation"] < 1e-6, str(today["deviation"]))


def test_vwap_bands_widen_with_dispersion():
    print("the bands are the deviation about the anchor, not about a moving average")
    start = datetime(2026, 9, 4, 0, 0, tzinfo=timezone.utc)
    tight = vwap.anchored(flat_frame(4000.0, 60, start))
    wide = vwap.anchored(frame(
        [(4000 + (30 if i % 2 else -30),) * 4 for i in range(60)], start))
    check("a dispersed series has wider bands",
          wide["deviation"] > tight["deviation"] + 10,
          f"{wide['deviation']} vs {tight['deviation']}")


def test_vwap_needs_enough_bars():
    print("a VWAP from three bars is not reported as a reference")
    start = datetime(2026, 9, 4, 0, 0, tzinfo=timezone.utc)
    check("too few bars returns nothing",
          vwap.anchored(flat_frame(4000.0, 3, start)) is None)


def test_vwap_survives_a_feed_with_no_volume():
    print("a zero-volume feed degrades to an unweighted mean, not to nothing")
    start = datetime(2026, 9, 4, 0, 0, tzinfo=timezone.utc)
    df = flat_frame(4000.0, 60, start)
    df["volume"] = 0.0
    result = vwap.anchored(df)
    check("a reading is still produced", result is not None)
    check("and it says it is not volume weighted",
          result is not None and not result["volume_weighted"])


def test_the_session_anchor_follows_the_clock():
    print("the current session is the most recent one to have opened")
    _, name = vwap._session_start(pd.Timestamp("2026-09-04 13:00", tz="UTC"))
    check("13:00 UTC is New York", name == "newyork", name)
    _, name = vwap._session_start(pd.Timestamp("2026-09-04 08:00", tz="UTC"))
    check("08:00 UTC is London", name == "london", name)
    _, name = vwap._session_start(pd.Timestamp("2026-09-04 03:00", tz="UTC"))
    check("03:00 UTC is Asia", name == "asia", name)


# --- session structure ------------------------------------------------------

def _session_frames() -> dict:
    """One full day: a quiet Asia, then a London push above it."""
    start = datetime(2026, 9, 4, 0, 0, tzinfo=timezone.utc)
    asia = [(4000, 4005, 3995, 4000)] * 84          # 00:00-07:00 on M5
    london = [(4000 + i * 0.5, 4002 + i * 0.5, 3999 + i * 0.5, 4001 + i * 0.5)
              for i in range(60)]                    # 07:00-12:00, trending up
    m5 = frame(asia + london, start)
    daily = pd.DataFrame(
        {"open": [3950.0, 3990.0], "high": [4010.0, 4020.0],
         "low": [3900.0, 3960.0], "close": [3990.0, 4000.0],
         "volume": [1000.0, 1000.0], "spread": [25, 25]},
        index=pd.DatetimeIndex(["2026-09-02", "2026-09-03"], tz="UTC", name="time"))
    return {"M5": m5, "D1": daily}


def test_session_structure_reads_the_clock():
    print("prior day, Asia range and initial balance come from timestamps")
    frames = _session_frames()
    now = pd.Timestamp("2026-09-04 11:55", tz="UTC")
    read = auction.analyse(frames, now)

    check("the read is available", read.get("available"), read.get("note", ""))
    prior = read["prior_day"]
    check("the prior day is yesterday, not today",
          prior["date"] == "2026-09-03", prior["date"])
    check("with yesterday's high", prior["high"] == 4020.0, str(prior["high"]))

    asia = read["asia"]
    check("the Asia range covers 00:00-07:00 only",
          asia["high"] == 4005.0 and asia["low"] == 3995.0, str(asia))

    ib = read["initial_balance"]
    check("the initial balance belongs to the live session",
          ib["session"] == "london", ib["session"])
    check("and covers its first hour", ib["bars"] == 12, str(ib["bars"]))


def test_range_extension_distinguishes_a_trend_from_a_rotation():
    print("one-sided extension is a trend day, two-sided is a rotation")
    ib = {"high": 4010.0, "low": 3990.0, "width": 20.0}
    up = auction._extension(ib, {"high": 4030.0, "low": 3995.0}, 4025.0)
    check("breaking only the high is an upward extension",
          up["direction"] == "upward", up["direction"])

    both = auction._extension(ib, {"high": 4030.0, "low": 3970.0}, 4000.0)
    check("breaking both edges is a rotation", both["direction"] == "both",
          both["direction"])
    check("and the note says to fade rather than chase",
          "rotation" in both["note"], both["note"])

    inside = auction._extension(ib, {"high": 4008.0, "low": 3992.0}, 4000.0)
    check("staying inside is no extension", inside["direction"] == "none",
          inside["direction"])
    check("and the setup is the first break", inside["inside"])


def test_range_used_knows_when_the_day_is_spent():
    print("a day that has run its range is called extended")
    spent = auction._range_used({"width": 140.0}, typical=90.0)
    check("140 of a typical 90 is extended", spent["verdict"] == "extended",
          spent["verdict"])
    check("and advises rotation over chasing", "fading" in spent["note"], spent["note"])

    fresh = auction._range_used({"width": 25.0}, typical=90.0)
    check("25 of 90 leaves room", fresh["verdict"] == "room", fresh["verdict"])
    check("no typical range means no verdict at all",
          auction._range_used({"width": 25.0}, typical=None) is None)


# --- statistical persistence ------------------------------------------------

def test_a_trending_series_reads_as_persistent():
    print("a steadily rising series is measured as persistent")
    closes = np.array([4000.0 + i * 2.0 for i in range(200)])
    er = persistence.efficiency_ratio(closes)
    check("the efficiency ratio is near 1 for a straight line",
          er is not None and er > 0.95, str(er))
    h = persistence.hurst(closes)
    check("the Hurst exponent is produced", h is not None, str(h))


def test_an_alternating_series_reads_as_reverting():
    print("a series that gives everything back is measured as mean-reverting")
    closes = np.array([4000.0 + (5.0 if i % 2 else -5.0) for i in range(200)])
    er = persistence.efficiency_ratio(closes)
    check("the efficiency ratio is near zero", er is not None and er < 0.05, str(er))

    returns = persistence._returns(closes)
    measured = persistence.variance_ratio(returns, 4)
    check("the variance ratio is below 1", measured is not None and measured[0] < 1.0,
          str(measured))


def test_the_variance_ratio_needs_a_sample():
    print("the variance ratio refuses a sample too short to test")
    returns = persistence._returns(np.array([4000.0, 4001.0, 4002.0, 4003.0]))
    check("a four-bar sample returns nothing",
          persistence.variance_ratio(returns, 8) is None)


def test_disagreeing_estimators_are_reported_as_disagreement():
    print("estimators that disagree produce a weak verdict, not a confident one")
    verdict, confidence, playbook, _ = persistence._interpret(
        ratios={2: {"ratio": 0.8, "z": -0.5, "significant": False}},
        h=0.70,          # persistent
        er=0.05)         # reverting
    check("confidence is not high", confidence != "high", confidence)
    check("a playbook is still offered", bool(playbook))


def test_persistence_needs_enough_bars():
    print("too little history reports unavailable rather than a number")
    start = datetime(2026, 9, 4, tzinfo=timezone.utc)
    read = persistence.analyse({"M15": flat_frame(4000.0, 20, start, minutes=15)})
    check("a twenty-bar frame is refused", not read.get("available"),
          read.get("note", ""))


# --- the regime never gates -------------------------------------------------

def test_the_regime_sizes_a_setup_and_never_hides_it():
    print("a detector is scaled by the regime, never silenced by it")
    from goldassist.strategies import auction as auction_strategies

    class _Ctx:
        def __init__(self, regime):
            self.persistence = {"available": True, "regime": regime} if regime \
                else {"available": False}

        def persistent(self):
            return strategy_base.Context.persistent(self)

    agree, _ = auction_strategies._regime_weight(_Ctx("mean-reverting"),
                                                 wants_persistence=False)
    against, note = auction_strategies._regime_weight(_Ctx("persistent"),
                                                      wants_persistence=False)
    unknown, _ = auction_strategies._regime_weight(_Ctx(None),
                                                   wants_persistence=False)

    check("a matching regime scores full", agree == 1.0, str(agree))
    check("an opposing regime is reduced but never zero",
          0 < against < agree, str(against))
    check("and says it is shown anyway", "sized down" in note, note)
    check("an unmeasured regime is neutral, not a penalty",
          0.5 <= unknown <= 0.7, str(unknown))


def main() -> int:
    from tests.clock import outside_rollover
    outside_rollover()
    for test in (test_a_bar_before_arming_cannot_fill,
                 test_a_bar_after_arming_fills_normally,
                 test_an_unarmed_setup_can_still_be_invalidated_early,
                 test_a_missing_arming_time_degrades_to_the_old_behaviour,
                 test_the_live_quote_always_fills,
                 test_vwap_is_anchored_not_rolling,
                 test_vwap_bands_widen_with_dispersion,
                 test_vwap_needs_enough_bars,
                 test_vwap_survives_a_feed_with_no_volume,
                 test_the_session_anchor_follows_the_clock,
                 test_session_structure_reads_the_clock,
                 test_range_extension_distinguishes_a_trend_from_a_rotation,
                 test_range_used_knows_when_the_day_is_spent,
                 test_a_trending_series_reads_as_persistent,
                 test_an_alternating_series_reads_as_reverting,
                 test_the_variance_ratio_needs_a_sample,
                 test_disagreeing_estimators_are_reported_as_disagreement,
                 test_persistence_needs_enough_bars,
                 test_the_regime_sizes_a_setup_and_never_hides_it):
        test()

    if FAILURES:
        print(f"\n{len(FAILURES)} check(s) failed:")
        for name in FAILURES:
            print(f"  - {name}")
        return 1
    print("\nAll intraday-layer checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
