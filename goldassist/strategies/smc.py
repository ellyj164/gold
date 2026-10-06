"""Smart-money strategies: where institutional footprints leave a tradeable edge.

Five patterns, each with its own detector because each one is a different
trade with a different stop, a different confirmation and a different reason
to fail:

``zone_reaction``    price returning to an unspent supply/demand base
``ob_mitigation``    price returning to the order block that caused a break
``fvg_fill``         price rebalancing an unfilled imbalance
``bos_retest``       the broken level retested from the other side
``sweep_reversal``   stops taken beyond a level, then reclaimed
``liquidity_draw``   a clear run at untapped liquidity in front of price

They overlap on purpose. When two of them point at the same area the book
merges them into one setup and scores it higher -- which is exactly what a
trader means by confluence.
"""

from __future__ import annotations

from . import base
from .base import Candidate, Context, ZoneHint, SMC, EITHER, COUNTER

# Timeframes searched for an area, best-quality first regardless of order.
AREA_TIMEFRAMES = ("H4", "H1", "M30", "M15", "M5")


def _best_area(ctx: Context, summary_key: str, item_key: str,
               timeframes=AREA_TIMEFRAMES) -> dict | None:
    """The highest-quality area of one kind across the timeframes."""
    best = None
    for tf in timeframes:
        item = (ctx.tf(tf).get(summary_key) or {}).get(item_key)
        if not item:
            continue
        candidate = dict(item)
        candidate.setdefault("timeframe", tf)
        if best is None or candidate.get("quality", 0) > best.get("quality", 0):
            best = candidate
    return best


def _reachable(ctx: Context, low: float, high: float, direction: str,
               max_atr: float = 3.0) -> bool:
    """The area must be behind price (a pullback entry) and within reach."""
    if direction == "bullish" and low > ctx.price + ctx.atr * 0.35:
        return False
    if direction == "bearish" and high < ctx.price - ctx.atr * 0.35:
        return False
    edge = 0.0 if low <= ctx.price <= high else min(abs(ctx.price - low),
                                                    abs(ctx.price - high))
    return edge <= ctx.atr * max_atr


def _preferred(low: float, high: float, direction: str) -> float:
    """Deep-side fill: the cheapest third of a demand zone, the dearest of supply."""
    return low + (high - low) * (0.35 if direction == "bullish" else 0.65)


def _proximity(ctx: Context, low: float, high: float) -> float:
    """1.0 when price is in the area, decaying to 0 at three ATR away."""
    if low <= ctx.price <= high:
        return 1.0
    edge = min(abs(ctx.price - low), abs(ctx.price - high)) / ctx.atr
    return base.clamp(1.0 - edge / 3.0, 0.0, 1.0)


# --- supply / demand --------------------------------------------------------


@base.strategy("zone_reaction", "Supply/Demand Reaction", SMC,
               alignment=EITHER, min_conviction=52)
def zone_reaction(ctx: Context) -> list[Candidate]:
    """Price pulling back into the base an impulse originated from."""
    out: list[Candidate] = []
    for direction in ("bullish", "bearish"):
        key = "nearest_demand" if direction == "bullish" else "nearest_supply"
        area = _best_area(ctx, "zones", key, ("H4", "H1", "M15"))
        if not area:
            continue
        low, high = float(area["bottom"]), float(area["top"])
        if not _reachable(ctx, low, high, direction):
            continue

        tf = area.get("timeframe", "H1")
        fresh = bool(area.get("fresh"))
        tests = int(area.get("tests") or 0)
        departure = float(area.get("departure") or 0.0)
        conviction = base.mix(
            (0.35, float(area.get("quality", 0)) / 100),
            (0.20, _proximity(ctx, low, high)),
            (0.15, 1.0 if fresh else max(0.0, 1.0 - tests * 0.35)),
            (0.15, base.clamp(departure / 3.0, 0.0, 1.0)),
            (0.15, 1.0 if ctx.trend_agrees("H1", direction) else
                   0.6 if ctx.htf_agrees(direction) else 0.25),
        )
        kind = "demand" if direction == "bullish" else "supply"
        out.append(Candidate(
            key="zone_reaction", name="Supply/Demand Reaction", family=SMC,
            direction=direction, trigger_family="pullback",
            label=base.label_for(f"{kind.title()} Reaction", direction),
            conviction=conviction, alignment=EITHER,
            zone=(low, high), preferred=_preferred(low, high, direction),
            stop=base.stop_beyond(low if direction == "bullish" else high,
                                  direction, ctx.atr * 0.35),
            stop_basis=f"beyond the far edge of the {tf} {kind} zone",
            sources=[ZoneHint(kind, tf, low, high, float(area.get("quality", 0)),
                              f"{tf} {kind}" + (" (fresh)" if fresh else ""))],
            evidence=[f"{tf} {kind} {low:.2f}-{high:.2f}, quality "
                      f"{float(area.get('quality', 0)):.0f}/100"
                      + (", untested" if fresh else f", {tests} prior test(s)"),
                      f"impulse away from it measured {departure:.1f} ATR"],
        ))
    return out


# --- order blocks -----------------------------------------------------------


@base.strategy("ob_mitigation", "Order Block Mitigation", SMC,
               alignment=EITHER, min_conviction=52)
def ob_mitigation(ctx: Context) -> list[Candidate]:
    """Price returning to the last opposing candle before a displacement leg."""
    out: list[Candidate] = []
    for direction in ("bullish", "bearish"):
        key = "nearest_bullish" if direction == "bullish" else "nearest_bearish"
        block = _best_area(ctx, "order_blocks", key)
        if not block:
            continue
        low, high = float(block["bottom"]), float(block["top"])
        if not _reachable(ctx, low, high, direction):
            continue

        tf = block.get("timeframe", "H1")
        mitigation = float(block.get("mitigation_ratio") or 0.0)
        if mitigation >= 0.9:
            continue      # already spent; there is nothing left to react from
        conviction = base.mix(
            (0.30, float(block.get("quality", 0)) / 100),
            (0.20, _proximity(ctx, low, high)),
            (0.20, base.clamp(float(block.get("displacement") or 0) / 2.5, 0.0, 1.0)),
            (0.15, 1.0 - mitigation),
            (0.15, 1.0 if block.get("caused_bos") else 0.4),
        )
        extras = []
        if block.get("caused_bos"):
            extras.append("it caused the break of structure")
        if block.get("has_fvg"):
            extras.append("it left an imbalance behind it")
        out.append(Candidate(
            key="ob_mitigation", name="Order Block Mitigation", family=SMC,
            direction=direction, trigger_family="pullback",
            label=base.label_for("Order Block", direction),
            conviction=conviction, alignment=EITHER,
            zone=(low, high), preferred=_preferred(low, high, direction),
            stop=base.stop_beyond(low if direction == "bullish" else high,
                                  direction, ctx.atr * 0.3),
            stop_basis=f"beyond the {tf} order block",
            sources=[ZoneHint("order block", tf, low, high,
                              float(block.get("quality", 0)), f"{tf} order block")],
            evidence=[f"{tf} {direction} order block {low:.2f}-{high:.2f}, "
                      f"quality {float(block.get('quality', 0)):.0f}/100",
                      f"{mitigation:.0%} mitigated so far"] + extras,
        ))
    return out


# --- fair value gaps --------------------------------------------------------


@base.strategy("fvg_fill", "Imbalance Rebalance", SMC,
               alignment=EITHER, min_conviction=50)
def fvg_fill(ctx: Context) -> list[Candidate]:
    """Price coming back to rebalance an unfilled Fair Value Gap."""
    out: list[Candidate] = []
    for direction in ("bullish", "bearish"):
        key = "nearest_bullish" if direction == "bullish" else "nearest_bearish"
        gap = _best_area(ctx, "fvg", key)
        if not gap:
            continue
        low, high = float(gap["bottom"]), float(gap["top"])
        if not _reachable(ctx, low, high, direction, max_atr=2.5):
            continue
        fill_ratio = float(gap.get("fill_ratio") or 0.0)
        if fill_ratio >= 0.85:
            continue

        tf = gap.get("timeframe", "M15")
        size_atr = float(gap.get("size_atr") or 0.0)
        conviction = base.mix(
            (0.30, float(gap.get("quality", 0)) / 100),
            (0.20, _proximity(ctx, low, high)),
            (0.20, base.clamp(size_atr / 0.8, 0.0, 1.0)),
            (0.15, 1.0 - fill_ratio),
            (0.15, base.clamp(float(gap.get("displacement") or 0) / 2.0, 0.0, 1.0)),
        )
        # Consequent encroachment -- the midpoint of the gap -- is the fill a
        # trader actually waits for, not the far edge.
        midpoint = (low + high) / 2
        out.append(Candidate(
            key="fvg_fill", name="Imbalance Rebalance", family=SMC,
            direction=direction, trigger_family="pullback",
            label=base.label_for("Imbalance Fill", direction),
            conviction=conviction, alignment=EITHER,
            zone=(low, high), preferred=midpoint,
            stop=base.stop_beyond(low if direction == "bullish" else high,
                                  direction, ctx.atr * 0.3),
            stop_basis=f"beyond the {tf} Fair Value Gap",
            sources=[ZoneHint("FVG", tf, low, high, float(gap.get("quality", 0)),
                              f"{tf} FVG")],
            evidence=[f"{tf} {direction} FVG {low:.2f}-{high:.2f} "
                      f"({size_atr:.2f} ATR wide, {fill_ratio:.0%} filled)",
                      f"entry at consequent encroachment {midpoint:.2f}"],
        ))
    return out


# --- break of structure retest ---------------------------------------------


@base.strategy("bos_retest", "Break of Structure Retest", SMC,
               alignment=EITHER, min_conviction=54)
def bos_retest(ctx: Context) -> list[Candidate]:
    """The level that was broken, retested from the other side."""
    out: list[Candidate] = []
    for direction in ("bullish", "bearish"):
        best = None
        for tf in ("H1", "M30", "M15"):
            for event in ctx.events(tf, direction, ("BOS", "MSS")):
                if float(event.get("displacement") or 0) < 0.8:
                    continue
                scored = dict(event, timeframe=tf)
                if best is None or _bos_rank(scored) > _bos_rank(best):
                    best = scored
        if best is None:
            continue

        tf = best["timeframe"]
        level = float(best["price"])
        atr_tf = ctx.atr_of(tf)
        # The retest only exists once price has actually left the level and is
        # coming back to it from the correct side.
        if direction == "bullish" and level > ctx.price + atr_tf * 0.3:
            continue
        if direction == "bearish" and level < ctx.price - atr_tf * 0.3:
            continue
        if ctx.distance_atr(level) > 2.5:
            continue

        low, high = base.band(level, atr_tf * 0.5)
        swing_key = "last_swing_low" if direction == "bullish" else "last_swing_high"
        swing = (ctx.struct(tf) or {}).get(swing_key) or {}
        anchor = float(swing.get("price") or 0.0)
        if anchor and ((direction == "bullish" and anchor < low)
                       or (direction == "bearish" and anchor > high)):
            stop = base.stop_beyond(anchor, direction, atr_tf * 0.25)
            stop_basis = f"beyond the {tf} swing that started the break"
        else:
            stop = base.stop_beyond(low if direction == "bullish" else high,
                                    direction, atr_tf * 0.8)
            stop_basis = f"0.8 ATR beyond the broken {tf} level"

        conviction = base.mix(
            (0.30, base.clamp(float(best.get("displacement") or 0) / 2.5, 0.0, 1.0)),
            (0.25, {"H1": 1.0, "M30": 0.8, "M15": 0.65}.get(tf, 0.6)),
            (0.25, _proximity(ctx, low, high)),
            (0.20, 1.0 if ctx.trend_agrees(tf, direction) else 0.5),
        )
        out.append(Candidate(
            key="bos_retest", name="Break of Structure Retest", family=SMC,
            direction=direction, trigger_family="breakout",
            label=base.label_for("BOS Retest", direction),
            conviction=conviction, alignment=EITHER,
            zone=(low, high), preferred=level,
            stop=stop, stop_basis=stop_basis,
            sources=[ZoneHint("broken level", tf, low, high, 60.0,
                              f"{tf} {best['kind']} at {level:.2f}")],
            evidence=[f"{tf} {best['kind']} {direction} at {level:.2f} "
                      f"({float(best.get('displacement') or 0):.1f} ATR displacement)",
                      "price is returning to the level it broke"],
        ))
    return out


def _bos_rank(event: dict) -> float:
    weight = {"MSS": 1.15, "BOS": 1.0}.get(event.get("kind"), 0.9)
    tf_weight = {"H1": 1.2, "M30": 1.0, "M15": 0.85}.get(event.get("timeframe"), 0.8)
    return float(event.get("displacement") or 0) * weight * tf_weight


# --- liquidity sweeps -------------------------------------------------------


@base.strategy("sweep_reversal", "Liquidity Sweep Reversal", SMC,
               alignment=COUNTER, min_conviction=55)
def sweep_reversal(ctx: Context) -> list[Candidate]:
    """Stops taken beyond a level, then the level reclaimed.

    Both sides are examined, because a session that raids the lows and a
    session that raids the highs are both this pattern. What they may not do
    is describe the *same* price: a wide, indecisive candle wicks through the
    highs and the lows at once, and reading each wick on its own produced a
    long and a short a couple of pips apart, fired on the same cycle, each
    with its stop inside the other's entry. That is not two setups. It is one
    candle read twice. ``_operative`` keeps whichever raid is still live.
    """
    picked: dict[str, tuple[Candidate, dict]] = {}
    sweeps = ctx.liquidity.get("sweeps") or []
    for direction in ("bullish", "bearish"):
        matching = [s for s in sweeps if s.get("direction") == direction]
        if not matching:
            continue
        best = max(matching, key=lambda s: float(s.get("strength") or 0))
        if float(best.get("strength") or 0) < 40:
            continue

        tf = best.get("timeframe", "M15")
        level = float(best["level"])
        atr_tf = ctx.atr_of(tf)
        # The reclaim entry sits on the correct side of the swept level.
        if direction == "bullish" and level > ctx.price + atr_tf * 0.5:
            continue
        if direction == "bearish" and level < ctx.price - atr_tf * 0.5:
            continue
        if ctx.distance_atr(level) > 3.0:
            continue

        wick = float(best.get("wick_depth_atr") or 0.5) * atr_tf
        if direction == "bullish":
            low, high = level, level + atr_tf * 0.45
            stop = level - wick - atr_tf * 0.25
        else:
            low, high = level - atr_tf * 0.45, level
            stop = level + wick + atr_tf * 0.25

        shift = bool(ctx.events("M5", direction, ("CHoCH", "MSS"))
                     or ctx.events("M15", direction, ("CHoCH", "MSS")))
        conviction = base.mix(
            (0.30, float(best.get("strength") or 0) / 100),
            (0.20, 1.0 if best.get("confirmed_by_structure") else 0.35),
            (0.20, 1.0 if shift else 0.4),
            (0.15, base.clamp(1.5 / max(1.0, float(best.get("reclaimed_bars") or 1)),
                              0.0, 1.0)),
            (0.15, base.clamp(1.0 - ctx.distance_atr(level) / 3.0, 0.0, 1.0)),
        )
        side = "sell-side" if direction == "bullish" else "buy-side"
        picked[direction] = (Candidate(
            key="sweep_reversal", name="Liquidity Sweep Reversal", family=SMC,
            direction=direction, trigger_family="reversal",
            label=base.label_for("Sweep Reversal", direction),
            conviction=conviction, alignment=COUNTER,
            zone=(low, high), preferred=(low + high) / 2,
            stop=stop, stop_basis="beyond the wick that took the stops",
            sources=[ZoneHint("swept level", tf, low, high,
                              float(best.get("strength") or 0),
                              f"{tf} {side} sweep at {level:.2f}")],
            evidence=[f"{tf} {side} sweep at {level:.2f} "
                      f"({float(best.get('wick_depth_atr') or 0):.1f} ATR wick, "
                      f"reclaimed in {int(best.get('reclaimed_bars') or 0)} candle(s))",
                      "structure shifted after the sweep" if shift
                      else "waiting for the structure shift to confirm it"],
        ), best)
    return _operative(picked, ctx)


# How close two opposite sweep zones have to be before they are treated as one
# candle read twice rather than the two edges of a range. A raid on the lows at
# 4590 and a raid on the highs at 4600 are a range and both are tradeable; the
# same two readings a quarter of an ATR apart are one indecisive bar.
SWEEP_SAME_AREA_ATR = 0.25


def _operative(picked: dict[str, tuple[Candidate, dict]],
               ctx: Context) -> list[Candidate]:
    """Drop the side of a two-way raid that has already been overtaken.

    When the bullish and bearish reads land on the same area only one of them
    can still be in play, and it is the *later* raid: a sweep that has since
    been swept the other way was resolved by the market, not by this detector.
    Strength settles the case when both wicks belong to the same candle.
    """
    bull, bear = picked.get("bullish"), picked.get("bearish")
    if bull is None or bear is None:
        return [c for c, _ in picked.values()]

    buffer = ctx.atr * SWEEP_SAME_AREA_ATR
    (b_low, b_high), (s_low, s_high) = bull[0].zone, bear[0].zone
    if b_high + buffer < s_low or b_low - buffer > s_high:
        return [bull[0], bear[0]]           # two edges of a range: both stand

    def rank(entry: tuple[Candidate, dict]) -> tuple[float, float]:
        candidate, sweep = entry
        return (float(sweep.get("time") or 0), float(sweep.get("strength") or 0))

    return [max((bull, bear), key=rank)[0]]


# --- draw on liquidity ------------------------------------------------------


@base.strategy("liquidity_draw", "Draw on Liquidity", SMC,
               alignment=EITHER, min_conviction=53)
def liquidity_draw(ctx: Context) -> list[Candidate]:
    """A clear run at untapped liquidity far enough to be worth taking."""
    out: list[Candidate] = []
    pools = ctx.liquidity.get("pools") or []
    minimum = float(getattr(ctx.profile, "min_target_pips", 500.0)) * 0.01

    for direction in ("bullish", "bearish"):
        side = "buyside" if direction == "bullish" else "sellside"
        ahead = [p for p in pools
                 if p.get("side") == side and ctx.ahead(float(p["price"]), direction)]
        if not ahead:
            continue
        target = max(ahead, key=lambda p: float(p.get("strength") or 0))
        distance = abs(float(target["price"]) - ctx.price)
        if distance < minimum:
            continue
        if not ctx.clear_run(direction, distance):
            continue
        if not (ctx.trend_agrees("H1", direction) or ctx.htf_agrees(direction)):
            continue

        # The entry is a shallow pullback into the last M15 structure, not a
        # chase: half an ATR back from spot, floored at the M15 swing.
        atr_m15 = ctx.atr_of("M15")
        if direction == "bullish":
            low, high = ctx.price - atr_m15 * 0.8, ctx.price - atr_m15 * 0.1
        else:
            low, high = ctx.price + atr_m15 * 0.1, ctx.price + atr_m15 * 0.8
        swing_key = "last_swing_low" if direction == "bullish" else "last_swing_high"
        swing = (ctx.struct("M15") or {}).get(swing_key) or {}
        anchor = float(swing.get("price") or 0.0)
        stop = (base.stop_beyond(anchor, direction, atr_m15 * 0.3) if anchor
                else base.stop_beyond(low if direction == "bullish" else high,
                                      direction, atr_m15 * 1.0))
        if direction == "bullish" and stop >= low:
            stop = low - atr_m15 * 0.8
        if direction == "bearish" and stop <= high:
            stop = high + atr_m15 * 0.8

        conviction = base.mix(
            (0.30, float(target.get("strength") or 0) / 100),
            (0.25, 1.0 if ctx.trend_agrees("H1", direction) else 0.5),
            (0.20, 1.0 if ctx.htf_agrees(direction) else 0.4),
            (0.25, base.clamp(distance / (minimum * 2), 0.0, 1.0)),
        )
        out.append(Candidate(
            key="liquidity_draw", name="Draw on Liquidity", family=SMC,
            direction=direction, trigger_family="continuation",
            label=base.label_for("Liquidity Run", direction),
            conviction=conviction, alignment=EITHER,
            zone=(low, high), preferred=(low + high) / 2,
            stop=stop, stop_basis="beyond the last M15 swing",
            targets=[{"price": float(target["price"]),
                      "basis": f"{target.get('timeframe', '')} "
                               f"{str(target.get('source', '')).replace('_', ' ')} liquidity"}],
            sources=[ZoneHint("pullback", "M15", low, high, 55.0,
                              "M15 shallow pullback")],
            evidence=[f"untapped {side} liquidity at {float(target['price']):.2f}, "
                      f"{distance / 0.01:.0f} pips away",
                      "no opposing level of note in between",
                      f"H1 structure is {ctx.struct('H1').get('trend', 'unclear')}"],
        ))
    return out
