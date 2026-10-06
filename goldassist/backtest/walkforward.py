"""Out-of-sample validation: does what worked before keep working after?

``stability.py`` cuts a finished run into folds and asks whether an ordering
held in each. That is a real check and it is not an out-of-sample test: every
fold is scored after the fact, with the whole run already in view. It can tell
you an ordering was consistent; it cannot tell you the ordering would have been
*usable in advance*, which is the only property that matters.

This module asks the harder question. It walks forward through the trade record
in sequential train/test pairs, and for each pair it makes a decision using
only the training window and then grades that decision on the test window the
decision could not see.

Two experiments, because the application makes two distinct claims
------------------------------------------------------------------

**Does the score band predict the outcome?** The engine's central claim is that
its 0-100 confluence score ranks its own setups correctly. The test: measure
what each band was worth in the training window, then check whether the bands
kept that order in the test window. Reported as a rank correlation per split
and averaged.

**Does past strategy performance predict future strategy performance?** The
tempting use of a results table is to switch off the detectors that lost. The
test: rank the detectors by training-window expectancy, take the better half,
and compare how that selection did out-of-sample against simply trading
everything. If selection does not beat the whole library, the table should not
be used to select -- which is a conclusion the development log already reached
from two overlapping windows, and this measures it properly.

Why the second experiment usually fails, and why that is the finding
--------------------------------------------------------------------

Thirty-four detectors splitting a few hundred trades leaves a dozen trades
each, and a dozen trades cannot rank anything. The expected result is that
selection performs no better than the full library and often worse, because
what is being selected on is mostly noise. That result is worth having
explicitly: it is the difference between "we chose not to prune the library"
and "we measured that pruning it destroys value".

Nothing here re-runs a replay. It reads a stored trade list, so it is free.
"""

from __future__ import annotations

import statistics as stats
from collections import defaultdict
from datetime import datetime

# Sequential splits. Four gives three train/test pairs from a year of trades,
# each with enough on both sides to say something.
DEFAULT_SPLITS = 4

# A window needs this many trades before a decision made from it is worth
# grading. Below it the split is reported as unusable rather than scored.
MIN_WINDOW = 40

# A strategy needs this many trades in the training window to be ranked at all.
# Matches ``trader/expectancy.MIN_SAMPLE`` deliberately: the two answer the
# same question and disagreeing about it would be indefensible.
MIN_STRATEGY_SAMPLE = 8

# Testable splits required before either experiment states a verdict rather
# than an observation. Two is still thin; one is not a sample at all, and a
# perfect rank correlation on one window is a coin landing heads twice.
MIN_SPLITS_FOR_VERDICT = 3

BANDS = (("85-100", 85.0, 1e9), ("75-85", 75.0, 85.0),
         ("65-75", 65.0, 75.0), ("below 65", -1e9, 65.0))


def _net_r(trade: dict) -> float:
    value = trade.get("net_r_multiple")
    if value is None:
        value = trade.get("r_multiple", 0.0)
    return float(value or 0.0)


def _band_of(score) -> str:
    try:
        value = float(score)
    except (TypeError, ValueError):
        return "below 65"
    for name, low, high in BANDS:
        if low <= value < high:
            return name
    return "below 65"


def _ordered(trades: list[dict]) -> list[dict]:
    return sorted((t for t in trades if t.get("entered_at")),
                  key=lambda t: t["entered_at"])


def spearman(pairs: list[tuple[float, float]]) -> float | None:
    """Rank correlation between two series, without a scipy dependency.

    Rank rather than linear correlation because the question is about
    *ordering* -- whether the better band stayed the better band -- not about
    whether the magnitudes matched, which they never do out-of-sample.
    """
    if len(pairs) < 3:
        return None

    def ranks(values: list[float]) -> list[float]:
        order = sorted(range(len(values)), key=lambda i: values[i])
        out = [0.0] * len(values)
        index = 0
        while index < len(order):
            # Ties share the average of the ranks they span, which is what
            # makes this Spearman rather than an approximation of it.
            same = index
            while (same + 1 < len(order)
                   and values[order[same + 1]] == values[order[index]]):
                same += 1
            average = (index + same) / 2.0 + 1.0
            for position in range(index, same + 1):
                out[order[position]] = average
            index = same + 1
        return out

    left = ranks([a for a, _ in pairs])
    right = ranks([b for _, b in pairs])
    if len(set(left)) < 2 or len(set(right)) < 2:
        return None

    mean_l, mean_r = stats.mean(left), stats.mean(right)
    numerator = sum((l - mean_l) * (r - mean_r) for l, r in zip(left, right))
    denominator = (sum((l - mean_l) ** 2 for l in left)
                   * sum((r - mean_r) ** 2 for r in right)) ** 0.5
    return (numerator / denominator) if denominator > 0 else None


def _by(trades: list[dict], key) -> dict[str, list[float]]:
    out: dict[str, list[float]] = defaultdict(list)
    for trade in trades:
        out[key(trade)].append(_net_r(trade))
    return out


def split_points(trades: list[dict], splits: int) -> list[tuple[int, int]]:
    """Anchored train/test boundaries: (train_end, test_end) index pairs.

    Anchored rather than rolling -- the training window always starts at the
    beginning and grows. That matches how the system would actually be used:
    a trader in June has January through May available, not a fixed-width
    window of it.
    """
    n = len(trades)
    size = n // splits
    if size < MIN_WINDOW // 2:
        return []
    return [(size * i, size * (i + 1)) for i in range(1, splits)]


def score_band_experiment(trades: list[dict], splits: int) -> dict:
    """Does the training window's band ordering survive into the test window?"""
    ordered = _ordered(trades)
    results = []

    for train_end, test_end in split_points(ordered, splits):
        train, test = ordered[:train_end], ordered[train_end:test_end]
        if len(train) < MIN_WINDOW or len(test) < MIN_WINDOW:
            continue

        train_bands = _by(train, lambda t: _band_of(t.get("score")))
        test_bands = _by(test, lambda t: _band_of(t.get("score")))
        shared = [b for b, _, _ in BANDS
                  if len(train_bands.get(b, [])) >= MIN_STRATEGY_SAMPLE
                  and len(test_bands.get(b, [])) >= MIN_STRATEGY_SAMPLE]
        if len(shared) < 3:
            continue

        pairs = [(stats.mean(train_bands[b]), stats.mean(test_bands[b]))
                 for b in shared]
        results.append({
            "train_trades": len(train),
            "test_trades": len(test),
            "bands_compared": len(shared),
            "rank_correlation": (round(spearman(pairs), 3)
                                 if spearman(pairs) is not None else None),
            "train": {b: round(stats.mean(train_bands[b]), 3) for b in shared},
            "test": {b: round(stats.mean(test_bands[b]), 3) for b in shared},
            "from": test[0]["entered_at"][:10],
            "to": test[-1]["entered_at"][:10],
        })

    correlations = [r["rank_correlation"] for r in results
                    if r["rank_correlation"] is not None]
    return {
        "available": bool(results),
        "splits": results,
        "mean_rank_correlation": (round(stats.mean(correlations), 3)
                                  if correlations else None),
        "held_in": sum(1 for c in correlations if c > 0),
        "of": len(correlations),
        "verdict": _band_verdict(correlations),
    }


def _band_verdict(correlations: list[float]) -> str:
    if not correlations:
        return ("Not enough trades per band per split to test the ordering "
                "out-of-sample. This needs more history, not a different test.")
    mean = stats.mean(correlations)
    held = sum(1 for c in correlations if c > 0)

    # One split is one observation, and a rank correlation of +1.00 across
    # three bands on a single window is roughly as likely as a coin landing
    # heads twice. Saying "held in all 1 splits" would be both bad prose and
    # bad statistics, so a single testable split reports itself as a single
    # testable split and claims nothing.
    if len(correlations) < MIN_SPLITS_FOR_VERDICT:
        return (f"Only {len(correlations)} split had enough trades per band to "
                f"test (rank correlation {mean:+.2f}). One window is an "
                "observation, not evidence — the ordering is encouraging and "
                "unproven until there is more history behind it.")

    if held == len(correlations) and mean >= 0.5:
        return (f"The score band ordering held out-of-sample in all "
                f"{len(correlations)} splits (mean rank correlation {mean:+.2f}). "
                "The score is usable in advance, not merely consistent in "
                "hindsight — this is the strongest form of the claim.")
    if held > len(correlations) / 2:
        return (f"The ordering held in {held} of {len(correlations)} splits "
                f"(mean {mean:+.2f}). Directionally real but not dependable "
                "split by split; treat the score as a tilt rather than a "
                "calibrated probability.")
    return (f"The ordering held in only {held} of {len(correlations)} splits "
            f"(mean {mean:+.2f}). The in-sample band table is not predictive "
            "out-of-sample and must not be used to set a threshold.")


def strategy_selection_experiment(trades: list[dict], splits: int) -> dict:
    """Does trading the in-sample winners beat trading the whole library?"""
    ordered = _ordered(trades)
    results = []

    for train_end, test_end in split_points(ordered, splits):
        train, test = ordered[:train_end], ordered[train_end:test_end]
        if len(train) < MIN_WINDOW or len(test) < MIN_WINDOW:
            continue

        train_by = _by(train, lambda t: str(t.get("strategy") or "unknown"))
        rankable = {k: stats.mean(v) for k, v in train_by.items()
                    if len(v) >= MIN_STRATEGY_SAMPLE}
        if len(rankable) < 4:
            continue

        # The decision: keep the better half by training expectancy.
        keep = {k for k, _ in sorted(rankable.items(), key=lambda kv: -kv[1])
                [:max(2, len(rankable) // 2)]}

        selected = [_net_r(t) for t in test
                    if str(t.get("strategy") or "unknown") in keep]
        everything = [_net_r(t) for t in test]
        if len(selected) < 10:
            continue

        results.append({
            "from": test[0]["entered_at"][:10],
            "to": test[-1]["entered_at"][:10],
            "strategies_ranked": len(rankable),
            "strategies_kept": len(keep),
            "selected_trades": len(selected),
            "selected_avg_r": round(stats.mean(selected), 3),
            "all_trades": len(everything),
            "all_avg_r": round(stats.mean(everything), 3),
            "edge": round(stats.mean(selected) - stats.mean(everything), 3),
        })

    edges = [r["edge"] for r in results]
    return {
        "available": bool(results),
        "splits": results,
        "mean_edge_r": round(stats.mean(edges), 3) if edges else None,
        "helped_in": sum(1 for e in edges if e > 0),
        "of": len(edges),
        "verdict": _selection_verdict(edges),
    }


def _selection_verdict(edges: list[float]) -> str:
    if not edges:
        return ("Too few strategies carry a usable sample in both windows to "
                "test selection. That is itself the answer for now: the record "
                "cannot support pruning the library.")
    mean = stats.mean(edges)
    helped = sum(1 for e in edges if e > 0)
    if len(edges) < MIN_SPLITS_FOR_VERDICT:
        return (f"Only {len(edges)} split(s) could be tested "
                f"({mean:+.3f}R per trade). Too few to conclude anything about "
                "whether pruning the library helps.")
    if helped == len(edges) and mean > 0.05:
        return (f"Selecting the in-sample better half beat trading everything in "
                f"all {len(edges)} splits, by {mean:+.3f}R per trade on average. "
                "Past strategy performance does carry forward here.")
    if helped > len(edges) / 2:
        return (f"Selection helped in {helped} of {len(edges)} splits "
                f"({mean:+.3f}R per trade on average) — weak and inconsistent "
                "evidence. Not enough to justify switching detectors off.")
    return (f"Selection helped in only {helped} of {len(edges)} splits "
            f"({mean:+.3f}R per trade on average). Ranking detectors on past "
            "performance does not predict future performance at this sample "
            "size — the results table must be read, not acted on. This is the "
            "measured case for sizing on conviction rather than gating on it.")


def assess(trades: list[dict], splits: int = DEFAULT_SPLITS) -> dict:
    """Both experiments, on one stored trade list."""
    ordered = _ordered(trades)
    if len(ordered) < MIN_WINDOW * 2:
        return {"available": False,
                "note": (f"{len(ordered)} trades is too few for a walk-forward; "
                         f"at least {MIN_WINDOW * 2} are needed.")}
    return {
        "available": True,
        "trades": len(ordered),
        "splits": splits,
        "score_bands": score_band_experiment(ordered, splits),
        "strategy_selection": strategy_selection_experiment(ordered, splits),
    }


def render(report: dict) -> str:
    """The walk-forward as the text a person actually reads."""
    if not report.get("available"):
        return report.get("note", "Walk-forward unavailable.")

    lines = ["", "Walk-forward (out-of-sample)", "=" * 64,
             f"  {report['trades']} trades, {report['splits']} anchored splits", ""]

    bands = report.get("score_bands") or {}
    lines.append("Score-band ordering — does it predict, or only describe?")
    lines.append("-" * 64)
    if bands.get("available"):
        for split in bands["splits"]:
            correlation = split["rank_correlation"]
            lines.append(
                f"  {split['from']} -> {split['to']}  "
                f"n={split['test_trades']:<4} bands={split['bands_compared']}  "
                f"rank corr {correlation:+.2f}" if correlation is not None
                else f"  {split['from']} -> {split['to']}  (not rankable)")
        if bands.get("mean_rank_correlation") is not None:
            lines.append(f"  mean rank correlation "
                         f"{bands['mean_rank_correlation']:+.2f} "
                         f"({bands['held_in']}/{bands['of']} splits positive)")
    lines.append(f"  {bands.get('verdict', 'unavailable')}")

    selection = report.get("strategy_selection") or {}
    lines += ["", "Strategy selection — does pruning the library help?", "-" * 64]
    if selection.get("available"):
        for split in selection["splits"]:
            lines.append(
                f"  {split['from']} -> {split['to']}  "
                f"kept {split['strategies_kept']}/{split['strategies_ranked']}  "
                f"selected {split['selected_avg_r']:+.3f}R vs all "
                f"{split['all_avg_r']:+.3f}R  ({split['edge']:+.3f})")
        if selection.get("mean_edge_r") is not None:
            lines.append(f"  mean edge {selection['mean_edge_r']:+.3f}R per trade "
                         f"({selection['helped_in']}/{selection['of']} splits helped)")
    lines.append(f"  {selection.get('verdict', 'unavailable')}")

    return "\n".join(lines)
