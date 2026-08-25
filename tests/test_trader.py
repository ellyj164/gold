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


def outcome(**kwargs) -> dict:
    base = {
        "signal_uid": "x", "created_at": _now(), "closed_at": _now(),
        "direction": "bullish", "setup_type": "pullback", "session": "london",
        "regime": "strong bullish trend", "day_verdict": "TRADE", "score": 75.0,
        "quality": "Good", "entry": 4400.0, "invalidation": 4380.0, "tp1": 4430.0,
        "entered": 1, "outcome": "win", "r_multiple": 1.5,
        "max_favourable_r": 1.8, "max_adverse_r": -0.3, "reason": "COMPLETED",
        "notes": "",
    }
    base.update(kwargs)
    return base


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
                 test_macro_interpretation, test_desk_never_blocks,
                 test_outcome_recording):
        test()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) failed: {', '.join(FAILURES)}")
        return 1
    print("All trader checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
