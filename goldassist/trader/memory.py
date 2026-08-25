"""Learning from your own results.

The application already stored a signal history. What it never did was *read
it back*. Every setup was presented with the same confidence as the first,
however many times that particular kind of setup, in that particular session,
had already failed.

A trader who has lost four breakouts in a row does not need a statistician to
tell them to stop taking breakouts. This module gives the application the same
crude but valuable memory: it groups closed setups by the things a trader
would actually group them by -- setup type, session, regime, score band,
direction, day character -- and reports what happened.

Two rules govern everything here, because a feedback loop that lies is worse
than none:

1. **Never make a claim a sample cannot support.** Below ``MIN_SAMPLE`` the
   module says it does not know. It does not round three trades up into a
   trend.
2. **Always state the sample size.** Every line the trader reads carries the
   ``n`` it came from, so they can weigh it themselves.

Outcomes come from ``setup_outcomes``, which the service records automatically
when a tracked setup terminates, so the memory fills up whether or not the
trader keeps a journal.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Below this, a group is reported but no conclusion is drawn from it.
MIN_SAMPLE = 8
# Above this, the record is described as established rather than early.
ESTABLISHED_SAMPLE = 20

# The dimensions a trader would slice their own results by.
DIMENSIONS = ("strategy", "setup_type", "session", "regime", "direction",
              "score_band", "day_verdict", "family")

DIMENSION_LABELS = {
    "strategy": "strategy",
    "family": "strategy family",
    "setup_type": "setup type",
    "session": "session",
    "regime": "market regime",
    "direction": "direction",
    "score_band": "score band",
    "day_verdict": "day character",
}


def score_band(score: float | None) -> str:
    if score is None:
        return "unknown"
    if score >= 85:
        return "85+"
    if score >= 75:
        return "75-84"
    if score >= 70:
        return "70-74"
    return "under 70"


@dataclass
class Record:
    key: str
    n: int
    wins: int
    losses: int
    scratches: int
    total_r: float
    avg_r: float
    win_rate: float | None

    @property
    def reliable(self) -> bool:
        return self.n >= MIN_SAMPLE

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "n": self.n,
            "wins": self.wins,
            "losses": self.losses,
            "scratches": self.scratches,
            "total_r": round(self.total_r, 2),
            "avg_r": round(self.avg_r, 2),
            "win_rate": round(self.win_rate, 1) if self.win_rate is not None else None,
            "reliable": self.reliable,
            "confidence": ("established" if self.n >= ESTABLISHED_SAMPLE
                           else "early" if self.reliable else "insufficient"),
        }


@dataclass
class MemoryReport:
    total: int
    overall: Record | None
    by_dimension: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    cautions: list[str] = field(default_factory=list)
    headline: str = ""

    def to_dict(self) -> dict:
        return {
            "total": self.total,
            "overall": self.overall.to_dict() if self.overall else None,
            "by_dimension": {
                dim: [r.to_dict() for r in records]
                for dim, records in self.by_dimension.items()
            },
            "notes": self.notes,
            "cautions": self.cautions,
            "headline": self.headline,
            "min_sample": MIN_SAMPLE,
        }


def _tally(rows: list[dict], key: str) -> Record:
    wins = sum(1 for r in rows if r.get("outcome") == "win")
    losses = sum(1 for r in rows if r.get("outcome") == "loss")
    scratches = len(rows) - wins - losses
    total_r = sum(float(r.get("r_multiple") or 0.0) for r in rows)
    decided = wins + losses
    return Record(
        key=key,
        n=len(rows),
        wins=wins,
        losses=losses,
        scratches=scratches,
        total_r=total_r,
        avg_r=total_r / len(rows) if rows else 0.0,
        win_rate=(wins / decided * 100) if decided else None,
    )


def _group(rows: list[dict], dimension: str) -> list[Record]:
    buckets: dict[str, list[dict]] = {}
    for row in rows:
        value = row.get(dimension)
        if dimension == "score_band":
            value = score_band(row.get("score"))
        if not value:
            continue
        buckets.setdefault(str(value), []).append(row)
    records = [_tally(rows_, key) for key, rows_ in buckets.items()]
    records.sort(key=lambda r: (-r.n, r.key))
    return records


class Memory:
    """The trader's own record, read back at decision time."""

    def __init__(self, db):
        self.db = db

    def rows(self, limit: int = 500) -> list[dict]:
        """Setups that actually triggered and have since closed."""
        return [
            row for row in self.db.setup_outcomes(limit=limit)
            if row.get("entered") and row.get("outcome") in ("win", "loss", "scratch")
        ]

    def strategy_edge(self, limit: int = 500) -> dict:
        """Per-strategy results, in the shape the setup book scores with.

        The book uses this to nudge a strategy's conviction up or down by a
        few points once there is a real sample behind it. Deliberately a small
        adjustment: a record of eight trades is information, not a verdict, and
        a feedback loop allowed to swing scores freely would chase noise.
        """
        edge: dict[str, dict] = {}
        for row in _group(self.rows(limit), "strategy"):
            edge[row.key] = {
                "n": row.n,
                "wins": row.wins,
                "losses": row.losses,
                "avg_r": round(row.avg_r, 3),
                "total_r": round(row.total_r, 2),
                "win_rate": row.win_rate,
                "reliable": row.reliable,
            }
        return edge

    def report(self, limit: int = 500) -> MemoryReport:
        rows = self.rows(limit)
        if not rows:
            return MemoryReport(
                total=0, overall=None,
                headline="No closed setups recorded yet — this record fills up "
                         "as tracked setups reach their targets or invalidate.",
            )

        overall = _tally(rows, "all setups")
        by_dimension = {dim: _group(rows, dim) for dim in DIMENSIONS}

        notes: list[str] = []
        cautions: list[str] = []
        for dim, records in by_dimension.items():
            label = DIMENSION_LABELS[dim]
            for record in records:
                if not record.reliable:
                    continue
                if record.avg_r <= -0.2 or (record.win_rate is not None
                                            and record.win_rate < 35):
                    cautions.append(
                        f"{label} '{record.key}': {record.wins}W/{record.losses}L "
                        f"over {record.n} setups, {record.avg_r:+.2f}R average. "
                        "This has not worked for you.")
                elif record.avg_r >= 0.35:
                    notes.append(
                        f"{label} '{record.key}': {record.wins}W/{record.losses}L "
                        f"over {record.n} setups, {record.avg_r:+.2f}R average.")

        if overall.n < MIN_SAMPLE:
            headline = (f"{overall.n} closed setup(s) on record — too few to draw "
                        f"any conclusion from yet ({MIN_SAMPLE} is the minimum "
                        "before this module will make a claim).")
        else:
            headline = (f"{overall.n} closed setups: {overall.wins}W/"
                        f"{overall.losses}L, {overall.avg_r:+.2f}R per setup, "
                        f"{overall.total_r:+.1f}R total.")

        return MemoryReport(total=overall.n, overall=overall,
                            by_dimension=by_dimension, notes=notes,
                            cautions=cautions, headline=headline)

    def verdict_on(self, context: dict, report: MemoryReport | None = None) -> dict:
        """What the trader's own record says about this specific setup.

        ``context`` carries the same keys the outcomes are grouped by. Returns
        the matching records plus, where the sample allows it, a single blunt
        line to put in front of the trader before they act.
        """
        report = report or self.report()
        matches: list[dict] = []
        warnings: list[str] = []
        support: list[str] = []

        for dim in DIMENSIONS:
            value = context.get(dim)
            if dim == "score_band" and value is None:
                value = score_band(context.get("score"))
            if not value:
                continue
            record = next((r for r in report.by_dimension.get(dim, [])
                           if r.key == str(value)), None)
            if record is None:
                continue
            entry = record.to_dict()
            entry["dimension"] = DIMENSION_LABELS[dim]
            matches.append(entry)
            if not record.reliable:
                continue
            if record.avg_r <= -0.2 or (record.win_rate is not None
                                        and record.win_rate < 35):
                warnings.append(
                    f"your {DIMENSION_LABELS[dim]} record for '{record.key}' is "
                    f"{record.wins}W/{record.losses}L ({record.avg_r:+.2f}R avg, "
                    f"n={record.n})")
            elif record.avg_r >= 0.35:
                support.append(
                    f"'{record.key}' has been good for you "
                    f"({record.avg_r:+.2f}R avg, n={record.n})")

        if warnings:
            headline = ("Your own history argues against this one: "
                        + "; ".join(warnings) + ".")
        elif support:
            headline = "Your history supports this: " + "; ".join(support) + "."
        elif report.total < MIN_SAMPLE:
            headline = (f"Not enough closed setups yet ({report.total}) for your "
                        "own record to say anything about this one.")
        else:
            headline = "Nothing in your record argues for or against this setup."

        # A conviction multiplier, kept gentle: history informs, it does not veto.
        conviction = 1.0
        if warnings:
            conviction = max(0.6, 1.0 - 0.15 * len(warnings))
        elif support:
            conviction = min(1.15, 1.0 + 0.05 * len(support))

        return {
            "headline": headline,
            "warnings": warnings,
            "support": support,
            "matches": matches,
            "conviction": round(conviction, 2),
        }
