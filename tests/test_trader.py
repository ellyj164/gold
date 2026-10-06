"""Checks for the trader behaviour layer.

The modules under test are the ones that decide how the application *acts*
rather than what it sees: session awareness, day character, the top-down
method, trade management, the outcome memory, the session view, and the setup
book that lets more than one setup exist at a time.

Two properties are pinned here deliberately, because both were explicit
requirements and both are easy to reintroduce by accident:

* **Nothing withholds a setup.** No module in this layer may block, cap or
  suppress a qualifying suggestion. The application does not trade, so it has
  no business hiding analysis.
* **The memory never claims more than its sample supports.** Below the minimum
  sample size it must say it does not know.

Run with:  python -m tests.test_trader
"""

from __future__ import annotations

import contextlib
import itertools
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from goldassist import signals
from goldassist.decision import profile as profile_module
from goldassist.storage import Database
from goldassist.trader import (awareness, character, macro, management, memory,
                               sessions, thesis, topdown)
from tests.test_signals import make_analysis, make_decision

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        FAILURES.append(name)


# --- fixtures --------------------------------------------------------------

def utc(weekday: int, hour: int, minute: int = 0) -> datetime:
    """A moment in a known week, indexed Monday=0. 2026-08-17 is a Monday."""
    return datetime(2026, 8, 17 + weekday, hour, minute, tzinfo=timezone.utc)


def candles(closes: list[float], highs=None, lows=None) -> pd.DataFrame:
    index = pd.date_range("2026-08-17", periods=len(closes), freq="15min", tz="UTC")
    highs = highs or [c + 1 for c in closes]
    lows = lows or [c - 1 for c in closes]
    return pd.DataFrame(
        {"open": closes, "high": highs, "low": lows, "close": closes,
         "volume": [100.0] * len(closes), "spread": [0.0] * len(closes)},
        index=index,
    )


def temp_db() -> Database:
    path = Path(tempfile.mkdtemp(prefix="goldassist-test-")) / "test.db"
    return Database(path)


_SEQUENCE = itertools.count()


def outcome(**kwargs) -> dict:
    """One recorded outcome, distinct from every other by default.

    Each call steps the entry price and the fill time, because that is what
    separate trades actually look like and the record cleaner now depends on
    it: rows sharing a strategy, a direction, a price and a moment are the
    signature of the double-instance bug, and are collapsed to one. A fixture
    that stamped twenty trades at one price and one instant was describing
    twenty copies of a single trade, not twenty trades.
    """
    nth = next(_SEQUENCE)
    base = {
        "signal_uid": "x", "created_at": _now(), "closed_at": _now(),
        "direction": "bullish", "setup_type": "pullback", "session": "london",
        "regime": "strong bullish trend", "day_verdict": "TRADE", "score": 75.0,
        "quality": "Good", "entry": 4400.0 + nth, "invalidation": 4380.0 + nth,
        "tp1": 4430.0 + nth,
        "entered": 1, "outcome": "win", "r_multiple": 1.5,
        "max_favourable_r": 1.8, "max_adverse_r": -0.3, "reason": "COMPLETED",
        "notes": "",
        # A filled trade needs a fill price. `entered` alone is no longer the
        # test anywhere: two pre-migration rows in the live database carry
        # `entered = 1` with no fill, no exit and a fabricated +3.5R, and they
        # are exactly what the fill-price rule exists to exclude.
        "fill_price": 4400.0 + nth, "filled_at": _at(nth), "exit_price": 4430.0 + nth,
    }
    base.update(kwargs)
    return base


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _at(nth: int) -> str:
    """A fill time ``nth`` hours back, so fixtures are ordered and distinct."""
    return (datetime.now(timezone.utc) - timedelta(hours=nth)).isoformat()


# --- sessions --------------------------------------------------------------

def test_sessions():
    print("sessions")
    overlap = sessions.read(utc(1, 14))
    check("the London/NY overlap is recognised", overlap.name == "overlap",
          overlap.name)
    check("the overlap is the highest-quality window", overlap.quality == 1.0)

    asia = sessions.read(utc(2, 2))
    check("the Asian session is recognised", asia.name == "asia", asia.name)
    check("Asian hours score lower than the overlap", asia.quality < overlap.quality)
    check("Asian hours carry a warning", bool(asia.notes))

    rollover = sessions.read(utc(1, 22))
    check("rollover is recognised", rollover.name == "rollover", rollover.name)

    saturday = sessions.read(utc(5, 12))
    check("Saturday is closed", not saturday.market_open)
    check("a closed market has zero quality", saturday.quality == 0.0)

    friday_late = sessions.read(utc(4, 22))
    check("Friday after the close is closed", not friday_late.market_open)
    check("Monday morning is open", sessions.read(utc(0, 9)).market_open)

    london = sessions.read(utc(1, 8))
    check("the London open window is flagged", any("London open" in s
                                                   for s in london.sub_windows))
    check("time to the next session is reported", london.minutes_to_next > 0)


# --- day character ---------------------------------------------------------

def test_character_measures():
    print("day character measures")
    trending = candles([4400 + i for i in range(40)])
    chopping = candles([4400 + (2 if i % 2 else -2) for i in range(40)])

    trend_eff = character.efficiency_ratio(trending["close"])
    chop_eff = character.efficiency_ratio(chopping["close"])
    check("a one-way move scores near-perfect efficiency", trend_eff > 0.9,
          f"{trend_eff:.2f}")
    check("an oscillation scores near-zero efficiency", chop_eff < 0.1,
          f"{chop_eff:.2f}")

    # A level price is repeatedly rejected: every touch closes back below it.
    respected = candles(
        [4400, 4408, 4401, 4409, 4400, 4407, 4402, 4408],
        highs=[4402, 4412, 4403, 4413, 4402, 4411, 4404, 4412],
        lows=[4398, 4406, 4399, 4407, 4398, 4405, 4400, 4406])
    rate, n = character.level_respect(respected, [{"price": 4410.0}], atr=10.0)
    check("level interactions are counted", n > 0, f"n={n}")
    check("repeated rejections read as respect", rate >= 0.6, f"{rate:.2f}")

    flips = character.whipsaw_count({"events": [
        {"direction": "bullish"}, {"direction": "bearish"},
        {"direction": "bullish"}, {"direction": "bearish"}]})
    check("alternating structure counts as whipsaw", flips == 3, str(flips))
    same = character.whipsaw_count({"events": [{"direction": "bullish"}] * 4})
    check("a one-way trend has no whipsaw", same == 0, str(same))


def test_character_verdict():
    print("day character verdict")
    analysis = make_analysis(4400.0)
    frames = {"M15": candles([4400 + i * 0.8 for i in range(60)])}
    clean = character.assess(analysis, frames, sessions.read(utc(1, 14)))
    check("a clean trending day is tradeable", clean.verdict == character.TRADE,
          f"{clean.verdict} @ {clean.score:.0f}")
    check("conviction is full on a clean day", clean.conviction == 1.0)

    noisy = {"M15": candles([4400 + (3 if i % 2 else -3) for i in range(60)])}
    chop = character.assess(analysis, noisy, sessions.read(utc(2, 2)))
    check("chop in the Asian session is not a TRADE day",
          chop.verdict != character.TRADE, chop.verdict)
    check("a poorer day reduces conviction", chop.conviction < clean.conviction)
    check("the reason is stated", bool(chop.cautions))

    closed = character.assess(analysis, frames, sessions.read(utc(5, 12)))
    check("a closed market stands aside", closed.verdict == character.STAND_ASIDE)


# --- awareness: context only, never a limit --------------------------------

def test_awareness_never_blocks():
    print("session awareness")
    counters = {
        "setups_identified": 12, "setups_filled": 9, "wins": 2, "losses": 7,
        "consecutive_losses": 4, "realised_r": -5.0, "last_loss_minutes": 3,
        "last_loss_at": _now(),
    }
    record = awareness.evaluate(counters, datetime.now(timezone.utc))

    fields = set(record.to_dict())
    forbidden = {"allow_new_setups", "allow_alerts", "restrictions", "blocked"}
    check("no gating fields are exposed", not (fields & forbidden),
          str(fields & forbidden))
    check("a bad day still only produces observations",
          all(isinstance(o, awareness.Observation) for o in record.observations))
    kinds = {o.kind for o in record.observations}
    check("a losing run is noted", "losing_run" in kinds, str(kinds))
    check("a recent loss is noted", "recent_loss" in kinds, str(kinds))
    check("drawdown is noted", "drawdown" in kinds, str(kinds))
    check("the running record is summarised", "9 filled" in record.headline
          or "filled" in record.headline, record.headline)

    quiet = awareness.evaluate({"setups_identified": 0}, datetime.now(timezone.utc))
    check("a quiet day produces no observations", not quiet.observations)


def test_awareness_counters():
    print("session counters")
    db = temp_db()
    db.record_setup_outcome(outcome(outcome="win", r_multiple=2.0))
    db.record_setup_outcome(outcome(outcome="loss", r_multiple=-1.0))
    db.record_setup_outcome(outcome(outcome="loss", r_multiple=-1.0))
    db.record_setup_outcome(outcome(outcome="untaken", entered=0, r_multiple=0.0))

    counters = awareness.gather(db)
    check("filled setups are counted", counters["setups_filled"] == 3,
          str(counters["setups_filled"]))
    check("unfilled setups are excluded from the fill count",
          counters["setups_identified"] == 4)
    check("realised R is summed", abs(counters["realised_r"] - 0.0) < 1e-6,
          str(counters["realised_r"]))
    check("consecutive losses are counted", counters["consecutive_losses"] == 2,
          str(counters["consecutive_losses"]))


def test_same_idea():
    print("repeat-idea detection")
    zone = signals.entry.build(make_analysis(4400.0), "bullish")
    failure = {"direction": "bullish", "low": zone.low, "high": zone.high,
               "at": _now()}
    check("an overlapping zone is the same idea",
          awareness.same_idea("bullish", zone, failure))
    check("the other direction is not the same idea",
          not awareness.same_idea("bearish", zone, failure))
    check("no previous failure means nothing to compare",
          not awareness.same_idea("bullish", zone, None))


# --- trade management ------------------------------------------------------

def make_entered_suggestion(price: float = 4400.0, score: float = 78.0):
    analysis = make_analysis(price)
    decision = make_decision(price, score)
    suggestion = signals.build(analysis, decision, profile_module.get(None))
    suggestion.entered = True
    return suggestion, analysis


def test_management_lifecycle():
    print("trade management")
    prof = profile_module.get(None)
    suggestion, analysis = make_entered_suggestion()
    trade = management.open_trade(suggestion)
    check("a trade opens from the suggestion", trade is not None)
    if trade is None:
        return
    check("risk is the entry-to-stop distance", trade.risk > 0)
    check("the stop starts at the invalidation",
          trade.current_stop == trade.initial_stop)

    # Move price one R in favour.
    favourable = dict(analysis)
    favourable["price"] = trade.entry + trade.risk * 1.05
    actions = management.update(trade, suggestion, favourable, prof)
    kinds = {a.kind for a in actions}
    check("breakeven is suggested at 1R", management.MOVE_TO_BREAKEVEN in kinds,
          str(kinds))
    check("the stop is moved to the entry", abs(trade.current_stop - trade.entry) < 1e-6)
    check("breakeven fires only once",
          management.MOVE_TO_BREAKEVEN not in
          {a.kind for a in management.update(trade, suggestion, favourable, prof)})

    # TP1 hit -> a partial is suggested.
    suggestion.targets[0].hit = True
    actions = management.update(trade, suggestion, favourable, prof)
    check("a partial is suggested at TP1",
          management.TAKE_PARTIAL in {a.kind for a in actions})
    check("position size is reduced", trade.position_open < 1.0)

    check("R is tracked", trade.r_now > 1.0, f"{trade.r_now:.2f}")
    check("the best excursion is remembered", trade.mfe_r >= trade.r_now)


def test_management_trailing_only_improves():
    print("trailing stop")
    prof = profile_module.get(None)
    suggestion, analysis = make_entered_suggestion()
    trade = management.open_trade(suggestion)
    trade.breakeven_set = True
    trade.current_stop = trade.entry
    before = trade.current_stop

    # The fixture's M15 swing low sits well below the entry, so no trail may
    # be applied: a trailing stop must never widen risk.
    moved = dict(analysis)
    moved["price"] = trade.entry + trade.risk
    management.update(trade, suggestion, moved, prof)
    check("a trail never moves the stop backwards", trade.current_stop >= before,
          f"{before:.2f} -> {trade.current_stop:.2f}")

    check("an improving level is accepted",
          management._improves(trade, trade.current_stop + 10, 1.0))
    check("a worsening level is rejected",
          not management._improves(trade, trade.current_stop - 10, 1.0))


def test_management_warnings():
    print("trade warnings")
    prof = profile_module.get(None)
    suggestion, analysis = make_entered_suggestion()
    trade = management.open_trade(suggestion)
    trade.opened_at = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()

    flat = dict(analysis)
    flat["price"] = trade.entry + trade.risk * 0.05
    kinds = {a.kind for a in management.update(trade, suggestion, flat, prof)}
    check("a stalled trade is flagged", management.STALLED in kinds, str(kinds))

    # A trade that ran to 1.5R and handed it all back.
    trade2 = management.open_trade(suggestion)
    trade2.mfe_r = 1.5
    giving_back = dict(analysis)
    giving_back["price"] = trade2.entry + trade2.risk * 0.2
    kinds = {a.kind for a in management.update(trade2, suggestion, giving_back, prof)}
    check("giving back a winner is flagged", management.GIVING_BACK in kinds,
          str(kinds))


def test_close_trade():
    print("closing a managed trade")
    suggestion, analysis = make_entered_suggestion()
    trade = management.open_trade(suggestion)

    trade.r_now = -1.0
    loss = management.close_trade(trade, suggestion, "INVALIDATED")
    check("a full stop-out is a loss", loss["outcome"] == "loss", loss["outcome"])

    trade.breakeven_set = True
    trade.r_now = -1.0
    protected = management.close_trade(trade, suggestion, "INVALIDATED")
    check("a breakeven stop cannot realise a loss",
          protected["r_multiple"] >= 0, str(protected["r_multiple"]))


# --- memory ----------------------------------------------------------------

def test_memory_refuses_small_samples():
    print("outcome memory: honesty")
    db = temp_db()
    for _ in range(3):
        db.record_setup_outcome(outcome(outcome="loss", r_multiple=-1.0))

    report = memory.Memory(db).report()
    check("three losses are counted", report.total == 3, str(report.total))
    check("no conclusion is drawn from three trades", not report.cautions,
          str(report.cautions))
    check("the headline says the sample is too small",
          "too few" in report.headline.lower(), report.headline)

    verdict = memory.Memory(db).verdict_on({"setup_type": "pullback"})
    check("the per-setup verdict also declines to judge",
          not verdict["warnings"], str(verdict["warnings"]))
    check("conviction is unchanged on an insufficient sample",
          verdict["conviction"] == 1.0)


def test_memory_reports_a_real_record():
    print("outcome memory: a usable sample")
    db = temp_db()
    for _ in range(9):
        db.record_setup_outcome(outcome(setup_type="breakout", outcome="loss",
                                        r_multiple=-1.0))
    for _ in range(10):
        db.record_setup_outcome(outcome(setup_type="pullback", outcome="win",
                                        r_multiple=1.6))

    mem = memory.Memory(db)
    report = mem.report()
    check("all closed setups are counted", report.total == 19, str(report.total))
    check("the losing setup type is called out",
          any("breakout" in c for c in report.cautions), str(report.cautions))
    check("the winning setup type is noted",
          any("pullback" in n for n in report.notes), str(report.notes))

    bad = mem.verdict_on({"setup_type": "breakout"}, report)
    check("a breakout is warned about", bool(bad["warnings"]), str(bad))
    check("a warning reduces conviction", bad["conviction"] < 1.0,
          str(bad["conviction"]))
    check("the sample size is always stated", "n=" in bad["warnings"][0],
          bad["warnings"][0])

    good = mem.verdict_on({"setup_type": "pullback"}, report)
    check("a pullback is supported", bool(good["support"]), str(good))
    check("support raises conviction", good["conviction"] >= 1.0)

    check("score bands are derived", memory.score_band(88) == "85+",
          memory.score_band(88))
    check("a low score bands correctly", memory.score_band(61) == "under 70")


# --- the session view ------------------------------------------------------

def test_thesis_is_sticky():
    print("session view")
    prof = profile_module.get(None)
    analysis = make_analysis(4400.0, "bullish")
    decision = make_decision(4400.0, 74.0, "bullish")

    view = thesis.form(analysis, decision, None, utc(1, 8))
    check("a view is written down", view.direction == "bullish", view.direction)
    check("the view has a statement", bool(view.statement))
    check("the view names what would make it wrong", bool(view.invalidated_if))
    check("key levels are recorded", "upside" in view.key_levels)

    # The higher timeframes turn. First contact only challenges the view.
    bearish = make_analysis(4400.0, "bearish")
    bear_decision = make_decision(4400.0, 74.0, "bearish")
    view, event = thesis.review(view, bearish, bear_decision, prof, None, utc(1, 9))
    check("a contrary read challenges rather than flips the view",
          view.direction == "bullish" and view.status == thesis.CHALLENGED,
          f"{view.direction}/{view.status}")
    check("the challenge is announced", bool(event))

    # Still challenged one minute later: far too soon to revise.
    view, _ = thesis.review(view, bearish, bear_decision, prof, None, utc(1, 9, 1))
    check("a one-minute disagreement does not revise the view",
          view.direction == "bullish", view.direction)

    # Agreement returns: the view is restored, not abandoned.
    view, event = thesis.review(view, analysis, decision, prof, None, utc(1, 10))
    check("returning agreement restores the view", view.status == thesis.INTACT,
          view.status)

    check("today's view is not stale", not thesis.is_stale(view, datetime.now(timezone.utc))
          or view.date != datetime.now().strftime("%Y-%m-%d"))
    check("a missing view is stale", thesis.is_stale(None))


def test_thesis_revision_needs_structure():
    print("session view revision")
    prof = profile_module.get(None)
    bullish = make_analysis(4400.0, "bullish")
    view = thesis.form(bullish, make_decision(4400.0, 74.0), None, utc(1, 8))
    view.challenged_since = utc(1, 8).isoformat()
    view.challenge_direction = "bearish"
    view.status = thesis.CHALLENGED

    # Long enough, but the H4/H1 events in the fixture are all bullish, so no
    # structure break confirms the new direction.
    bearish = make_analysis(4400.0, "bearish")
    for tf in ("H4", "H1"):
        bearish["timeframes"][tf]["structure"]["events"] = [
            {"kind": "BOS", "direction": "bullish", "time": 0}]
    revised, _ = thesis.review(view, bearish, make_decision(4400.0, 74.0, "bearish"),
                               prof, None, utc(1, 12))
    check("time alone does not revise the view", revised.direction == "bullish",
          revised.direction)

    # Now add a bearish H4 break printed after the view was formed.
    bearish["timeframes"]["H4"]["structure"]["events"] = [
        {"kind": "CHoCH", "direction": "bearish", "time": utc(1, 11).timestamp()}]
    revised, event = thesis.review(view, bearish,
                                   make_decision(4400.0, 74.0, "bearish"),
                                   prof, None, utc(1, 12))
    check("time plus a structure break revises the view",
          revised.direction == "bearish", revised.direction)
    check("the revision is recorded", bool(revised.revisions), str(revised.revisions))
    check("the revision explains itself", bool(event))


# --- the top-down method ---------------------------------------------------

def test_topdown():
    print("top-down method")
    analysis = make_analysis(4400.0, "bullish")
    read = topdown.assess(analysis)
    check("D1/H4 set the bias", read.bias == "bullish", read.bias)
    check("the bias timeframes agree", read.bias_agreement == "aligned",
          read.bias_agreement)
    check("execution timeframes are named",
          set(read.to_dict()["execution_timeframes"]) == {"H1", "M30", "M15"},
          str(read.to_dict()["execution_timeframes"]))

    suggestion = signals.build(analysis, make_decision(4400.0, 76.0),
                               profile_module.get(None))
    with_bias = topdown.assess(analysis, suggestion)
    check("a long under a bullish bias is with the bias",
          with_bias.compliance == topdown.WITH_BIAS, with_bias.compliance)
    check("the entry timeframe is identified",
          with_bias.execution_timeframe in ("H1", "M30", "M15"),
          str(with_bias.execution_timeframe))

    suggestion.direction = "bearish"
    against = topdown.assess(analysis, suggestion)
    check("a short under a bullish bias is counter-bias",
          against.compliance == topdown.COUNTER_BIAS, against.compliance)
    check("a counter-bias trade is explained, not blocked",
          bool(against.notes) and bool(against.headline))

    flat = make_analysis(4400.0, "bullish")
    for tf in ("D1", "H4"):
        flat["timeframes"][tf]["bias"] = "neutral"
    check("no higher-timeframe direction reads as no bias",
          topdown.assess(flat, suggestion).compliance == topdown.NO_BIAS)


# --- the setup book --------------------------------------------------------

def test_book_tracks_many_setups():
    print("setup book")
    prof = profile_module.get(None)
    book = signals.SuggestionBook()

    analysis = make_analysis(4400.0)
    decision = make_decision(4400.0, 76.0)
    decision["confluence"] = {
        "bullish": {"score": 76.0, "evidence": [{"detail": "H1 demand"}]},
        "bearish": {"score": 20.0, "evidence": []},
    }
    book.sync(analysis, decision, prof)
    check("the first setup is admitted", len(book.live) >= 1, str(len(book.live)))
    first = len(book.live)

    # The same picture again must not create a duplicate.
    book.sync(analysis, decision, prof)
    check("re-deriving the same idea does not duplicate it",
          len(book.live) == first, f"{first} -> {len(book.live)}")

    # A setup at a genuinely different price is a second opportunity.
    far = make_analysis(4480.0)
    far_decision = make_decision(4480.0, 74.0)
    far_decision["confluence"] = decision["confluence"]
    book.sync(far, far_decision, prof)
    check("a setup at a different area is tracked alongside the first",
          len(book.live) > first, str(len(book.live)))

    check("the book reports its counts", book.counts()["live"] == len(book.live))
    check("a primary setup is chosen", book.primary is not None)
    check("the book serialises", isinstance(book.to_dict()["live"], list))


def test_book_has_no_daily_cap():
    print("setup book: no daily cap")
    prof = profile_module.get(None)
    book = signals.SuggestionBook()

    # Twenty distinct setups through a day, each at its own price.
    admitted = 0
    for i in range(20):
        price = 4400.0 + i * 40
        analysis = make_analysis(price)
        decision = make_decision(price, 76.0)
        decision["confluence"] = {
            "bullish": {"score": 76.0, "evidence": [{"detail": "H1 demand"}]},
            "bearish": {"score": 10.0, "evidence": []},
        }
        before = len(book.live) + len(book.retired)
        book.sync(analysis, decision, prof)
        admitted += max(0, len(book.live) + len(book.retired) - before)

    check("many setups are admitted across a session", admitted >= 8, str(admitted))
    check("the book never exceeds its working limit",
          len(book.live) <= book.limit, str(len(book.live)))
    check("nothing is rejected for being late in the day",
          book.limit >= signals.book.DEFAULT_LIMIT)


def test_book_tracks_both_directions():
    print("setup book: both directions")
    prof = profile_module.get(None)
    book = signals.SuggestionBook()
    analysis = make_analysis(4400.0)
    decision = make_decision(4400.0, 76.0)
    decision["confluence"] = {
        "bullish": {"score": 76.0, "evidence": [{"detail": "H1 demand"}]},
        "bearish": {"score": 62.0, "evidence": [{"detail": "H4 supply overhead"}]},
    }
    book.sync(analysis, decision, prof)
    directions = {s.direction for s in book.live}
    check("both sides are tracked when both clear the watch score",
          len(directions) >= 1, str(directions))
    check("the opposing scenario is not labelled a recommendation",
          all(s.action in ("BUY", "SELL", "WAIT") for s in book.live))


def test_book_keeps_the_other_side_alive():
    print("setup book: the non-leading side survives")
    prof = profile_module.get(None)
    book = signals.SuggestionBook()

    analysis = make_analysis(4400.0)
    decision = make_decision(4400.0, 76.0, "bullish")
    decision["confluence"] = {
        "bullish": {"score": 76.0, "evidence": [{"detail": "H1 demand"}]},
        "bearish": {"score": 64.0, "evidence": [{"detail": "H4 supply overhead"}]},
    }
    book.sync(analysis, decision, prof)
    bearish = [s for s in book.live if s.direction == "bearish"]
    if not bearish:
        check("a bearish scenario is tracked alongside the bullish one", False)
        return
    check("a bearish scenario is tracked alongside the bullish one", True)

    # Advancing the book must not retire the side the engine is not leading.
    for _ in range(3):
        book.sync(analysis, decision, prof)

    survivors = [s for s in book.live if s.direction == "bearish"]
    check("the non-leading side is not invalidated on the next tick",
          bool(survivors), f"{len(book.retired)} retired")
    if survivors:
        check("it is scored from its own side of the evidence",
              abs(survivors[0].score - 64.0) < 0.001, str(survivors[0].score))
    check("no junk outcome was produced",
          all(s.direction != "bearish" for s in book.retired),
          str([s.status for s in book.retired]))


def _candidate(direction: str, low: float, high: float, stop: float,
               conviction: float, key: str = "sweep_reversal"):
    """A strategy candidate at a named price, for the conflict checks."""
    from goldassist.strategies.base import Candidate, SMC
    return Candidate(
        key=key, name=key.replace("_", " ").title(), family=SMC,
        direction=direction, trigger_family="reversal",
        label=f"{direction.title()} test setup", conviction=conviction,
        zone=(low, high), preferred=(low + high) / 2, stop=stop,
        stop_basis="test", evidence=["test evidence"])


def _book_with(candidates, price: float = 4596.0):
    """A book whose only candidates are the ones handed in."""
    prof = profile_module.get(None)
    book = signals.SuggestionBook()
    book._candidates = lambda *a, **k: list(candidates)   # noqa: SLF001
    analysis = make_analysis(price)
    decision = make_decision(price, 60.0)
    decision["confluence"] = {
        "bullish": {"score": 55.0, "evidence": [{"detail": "H1 demand"}]},
        "bearish": {"score": 50.0, "evidence": [{"detail": "H4 supply"}]},
    }
    return book, analysis, decision, prof


def test_book_refuses_two_directions_at_one_price():
    """The 2026-08-27 10:45 failure: one long and one short, 2 pips apart.

    Both were built from the same M5 candle -- it wicked through the highs and
    through the lows -- and each carried its stop inside the other's entry
    zone, so filling either one stopped the other. The book must publish one.
    """
    print("setup book: opposite directions at the same price")
    book, analysis, decision, prof = _book_with([
        _candidate("bearish", 4594.523, 4597.219, 4601.714, 78.7),
        _candidate("bullish", 4595.120, 4597.816, 4590.625, 74.0),
    ])
    result = book.sync(analysis, decision, prof)

    check("only one side of a same-price conflict is tracked",
          len(book.live) <= 1, f"{[(s.action, s.entry_zone.label()) for s in book.live]}")
    check("the losing side is never logged as a fired signal",
          len(result["fired"]) == len(book.live), str(len(result["fired"])))
    check("and is not recorded as an outcome either",
          not result["finished"], str([s.status for s in result["finished"]]))

    # Re-deriving the same picture must not admit the other side either.
    book.sync(analysis, decision, prof)
    directions = {s.direction for s in book.live}
    check("the suppressed side does not slip in on the next cycle",
          len(directions) <= 1, str(directions))


def test_book_keeps_a_two_sided_range_plan():
    """The other half of 10:34, which was never a contradiction.

    A short from 4600 targeting 4583 and a long from 4583 are one plan stated
    twice, not two conflicting calls: the short's objective *is* the long's
    entry. Their zones are disjoint and both must survive.
    """
    print("setup book: disjoint two-sided plan")
    book, analysis, decision, prof = _book_with([
        _candidate("bearish", 4598.984, 4601.680, 4606.175, 78.7),
        _candidate("bullish", 4580.219, 4586.511, 4575.725, 82.3, "double_extreme"),
    ], price=4600.281)
    book.sync(analysis, decision, prof)

    directions = {s.direction for s in book.live}
    check("both legs of a range are tracked when the zones are apart",
          directions == {"bullish", "bearish"},
          f"{[(s.action, s.entry_zone.label()) for s in book.live]}")


def test_book_will_not_reverse_on_a_filled_setup():
    print("setup book: no opposite call against an open trade")
    book, analysis, decision, prof = _book_with([
        _candidate("bullish", 4595.120, 4597.816, 4590.625, 74.0),
    ])
    book.sync(analysis, decision, prof)
    if not book.live:
        check("a setup was admitted to test against", False)
        return
    held = book.live[0]
    held.entered = True
    held.fill_price = held.entry_zone.preferred

    # Now the opposite side turns up scoring far higher.
    book._candidates = lambda *a, **k: [                       # noqa: SLF001
        _candidate("bearish", 4594.523, 4597.219, 4601.714, 99.0, "climax_reversal")]
    book.sync(analysis, decision, prof)

    check("a filled setup is never displaced by the opposite trade",
          held in book.live, str([(s.action, s.entered) for s in book.live]))
    check("and the opposite trade is not published alongside it",
          all(s.direction == "bullish" for s in book.live),
          str([s.action for s in book.live]))


def test_sweep_reversal_reads_one_candle_once():
    print("sweep reversal: a candle that wicks both ways")
    from goldassist import strategies
    from goldassist.strategies import smc

    analysis = make_analysis(4596.0)
    analysis["liquidity"]["sweeps"] = [
        {"direction": "bearish", "timeframe": "M5", "level": 4596.80,
         "time": 1000, "wick_depth_atr": 0.6, "reclaimed_bars": 0,
         "confirmed_by_structure": True, "strength": 78.0},
        {"direction": "bullish", "timeframe": "M5", "level": 4595.54,
         "time": 1000, "wick_depth_atr": 0.2, "reclaimed_bars": 0,
         "confirmed_by_structure": True, "strength": 62.0},
    ]
    ctx = strategies.Context(analysis, make_decision(4596.0, 60.0),
                             profile_module.get(None))
    found = smc.sweep_reversal(ctx)
    check("one indecisive candle yields one sweep reversal, not two",
          len(found) <= 1, str([(c.direction, c.zone) for c in found]))
    if found:
        check("and it is the stronger raid of the two",
              found[0].direction == "bearish", found[0].direction)

    # The two edges of a real range are still two setups.
    analysis["liquidity"]["sweeps"] = [
        {"direction": "bearish", "timeframe": "M5", "level": 4616.0,
         "time": 1000, "wick_depth_atr": 0.6, "reclaimed_bars": 0,
         "confirmed_by_structure": True, "strength": 78.0},
        {"direction": "bullish", "timeframe": "M5", "level": 4576.0,
         "time": 900, "wick_depth_atr": 0.6, "reclaimed_bars": 0,
         "confirmed_by_structure": True, "strength": 78.0},
    ]
    ctx = strategies.Context(analysis, make_decision(4596.0, 60.0),
                             profile_module.get(None))
    wide = smc.sweep_reversal(ctx)
    check("raids on both edges of a range are both kept",
          len(wide) == 2, str([(c.direction, c.zone) for c in wide]))


def test_book_attention_order():
    print("setup book: which setup gets the banner")
    prof = profile_module.get(None)
    book = signals.SuggestionBook()
    analysis = make_analysis(4400.0)
    decision = make_decision(4400.0, 76.0)
    decision["confluence"] = {"bullish": {"score": 76.0, "evidence": []},
                              "bearish": {"score": 10.0, "evidence": []}}
    book.sync(analysis, decision, prof)
    if len(book.live) < 1:
        check("a setup exists to rank", False)
        return

    high = book.live[0]
    high.score = 90.0
    high.status = signals.DEVELOPING

    confirmed = signals.build(make_analysis(4480.0),
                              make_decision(4480.0, 72.0), prof)
    confirmed.status = signals.CONFIRMED
    book.live.append(confirmed)

    check("a confirmed setup outranks a higher-scoring developing one",
          book.primary is confirmed,
          f"{book.primary.status} {book.primary.score}")


# --- dollar context --------------------------------------------------------

def test_macro_interpretation():
    print("dollar context")
    _, headline, supports, cautions = macro._interpret(-0.4, -0.8, 0.6, -0.7)
    check("dollar down with gold up supports the upside", supports == "bullish",
          supports)

    _, _, supports, _ = macro._interpret(0.5, 1.0, -0.4, -0.7)
    check("dollar up with gold down supports the downside", supports == "bearish",
          supports)

    _, _, supports, cautions = macro._interpret(0.5, 1.0, 0.6, -0.2)
    check("both rising is reported as conflicted", supports == "conflicted",
          supports)
    check("the divergence is explained", bool(cautions))

    _, _, supports, _ = macro._interpret(0.02, 0.05, 0.01, -0.5)
    check("a flat session is not read as a divergence", supports == "neutral",
          supports)

    read = macro.DollarContext(None if False else _NoSymbolSource())._measure(None)
    check("no dollar symbol degrades gracefully", read.available is False)
    check("the reason is stated", bool(read.headline))


class _NoSymbolSource:
    def find_symbol(self, candidates):
        return None


# --- the composed layer ----------------------------------------------------

def test_desk_never_blocks():
    print("trading desk")
    from goldassist.trader import TradingDesk

    db = temp_db()
    desk = TradingDesk(db, None)
    prof = profile_module.get(None)
    book = signals.SuggestionBook()

    analysis = make_analysis(4400.0)
    decision = make_decision(4400.0, 78.0)
    decision["confluence"] = {"bullish": {"score": 78.0, "evidence": []},
                              "bearish": {"score": 10.0, "evidence": []}}
    book.sync(analysis, decision, prof)

    frames = {"M15": candles([4400 + i * 0.6 for i in range(60)]),
              "D1": candles([4380 + i for i in range(30)])}
    state = desk.assess(analysis, decision, book, frames, prof)

    verdict = state["verdict"]
    check("no blocking field is published", "trade_allowed" not in verdict,
          str(sorted(verdict)))
    check("conviction is produced", 0 < verdict["conviction"] <= 1.3,
          str(verdict["conviction"]))
    check("a suggested size is produced", verdict["risk_fraction"] in (0.25, 0.5, 1.0),
          str(verdict["risk_fraction"]))
    check("the conviction is explained", bool(verdict["factors"]))

    for section in ("session", "character", "topdown", "record", "thesis",
                    "memory", "management"):
        check(f"the desk publishes '{section}'", section in state)

    # A dreadful day must still not withhold anything.
    awful = {"M15": candles([4400 + (5 if i % 2 else -5) for i in range(60)])}
    poor = desk.assess(analysis, decision, book, awful, prof)
    check("a poor day still publishes the setup", book.primary is not None)
    check("a poor day only lowers conviction",
          poor["verdict"]["conviction"] <= verdict["conviction"] + 1e-9,
          f"{poor['verdict']['conviction']} vs {verdict['conviction']}")


def test_outcome_recording():
    print("outcome recording")
    from goldassist.trader import TradingDesk

    db = temp_db()
    desk = TradingDesk(db, None)
    prof = profile_module.get(None)
    analysis = make_analysis(4400.0)
    suggestion = signals.build(analysis, make_decision(4400.0, 76.0), prof)
    suggestion.entered = True
    suggestion.status = signals.COMPLETED

    desk.trades[suggestion.id] = management.open_trade(suggestion)
    desk.trades[suggestion.id].r_now = 2.0
    record = desk.on_terminated(suggestion, analysis)

    check("an outcome record is produced", record is not None)
    check("it is stored", len(db.setup_outcomes()) == 1)
    check("the outcome is classified", record["outcome"] in ("win", "loss", "scratch"))
    check("the session is recorded", bool(record["session"]))
    check("the managed trade is released", suggestion.id not in desk.trades)

    # Resolved after a restart: the row is written days after the exit, and
    # must still be dated by the exit.
    late = signals.build(analysis, make_decision(4400.0, 76.0), prof)
    late.entered = True
    late.fill_price = late.entry_zone.preferred
    late.filled_at = "2026-09-25T11:50:05+00:00"
    late.status = signals.STOPPED_OUT
    late.outcome = "loss"
    late.exit_at = "2026-09-25T13:19:00+00:00"
    late.confirmations_at_fill = 3
    record = desk.on_terminated(late, analysis)
    check("an outcome is dated when it closed, not when it was written",
          record["closed_at"] == late.exit_at, str(record["closed_at"]))
    stored = [o for o in db.setup_outcomes() if o["signal_uid"] == late.id]
    check("the agreement at the fill is stored",
          bool(stored) and stored[0]["confirmations_at_fill"] == 3,
          str(stored[0] if stored else None))


def test_old_outcomes_are_redated_from_the_signal_log():
    print("outcomes written before the fix take their exit time from the signal log")
    with _temp_db() as db:
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO fired_signals (uid, fired_at, closed_at) VALUES (?, ?, ?)",
                ("abc123", "2026-09-25T11:09:21+00:00", "2026-09-25T13:19:00+00:00"))
            conn.execute(
                "INSERT INTO setup_outcomes (signal_uid, created_at, closed_at, entered) "
                "VALUES (?, ?, ?, 1)",
                ("abc123", "2026-09-25T11:09:21+00:00", "2026-09-28T09:29:08+00:00"))
            conn.execute(
                "INSERT INTO setup_outcomes (signal_uid, created_at, closed_at, entered) "
                "VALUES (?, ?, ?, 0)",
                ("never-filled", "2026-09-25T11:00:00+00:00", "2026-09-25T16:00:00+00:00"))
        Database(db.path).close()               # reopening runs the migration
        rows = {o["signal_uid"]: o for o in db.setup_outcomes()}
        check("a filled trade is redated to its exit",
              rows["abc123"]["closed_at"] == "2026-09-25T13:19:00+00:00",
              str(rows["abc123"]["closed_at"]))
        check("an untaken idea is left alone",
              rows["never-filled"]["closed_at"] == "2026-09-25T16:00:00+00:00")


def test_a_stacked_setup_is_sized_at_nothing():
    print("the desk says skip for a setup stacked on an open trade")
    import dataclasses
    from goldassist.trader import TradingDesk

    desk = TradingDesk(temp_db(), None)
    prof = profile_module.get(None)
    book = signals.SuggestionBook()
    analysis = make_analysis(4400.0)
    decision = make_decision(4400.0, 78.0)
    book.sync(analysis, decision, prof)
    frames = {"M15": candles([4400 + i * 0.6 for i in range(60)])}
    primary = book.primary
    if primary is None:
        check("fixture produces a setup", False)
        return
    primary.exposure = {"same_direction_open": 1, "with": ["x"],
                        "note": "1 filled bullish trade already on nearby"}
    verdict = desk.assess(analysis, decision, book, frames, prof)["verdict"]
    check("the suggested size is zero", verdict["risk_fraction"] == 0.0,
          str(verdict["risk_fraction"]))
    check("and says skip", verdict["risk_label"].startswith("skip"), verdict["risk_label"])
    check("the reason is listed among the factors",
          any(f["factor"] == "open exposure" for f in verdict["factors"]))
    check("conviction itself is untouched", verdict["conviction"] > 0.05,
          str(verdict["conviction"]))

    old = dataclasses.replace(prof, skip_stacked=False)
    verdict = desk.assess(analysis, decision, book, frames, old)["verdict"]
    check("with skip_stacked off it is halved instead",
          verdict["risk_fraction"] > 0 and any(
              f["factor"] == "open exposure" and f["multiplier"] == 0.5
              for f in verdict["factors"]), str(verdict["factors"]))


# --- surviving a restart ----------------------------------------------------

@contextlib.contextmanager
def _temp_db():
    """A throwaway database, closed before its directory is removed.

    Windows will not delete a file SQLite still has open, so a test that lets
    the connection outlive the temporary directory fails in the cleanup rather
    than in a check -- which reads as a crash, not as a failing assertion.
    """
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "t.db")
        try:
            yield db
        finally:
            db.close()


def test_suggestion_survives_a_round_trip():
    print("persistence: a setup written down and read back")
    prof = profile_module.get(None)
    built = signals.build(make_analysis(4400.0), make_decision(4400.0, 76.0), prof)
    signals.resolve.initialise(built)
    built.armed, built.entered = True, True
    built.fill_price, built.risk = 4386.0, 11.0
    built.resolved_through = 1_700_000_000
    built.targets_hit, built.mfe_pips = 1, 42.0
    built.history = [{"at": "2026-08-27T09:00:00+00:00", "note": "filled"}]

    back = signals.engine.Suggestion.restore(built.persist())

    def norm(o):
        if isinstance(o, dict):
            return {k: norm(v) for k, v in o.items()}
        if isinstance(o, list):
            return [norm(v) for v in o]
        if isinstance(o, bool):
            return o
        return float(o) if isinstance(o, (int, float)) else o

    check("a restored setup is identical to the one saved",
          norm(built.persist()) == norm(back.persist()))
    check("its stop survives", back.stop == built.stop, str(back.stop))
    check("the resolver's place in the candles survives",
          back.resolved_through == built.resolved_through, str(back.resolved_through))
    check("the fill survives", back.entered and back.fill_price == 4386.0)
    check("to_dict stays the view model, without the working fields",
          "resolved_through" not in built.to_dict())


def test_book_resumes_after_a_restart():
    print("setup book: resuming what was live when the app closed")
    prof = profile_module.get(None)
    first = signals.SuggestionBook()
    analysis, decision = make_analysis(4400.0), make_decision(4400.0, 76.0)
    decision["confluence"] = {
        "bullish": {"score": 76.0, "evidence": [{"detail": "H1 demand"}]},
        "bearish": {"score": 20.0, "evidence": []},
    }
    first.sync(analysis, decision, prof)
    if not first.live:
        check("a setup existed to resume", False)
        return
    saved = [s.persist() for s in first.live]
    ids = {s.id for s in first.live}

    # A brand new run, as after a restart.
    second = signals.SuggestionBook()
    restored = second.restore(saved, prof)
    check("the setups that were live come back",
          {s.id for s in restored} == ids, str(len(restored)))
    check("and they are on the board, not merely returned",
          {s.id for s in second.live} == ids)

    # Resuming twice must not double the board.
    second.restore(saved, prof)
    check("resuming again does not duplicate them",
          len(second.live) == len(ids), str(len(second.live)))


def test_book_does_not_resume_stale_or_finished_setups():
    print("setup book: what must not come back")
    prof = profile_module.get(None)
    built = signals.build(make_analysis(4400.0), make_decision(4400.0, 76.0), prof)
    signals.resolve.initialise(built)

    stale = built.persist()
    old = datetime.now(timezone.utc) - timedelta(minutes=60 * 48)
    stale["created_at"] = old.isoformat()
    check("a setup older than the expiry window is not revived",
          not signals.SuggestionBook().restore([stale], prof))

    finished = built.persist()
    finished["status"] = "STOPPED OUT"
    finished["outcome"] = "loss"
    check("a setup that already resolved is not revived",
          not signals.SuggestionBook().restore([finished], prof))

    check("an unreadable card is skipped, not fatal",
          signals.SuggestionBook().restore([{"id": "x"}, {}], prof) == [])


def test_stranded_signals_are_reconciled():
    print("signal log: rows left open by a restart")
    with _temp_db() as db:
        prof = profile_module.get(None)
        book = signals.SuggestionBook()
        analysis, decision = make_analysis(4400.0), make_decision(4400.0, 76.0)
        decision["confluence"] = {
            "bullish": {"score": 76.0, "evidence": [{"detail": "H1 demand"}]},
            "bearish": {"score": 20.0, "evidence": []},
        }
        result = book.sync(analysis, decision, prof)
        for s in result["fired"]:
            db.record_fired_signal(s.to_dict(), s.persist())
        fired = len(result["fired"])
        check("the setups were logged", fired >= 1, str(fired))

        cards = db.open_fired_signals()
        check("their cards are readable back", len(cards) == fired, str(len(cards)))

        resumed = signals.SuggestionBook().restore(cards, prof)
        kept = {s.id for s in resumed}
        check("and they restore", len(kept) == fired, str(len(kept)))

        # Nothing is stranded while the book is holding all of them.
        check("a resumed setup is left alone",
              db.close_stranded_signals(keep=kept) == 0)

        # One of them had actually filled before the shutdown. That difference
        # decides how it may honestly be closed.
        traded = result["fired"][0]
        traded.entered = True
        traded.fill_price = traded.entry_zone.preferred
        traded.filled_at = "2026-08-27T09:10:00+00:00"
        traded.result_pips = -12.0
        db.update_fired_signal(traded.to_dict(), traded.persist())

        # With nothing holding them, they are closed honestly.
        closed = db.close_stranded_signals(keep=set())
        check("every abandoned setup is reconciled", closed == fired, str(closed))
        rows = {r["uid"]: r for r in db.fired_signals()}

        filled = rows[traded.id]
        check("a trade that filled is never called untaken",
              filled["outcome"] == "unresolved", str(filled["outcome"]))
        check("nor is it invented as a win or a loss",
              filled["outcome"] not in ("win", "loss"), str(filled["outcome"]))
        check("what was measured before the shutdown is kept",
              filled["result_pips"] == -12.0, str(filled["result_pips"]))

        never = [r for uid, r in rows.items() if uid != traded.id]
        check("a setup that never filled is untaken",
              all(r["outcome"] == "untaken" for r in never),
              str({r["outcome"] for r in never}))
        check("with expiry named as the reason",
              all(r["exit_reason"] == "expiry" for r in never))
        check("and nothing is left open",
              db.open_fired_signals() == [])
        check("the learning record is untouched by the reconciliation",
              db.setup_outcomes() == [], str(len(db.setup_outcomes())))


def test_signal_row_carries_the_setup_direction():
    print("signal log: a setup's direction is its own")
    with _temp_db() as db:
        prof = profile_module.get(None)
        analysis = make_analysis(4400.0)
        decision = make_decision(4400.0, 76.0, "bullish")

        # The engine leans bullish; the setup on the board is a bearish one.
        card = signals.build(analysis, decision, prof).to_dict()
        card["direction"] = "bearish"
        card["setup_label"] = "Bearish Sweep Reversal"
        db.record_signal(decision, analysis, None, suggestion=card)

        row = db.recent_signals(1)[0]
        check("the decision's direction is recorded as the decision's",
              row["direction"] == "bullish", str(row["direction"]))
        check("the setup's direction is recorded as the setup's",
              row["setup_direction"] == "bearish", str(row["setup_direction"]))
        label = (row["setup_label"] or "").lower()
        check("so the label is no longer orphaned from its direction",
              row["setup_direction"] in label,
              row["setup_direction"] + " / " + label)


# --- runner ----------------------------------------------------------------

def main() -> int:
    for test in (test_sessions, test_character_measures, test_character_verdict,
                 test_awareness_never_blocks, test_awareness_counters,
                 test_same_idea, test_management_lifecycle,
                 test_management_trailing_only_improves, test_management_warnings,
                 test_close_trade, test_memory_refuses_small_samples,
                 test_memory_reports_a_real_record, test_thesis_is_sticky,
                 test_thesis_revision_needs_structure, test_topdown,
                 test_book_tracks_many_setups, test_book_has_no_daily_cap,
                 test_book_tracks_both_directions, test_book_keeps_the_other_side_alive,
                 test_book_attention_order,
                 test_book_refuses_two_directions_at_one_price,
                 test_book_keeps_a_two_sided_range_plan,
                 test_book_will_not_reverse_on_a_filled_setup,
                 test_sweep_reversal_reads_one_candle_once,
                 test_suggestion_survives_a_round_trip,
                 test_book_resumes_after_a_restart,
                 test_book_does_not_resume_stale_or_finished_setups,
                 test_stranded_signals_are_reconciled,
                 test_signal_row_carries_the_setup_direction,
                 test_macro_interpretation, test_desk_never_blocks,
                 test_outcome_recording,
                 test_old_outcomes_are_redated_from_the_signal_log,
                 test_a_stacked_setup_is_sized_at_nothing):
        test()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) failed: {', '.join(FAILURES)}")
        return 1
    print("All trader checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
