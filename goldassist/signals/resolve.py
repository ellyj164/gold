"""Signal resolution: deciding what actually happened to a setup.

The failure this module exists to fix
-------------------------------------

A setup used to sit on the dashboard indefinitely. ``COMPLETED`` required
*every* target to be hit, which on a three-target setup essentially never
happened, so a signal that reached its first objective stayed on the board as
``ACTIVE`` for the rest of the session and blocked nothing, resolved nothing
and was recorded as nothing. A stop being hit fared little better: it was
detected from spot price on a five-second poll, so any move that ran through
the level and came back between two polls was simply never seen.

Resolution here is therefore done the only way it can be done honestly:

* **From candles, not from spot.** Every M1 bar since the last check is walked,
  so a wick that touched the stop at 14:32:10 is found even though the app was
  looking at a price from 14:32:05. The live tick is applied on top as a
  partial bar, so an outcome is never a minute late either.
* **Adverse first inside a bar.** When one bar contains both the stop and the
  target there is no way to know which came first, so the stop is assumed. A
  resolver that guesses in its own favour produces a record that flatters the
  strategy and cannot be learned from.
* **A stop fills where the market was, not where the order was.** A target is a
  limit and fills at its price; a stop is a *market* order and fills at whatever
  is on offer once it triggers. When a bar opens beyond the stop the level was
  never available -- the market gapped over it -- so the fill is the open. See
  :func:`_stop_fill`.
* **Nothing counts before the fill.** A target reached while price never
  traded into the entry zone is not a win -- nobody was in the trade. The same
  setup stopping out before it filled is not a loss; it is an idea that never
  became a trade, recorded as untaken.

The result is a setup that leaves the board the moment it is decided, with a
banked outcome in pips and R, and a slot free for the next opportunity.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd

from . import opportunity
from .states import (COMPLETED, EXPIRED, INVALIDATED, STOPPED_OUT, TARGET_HIT,
                     TERMINAL)

# Terminal reasons, stored on the suggestion and in the outcome record.
TARGET = "target"
STOP = "stop"
BREAKEVEN = "breakeven"
EXPIRY = "expiry"
CANCELLED = "cancelled"
STRUCTURE = "structure"
ROLLOVER = "rollover"
SKIPPED = "skipped"

# Two same-direction trades whose entries sit within this many ATR of each
# other, or that filled within ``EXPOSURE_MINUTES`` of each other, are one
# exposure to one move. Defined here rather than in the book because the
# resolver is what refuses a stacked fill, and the book imports the resolver.
EXPOSURE_ATR = 2.0
EXPOSURE_MINUTES = 90

# Gold's trading day ends at the 17:00 New York rollover: the daily candle
# closes, swap is charged and the spread blows out for the better part of an
# hour. Named in New York time rather than UTC so it moves with US daylight
# saving, exactly as the broker's rollover does.
NEWYORK = ZoneInfo("America/New_York")

# Outcome classes used by the learning layer.
WIN = "win"
LOSS = "loss"
SCRATCH = "scratch"
UNTAKEN = "untaken"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def initialise(suggestion) -> None:
    """Give a freshly built suggestion its working risk levels."""
    stop = (suggestion.invalidation or {}).get("price")
    suggestion.initial_stop = float(stop) if stop is not None else None
    suggestion.stop = suggestion.initial_stop
    suggestion.risk = None
    if suggestion.entry_zone is not None and suggestion.initial_stop is not None:
        suggestion.risk = abs(suggestion.entry_zone.preferred - suggestion.initial_stop) or None


def apply_bars(suggestion, bars, profile) -> list[str]:
    """Walk every candle newer than the last one processed. Returns events.

    ``bars`` is a DataFrame of the finest timeframe available (M1 in the live
    app). Bars are processed oldest first so the sequence of fill, targets and
    stop is the sequence that actually occurred.

    The window is sliced before it is walked rather than filtered inside the
    loop. A caller may hand over a long trailing frame every cycle -- the live
    service does, and so does the backtester -- and re-reading thousands of
    already-resolved candles per setup per tick is the difference between a
    replay that finishes and one that does not.

    **A bar that closed before the setup armed may not fill it.** Arming is
    decided from the analysis at the caller's clock, and the window handed over
    reaches back to the previous cycle -- five seconds live, but a full
    evaluation step in a replay. Without this guard a setup that confirms at
    12:00 can be filled on the 11:05 bar, at a price that was only available
    *before* the evidence that armed it existed. The fill is then better than
    anything the method could actually have obtained, which flatters every
    figure downstream in proportion to how coarsely the replay steps.

    Those earlier bars are still walked -- they can invalidate an idea, and an
    entered setup resolves normally through them -- only the fill is withheld.
    Live behaviour is unchanged: ``apply_price`` runs immediately afterwards
    with the current quote, so a setup that confirms on this cycle still fills
    on this cycle, at the price actually on offer when it confirmed.
    """
    if bars is None or len(bars) == 0 or suggestion.status in TERMINAL:
        return []

    window = _unwalked(suggestion, bars)
    if window is None or window.empty:
        return []

    armed_at = _parse(getattr(suggestion, "armed_at", None))

    events: list[str] = []
    for row in window.itertuples():
        when = row.Index
        events += step(suggestion, float(row.high), float(row.low),
                       float(row.close), profile,
                       when=when.isoformat(),
                       may_fill=_after(when, armed_at),
                       open_=float(row.open))
        suggestion.resolved_through = int(when.timestamp())
        if suggestion.status in TERMINAL:
            break
    return events


def _after(bar_time, armed_at: datetime | None) -> bool:
    """Whether this bar closed strictly after the setup armed.

    An unknown ``armed_at`` returns True, which is the pre-guard behaviour. A
    setup restored from a row written before the column existed carries
    ``armed`` without it, and the alternative reading -- "the arming instant is
    unknown, so refuse every fill" -- turns a missing timestamp into a setup
    that can never enter and never resolve. Degrading to the older, looser
    behaviour is recoverable; degrading to silence is not.

    Tolerant of a naive bar index. Every production frame is UTC-aware, but a
    test fixture or a restored frame need not be, and a comparison that raises
    would take out resolution entirely rather than one fill.
    """
    if armed_at is None:
        return True
    try:
        when = bar_time.to_pydatetime()
    except AttributeError:
        when = bar_time
    if getattr(when, "tzinfo", None) is None:
        armed_at = armed_at.replace(tzinfo=None)
    return when > armed_at


def visited_zone(suggestion, bars) -> bool:
    """Whether price traded into the entry zone in the candles not yet walked.

    Arming used to be judged on spot alone, which is fine at a five-second
    poll and wrong everywhere else: a candle that wicked into the zone and left
    again never armed the setup, so it could not fill, and it eventually
    expired as though price had never come. Reading the unwalked window instead
    means the setup arms when price *reached the entry*, not when it happened
    to still be sitting there at the moment the engine looked.
    """
    zone = suggestion.entry_zone
    if zone is None or bars is None or len(bars) == 0:
        return False
    window = _unwalked(suggestion, bars)
    if window is None or window.empty:
        return False
    return bool(((window["low"] <= zone.high) & (window["high"] >= zone.low)).any())


def apply_price(suggestion, price: float, profile,
                when: str | None = None) -> list[str]:
    """Apply the live quote as a partial bar, so nothing waits for a close.

    ``may_fill`` is True here and must stay so: this is the *current* quote, so
    a setup that armed a moment ago is being offered a price that exists now.
    That is the honest fill, and it is what keeps the guard in ``apply_bars``
    from delaying a live entry by a candle.
    """
    if suggestion.status in TERMINAL:
        return []
    return step(suggestion, price, price, price, profile, when=when or _now(),
                may_fill=True)


def step(suggestion, high: float, low: float, close: float, profile,
         when: str | None = None, may_fill: bool = True,
         open_: float | None = None) -> list[str]:
    """Advance one setup through one bar of price action.

    ``may_fill`` False walks the bar for everything except the entry: the setup
    can still be invalidated, and an already-entered one still resolves. It is
    how a bar that predates the confirmation is prevented from granting a fill
    that was never on offer -- see :func:`apply_bars`.

    ``open_`` is the bar's opening price, and only the stop reads it: a bar that
    opened beyond the stop gapped over it, and the fill is the open rather than
    the level. It is optional because a live quote applied by
    :func:`apply_price` is a single price with no bar to open, and there is no
    gap to detect in it.
    """
    events: list[str] = []
    when = when or _now()
    bullish = suggestion.direction == "bullish"
    zone = suggestion.entry_zone
    moment = _parse(when)

    # -- the day ends at the rollover ---------------------------------------
    #
    # This is an intraday method: a trade still open at the end of Gold's
    # trading day is closed at the price on offer just before the rollover,
    # rather than carried through the spread blowout and the swap charge into
    # a session its evidence was never about. ``trade_expiry_minutes`` could
    # not express this -- 720 minutes from a 16:00 UTC fill is 04:00 the next
    # morning.
    if suggestion.entered and moment is not None:
        deadline = rollover_deadline(profile, _parse(suggestion.filled_at))
        if deadline is not None and moment >= deadline:
            exit_at = open_ if open_ is not None else close
            _close(suggestion, float(exit_at), ROLLOVER, when)
            suggestion.exit_note = "closed flat before the New York rollover"
            events.append(f"Closed at {exit_at:.2f} before the rollover — "
                          "an intraday trade is not carried into the next day")
            return events

    # -- the fill -----------------------------------------------------------
    #
    # Only an armed setup fills. Arming happens in the lifecycle when price is
    # in the zone *and* the trigger is met, which is the method's own rule: an
    # entry zone touched by price that never confirmed is not a trade.
    #
    # Nor does anything fill across the rollover itself. A fill there would
    # either be closed within minutes by the rule above or pay the widest
    # spread of the day to enter, and neither is a trade this method takes.
    if (not suggestion.entered and moment is not None
            and in_rollover_window(profile, moment)):
        may_fill = False

    # -- no second bet on one move --------------------------------------------
    #
    # A setup that would fill while a same-direction trade on the same move is
    # still open is skipped, not sized down. Measured at the fill, stacked
    # trades returned -0.12R against +0.21R standalone over 683 backtest trades
    # (Mar-Sep 2026), and -0.18R against +0.18R live after v1.8; halving them
    # still left them losing. Checked bar by bar against when each peer filled
    # and closed, so a replay step that walks many candles cannot block a fill
    # on a trade that was not yet open, or no longer open, at that candle.
    if (not suggestion.entered and suggestion.armed and may_fill
            and getattr(profile, "skip_stacked", True) and moment is not None
            and (zone is None or (low <= zone.high and high >= zone.low))):
        reference = float(zone.preferred) if zone is not None else close
        blocker = stacked_on(suggestion, reference, moment)
        if blocker is not None:
            _close(suggestion, None, SKIPPED, when)
            reason = (f"a {suggestion.direction} trade on the same move was "
                      f"already open (filled at {float(blocker.fill_price):.2f})")
            suggestion.exit_note = "skipped — " + reason
            events.append("Skipped at the entry — " + reason)
            return events

    if not suggestion.entered:
        if suggestion.armed and not may_fill:
            # Armed, but this bar closed before the confirmation did. It may
            # not fill; it may still invalidate.
            if suggestion.stop is not None:
                breached = low <= suggestion.stop if bullish else high >= suggestion.stop
                if breached:
                    _close(suggestion, float(suggestion.stop), STRUCTURE, when)
                    events.append("Invalidated before the entry was ever offered")
            return events
        if suggestion.armed and zone is None:
            # No zone could be derived, so the card offers entry at market and
            # the fill is the price at the moment it confirmed.
            _fill(suggestion, close, when)
            events.append(f"Entry filled at market {close:.2f}")
        elif suggestion.armed and zone is not None and low <= zone.high and high >= zone.low:
            fill = _entry_fill(zone, bullish, high, low, open_)
            _fill(suggestion, fill, when)
            events.append(f"Entry filled at {fill:.2f}")
        elif suggestion.stop is not None:
            # Price ran past the invalidation without ever offering the entry.
            breached = low <= suggestion.stop if bullish else high >= suggestion.stop
            if breached:
                _close(suggestion, float(suggestion.stop), STRUCTURE, when)
                events.append("Invalidated before the entry was ever offered")
                return events
        else:
            return events

    if not suggestion.entered:
        return events

    # -- excursions ---------------------------------------------------------
    favourable = high if bullish else low
    adverse = low if bullish else high
    fill = float(suggestion.fill_price)
    gained = (favourable - fill) if bullish else (fill - favourable)
    lost = (fill - adverse) if bullish else (adverse - fill)
    if gained > 0:
        suggestion.mfe_pips = max(suggestion.mfe_pips, opportunity.to_pips(gained))
    if lost > 0:
        suggestion.mae_pips = max(suggestion.mae_pips, opportunity.to_pips(lost))

    # -- the stop, checked before the targets -------------------------------
    if suggestion.stop is not None:
        hit = adverse <= suggestion.stop if bullish else adverse >= suggestion.stop
        if hit:
            reason = BREAKEVEN if _at_breakeven(suggestion) else STOP
            filled = _stop_fill(float(suggestion.stop), open_, bullish)
            _close(suggestion, filled, reason, when)
            slipped = abs(filled - float(suggestion.stop))
            if reason == BREAKEVEN:
                events.append("Closed at breakeven %.2f" % filled)
            elif slipped > 1e-9:
                events.append(
                    f"Stop at {suggestion.stop:.2f} gapped over — filled at "
                    f"{filled:.2f}, {opportunity.to_pips(slipped):.0f} pips worse")
            else:
                events.append("Stop hit at %.2f" % filled)
            return events

    # -- the targets --------------------------------------------------------
    close_index = max(1, int(getattr(profile, "close_at_target", 1)))
    for position, target in enumerate(suggestion.targets, start=1):
        if target.hit:
            continue
        reached = favourable >= target.price if bullish else favourable <= target.price
        if not reached:
            break            # targets are ordered; the rest are further away
        target.hit = True
        suggestion.targets_hit = position
        events.append(f"{target.level} reached at {target.price:.2f} "
                      f"({target.pips:.0f} pips)")

        if position >= close_index or position >= len(suggestion.targets):
            _close(suggestion, float(target.price), TARGET, when)
            return events

        # Runner: the risk comes off the trade the moment it has paid.
        if getattr(profile, "breakeven_after_target", True) and suggestion.fill_price:
            if suggestion.stop != suggestion.fill_price:
                suggestion.stop = float(suggestion.fill_price)
                events.append(f"Stop moved to breakeven at {suggestion.fill_price:.2f}")

    # -- breakeven before the target, when the profile records it ------------
    #
    # The desk has always *advised* moving the stop to breakeven at
    # ``breakeven_at_r``, but the record never did it, so the record could not
    # say whether the advice pays. Off by default: it is a hypothesis to be
    # backtested, not a rule to adopt because three losers once went +1R first.
    #
    # Applied after this bar's stop and target have been checked, so it takes
    # effect from the next bar. Moving the stop on the same bar that produced
    # the excursion would assume the favourable move came before the adverse
    # one -- the ordering this resolver refuses to guess in its own favour.
    if (getattr(profile, "breakeven_in_record", False) and suggestion.risk
            and suggestion.fill_price is not None and suggestion.stop is not None
            and not _at_breakeven(suggestion)):
        trigger = float(getattr(profile, "breakeven_at_r", 1.0) or 0.0)
        if trigger > 0 and gained >= trigger * float(suggestion.risk):
            suggestion.stop = float(suggestion.fill_price)
            events.append(f"Stop moved to breakeven at {suggestion.fill_price:.2f} "
                          f"after {trigger:.1f}R in favour")

    return events


def check_expiry(suggestion, profile, now: datetime | None = None) -> str | None:
    """Retire a setup that has run out of time. Returns an event, or None."""
    if suggestion.status in TERMINAL:
        return None
    now = now or datetime.now(timezone.utc)

    if not suggestion.entered:
        limit = float(getattr(profile, "setup_expiry_minutes", 240) or 0)
        if limit <= 0:
            return None
        age = _minutes_since(suggestion.created_at, now)
        if age is not None and age >= limit:
            _close(suggestion, None, EXPIRY, now.isoformat())
            return (f"Setup expired unfilled after {age:.0f} minutes — "
                    "price never came to the entry zone")
        return None

    limit = float(getattr(profile, "trade_expiry_minutes", 720) or 0)
    if limit <= 0:
        return None
    age = _minutes_since(suggestion.filled_at or suggestion.created_at, now)
    if age is not None and age >= limit:
        _close(suggestion, float(suggestion.price), EXPIRY, now.isoformat())
        return (f"Closed on time after {age:.0f} minutes — the trade stopped "
                "going anywhere")
    return None


def cancel(suggestion, reason: str) -> str:
    """Retire an unfilled setup whose premise has gone."""
    _close(suggestion, None, CANCELLED, _now())
    suggestion.exit_note = reason
    return f"Setup cancelled — {reason}"


# --- the trading day ----------------------------------------------------------

# The rollover, New York wall clock.
ROLLOVER_TIME = time(17, 0)


def _rollover_settings(profile) -> tuple[bool, float, float]:
    enabled = bool(getattr(profile, "flat_before_rollover", True))
    before = float(getattr(profile, "flat_minutes_before_rollover", 5) or 0)
    quiet = float(getattr(profile, "rollover_quiet_minutes", 60) or 0)
    return enabled, before, quiet


def _rollovers_around(moment: datetime):
    """The rollover instants on the New York days either side of ``moment``.

    Built with ``datetime.combine`` in the New York zone rather than by adding
    24 hours to a UTC instant, so a daylight-saving change between the two days
    moves the rollover by the hour it really moves.
    """
    local = moment.astimezone(NEWYORK).date()
    for offset in (-1, 0, 1):
        day = local + timedelta(days=offset)
        yield datetime.combine(day, ROLLOVER_TIME, tzinfo=NEWYORK).astimezone(timezone.utc)


def rollover_deadline(profile, filled_at: datetime | None) -> datetime | None:
    """When a trade filled at ``filled_at`` must be flat: just before the next
    rollover. None when the profile does not close trades at the rollover or
    the fill time is unknown."""
    enabled, before, _ = _rollover_settings(profile)
    if not enabled or filled_at is None:
        return None
    for roll in _rollovers_around(filled_at):
        deadline = roll - timedelta(minutes=before)
        if deadline > filled_at:
            return deadline
    return None


def in_rollover_window(profile, moment: datetime) -> bool:
    """Whether ``moment`` falls between the flat deadline and the end of the
    post-rollover spread blowout -- the stretch in which nothing may fill."""
    enabled, before, quiet = _rollover_settings(profile)
    if not enabled:
        return False
    for roll in _rollovers_around(moment):
        if roll - timedelta(minutes=before) <= moment < roll + timedelta(minutes=quiet):
            return True
    return False


# --- exposure -----------------------------------------------------------------


def stacked_on(suggestion, price: float, moment: datetime):
    """The open same-direction trade a fill here would stack on, or None.

    ``suggestion.peers`` is handed over by the setup book each cycle: every
    other setup it holds, filled or not, so a peer that fills earlier in the
    same cycle is seen too. A peer counts only while it is actually open at
    ``moment`` -- filled at or before it, not yet closed -- and only when it is
    the same move: entries within ``EXPOSURE_ATR`` ATR, or filled within
    ``EXPOSURE_MINUTES``.
    """
    atr = float(getattr(suggestion, "peer_atr", 0.0) or 0.0)
    for peer in getattr(suggestion, "peers", None) or ():
        if (peer is suggestion or not peer.entered
                or peer.direction != suggestion.direction
                or peer.fill_price is None):
            continue
        filled = _parse(peer.filled_at)
        if filled is None or filled > moment:
            continue
        closed = _parse(peer.exit_at) if peer.outcome else None
        if closed is not None and closed <= moment:
            continue
        if atr > 0 and abs(float(peer.fill_price) - price) <= EXPOSURE_ATR * atr:
            return peer
        if (moment - filled).total_seconds() <= EXPOSURE_MINUTES * 60:
            return peer
    return None


# --- internals --------------------------------------------------------------


def _entry_fill(zone, bullish: bool, high: float, low: float,
                open_: float | None) -> float:
    """The price an armed setup actually enters at on this bar.

    A resting order at the preferred price fills at the preferred price, and
    only when the bar reaches it. It is never credited a better price when the
    bar trades through: a limit that fills at its level is the conservative
    reading, and anything else is optimism.

    When the bar enters the zone *without* reaching the preferred price, the
    entry is a market order taken on confirmation, and it gets the first
    in-zone price the bar offered: its open, if the bar opened inside the zone,
    otherwise the edge price crossed on the way in. This replaced taking the
    bar's low for a long and its high for a short -- the single best price in
    the bar, which no order could count on and which a live fill never got.
    Measured on the live record, real fills came in 0.13R worse than the
    preferred price on average; the old rule came in at or better than it on
    every bar, and shrank the risk the result is divided by into the bargain.

    A live quote is a single price with no bar to open (``open_`` None): that
    price is what is on offer, clamped into the zone.
    """
    preferred = float(zone.preferred)
    reached = low <= preferred if bullish else high >= preferred
    if reached:
        return preferred
    if open_ is None:
        quote = low if bullish else high
        return min(max(quote, zone.low), zone.high)
    if zone.low <= open_ <= zone.high:
        return float(open_)
    return float(zone.high if bullish else zone.low)


def _fill(suggestion, price: float, when: str) -> None:
    suggestion.entered = True
    suggestion.fill_price = round(float(price), 3)
    suggestion.filled_at = when
    # Agreement keeps arriving after the fill, and more of it reaches winners,
    # which simply stay on the board longer: live, winners gained 2.6 further
    # confirmations after filling against 1.1 for losers. Only the count at
    # this moment is something the trader could have acted on.
    suggestion.confirmations_at_fill = len(suggestion.confirmations)
    if suggestion.stop is not None:
        suggestion.risk = abs(suggestion.fill_price - float(suggestion.stop)) or None


def _close(suggestion, exit_price: float | None, reason: str, when: str) -> None:
    """Record the terminal state, the outcome and what it was worth."""
    suggestion.exit_reason = reason
    suggestion.exit_at = when

    if not suggestion.entered:
        suggestion.outcome = UNTAKEN
        suggestion.result_pips = 0.0
        suggestion.r_multiple = 0.0
        suggestion.exit_price = round(float(exit_price), 3) if exit_price else None
        suggestion.status = (EXPIRED if reason in (EXPIRY, CANCELLED, SKIPPED)
                             else INVALIDATED)
        return

    price = float(exit_price if exit_price is not None else suggestion.price)
    suggestion.exit_price = round(price, 3)
    fill = float(suggestion.fill_price)
    signed = (price - fill) if suggestion.direction == "bullish" else (fill - price)
    suggestion.result_pips = round(opportunity.to_pips(signed) * (1 if signed >= 0 else -1), 1)

    risk = suggestion.risk or abs(fill - float(suggestion.initial_stop or fill)) or None
    suggestion.r_multiple = round(signed / risk, 2) if risk else 0.0

    # The same trade after the broker has been paid.
    #
    # One full round turn is charged whichever way the trade ran: a long buys
    # at the ask and sells at the bid, a short does the reverse, and the candle
    # series these prices came from is a bid series either way. Charging it
    # against the result rather than shifting the fill and exit prices keeps
    # those two meaning what they say -- the prices on the chart where the
    # trade opened and closed -- and confines the cost to the figures that are
    # meant to be net.
    #
    # With no cost model the net figures equal the gross ones, which is the
    # honest reading of "costs were not measured" and keeps every stored result
    # from before this change comparable with the ones after it.
    costs = getattr(suggestion, "costs", None)
    if costs is not None and costs.available:
        suggestion.cost_pips = round(costs.round_turn_pips, 1)
        net_signed = costs.net_price_move(signed)
        suggestion.net_result_pips = round(
            opportunity.to_pips(net_signed) * (1 if net_signed >= 0 else -1), 1)
        # Net R is measured against the *same* risk the gross figure uses --
        # the initial stop distance -- and not against a risk inflated by the
        # round turn.
        #
        # Inflating both sides is arithmetically defensible and useless: a
        # stop-out then comes to exactly -1.00R net as well as gross, because
        # numerator and denominator grow together, and the cost vanishes from
        # the one number the statistics are built on. Keeping the denominator
        # fixed is also the convention that matches how the position was
        # actually sized -- risk is set from the stop distance on the card --
        # so a stop-out costing a round turn more than it promised reads as
        # -1.10R, which is what the account experienced.
        suggestion.net_r_multiple = round(net_signed / risk, 2) if risk else 0.0
    else:
        suggestion.cost_pips = 0.0
        suggestion.net_result_pips = suggestion.result_pips
        suggestion.net_r_multiple = suggestion.r_multiple

    if reason == TARGET:
        suggestion.outcome = WIN
        suggestion.status = (COMPLETED if all(t.hit for t in suggestion.targets)
                             else TARGET_HIT)
        return
    if reason in (STOP, BREAKEVEN):
        suggestion.status = STOPPED_OUT
        suggestion.outcome = (SCRATCH if reason == BREAKEVEN or abs(suggestion.r_multiple) < 0.2
                              else LOSS)
        return

    # Time exit: whatever it was worth at the moment it was closed.
    suggestion.status = STOPPED_OUT if suggestion.r_multiple < 0 else TARGET_HIT
    suggestion.outcome = (WIN if suggestion.r_multiple >= 0.2
                          else LOSS if suggestion.r_multiple <= -0.2 else SCRATCH)


def _stop_fill(stop: float, open_: float | None, bullish: bool) -> float:
    """Where a stop actually fills, given where the bar opened.

    A stop is a *market* order. Reaching the level triggers it; the fill is
    whatever the book offers next, which is the level only when price traded
    through it continuously. This resolver used to return the level
    unconditionally, and that is optimistic in exactly one direction: it lands
    entirely on the loss side of every result, because a stop is the only order
    here that is not a limit.

    When the bar *opens* beyond the stop, the level was never on offer -- the
    market gapped over it and the first available price is the open. Gold does
    this at the Sunday reopen, over the weekend, and through high-impact
    releases.

    A favourable gap is deliberately not modelled, because there is nothing to
    model: a stop only triggers when price reaches it adversely, so a gap the
    other way means it was never hit at all. Targets keep filling at their own
    price for the same reason in reverse -- a limit that gaps through would fill
    *better*, and crediting that would put the optimism back on the other side.

    ``open_`` of None returns the level unchanged, which is the honest answer
    for a live quote: one price is not a bar and carries no gap to detect.
    """
    stop = float(stop)
    if open_ is None:
        return stop
    open_ = float(open_)
    gapped_through = open_ <= stop if bullish else open_ >= stop
    return open_ if gapped_through else stop


def _at_breakeven(suggestion) -> bool:
    if suggestion.stop is None or suggestion.fill_price is None:
        return False
    return abs(float(suggestion.stop) - float(suggestion.fill_price)) < 1e-6


def _unwalked(suggestion, bars):
    """The slice of ``bars`` this setup has not seen yet.

    Sliced by timestamp rather than filtered inside a loop: callers hand over a
    long trailing frame every cycle -- the live service does, and so does the
    backtester -- and re-reading thousands of already-resolved candles per
    setup per tick is the difference between a replay that finishes and one
    that does not.
    """
    if bars is None or len(bars) == 0:
        return None
    since = suggestion.resolved_through or _created_epoch(suggestion)
    if not since:
        return bars
    cutoff = pd.Timestamp(since, unit="s", tz="UTC")
    if bars.index.tz is None:
        cutoff = cutoff.tz_localize(None)
    start = int(bars.index.searchsorted(cutoff, side="right"))
    return bars.iloc[start:] if start < len(bars) else None


def _created_epoch(suggestion) -> int:
    stamp = _parse(suggestion.created_at)
    return int(stamp.timestamp()) if stamp else 0


def _minutes_since(value: str | None, now: datetime) -> float | None:
    stamp = _parse(value)
    if stamp is None:
        return None
    return (now - stamp).total_seconds() / 60.0


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        stamp = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)
