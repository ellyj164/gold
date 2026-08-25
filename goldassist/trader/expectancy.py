"""What the record actually says, once the record is cleaned.

The application has measured its own results since the resolver was written,
and every number it produced was wrong in the same three ways. This module is
the one place those three faults are fixed, so that everything downstream --
the conviction a setup carries, the ranking of the board, the strategy table
in the interface -- reads the same cleaned figures.

**Rows that never filled were counted as trades.** The stored filter was
``entered = 1``, and two rows written before the schema migration carry
``entered = 1`` with a null ``fill_price``, a null ``exit_price`` and an
``r_multiple`` of +3.5. They are the only reason the recorded total was ever
positive. A trade is a row with a fill price; nothing else is evidence.

**Duplicates were counted as independent trades.** A double-instance bug ran
two analysis loops against one database for two days, writing the same setup
twice seconds apart under different uids. Eleven of thirty-one stored fills
are copies. Counting them inflates the sample, halves the apparent variance
and makes a coin-flip record look significant.

**Sample size was ignored.** A strategy with three fills was allowed to move
scoring as if it had three hundred. With this much data almost every
per-strategy figure is noise, and the honest response is not to discard it but
to *shrink* it: pull every estimate toward "no edge" in proportion to how
little evidence stands behind it. A strategy needs to earn its influence.

What this module will not do is gate a trade. Standing rule: context may size
a setup, it may never hide one. Everything here returns a multiplier or a
ranking, and a strategy with an atrocious record still produces its setup,
still appears on the board, still alerts -- it simply gets sized down and
ranked below its betters. A detector that has never fired is reported as
untested, not disabled.
"""

from __future__ import annotations

import logging
import math
import statistics
from dataclasses import dataclass, field
from datetime import datetime

log = logging.getLogger(__name__)

# Trades that must accumulate before an estimate is worth half its face value.
#
# This is the shrinkage constant, and it is the single most important number
# here. An estimate from `n` trades is pulled toward zero by `k / (n + k)`, so
# at n = k the measured edge counts for half. Set at 25 because the measured
# per-trade standard deviation of this system is about 1.2R: detecting a 0.1R
# edge against that noise needs hundreds of trades, and anything that lets a
# five-trade sample speak loudly is fitting the noise.
SHRINK_K = 25.0

# Below this a strategy is "emerging": its record is shown, and shrinkage
# means it barely moves anything.
MIN_SAMPLE = 8

# The most the measured record may move a conviction, in points either way.
# Wider than the ±6 it replaces, because the estimate feeding it is now
# shrunk -- the cap protects against a large *sample*, the shrinkage against
# a small one, and the two do different jobs.
EDGE_LIMIT = 12.0

# Points of conviction per 1R of shrunk expectancy. A strategy running a
# genuine +0.5R after shrinkage is worth about six points.
EDGE_SCALE = 12.0

# Two fills are the same idea if they share strategy, direction and day, and
# their entries sit within this many price units of each other. Gold's spread
# alone is ~0.3, so a tenth of a dollar is comfortably inside "the same zone"
# without merging two genuinely different levels.
DEDUP_PRICE_TOLERANCE = 0.10

# How close in time two otherwise-identical rows must be to be one duplicate
# rather than two trades. The double-instance copies landed seconds apart;
# twenty minutes is the same guard the setup book already uses to stop an
# identical resolved idea being re-admitted, so the two agree by construction.
DEDUP_WINDOW_MINUTES = 20

VALID_OUTCOMES = ("win", "loss", "scratch")


@dataclass
class Tally:
    """One group's cleaned record."""
    key: str
    label: str
    n: int = 0
    wins: int = 0
    losses: int = 0
    scratches: int = 0
    total_r: float = 0.0
    avg_r: float = 0.0
    shrunk_r: float = 0.0
    win_rate: float = 0.0
    profit_factor: float | None = None
    stdev_r: float | None = None
    status: str = "untested"       # untested | emerging | measured
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "n": self.n,
            "wins": self.wins,
            "losses": self.losses,
            "scratches": self.scratches,
            "total_r": round(self.total_r, 2),
            "avg_r": round(self.avg_r, 3),
            "shrunk_r": round(self.shrunk_r, 3),
            "win_rate": round(self.win_rate, 1),
            "profit_factor": (round(self.profit_factor, 2)
                              if self.profit_factor is not None else None),
            "stdev_r": round(self.stdev_r, 2) if self.stdev_r is not None else None,
            "status": self.status,
            "note": self.note,
        }


@dataclass
class Report:
    overall: Tally
    strategies: list[Tally] = field(default_factory=list)
    sessions: list[Tally] = field(default_factory=list)
    weekdays: list[Tally] = field(default_factory=list)
    hours: list[Tally] = field(default_factory=list)
    silent: list[str] = field(default_factory=list)
    never_filled: list[str] = field(default_factory=list)
    duplicates_removed: int = 0
    unfilled_excluded: int = 0
    headline: str = ""

    def to_dict(self) -> dict:
        return {
            "overall": self.overall.to_dict(),
            "strategies": [t.to_dict() for t in self.strategies],
            "sessions": [t.to_dict() for t in self.sessions],
            "weekdays": [t.to_dict() for t in self.weekdays],
            "hours": [t.to_dict() for t in self.hours],
            "silent": self.silent,
            "never_filled": self.never_filled,
            "duplicates_removed": self.duplicates_removed,
            "unfilled_excluded": self.unfilled_excluded,
            "headline": self.headline,
        }


# --- cleaning ---------------------------------------------------------------


def filled(rows: list[dict]) -> tuple[list[dict], int]:
    """Keep only rows that actually filled. Returns the rows and what was cut.

    ``fill_price`` is the test, not ``entered``. A row can carry ``entered=1``
    from a code path that no longer exists; it cannot carry a fill price
    without the resolver having walked a candle that traded there.
    """
    kept = [r for r in rows
            if r.get("fill_price") is not None
            and r.get("outcome") in VALID_OUTCOMES
            and r.get("r_multiple") is not None]
    return kept, len(rows) - len(kept)


def deduplicate(rows: list[dict]) -> tuple[list[dict], int]:
    """Collapse repeats of one idea to a single trade, keeping the earliest.

    Two rows are the same idea when they share strategy and direction, their
    entries are within ``DEDUP_PRICE_TOLERANCE``, *and* they were recorded
    within ``DEDUP_WINDOW_MINUTES`` of each other.

    The time window is what keeps this honest. The double-instance bug wrote
    its copies seconds apart -- two processes reacting to the same tick -- so
    a tight window catches all of them. A genuine re-entry at the same level
    looks identical on every other axis and is separated by hours, because
    the first trade had to resolve before the second could be created. Without
    the window this function silently deletes real trades, and a cleaner that
    quietly discards evidence is worse than the duplicates it removes.
    """
    buckets: dict[tuple, list[dict]] = {}
    for row in rows:
        entry = row.get("entry")
        bucket = round(float(entry) / DEDUP_PRICE_TOLERANCE) if entry is not None else None
        buckets.setdefault((row.get("strategy"), row.get("direction"), bucket),
                           []).append(row)

    unique: list[dict] = []
    removed = 0
    for group in buckets.values():
        group.sort(key=lambda r: str(r.get("filled_at") or r.get("created_at") or ""))
        kept_at: datetime | None = None
        for row in group:
            when = _when(row)
            if (kept_at is not None and when is not None
                    and abs((when - kept_at).total_seconds()) <= DEDUP_WINDOW_MINUTES * 60):
                removed += 1
                continue
            unique.append(row)
            # Compare against the row that was kept, not the previous row, so
            # a burst of five copies collapses to one rather than to a chain
            # that walks forward through the whole window.
            kept_at = when if when is not None else kept_at
    unique.sort(key=lambda r: str(r.get("filled_at") or r.get("created_at") or ""))
    return unique, removed


def clean(rows: list[dict]) -> tuple[list[dict], int, int]:
    """Both passes, in the order they must run."""
    kept, unfilled = filled(rows)
    unique, duplicates = deduplicate(kept)
    return unique, duplicates, unfilled


# --- tallying ---------------------------------------------------------------


def shrink(avg_r: float, n: int, k: float = SHRINK_K) -> float:
    """Pull an estimate toward zero in proportion to how thin its evidence is.

    The standard empirical-Bayes shrinkage, with a prior of "no edge" because
    that is the honest prior for a trading strategy: assuming an unproven
    pattern makes money is the mistake this whole module exists to stop.
    """
    if n <= 0:
        return 0.0
    return avg_r * (n / (n + k))


def tally(rows: list[dict], key: str, label: str) -> Tally:
    """Reduce a cleaned group to its record."""
    result = Tally(key=key, label=label)
    if not rows:
        result.note = "No filled trades."
        return result

    rs = [float(r["r_multiple"]) for r in rows]
    result.n = len(rs)
    result.wins = sum(1 for r in rows if r.get("outcome") == "win")
    result.losses = sum(1 for r in rows if r.get("outcome") == "loss")
    result.scratches = sum(1 for r in rows if r.get("outcome") == "scratch")
    result.total_r = sum(rs)
    result.avg_r = result.total_r / result.n
    result.shrunk_r = shrink(result.avg_r, result.n)
    result.win_rate = result.wins / result.n * 100
    result.stdev_r = statistics.pstdev(rs) if result.n > 1 else None

    gross_profit = sum(r for r in rs if r > 0)
    gross_loss = -sum(r for r in rs if r < 0)
    result.profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else None

    if result.n >= MIN_SAMPLE:
        result.status = "measured"
        result.note = (f"{result.n} trades, {result.avg_r:+.2f}R average "
                       f"(counts as {result.shrunk_r:+.2f}R after shrinkage)")
    else:
        result.status = "emerging"
        result.note = (f"only {result.n} trade{'s' if result.n != 1 else ''} -- "
                       f"{result.avg_r:+.2f}R average, too thin to weight")
    return result


def _group(rows: list[dict], field_name: str) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for row in rows:
        value = row.get(field_name)
        if value in (None, ""):
            continue
        out.setdefault(str(value), []).append(row)
    return out


def _when(row: dict) -> datetime | None:
    raw = row.get("filled_at") or row.get("created_at")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw))
    except ValueError:
        return None


WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
            "Saturday", "Sunday")


def build(rows: list[dict], known_strategies: dict[str, str] | None = None) -> Report:
    """The whole cleaned picture, from raw ``setup_outcomes`` rows.

    ``known_strategies`` maps every registered detector key to its display
    name, so the report can name the ones that have never fired. Without it
    the untested list is empty and a silent detector stays invisible -- which
    is how fourteen of them went unnoticed for a week.
    """
    unique, duplicates, unfilled = clean(rows)

    report = Report(overall=tally(unique, "all", "All setups"),
                    duplicates_removed=duplicates,
                    unfilled_excluded=unfilled)

    report.strategies = sorted(
        (tally(group, key, (known_strategies or {}).get(key, key))
         for key, group in _group(unique, "strategy").items()),
        key=lambda t: -t.n)

    report.sessions = sorted(
        (tally(group, key, key.title())
         for key, group in _group(unique, "session").items()),
        key=lambda t: -t.n)

    by_weekday: dict[str, list[dict]] = {}
    by_hour: dict[str, list[dict]] = {}
    for row in unique:
        when = _when(row)
        if when is None:
            continue
        by_weekday.setdefault(WEEKDAYS[when.weekday()], []).append(row)
        by_hour.setdefault(f"{when.hour:02d}:00", []).append(row)

    report.weekdays = [tally(g, k, k) for k, g in
                       sorted(by_weekday.items(), key=lambda kv: WEEKDAYS.index(kv[0]))]
    report.hours = [tally(g, k, f"{k} UTC") for k, g in sorted(by_hour.items())]

    # Two very different failures, and conflating them hides both.
    #
    # A detector that never produced a setup is broken or mis-calibrated --
    # the trendline-break bug was exactly this, silent for a week while
    # looking correct. A detector that produces setups which never fill is
    # working but proposing entries the market does not come back to, which is
    # a zone-placement problem, not a detection one. Same symptom in a
    # results table, opposite fixes.
    proposed = {str(r.get("strategy")) for r in rows if r.get("strategy")}
    filled_keys = {t.key for t in report.strategies}
    report.silent = sorted(k for k in (known_strategies or {}) if k not in proposed)
    report.never_filled = sorted(k for k in proposed
                                 if k not in filled_keys and k in (known_strategies or {}))

    report.headline = _headline(report)
    return report


def _headline(report: Report) -> str:
    overall = report.overall
    if overall.n == 0:
        return ("No filled trades on record yet. Every figure here fills in as "
                "tracked setups reach a target or a stop.")

    caveat = ""
    if report.duplicates_removed or report.unfilled_excluded:
        parts = []
        if report.duplicates_removed:
            parts.append(f"{report.duplicates_removed} duplicate")
        if report.unfilled_excluded:
            parts.append(f"{report.unfilled_excluded} unfilled")
        caveat = f" ({' and '.join(parts)} row(s) excluded)"

    # The sample size needed to tell this average from zero, at the measured
    # variance. Almost always the most important sentence on the page.
    needed = required_sample(overall.avg_r, overall.stdev_r)
    verdict = (f"{overall.n} clean trades, {overall.total_r:+.2f}R total, "
               f"{overall.win_rate:.0f}% wins{caveat}.")
    if needed:
        verdict += (f" At this variance it would take about {needed} trades to "
                    f"show that this average is not zero.")
    if report.silent:
        verdict += f" {len(report.silent)} detectors have never produced a setup."
    if report.never_filled:
        verdict += (f" {len(report.never_filled)} have produced setups that never "
                    f"filled.")
    return verdict


def required_sample(avg_r: float, stdev_r: float | None,
                    z: float = 1.96) -> int | None:
    """Trades needed for an edge this size to clear noise this large.

    ``n = (z * sd / effect)^2`` -- the standard two-sided test. Reported
    because it is the number that decides whether any of the rest means
    anything, and it is never the number anyone hopes for.
    """
    if not stdev_r or abs(avg_r) < 1e-9:
        return None
    n = (z * stdev_r / abs(avg_r)) ** 2
    if not math.isfinite(n) or n <= 0:
        return None
    return int(math.ceil(n))


# --- what the rest of the application consumes ------------------------------


def strategy_edge(report: Report) -> dict[str, dict]:
    """Per-strategy record in the shape the setup book scores with.

    Compatible with what ``Memory.strategy_edge`` returned, plus the shrunk
    estimate the book now weights on.
    """
    return {
        t.key: {
            "n": t.n,
            "wins": t.wins,
            "losses": t.losses,
            "avg_r": round(t.avg_r, 3),
            "shrunk_r": round(t.shrunk_r, 3),
            "total_r": round(t.total_r, 2),
            "win_rate": round(t.win_rate, 1),
            "status": t.status,
            "reliable": t.status == "measured",
        }
        for t in report.strategies
    }


def conviction_adjustment(edge: dict, key: str) -> tuple[float, str]:
    """Points to add to a candidate's conviction, and why.

    Returns the reason as well as the number so the conviction a setup carries
    can be explained rather than merely asserted -- a saturated, unexplained
    score is what made the old one useless.
    """
    record = (edge or {}).get(key) or {}
    n = int(record.get("n", 0))
    if n <= 0:
        return 0.0, "no record yet for this strategy"

    shrunk = float(record.get("shrunk_r", 0.0))
    points = max(-EDGE_LIMIT, min(EDGE_LIMIT, shrunk * EDGE_SCALE))
    if abs(points) < 0.5:
        return 0.0, f"{n} trade(s) on record, no measurable edge either way"

    direction = "earned" if points > 0 else "lost"
    return points, (f"{points:+.1f} {direction} from {n} recorded trade(s) "
                    f"averaging {record.get('avg_r', 0):+.2f}R")


def context_multiplier(report: Report, session: str | None,
                       weekday: str | None) -> tuple[float, list[str]]:
    """A sizing multiplier from *when* this setup is happening.

    Seasonality, in the only form this dataset can support: which sessions and
    which weekdays have actually paid. Shrunk like everything else, and
    bounded tightly -- the clock is the weakest evidence in the building and
    must never be able to halve a position on eight trades.
    """
    multiplier = 1.0
    notes: list[str] = []

    for tallies, value, what in ((report.sessions, session, "session"),
                                 (report.weekdays, weekday, "weekday")):
        if not value:
            continue
        match = next((t for t in tallies if t.key.lower() == str(value).lower()), None)
        if match is None or match.n < MIN_SAMPLE:
            continue
        step = max(-0.10, min(0.10, match.shrunk_r * 0.25))
        if abs(step) < 0.01:
            continue
        multiplier *= (1 + step)
        notes.append(f"{match.label} {what}s have run {match.avg_r:+.2f}R "
                     f"over {match.n} trades")

    return multiplier, notes
