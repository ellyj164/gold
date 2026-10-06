"""Auction strategies: trading against the day's own reference prices.

The rest of the library finds setups in price *shape* -- swings, blocks, gaps,
ranges. These three find them against the references the day itself creates,
which is how an institutional intraday desk frames the session:

``vwap_reversion``   fading a band excursion back toward the day's fair value
``vwap_reclaim``     the continuation trade when price takes VWAP back
``ib_extension``     the trend-day trade once the initial balance gives way

Why these are worth having when the library already has twenty-six detectors
---------------------------------------------------------------------------

Because none of the existing ones reference VWAP at all, and VWAP is the line
the other side of the trade is being managed against. A desk filling a large
order measures itself against the volume-weighted average price; that is not a
theory about what the market *should* do, it is a description of what a large
participant is actually rewarded for. Price reliably reacts around it for the
same reason it reacts at a prior-day high: enough people care.

They also fire often, which for an intraday method is the point. A band
excursion, a reclaim and an initial-balance break are each available most
sessions and in both directions, so on a day when no order block has mitigated
and no sweep has printed there is still something measurable to trade.

The regime gate that is deliberately not a gate
-----------------------------------------------

``vwap_reversion`` and ``vwap_reclaim`` are opposite trades and both are
correct -- in different regimes. The obvious design is to let
``analysis/persistence`` decide which one may fire. That would be a mistake,
and the same one the standing rule about conviction warns against: the regime
estimate is itself uncertain, it changes within a session, and a detector
silenced by it produces nothing on precisely the days the estimate is wrong.

So the regime *sizes* these candidates and never suppresses them. A reversion
setup in a strongly trending market still appears, with its conviction cut and
the reason stated. The trader decides.
"""

from __future__ import annotations

from . import base
from .base import Candidate, Context, ZoneHint, RANGE, TREND, EITHER, COUNTER

PIP = 0.01

# How far from the anchor, in standard deviations, before an excursion is worth
# fading. Below this price is simply somewhere inside the day's normal range
# and there is no edge in calling it stretched.
REVERSION_SIGMA = 1.6

# The excursion must not be so extreme that it is a trend day riding the band.
# Beyond this the move is not stretched, it is directional, and fading it is
# the most expensive trade on the board.
REVERSION_SIGMA_MAX = 3.2

# Bars examined for a VWAP cross. On M5 this is an hour, which is long enough
# that a genuine reclaim is caught and short enough that a cross from this
# morning is not still being traded at the close.
CROSS_LOOKBACK = 12

# How close to the anchor price must be, in standard deviations, to count as a
# retest of it after a reclaim.
RECLAIM_TOLERANCE = 0.45

# Minimum first objective, matching the intraday floor the rest of the
# application applies: a day trade needs 500 pips ($5) of room to be worth the
# spread and the attention.
MIN_OBJECTIVE_PIPS = 500.0

# Bars an anchor needs before its standard deviation is worth trading against.
# On M5 this is four hours. Gold reopens at 22:00 UTC on a Sunday, so without
# this the first hour of every week offers a two-sigma "excursion" measured
# from a dozen candles.
MIN_ANCHOR_BARS = 48


def _regime_weight(ctx: Context, wants_persistence: bool) -> tuple[float, str]:
    """How much the statistical regime supports this kind of trade.

    Returns a 0-1 factor and the sentence explaining it. An unmeasured regime
    returns a neutral 0.6 rather than 0 -- absence of evidence must not read as
    evidence against.
    """
    persistent = ctx.persistent()
    if persistent is None:
        return 0.6, "the statistical regime is undecided, so it neither helps nor hurts"
    if persistent == wants_persistence:
        return 1.0, ("the market is statistically trending, which is this setup's regime"
                     if persistent else
                     "the market is statistically mean-reverting, which is this setup's regime")
    return 0.25, ("the market is statistically trending, so a fade here is against "
                  "the measured regime — shown, but sized down"
                  if persistent else
                  "the market is statistically mean-reverting, so a continuation "
                  "here is against the measured regime — shown, but sized down")


# --- fading a band excursion ------------------------------------------------


@base.strategy("vwap_band_fade", "VWAP Band Fade", RANGE,
               alignment=COUNTER, min_conviction=52)
def vwap_band_fade(ctx: Context) -> list[Candidate]:
    """Price stretched to a VWAP band with the anchor flat: fade back to fair value.

    Distinct from ``trend.vwap_reversion``, which measures the same idea by a
    different instrument: that one fades a stretch measured in ATR from the
    *indicator* VWAP, this one fades an excursion measured in standard
    deviations from the *anchored* day VWAP, and only when the anchor is flat.
    They will often agree, and when they do the book merges them into one setup
    with both arguments attached — which is what confluence means and is a
    better outcome than either detector having been deleted in favour of the
    other.
    """
    anchor = ctx.anchor("day")
    if anchor is None:
        return []

    deviation = float(anchor.get("deviation") or 0.0)
    sigma = float(anchor.get("distance_sigma") or 0.0)
    vwap_price = float(anchor.get("vwap") or 0.0)
    if deviation <= 0 or not vwap_price:
        return []
    if not (REVERSION_SIGMA <= abs(sigma) <= REVERSION_SIGMA_MAX):
        return []

    # A barely-formed anchor has an unreliable deviation, and this whole setup
    # is a claim about a deviation. Sunday's 22:00 open is the case that found
    # this: an hour into the session the day anchor holds a dozen bars, the
    # band is meaningless, and price is trivially two sigma from it. The bar
    # floor is higher here than ``vwap.MIN_BARS`` because a reference that is
    # merely reportable is not automatically tradeable.
    if int(anchor.get("bars") or 0) < MIN_ANCHOR_BARS:
        return []

    # A sloping anchor means price is riding the band, not stretched from it.
    # This is the single most important condition in the detector.
    #
    # "forming" is treated as sloping, not as flat: it means the slope could
    # not be measured, and an unmeasured slope must not be read as the
    # favourable case. The fade needs a *known* flat anchor.
    slope = anchor.get("slope", "flat")
    stretched_with_slope = ((sigma > 0 and slope in ("rising", "forming"))
                            or (sigma < 0 and slope in ("falling", "forming")))

    direction = "bearish" if sigma > 0 else "bullish"
    band_price = float(ctx.price)
    objective = abs(band_price - vwap_price)
    if objective / PIP < MIN_OBJECTIVE_PIPS:
        return []

    regime_factor, regime_note = _regime_weight(ctx, wants_persistence=False)
    rejection = bool(ctx.patterns("M5", direction) or ctx.patterns("M15", direction))
    shape = ctx.day_shape()
    rotation = "rotation" in shape or "balanced" in shape

    conviction = base.mix(
        (0.26, base.clamp((abs(sigma) - REVERSION_SIGMA) / 1.4, 0.0, 1.0)),
        (0.24, 0.15 if stretched_with_slope else 1.0),
        (0.20, regime_factor),
        (0.16, 1.0 if rejection else 0.45),
        (0.14, 1.0 if rotation else 0.4),
    )

    tolerance = deviation * 0.22
    if direction == "bearish":
        low, high = band_price - tolerance, band_price + tolerance * 0.6
    else:
        low, high = band_price - tolerance * 0.6, band_price + tolerance

    evidence = [
        f"Price is {anchor['position']} at {sigma:+.2f} sigma from the day's "
        f"VWAP ({vwap_price:.2f})",
        f"The day's anchor is {slope}",
        regime_note,
    ]
    if rejection:
        evidence.append(f"A {direction} rejection candle has printed at the band")
    if stretched_with_slope:
        evidence.append(
            "The anchor is sloping the same way as the excursion, which is how a "
            "trend day looks — this is the weakest version of this setup")

    return [Candidate(
        key="vwap_band_fade", name="VWAP Band Fade", family=RANGE,
        direction=direction, trigger_family="range",
        label=base.label_for("VWAP Reversion", direction),
        conviction=conviction, alignment=COUNTER,
        zone=(low, high), preferred=(low + high) / 2,
        stop=base.stop_beyond(band_price, direction,
                              deviation * 0.5 + ctx.atr * 0.25),
        stop_basis="beyond the VWAP band the excursion reached",
        targets=[
            {"price": vwap_price, "basis": "the day's VWAP — fair value"},
            {"price": (vwap_price - deviation if direction == "bearish"
                       else vwap_price + deviation),
             "basis": "the opposite first band"},
        ],
        sources=[ZoneHint("vwap band", anchor.get("bars") and "day" or "day",
                          low, high, conviction,
                          f"day VWAP band at {band_price:.2f}")],
        evidence=evidence,
        note=("Fading an excursion back to the day's volume-weighted average. "
              "Valid while the anchor is flat; a sloping anchor turns this into "
              "a trend day and the same touch becomes a continuation."),
    )]


# --- taking the anchor back -------------------------------------------------


def _crossed(ctx: Context, vwap_price: float) -> str | None:
    """Which way price has just crossed the anchor, if it has.

    Read from the candles rather than from a stored previous value: the
    application is restarted daily and a crossing detector that depends on
    remembering the last cycle silently reports nothing for the first hour
    after every launch.
    """
    timeframe = ctx.vwap.get("timeframe") or "M5"
    frame = ctx.frame(timeframe)
    if frame is None or len(frame) < CROSS_LOOKBACK + 2:
        return None
    window = frame.tail(CROSS_LOOKBACK)
    closes = window["close"].to_numpy(dtype=float)
    above = closes > vwap_price
    if above[-1] and not above[0] and not above[:CROSS_LOOKBACK // 2].all():
        return "bullish"
    if not above[-1] and above[0] and above[:CROSS_LOOKBACK // 2].any():
        return "bearish"
    return None


@base.strategy("vwap_reclaim", "VWAP Reclaim", TREND,
               alignment=EITHER, min_conviction=53)
def vwap_reclaim(ctx: Context) -> list[Candidate]:
    """Price takes the day's VWAP back and holds it: continuation from fair value."""
    anchor = ctx.anchor("day")
    if anchor is None:
        return []

    deviation = float(anchor.get("deviation") or 0.0)
    vwap_price = float(anchor.get("vwap") or 0.0)
    if deviation <= 0 or not vwap_price:
        return []

    direction = _crossed(ctx, vwap_price)
    if direction is None:
        return []

    # The trade is the retest of the anchor, so price must still be near it.
    sigma = abs(float(anchor.get("distance_sigma") or 0.0))
    if sigma > RECLAIM_TOLERANCE * 3:
        return []

    band = deviation * 2.0
    target = vwap_price + band if direction == "bullish" else vwap_price - band
    if abs(target - ctx.price) / PIP < MIN_OBJECTIVE_PIPS:
        return []

    regime_factor, regime_note = _regime_weight(ctx, wants_persistence=True)
    slope = anchor.get("slope", "flat")
    slope_agrees = ((direction == "bullish" and slope == "rising")
                    or (direction == "bearish" and slope == "falling"))
    session = ctx.anchor("session")
    session_agrees = bool(
        session is not None
        and ((direction == "bullish" and float(session.get("distance_sigma") or 0) > 0)
             or (direction == "bearish" and float(session.get("distance_sigma") or 0) < 0)))
    htf = ctx.htf_agrees(direction)

    conviction = base.mix(
        (0.24, regime_factor),
        (0.22, 1.0 if slope_agrees else 0.45),
        (0.20, 1.0 if htf else 0.5),
        (0.18, 1.0 if session_agrees else 0.5),
        (0.16, base.clamp(1.0 - sigma / (RECLAIM_TOLERANCE * 3), 0.0, 1.0)),
    )

    tolerance = deviation * RECLAIM_TOLERANCE
    low, high = vwap_price - tolerance, vwap_price + tolerance

    evidence = [
        f"Price has crossed back {'above' if direction == 'bullish' else 'below'} "
        f"the day's VWAP at {vwap_price:.2f} within the last {CROSS_LOOKBACK} candles",
        f"The anchor is {slope}",
        regime_note,
    ]
    if session_agrees:
        evidence.append("The session anchor agrees with the reclaim")
    if htf:
        evidence.append("The higher timeframes point the same way")

    return [Candidate(
        key="vwap_reclaim", name="VWAP Reclaim", family=TREND,
        direction=direction, trigger_family="continuation",
        label=base.label_for("VWAP Reclaim", direction),
        conviction=conviction, alignment=EITHER,
        zone=(low, high), preferred=vwap_price,
        stop=base.stop_beyond(vwap_price, direction, deviation * 0.75),
        stop_basis="the far side of the day's VWAP, beyond the reclaim",
        targets=[
            {"price": (vwap_price + deviation if direction == "bullish"
                       else vwap_price - deviation),
             "basis": "the first VWAP band"},
            {"price": target, "basis": "the second VWAP band"},
        ],
        sources=[ZoneHint("vwap", "day", low, high, conviction,
                          f"day VWAP {vwap_price:.2f}")],
        evidence=evidence,
        note=("Price has taken the day's fair value back. The retest of the "
              "anchor is the entry; losing it again invalidates the idea."),
    )]


# --- the initial balance gives way ------------------------------------------


@base.strategy("ib_extension", "Initial Balance Extension", TREND,
               alignment=EITHER, min_conviction=54)
def ib_extension(ctx: Context) -> list[Candidate]:
    """Price extends beyond the session's first hour and retests the edge."""
    if not ctx.auction.get("available"):
        return []
    extension = ctx.auction.get("extension") or {}
    ib = ctx.auction.get("initial_balance") or {}
    direction_of = extension.get("direction")

    # "both" is a rotation and "none" has not chosen yet; neither is this trade.
    if direction_of not in ("upward", "downward"):
        return []
    if not ib.get("complete"):
        return []

    direction = "bullish" if direction_of == "upward" else "bearish"
    edge = float(ib["high"] if direction == "bullish" else ib["low"])
    width = float(ib.get("width") or 0.0)
    if width <= 0:
        return []

    # The entry is the retest of the broken edge, so price must be near it.
    if ctx.distance_atr(edge) > 1.6:
        return []

    projection = width  # a measured move of the initial balance itself
    target = edge + projection if direction == "bullish" else edge - projection
    if abs(target - edge) / PIP < MIN_OBJECTIVE_PIPS:
        return []

    regime_factor, regime_note = _regime_weight(ctx, wants_persistence=True)
    room = ctx.has_room()
    anchor = ctx.anchor("session")
    anchor_agrees = bool(
        anchor is not None
        and ((direction == "bullish" and float(anchor.get("distance_sigma") or 0) > 0)
             or (direction == "bearish" and float(anchor.get("distance_sigma") or 0) < 0)))

    conviction = base.mix(
        (0.26, regime_factor),
        (0.22, 1.0 if room is None else (1.0 if room else 0.25)),
        (0.20, 1.0 if anchor_agrees else 0.5),
        (0.18, 1.0 if ctx.htf_agrees(direction) else 0.5),
        (0.14, base.clamp(1.0 - ctx.distance_atr(edge) / 1.6, 0.0, 1.0)),
    )

    tolerance = max(ctx.atr * 0.35, width * 0.08)
    if direction == "bullish":
        low, high = edge - tolerance * 0.4, edge + tolerance
    else:
        low, high = edge - tolerance, edge + tolerance * 0.4

    session = str(ib.get("session", "session")).title()
    evidence = [
        f"Price has extended {direction_of} out of the {session} initial "
        f"balance ({ib['low']:.2f}-{ib['high']:.2f})",
        f"The broken edge at {edge:.2f} now acts as "
        f"{'support' if direction == 'bullish' else 'resistance'}",
        regime_note,
    ]
    if room is False:
        evidence.append(
            "Today has already covered its typical range, so the measured move "
            "is unlikely to complete — banked at the first objective")
    if anchor_agrees:
        evidence.append("The session VWAP agrees with the extension")

    return [Candidate(
        key="ib_extension", name="Initial Balance Extension", family=TREND,
        direction=direction, trigger_family="continuation",
        label=base.label_for("IB Extension", direction),
        conviction=conviction, alignment=EITHER,
        zone=(low, high), preferred=edge,
        # The stop sits just back inside the *broken* edge, not at the far one.
        #
        # Placing it at the opposite edge is the intuitive reading of "a close
        # back inside the initial balance invalidates" and it is structurally
        # indefensible: risk becomes the full IB width while the first
        # objective is half of it, so the setup is guaranteed a reward/risk
        # below 1 before the market has done anything. Measured on real
        # firings this produced 0.45:1. The trade being expressed is the retest
        # of the broken edge holding, so what invalidates it is that retest
        # failing -- a few tenths of an ATR back through the edge, not a
        # journey to the other side of the range.
        stop=base.stop_beyond(edge, direction, tolerance + ctx.atr * 0.35),
        stop_basis=f"back through the {session} initial-balance edge it broke",
        targets=[
            {"price": edge + projection * 0.5 if direction == "bullish"
             else edge - projection * 0.5,
             "basis": "half the initial-balance width projected from the edge"},
            {"price": target,
             "basis": "a full measured move of the initial balance"},
        ],
        sources=[ZoneHint("initial balance", ib.get("session", "session"),
                          low, high, conviction,
                          f"{session} IB edge {edge:.2f}")],
        evidence=evidence,
        note=("Range extension out of the first hour is the earliest reliable "
              "sign of a trend day. The retest of the broken edge is the entry; "
              "a close back inside the initial balance says it was a false one."),
    )]
