"""Checks for signal resolution and the multi-strategy book.

Two things are verified here that nothing else could verify:

1. **A signal is decided and leaves the board.** It fills only after it
   confirms, its targets and stop are read from candles rather than from a
   five-second poll, and when one of them is touched it retires carrying a
   result in pips and R.
2. **Many strategies produce many setups.** One analysis is offered to the
   whole library and every pattern present in it is found, rather than one
   classifier picking a single label.

Run with:  python -m tests.test_resolution
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

import pandas as pd

from goldassist import signals, strategies
from goldassist.decision import profile as profile_module
from goldassist.signals import resolve

from tests.test_signals import make_analysis, make_decision

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        FAILURES.append(name)


# --- fixtures --------------------------------------------------------------

def bars(rows: list[tuple[float, float, float, float]], start: datetime | None = None,
         minutes: int = 1) -> pd.DataFrame:
    """Build an OHLC frame from (open, high, low, close) tuples.

    Bars start just after "now" because resolution deliberately ignores
    candles older than the setup: a target reached before the setup existed is
    not a result.
    """
    start = start or datetime.now(timezone.utc) + timedelta(seconds=30)
    index = [start + timedelta(minutes=i * minutes) for i in range(len(rows))]
    return pd.DataFrame(
        [{"open": o, "high": h, "low": l, "close": c, "volume": 100.0}
         for o, h, l, c in rows],
        index=pd.DatetimeIndex(index, name="time"),
    )


def armed_setup(price: float = 4400.0, score: float = 76.0):
    """A built setup, armed as if it had confirmed in the zone."""
    prof = profile_module.get("Default XAUUSD")
    suggestion = signals.build(make_analysis(price), make_decision(price, score), prof)
    suggestion.armed = True
    return suggestion, prof


# --- resolution ------------------------------------------------------------

def test_fill_requires_confirmation():
    print("a setup fills only after it confirms")
    prof = profile_module.get("Default XAUUSD")
    suggestion = signals.build(make_analysis(4400.0), make_decision(4400.0, 76.0), prof)
    zone = suggestion.entry_zone
    middle = (zone.low + zone.high) / 2

    unconfirmed = signals.build(make_analysis(4400.0), make_decision(4400.0, 76.0), prof)
    unconfirmed.armed = False
    resolve.step(unconfirmed, middle, middle, middle, prof)
    check("price in the zone without confirmation does not fill",
          not unconfirmed.entered)

    suggestion.armed = True
    resolve.step(suggestion, middle, middle, middle, prof)
    check("an armed setup fills when price reaches the zone", suggestion.entered)
    check("the fill price is inside the zone",
          zone.low <= suggestion.fill_price <= zone.high, str(suggestion.fill_price))
    check("risk is measured from the fill", suggestion.risk and suggestion.risk > 0)


def test_target_resolves_as_a_win():
    print("reaching the target books a win")
    suggestion, prof = armed_setup()
    zone = suggestion.entry_zone
    tp1 = suggestion.targets[0].price

    events = resolve.apply_bars(suggestion, bars([
        (4400, 4400, zone.preferred - 0.2, zone.preferred),      # fills
        (zone.preferred, tp1 - 1, zone.preferred - 0.5, tp1 - 2),
        (tp1 - 2, tp1 + 0.5, tp1 - 2, tp1 + 0.2),                # takes TP1
    ]), prof)

    check("the setup is terminal", suggestion.status in signals.TERMINAL_STATES,
          suggestion.status)
    check("it is recorded as TARGET HIT", suggestion.status == signals.TARGET_HIT,
          suggestion.status)
    check("the outcome is a win", suggestion.outcome == "win", str(suggestion.outcome))
    check("the result is at least the 50-pip minimum",
          suggestion.result_pips >= 50.0, f"{suggestion.result_pips} pips")
    check("R is positive", suggestion.r_multiple > 0, str(suggestion.r_multiple))
    check("the exit price is the target", suggestion.exit_price == round(tp1, 3),
          str(suggestion.exit_price))
    check("the fill and the target are both reported",
          any("filled" in e.lower() for e in events)
          and any("TP1" in e for e in events), str(events))


def test_stop_resolves_as_a_loss():
    print("hitting the stop books a loss")
    suggestion, prof = armed_setup()
    zone = suggestion.entry_zone
    stop = suggestion.stop

    resolve.apply_bars(suggestion, bars([
        (4400, 4400, zone.preferred - 0.2, zone.preferred),
        (zone.preferred, zone.preferred, stop - 0.5, stop - 0.2),
    ]), prof)

    check("the setup is terminal", suggestion.status in signals.TERMINAL_STATES,
          suggestion.status)
    check("it is recorded as STOPPED OUT", suggestion.status == signals.STOPPED_OUT,
          suggestion.status)
    check("the outcome is a loss", suggestion.outcome == "loss", str(suggestion.outcome))
    check("the loss is about one R", -1.2 <= suggestion.r_multiple <= -0.8,
          str(suggestion.r_multiple))
    check("pips lost are reported", suggestion.result_pips < 0,
          str(suggestion.result_pips))


def test_wick_between_polls_is_caught():
    print("a wick between two polls is not missed")
    suggestion, prof = armed_setup()
    zone = suggestion.entry_zone
    stop = suggestion.stop

    # The candle dips through the stop and closes well above it. Spot-price
    # polling five seconds apart would very likely never see this.
    resolve.apply_bars(suggestion, bars([
        (4400, 4400, zone.preferred - 0.2, zone.preferred),
        (zone.preferred, zone.preferred + 1, stop - 0.3, zone.preferred + 0.5),
    ]), prof)
    check("the wick through the stop resolved the setup",
          suggestion.status == signals.STOPPED_OUT, suggestion.status)


def test_ambiguous_bar_assumes_the_worst():
    print("a bar holding both stop and target counts as the stop")
    suggestion, prof = armed_setup()
    zone = suggestion.entry_zone
    stop = suggestion.stop
    tp1 = suggestion.targets[0].price

    resolve.apply_bars(suggestion, bars([
        (4400, 4400, zone.preferred - 0.2, zone.preferred),
        (zone.preferred, tp1 + 1, stop - 1, zone.preferred),
    ]), prof)
    check("the stop is assumed to have come first",
          suggestion.status == signals.STOPPED_OUT, suggestion.status)
    check("the outcome is not flattered", suggestion.outcome == "loss",
          str(suggestion.outcome))


def test_unfilled_setup_is_untaken():
    print("an idea that never filled is untaken, not a loss")
    prof = profile_module.get("Default XAUUSD")
    suggestion = signals.build(make_analysis(4400.0), make_decision(4400.0, 76.0), prof)
    stop = suggestion.stop

    # Price collapses straight through without the setup ever confirming.
    resolve.apply_bars(suggestion, bars([(4400, 4400, stop - 2, stop - 1)]), prof)
    check("the setup is terminal", suggestion.status in signals.TERMINAL_STATES,
          suggestion.status)
    check("it is recorded as untaken", suggestion.outcome == "untaken",
          str(suggestion.outcome))
    check("nothing was risked", suggestion.r_multiple == 0.0,
          str(suggestion.r_multiple))


def test_expiry():
    print("stale setups retire")
    prof = profile_module.get("Default XAUUSD")
    suggestion = signals.build(make_analysis(4400.0), make_decision(4400.0, 76.0), prof)
    suggestion.created_at = (datetime.now(timezone.utc)
                             - timedelta(minutes=prof.setup_expiry_minutes + 5)).isoformat()
    event = resolve.check_expiry(suggestion, prof)
    check("an unfilled setup expires", suggestion.status == signals.EXPIRED,
          suggestion.status)
    check("the expiry is explained", bool(event) and "never came" in event, str(event))
    check("an expired setup is untaken", suggestion.outcome == "untaken")


def test_runner_moves_to_breakeven():
    print("a runner protects itself at breakeven")
    prof = profile_module.get("High Conviction")     # closes at TP2
    suggestion = signals.build(make_analysis(4400.0), make_decision(4400.0, 80.0), prof)
    if len(suggestion.targets) < 2:
        check("profile with a runner has two targets", False,
              f"{len(suggestion.targets)} target(s)")
        return
    suggestion.armed = True
    zone = suggestion.entry_zone
    tp1 = suggestion.targets[0].price

    resolve.apply_bars(suggestion, bars([
        (4400, 4400, zone.preferred - 0.2, zone.preferred),
        (zone.preferred, tp1 + 0.2, zone.preferred, tp1),
    ]), prof)
    check("TP1 does not close a runner", suggestion.status not in signals.TERMINAL_STATES,
          suggestion.status)
    check("the stop is now the fill price",
          suggestion.stop == suggestion.fill_price,
          f"{suggestion.stop} vs {suggestion.fill_price}")

    resolve.apply_bars(suggestion, bars([
        (tp1, tp1, suggestion.fill_price - 0.5, suggestion.fill_price - 0.4),
    ], start=datetime.now(timezone.utc) + timedelta(minutes=10)), prof)
    check("coming back to breakeven closes it as a scratch, not a loss",
          suggestion.outcome == "scratch", str(suggestion.outcome))


# --- the book --------------------------------------------------------------

def test_book_clears_and_refills():
    print("the board clears when a setup is decided")
    prof = profile_module.get("Default XAUUSD")
    book = signals.SuggestionBook()
    analysis = make_analysis(4400.0)
    decision = make_decision(4400.0, 76.0)

    book.sync(analysis, decision, prof)
    check("setups are admitted", len(book.live) > 0, str(len(book.live)))
    first = book.primary
    check("each setup names the strategy that found it",
          all(s.strategy and s.strategy_name for s in book.live))

    # Resolve the leading setup as a win.
    first.armed = True
    zone = first.entry_zone
    tp1 = first.targets[0].price
    resolve.apply_bars(first, bars([
        (4400, 4400, zone.preferred - 0.2, zone.preferred),
        (zone.preferred, tp1 + 0.5, zone.preferred, tp1 + 0.2),
    ]), prof)
    check("the setup resolved as a win", first.outcome == "win", str(first.outcome))

    result = book.sync(analysis, decision, prof)
    check("the resolved setup left the board", first not in book.live)
    check("it is in the retired list", first in book.retired)
    check("the caller is told it finished", first in result["finished"])

    results = book.results()
    check("the session record counts the win", results["wins"] >= 1, str(results))
    check("the session record counts the pips", results["pips"] > 0, str(results))

    check("the same idea is not rebuilt immediately",
          not any(s.strategy == first.strategy
                  and s.direction == first.direction
                  and s.entry_zone
                  and not (s.entry_zone.high < zone.low or s.entry_zone.low > zone.high)
                  for s in book.live))


def test_book_merges_agreeing_strategies():
    print("two strategies on one trade merge instead of duplicating")
    prof = profile_module.get("Default XAUUSD")
    book = signals.SuggestionBook()
    book.sync(make_analysis(4400.0), make_decision(4400.0, 76.0), prof)

    merged = [s for s in book.live if s.confirmations]
    check("agreement is recorded as confluence, not as a second signal",
          bool(merged) or len({(s.direction, round(s.entry_zone.low, 1))
                               for s in book.live if s.entry_zone}) == len(book.live),
          f"{len(book.live)} live setups")
    for suggestion in merged:
        check(f"'{suggestion.strategy}' names what agreed with it",
              all(c.get("name") for c in suggestion.confirmations))


def test_entered_setups_survive_a_direction_flip():
    print("a filled trade is not closed by the engine changing its mind")
    prof = profile_module.get("Default XAUUSD")
    book = signals.SuggestionBook()
    analysis = make_analysis(4400.0)
    book.sync(analysis, make_decision(4400.0, 76.0), prof)

    live = book.primary
    live.armed = True
    zone = live.entry_zone
    resolve.step(live, zone.preferred, zone.preferred, zone.preferred, prof)
    check("the setup filled", live.entered)

    # The engine now leads the other way entirely.
    against = make_decision(4400.0, 74.0, "bearish")
    book.sync(analysis, against, prof)
    check("the filled trade is still live", live in book.live, live.status)
    check("only its stop or target can close it",
          live.status not in signals.TERMINAL_STATES, live.status)


# --- the strategy library --------------------------------------------------

def test_library_finds_more_than_one_idea():
    print("the strategy library")
    prof = profile_module.get("Default XAUUSD")
    context = strategies.Context(make_analysis(4400.0),
                                 make_decision(4400.0, 76.0), prof)
    found = strategies.detect_all(context)

    check("more than one strategy is registered", len(strategies.REGISTRY) >= 15,
          str(len(strategies.REGISTRY)))
    check("several strategies see this market", len({c.key for c in found}) >= 3,
          str(sorted({c.key for c in found})))
    check("every candidate names its evidence",
          all(c.evidence for c in found),
          str([c.key for c in found if not c.evidence]))
    check("every candidate has a direction",
          all(c.direction in ("bullish", "bearish") for c in found))
    check("a buy zone never sits above price",
          all(c.zone is None or c.zone[0] <= context.price + context.atr
              for c in found if c.direction == "bullish"))
    check("families are covered",
          len({c.family for c in found}) >= 2, str({c.family for c in found}))

    disabled = strategies.detect_all(context, enabled=("zone_reaction",))
    check("a profile can restrict the library",
          all(c.key == "zone_reaction" for c in disabled),
          str({c.key for c in disabled}))


def test_registry_is_well_formed():
    print("registry hygiene")
    described = strategies.describe_all()
    check("every strategy is described", len(described) == len(strategies.REGISTRY))
    check("each has a description", all(d["description"] for d in described))
    check("each declares its alignment",
          all(d["alignment"] in ("with", "either", "counter") for d in described))
    check("keys are unique", len({d["key"] for d in described}) == len(described))

    # A profile that names a strategy which no longer exists disables nothing
    # visibly -- it just quietly runs a shorter library. The same hazard runs
    # the other way: a profile with an explicit list silently excludes every
    # strategy added after it was written, which is how nine new detectors can
    # ship and never fire for anyone not on the default profile.
    from goldassist.decision import profile as profile_module

    registry = set(strategies.keys())
    for name, prof in profile_module.BUILTIN_PROFILES.items():
        enabled = prof.enabled_strategies
        if enabled is None:
            continue
        unknown = set(enabled) - registry
        check(f"profile '{name}' names only real strategies", not unknown,
              str(unknown))


def main() -> int:
    for test in (test_fill_requires_confirmation, test_target_resolves_as_a_win,
                 test_stop_resolves_as_a_loss, test_wick_between_polls_is_caught,
                 test_ambiguous_bar_assumes_the_worst, test_unfilled_setup_is_untaken,
                 test_expiry, test_runner_moves_to_breakeven,
                 test_book_clears_and_refills, test_book_merges_agreeing_strategies,
                 test_entered_setups_survive_a_direction_flip,
                 test_library_finds_more_than_one_idea, test_registry_is_well_formed):
        test()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) failed: {', '.join(FAILURES)}")
        return 1
    print("All resolution and strategy checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
