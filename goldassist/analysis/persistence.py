"""Is Gold trending or reverting right now, and is that statistically real?

``regime.py`` already classifies the market, and it does so the way most
chart packages do: ADX, Bollinger width, where price sits in its range. Those
are useful heuristics and they share one weakness -- they describe the *recent
path* without ever testing whether that path is distinguishable from a random
walk. A market can print a rising ADX purely by chance, and a trader acting on
it is acting on noise that happens to look like signal.

This module asks the harder question with the tools built for it. Three
independent measures, each answering "do moves persist, or do they revert?":

**The variance ratio** (Lo and MacKinlay, 1988)
    The formal test. If price follows a random walk, the variance of q-period
    returns must be exactly q times the variance of one-period returns. Divide
    one by the other: a ratio above 1 means moves persist -- a trend that is
    doing more than chance allows. Below 1 means they revert. The test comes
    with a z-statistic, so "trending" becomes a claim with a confidence
    attached rather than an impression, and the heteroskedasticity-robust form
    is used because Gold's volatility is emphatically not constant.

**The Hurst exponent**
    Rescaled-range analysis over several window sizes. H above 0.5 is
    persistence, below 0.5 is mean reversion, 0.5 is a random walk. It measures
    the same property as the variance ratio by a different route, which is
    exactly why both are computed: agreement between two unrelated estimators
    is worth far more than either alone, and disagreement is a warning that
    neither should be leaned on.

**The efficiency ratio** (Kaufman)
    Net distance travelled divided by total distance covered. No statistics at
    all -- just how directly price got from A to B. It is the most intuitive of
    the three and the most robust to a short sample, and it is what stops a
    thin window producing a confident answer from two clean estimators that
    happen to agree by accident.

Why an intraday trader who takes many trades needs this
-------------------------------------------------------

Because the two profitable playbooks are opposites, and running the wrong one
is how a good day becomes a bad one. In a persistent regime, breakouts follow
through and fading the extremes loses steadily; in a mean-reverting regime,
breakouts fail and fading the extremes is close to free money. The regime
changes within a single session -- Gold routinely reverts through Asia and
trends through London -- and the same setup deserves opposite treatment on
either side of that switch.

So this module does not pick a direction. It picks the **playbook**, which is
what lets a trader profit from any market change rather than only from the ones
that happen to suit a fixed style.

Everything here is computed on closed candles only and looks strictly
backwards.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

# Bars used for the estimates. About a day and a half of M15, which is long
# enough for the variance ratio to have power and short enough to describe the
# regime the trader is actually in rather than last week's.
WINDOW = 160

# The minimum sample any of these estimators needs before its answer means
# anything. Below this the module reports "not enough data" instead of a
# number, because a Hurst exponent from thirty bars is decoration.
MIN_BARS = 80

# Aggregation periods for the variance ratio. Several are tested because the
# regime is scale-dependent: Gold can mean-revert over five bars and trend over
# twenty, and a trader timing entries on M15 cares about both.
VR_PERIODS = (2, 4, 8, 16)

# |z| above this is conventional significance at 95%. Reported rather than
# applied as a gate -- a variance ratio of 1.3 that misses significance is
# still information, it is simply weaker information.
Z_SIGNIFICANT = 1.96

# Hurst window sizes for the rescaled-range regression.
HURST_WINDOWS = (8, 16, 32, 64)

# Efficiency-ratio bounds. Gold's intraday efficiency sits around 0.2-0.3 in
# rotation and above 0.4 on a genuine trend leg.
ER_TRENDING = 0.40
ER_REVERTING = 0.20


def _returns(closes: np.ndarray) -> np.ndarray:
    """Log returns, which is what every estimator here is defined on."""
    positive = closes[closes > 0]
    if len(positive) < 2:
        return np.array([])
    return np.diff(np.log(positive))


def variance_ratio(returns: np.ndarray, q: int) -> tuple[float, float] | None:
    """The Lo-MacKinlay variance ratio at lag ``q``, and its robust z-statistic.

    Returns ``(ratio, z)``. A ratio above 1 means q-period moves are larger
    than q independent one-period moves would be -- persistence. Below 1 means
    the opposite.

    The z-statistic uses the heteroskedasticity-consistent variance, not the
    simpler homoskedastic form. That matters here specifically: Gold's
    volatility clusters hard around releases and the session opens, and the
    homoskedastic statistic reads that clustering as evidence of trend. It
    would report significance on almost every window, which is worse than
    reporting nothing.
    """
    n = len(returns)
    if n < q * 4 or q < 2:
        return None

    mu = float(returns.mean())
    centred = returns - mu
    variance_1 = float((centred ** 2).sum()) / (n - 1)
    if variance_1 <= 0:
        return None

    # Overlapping q-period sums, the unbiased estimator from the paper.
    cumulative = np.cumsum(returns)
    q_sums = cumulative[q - 1:] - np.concatenate(([0.0], cumulative[:-q]))
    m = q * (n - q + 1) * (1 - q / n)
    if m <= 0:
        return None
    variance_q = float(((q_sums - q * mu) ** 2).sum()) / m

    ratio = variance_q / variance_1

    # Heteroskedasticity-robust asymptotic variance.
    squared = centred ** 2
    denominator = float(squared.sum()) ** 2
    if denominator <= 0:
        return None
    theta = 0.0
    for j in range(1, q):
        delta = float((squared[j:] * squared[:-j]).sum()) / denominator
        theta += ((2.0 * (q - j) / q) ** 2) * delta
    if theta <= 0:
        return None

    return ratio, (ratio - 1.0) / float(np.sqrt(theta))


def hurst(closes: np.ndarray) -> float | None:
    """The Hurst exponent by rescaled-range analysis.

    Above 0.5 is persistent, below 0.5 is mean-reverting, 0.5 is a random walk.
    Estimated by regressing log(R/S) on log(window) across several window
    sizes, which is the classical construction and needs no distributional
    assumption.
    """
    series = _returns(closes)
    if len(series) < MIN_BARS:
        return None

    sizes, values = [], []
    for size in HURST_WINDOWS:
        chunks = len(series) // size
        if chunks < 2:
            continue
        rescaled = []
        for index in range(chunks):
            chunk = series[index * size:(index + 1) * size]
            deviation = chunk - chunk.mean()
            spread = float(np.ptp(deviation.cumsum()))
            scale = float(chunk.std(ddof=1))
            if scale > 0 and spread > 0:
                rescaled.append(spread / scale)
        if rescaled:
            sizes.append(np.log(size))
            values.append(np.log(float(np.mean(rescaled))))

    if len(sizes) < 3:
        return None
    slope = float(np.polyfit(np.array(sizes), np.array(values), 1)[0])
    # The estimator is noisy at these sample sizes and can stray outside the
    # interval it is defined on; clamping is honest because a value of 1.2 says
    # "strongly persistent" and nothing more precise than that.
    return max(0.0, min(1.0, slope))


def efficiency_ratio(closes: np.ndarray) -> float | None:
    """Net movement over total movement: how directly price travelled.

    Kaufman's measure. 1.0 is a straight line, 0.0 is a round trip that ended
    where it began. Free of any distributional assumption and the most reliable
    of the three on a short window.
    """
    if len(closes) < 10:
        return None
    net = abs(float(closes[-1] - closes[0]))
    total = float(np.abs(np.diff(closes)).sum())
    return (net / total) if total > 0 else None


def analyse(frames: dict[str, pd.DataFrame],
            timeframe: str = "M15") -> dict:
    """The statistical character of the current market, and which playbook fits.

    Read on the timeframe entries are actually timed on, because that is the
    scale whose persistence decides whether a breakout follows through.
    """
    frame = frames.get(timeframe)
    if frame is None or len(frame) < MIN_BARS:
        for fallback in ("M15", "M30", "M5", "H1"):
            candidate = frames.get(fallback)
            if candidate is not None and len(candidate) >= MIN_BARS:
                frame, timeframe = candidate, fallback
                break
    if frame is None or len(frame) < MIN_BARS:
        return {"available": False,
                "note": f"Fewer than {MIN_BARS} bars available; the statistical "
                        "regime cannot be estimated."}

    closes = frame["close"].tail(WINDOW).to_numpy(dtype=float)
    returns = _returns(closes)
    if len(returns) < MIN_BARS - 1:
        return {"available": False, "note": "Not enough clean returns to estimate."}

    ratios = {}
    for q in VR_PERIODS:
        measured = variance_ratio(returns, q)
        if measured is not None:
            ratios[q] = {"ratio": round(measured[0], 3),
                         "z": round(measured[1], 2),
                         "significant": abs(measured[1]) >= Z_SIGNIFICANT}

    h = hurst(closes)
    er = efficiency_ratio(closes)

    verdict, confidence, playbook, notes = _interpret(ratios, h, er)

    return {
        "available": True,
        "timeframe": timeframe,
        "bars": int(len(closes)),
        "variance_ratios": ratios,
        "hurst": round(h, 3) if h is not None else None,
        "efficiency_ratio": round(er, 3) if er is not None else None,
        "regime": verdict,
        "confidence": confidence,
        "playbook": playbook,
        "observations": notes,
    }


def _interpret(ratios: dict, h: float | None,
               er: float | None) -> tuple[str, str, str, list[str]]:
    """Three estimators into one verdict, with disagreement reported honestly."""
    notes: list[str] = []
    votes: list[int] = []          # +1 persistent, -1 reverting, 0 neither

    # The variance ratio votes once, on the balance of its own lags. A regime
    # that only appears at one aggregation is not a regime.
    if ratios:
        significant = [v for v in ratios.values() if v["significant"]]
        pool = significant or list(ratios.values())
        mean_ratio = float(np.mean([v["ratio"] for v in pool]))
        strongest = max(ratios.items(), key=lambda kv: abs(kv[1]["z"]))
        if mean_ratio > 1.05:
            votes.append(1)
        elif mean_ratio < 0.95:
            votes.append(-1)
        else:
            votes.append(0)

        lag, best = strongest
        notes.append(
            f"The variance ratio averages {mean_ratio:.2f} across lags "
            f"{'/'.join(str(q) for q in ratios)}; the strongest reading is "
            f"{best['ratio']:.2f} at lag {lag} (z = {best['z']:+.2f}, "
            f"{'significant' if best['significant'] else 'not significant'}). "
            + ("Above 1 means moves persist beyond what chance allows."
               if mean_ratio > 1.05 else
               "Below 1 means moves are being given back — reversion."
               if mean_ratio < 0.95 else
               "At 1 the series is indistinguishable from a random walk."))
        if not significant:
            notes.append(
                "No lag reaches statistical significance, so the variance ratio "
                "is a lean rather than a finding. Treat the regime as undecided "
                "unless the other two measures agree strongly.")

    if h is not None:
        votes.append(1 if h > 0.55 else -1 if h < 0.45 else 0)
        notes.append(
            f"The Hurst exponent is {h:.2f} — "
            + ("persistent: legs tend to extend." if h > 0.55 else
               "mean-reverting: excursions tend to be retraced." if h < 0.45 else
               "essentially a random walk at this scale."))

    if er is not None:
        votes.append(1 if er >= ER_TRENDING else -1 if er <= ER_REVERTING else 0)
        notes.append(
            f"The efficiency ratio is {er:.2f}: price kept {er * 100:.0f}% of "
            "the distance it actually travelled. "
            + ("That is a directional market." if er >= ER_TRENDING else
               "That is a market going nowhere expensively." if er <= ER_REVERTING
               else "That is an ordinary, mixed session."))

    if not votes:
        return ("unknown", "none", "no statistical read available", notes)

    total = sum(votes)
    agreement = abs(total) / len(votes)
    confidence = ("high" if agreement >= 0.99 else
                  "moderate" if agreement >= 0.6 else "low")

    if total > 0:
        verdict = "persistent"
        playbook = (
            "Trend playbook: trade breakouts and continuation, buy pullbacks "
            "into VWAP and structure, and let a runner go. Fading extremes is "
            "the losing trade in this regime.")
    elif total < 0:
        verdict = "mean-reverting"
        playbook = (
            "Rotation playbook: fade the band extremes back toward VWAP, take "
            "the first objective rather than holding for a runner, and treat "
            "breakouts as suspect until they hold on a retest.")
    else:
        verdict = "indeterminate"
        playbook = (
            "The measures disagree, which is itself the read: there is no edge "
            "in either playbook right now. Trade only the highest-quality "
            "setups and size them down.")

    if confidence == "low" and verdict != "indeterminate":
        notes.append(
            "The three measures only partly agree, so this verdict is weak. "
            "It is a tilt in how to treat a setup, not a reason to take one.")

    return (verdict, confidence, playbook, notes)
