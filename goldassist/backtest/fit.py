"""Fitting the confluence weights to outcomes, out of sample.

The seven pillar weights in ``decision.confluence.CATALOGUE`` were chosen by
judgment. The score they produce does rank setups -- the score bands are
monotonic and held across folds -- but "ranks correctly" is not "weighted
correctly", and nothing had ever asked which pillars carry the ranking.

This module asks. Every trade a replay produces now carries the evidence it
armed on (``BacktestTrade.features``): each pillar's share of its maximum and
the penalties charged. A logistic regression of *did the trade pay* on those
features, fitted walk-forward, answers two questions:

1. **Does a fitted combination rank out-of-sample trades better than the
   judged score does?** Measured by AUC on each test fold, against the AUC of
   the score itself on the same trades. If it does not, the judged weights
   stay, and the report says so.
2. **If it does, which weights would it suggest?** Reported as
   ``weight_overrides`` a profile already understands, bounded, and printed --
   never applied. Refitting weights silently on every run is how a method
   quietly curve-fits itself; a person reads the table and decides.

Three rules, each learned the hard way elsewhere in this project:

* **Walk-forward only.** Anchored folds in time order; a fold's test trades are
  never seen by its fit. A fit on all trades would report the in-sample fit,
  which is always good.
* **Regularised and small.** Nine features, L2 penalty. A thousand trades is
  enough for that and nowhere near enough for per-strategy or per-session
  interactions.
* **Standard library only.** Gradient descent on nine features needs nothing
  more, and the fit can run on a machine whose numpy is unavailable.
"""

from __future__ import annotations

import math
from datetime import datetime

# Pillars, in catalogue order. Duplicated rather than imported so this module
# stays importable without the decision package (and its dependencies).
PILLARS = ("htf_alignment", "h1_structure", "location", "liquidity_sweep",
           "structure_confirm", "candles", "momentum")

FEATURES = tuple(f"pillar:{p}" for p in PILLARS) + ("penalties", "conviction")

# Below this many trades carrying features, no fit is attempted.
MIN_TRADES = 150
# A test fold smaller than this cannot give a stable AUC.
MIN_FOLD = 40
L2 = 0.5
# Features are standardised inside the fit, so a large step converges in a
# few hundred passes; pure Python makes every pass count.
LEARNING_RATE = 0.5
ITERATIONS = 600
# Suggested weight overrides are bounded: the fit may say a pillar is worth
# half or half again what it was judged, never that it is worthless or
# dominant on one window.
WEIGHT_BOUNDS = (0.5, 1.5)


def _net_r(trade: dict) -> float:
    value = trade.get("net_r_multiple")
    if value is None:
        value = trade.get("r_multiple", 0.0)
    return float(value or 0.0)


def vector(trade: dict) -> list[float] | None:
    """The feature vector of one trade, or None if it carries no features."""
    features = trade.get("features") or {}
    if not features:
        return None
    row = [float(features.get(name, 0.0) or 0.0) for name in FEATURES[:len(PILLARS)]]
    # All penalties as one feature, in tens of points: the individual penalty
    # types fire too rarely to fit one coefficient each on a year of Gold.
    penalties = sum(float(v or 0.0) for k, v in features.items() if k.startswith("penalty:"))
    row.append(penalties / 10.0)
    row.append(float(features.get("conviction", 0.0) or 0.0) / 100.0)
    return row


def _sigmoid(z: float) -> float:
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    e = math.exp(z)
    return e / (1.0 + e)


def logistic(rows: list[list[float]], labels: list[int]) -> tuple[list[float], float]:
    """L2-regularised logistic regression by batch gradient descent.

    Features are standardised inside the fit and the coefficients are mapped
    back, so the returned weights apply to raw feature values.
    """
    n, k = len(rows), len(rows[0])
    means = [sum(r[j] for r in rows) / n for j in range(k)]
    sds = []
    for j in range(k):
        var = sum((r[j] - means[j]) ** 2 for r in rows) / n
        sds.append(math.sqrt(var) or 1.0)
    z = [[(r[j] - means[j]) / sds[j] for j in range(k)] for r in rows]

    base = sum(labels) / n
    bias = math.log(base / (1 - base)) if 0 < base < 1 else 0.0
    weights = [0.0] * k
    for _ in range(ITERATIONS):
        grad = [0.0] * k
        grad_b = 0.0
        for row, label in zip(z, labels):
            error = _sigmoid(bias + sum(w * x for w, x in zip(weights, row))) - label
            grad_b += error
            for j in range(k):
                grad[j] += error * row[j]
        bias -= LEARNING_RATE * grad_b / n
        for j in range(k):
            weights[j] -= LEARNING_RATE * (grad[j] / n + L2 * weights[j] / n)

    raw = [weights[j] / sds[j] for j in range(k)]
    raw_bias = bias - sum(weights[j] * means[j] / sds[j] for j in range(k))
    return raw, raw_bias


def predict(weights: list[float], bias: float, row: list[float]) -> float:
    return _sigmoid(bias + sum(w * x for w, x in zip(weights, row)))


def auc(scores: list[float], labels: list[int]) -> float | None:
    """Probability a random winner outranks a random loser. Ties count half."""
    wins = [s for s, l in zip(scores, labels) if l]
    losses = [s for s, l in zip(scores, labels) if not l]
    if not wins or not losses:
        return None
    ranked = sorted((s, l) for s, l in zip(scores, labels))
    # Mann-Whitney U with average ranks for ties.
    rank_sum = 0.0
    i = 0
    while i < len(ranked):
        j = i
        while j + 1 < len(ranked) and ranked[j + 1][0] == ranked[i][0]:
            j += 1
        average = (i + j) / 2 + 1
        rank_sum += average * sum(1 for m in range(i, j + 1) if ranked[m][1])
        i = j + 1
    u = rank_sum - len(wins) * (len(wins) + 1) / 2
    return u / (len(wins) * len(losses))


def _when(trade: dict) -> datetime:
    try:
        return datetime.fromisoformat(str(trade.get("entered_at")))
    except (TypeError, ValueError):
        return datetime.min


def assess(trades: list[dict], folds: int = 4) -> dict:
    """Walk-forward fit of outcome on evidence. See the module docstring."""
    usable = [t for t in trades if vector(t) is not None]
    usable.sort(key=_when)
    if len(usable) < MIN_TRADES:
        return {"available": False,
                "note": (f"{len(usable)} of {len(trades)} trades carry the evidence "
                         f"they armed on; {MIN_TRADES} are needed. Runs made before "
                         "features were recorded cannot be fitted -- replay again.")}

    size = len(usable) // (folds + 1)
    if size < MIN_FOLD:
        return {"available": False,
                "note": f"{len(usable)} trades over {folds} folds leaves test folds "
                        f"of {size}; {MIN_FOLD} are needed."}

    splits = []
    for fold in range(1, folds + 1):
        train = usable[:size * fold]
        test = usable[size * fold: size * (fold + 1)] if fold < folds else usable[size * fold:]
        x_train = [vector(t) for t in train]
        y_train = [1 if _net_r(t) > 0 else 0 for t in train]
        weights, bias = logistic(x_train, y_train)

        x_test = [vector(t) for t in test]
        y_test = [1 if _net_r(t) > 0 else 0 for t in test]
        fitted = [predict(weights, bias, row) for row in x_test]
        judged = [float(t.get("score") or 0.0) for t in test]

        # Does the fitted probability sort realised R, not just win/loss?
        ordered = sorted(zip(fitted, (_net_r(t) for t in test)))
        third = max(1, len(ordered) // 3)
        low = [r for _, r in ordered[:third]]
        high = [r for _, r in ordered[-third:]]

        splits.append({
            "fold": fold,
            "train": len(train),
            "test": len(test),
            "fitted_auc": _round(auc(fitted, y_test)),
            "score_auc": _round(auc(judged, y_test)),
            "top_third_avg_r": round(sum(high) / len(high), 3),
            "bottom_third_avg_r": round(sum(low) / len(low), 3),
            "weights": dict(zip(FEATURES, (round(w, 3) for w in weights))),
        })

    comparable = [s for s in splits if s["fitted_auc"] is not None and s["score_auc"] is not None]
    better = sum(1 for s in comparable if s["fitted_auc"] > s["score_auc"])
    beats = bool(comparable) and better > len(comparable) / 2

    final_weights, _ = logistic([vector(t) for t in usable],
                                [1 if _net_r(t) > 0 else 0 for t in usable])
    suggestion = _overrides(final_weights) if beats else None

    if not comparable:
        verdict = "No fold had both winners and losers to rank; nothing can be concluded."
    elif beats:
        verdict = (f"The fitted evidence weights ranked out-of-sample trades better "
                   f"than the judged score in {better} of {len(comparable)} folds. "
                   "The suggested weight overrides below are worth a replay -- "
                   "apply them to a copy of the profile, rerun, and keep them only "
                   "if the new run's holdout agrees.")
    else:
        verdict = (f"The fitted weights beat the judged score in only {better} of "
                   f"{len(comparable)} folds. The judged weights stay: a fit that "
                   "cannot beat them out of sample is fitting noise.")

    return {"available": True, "trades": len(usable), "splits": splits,
            "beats_score": beats, "suggested_weight_overrides": suggestion,
            "verdict": verdict}


def _overrides(weights: list[float]) -> dict:
    """Pillar coefficients as ``weight_overrides`` multipliers around 1.0.

    Coefficients are per unit of "share of the pillar's maximum", so they are
    directly comparable across pillars. Negative or zero coefficients take the
    lower bound: the fit is saying the pillar does not help, but one window is
    not enough to switch a pillar off.
    """
    pillar = weights[:len(PILLARS)]
    positive = [w for w in pillar if w > 0]
    if not positive:
        return {p: WEIGHT_BOUNDS[0] for p in PILLARS}
    mean = sum(positive) / len(positive)
    low, high = WEIGHT_BOUNDS
    return {p: round(min(high, max(low, w / mean)), 2) if w > 0 else low
            for p, w in zip(PILLARS, pillar)}


def _round(value: float | None) -> float | None:
    return round(value, 3) if value is not None else None


def render(report: dict) -> str:
    lines = ["", "Evidence weights, fitted walk-forward", "=" * 60]
    if not report.get("available"):
        lines.append(f"  {report.get('note', 'unavailable')}")
        return "\n".join(lines)
    lines.append(f"  {report['trades']} trades with recorded evidence")
    lines.append("")
    lines.append("  fold  train  test   fitted AUC  score AUC   top-3rd R  bottom-3rd R")
    for s in report["splits"]:
        lines.append(
            f"  {s['fold']:>4}  {s['train']:>5}  {s['test']:>4}   "
            f"{_fmt(s['fitted_auc']):>10}  {_fmt(s['score_auc']):>9}   "
            f"{s['top_third_avg_r']:>+9.3f}  {s['bottom_third_avg_r']:>+12.3f}")
    lines.append("")
    lines.append(f"  {report['verdict']}")
    overrides = report.get("suggested_weight_overrides")
    if overrides:
        lines.append("")
        lines.append("  Suggested weight_overrides (not applied):")
        for name, value in overrides.items():
            lines.append(f"    {name:20} {value:.2f}")
    return "\n".join(lines)


def _fmt(value: float | None) -> str:
    return f"{value:.3f}" if value is not None else "-"
