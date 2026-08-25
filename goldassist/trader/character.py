"""Day character: is today worth trading at all?

Every setup engine in this application answers "is this a good setup?". A
human trader asks a question first that no engine here asked: *is the market
behaving well enough today for any setup to be worth acting on?*

Those are genuinely different questions. The same pullback into demand is a
trade on a clean trending day and a donation on a day where price is knifing
through every level. The evidence looks identical on the chart; what differs
is the character of the session around it.

Four measurements, all taken from the candles rather than inferred:

``efficiency``
    How much of the distance travelled ended up as net progress. Kaufman's
    ratio: net move divided by the sum of the individual moves. Near 1.0 means
    a directional day; near 0 means price is covering ground without going
    anywhere.

``level respect``
    Of the recent interactions with significant levels, how many produced a
    rejection versus a clean break. A day that respects levels is a day where
    location-based entries mean something.

``whipsaw``
    How often market structure has changed its mind. Repeated BOS/CHoCH flips
    inside a few hours describe indecision, not a trend.

``volatility``
    ATR against its own median. Too low and targets are unreachable; too high
    and stops that looked structural become noise.

The output is deliberately a *verdict plus a conviction multiplier*, not a
veto. Standing aside is advice a trader can overrule; the app should say so
clearly and then get out of the way.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

# Verdicts, in descending order of willingness to trade.
TRADE = "TRADE"
SELECTIVE = "SELECTIVE"
STAND_ASIDE = "STAND ASIDE"

# Below this many level interactions the respect rate is noise, not a finding.
MIN_INTERACTIONS = 4


@dataclass
class DayCharacter:
    verdict: str
    score: float                     # 0-100 tradeability
    conviction: float                # multiplier applied to setup conviction
    headline: str
    measures: dict = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)
    cautions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "score": round(self.score, 1),
            "conviction": round(self.conviction, 2),
            "headline": self.headline,
            "measures": self.measures,
            "reasons": self.reasons,
            "cautions": self.cautions,
        }


def efficiency_ratio(closes: pd.Series) -> float:
    """Net progress divided by total distance travelled, over the window."""
    if len(closes) < 4:
        return 0.5
    moves = closes.diff().abs().sum()
    if moves <= 0:
        return 0.0
    return float(abs(closes.iloc[-1] - closes.iloc[0]) / moves)


def level_respect(df: pd.DataFrame, levels: list[dict], atr: float) -> tuple[float, int]:
    """Fraction of level interactions that ended in rejection rather than a break.

    A candle "interacts" when its range reaches a level. It counts as a
    rejection when the close returns to the side price approached from, and as
    a break when the close finishes decisively through it. Candles that merely
    hover are ignored -- they say nothing either way.
    """
    if not levels or atr <= 0 or df is None or df.empty:
        return 0.5, 0

    touch_band = atr * 0.25
    break_band = atr * 0.30
    rejections = breaks = 0

    prices = [float(lvl["price"]) for lvl in levels if lvl.get("price") is not None]
    closes = df["close"].to_numpy(dtype=float)
    highs = df["high"].to_numpy(dtype=float)
    lows = df["low"].to_numpy(dtype=float)

    for i in range(1, len(df)):
        prior_close = closes[i - 1]
        high, low, close = highs[i], lows[i], closes[i]
        for price in prices:
            if not (low - touch_band <= price <= high + touch_band):
                continue
            if prior_close < price:                      # approached from below
                if close > price + break_band:
                    breaks += 1
                elif close < price:
                    rejections += 1
            else:                                        # approached from above
                if close < price - break_band:
                    breaks += 1
                elif close > price:
                    rejections += 1

    total = rejections + breaks
    if total == 0:
        return 0.5, 0
    return rejections / total, total


def whipsaw_count(structure: dict) -> int:
    """How many times structure has reversed direction over the recent window.

    Only the most recent events are considered, so a clean trend that broke
    structure three times in the same direction scores zero.
    """
    events = structure.get("events") or []
    flips = 0
    previous = None
    for event in events[-8:]:
        direction = event.get("direction")
        if not direction:
            continue
        if previous and direction != previous:
            flips += 1
        previous = direction
    return flips


def assess(analysis: dict, frames: dict, session=None,
           lookback: int = 48) -> DayCharacter:
    """Read the character of the current trading day.

    ``frames`` maps timeframe -> closed candles; M15 is the working resolution
    because it is fine enough to show intraday character and coarse enough not
    to be dominated by noise.
    """
    df = frames.get("M15") if frames else None
    if df is None or len(df) < 20:
        return DayCharacter(
            verdict=SELECTIVE, score=50.0, conviction=1.0,
            headline="Not enough intraday history to judge the day's character.",
        )

    window = df.tail(lookback)
    atr = float(analysis.get("atr") or 0) or float((window["high"] - window["low"]).mean())

    efficiency = efficiency_ratio(window["close"])
    levels = (analysis.get("levels") or {}).get("levels", [])[:12]
    respect, interactions = level_respect(window, levels, atr)

    m15 = (analysis.get("timeframes") or {}).get("M15", {})
    h1 = (analysis.get("timeframes") or {}).get("H1", {})
    flips = whipsaw_count(m15.get("structure") or {})

    regime = h1.get("regime") or {}
    measures = regime.get("measures") or {}
    vol_ratio = float(measures.get("volatility_ratio") or 1.0)
    adx = float(measures.get("adx") or 0.0)
    quality = float(getattr(session, "quality", 1.0)) if session is not None else 1.0

    if session is not None and not session.market_open:
        return DayCharacter(
            verdict=STAND_ASIDE, score=0.0, conviction=0.0,
            headline="Gold is closed. Nothing to trade until the session reopens.",
            measures=_measures(efficiency, respect, interactions, flips,
                               vol_ratio, adx, quality),
            cautions=["The market is shut; every level shown is last week's."],
        )

    # Each component contributes to a 0-100 tradeability score. The weights
    # reflect what actually ruins a day: chop first, disrespected levels
    # second, wrong volatility third.
    score = 0.0
    reasons: list[str] = []
    cautions: list[str] = []

    # --- efficiency (0-32) -------------------------------------------------
    score += float(np.clip(efficiency / 0.35, 0, 1) * 32)
    if efficiency >= 0.30:
        reasons.append(
            f"price is travelling efficiently ({efficiency:.0%} of the distance "
            "covered became net progress)")
    elif efficiency <= 0.12:
        cautions.append(
            f"only {efficiency:.0%} of today's movement became net progress -- "
            "this is chop, not trend")

    # --- level respect (0-28) ---------------------------------------------
    if interactions >= MIN_INTERACTIONS:
        score += float(np.clip((respect - 0.25) / 0.5, 0, 1) * 28)
        if respect >= 0.65:
            reasons.append(
                f"levels are holding -- {respect:.0%} of {interactions} "
                "interactions produced a rejection")
        elif respect <= 0.35:
            cautions.append(
                f"levels are not holding -- price broke through {1 - respect:.0%} "
                f"of {interactions} interactions, so location-based entries are "
                "unreliable today")
    else:
        score += 14.0    # unmeasured, so neither rewarded nor penalised
        cautions.append("too few level interactions today to judge whether price "
                        "is respecting structure")

    # --- whipsaw (0-22) ----------------------------------------------------
    score += max(0.0, 22.0 - flips * 7.0)
    if flips >= 3:
        cautions.append(f"market structure has reversed {flips} times recently -- "
                        "the market has not decided anything yet")
    elif flips == 0:
        reasons.append("structure has not contradicted itself in the recent window")

    # --- volatility (0-18) -------------------------------------------------
    if 0.75 <= vol_ratio <= 1.6:
        score += 18.0
        reasons.append(f"volatility is normal ({vol_ratio:.2f}x its median)")
    elif vol_ratio < 0.75:
        score += 18.0 * float(np.clip(vol_ratio / 0.75, 0, 1))
        cautions.append(f"volatility is only {vol_ratio:.2f}x its median -- "
                        "targets may simply be out of reach today")
    else:
        score += max(0.0, 18.0 - (vol_ratio - 1.6) * 18.0)
        cautions.append(f"volatility is {vol_ratio:.2f}x its median -- structural "
                        "stops sit inside the noise at this range")

    score = float(np.clip(score, 0, 100))

    # A poor hour drags the day's score down without ever making a good day bad
    # on its own: at worst the session halves it.
    adjusted = score * (0.5 + 0.5 * quality)
    if session is not None and quality <= 0.35:
        cautions.append(session.description)

    verdict, conviction, headline = _verdict(
        adjusted, efficiency, flips, respect, interactions, session)

    return DayCharacter(
        verdict=verdict,
        score=adjusted,
        conviction=conviction,
        headline=headline,
        measures=_measures(efficiency, respect, interactions, flips,
                           vol_ratio, adx, quality),
        reasons=reasons,
        cautions=cautions,
    )


def _measures(efficiency, respect, interactions, flips, vol_ratio, adx, quality) -> dict:
    return {
        "efficiency": round(efficiency, 3),
        "level_respect": round(respect, 2) if interactions >= MIN_INTERACTIONS else None,
        "level_interactions": interactions,
        "structure_flips": flips,
        "volatility_ratio": round(vol_ratio, 2),
        "adx": round(adx, 1),
        "session_quality": round(quality, 2),
    }


def _verdict(score, efficiency, flips, respect, interactions, session):
    """Turn the score into a decision, and say why in a trader's words."""
    if score >= 62:
        return (TRADE, 1.0,
                "The market is behaving well today -- clean enough to trade "
                "setups on their merits.")

    if score >= 40:
        if flips >= 3:
            detail = "structure keeps reversing, so wait for it to commit"
        elif interactions >= MIN_INTERACTIONS and respect <= 0.35:
            detail = "levels are being cut through, so demand confirmation"
        elif efficiency <= 0.15:
            detail = "movement is not going anywhere, so take only the clearest setups"
        elif session is not None and session.quality <= 0.35:
            detail = f"these are low-quality hours ({session.name.replace('_', ' ')})"
        else:
            detail = "be selective"
        return SELECTIVE, 0.75, f"Mixed conditions -- {detail}."

    return (STAND_ASIDE, 0.45,
            "Conditions are poor. A good trader takes fewer trades on days like "
            "this, not the same number with more hope.")
