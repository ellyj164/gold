"""Checks for execution realism and the measurement layer (v1.8.0).

Each check here pins a way the record used to be kinder than the market:

1. **Fills.** An armed setup that enters its zone without reaching the
   preferred price gets the first in-zone price on offer, not the best price in
   the bar.
2. **The trading day.** A filled trade is flat just before the New York
   rollover, and nothing fills across it.
3. **Releases.** The news clock is the analysis clock, and the hour after a
   high-impact print is penalised as well as the half hour before it.
4. **Exposure.** Setups stacked on an open same-direction trade are told so,
   and their fill is skipped.
5. **Restarts.** A filled trade is resumed whatever its age, not dropped.
6. **Measurement.** Evidence is recorded per trade; the weight fit, the Monte
   Carlo band, the live-forward check and the score record behave.

Bars in this file are dated in 2030 at fixed New York times, so no check
depends on the hour the suite happens to be run at -- the rollover rules would
otherwise make any fill test flaky between 16:55 and 18:00 New York.

Run with:  python -m tests.test_realism
"""

from __future__ import annotations

import random
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from goldassist import signals
from goldassist.analysis import events, mtf
from goldassist.backtest import calibration, fit, rigour
from goldassist.decision import confluence
from goldassist.decision import engine as decision_engine
from goldassist.decision import profile as profile_module
from goldassist.signals import resolve
from goldassist.signals.engine import features_for

from tests.test_engines import make_frame
from tests.test_signals import make_analysis, make_decision

FAILURES: list[str] = []
NEWYORK = ZoneInfo("America/New_York")
NOON = datetime(2030, 1, 16, 12, 0, tzinfo=NEWYORK).astimezone(timezone.utc)


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        FAILURES.append(name)


def bars(rows, start: datetime = NOON, minutes: int = 1) -> pd.DataFrame:
    index = [start + timedelta(minutes=i * minutes) for i in range(len(rows))]
    return pd.DataFrame(
        [{"open": o, "high": h, "low": l, "close": c, "volume": 100.0}
         for o, h, l, c in rows],
        index=pd.DatetimeIndex(index, name="time"))


def armed(profile_name: str = "Default XAUUSD"):
    prof = profile_module.get(profile_name)
    suggestion = signals.build(make_analysis(4400.0), make_decision(4400.0, 76.0), prof)
    suggestion.armed = True
    return suggestion, prof


# --- fills -------------------------------------------------------------------

def test_fill_is_the_first_in_zone_price():
    print("an entry that misses the preferred price gets the price on offer")
    suggestion, prof = armed()
    zone = suggestion.entry_zone
    inside = (zone.preferred + zone.high) / 2
    resolve.apply_bars(suggestion, bars([
        (inside, inside + 0.1, zone.preferred + 0.05, inside),
    ]), prof)
    check("a bar opening inside the zone fills at its open",
          suggestion.fill_price == round(inside, 3),
          f"{suggestion.fill_price} vs open {inside:.3f}")

    suggestion, prof = armed()
    zone = suggestion.entry_zone
    resolve.apply_bars(suggestion, bars([
        (zone.high + 1.0, zone.high + 1.2, zone.preferred + 0.05, zone.high + 0.5),
    ]), prof)
    check("a bar opening above the zone fills at the zone's top, not its low",
          suggestion.fill_price == round(zone.high, 3),
          f"{suggestion.fill_price} vs top {zone.high:.3f}")

    suggestion, prof = armed()
    zone = suggestion.entry_zone
    resolve.apply_bars(suggestion, bars([
        (zone.high + 1.0, zone.high + 1.2, zone.preferred - 0.5, zone.preferred),
    ]), prof)
    check("reaching the preferred price fills exactly there, never better",
          suggestion.fill_price == round(zone.preferred, 3), str(suggestion.fill_price))


# --- the trading day -----------------------------------------------------------

def test_trade_is_flat_before_the_rollover():
    print("an intraday trade does not survive the rollover")
    suggestion, prof = armed()
    zone = suggestion.entry_zone
    tp1 = suggestion.targets[0].price
    stop = suggestion.stop
    late = datetime(2030, 1, 16, 16, 56, tzinfo=NEWYORK).astimezone(timezone.utc)
    middle = (zone.preferred + tp1) / 2
    resolve.apply_bars(suggestion, bars([(zone.high, zone.high, zone.preferred - 0.2,
                                          zone.preferred)]), prof)
    resolve.apply_bars(suggestion, bars([(middle, middle + 0.1, max(stop + 1, middle - 0.1),
                                          middle)], start=late), prof)
    check("the trade closed", suggestion.status in signals.TERMINAL, suggestion.status)
    check("on the rollover rule", suggestion.exit_reason == resolve.ROLLOVER,
          str(suggestion.exit_reason))
    check("at the open of the bar that crossed the deadline",
          suggestion.exit_price == round(middle, 3), str(suggestion.exit_price))


def test_nothing_fills_across_the_rollover():
    print("nothing fills between the flat deadline and the end of the blowout")
    suggestion, prof = armed()
    zone = suggestion.entry_zone
    during = datetime(2030, 1, 16, 17, 20, tzinfo=NEWYORK).astimezone(timezone.utc)
    resolve.apply_bars(suggestion, bars([(zone.high, zone.high, zone.preferred - 0.2,
                                          zone.preferred)], start=during), prof)
    check("an armed setup does not fill at 17:20 New York", not suggestion.entered)

    after = datetime(2030, 1, 16, 18, 10, tzinfo=NEWYORK).astimezone(timezone.utc)
    resolve.apply_bars(suggestion, bars([(zone.high, zone.high, zone.preferred - 0.2,
                                          zone.preferred)], start=after), prof)
    check("it fills once the spread has settled", suggestion.entered)


def test_rollover_follows_daylight_saving():
    print("the rollover is New York time, not a fixed UTC hour")
    prof = profile_module.get("Default XAUUSD")
    winter = resolve.rollover_deadline(prof, datetime(2030, 1, 15, 15, 0, tzinfo=timezone.utc))
    summer = resolve.rollover_deadline(prof, datetime(2030, 7, 15, 15, 0, tzinfo=timezone.utc))
    check("winter flat deadline is 21:55 UTC",
          winter == datetime(2030, 1, 15, 21, 55, tzinfo=timezone.utc), str(winter))
    check("summer flat deadline is 20:55 UTC",
          summer == datetime(2030, 7, 15, 20, 55, tzinfo=timezone.utc), str(summer))


def test_breakeven_in_record_is_opt_in():
    print("breakeven in the record is a hypothesis, off until switched on")
    default = profile_module.get("Default XAUUSD")
    check("off by default", default.breakeven_in_record is False)

    trial = profile_module.StrategyProfile.from_dict(
        dict(default.to_dict(), breakeven_in_record=True, name="trial"))
    suggestion, _ = armed()
    zone = suggestion.entry_zone
    resolve.apply_bars(suggestion, bars([(zone.high, zone.high, zone.preferred - 0.2,
                                          zone.preferred)]), trial)
    risk = float(suggestion.risk)
    up = suggestion.fill_price + 1.1 * risk
    tp1 = suggestion.targets[0].price
    high = min(up, tp1 - 0.01)
    if high < suggestion.fill_price + risk:
        check("fixture leaves room for 1R before TP1", False, f"risk {risk}, tp1 {tp1}")
        return
    resolve.apply_bars(suggestion, bars([(suggestion.fill_price, high,
                                          suggestion.fill_price + 0.1, high - 0.1)],
                                        start=NOON + timedelta(minutes=5)), trial)
    check("the stop moved to the fill after 1R", suggestion.stop == suggestion.fill_price,
          f"{suggestion.stop} vs {suggestion.fill_price}")
    resolve.apply_bars(suggestion, bars([(high, high, suggestion.fill_price - 0.5,
                                          suggestion.fill_price - 0.4)],
                                        start=NOON + timedelta(minutes=10)), trial)
    check("coming back closes it as a scratch", suggestion.outcome == "scratch",
          str(suggestion.outcome))


# --- releases ------------------------------------------------------------------

def test_news_is_read_on_the_analysis_clock():
    print("the calendar is read at the replayed moment, and looks back as well")
    cpi = datetime(2026, 9, 11, 12, 30, tzinfo=timezone.utc)
    calendar = [{"time": cpi, "title": "US CPI", "impact": "high"}]
    after = events.upcoming(calendar, now=cpi + timedelta(minutes=25))
    check("25 minutes after, the release is reported as just printed",
          (after.get("last_high_impact") or {}).get("minutes_since") == 25,
          str(after.get("last_high_impact")))

    frames = {"M5": make_frame(np.linspace(4390, 4400, 300), freq="5min")}
    closed = mtf._close_time(frames)                         # noqa: SLF001
    newest = frames["M5"].index[-1].to_pydatetime()
    check("the analysis clock is the newest candle's close, not its open",
          closed is not None and abs((closed - newest.replace(
              tzinfo=newest.tzinfo or timezone.utc)).total_seconds() - 300) < 1,
          f"{closed} vs open {newest}")


def test_the_hour_after_a_release_is_penalised():
    print("a release that has just printed costs the score, like one about to")
    frames = {}
    base = np.linspace(2000, 2100, 400)
    for tf, freq in (("D1", "1D"), ("H4", "4h"), ("H1", "1h"), ("M15", "15min"), ("M5", "5min")):
        frames[tf] = make_frame(base + np.random.default_rng(7).normal(0, 0.4, len(base)), freq=freq)
    analysis = mtf.analyse(frames)
    profile = profile_module.get("Default XAUUSD")

    analysis["events"] = {"next_high_impact": None,
                          "last_high_impact": {"title": "US CPI", "impact": "high",
                                               "time": "", "minutes_since": 25}}
    scored = confluence.evaluate(analysis, profile)
    factors = {p["factor"] for p in scored["bullish"]["penalties"]}
    check("25 minutes after CPI the aftermath penalty applies", "news_aftermath" in factors,
          str(factors))

    analysis["events"]["last_high_impact"]["minutes_since"] = 90
    scored = confluence.evaluate(analysis, profile)
    factors = {p["factor"] for p in scored["bullish"]["penalties"]}
    check("90 minutes after, it has lifted", "news_aftermath" not in factors, str(factors))

    decision = decision_engine.decide(analysis, profile)
    record = features_for(decision, "bullish", 80.0)
    check("the evidence record carries every pillar",
          all(f"pillar:{p}" in record for p in fit.PILLARS), str(sorted(record)))
    check("pillar features are shares of their maximum",
          all(0.0 <= v <= 1.0 for k, v in record.items() if k.startswith("pillar:")))


# --- exposure and restarts ---------------------------------------------------------

def test_stacked_setups_are_told_about_open_exposure():
    print("a setup on top of an open same-direction trade says so")
    prof = profile_module.get("Default XAUUSD")
    book = signals.SuggestionBook()
    analysis = make_analysis(4400.0)
    book.sync(analysis, make_decision(4400.0, 76.0), prof)
    longs = [s for s in book.live if s.direction == "bullish"]
    if len(longs) < 2:
        check("fixture produces two long setups", False, str(len(longs)))
        return
    first, second = longs[0], longs[1]
    first.entered = True
    first.fill_price = first.entry_zone.preferred
    first.filled_at = datetime.now(timezone.utc).isoformat()

    book._mark_exposure(analysis, datetime.now(timezone.utc))     # noqa: SLF001
    check("the unfilled long reports one open long",
          second.exposure.get("same_direction_open") == 1, str(second.exposure))
    check("and carries it among its risks",
          any("added exposure" in r for r in second.risks), str(second.risks))
    check("the filled trade froze its own at-fill count",
          first.exposure_at_fill == 0, str(first.exposure_at_fill))


def _open_peer(filled_at: datetime, closed_at: datetime | None = None):
    """A filled long on the same move, for a second setup to stack on."""
    peer, _ = armed()
    peer.entered = True
    peer.fill_price = peer.entry_zone.preferred
    peer.filled_at = filled_at.isoformat()
    if closed_at is not None:
        peer.outcome = "loss"
        peer.exit_at = closed_at.isoformat()
    return peer


def _into_zone(suggestion, prof):
    zone = suggestion.entry_zone
    return resolve.apply_bars(suggestion, bars([
        (zone.high, zone.high, zone.preferred - 0.05, zone.preferred),
    ]), prof)


def test_a_stacked_fill_is_skipped():
    print("a fill that would stack on an open trade on the same move is skipped")
    import dataclasses

    suggestion, prof = armed()
    suggestion.peers = [_open_peer(NOON - timedelta(minutes=10))]
    suggestion.peer_atr = 5.0
    events = _into_zone(suggestion, prof)
    check("it does not fill", not suggestion.entered, str(suggestion.fill_price))
    check("it is retired as skipped", suggestion.exit_reason == resolve.SKIPPED,
          str(suggestion.exit_reason))
    check("and recorded as untaken, not as a loss", suggestion.outcome == "untaken",
          str(suggestion.outcome))
    check("the trader is told why", any("Skipped" in e for e in events), str(events))

    off = dataclasses.replace(prof, skip_stacked=False)
    suggestion, _ = armed()
    suggestion.peers = [_open_peer(NOON - timedelta(minutes=10))]
    suggestion.peer_atr = 5.0
    _into_zone(suggestion, off)
    check("with skip_stacked off it fills as before", suggestion.entered)

    suggestion, prof = armed()
    suggestion.peers = [_open_peer(NOON - timedelta(minutes=30),
                                   closed_at=NOON - timedelta(minutes=1))]
    suggestion.peer_atr = 5.0
    _into_zone(suggestion, prof)
    check("a peer that already closed does not block it", suggestion.entered)

    suggestion, prof = armed()
    suggestion.peers = [_open_peer(NOON + timedelta(minutes=30))]
    suggestion.peer_atr = 5.0
    _into_zone(suggestion, prof)
    check("nor does one that filled after this candle", suggestion.entered)

    suggestion, prof = armed()
    peer = _open_peer(NOON - timedelta(minutes=10))
    peer.direction = "bearish"
    suggestion.peers = [peer]
    suggestion.peer_atr = 5.0
    _into_zone(suggestion, prof)
    check("nor does an open trade the other way", suggestion.entered)


def test_the_book_hands_every_setup_its_peers():
    print("the book tells the resolver what else is on the board, and lets go after")
    prof = profile_module.get("Default XAUUSD")
    book = signals.SuggestionBook()
    analysis = make_analysis(4400.0)
    # Setups admitted on a cycle are handed the board on the next one, which is
    # also the first cycle on which they can fill.
    book.sync(analysis, make_decision(4400.0, 76.0), prof)
    book.sync(analysis, make_decision(4400.0, 76.0), prof)
    check("every live setup carries the board",
          bool(book.live) and all(s.peers for s in book.live), str(len(book.live)))
    check("and the ATR that defines the same move",
          all(s.peer_atr == float(analysis.get("atr") or 0.0) for s in book.live))
    check("a retired setup holds no board",
          all(not s.peers for s in book.retired), str(len(book.retired)))


def test_confirmations_are_counted_at_the_fill():
    print("the agreement a trade is recorded with is the agreement at its fill")
    from goldassist.backtest.engine import _trade_from              # noqa: PLC2701
    from goldassist.signals.engine import Suggestion

    suggestion, prof = armed()
    suggestion.confirmations = [{"key": "a"}, {"key": "b"}]
    _into_zone(suggestion, prof)
    check("it filled", suggestion.entered)
    suggestion.confirmations.append({"key": "c"})        # agreement after entry
    check("the at-fill count is frozen", suggestion.confirmations_at_fill == 2,
          str(suggestion.confirmations_at_fill))
    restored = Suggestion.restore(suggestion.persist())
    check("and survives a restart", restored.confirmations_at_fill == 2,
          str(restored.confirmations_at_fill))
    check("a restored setup carries no board", restored.peers == [])
    now = datetime.now(timezone.utc)
    trade = _trade_from(suggestion, entered_at=now, exited_at=now, bars_held=1)
    check("the backtest trade carries it", trade.confirmations_at_fill == 2,
          str(trade.confirmations_at_fill))
    check("beside the closing count", trade.confirmations == 3, str(trade.confirmations))


def test_trades_are_grouped_on_the_score_they_were_taken_on():
    print("a trade's recorded score is its score at entry, not at close")
    from datetime import datetime as _dt
    from goldassist.backtest.engine import _trade_from              # noqa: PLC2701

    suggestion, prof = armed()
    suggestion.entry_score = 78.0
    zone = suggestion.entry_zone
    stop = suggestion.stop
    resolve.apply_bars(suggestion, bars([
        (zone.high, zone.high, zone.preferred - 0.2, zone.preferred),
        (zone.preferred, zone.preferred, stop - 0.5, stop - 0.2),
    ]), prof)
    suggestion.score = 61.0          # the evidence drained away as it lost
    now = _dt.now(timezone.utc)
    trade = _trade_from(suggestion, entered_at=now, exited_at=now, bars_held=1)
    check("the backtest trade carries the entry score", trade.score == 78.0, str(trade.score))
    check("and keeps the closing score beside it", trade.final_score == 61.0,
          str(trade.final_score))


def test_a_filled_trade_survives_a_restart_whatever_its_age():
    print("a filled trade is resumed after a restart, an old unfilled idea is not")
    prof = profile_module.get("Default XAUUSD")
    old = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()

    filled = signals.build(make_analysis(4400.0), make_decision(4400.0, 76.0), prof)
    filled.created_at = old
    filled.armed = filled.entered = True
    filled.fill_price = filled.entry_zone.preferred
    filled.filled_at = old

    idea = signals.build(make_analysis(4400.0), make_decision(4400.0, 76.0), prof)
    idea.created_at = old

    book = signals.SuggestionBook()
    restored = book.restore([filled.persist(), idea.persist()], prof)
    ids = {s.id for s in restored}
    check("the two-day-old filled trade is resumed", filled.id in ids)
    check("the two-day-old unfilled idea is not", idea.id not in ids)


# --- measurement --------------------------------------------------------------------

def test_weight_fit_recovers_real_evidence():
    print("the walk-forward fit finds evidence that matters and ignores noise")
    rng = random.Random(7)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    trades = []
    for i in range(900):
        features = {f"pillar:{p}": rng.random() for p in fit.PILLARS}
        features["conviction"] = rng.uniform(50, 95)
        z = -1.0 + 2.2 * features["pillar:location"] + 1.6 * features["pillar:liquidity_sweep"]
        win = rng.random() < 1 / (1 + np.exp(-z))
        trades.append({"entered_at": (start + timedelta(hours=i)).isoformat(),
                       "features": features,
                       "score": sum(features[f"pillar:{p}"] for p in fit.PILLARS) / 7 * 100,
                       "net_r_multiple": 1.8 if win else -1.02})
    report = fit.assess(trades)
    check("a fit is produced", report["available"], report.get("note", ""))
    if report["available"]:
        check("it beats an equal-weight score out of sample", report["beats_score"],
              report["verdict"])
        weights = report["splits"][-1]["weights"]
        check("location outweighs momentum", weights["pillar:location"]
              > abs(weights["pillar:momentum"]), str(weights))
    check("a run without recorded evidence is refused",
          not fit.assess([{"score": 70, "net_r_multiple": 1.0}] * 300)["available"])


def test_monte_carlo_and_forward_check():
    print("the reshuffled band, and the live record measured against it")
    backtest = [{"net_r_multiple": v, "r_multiple": v} for v in [1.8] * 45 + [-1.0] * 55]
    band = rigour.monte_carlo(backtest)
    check("the 95th percentile drawdown is at least the median",
          band["drawdown_r"]["p95"] >= band["drawdown_r"]["p50"] > 0, str(band))
    check("the same run always prints the same band", rigour.monte_carlo(backtest) == band)
    check("a live record like the backtest sits inside it",
          rigour.forward_check(backtest, [1.8, -1.0, 1.8, -1.0, -1.0] * 12)["percentile"] > 20)
    check("sixty straight losses sit outside it",
          rigour.forward_check(backtest, [-1.0] * 60)["percentile"] < 5)


def test_score_record_is_history_not_odds():
    print("the score record states its sample and never calls itself odds")
    stats = {"by_score_band": {"75-85": {"setups": 12, "win_rate": 50.0,
                                          "net_average_r": 0.3}}}
    check("a run that grouped on the closing score says so",
          "drifts with the outcome" in calibration.band_record(stats, 80.0)["note"])
    record = calibration.band_record(stats, 80.0, {"id": 1, "start_date": "2026-01-01",
                                                   "end_date": "2026-06-01"})
    check("a thin band says so", "too few" in record["note"], record["note"])
    check("a run from before the fill fix says so", "optimistic" in record["note"])
    check("it is framed as history", "not the odds" in record["note"])


def main() -> int:
    for test in (test_fill_is_the_first_in_zone_price,
                 test_trade_is_flat_before_the_rollover,
                 test_nothing_fills_across_the_rollover,
                 test_rollover_follows_daylight_saving,
                 test_breakeven_in_record_is_opt_in,
                 test_news_is_read_on_the_analysis_clock,
                 test_the_hour_after_a_release_is_penalised,
                 test_stacked_setups_are_told_about_open_exposure,
                 test_a_stacked_fill_is_skipped,
                 test_the_book_hands_every_setup_its_peers,
                 test_confirmations_are_counted_at_the_fill,
                 test_trades_are_grouped_on_the_score_they_were_taken_on,
                 test_a_filled_trade_survives_a_restart_whatever_its_age,
                 test_weight_fit_recovers_real_evidence,
                 test_monte_carlo_and_forward_check,
                 test_score_record_is_history_not_odds):
        test()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) failed: {', '.join(FAILURES)}")
        return 1
    print("All realism and measurement checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
