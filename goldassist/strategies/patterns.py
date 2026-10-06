"""Classical price-action and momentum strategies.

``divergence``        momentum failing to confirm a new price extreme
``double_extreme``    double top / double bottom with a measured objective
``round_number``      Gold's reaction to whole-number magnets
``fib_pocket``        the 0.618-0.786 retracement band of the live impulse
``pattern_at_level``  an engulfing / pin bar printed exactly at a key level
``prev_day_level``    yesterday's high or low defended or reclaimed

These are the oldest ideas in the book and they remain in it because they keep
working on Gold, which respects round numbers and previous-day extremes far
more than most instruments.
"""

from __future__ import annotations

from . import base
from .base import Candidate, Context, ZoneHint, PATTERN, EITHER, COUNTER, WITH_TREND

PIP = 0.01

# Candles that count as a genuine rejection rather than noise.
REJECTION_PATTERNS = {
    "hammer", "bullish pin bar", "bearish pin bar", "shooting star",
    "inverted hammer", "bullish engulfing", "bearish engulfing",
    "morning star", "evening star", "failed breakout candle",
    "failed breakdown candle", "tweezer top", "tweezer bottom",
}


def _swings(ctx: Context, timeframe: str, kind: str) -> list[dict]:
    """Swing points of one kind, oldest first."""
    swings = (ctx.struct(timeframe).get("swings") or [])
    return [s for s in swings if s.get("kind") == kind]


# --- divergence -------------------------------------------------------------


@base.strategy("divergence", "Momentum Divergence", PATTERN,
               alignment=COUNTER, min_conviction=55)
def divergence(ctx: Context) -> list[Candidate]:
    """A new price extreme that RSI refuses to confirm."""
    from ..analysis import indicators   # local: keeps import cost off the hot path

    out: list[Candidate] = []
    for timeframe in ("M15", "H1"):
        df = ctx.frame(timeframe)
        if df is None or len(df) < 60:
            continue
        rsi = indicators.rsi(df["close"])
        atr_tf = ctx.atr_of(timeframe)

        for direction, kind in (("bullish", "low"), ("bearish", "high")):
            points = _swings(ctx, timeframe, kind)
            if len(points) < 2:
                continue
            latest, previous = points[-1], points[-2]
            i_new, i_old = int(latest.get("index", -1)), int(previous.get("index", -1))
            if not (0 <= i_old < i_new < len(rsi)):
                continue
            if i_new - i_old < 3:
                continue

            price_new, price_old = float(latest["price"]), float(previous["price"])
            rsi_new, rsi_old = float(rsi.iloc[i_new]), float(rsi.iloc[i_old])

            if direction == "bullish":
                # Lower low in price, higher low in RSI.
                if not (price_new < price_old and rsi_new > rsi_old):
                    continue
            else:
                if not (price_new > price_old and rsi_new < rsi_old):
                    continue

            # The divergence is only tradeable while price is still near it.
            if ctx.distance_atr(price_new) > 2.0:
                continue

            gap = abs(rsi_new - rsi_old)
            confirmed = bool(ctx.events("M5", direction, ("CHoCH", "MSS"))
                             or ctx.events(timeframe, direction, ("CHoCH", "MSS")))
            low, high = base.band(price_new, atr_tf * 0.5)
            if direction == "bullish":
                low = min(low, price_new)
            else:
                high = max(high, price_new)

            conviction = base.mix(
                (0.30, base.clamp(gap / 12.0, 0.0, 1.0)),
                (0.25, 1.0 if confirmed else 0.45),
                (0.20, {"H1": 1.0, "M15": 0.8}.get(timeframe, 0.7)),
                (0.15, 1.0 if ctx.patterns(timeframe, direction) else 0.5),
                (0.10, base.clamp(1.0 - ctx.distance_atr(price_new) / 2.0, 0.0, 1.0)),
            )
            out.append(Candidate(
                key="divergence", name="Momentum Divergence", family=PATTERN,
                direction=direction, trigger_family="reversal",
                label=base.label_for("RSI Divergence", direction),
                conviction=conviction, alignment=COUNTER,
                zone=(low, high), preferred=price_new,
                stop=base.stop_beyond(price_new, direction, atr_tf * 0.6),
                stop_basis=f"beyond the {timeframe} swing that made the divergence",
                sources=[ZoneHint("divergence swing", timeframe, low, high, 55.0,
                                  f"{timeframe} swing {kind} {price_new:.2f}")],
                evidence=[f"{timeframe} price made a "
                          f"{'lower low' if direction == 'bullish' else 'higher high'} "
                          f"at {price_new:.2f} while RSI made a "
                          f"{'higher low' if direction == 'bullish' else 'lower high'} "
                          f"({rsi_old:.0f} to {rsi_new:.0f})",
                          "structure has since shifted the same way" if confirmed
                          else "waiting for a structure shift to confirm it"],
            ))
    return out


# --- double top / bottom ----------------------------------------------------


@base.strategy("double_extreme", "Double Top / Bottom", PATTERN,
               alignment=COUNTER, min_conviction=55)
def double_extreme(ctx: Context) -> list[Candidate]:
    """Two failures at the same price, with the neckline as the objective."""
    out: list[Candidate] = []
    for timeframe in ("M15", "H1"):
        atr_tf = ctx.atr_of(timeframe)
        tolerance = atr_tf * 0.35

        for direction, kind in (("bearish", "high"), ("bullish", "low")):
            points = _swings(ctx, timeframe, kind)
            if len(points) < 2:
                continue
            latest, previous = points[-1], points[-2]
            price_new, price_old = float(latest["price"]), float(previous["price"])
            if abs(price_new - price_old) > tolerance:
                continue
            if int(latest.get("index", 0)) - int(previous.get("index", 0)) < 4:
                continue
            level = (price_new + price_old) / 2
            if ctx.distance_atr(level) > 1.8:
                continue

            # The neckline is the opposite swing sitting between the two.
            opposite_kind = "low" if kind == "high" else "high"
            between = [
                s for s in _swings(ctx, timeframe, opposite_kind)
                if int(previous.get("index", 0)) < int(s.get("index", 0)) < int(latest.get("index", 0))
            ]
            if not between:
                continue
            neckline = float(min(between, key=lambda s: abs(float(s["price"]) - level))["price"])
            height = abs(level - neckline)
            if height < 300 * PIP:
                continue      # too shallow to pay for the risk

            low, high = base.band(level, tolerance)
            rejection = bool(ctx.patterns(timeframe, direction))
            conviction = base.mix(
                (0.30, base.clamp(1.0 - abs(price_new - price_old) / tolerance, 0.0, 1.0)),
                (0.25, base.clamp(height / (atr_tf * 2.0), 0.0, 1.0)),
                (0.20, 1.0 if rejection else 0.5),
                (0.15, {"H1": 1.0, "M15": 0.8}.get(timeframe, 0.7)),
                (0.10, 1.0 if ctx.events("M5", direction, ("CHoCH", "MSS", "BOS")) else 0.5),
            )
            projected = neckline - height if direction == "bearish" else neckline + height
            name = "Double Top" if kind == "high" else "Double Bottom"
            out.append(Candidate(
                key="double_extreme", name="Double Top / Bottom", family=PATTERN,
                direction=direction, trigger_family="reversal",
                label=f"{name} ({timeframe})",
                conviction=conviction, alignment=COUNTER,
                zone=(low, high), preferred=level,
                stop=base.stop_beyond(max(price_new, price_old) if kind == "high"
                                      else min(price_new, price_old),
                                      direction, atr_tf * 0.4),
                stop_basis=f"beyond the {timeframe} {name.lower()}",
                targets=[{"price": neckline, "basis": f"{timeframe} neckline"},
                         {"price": projected,
                          "basis": f"measured move from the {name.lower()} "
                                   f"({height / PIP:.0f} pips)"}],
                sources=[ZoneHint(name.lower(), timeframe, low, high, 60.0,
                                  f"{timeframe} {name.lower()} {level:.2f}")],
                evidence=[f"{timeframe} {name.lower()} at {level:.2f} "
                          f"(two swings {abs(price_new - price_old) / PIP:.0f} pips apart)",
                          f"neckline at {neckline:.2f}, {height / PIP:.0f} pips away",
                          "rejection candle at the second test" if rejection
                          else "no rejection candle yet"],
            ))
    return out


# --- round numbers ----------------------------------------------------------


@base.strategy("round_number", "Round Number Reaction", PATTERN,
               alignment=EITHER, min_conviction=52)
def round_number(ctx: Context) -> list[Candidate]:
    """Gold's reaction at whole-number magnets.

    Stops cluster at 4400.00 in a way they never do at 4397.30. The level only
    becomes a setup when price has actually reacted to it, so a rejection
    candle or a structure turn is required -- proximity alone is not evidence.
    """
    magnitudes = ((10.0, 1.0), (5.0, 0.7))
    out: list[Candidate] = []
    tolerance = ctx.atr * 0.25

    for step, weight in magnitudes:
        level = round(ctx.price / step) * step
        if abs(ctx.price - level) > tolerance:
            continue
        for direction in ("bullish", "bearish"):
            rejection = [p for p in ctx.patterns("M15", direction) + ctx.patterns("M5", direction)
                         if p.get("name") in REJECTION_PATTERNS]
            turned = bool(ctx.events("M5", direction, ("CHoCH", "MSS", "BOS")))
            if not rejection and not turned:
                continue
            # Buying a round number means it is holding as support: price must
            # be on the correct side of it.
            if direction == "bullish" and ctx.price < level - tolerance * 0.5:
                continue
            if direction == "bearish" and ctx.price > level + tolerance * 0.5:
                continue

            low, high = base.band(level, tolerance)
            conviction = base.mix(
                (0.30, weight),
                (0.25, 1.0 if rejection else 0.5),
                (0.25, 1.0 if turned else 0.5),
                (0.20, 1.0 if ctx.htf_agrees(direction) else 0.5),
            )
            out.append(Candidate(
                key="round_number", name="Round Number Reaction", family=PATTERN,
                direction=direction, trigger_family="range",
                label=base.label_for(f"{level:.0f} Reaction", direction),
                conviction=conviction, alignment=EITHER,
                zone=(low, high), preferred=level,
                stop=base.stop_beyond(level, direction, tolerance + ctx.atr * 0.4),
                stop_basis=f"beyond the {level:.0f} handle",
                sources=[ZoneHint("round number", "D1", low, high, 50.0,
                                  f"{level:.0f} handle")],
                evidence=[f"price is at the {level:.0f} handle",
                          (f"{rejection[0]['name']} printed there" if rejection
                           else "M5 structure turned there")],
            ))
        break     # only the nearest magnitude is the level in play
    return out


# --- fibonacci --------------------------------------------------------------


@base.strategy("fib_pocket", "Fibonacci Pocket", PATTERN,
               alignment=WITH_TREND, min_conviction=50)
def fib_pocket(ctx: Context) -> list[Candidate]:
    """The 0.618-0.786 retracement of the live impulse leg."""
    fib = (ctx.levels.get("fibonacci") or {})
    band_prices = fib.get("optimal_entry_zone")
    direction = fib.get("direction")
    if not band_prices or len(band_prices) != 2 or direction not in ("bullish", "bearish"):
        return []

    low, high = float(min(band_prices)), float(max(band_prices))
    if direction == "bullish" and low > ctx.price + ctx.atr * 0.3:
        return []
    if direction == "bearish" and high < ctx.price - ctx.atr * 0.3:
        return []
    edge = 0.0 if low <= ctx.price <= high else min(abs(ctx.price - low),
                                                    abs(ctx.price - high))
    if edge > ctx.atr * 2.5:
        return []

    anchor_high = float(fib.get("anchor_high") or 0)
    anchor_low = float(fib.get("anchor_low") or 0)
    leg = abs(anchor_high - anchor_low)
    extensions = fib.get("extensions") or {}
    targets = []
    for ratio in ("1.272", "1.618"):
        value = extensions.get(ratio)
        if value:
            targets.append({"price": float(value), "basis": f"Fibonacci {ratio} extension"})

    conviction = base.mix(
        (0.30, base.clamp(1.0 - edge / (ctx.atr * 2.5), 0.0, 1.0)),
        (0.25, 1.0 if ctx.htf_agrees(direction) else 0.4),
        (0.25, base.clamp(leg / (ctx.atr * 3.0), 0.0, 1.0)),
        (0.20, 1.0 if ctx.trend_agrees("H1", direction) else 0.5),
    )
    stop_anchor = anchor_low if direction == "bullish" else anchor_high
    return [Candidate(
        key="fib_pocket", name="Fibonacci Pocket", family=PATTERN,
        direction=direction, trigger_family="pullback",
        label=base.label_for("Fib Pocket", direction),
        conviction=conviction, alignment=WITH_TREND,
        zone=(low, high),
        preferred=low + (high - low) * (0.5 if direction == "bullish" else 0.5),
        stop=base.stop_beyond(stop_anchor, direction, ctx.atr * 0.3),
        stop_basis="beyond the origin of the impulse leg",
        targets=targets,
        sources=[ZoneHint("fibonacci", "H4", low, high, 55.0,
                          "0.618-0.786 retracement band")],
        evidence=[f"0.618-0.786 band of the {direction} leg "
                  f"{anchor_low:.2f}-{anchor_high:.2f} sits at {low:.2f}-{high:.2f}",
                  f"the impulse measured {leg / PIP:.0f} pips"],
    )]


# --- candles at levels ------------------------------------------------------


@base.strategy("pattern_at_level", "Rejection At Level", PATTERN,
               alignment=EITHER, min_conviction=52)
def pattern_at_level(ctx: Context) -> list[Candidate]:
    """A rejection candle printed exactly where it matters."""
    out: list[Candidate] = []
    for timeframe in ("M15", "M5"):
        for direction in ("bullish", "bearish"):
            candidates = [p for p in ctx.patterns(timeframe, direction)
                          if p.get("at_level") and p.get("name") in REJECTION_PATTERNS
                          and p.get("low") is not None and p.get("high") is not None]
            if not candidates:
                continue
            best = max(candidates, key=lambda p: p.get("weight", 0))
            low, high = float(best["low"]), float(best["high"])
            if high <= low:
                continue
            if ctx.distance_atr((low + high) / 2) > 1.5:
                continue

            atr_tf = ctx.atr_of(timeframe)
            # Enter from the half of the candle that rejected.
            if direction == "bullish":
                zone = (low, low + (high - low) * 0.5)
            else:
                zone = (high - (high - low) * 0.5, high)

            conviction = base.mix(
                (0.30, base.clamp(float(best.get("strength") or 0), 0.0, 1.0)),
                (0.25, base.clamp(float(best.get("weight") or 0) / 1.5, 0.0, 1.0)),
                (0.25, 1.0 if ctx.events("M5", direction, ("CHoCH", "MSS", "BOS")) else 0.5),
                (0.20, 1.0 if ctx.htf_agrees(direction) else 0.5),
            )
            out.append(Candidate(
                key="pattern_at_level", name="Rejection At Level", family=PATTERN,
                direction=direction, trigger_family="reversal",
                label=base.label_for(str(best.get("name", "rejection")).title(), direction),
                conviction=conviction, alignment=EITHER,
                zone=zone, preferred=(zone[0] + zone[1]) / 2,
                stop=base.stop_beyond(low if direction == "bullish" else high,
                                      direction, atr_tf * 0.3),
                stop_basis="beyond the rejection candle",
                sources=[ZoneHint("rejection candle", timeframe, zone[0], zone[1],
                                  60.0, f"{timeframe} {best.get('name')}")],
                evidence=[f"{timeframe} {best.get('name')} at a key level "
                          f"({best.get('note', '')})"],
            ))
            break
    return out


# --- previous day extremes --------------------------------------------------


@base.strategy("prev_day_level", "Previous Day Extreme", PATTERN,
               alignment=EITHER, min_conviction=52)
def prev_day_level(ctx: Context) -> list[Candidate]:
    """Yesterday's high or low, defended or reclaimed."""
    out: list[Candidate] = []
    tolerance = ctx.atr * 0.35

    for level in (ctx.levels.get("levels") or []):
        if level.get("source") not in ("PDH", "PDL", "PWH", "PWL"):
            continue
        price = float(level["price"])
        if abs(ctx.price - price) > tolerance:
            continue

        # Above the level it is support; below it, resistance.
        direction = "bullish" if ctx.price >= price else "bearish"
        rejection = [p for p in ctx.patterns("M15", direction) + ctx.patterns("M5", direction)
                     if p.get("name") in REJECTION_PATTERNS]
        turned = bool(ctx.events("M5", direction, ("CHoCH", "MSS", "BOS")))
        if not rejection and not turned:
            continue

        low, high = base.band(price, tolerance)
        conviction = base.mix(
            (0.30, float(level.get("strength", 50)) / 100),
            (0.25, 1.0 if rejection else 0.5),
            (0.25, 1.0 if turned else 0.5),
            (0.20, 1.0 if ctx.htf_agrees(direction) else 0.5),
        )
        out.append(Candidate(
            key="prev_day_level", name="Previous Day Extreme", family=PATTERN,
            direction=direction, trigger_family="range",
            label=base.label_for(f"{level['source']} Reaction", direction),
            conviction=conviction, alignment=EITHER,
            zone=(low, high), preferred=price,
            stop=base.stop_beyond(price, direction, tolerance + ctx.atr * 0.35),
            stop_basis=f"beyond {level['source']}",
            sources=[ZoneHint(str(level["source"]), level.get("timeframe", "D1"),
                              low, high, float(level.get("strength", 50)),
                              f"{level['source']} {price:.2f}")],
            evidence=[f"price is at {level['source']} ({price:.2f}), "
                      f"strength {float(level.get('strength', 0)):.0f}/100",
                      (f"{rejection[0]['name']} printed there" if rejection
                       else "M5 structure turned there")],
        ))
        break     # the nearest previous-day extreme is the one in play
    return out
