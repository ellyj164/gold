"""Trend strategies: ways of joining a move that is already working.

``confluence_setup``  the original engine read, kept as one strategy of many
``ema_pullback``      a pullback into the moving-average ribbon
``supertrend_ride``   a retest of the Supertrend line while it holds
``vwap_pullback``     a return to session VWAP inside the day's direction
``momentum_ride``     joining an ADX-confirmed expansion on a shallow dip

The first one matters for a different reason to the rest. Before this package
existed, the whole application produced exactly what ``confluence_setup``
produces -- one idea per direction, out of the confluence score. Keeping it as
a registered strategy means nothing the app used to find is lost; everything
else here is new ground it could not previously see.
"""

from __future__ import annotations

from . import base
from .base import Candidate, Context, ZoneHint, TREND, EITHER, WITH_TREND, COUNTER


@base.strategy("confluence_setup", "Confluence Read", TREND,
               alignment=EITHER, min_conviction=45)
def confluence_setup(ctx: Context) -> list[Candidate]:
    """The multi-factor confluence score, expressed as a setup.

    Entry zone is left unset on purpose: the overlap engine in
    ``signals.entry`` derives it from every technical area at once, which is
    exactly the right behaviour for a read that is not built on one pattern.
    """
    from ..signals import classify   # imported late: signals imports strategies

    out: list[Candidate] = []
    watch = float(getattr(ctx.profile, "watch_score", 50))
    for direction in ("bullish", "bearish"):
        score = ctx.side_score(direction)
        if score < watch:
            continue
        kind = classify.setup_type(ctx.analysis, direction)
        side = (ctx.decision.get("confluence") or {}).get(direction) or {}
        evidence = [e["detail"] for e in (side.get("evidence") or [])[:4]]
        out.append(Candidate(
            key="confluence_setup", name="Confluence Read", family=TREND,
            direction=direction, trigger_family=kind,
            label=classify.label(kind, direction),
            conviction=score, alignment=EITHER,
            evidence=evidence or [f"confluence score {score:.0f}/100"],
        ))
    return out


# --- moving average ribbon --------------------------------------------------


@base.strategy("ema_pullback", "EMA Ribbon Pullback", TREND,
               alignment=WITH_TREND, min_conviction=52)
def ema_pullback(ctx: Context) -> list[Candidate]:
    """A pullback into the 20/50 EMA band while the ribbon stays stacked."""
    out: list[Candidate] = []
    for tf in ("H1", "M15"):
        ema = ctx.ind(tf, "ema")
        values = ema.get("value") or {}
        fast, slow = values.get("ema20"), values.get("ema50")
        if not fast or not slow:
            continue
        direction = ("bullish" if ema.get("bias") == "bullish"
                     else "bearish" if ema.get("bias") == "bearish" else None)
        if direction is None or not ctx.trend_agrees(tf, direction):
            continue

        atr_tf = ctx.atr_of(tf)
        low, high = min(fast, slow), max(fast, slow)
        # Widen a collapsed ribbon so the zone is executable.
        if high - low < atr_tf * 0.3:
            low, high = base.band((low + high) / 2, atr_tf * 0.3)
        if direction == "bullish" and low > ctx.price + atr_tf * 0.2:
            continue
        if direction == "bearish" and high < ctx.price - atr_tf * 0.2:
            continue
        if min(abs(ctx.price - low), abs(ctx.price - high)) > atr_tf * 2.0:
            continue

        adx = ctx.ind(tf, "adx")
        rsi = ctx.ind(tf, "rsi").get("value") or 50
        # A pullback is only a pullback while momentum has not flipped outright.
        exhausted = (direction == "bullish" and rsi < 35) or (direction == "bearish" and rsi > 65)
        conviction = base.mix(
            (0.30, 1.0 if "stack" in str(ema.get("note", "")) else 0.55),
            (0.25, base.clamp(float(adx.get("value") or 0) / 30.0, 0.0, 1.0)),
            (0.20, base.clamp(1.0 - min(abs(ctx.price - low), abs(ctx.price - high))
                              / (atr_tf * 2.0), 0.0, 1.0)),
            (0.15, 1.0 if ctx.htf_agrees(direction) else 0.45),
            (0.10, 0.2 if exhausted else 1.0),
        )
        stop_anchor = low if direction == "bullish" else high
        out.append(Candidate(
            key="ema_pullback", name="EMA Ribbon Pullback", family=TREND,
            direction=direction, trigger_family="pullback",
            label=base.label_for("EMA Pullback", direction),
            conviction=conviction, alignment=WITH_TREND,
            zone=(low, high), preferred=(low + high) / 2,
            stop=base.stop_beyond(stop_anchor, direction, atr_tf * 0.9),
            stop_basis=f"beyond the {tf} 50 EMA",
            sources=[ZoneHint("EMA band", tf, low, high, 55.0, f"{tf} 20/50 EMA band")],
            evidence=[f"{tf} EMA ribbon {ema.get('note', '')}",
                      f"{tf} structure is {ctx.struct(tf).get('trend', 'unclear')}",
                      f"{tf} {adx.get('note', 'ADX unavailable')}"],
        ))
    return out


# --- supertrend -------------------------------------------------------------


@base.strategy("supertrend_ride", "Supertrend Retest", TREND,
               alignment=WITH_TREND, min_conviction=52)
def supertrend_ride(ctx: Context) -> list[Candidate]:
    """Price pulling back to a Supertrend line that is still holding."""
    out: list[Candidate] = []
    for tf in ("H1", "M15"):
        st = ctx.ind(tf, "supertrend")
        line = st.get("value")
        direction = st.get("bias")
        if not line or direction not in ("bullish", "bearish"):
            continue
        # The line must be trailing the correct side of price.
        if direction == "bullish" and line >= ctx.price:
            continue
        if direction == "bearish" and line <= ctx.price:
            continue

        atr_tf = ctx.atr_of(tf)
        distance = abs(ctx.price - float(line)) / atr_tf
        if distance > 1.6:
            continue      # too far from the line to call this a retest

        higher = ctx.ind("H1" if tf == "M15" else "H4", "supertrend").get("bias")
        low, high = base.band(float(line), atr_tf * 0.45)
        if direction == "bullish":
            high = min(high, ctx.price)
        else:
            low = max(low, ctx.price)
        if high <= low:
            continue

        conviction = base.mix(
            (0.30, base.clamp(1.0 - distance / 1.6, 0.0, 1.0)),
            (0.25, 1.0 if higher == direction else 0.4),
            (0.25, 1.0 if ctx.trend_agrees(tf, direction) else 0.5),
            (0.20, base.clamp(float(ctx.ind(tf, "adx").get("value") or 0) / 30.0,
                              0.0, 1.0)),
        )
        out.append(Candidate(
            key="supertrend_ride", name="Supertrend Retest", family=TREND,
            direction=direction, trigger_family="continuation",
            label=base.label_for("Supertrend Retest", direction),
            conviction=conviction, alignment=WITH_TREND,
            zone=(low, high), preferred=(low + high) / 2,
            stop=base.stop_beyond(float(line), direction, atr_tf * 0.7),
            stop_basis=f"beyond the {tf} Supertrend line",
            sources=[ZoneHint("Supertrend", tf, low, high, 50.0,
                              f"{tf} Supertrend at {float(line):.2f}")],
            evidence=[f"{tf} Supertrend {st.get('note', '')} at {float(line):.2f}",
                      f"price is {distance:.1f} ATR from the line",
                      f"higher timeframe Supertrend is {higher or 'unavailable'}"],
        ))
    return out


# --- VWAP -------------------------------------------------------------------


@base.strategy("vwap_pullback", "VWAP Pullback", TREND,
               alignment=WITH_TREND, min_conviction=50)
def vwap_pullback(ctx: Context) -> list[Candidate]:
    """A return to session VWAP with the day's direction still intact."""
    out: list[Candidate] = []
    for tf in ("M15", "H1"):
        vwap = ctx.ind(tf, "vwap").get("value")
        if not vwap:
            continue
        atr_tf = ctx.atr_of(tf)
        direction = "bullish" if ctx.price > vwap else "bearish"
        if not ctx.trend_agrees("H1", direction) and not ctx.htf_agrees(direction):
            continue
        distance = abs(ctx.price - float(vwap)) / atr_tf
        if distance > 2.0:
            continue      # too extended for a pullback entry; see vwap_reversion

        low, high = base.band(float(vwap), atr_tf * 0.4)
        conviction = base.mix(
            (0.30, base.clamp(1.0 - distance / 2.0, 0.0, 1.0)),
            (0.25, 1.0 if ctx.trend_agrees("H1", direction) else 0.5),
            (0.25, 1.0 if ctx.htf_agrees(direction) else 0.45),
            (0.20, base.clamp(float(ctx.ind(tf, "adx").get("value") or 0) / 28.0,
                              0.0, 1.0)),
        )
        out.append(Candidate(
            key="vwap_pullback", name="VWAP Pullback", family=TREND,
            direction=direction, trigger_family="pullback",
            label=base.label_for("VWAP Pullback", direction),
            conviction=conviction, alignment=WITH_TREND,
            zone=(low, high), preferred=float(vwap),
            stop=base.stop_beyond(float(vwap), direction, atr_tf * 0.9),
            stop_basis=f"beyond session VWAP on {tf}",
            sources=[ZoneHint("VWAP", tf, low, high, 50.0,
                              f"{tf} session VWAP {float(vwap):.2f}")],
            evidence=[f"session VWAP at {float(vwap):.2f}, price {distance:.1f} ATR "
                      f"{'above' if direction == 'bullish' else 'below'} it",
                      f"H1 structure is {ctx.struct('H1').get('trend', 'unclear')}"],
        ))
        break     # one VWAP idea is enough; M15 is preferred when both exist
    return out


@base.strategy("vwap_reversion", "VWAP Reversion", TREND,
               alignment=COUNTER, min_conviction=55)
def vwap_reversion(ctx: Context) -> list[Candidate]:
    """Price stretched far from VWAP with momentum turning, reverting to it."""
    vwap = ctx.ind("M15", "vwap").get("value") or ctx.ind("H1", "vwap").get("value")
    if not vwap:
        return []
    atr_tf = ctx.atr_of("M15")
    stretch = abs(ctx.price - float(vwap)) / atr_tf
    if stretch < 2.5:
        return []

    # Fade the stretch: if price is far above VWAP the trade is short.
    direction = "bearish" if ctx.price > float(vwap) else "bullish"
    rsi = float(ctx.ind("M15", "rsi").get("value") or 50)
    extreme = rsi >= 68 if direction == "bearish" else rsi <= 32
    if not extreme:
        return []
    if ctx.trend_agrees("H4", base.opposite(direction)) and stretch < 3.5:
        return []      # a strong H4 trend needs more stretch before fading it

    low, high = base.band(ctx.price, atr_tf * 0.4)
    stop = base.stop_beyond(ctx.price, base.opposite(direction), atr_tf * 1.2)
    conviction = base.mix(
        (0.35, base.clamp((stretch - 2.5) / 2.0, 0.0, 1.0)),
        (0.30, base.clamp(abs(rsi - 50) / 25.0, 0.0, 1.0)),
        (0.20, 1.0 if ctx.regime in ("range", "consolidation", "potential reversal") else 0.5),
        (0.15, 1.0 if ctx.patterns("M15", direction) else 0.5),
    )
    return [Candidate(
        key="vwap_reversion", name="VWAP Reversion", family=TREND,
        direction=direction, trigger_family="reversal",
        label=base.label_for("VWAP Reversion", direction),
        conviction=conviction, alignment=COUNTER,
        zone=(low, high), preferred=ctx.price,
        stop=stop, stop_basis="1.2 ATR beyond the extreme",
        targets=[{"price": float(vwap), "basis": "session VWAP"}],
        sources=[ZoneHint("stretch", "M15", low, high, 45.0, "M15 VWAP stretch")],
        evidence=[f"price is {stretch:.1f} ATR from session VWAP at {float(vwap):.2f}",
                  f"M15 RSI {rsi:.0f}",
                  f"market regime is {ctx.regime or 'unclassified'}"],
    )]


# --- momentum ---------------------------------------------------------------


@base.strategy("momentum_ride", "Momentum Expansion", TREND,
               alignment=WITH_TREND, min_conviction=54)
def momentum_ride(ctx: Context) -> list[Candidate]:
    """Joining a confirmed expansion on the first shallow dip."""
    adx = ctx.ind("M15", "adx")
    if not adx.get("trending") or adx.get("bias") not in ("bullish", "bearish"):
        return []
    direction = adx["bias"]
    if not (ctx.trend_agrees("M15", direction) or ctx.trend_agrees("H1", direction)):
        return []

    macd = ctx.ind("M15", "macd")
    if macd.get("bias") != direction:
        return []

    atr_tf = ctx.atr_of("M15")
    if direction == "bullish":
        low, high = ctx.price - atr_tf * 0.9, ctx.price - atr_tf * 0.15
    else:
        low, high = ctx.price + atr_tf * 0.15, ctx.price + atr_tf * 0.9

    swing_key = "last_swing_low" if direction == "bullish" else "last_swing_high"
    swing = (ctx.struct("M15") or {}).get(swing_key) or {}
    anchor = float(swing.get("price") or 0.0)
    stop = (base.stop_beyond(anchor, direction, atr_tf * 0.3) if anchor
            else base.stop_beyond(low if direction == "bullish" else high,
                                  direction, atr_tf * 1.0))
    if direction == "bullish" and stop >= low:
        stop = low - atr_tf * 0.9
    if direction == "bearish" and stop <= high:
        stop = high + atr_tf * 0.9

    volume = ctx.ind("M15", "volume")
    conviction = base.mix(
        (0.30, base.clamp(float(adx.get("value") or 0) / 35.0, 0.0, 1.0)),
        (0.20, 1.0 if ctx.regime == "expansion" else 0.55),
        (0.20, 1.0 if ctx.htf_agrees(direction) else 0.45),
        (0.15, 1.0 if volume.get("expanding") else 0.5),
        (0.15, 1.0 if ctx.events("M5", direction, ("BOS", "MSS")) else 0.5),
    )
    return [Candidate(
        key="momentum_ride", name="Momentum Expansion", family=TREND,
        direction=direction, trigger_family="continuation",
        label=base.label_for("Momentum Continuation", direction),
        conviction=conviction, alignment=WITH_TREND,
        zone=(low, high), preferred=(low + high) / 2,
        stop=stop, stop_basis="beyond the last M15 swing",
        sources=[ZoneHint("dip", "M15", low, high, 50.0, "M15 shallow pullback")],
        evidence=[f"M15 {adx.get('note', '')} with DI aligned {direction}",
                  f"M15 MACD {macd.get('note', '')}",
                  f"regime is {ctx.regime or 'unclassified'}",
                  f"volume {volume.get('note', 'unavailable')}"],
    )]
