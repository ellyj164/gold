"""Commitments of Traders: who is actually positioned in Gold, and how heavily.

Every other input in this application describes price. This one describes
*people*. Once a week the CFTC publishes, for every US futures market, how many
contracts each category of participant is holding long and short. For Gold it
is the only direct measurement of positioning available to anyone at any price,
and it is the input that most cleanly separates an institutional read of the
market from a retail one -- not because it is secret, but because it is
published in a fixed-width government text file that nobody bothers to parse.

What it is good for, and what it is not
---------------------------------------

**It is a crowding measure, not a timing signal.** Speculators being record
long does not mean Gold falls tomorrow; it means the marginal buyer is already
in, so the *asymmetry* has changed -- there is more fuel for a liquidation than
for a continuation. A trader who knows positioning is stretched holds a long
with less conviction and treats a sweep of the lows with more respect. That is
the whole of the claim, and this module is careful not to make a larger one.

**It is reported with a lag.** The snapshot is taken Tuesday and published
Friday afternoon. So the number is three days old on release and up to ten days
old by the following Thursday. For an intraday trader this is context on the
week, never a reason to enter or exit, and the module always reports the age.

**Non-commercials are the speculators.** The legacy report splits into
commercial (producers, refiners, banks hedging physical flow) and
non-commercial (funds and large speculators). Commercials are structurally
short because they hedge production, so their net position is not a directional
opinion. The non-commercial net is the one that reflects a view.

Why the net is also expressed against open interest
---------------------------------------------------

A raw net of +228,000 contracts means nothing without a denominator: open
interest in Gold has ranged from 400k to 800k over the past decade, so the same
absolute net is a very different degree of crowding at each. The percentage of
open interest is the comparable figure across time, and it is what the
percentile is computed on.

Sources
-------

The current week comes from the CFTC's small weekly file. History -- needed
because a positioning number without its own distribution is uninterpretable --
is seeded once from the annual archive and then extended locally as each new
week arrives. Only Gold's rows are ever kept, so the stored history is a few
kilobytes rather than the fourteen megabytes it is extracted from.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from . import http

log = logging.getLogger(__name__)

# COMEX Gold. The CFTC identifies every market by this code, and it is stable:
# names get reworded, the code does not.
GOLD_CODE = "088691"

CURRENT_URL = "https://www.cftc.gov/dea/newcot/deafut.txt"
ARCHIVE_URL = "https://www.cftc.gov/files/dea/history/deacot{year}.zip"

# Six hours. The report lands once a week; this keeps the current file fresh
# without ever asking twice for an unchanged release.
TTL = 6 * 3600

HISTORY_FILE = "cot-gold.json"

# Weeks of history kept. Three years is enough for a percentile to describe a
# full cycle of Gold positioning and stays trivially small on disk.
KEEP_WEEKS = 160

# Minimum weeks before a percentile is reported at all. Below this the
# distribution is not a distribution.
MIN_HISTORY = 26

# Percentile bounds for calling positioning crowded. Deliberately wide: the
# interesting states are the extremes, and a reading at the 60th percentile is
# not news.
CROWDED_HIGH = 85.0
CROWDED_LOW = 15.0

# Week-over-week change in net contracts that counts as a real shift rather
# than noise, as a share of open interest.
SHIFT_SIGNIFICANT_PCT = 2.0

# The weekly file has no header row, so these positions are read directly. The
# arithmetic is self-checking and verified on every parse: non-commercial long
# plus spreading plus commercial long must equal total reportable long, and
# that plus non-reportable long must equal open interest. A layout change
# therefore fails loudly instead of silently returning a wrong number.
COL_DATE = 2
COL_CODE = 3
COL_OPEN_INTEREST = 7
COL_NC_LONG = 8
COL_NC_SHORT = 9
COL_NC_SPREAD = 10
COL_COMM_LONG = 11
COL_COMM_SHORT = 12
COL_TOTAL_LONG = 13
COL_TOTAL_SHORT = 14
COL_NONREPT_LONG = 15
COL_NONREPT_SHORT = 16


@dataclass
class Week:
    """One weekly snapshot of Gold positioning."""

    report_date: str
    open_interest: int
    nc_long: int
    nc_short: int
    nc_spread: int
    comm_long: int
    comm_short: int

    @property
    def nc_net(self) -> int:
        """Speculative net length in contracts."""
        return self.nc_long - self.nc_short

    @property
    def comm_net(self) -> int:
        return self.comm_long - self.comm_short

    @property
    def nc_net_pct_oi(self) -> float:
        """The comparable figure: net speculative length as a share of the market."""
        return (self.nc_net / self.open_interest * 100.0) if self.open_interest else 0.0

    def to_dict(self) -> dict:
        return {
            "report_date": self.report_date,
            "open_interest": self.open_interest,
            "nc_long": self.nc_long,
            "nc_short": self.nc_short,
            "nc_spread": self.nc_spread,
            "comm_long": self.comm_long,
            "comm_short": self.comm_short,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Week":
        return cls(
            report_date=str(d["report_date"]),
            open_interest=int(d["open_interest"]),
            nc_long=int(d["nc_long"]),
            nc_short=int(d["nc_short"]),
            nc_spread=int(d.get("nc_spread", 0)),
            comm_long=int(d.get("comm_long", 0)),
            comm_short=int(d.get("comm_short", 0)),
        )


@dataclass
class CotRead:
    available: bool = False
    report_date: str = ""
    age_days: int | None = None
    net: int | None = None
    net_pct_oi: float | None = None
    net_change: int | None = None
    open_interest: int | None = None
    oi_change_pct: float | None = None
    percentile: float | None = None
    history_weeks: int = 0
    crowding: str = "unknown"        # crowded long | crowded short | neutral | unknown
    supports: str = "neutral"        # bullish | bearish | neutral | conflicted
    headline: str = ""
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "available": self.available,
            "report_date": self.report_date,
            "age_days": self.age_days,
            "net": self.net,
            "net_pct_oi": round(self.net_pct_oi, 2) if self.net_pct_oi is not None else None,
            "net_change": self.net_change,
            "open_interest": self.open_interest,
            "oi_change_pct": (round(self.oi_change_pct, 1)
                              if self.oi_change_pct is not None else None),
            "percentile": round(self.percentile, 0) if self.percentile is not None else None,
            "history_weeks": self.history_weeks,
            "crowding": self.crowding,
            "supports": self.supports,
            "headline": self.headline,
            "notes": self.notes,
        }


# --- parsing ----------------------------------------------------------------


def _row_to_week(parts: list[str]) -> Week | None:
    """One CFTC row into a snapshot, rejecting it if the arithmetic disagrees.

    The consistency check is the point. This is a positional parse of a
    government text file with no header, and the failure mode of a positional
    parse is not an exception -- it is a plausible-looking wrong number. The
    report's own internal identities catch that: reportable longs must sum, and
    reportable plus non-reportable must equal open interest. A file whose
    layout has shifted fails both and is dropped.
    """
    try:
        values = {i: int(parts[i]) for i in (
            COL_OPEN_INTEREST, COL_NC_LONG, COL_NC_SHORT, COL_NC_SPREAD,
            COL_COMM_LONG, COL_COMM_SHORT, COL_TOTAL_LONG, COL_TOTAL_SHORT,
            COL_NONREPT_LONG, COL_NONREPT_SHORT)}
        report_date = parts[COL_DATE].strip().strip('"')
        datetime.strptime(report_date, "%Y-%m-%d")
    except (ValueError, IndexError, KeyError):
        return None

    long_side = (values[COL_NC_LONG] + values[COL_NC_SPREAD] + values[COL_COMM_LONG])
    short_side = (values[COL_NC_SHORT] + values[COL_NC_SPREAD] + values[COL_COMM_SHORT])
    if long_side != values[COL_TOTAL_LONG] or short_side != values[COL_TOTAL_SHORT]:
        log.warning("COT row failed its reportable-side identity; layout may have changed")
        return None
    if (values[COL_TOTAL_LONG] + values[COL_NONREPT_LONG]) != values[COL_OPEN_INTEREST]:
        log.warning("COT row failed its open-interest identity; layout may have changed")
        return None

    return Week(
        report_date=report_date,
        open_interest=values[COL_OPEN_INTEREST],
        nc_long=values[COL_NC_LONG],
        nc_short=values[COL_NC_SHORT],
        nc_spread=values[COL_NC_SPREAD],
        comm_long=values[COL_COMM_LONG],
        comm_short=values[COL_COMM_SHORT],
    )


def _gold_rows(text: str) -> list[Week]:
    """Every Gold snapshot in a CFTC text file, oldest first.

    Parsed with :mod:`csv` rather than by splitting on commas: market names
    contain commas inside quotes ("GOLD - COMMODITY EXCHANGE INC."), and a
    naive split shifts every subsequent column by one.
    """
    weeks: list[Week] = []
    for parts in csv.reader(io.StringIO(text)):
        if len(parts) <= COL_NONREPT_SHORT:
            continue
        if parts[COL_CODE].strip().strip('"') != GOLD_CODE:
            continue
        week = _row_to_week(parts)
        if week is not None:
            weeks.append(week)
    weeks.sort(key=lambda w: w.report_date)
    return weeks


# --- local history ----------------------------------------------------------


def _load_history() -> list[Week]:
    path = http.cache_path(HISTORY_FILE)
    try:
        if not path.exists():
            return []
        raw = json.loads(path.read_text(encoding="utf-8"))
        return [Week.from_dict(d) for d in raw.get("weeks", [])]
    except (OSError, ValueError, KeyError, TypeError):
        log.debug("Discarding unreadable COT history", exc_info=True)
        return []


def _save_history(weeks: list[Week]) -> None:
    path = http.cache_path(HISTORY_FILE)
    try:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(
            {"weeks": [w.to_dict() for w in weeks[-KEEP_WEEKS:]]}), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        log.debug("Could not write COT history", exc_info=True)


def _merge(existing: list[Week], incoming: list[Week]) -> list[Week]:
    """Union by report date, newest definition winning, oldest first."""
    by_date = {w.report_date: w for w in existing}
    by_date.update({w.report_date: w for w in incoming})
    return [by_date[d] for d in sorted(by_date)][-KEEP_WEEKS:]


def _seed_from_archive(history: list[Week]) -> list[Week]:
    """Fill history from the annual archives, once, when it is too thin.

    Two years are pulled so that a January run is not left with three weeks of
    a distribution. Each archive is a 14 MB zip holding every market; only
    Gold's fifty-odd rows survive into storage.
    """
    year = datetime.now(timezone.utc).year
    for target in (year, year - 1):
        raw, error = http.get_bytes(ARCHIVE_URL.format(year=target))
        if raw is None:
            log.info("COT archive for %d unavailable: %s", target, error)
            continue
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                name = next((n for n in archive.namelist()
                             if n.lower().endswith((".txt", ".csv"))), None)
                if name is None:
                    continue
                with archive.open(name) as handle:
                    text = handle.read().decode("utf-8", "replace")
        except (zipfile.BadZipFile, OSError, ValueError):
            log.info("COT archive for %d could not be read", target, exc_info=True)
            continue
        found = _gold_rows(text)
        log.info("Seeded %d weeks of Gold COT history from %d", len(found), target)
        history = _merge(history, found)
        if len(history) >= MIN_HISTORY:
            break
    return history


# --- the read ---------------------------------------------------------------


def read() -> CotRead:
    """The latest Gold positioning, in the context of its own history."""
    out = CotRead()

    fetched = http.get(CURRENT_URL, TTL)
    current = _gold_rows(fetched.body) if fetched.ok else []

    history = _load_history()
    if current:
        history = _merge(history, current)

    # Seeding is attempted only when history is genuinely too thin to reason
    # from, so the 14 MB archive is fetched roughly once in the life of an
    # installation rather than on a schedule.
    if len(history) < MIN_HISTORY:
        history = _seed_from_archive(history)

    if history:
        _save_history(history)

    if not history:
        out.headline = ("Positioning data is unavailable" +
                        (f" — {fetched.error}" if fetched.error else "."))
        return out

    latest = history[-1]
    out.available = True
    out.report_date = latest.report_date
    out.net = latest.nc_net
    out.net_pct_oi = latest.nc_net_pct_oi
    out.open_interest = latest.open_interest
    out.history_weeks = len(history)

    try:
        observed = datetime.strptime(latest.report_date, "%Y-%m-%d").date()
        out.age_days = (date.today() - observed).days
    except ValueError:
        out.age_days = None

    if len(history) >= 2:
        previous = history[-2]
        out.net_change = latest.nc_net - previous.nc_net
        if previous.open_interest:
            out.oi_change_pct = ((latest.open_interest / previous.open_interest - 1)
                                 * 100.0)

    if len(history) >= MIN_HISTORY:
        series = [w.nc_net_pct_oi for w in history]
        below = sum(1 for v in series if v < latest.nc_net_pct_oi)
        out.percentile = below / len(series) * 100.0

    _interpret(out, latest, history)
    return out


def _interpret(out: CotRead, latest: Week, history: list[Week]) -> None:
    """Turn the positioning into the sentences a trader would actually use."""
    notes: list[str] = []
    side = "long" if latest.nc_net >= 0 else "short"

    headline = (f"Large speculators are net {side} {abs(latest.nc_net):,} contracts "
                f"({latest.nc_net_pct_oi:+.1f}% of open interest) as of "
                f"{latest.report_date}")
    if out.age_days is not None:
        headline += f", {out.age_days} days ago"
    headline += "."

    if out.percentile is None:
        notes.append(
            f"Only {len(history)} weeks of history are stored, so this level "
            "cannot yet be placed in its own distribution. It fills in as each "
            "weekly report arrives.")
        out.crowding = "unknown"
    elif out.percentile >= CROWDED_HIGH:
        out.crowding = "crowded long"
        out.supports = "bearish"
        notes.append(
            f"That is the {out.percentile:.0f}th percentile of the last "
            f"{len(history)} weeks — positioning is crowded long. The marginal "
            "buyer is already in, so a long here has less fuel behind it and a "
            "sweep of the lows has more. This argues for banking the first "
            "objective rather than holding a runner, not for shorting.")
    elif out.percentile <= CROWDED_LOW:
        out.crowding = "crowded short"
        out.supports = "bullish"
        notes.append(
            f"That is the {out.percentile:.0f}th percentile of the last "
            f"{len(history)} weeks — speculative length is unusually light. "
            "There is room for buyers to return, which favours upside "
            "continuation over a fade.")
    else:
        out.crowding = "neutral"
        notes.append(
            f"That is the {out.percentile:.0f}th percentile of the last "
            f"{len(history)} weeks — positioning is unremarkable and is not an "
            "argument either way.")

    # A weekly shift large relative to the market is worth naming on its own,
    # independently of the level: it says the money moved, not merely that it
    # is somewhere.
    if out.net_change is not None and latest.open_interest:
        shift_pct = abs(out.net_change) / latest.open_interest * 100.0
        if shift_pct >= SHIFT_SIGNIFICANT_PCT:
            direction = "added to" if out.net_change > 0 else "cut"
            notes.append(
                f"Speculators {direction} net length by {abs(out.net_change):,} "
                f"contracts on the week ({shift_pct:.1f}% of open interest) — a "
                "decisive shift rather than drift.")
            if out.crowding == "neutral":
                out.supports = "bullish" if out.net_change > 0 else "bearish"

    if out.oi_change_pct is not None and abs(out.oi_change_pct) >= 5:
        notes.append(
            f"Open interest is {out.oi_change_pct:+.1f}% on the week: "
            + ("new money is entering the market, which tends to sustain a move."
               if out.oi_change_pct > 0 else
               "positions are being closed, which tends to precede a stall."))

    notes.append(
        "Positioning is a crowding measure, not a timing signal. It changes how "
        "much conviction a setup deserves; it never decides the direction.")

    out.headline = headline
    out.notes = notes
