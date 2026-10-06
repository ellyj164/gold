"""Reversal, channel and gap strategies.

What was missing from the library here was the other half of a range: the
sloping one. Gold spends whole London sessions inside a rising channel, where
every touch of the lower rail is a trade and none of them are visible to a
horizontal range detector or to a trend-pullback detector looking for an EMA.

``channel_ride``     buying the lower rail of a rising channel, and the mirror
``head_shoulders``   a three-peak reversal taken on the neckline
``climax_reversal``  exhaustion: a range-and-volume climax into a level
``gap_fill``         the Sunday open gap, back towards Friday's close

The detectors here are the ones most able to talk themselves into a pattern
that is not there -- three random swings look like a head and shoulders often
enough -- so each one carries an explicit symmetry or proportion test and
discards anything that only roughly resembles the shape.
"""

from __future__ import annotations

import numpy as np

from . import base
from .base import Candidate, Context, ZoneHint, COUNTER, EITHER, PATTERN, TREND

PIP = 0.01
MIN_MOVE = 500.0 * PIP


def _swings(ctx: Context, timeframe: str, kind: str, count: int = 6) -> list[dict]:
    return [s for s in (ctx.struct(timeframe).get("swings") or [])
            if s.get("kind") == kind][-count:]


def _fit(points: list[tuple[float, float]]):
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


# --- channels ---------------------------------------------------------------


@base.strategy("channel_ride", "Channel Rail", TREND,
               alignment=EITHER, min_conviction=56)
def channel_ride(ctx: Context) -> list[Candidate]:
    """A parallel channel, traded from the rail price is currently at.

    Two lines are fitted independently -- one through the highs, one through
    the lows -- and the pattern is only a channel if they come out close to
    parallel. That test is what separates a channel from a wedge, and the two
    trade in opposite directions, so getting it wrong is not a cosmetic error.
    """
    out: list[Candidate] = []
    for timeframe in ("H1", "M15"):
        df = ctx.frame(timeframe)
        if df is None or len(df) < 40:
            continue
        highs = _swings(ctx, timeframe, "high", 5)
        lows = _swings(ctx, timeframe, "low", 5)
        if len(highs) < 3 or len(lows) < 3:
            continue
        top = _fit([(float(s["time"]), float(s["price"])) for s in highs])
        bottom = _fit([(float(s["time"]), float(s["price"])) for s in lows])
        if not top or not bottom or top[2] < 0.65 or bottom[2] < 0.65:
            continue

        # Parallel, within a tolerance scaled to the price range the fits
        # cover, so the test means the same thing on a quiet day and a wild one.
        scale = max(abs(top[0]), abs(bottom[0]), 1e-12)
        if abs(top[0] - bottom[0]) / scale > 0.45:
            continue

        now_x = float(df.index[-1].timestamp())
        upper = top[0] * now_x + top[1]
        lower = bottom[0] * now_x + bottom[1]
        width = upper - lower
        if width < MIN_MOVE or width > ctx.atr * 8:
            continue

        # "At the rail" is judged on Gold's own scale. At 0.45 ATR this was
        # roughly a two-dollar window on a five-second poll, which price
        # crosses between two ticks -- the strategy would have been silent
        # almost always and fired almost at random when it was not.
        near = ctx.atr * 0.9
        if abs(ctx.price - lower) <= near:
            direction, rail, opposite = "bullish", lower, upper
        elif abs(ctx.price - upper) <= near:
            direction, rail, opposite = "bearish", upper, lower
        else:
            continue

        rising = bottom[0] > 0
        # Buying the lower rail of a rising channel is a continuation trade;
        # selling the upper rail of a rising channel fades it. The alignment
        # label decides how much the confluence engine is allowed to weigh in,
        # so it has to reflect which of the two this actually is.
        with_slope = (direction == "bullish") == rising
        alignment = EITHER if with_slope else COUNTER

        buffer = ctx.atr * 0.32
        zone = ((rail - buffer * 0.6, rail + buffer) if direction == "bullish"
                else (rail - buffer, rail + buffer * 0.6))
        rejection = bool(ctx.patterns(timeframe, direction) or ctx.patterns("M5", direction))
        conviction = base.mix(
            (0.28, (top[2] + bottom[2]) / 2),
            (0.22, base.clamp(width / (MIN_MOVE * 2), 0.2, 1.0)),
            (0.20, 1.0 if rejection else 0.45),
            (0.18, 1.0 if with_slope else 0.55),
            (0.12, 1.0 if ctx.htf_agrees(direction) else 0.5),
        )
        out.append(Candidate(
            key="channel_ride", name="Channel Rail", family=TREND,
            direction=direction, trigger_family="reversal",
            label=base.label_for(
                ("Rising" if rising else "Falling") + " Channel Rail", direction),
            conviction=conviction, alignment=alignment,
            zone=zone, preferred=rail,
            stop=base.stop_beyond(rail, direction, ctx.atr * 0.75),
            stop_basis=f"through the {timeframe} channel rail",
            targets=[{"price": (opposite - buffer * 0.5 if direction == "bullish"
                                else opposite + buffer * 0.5),
                      "basis": "the opposite channel rail"},
                     {"price": (rail + width * 0.5 if direction == "bullish"
                                else rail - width * 0.5),
                      "basis": "the middle of the channel"}],
            sources=[ZoneHint("channel rail", timeframe, zone[0], zone[1],
                              (top[2] + bottom[2]) * 50,
                              f"{timeframe} channel {lower:.2f}-{upper:.2f}")],
            evidence=[f"{timeframe} {'rising' if rising else 'falling'} channel, "
                      f"{width / PIP:.0f} pips wide",
                      f"price at the {'lower' if direction == 'bullish' else 'upper'} "
                      f"rail ({rail:.2f})",
                      "rejection candle at the rail" if rejection
                      else "no rejection candle yet",
                      "with the channel's slope" if with_slope
                      else "fading the channel's slope"],
        ))
    return out


# --- head and shoulders -----------------------------------------------------


@base.strategy("head_shoulders", "Head and Shoulders", PATTERN,
               alignment=EITHER, min_conviction=60)
def head_shoulders(ctx: Context) -> list[Candidate]:
    """Three peaks with a higher middle, taken on the neckline break.

    Held to strict proportions on purpose. Any three swings can be called a
    head and shoulders if the tolerances are loose enough, and the pattern's
    reputation then does the arguing instead of the evidence. Here the head
    must genuinely exceed both shoulders, the shoulders must be within a
    quarter of each other, and the neckline must be roughly level.
    """
    out: list[Candidate] = []
    for timeframe in ("H1", "M15"):
        for kind, direction in (("high", "bearish"), ("low", "bullish")):
            peaks = _swings(ctx, timeframe, kind, 3)
            if len(peaks) < 3:
                continue
            left, head, right = (float(p["price"]) for p in peaks)

            if direction == "bearish":
                if not (head > left and head > right):
                    continue
                prominence = head - max(left, right)
            else:
                if not (head < left and head < right):
                    continue
                prominence = min(left, right) - head

            shoulder_gap = abs(left - right)
            shoulder_size = abs((left + right) / 2 - head)
            if shoulder_size <= 0 or prominence < ctx.atr * 0.5:
                continue
            # Symmetry: shoulders within 25% of the head's prominence.
            if shoulder_gap > prominence * 0.5:
                continue

            # The neckline runs through the two troughs between the peaks.
            troughs = _swings(ctx, timeframe, "low" if kind == "high" else "high", 4)
            between = [float(t["price"]) for t in troughs
                       if peaks[0]["time"] <= t["time"] <= peaks[2]["time"]]
            if len(between) < 2:
                continue
            neckline = float(np.mean(between))
            if max(between) - min(between) > prominence * 0.6:
                continue          # a neckline that sloped that much is not one

            depth = abs(head - neckline)
            if depth < MIN_MOVE:
                continue
            broken = (ctx.price < neckline if direction == "bearish"
                      else ctx.price > neckline)
            if not broken or abs(ctx.price - neckline) > ctx.atr * 1.5:
                continue

            buffer = ctx.atr * 0.3
            zone = ((neckline - buffer * 0.5, neckline + buffer) if direction == "bullish"
                    else (neckline - buffer, neckline + buffer * 0.5))
            target = (neckline - depth if direction == "bearish" else neckline + depth)
            symmetry = base.clamp(1.0 - shoulder_gap / max(prominence, 1e-9), 0.0, 1.0)

            conviction = base.mix(
                (0.32, symmetry),
                (0.24, base.clamp(prominence / (ctx.atr * 2), 0.2, 1.0)),
                (0.22, 1.0 if ctx.events(timeframe, direction,
                                         ("CHoCH", "MSS", "BOS")) else 0.5),
                (0.22, 1.0 if ctx.clear_run(direction, depth) else 0.5),
            )
            out.append(Candidate(
                key="head_shoulders", name="Head and Shoulders", family=PATTERN,
                direction=direction, trigger_family="reversal",
                label=base.label_for(
                    "Head and Shoulders" if direction == "bearish"
                    else "Inverse Head and Shoulders", direction),
                conviction=conviction, alignment=EITHER,
                zone=zone, preferred=neckline,
                stop=base.stop_beyond(
                    max(left, right) if direction == "bearish" else min(left, right),
                    direction, ctx.atr * 0.3),
                stop_basis="beyond the nearer shoulder",
                targets=[{"price": target,
                          "basis": "head-to-neckline depth projected from the break"},
                         {"price": (neckline - depth * 0.6 if direction == "bearish"
                                    else neckline + depth * 0.6),
                          "basis": "60% of the measured move"}],
                sources=[ZoneHint("neckline", timeframe, zone[0], zone[1],
                                  symmetry * 100,
                                  f"{timeframe} neckline at {neckline:.2f}")],
                evidence=[f"{timeframe} "
                          + ("head and shoulders" if direction == "bearish"
                             else "inverse head and shoulders")
                          + f", head {prominence / PIP:.0f} pips clear of the shoulders",
                          f"shoulders within {shoulder_gap / PIP:.0f} pips "
                          f"({symmetry * 100:.0f}% symmetry)",
                          f"neckline {neckline:.2f} broken, projecting "
                          f"{depth / PIP:.0f} pips"],
            ))
    return out


# --- exhaustion -------------------------------------------------------------


@base.strategy("climax_reversal", "Exhaustion Climax", PATTERN,
               alignment=COUNTER, min_conviction=58)
def climax_reversal(ctx: Context) -> list[Candidate]:
    """A range-and-volume climax into a level, faded.

    The distinction that matters: a big candle in the middle of nowhere is
    momentum and should be joined, while the same candle arriving *into* a
    level that has already held is exhaustion and can be faded. So this
    detector refuses to produce anything unless there is a level in front of
    price for the move to have exhausted against.
    """
    out: list[Candidate] = []
    for timeframe in ("M15", "M5"):
        df = ctx.frame(timeframe)
        if df is None or len(df) < 30:
            continue

        last = df.iloc[-1]
        window = df.tail(20)
        ranges = (window["high"] - window["low"]).astype(float)
        last_range = float(last["high"] - last["low"])
        if last_range <= 0 or ranges.mean() <= 0:
            continue
        range_ratio = last_range / float(ranges.mean())
        if range_ratio < 2.0:
            continue

        volumes = window["volume"].astype(float)
        volume_ratio = (float(last["volume"]) / float(volumes.mean())
                        if float(volumes.mean()) > 0 else 1.0)
        if volume_ratio < 1.5:
            continue

        push_up = float(last["close"]) > float(last["open"])
        direction = "bearish" if push_up else "bullish"
        extreme = float(last["high"]) if push_up else float(last["low"])

        # The level the move exhausted into. Without one this is momentum.
        #
        # "Into" is measured against the climax candle's own reach as well as a
        # fixed tolerance: a climax characteristically spikes *through* a level
        # and closes back below it, so a level a fixed fraction of ATR from the
        # extreme is exactly the case this must catch, not exclude.
        wanted = "resistance" if push_up else "support"
        reach = max(ctx.atr, last_range * 0.6)
        levels = [lv for lv in (ctx.levels.get("levels") or [])
                  if lv.get("kind") == wanted
                  and abs(float(lv["price"]) - extreme) <= reach]
        if not levels:
            continue
        level = max(levels, key=lambda lv: lv.get("strength", 0))

        # The close has to have come back off the extreme; a candle that closed
        # on its high has not exhausted, it is still going.
        rejection = ((float(last["high"]) - float(last["close"])) / last_range if push_up
                     else (float(last["close"]) - float(last["low"])) / last_range)
        if rejection < 0.3:
            continue

        buffer = ctx.atr * 0.3
        zone = ((extreme - buffer, extreme + buffer * 0.4) if direction == "bullish"
                else (extreme - buffer * 0.4, extreme + buffer))
        # The objective is where the climax candle came from.
        origin = float(last["open"])
        if abs(origin - extreme) < MIN_MOVE:
            origin = (extreme - MIN_MOVE * 1.2 if direction == "bearish"
                      else extreme + MIN_MOVE * 1.2)

        conviction = base.mix(
            (0.28, base.clamp((range_ratio - 2.0) / 2.0, 0.0, 1.0)),
            (0.24, base.clamp((volume_ratio - 1.5) / 1.5, 0.0, 1.0)),
            (0.24, base.clamp(rejection / 0.6, 0.0, 1.0)),
            (0.24, base.clamp(float(level.get("strength", 50)) / 100, 0.0, 1.0)),
        )
        out.append(Candidate(
            key="climax_reversal", name="Exhaustion Climax", family=PATTERN,
            direction=direction, trigger_family="reversal",
            label=base.label_for("Exhaustion Reversal", direction),
            conviction=conviction, alignment=COUNTER,
            zone=zone, preferred=extreme,
            stop=base.stop_beyond(extreme, direction, ctx.atr * 0.45),
            stop_basis="beyond the climax candle's extreme",
            targets=[{"price": origin, "basis": "where the climax candle began"}],
            sources=[ZoneHint("climax", timeframe, zone[0], zone[1],
                              conviction, f"{timeframe} climax at {extreme:.2f}")],
            evidence=[f"{timeframe} candle {range_ratio:.1f}x the recent range "
                      f"on {volume_ratio:.1f}x volume",
                      f"closed {rejection * 100:.0f}% back off its extreme",
                      f"into {wanted} at {float(level['price']):.2f} "
                      f"(strength {level.get('strength', 0):.0f})"],
        ))
    return out


# --- gaps -------------------------------------------------------------------


@base.strategy("gap_fill", "Session Gap Fill", PATTERN,
               alignment=EITHER, min_conviction=55)
def gap_fill(ctx: Context) -> list[Candidate]:
    """The weekend or session gap, traded back towards the prior close.

    Gold gaps at the Sunday open often enough to be worth a standing rule, and
    the gap fills far more often than it extends. Small gaps are ignored: the
    trade only exists if the distance back to the prior close clears the
    day-trading minimum on its own.
    """
    df = ctx.frame("H1")
    if df is None or len(df) < 6:
        return []

    # Find the largest opening gap in the recent past, measured between one
    # candle's close and the next candle's open.
    recent = df.tail(48)
    opens = recent["open"].astype(float).to_numpy()
    closes = recent["close"].astype(float).to_numpy()
    if len(opens) < 3:
        return []
    gaps = opens[1:] - closes[:-1]
    index = int(np.argmax(np.abs(gaps)))
    gap = float(gaps[index])
    if abs(gap) < MIN_MOVE:
        return []

    prior_close = float(closes[index])
    gap_open = float(opens[index + 1])
    # Already filled? Then there is nothing left to trade.
    since = recent.iloc[index + 1:]
    if since.empty:
        return []
    if gap > 0 and float(since["low"].min()) <= prior_close:
        return []
    if gap < 0 and float(since["high"].max()) >= prior_close:
        return []

    # A gap up fills downwards, and the other way round.
    direction = "bearish" if gap > 0 else "bullish"
    if abs(ctx.price - prior_close) < MIN_MOVE:
        return []
    # Price should still be the gap's side of the prior close.
    if direction == "bearish" and ctx.price <= prior_close:
        return []
    if direction == "bullish" and ctx.price >= prior_close:
        return []

    buffer = ctx.atr * 0.35
    zone = ((gap_open - buffer * 0.4, gap_open + buffer) if direction == "bullish"
            else (gap_open - buffer, gap_open + buffer * 0.4))
    filled_so_far = base.clamp(
        1.0 - abs(ctx.price - prior_close) / max(abs(gap), 1e-9), 0.0, 1.0)

    conviction = base.mix(
        (0.34, base.clamp(abs(gap) / (MIN_MOVE * 2), 0.2, 1.0)),
        (0.24, 1.0 - filled_so_far),
        (0.22, 1.0 if ctx.htf_agrees(direction) else 0.5),
        (0.20, 1.0 if ctx.clear_run(direction, abs(ctx.price - prior_close) * 0.7)
         else 0.45),
    )
    return [Candidate(
        key="gap_fill", name="Session Gap Fill", family=PATTERN,
        direction=direction, trigger_family="reversal",
        label=base.label_for("Gap Fill", direction),
        conviction=conviction, alignment=EITHER,
        zone=zone, preferred=gap_open,
        stop=base.stop_beyond(gap_open, direction, abs(gap) * 0.5 + ctx.atr * 0.3),
        stop_basis="beyond the gap's open, where the gap would be extending",
        targets=[{"price": prior_close, "basis": "the close the gap left behind"},
                 {"price": (prior_close + (gap_open - prior_close) * 0.5),
                  "basis": "halfway back into the gap"}],
        sources=[ZoneHint("gap", "H1", zone[0], zone[1], 65,
                          f"gap of {abs(gap) / PIP:.0f} pips from {prior_close:.2f}")],
        evidence=[f"{abs(gap) / PIP:.0f}-pip gap "
                  f"{'up' if gap > 0 else 'down'} from {prior_close:.2f}",
                  f"{filled_so_far * 100:.0f}% of the gap has closed so far",
                  f"{abs(ctx.price - prior_close) / PIP:.0f} pips remain to the "
                  f"prior close"],
    )]
