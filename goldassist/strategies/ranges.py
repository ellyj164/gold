"""Range and volatility strategies.

Gold spends a large part of every week going nowhere in particular, and the
trend engines are silent for all of it. That silence was costing setups: a
clean 120-pip range offers a trade at each edge and a trade on the break, none
of which any trend-following read will ever produce.

``range_fade``        selling the high and buying the low of an established range
``range_breakout``    the break of that range, entered on the retest
``asian_range_break`` the London break of the Asian session range
``squeeze_expansion`` the first expansion out of a volatility squeeze
``pivot_reaction``    a reaction at a floor-trader pivot level

Range geometry is measured from the candles rather than inferred from labels,
because "the market is ranging" and "the range is 4382-4407" are different
statements and only the second one can be traded.
"""

from __future__ import annotations

from . import base
from .base import Candidate, Context, ZoneHint, RANGE, EITHER, COUNTER

# Minimum room, in pips, for a range trade to be worth taking. A range whose
# whole height is smaller than the day-trading minimum has no trade in it.
MIN_RANGE_PIPS = 600.0
PIP = 0.01


def _measure_range(ctx: Context, timeframe: str, bars: int) -> dict | None:
    """The high/low envelope of the last ``bars`` candles, with touch counts."""
    df = ctx.frame(timeframe)
    if df is None or len(df) < max(20, bars // 3):
        return None
    window = df.tail(bars)
    high = float(window["high"].max())
    low = float(window["low"].min())
    height = high - low
    if height <= 0:
        return None

    tolerance = max(ctx.atr * 0.25, height * 0.06)
    high_touches = int((window["high"] >= high - tolerance).sum())
    low_touches = int((window["low"] <= low + tolerance).sum())
    # Time spent inside the middle half tells a range from a trend leg that
    # merely happens to have a highest and a lowest candle.
    mid_low, mid_high = low + height * 0.25, high - height * 0.25
    inside = float(((window["close"] >= mid_low) & (window["close"] <= mid_high)).mean())
    return {
        "timeframe": timeframe,
        "high": high,
        "low": low,
        "height": height,
        "high_touches": high_touches,
        "low_touches": low_touches,
        "containment": inside,
        "tolerance": tolerance,
        "bars": len(window),
    }


def _quality(rng: dict) -> float:
    """0-1: how much this envelope actually behaves like a range."""
    return base.clamp(
        min(1.0, (rng["high_touches"] + rng["low_touches"]) / 6.0) * 0.5
        + rng["containment"] * 0.5, 0.0, 1.0)


# --- fading the edges -------------------------------------------------------


@base.strategy("range_fade", "Range Edge Fade", RANGE,
               alignment=COUNTER, min_conviction=54)
def range_fade(ctx: Context) -> list[Candidate]:
    """Selling the top of an established range, buying the bottom."""
    rng = _measure_range(ctx, "M15", 96) or _measure_range(ctx, "H1", 48)
    if not rng or rng["height"] < MIN_RANGE_PIPS * PIP:
        return []
    quality = _quality(rng)
    if quality < 0.35:
        return []

    tolerance = rng["tolerance"]
    out: list[Candidate] = []

    at_high = ctx.price >= rng["high"] - tolerance
    at_low = ctx.price <= rng["low"] + tolerance
    if not (at_high or at_low):
        return []

    direction = "bearish" if at_high else "bullish"
    edge = rng["high"] if at_high else rng["low"]
    opposite_edge = rng["low"] if at_high else rng["high"]
    tf = rng["timeframe"]

    if at_high:
        low, high = edge - tolerance, edge + tolerance * 0.5
    else:
        low, high = edge - tolerance * 0.5, edge + tolerance

    rsi = float(ctx.ind(tf, "rsi").get("value") or 50)
    extreme = rsi >= 62 if at_high else rsi <= 38
    rejection = bool(ctx.patterns(tf, direction) or ctx.patterns("M5", direction))
    conviction = base.mix(
        (0.30, quality),
        (0.20, 1.0 if extreme else 0.45),
        (0.20, 1.0 if rejection else 0.45),
        (0.15, 1.0 if ctx.regime in ("range", "consolidation") else 0.5),
        (0.15, base.clamp(rng["height"] / (MIN_RANGE_PIPS * PIP * 2), 0.0, 1.0)),
    )
    # The objective is the other side of the range, stopping short of the edge
    # itself -- the last few pips into a level are where fades give money back.
    target = (opposite_edge + tolerance * 0.5 if at_high
              else opposite_edge - tolerance * 0.5)
    out.append(Candidate(
        key="range_fade", name="Range Edge Fade", family=RANGE,
        direction=direction, trigger_family="range",
        label=base.label_for("Range Fade", direction),
        conviction=conviction, alignment=COUNTER,
        zone=(low, high), preferred=(low + high) / 2,
        stop=base.stop_beyond(edge, direction, tolerance + ctx.atr * 0.3),
        stop_basis=f"beyond the {tf} range {'high' if at_high else 'low'}",
        targets=[{"price": target, "basis": f"opposite edge of the {tf} range"},
                 {"price": (rng["high"] + rng["low"]) / 2,
                  "basis": f"midpoint of the {tf} range"}],
        sources=[ZoneHint("range edge", tf, low, high, quality * 100,
                          f"{tf} range {'high' if at_high else 'low'} {edge:.2f}")],
        evidence=[f"{tf} range {rng['low']:.2f}-{rng['high']:.2f} "
                  f"({rng['height'] / PIP:.0f} pips) over {rng['bars']} candles",
                  f"{rng['high_touches']} touches of the high, "
                  f"{rng['low_touches']} of the low",
                  f"{tf} RSI {rsi:.0f}" + (" (extreme)" if extreme else ""),
                  "rejection candle printed at the edge" if rejection
                  else "no rejection candle yet"],
    ))
    return out


# --- breaking the range -----------------------------------------------------


@base.strategy("range_breakout", "Range Breakout Retest", RANGE,
               alignment=EITHER, min_conviction=54)
def range_breakout(ctx: Context) -> list[Candidate]:
    """A break of the range, entered on the retest of the broken edge."""
    rng = _measure_range(ctx, "M15", 96) or _measure_range(ctx, "H1", 48)
    if not rng or rng["height"] < MIN_RANGE_PIPS * PIP * 0.6:
        return []

    tolerance = rng["tolerance"]
    tf = rng["timeframe"]
    broken_up = ctx.price > rng["high"] + tolerance * 0.4
    broken_down = ctx.price < rng["low"] - tolerance * 0.4
    if not (broken_up or broken_down):
        return []

    direction = "bullish" if broken_up else "bearish"
    edge = rng["high"] if broken_up else rng["low"]
    if ctx.distance_atr(edge) > 2.0:
        return []      # the retest has already gone; this is a chase now

    low, high = base.band(edge, tolerance)
    displacement = bool(ctx.events("M15", direction, ("BOS", "MSS"))
                        or ctx.events("M5", direction, ("BOS", "MSS")))
    volume = ctx.ind(tf, "volume")
    conviction = base.mix(
        (0.25, _quality(rng)),
        (0.25, 1.0 if displacement else 0.4),
        (0.20, 1.0 if volume.get("expanding") else 0.55),
        (0.15, 1.0 if ctx.htf_agrees(direction) else 0.5),
        (0.15, base.clamp(1.0 - ctx.distance_atr(edge) / 2.0, 0.0, 1.0)),
    )
    # The measured move -- range height projected from the break -- is the
    # standard objective and is what makes these setups worth 500+ pips.
    projected = edge + rng["height"] if broken_up else edge - rng["height"]
    return [Candidate(
        key="range_breakout", name="Range Breakout Retest", family=RANGE,
        direction=direction, trigger_family="breakout",
        label=base.label_for("Range Break Retest", direction),
        conviction=conviction, alignment=EITHER,
        zone=(low, high), preferred=edge,
        stop=base.stop_beyond(edge, direction, tolerance + ctx.atr * 0.4),
        stop_basis=f"back inside the {tf} range",
        targets=[{"price": projected,
                  "basis": f"measured move ({rng['height'] / PIP:.0f} pips) "
                           f"from the {tf} range"}],
        sources=[ZoneHint("broken edge", tf, low, high, 60.0,
                          f"{tf} range edge {edge:.2f}")],
        evidence=[f"{tf} range {rng['low']:.2f}-{rng['high']:.2f} broken "
                  f"{'above' if broken_up else 'below'}",
                  "structure confirmed the break" if displacement
                  else "waiting for structure to confirm the break",
                  f"volume {volume.get('note', 'unavailable')}",
                  f"measured move projects {projected:.2f}"],
    )]


# --- the Asian range --------------------------------------------------------


@base.strategy("asian_range_break", "Asian Range Break", RANGE,
               alignment=EITHER, min_conviction=53)
def asian_range_break(ctx: Context) -> list[Candidate]:
    """London taking out the Asian session range.

    The single most repeatable intraday pattern on Gold: the thin overnight
    session builds a box, and the first real liquidity of the day takes one
    side of it. Entered on the retest of the broken edge, stopped back inside.
    """
    df = ctx.frame("M15")
    if df is None or len(df) < 40:
        return []

    index = df.index
    try:
        hours = index.hour
    except AttributeError:
        return []

    # The Asian window is 23:00-07:00 UTC, so it straddles midnight: take the
    # most recent contiguous run of Asian-hour candles.
    asian_mask = (hours >= 23) | (hours < 7)
    if not asian_mask.any():
        return []
    positions = [i for i, flag in enumerate(asian_mask) if flag]
    end = positions[-1]
    start = end
    while start - 1 in positions:
        start -= 1
    if end - start < 8:
        return []      # not a full overnight session; nothing meaningful to break

    window = df.iloc[start:end + 1]
    # Only valid while the session that follows it is still running.
    last_hour = int(index[-1].hour)
    if not (7 <= last_hour < 17):
        return []

    high = float(window["high"].max())
    low = float(window["low"].min())
    height = high - low
    if height <= 0 or height > ctx.atr * 6:
        return []

    broken_up = ctx.price > high
    broken_down = ctx.price < low
    if not (broken_up or broken_down):
        return []

    direction = "bullish" if broken_up else "bearish"
    edge = high if broken_up else low
    if ctx.distance_atr(edge) > 2.0:
        return []

    tolerance = max(ctx.atr * 0.2, height * 0.08)
    zone_low, zone_high = base.band(edge, tolerance)
    displacement = bool(ctx.events("M15", direction, ("BOS", "MSS")))
    conviction = base.mix(
        (0.30, base.clamp(height / (ctx.atr * 2.5), 0.0, 1.0)),
        (0.25, 1.0 if displacement else 0.45),
        (0.25, 1.0 if ctx.htf_agrees(direction) else 0.5),
        (0.20, base.clamp(1.0 - ctx.distance_atr(edge) / 2.0, 0.0, 1.0)),
    )
    projected = edge + height if broken_up else edge - height
    return [Candidate(
        key="asian_range_break", name="Asian Range Break", family=RANGE,
        direction=direction, trigger_family="breakout",
        label=base.label_for("Asian Range Break", direction),
        conviction=conviction, alignment=EITHER,
        zone=(zone_low, zone_high), preferred=edge,
        stop=base.stop_beyond(edge, direction, tolerance + ctx.atr * 0.35),
        stop_basis="back inside the Asian range",
        targets=[{"price": projected,
                  "basis": f"Asian range measured move ({height / PIP:.0f} pips)"}],
        sources=[ZoneHint("Asian range edge", "M15", zone_low, zone_high, 60.0,
                          f"Asian {'high' if broken_up else 'low'} {edge:.2f}")],
        evidence=[f"Asian range {low:.2f}-{high:.2f} ({height / PIP:.0f} pips) "
                  f"broken {'above' if broken_up else 'below'}",
                  f"{len(window)} overnight candles built it",
                  "M15 structure confirmed the break" if displacement
                  else "waiting for M15 structure to confirm"],
    )]


# --- volatility -------------------------------------------------------------


@base.strategy("squeeze_expansion", "Volatility Squeeze", RANGE,
               alignment=EITHER, min_conviction=52)
def squeeze_expansion(ctx: Context) -> list[Candidate]:
    """The first directional expansion out of a compressed range."""
    out: list[Candidate] = []
    for tf in ("M15", "H1"):
        bollinger = ctx.ind(tf, "bollinger")
        if not bollinger.get("squeeze"):
            continue
        # Direction comes from what is actually breaking, not from a guess.
        direction = None
        for candidate_direction in ("bullish", "bearish"):
            if ctx.events(tf, candidate_direction, ("BOS", "MSS")) or \
                    ctx.events("M5", candidate_direction, ("BOS", "MSS")):
                direction = candidate_direction
                break
        if direction is None:
            keltner = ctx.ind(tf, "keltner").get("bias")
            if keltner in ("bullish", "bearish"):
                direction = keltner
        if direction is None:
            continue

        atr_tf = ctx.atr_of(tf)
        if direction == "bullish":
            low, high = ctx.price - atr_tf * 0.7, ctx.price - atr_tf * 0.1
        else:
            low, high = ctx.price + atr_tf * 0.1, ctx.price + atr_tf * 0.7

        conviction = base.mix(
            (0.30, 1.0),                      # the squeeze itself is the edge
            (0.25, 1.0 if ctx.events(tf, direction, ("BOS", "MSS")) else 0.5),
            (0.25, 1.0 if ctx.ind(tf, "volume").get("expanding") else 0.5),
            (0.20, 1.0 if ctx.htf_agrees(direction) else 0.5),
        )
        out.append(Candidate(
            key="squeeze_expansion", name="Volatility Squeeze", family=RANGE,
            direction=direction, trigger_family="breakout",
            label=base.label_for("Squeeze Break", direction),
            conviction=conviction, alignment=EITHER,
            zone=(low, high), preferred=(low + high) / 2,
            stop=base.stop_beyond(low if direction == "bullish" else high,
                                  direction, atr_tf * 1.0),
            stop_basis=f"1 ATR beyond the {tf} squeeze",
            sources=[ZoneHint("squeeze", tf, low, high, 50.0,
                              f"{tf} Bollinger squeeze")],
            evidence=[f"{tf} Bollinger bands are in the lowest quintile of "
                      "their 100-candle width",
                      f"{tf} {bollinger.get('note', '')}",
                      "expansion is starting " + direction],
        ))
        break
    return out


# --- pivots -----------------------------------------------------------------


@base.strategy("pivot_reaction", "Pivot Reaction", RANGE,
               alignment=EITHER, min_conviction=50)
def pivot_reaction(ctx: Context) -> list[Candidate]:
    """A reaction at a floor-trader pivot, which Gold respects intraday."""
    pivots = ctx.levels.get("pivots") or {}
    if not pivots:
        return []

    tolerance = ctx.atr * 0.3
    out: list[Candidate] = []
    for name, value in pivots.items():
        price = float(value)
        if abs(ctx.price - price) > tolerance:
            continue
        # Support pivots are bought, resistance pivots sold; the central pivot
        # is read from which side price approached it.
        if name.startswith("S"):
            direction = "bullish"
        elif name.startswith("R"):
            direction = "bearish"
        else:
            direction = "bullish" if ctx.price >= price else "bearish"

        rejection = bool(ctx.patterns("M15", direction) or ctx.patterns("M5", direction))
        if not rejection and not ctx.events("M5", direction, ("CHoCH", "MSS", "BOS")):
            continue

        low, high = base.band(price, tolerance)
        conviction = base.mix(
            (0.30, 1.0 if rejection else 0.5),
            (0.25, {"P": 0.8, "R1": 1.0, "S1": 1.0}.get(name, 0.7)),
            (0.25, 1.0 if ctx.events("M5", direction, ("CHoCH", "MSS", "BOS")) else 0.5),
            (0.20, 1.0 if ctx.htf_agrees(direction) else 0.5),
        )
        out.append(Candidate(
            key="pivot_reaction", name="Pivot Reaction", family=RANGE,
            direction=direction, trigger_family="range",
            label=base.label_for(f"{name} Pivot Reaction", direction),
            conviction=conviction, alignment=EITHER,
            zone=(low, high), preferred=price,
            stop=base.stop_beyond(price, direction, tolerance + ctx.atr * 0.4),
            stop_basis=f"beyond the daily {name} pivot",
            sources=[ZoneHint("pivot", "D1", low, high, 55.0, f"daily {name} pivot")],
            evidence=[f"price is at the daily {name} pivot ({price:.2f})",
                      "rejection candle printed there" if rejection
                      else "M5 structure turned there"],
        ))
    # One pivot idea per cycle: the nearest one is the one being traded.
    out.sort(key=lambda c: abs(ctx.price - (c.preferred or ctx.price)))
    return out[:1]
