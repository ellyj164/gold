"""Does a backtest result hold across time, or only in aggregate?

A single monotonic score-band table is weaker evidence than it looks. One
strong stretch can drag an average into the right order and leave the
impression that the score ranks setups correctly when it only ranked them
correctly in March. The distinction decides what may be done with the result:
an ordering that holds in every independent period is a property of the engine
and may be calibrated against; one that appears only in the total is a property
of that particular sample and may not.

So this module cuts a finished backtest into contiguous folds and asks the same
question of each. Nothing here re-runs anything -- it reads the trades a replay
already stored, which means the analysis is free and can be repeated on any
stored run.

Two rules it follows, both learned from the first real result:

**Report the sample size beside every figure.** The 85-100 band holds five to
nine trades per fold and swings from -0.17R to +1.58R. That is noise, and a
table that showed only the averages would invite exactly the wrong conclusion.

**A weak band is not a losing band.** The 65-75 band averaged +0.127R overall,
which reads like a candidate for exclusion until the folds show it positive in
all four. Raising the gate would have discarded real profit. Weak setups belong
in the sizing layer, not on the cutting-room floor -- which is what the
application already does, and now has evidence for.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field

# Score bands, matching the ones the backtest summariser reports.
BANDS = (("85-100", 85.0, 1e9), ("75-85", 75.0, 85.0),
         ("65-75", 65.0, 75.0), ("below 65", -1e9, 65.0))

# Below this many trades a fold's figure for a band is reported but must not be
# reasoned from. Matches `trader/expectancy.MIN_SAMPLE`, deliberately: the two
# answer the same question and disagreeing about it would be indefensible.
MIN_FOLD_SAMPLE = 8


def band_of(score: float) -> str:
    for name, low, high in BANDS:
        if low <= float(score) < high:
            return name
    return "below 65"


@dataclass
class FoldRead:
    """One contiguous slice of a backtest's trades."""
    index: int
    start: str
    end: str
    trades: int
    net_r: float
    average_r: float
    by_band: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"fold": self.index, "from": self.start, "to": self.end,
                "trades": self.trades, "net_r": round(self.net_r, 2),
                "average_r": round(self.average_r, 3),
                "by_band": self.by_band}


def _net_r(trade: dict) -> float:
    """Net R, falling back to gross for rows written before costs existed."""
    value = trade.get("net_r_multiple")
    if value is None:
        value = trade.get("r_multiple", 0.0)
    return float(value or 0.0)


def split(trades: list[dict], folds: int = 4) -> list[FoldRead]:
    """Cut trades into contiguous, equal-count folds in time order."""
    ordered = sorted((t for t in trades if t.get("entered_at")),
                     key=lambda t: t["entered_at"])
    if len(ordered) < folds:
        return []

    size = len(ordered) // folds
    out: list[FoldRead] = []
    for i in range(folds):
        lo = i * size
        hi = len(ordered) if i == folds - 1 else (i + 1) * size
        chunk = ordered[lo:hi]
        values = [_net_r(t) for t in chunk]

        by_band = {}
        for name, _, _ in BANDS:
            band = [_net_r(t) for t in chunk if band_of(t.get("score", 0)) == name]
            if band:
                by_band[name] = {
                    "trades": len(band),
                    "average_r": round(statistics.mean(band), 3),
                    "net_r": round(sum(band), 2),
                    "reliable": len(band) >= MIN_FOLD_SAMPLE,
                }

        out.append(FoldRead(
            index=i + 1,
            start=chunk[0]["entered_at"][:10],
            end=chunk[-1]["entered_at"][:10],
            trades=len(chunk),
            net_r=sum(values),
            average_r=statistics.mean(values) if values else 0.0,
            by_band=by_band,
        ))
    return out


def ordering(reads: list[FoldRead], better: str = "75-85",
             worse: str = "65-75") -> dict:
    """How often the higher band actually outperformed the lower one.

    Restricted to folds where *both* bands carry a usable sample, because a
    comparison against three trades is not a comparison.
    """
    usable = [r for r in reads
              if r.by_band.get(better, {}).get("reliable")
              and r.by_band.get(worse, {}).get("reliable")]
    held = [r for r in usable
            if r.by_band[better]["average_r"] > r.by_band[worse]["average_r"]]
    lower_positive = [r for r in usable if r.by_band[worse]["average_r"] > 0]
    return {
        "better": better,
        "worse": worse,
        "folds_compared": len(usable),
        "folds_ordered_correctly": len(held),
        "holds_everywhere": bool(usable) and len(held) == len(usable),
        "lower_band_positive_folds": len(lower_positive),
        # The finding that stops a gate being raised on one sample.
        "lower_band_always_positive": bool(usable) and len(lower_positive) == len(usable),
    }


def render(reads: list[FoldRead], verdict: dict) -> str:
    """The analysis as the text a person actually reads."""
    if not reads:
        return "Not enough trades to split into folds."

    lines = [f"{sum(r.trades for r in reads)} trades in {len(reads)} contiguous folds",
             ""]
    header = f"{'band':10}" + "".join(f"{'fold ' + str(r.index):>18}" for r in reads)
    lines.append(header)
    lines.append("-" * len(header))

    for name, _, _ in BANDS:
        row = f"{name:10}"
        for read in reads:
            cell = read.by_band.get(name)
            if not cell:
                row += f"{'—':>18}"
                continue
            mark = " " if cell["reliable"] else "?"
            row += f"  n={cell['trades']:>3}{mark}{cell['average_r']:+6.3f}R"
        lines.append(row)
    lines.append("")
    lines.append("  ? = fewer than %d trades; reported, not to be reasoned from."
                 % MIN_FOLD_SAMPLE)
    lines.append("")

    for read in reads:
        lines.append(f"  fold {read.index}: {read.start} -> {read.end}  "
                     f"n={read.trades:<4} netR={read.net_r:+7.2f}  "
                     f"avg={read.average_r:+.3f}")

    lines.append("")
    if verdict["folds_compared"] == 0:
        lines.append("  Not enough per-fold evidence to judge the ordering.")
        return "\n".join(lines)

    lines.append(f"  {verdict['better']} beat {verdict['worse']} in "
                 f"{verdict['folds_ordered_correctly']} of "
                 f"{verdict['folds_compared']} folds"
                 + ("  — the score ranks setups consistently, not just on average."
                    if verdict["holds_everywhere"] else
                    "  — the ordering is NOT consistent across time."))
    if verdict["lower_band_always_positive"]:
        lines.append(f"  {verdict['worse']} was positive in every fold: these are "
                     f"weaker setups, not losing ones.")
        lines.append("  Size them down; do not raise the gate. Excluding them "
                     "discards real profit.")
    return "\n".join(lines)
