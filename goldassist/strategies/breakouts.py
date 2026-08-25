"""Breakout, compression and continuation strategies.

The library already knew how to fade a range and how to buy a retest of one it
had broken. What it could not see was the whole family of setups that form
*before* a range is even recognisable as one: the first thirty minutes of a
session, a flag hanging off an impulse leg, a triangle squeezing shut, a
trendline that has held four times. Those are the setups an intraday Gold
trader spends most of the London morning waiting for, and none of them are a
horizontal envelope, so ``ranges._measure_range`` will never find them.

``opening_range_break``  the break of a session's first candles
``trendline_break``      a sloping line broken and retested
``flag_continuation``    a tight consolidation hanging off an impulse
``triangle_break``       converging highs and lows resolving
``compression_break``    an inside bar or an NR7 expanding

Every one of these measures its own geometry off the candles. A trendline is
fitted through actual swing points and rejected if the fit is poor; a flag has
to hang off a leg that really happened; a triangle has to be genuinely
converging rather than merely noisy. A detector that cannot measure its pattern
returns nothing, which is the whole discipline of this package: the library is
allowed to be silent, it is not allowed to guess.
"""

from __future__ import annotations

from datetime import time as _time

import numpy as np

from . import base
from .base import Candidate, Context, ZoneHint, EITHER, RANGE, TREND, WITH_TREND

PIP = 0.10

# The floor from opportunity.py, in price. A pattern whose whole structure is
# smaller than the trader's minimum move has no trade in it however clean it
# looks, so each detector checks its own geometry against this before doing any
# further work.
MIN_MOVE = 50.0 * PIP


# --- shared geometry --------------------------------------------------------


def _swings(ctx: Context, timeframe: str, kind: str, count: int = 6) -> list[dict]:
    """The most recent swing points of one kind, oldest first."""
    swings = [s for s in (ctx.struct(timeframe).get("swings") or [])
              if s.get("kind") == kind]
    return swings[-count:]


def _fit_line(points: list[tuple[float, float]]) -> tuple[float, float, float] | None:
    """Least-squares fit through (x, y) points.

    Returns ``(slope, intercept, fit)`` where ``fit`` is 0-1 -- how well the
    points actually sit on the line, measured as 1 minus the mean absolute
    residual over the price span they cover. A trendline drawn through three
    points that are nowhere near collinear is a drawing, not a level, and the
    caller uses this to throw those away.
    """
    if len(points) < 3:
        return None
    xs = np.array([p[0] for p in points], dtype=float)
    ys = np.array([p[1] for p in points], dtype=float)
    if xs.max() - xs.min() < 1e-9:
        return None
    slope, intercept = np.polyfit(xs, ys, 1)
    residual = float(np.abs(ys - (slope * xs + intercept)).mean())
    span = float(ys.max() - ys.min()) or 1.0
    return float(slope), float(intercept), base.clamp(1.0 - residual / span, 0.0, 1.0)


def _bar_span(ctx: Context, timeframe: str) -> float:
    """Seconds per candle, so a fitted line can be projected to *now*."""
    df = ctx.frame(timeframe)
    if df is None or len(df) < 3:
        return 0.0
    return float((df.index[-1] - df.index[-2]).total_seconds())


# --- session opening range --------------------------------------------------

# The window whose high and low form the opening range, per session, in UTC.
# These are the first candles after the session's participants arrive, and the
# break of them is one of the oldest intraday setups there is.
OPENING_RANGES = (
    ("London open", _time(7, 0), _time(8, 0)),
    ("New York open", _time(13, 30), _time(14, 30)),
)


@base.strategy("opening_range_break", "Opening Range Break", RANGE,
               alignment=EITHER, min_conviction=55)
def opening_range_break(ctx: Context) -> list[Candidate]:
    """The break of the first hour of London or New York.

    Deliberately distinct from ``asian_range_break``. That strategy trades the
    London sweep of the *overnight* range; this one trades the range the
    session builds for itself in its own first hour, which is a different
    envelope, forms later, and usually breaks in the opposite direction to the
    sweep that preceded it.
    """
    df = ctx.frame("M15")
    if df is None or len(df) < 20:
        return []

    today = df.index[-1].date()
    session = df[df.index.date == today]
    if session.empty:
        return []

    for label, start, end in OPENING_RANGES:
        window = session.between_time(start, end)
        # Needs to be finished: a range still forming has no break to trade.
        if len(window) < 3 or session.index[-1].time() <= end:
            continue

        high = float(window["high"].max())
        low = float(window["low"].min())
        height = high - low
        if height < MIN_MOVE * 0.4 or height > ctx.atr * 6:
            continue

        # Only the candles after the window closed can break it.
        after = session[session.index.time >= end]
        if after.empty:
            continue

        broke_up = float(after["close"].max()) > high
        broke_down = float(after["close"].min()) < low
        if broke_up == broke_down:          # neither, or both -- no clean break
            continue

        direction = "bullish" if broke_up else "bearish"
        edge = high if broke_up else low
        # Entry is the retest of the broken edge, not the break itself. Chasing
        # the break is where opening-range trading loses its money.
        buffer = max(ctx.atr * 0.22, height * 0.10)
        zone = ((edge - buffer * 0.4, edge + buffer) if broke_up
                else (edge - buffer, edge + buffer * 0.4))

        extension = (float(after["high"].max()) - high if broke_up
                     else low - float(after["low"].min()))
        volume_push = _volume_push(after)
        conviction = base.mix(
            (0.28, base.clamp(height / (MIN_MOVE * 1.5), 0.2, 1.0)),
            (0.24, base.clamp(extension / max(height, 1e-9), 0.0, 1.0)),
            (0.20, 1.0 if ctx.htf_agrees(direction) else 0.45),
            (0.16, volume_push),
            (0.12, 1.0 if ctx.regime in ("trending", "expansion") else 0.5),
        )
        return [Candidate(
            key="opening_range_break", name="Opening Range Break", family=RANGE,
            direction=direction, trigger_family="breakout",
            label=base.label_for(f"{label} Range Break", direction),
            conviction=conviction, alignment=EITHER,
            zone=zone, preferred=edge,
            stop=base.stop_beyond(low if broke_up else high, direction, buffer),
            stop_basis=f"the far side of the {label.lower()} range",
            # Measured objective: opening ranges characteristically extend by
            # about their own height, then by twice it.
            targets=[{"price": edge + height if broke_up else edge - height,
                      "basis": "one range height beyond the break"},
                     {"price": edge + height * 2 if broke_up else edge - height * 2,
                      "basis": "two range heights beyond the break"}],
            sources=[ZoneHint("opening range", "M15", zone[0], zone[1], 70,
                              f"{label} range {low:.2f}-{high:.2f}")],
            evidence=[f"{label} range {low:.2f}-{high:.2f} "
                      f"({height / PIP:.0f} pips) from {len(window)} M15 candles",
                      f"broken {'above' if broke_up else 'below'} and extended "
                      f"{extension / PIP:.0f} pips",
                      "higher timeframes agree" if ctx.htf_agrees(direction)
                      else "against the higher-timeframe bias"],
        )]
    return []


def _room(ctx: Context, direction: str, from_price: float,
          strength: int = 60, cap: float = None) -> dict | None:
    """How far a move can run before it meets a level, and which level.

    Returns ``{"distance": float, "level": float | None, "kind": str}``.
    ``level`` is None when nothing strong stands in the way inside the search
    cap, in which case ``distance`` is the cap itself -- the run is open as far
    as this is willing to look.
    """
    cap = cap if cap is not None else max(ctx.atr * 4, MIN_MOVE * 3)
    wanted = "resistance" if direction == "bullish" else "support"
    nearest = None
    for level in (ctx.levels.get("levels") or []):
        if level.get("kind") != wanted or level.get("strength", 0) < strength:
            continue
        price = float(level["price"])
        ahead = price > from_price if direction == "bullish" else price < from_price
        if not ahead:
            continue
        distance = abs(price - from_price)
        if distance > cap:
            continue
        if nearest is None or distance < nearest[0]:
            nearest = (distance, price)
    if nearest is None:
        return {"distance": cap, "level": None, "kind": wanted}
    return {"distance": nearest[0], "level": nearest[1], "kind": wanted}


def _volume_push(window) -> float:
    """0-1: whether the break carried more volume than the candles before it."""
    if "volume" not in window or len(window) < 3:
        return 0.5
    volumes = window["volume"].astype(float)
    if volumes.mean() <= 0:
        return 0.5
    return base.clamp(float(volumes.iloc[:3].mean() / volumes.mean()) / 1.6, 0.0, 1.0)


# --- trendlines -------------------------------------------------------------


@base.strategy("trendline_break", "Trendline Break Retest", TREND,
               alignment=EITHER, min_conviction=56)
def trendline_break(ctx: Context) -> list[Candidate]:
    """A sloping line through three or more swings, broken and retested.

    A rising line under the lows is support until it breaks; once it does, the
    same line is resistance, and the retest of it is the entry. The line has to
    fit the swings it is drawn through -- ``_fit_line`` reports how well, and a
    poor fit is discarded rather than traded.
    """
    out: list[Candidate] = []
    for timeframe in ("H1", "M30"):
        span = _bar_span(ctx, timeframe)
        if not span:
            continue
        df = ctx.frame(timeframe)
        if df is None or len(df) < 30:
            continue
        now_x = float(df.index[-1].timestamp())

        for kind, direction in (("low", "bearish"), ("high", "bullish")):
            # A rising line under the lows breaks downward; a falling line over
            # the highs breaks upward.
            #
            # The line is fitted only to the swings that *predate* the break.
            # This is not a refinement, it is the whole thing working at all:
            # breaking a rising support line prints new, lower swing lows, and
            # a fit that includes them is dragged down below price -- so the
            # line can never read as broken and the strategy is silent forever.
            # A trader draws the line on the history and then watches price
            # leave it; the fit has to do the same.
            swings = _swings(ctx, timeframe, kind, count=6)
            if len(swings) < 4:
                continue
            anchors = swings[:-1]
            fitted = _fit_line([(float(s["time"]), float(s["price"])) for s in anchors])
            if fitted is None:
                continue
            slope, intercept, fit = fitted
            if fit < 0.62:
                continue
            # The line must actually slope, or it is a horizontal level and
            # belongs to the range strategies.
            drift_per_bar = slope * span
            if abs(drift_per_bar) < ctx.atr * 0.04:
                continue
            # And it must slope the way the pattern requires.
            if kind == "low" and drift_per_bar <= 0:
                continue
            if kind == "high" and drift_per_bar >= 0:
                continue

            line_now = slope * now_x + intercept
            # How close counts as "retesting the line". Two ATR rather than
            # one and a half: the entry zone is at the line, and demanding
            # price already be almost on it before the setup may be *tracked*
            # means the setup only ever appears once the move is over. The
            # zone is what the trader waits at; this is only the window in
            # which the idea is worth carrying.
            if abs(ctx.price - line_now) > ctx.atr * 2.0:
                continue
            broken = (ctx.price < line_now if direction == "bearish"
                      else ctx.price > line_now)
            if not broken:
                continue

            buffer = ctx.atr * 0.3
            zone = ((line_now - buffer * 0.5, line_now + buffer) if direction == "bullish"
                    else (line_now - buffer, line_now + buffer * 0.5))
            # The objective is the swing the line was containing.
            containment = (float(df["high"].tail(60).max()) if direction == "bullish"
                           else float(df["low"].tail(60).min()))
            if abs(containment - line_now) < MIN_MOVE:
                continue

            touches = len(anchors)
            conviction = base.mix(
                (0.32, fit),
                (0.24, base.clamp((touches - 2) / 3.0, 0.0, 1.0)),
                (0.22, 1.0 if ctx.htf_agrees(direction) else 0.5),
                (0.22, 1.0 if ctx.events(timeframe, direction, ("BOS", "CHoCH", "MSS"))
                 else 0.5),
            )
            out.append(Candidate(
                key="trendline_break", name="Trendline Break Retest", family=TREND,
                direction=direction, trigger_family="breakout",
                label=base.label_for("Trendline Break", direction),
                conviction=conviction, alignment=EITHER,
                zone=zone, preferred=line_now,
                stop=base.stop_beyond(line_now, direction, ctx.atr * 0.8),
                stop_basis=f"back through the {timeframe} trendline",
                targets=[{"price": containment,
                          "basis": f"the {timeframe} extreme the line was holding"}],
                sources=[ZoneHint("trendline", timeframe, zone[0], zone[1],
                                  fit * 100,
                                  f"{timeframe} trendline through {touches} swings")],
                evidence=[f"{timeframe} trendline through {touches} swing "
                          f"{kind}s, fit {fit * 100:.0f}%",
                          f"broken and now retesting {line_now:.2f}",
                          f"slope {drift_per_bar / PIP:+.1f} pips per candle"],
            ))
            break        # one trendline idea per timeframe is enough
    return out


# --- flags and pennants -----------------------------------------------------


@base.strategy("flag_continuation", "Flag Continuation", TREND,
               alignment=WITH_TREND, min_conviction=58)
def flag_continuation(ctx: Context) -> list[Candidate]:
    """An impulse leg, then a tight drift against it, then continuation.

    The measured move is what makes this worth trading: a flag projects the
    length of the pole from the breakout point, which on Gold's M15 is
    routinely several hundred pips and comfortably clears the day-trading
    minimum on its own.
    """
    out: list[Candidate] = []
    for timeframe, pole_bars, flag_bars in (("M15", 12, 10), ("H1", 10, 8)):
        df = ctx.frame(timeframe)
        if df is None or len(df) < pole_bars + flag_bars + 5:
            continue

        flag = df.tail(flag_bars)
        pole = df.iloc[-(pole_bars + flag_bars):-flag_bars]
        if pole.empty or flag.empty:
            continue

        move = float(pole["close"].iloc[-1] - pole["open"].iloc[0])
        pole_height = abs(move)
        if pole_height < MIN_MOVE:
            continue
        direction = "bullish" if move > 0 else "bearish"

        flag_high = float(flag["high"].max())
        flag_low = float(flag["low"].min())
        flag_height = flag_high - flag_low
        # A flag consolidates. If the "flag" is as big as the pole it is not a
        # pause in the move, it is the end of it.
        if flag_height <= 0 or flag_height > pole_height * 0.55:
            continue
        # It should also drift against the pole, or at worst sideways.
        drift = float(flag["close"].iloc[-1] - flag["close"].iloc[0])
        if direction == "bullish" and drift > flag_height * 0.5:
            continue
        if direction == "bearish" and drift < -flag_height * 0.5:
            continue
        # And it should not have given back the whole leg.
        retrace = ((float(pole["close"].iloc[-1]) - flag_low) / pole_height
                   if direction == "bullish"
                   else (flag_high - float(pole["close"].iloc[-1])) / pole_height)
        if retrace > 0.62:
            continue

        breakout = flag_high if direction == "bullish" else flag_low
        if not ctx.ahead(breakout, direction) and abs(ctx.price - breakout) > ctx.atr:
            continue

        buffer = max(flag_height * 0.25, ctx.atr * 0.18)
        zone = ((breakout - buffer, breakout + buffer * 0.5) if direction == "bullish"
                else (breakout - buffer * 0.5, breakout + buffer))
        target = (breakout + pole_height if direction == "bullish"
                  else breakout - pole_height)

        tightness = base.clamp(1.0 - flag_height / (pole_height * 0.55), 0.0, 1.0)
        conviction = base.mix(
            (0.30, tightness),
            (0.24, base.clamp(pole_height / (MIN_MOVE * 2.5), 0.2, 1.0)),
            (0.22, 1.0 if ctx.trend_agrees(timeframe, direction) else 0.45),
            (0.14, 1.0 if ctx.htf_agrees(direction) else 0.5),
            (0.10, 1.0 if retrace < 0.4 else 0.55),
        )
        out.append(Candidate(
            key="flag_continuation", name="Flag Continuation", family=TREND,
            direction=direction, trigger_family="breakout",
            label=base.label_for("Flag Break", direction),
            conviction=conviction, alignment=WITH_TREND,
            zone=zone, preferred=breakout,
            stop=base.stop_beyond(flag_low if direction == "bullish" else flag_high,
                                  direction, ctx.atr * 0.25),
            stop_basis=f"the far side of the {timeframe} flag",
            targets=[{"price": target, "basis": "measured move: the pole projected "
                                                "from the breakout"},
                     {"price": (breakout + pole_height * 0.6 if direction == "bullish"
                                else breakout - pole_height * 0.6),
                      "basis": "60% of the measured move"}],
            sources=[ZoneHint("flag", timeframe, zone[0], zone[1], tightness * 100,
                              f"{timeframe} flag {flag_low:.2f}-{flag_high:.2f}")],
            evidence=[f"{timeframe} impulse of {pole_height / PIP:.0f} pips",
                      f"consolidating {flag_height / PIP:.0f} pips over "
                      f"{len(flag)} candles ({retrace * 100:.0f}% retrace)",
                      f"measured move projects {abs(target - breakout) / PIP:.0f} pips"],
        ))
    return out


# --- triangles --------------------------------------------------------------


@base.strategy("triangle_break", "Triangle Break", RANGE,
               alignment=EITHER, min_conviction=56)
def triangle_break(ctx: Context) -> list[Candidate]:
    """Converging highs and lows, taken on the break of the apex.

    Ascending, descending and symmetrical are the same measurement with
    different slopes, so they are detected together and named from the geometry
    rather than being three near-identical detectors.
    """
    out: list[Candidate] = []
    for timeframe in ("H1", "M15"):
        span = _bar_span(ctx, timeframe)
        df = ctx.frame(timeframe)
        if not span or df is None or len(df) < 40:
            continue

        highs = _swings(ctx, timeframe, "high", count=4)
        lows = _swings(ctx, timeframe, "low", count=4)
        if len(highs) < 3 or len(lows) < 3:
            continue
        top = _fit_line([(float(s["time"]), float(s["price"])) for s in highs])
        bottom = _fit_line([(float(s["time"]), float(s["price"])) for s in lows])
        if not top or not bottom or top[2] < 0.6 or bottom[2] < 0.6:
            continue

        now_x = float(df.index[-1].timestamp())
        upper = top[0] * now_x + top[1]
        lower = bottom[0] * now_x + bottom[1]
        if upper <= lower:
            continue

        # Converging means the walls were further apart before than they are
        # now. Measured over the pattern rather than assumed from the slopes.
        earlier_x = float(min(highs[0]["time"], lows[0]["time"]))
        opening = ((top[0] * earlier_x + top[1]) - (bottom[0] * earlier_x + bottom[1]))
        height = upper - lower
        if opening <= 0 or height >= opening * 0.75:
            continue
        if opening < MIN_MOVE:
            continue

        if ctx.price > upper:
            direction, edge = "bullish", upper
        elif ctx.price < lower:
            direction, edge = "bearish", lower
        else:
            continue

        shape = ("ascending" if abs(top[0] * span) < ctx.atr * 0.03
                 else "descending" if abs(bottom[0] * span) < ctx.atr * 0.03
                 else "symmetrical")
        buffer = ctx.atr * 0.28
        zone = ((edge - buffer * 0.5, edge + buffer) if direction == "bullish"
                else (edge - buffer, edge + buffer * 0.5))
        target = (edge + opening if direction == "bullish" else edge - opening)

        conviction = base.mix(
            (0.30, (top[2] + bottom[2]) / 2),
            (0.26, base.clamp(1.0 - height / opening, 0.0, 1.0)),
            (0.24, 1.0 if ctx.htf_agrees(direction) else 0.5),
            (0.20, 1.0 if ctx.clear_run(direction, abs(target - edge)) else 0.45),
        )
        out.append(Candidate(
            key="triangle_break", name="Triangle Break", family=RANGE,
            direction=direction, trigger_family="breakout",
            label=base.label_for(f"{shape.title()} Triangle Break", direction),
            conviction=conviction, alignment=EITHER,
            zone=zone, preferred=edge,
            stop=base.stop_beyond(lower if direction == "bullish" else upper,
                                  direction, buffer),
            stop_basis=f"the opposite wall of the {timeframe} triangle",
            targets=[{"price": target,
                      "basis": "the widest part of the triangle projected from "
                               "the break"}],
            sources=[ZoneHint("triangle", timeframe, zone[0], zone[1],
                              (top[2] + bottom[2]) * 50,
                              f"{shape} triangle on {timeframe}")],
            evidence=[f"{timeframe} {shape} triangle, walls now "
                      f"{lower:.2f}-{upper:.2f}",
                      f"narrowed from {opening / PIP:.0f} to {height / PIP:.0f} pips",
                      f"broken {'above' if direction == 'bullish' else 'below'} "
                      f"at {edge:.2f}"],
        ))
    return out


# --- compression ------------------------------------------------------------


@base.strategy("compression_break", "Compression Break", RANGE,
               alignment=EITHER, min_conviction=54)
def compression_break(ctx: Context) -> list[Candidate]:
    """An inside bar or a narrowest-range bar, taken on the expansion.

    Volatility is famously serial: the quietest candle of the last seven is
    disproportionately often followed by the loudest. This is the cheapest
    setup in the library to detect and one of the most reliable to define,
    because the invalidation is simply the other side of one candle.
    """
    out: list[Candidate] = []
    for timeframe, lookback in (("H1", 7), ("H4", 7)):
        df = ctx.frame(timeframe)
        if df is None or len(df) < lookback + 3:
            continue

        recent = df.tail(lookback)
        last = df.iloc[-1]
        previous = df.iloc[-2]
        ranges = (recent["high"] - recent["low"]).astype(float)
        last_range = float(last["high"] - last["low"])
        if last_range <= 0:
            continue

        inside = bool(last["high"] <= previous["high"] and last["low"] >= previous["low"])
        narrowest = bool(last_range <= ranges.min() + 1e-9)
        if not (inside or narrowest):
            continue

        high, low = float(last["high"]), float(last["low"])
        # The expansion has to be worth taking. A compressed H1 candle whose
        # whole height is ten pips projects a ten-pip move; the trade only
        # exists if the structure beyond it offers the minimum.
        #
        # Note this asks how much room there is, rather than demanding a
        # completely clear run. Gold's chart is dense with levels, and a rule
        # of "no strong level within 40 pips either way" silenced this detector
        # almost permanently. A level in front is where the move goes, not a
        # reason there is no move.
        room_up = _room(ctx, "bullish", high)
        room_down = _room(ctx, "bearish", low)

        compression = base.clamp(1.0 - last_range / max(float(ranges.mean()), 1e-9),
                                 0.0, 1.0)
        for direction, edge, room in (("bullish", high, room_up),
                                      ("bearish", low, room_down)):
            if room is None or room["distance"] < MIN_MOVE:
                continue
            # Only offer the side the higher timeframes lean towards, unless
            # they are genuinely neutral -- a compression break is directionless
            # by nature and taking both sides at once is not a view.
            if ctx.htf_opposes(direction):
                continue
            buffer = max(last_range * 0.2, ctx.atr * 0.12)
            zone = ((edge - buffer * 0.5, edge + buffer) if direction == "bullish"
                    else (edge - buffer, edge + buffer * 0.5))
            # Target the level that is actually in the way, when there is
            # one; otherwise project from the compressed candle's own range.
            projected = max(last_range * 3, MIN_MOVE * 1.2)
            if room["level"] is not None:
                target = room["level"]
                basis = f"the {room['kind']} at {room['level']:.2f} the break runs into"
            else:
                target = (edge + projected if direction == "bullish"
                          else edge - projected)
                basis = "three times the compressed candle's range"
            conviction = base.mix(
                (0.34, compression),
                (0.22, 1.0 if inside else 0.7),
                (0.24, 1.0 if ctx.htf_agrees(direction) else 0.5),
                (0.20, 1.0 if ctx.regime in ("range", "consolidation",
                                             "compression") else 0.55),
            )
            out.append(Candidate(
                key="compression_break", name="Compression Break", family=RANGE,
                direction=direction, trigger_family="breakout",
                label=base.label_for(
                    "Inside Bar Break" if inside else "Range Compression Break",
                    direction),
                conviction=conviction, alignment=EITHER,
                zone=zone, preferred=edge,
                stop=base.stop_beyond(low if direction == "bullish" else high,
                                      direction, ctx.atr * 0.15),
                stop_basis=f"the other side of the {timeframe} compression candle",
                targets=[{"price": target, "basis": basis}],
                sources=[ZoneHint("compression", timeframe, zone[0], zone[1],
                                  compression * 100,
                                  f"{timeframe} {'inside bar' if inside else 'NR7'}")],
                evidence=[f"{timeframe} "
                          + ("inside bar" if inside else f"narrowest of {lookback} candles")
                          + f" at {last_range / PIP:.0f} pips",
                          f"{compression * 100:.0f}% below the recent average range",
                          f"{room['distance'] / PIP:.0f} pips of room "
                          f"{'above' if direction == 'bullish' else 'below'}"
                          + (f" to {room['kind']} at {room['level']:.2f}"
                             if room["level"] is not None else " with nothing in the way")],
            ))
    return out
