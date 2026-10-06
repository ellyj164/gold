"""Federal Reserve series: the macro backbone Gold is actually priced against.

Gold's price is, to a first approximation, the market's answer to one question:
what does it cost to hold an asset that pays nothing? That cost is the **real
yield** -- the return on government debt after expected inflation is taken out
-- and it is the single most reliable macro driver of the metal. When the real
yield falls, the penalty for holding a non-yielding store of value falls with
it. When it rises, every ounce of Gold is competing against a bond that now
pays a genuine return.

``trader/intermarket`` already knew this and could not act on it. Its yield
reading carries the note "nominal proxy; real yields are not available from a
retail feed", which was true of MetaTrader and not true of the world: the
Federal Reserve Bank of St. Louis publishes the constant-maturity TIPS yield
daily, for free, without an API key. This module reads it.

What is read, and why each one earns its place
----------------------------------------------

``DFII10`` **10-year TIPS yield -- the real yield.**
    The primary driver. Read as a level (where it sits in its own recent range)
    and as a change (which way it has moved this week), because Gold responds
    to both: an extreme level sets the regime, a sharp move sets the day.

``DFII5`` **5-year TIPS yield.**
    The front of the real curve. Moves faster than the 10-year and is the
    better read on a policy repricing.

``T10YIE`` **10-year breakeven inflation.**
    What the market expects inflation to average. Rising breakevens with stable
    nominals means falling real yields, which is Gold's best environment; it is
    also the read that separates "inflation hedge" buying from "rate cut"
    buying, and those two behave differently.

``T5YIFR`` **5-year, 5-year forward inflation expectation.**
    The Fed's own preferred measure of whether long-run expectations are
    anchored. A move here is a regime event, not a data point.

``DGS10`` / ``DGS2`` **Nominal 10-year and 2-year.**
    The nominal curve. Carried because the 2s10s spread is the cleanest
    available read on whether the market is pricing a slowdown, and because the
    nominal-minus-real arithmetic has to reconcile.

``DTWEXBGS`` **Broad trade-weighted dollar.**
    The Fed's own dollar index, which is a considerably better measure than the
    ICE DXY's six currencies -- and much better than the EURUSD inversion this
    application falls back to when the broker carries no index at all.

Publication cadence and what it means for an intraday trader
------------------------------------------------------------

These series update once per business day, with a lag of a day or two. That is
not a defect for this use and it is important not to pretend otherwise: nothing
here times an entry. What it does is tell an intraday trader which way the
week's macro wind is blowing, so a long into a collapsing real yield is
understood as swimming with the tide and a short into the same thing is
understood as swimming against it. The TTL is set to six hours accordingly.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from . import http

log = logging.getLogger(__name__)

BASE = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}&cosd={start}"

# One request per series, bounded, with a pause between them.
#
# FRED will serve all seven as a single wide CSV, and measurement says not to:
# it ignores the date bound on a multi-series export, so the join runs from
# 1962 and returns 535 KB in seven seconds, against 9 KB in one second for a
# bounded single series. Seven small requests move an eighth of the bytes and
# leave each series independently able to succeed -- a partial macro read is
# useful, an all-or-nothing one is not.
#
# What the burst must not do is arrive all at once. FRED resets a connection
# under a rapid burst -- this module was watched doing exactly that during
# development, and the failures looked like an outage rather than the
# rate-limit they were. A short pause between requests removes it entirely.
HISTORY_DAYS = 800
REQUEST_PAUSE = 1.0


# Six hours. The series are daily; this refreshes a few times per session
# without ever asking twice for the same unchanged number.
TTL = 6 * 3600

# Observations kept per series. Two years of business days, which is enough for
# a percentile that means something and small enough to parse instantly.
KEEP = 520

# Lookbacks, in observations (business days), for the change readings.
FAST_WINDOW = 5
SLOW_WINDOW = 20

# The series this module reads, with the label the interface shows and the
# units the number is quoted in.
SERIES = {
    "real_10y": ("DFII10", "10-year real yield", "%"),
    "real_5y": ("DFII5", "5-year real yield", "%"),
    "breakeven_10y": ("T10YIE", "10-year inflation breakeven", "%"),
    "forward_inflation": ("T5YIFR", "5y5y forward inflation", "%"),
    "nominal_10y": ("DGS10", "10-year Treasury", "%"),
    "nominal_2y": ("DGS2", "2-year Treasury", "%"),
    "dollar_broad": ("DTWEXBGS", "broad trade-weighted dollar", "index"),
}

# A real-yield move of this many basis points over the fast window is a push
# rather than drift. Gold reliably notices a 10bp week; it ignores a 2bp one.
REAL_YIELD_SIGNIFICANT_BP = 10.0

# Percentile bounds for calling a level extreme rather than merely high.
EXTREME_LOW = 20.0
EXTREME_HIGH = 80.0


@dataclass
class Series:
    """One economic series, and the readings taken from it."""

    key: str
    series_id: str
    label: str
    units: str
    available: bool = False
    value: float | None = None
    observed_on: str = ""
    change_fast: float | None = None       # change over FAST_WINDOW, in units
    change_slow: float | None = None       # change over SLOW_WINDOW, in units
    percentile: float | None = None        # where the level sits in KEEP history
    age_note: str = ""
    note: str = ""

    @property
    def change_bp(self) -> float | None:
        """The fast change in basis points, for the yield series."""
        if self.change_fast is None or self.units != "%":
            return None
        return self.change_fast * 100.0

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "series_id": self.series_id,
            "label": self.label,
            "units": self.units,
            "available": self.available,
            "value": round(self.value, 4) if self.value is not None else None,
            "observed_on": self.observed_on,
            "change_fast": (round(self.change_fast, 4)
                            if self.change_fast is not None else None),
            "change_slow": (round(self.change_slow, 4)
                            if self.change_slow is not None else None),
            "change_bp": round(self.change_bp, 1) if self.change_bp is not None else None,
            "percentile": (round(self.percentile, 0)
                           if self.percentile is not None else None),
            "age_note": self.age_note,
            "note": self.note,
        }


@dataclass
class FredRead:
    """Every series this module could obtain, plus what they say together."""

    available: bool = False
    series: dict[str, Series] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)
    headline: str = ""
    supports: str = "neutral"          # bullish | bearish | neutral | conflicted
    drivers: list[str] = field(default_factory=list)

    def get(self, key: str) -> Series | None:
        found = self.series.get(key)
        return found if found is not None and found.available else None

    def to_dict(self) -> dict:
        return {
            "available": self.available,
            "series": {k: v.to_dict() for k, v in self.series.items()},
            "missing": self.missing,
            "headline": self.headline,
            "supports": self.supports,
            "drivers": self.drivers,
        }


def _parse_wide(body: str) -> dict[str, list[tuple[date, float]]]:
    """FRED's wide multi-series CSV into observations keyed by series id.

    The header names the columns, so this reads **by name, not by position**:
    FRED does not promise to return series in the order they were requested,
    and a positional parse would one day attribute the 2-year yield to the
    10-year real yield without anything appearing to be wrong.

    A blank cell is a genuine absence -- a market holiday, or a series that
    does not run as far back as the widest one in the request -- and is skipped
    rather than filled. FRED writes "." for a missing observation as well.
    """
    lines = body.splitlines()
    if not lines:
        return {}

    header = [h.strip() for h in lines[0].split(",")]
    if len(header) < 2:
        return {}

    out: dict[str, list[tuple[date, float]]] = {name: [] for name in header[1:]}
    for line in lines[1:]:
        parts = line.split(",")
        if len(parts) != len(header):
            continue
        try:
            when = datetime.strptime(parts[0].strip(), "%Y-%m-%d").date()
        except ValueError:
            continue
        for name, raw in zip(header[1:], parts[1:]):
            value = raw.strip()
            if not value or value == ".":
                continue
            try:
                out[name].append((when, float(value)))
            except ValueError:
                continue

    return {name: rows[-KEEP:] for name, rows in out.items()}


def _series_url(series_id: str) -> str:
    """The request URL, with a start date that is stable within a month.

    Anchored to the first of the month rather than to exactly ``HISTORY_DAYS``
    ago, because the URL is the cache key: a bound that moved every day would
    miss the cache every day, re-download every series each morning, and leave
    a trail of orphaned cache files behind it.
    """
    earliest = date.today() - timedelta(days=HISTORY_DAYS)
    return BASE.format(series=series_id, start=earliest.replace(day=1).isoformat())


def _build_series(key: str, series_id: str, label: str, units: str,
                  observations: list[tuple[date, float]],
                  age_note: str, stale: bool) -> Series:
    """One column of the response reduced to the readings taken from it."""
    result = Series(key=key, series_id=series_id, label=label, units=units)
    result.age_note = age_note

    if not observations:
        result.note = "the source returned no observations for this series"
        return result

    values = [v for _, v in observations]
    result.available = True
    result.value = values[-1]
    result.observed_on = observations[-1][0].isoformat()

    if len(values) > FAST_WINDOW:
        result.change_fast = values[-1] - values[-1 - FAST_WINDOW]
    if len(values) > SLOW_WINDOW:
        result.change_slow = values[-1] - values[-1 - SLOW_WINDOW]
    if len(values) >= 30:
        below = sum(1 for v in values if v < values[-1])
        result.percentile = below / len(values) * 100.0

    result.note = f"{result.value:.2f}{'%' if units == '%' else ''} on {result.observed_on}"
    if stale:
        result.note += f" (cached, refreshed {age_note})"
    return result


def read() -> FredRead:
    """Every series, and the composed macro read for Gold.

    One request, parsed into every column. A series missing from the response
    -- FRED discontinues and renames series occasionally -- produces an
    unavailable reading for that driver and leaves the rest intact.
    """
    out = FredRead()
    for index, (key, (series_id, label, units)) in enumerate(SERIES.items()):
        # Spaced out, and only when a request is actually going to leave the
        # machine: a cache hit costs nothing and must not be made to wait.
        fetched = http.get(_series_url(series_id), TTL)
        if not fetched.from_cache and index < len(SERIES) - 1:
            time.sleep(REQUEST_PAUSE)

        if not fetched.ok:
            series = Series(key=key, series_id=series_id, label=label,
                            units=units, age_note=fetched.age_note(),
                            note=f"unavailable — {fetched.error}")
        else:
            series = _build_series(key, series_id, label, units,
                                   _parse_wide(fetched.body).get(series_id, []),
                                   fetched.age_note(), fetched.stale)
        out.series[key] = series
        if not series.available:
            out.missing.append(label)

    out.available = any(s.available for s in out.series.values())
    if not out.available:
        out.headline = ("No macro series could be reached, so Gold is being read "
                        "on its chart alone.")
        return out

    out.supports, out.headline, out.drivers = _interpret(out)
    return out


def _interpret(read: FredRead) -> tuple[str, str, list[str]]:
    """What the macro backbone says about Gold, and the arithmetic behind it.

    Weighted the way the market weights them: the real yield leads, the
    breakeven modifies, the dollar confirms. A conflict between them is
    reported as a conflict rather than averaged into a false consensus --
    "conflicted" is a genuine and common state of the Gold macro picture and
    hiding it behind a net score would be the whole point missed.
    """
    drivers: list[str] = []
    votes: list[tuple[str, float]] = []      # (direction, weight)

    real = read.get("real_10y")
    if real is not None:
        bp = real.change_bp
        if bp is not None and abs(bp) >= REAL_YIELD_SIGNIFICANT_BP:
            direction = "bearish" if bp > 0 else "bullish"
            votes.append((direction, 3.0))
            drivers.append(
                f"The 10-year real yield is {real.value:.2f}% and has moved "
                f"{bp:+.0f}bp in a week — {'a headwind' if bp > 0 else 'a tailwind'} "
                f"for Gold, and the strongest single macro signal available.")
        elif bp is not None:
            drivers.append(
                f"The 10-year real yield is {real.value:.2f}% and essentially "
                f"unchanged on the week ({bp:+.0f}bp) — no macro push either way.")

        if real.percentile is not None:
            if real.percentile <= EXTREME_LOW:
                votes.append(("bullish", 1.5))
                drivers.append(
                    f"That level is in the bottom {real.percentile:.0f}% of its "
                    "two-year range: holding Gold costs unusually little right now.")
            elif real.percentile >= EXTREME_HIGH:
                votes.append(("bearish", 1.5))
                drivers.append(
                    f"That level is in the top {real.percentile:.0f}% of its "
                    "two-year range: the opportunity cost of holding Gold is "
                    "unusually high.")

    breakeven = read.get("breakeven_10y")
    if breakeven is not None and breakeven.change_bp is not None:
        bp = breakeven.change_bp
        if abs(bp) >= REAL_YIELD_SIGNIFICANT_BP:
            votes.append(("bullish" if bp > 0 else "bearish", 1.5))
            drivers.append(
                f"Inflation breakevens are {breakeven.value:.2f}% and {bp:+.0f}bp "
                f"on the week — {'rising' if bp > 0 else 'falling'} inflation "
                f"expectations, {'supportive of' if bp > 0 else 'a drag on'} Gold.")

    forward = read.get("forward_inflation")
    if forward is not None and forward.change_bp is not None and abs(forward.change_bp) >= 15:
        drivers.append(
            f"5y5y forward inflation has moved {forward.change_bp:+.0f}bp to "
            f"{forward.value:.2f}% — long-run expectations are repricing, which "
            "is a regime event rather than a daily fluctuation.")

    dollar = read.get("dollar_broad")
    if dollar is not None and dollar.change_fast is not None and dollar.value:
        pct = dollar.change_fast / (dollar.value - dollar.change_fast) * 100
        if abs(pct) >= 0.5:
            votes.append(("bearish" if pct > 0 else "bullish", 1.0))
            drivers.append(
                f"The broad trade-weighted dollar is {pct:+.1f}% on the week — "
                f"{'a headwind' if pct > 0 else 'a tailwind'} for a dollar-priced "
                "asset.")

    curve = _curve(read)
    if curve is not None:
        drivers.append(curve)

    return _tally(votes, drivers)


def _curve(read: FredRead) -> str | None:
    """The 2s10s spread, when both legs are available."""
    ten, two = read.get("nominal_10y"), read.get("nominal_2y")
    if ten is None or two is None or ten.value is None or two.value is None:
        return None
    spread = (ten.value - two.value) * 100
    shape = ("inverted" if spread < 0 else "flat" if spread < 25 else "positively sloped")
    return (f"The 2s10s curve is {spread:+.0f}bp ({shape}). "
            + ("An inverted curve prices a slowdown, which historically favours Gold "
               "once the first cut is in sight." if spread < 0 else
               "Curve shape is not currently arguing either way for Gold."))


def _tally(votes: list[tuple[str, float]], drivers: list[str]) -> tuple[str, str, list[str]]:
    """Reduce weighted votes to a direction, honestly naming a conflict."""
    if not votes:
        return ("neutral",
                "The macro picture is quiet: nothing in the rates or dollar "
                "complex is pushing Gold this week.", drivers)

    bullish = sum(w for d, w in votes if d == "bullish")
    bearish = sum(w for d, w in votes if d == "bearish")
    total = bullish + bearish

    # A near-even split is a real state of the world, not a rounding problem.
    if total and min(bullish, bearish) / total >= 0.35:
        return ("conflicted",
                f"The macro drivers disagree ({bullish:.1f} bullish against "
                f"{bearish:.1f} bearish). Gold has no macro tailwind to lean on, "
                "so treat this as a technicals-only session.", drivers)

    if bullish > bearish:
        return ("bullish",
                "The macro backbone supports Gold: the rates and dollar complex "
                "is pushing the same way as a long.", drivers)
    return ("bearish",
            "The macro backbone is against Gold: the rates and dollar complex is "
            "pushing the same way as a short.", drivers)
