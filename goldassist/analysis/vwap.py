"""Anchored VWAP and its standard-deviation bands.

The volume-weighted average price is the reference an institutional desk is
actually measured against, and for an intraday trader it is the most useful
single line on the chart -- not because it predicts anything, but because it is
where the day's business has been done. Price above it means buyers have paid
up relative to everyone who traded today; below it means sellers have. Every
mean-reversion trade in a range and every pullback entry in a trend is,
implicitly, a trade about distance from this line.

Why *anchored* rather than rolling
----------------------------------

A rolling VWAP over the last N bars answers a question nobody asks. The useful
anchor is an event: the day's open, the session's open, the week's open, the
swing that started the current leg. From that moment forward the average is the
real cost basis of everyone who has participated since, which is why price
reliably reacts to it -- a large position opened after the anchor is at
breakeven exactly there.

This module computes several anchors at once, because they answer different
questions and an intraday trader uses all of them within one session:

**Day** -- from 00:00 UTC. The reference every algorithmic desk trades around.
**Session** -- from the current session's open (Asia, London, New York). The
one that matters most in the first hours of a session, when the day anchor is
still dominated by a session the trader was asleep for.
**Week** -- from Monday's open. Slower, and the one that frames whether this
week is trending or rotating.

The bands
---------

Standard deviations of price around the anchored average, volume-weighted like
the average itself. They are not Bollinger bands: Bollinger measures deviation
around a *moving* average of closes, so it travels with price and normalises
away exactly the excursion an intraday trader is trying to measure. A VWAP band
is anchored, so the first standard deviation genuinely marks "this far from
today's fair value and no further, usually".

The conventional reads, and the ones this module reports:

* Price at the **first band** in a rotating market is where a fade is offered.
* Price at the **second or third band** is a genuine outlier -- either
  exhaustion, or a trend day in which the bands are being ridden rather than
  respected. The two are distinguished by whether VWAP itself is sloping, which
  is why the slope is reported alongside.
* A **reclaim** of VWAP after a session below it is one of the cleanest
  intraday continuation signals there is, and it happens several times a week.

What this module does not do
----------------------------

It produces no signal of its own. It measures where price sits relative to a
set of references and reports it, in the same way ``levels`` and ``zones`` do,
and the strategy layer decides what that is worth.

The volume is broker **tick volume**, not contracts, for the same reason
``volumeprofile`` uses it: no retail Gold feed carries real exchange volume.
Tick volume tracks activity closely enough that the weighting is meaningful,
and the module says so rather than implying a precision it does not have.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

# Session opens in UTC, matching goldassist.trader.sessions and the backtest's
# own SESSIONS map so the whole application agrees about when a session starts.
SESSION_OPENS = {"asia": 0, "london": 7, "newyork": 12}

# Band multiples reported. One and two are the ones traders act on; three is
# carried because a touch of it is rare enough to be worth naming.
BAND_MULTIPLES = (1.0, 2.0, 3.0)

# Minimum bars before an anchored VWAP means anything. A VWAP computed from
# three bars is just the price, and reporting it as a reference invites a trade
# against a number that carries no information.
MIN_BARS = 10

# Slope is measured over this many bars, as a share of the VWAP value, and
# above this threshold the anchor is called rising or falling rather than flat.
# Gold moves ~1% in a normal day, so a tenth of a percent over the window is a
# genuine directional drift rather than noise.
SLOPE_BARS = 20
SLOPE_SIGNIFICANT_PCT = 0.10

# How close price must be to an anchor, as a share of the first band width, to
# count as "at" it rather than merely near it.
AT_TOLERANCE = 0.15


def _typical_price(df: pd.DataFrame) -> np.ndarray:
    """The price each bar's volume is attributed to.

    (high + low + close) / 3 rather than the close, which is the standard
    convention and materially better on a wide bar: a candle that ranged four
    dollars and closed at its high did not do all of its business at the high.
    """
    return ((df["high"].to_numpy(dtype=float)
             + df["low"].to_numpy(dtype=float)
             + df["close"].to_numpy(dtype=float)) / 3.0)


def _weights(df: pd.DataFrame) -> np.ndarray:
    """Bar weights, falling back to equal weighting when volume is absent.

    A feed that reports zero volume must still produce a usable average -- it
    degrades to a simple anchored mean of typical prices, which is what a VWAP
    is when every bar carries the same weight. Silently returning nothing would
    remove the reference entirely on exactly the feeds that need it most.
    """
    volume = df["volume"].to_numpy(dtype=float)
    volume = np.where(np.isfinite(volume) & (volume > 0), volume, 0.0)
    if volume.sum() <= 0:
        return np.ones(len(df), dtype=float)
    # A single zero-volume bar inside an otherwise normal series would
    # contribute nothing at all; giving it the minimum non-zero weight keeps it
    # in the average without letting it distort one.
    smallest = volume[volume > 0].min()
    return np.where(volume > 0, volume, smallest)


def anchored(df: pd.DataFrame, start: pd.Timestamp | None = None) -> dict | None:
    """VWAP and bands for ``df``, anchored at ``start`` (or the whole frame).

    Returns None when there is not enough data for the reference to mean
    anything, which callers must treat as "no reading" rather than as zero.
    """
    if df is None or len(df) == 0:
        return None

    window = df if start is None else df[df.index >= start]
    if len(window) < MIN_BARS:
        return None

    prices = _typical_price(window)
    weights = _weights(window)
    cumulative_weight = weights.cumsum()
    vwap_series = (prices * weights).cumsum() / cumulative_weight

    # Volume-weighted variance about the running VWAP, which is what makes
    # these bands the anchored analogue of a standard deviation rather than a
    # rolling one.
    squared = ((prices - vwap_series) ** 2 * weights).cumsum() / cumulative_weight
    deviation = float(np.sqrt(max(squared[-1], 0.0)))
    vwap = float(vwap_series[-1])

    price = float(window["close"].iloc[-1])
    bands = {}
    for multiple in BAND_MULTIPLES:
        bands[f"upper_{multiple:g}"] = round(vwap + multiple * deviation, 3)
        bands[f"lower_{multiple:g}"] = round(vwap - multiple * deviation, 3)

    return {
        "vwap": round(vwap, 3),
        "deviation": round(deviation, 3),
        "bands": bands,
        "bars": int(len(window)),
        "anchored_at": window.index[0].isoformat(),
        "price": round(price, 3),
        "distance": round(price - vwap, 3),
        "distance_sigma": round((price - vwap) / deviation, 2) if deviation > 0 else 0.0,
        "position": _position(price, vwap, deviation),
        "slope": _slope(vwap_series),
        "volume_weighted": bool(window["volume"].to_numpy(dtype=float).sum() > 0),
    }


def _position(price: float, vwap: float, deviation: float) -> str:
    """Where price sits, in the vocabulary a trader would use."""
    if deviation <= 0:
        return "at vwap"
    sigma = (price - vwap) / deviation
    if abs(sigma) <= AT_TOLERANCE:
        return "at vwap"
    if sigma >= 3:
        return "above the third band"
    if sigma >= 2:
        return "above the second band"
    if sigma >= 1:
        return "above the first band"
    if sigma > 0:
        return "above vwap"
    if sigma <= -3:
        return "below the third band"
    if sigma <= -2:
        return "below the second band"
    if sigma <= -1:
        return "below the first band"
    return "below vwap"


def _slope(vwap_series: np.ndarray) -> str:
    """Whether the anchor itself is drifting, which decides how bands behave.

    This is the reading that separates the two opposite trades a band touch can
    justify. Against a flat VWAP the bands contain price and the touch is a
    fade; against a sloping one price rides the band and the same touch is a
    continuation. Getting this backwards is the most expensive mistake
    available to anyone trading VWAP bands.
    """
    if len(vwap_series) < SLOPE_BARS + 1:
        return "forming"
    now, before = vwap_series[-1], vwap_series[-1 - SLOPE_BARS]
    if before <= 0:
        return "flat"
    change = (now / before - 1) * 100
    if change >= SLOPE_SIGNIFICANT_PCT / 10:
        return "rising"
    if change <= -SLOPE_SIGNIFICANT_PCT / 10:
        return "falling"
    return "flat"


# --- anchors ----------------------------------------------------------------


def _day_start(now: pd.Timestamp) -> pd.Timestamp:
    return now.normalize()


def _week_start(now: pd.Timestamp) -> pd.Timestamp:
    return (now - pd.Timedelta(days=int(now.weekday()))).normalize()


def _session_start(now: pd.Timestamp) -> tuple[pd.Timestamp, str]:
    """The open of the session currently in progress.

    Sessions overlap, so "the current session" is the most recent one to have
    opened -- which is what a trader means by it. At 13:00 UTC London has been
    running for six hours and New York for one, and the reference that matters
    for a fresh entry is New York's.
    """
    hour = now.hour
    opened = [(name, h) for name, h in SESSION_OPENS.items() if h <= hour]
    if not opened:
        # Before Asia's open on this calendar day: the live session is the one
        # that opened yesterday and has not yet been replaced.
        yesterday = now.normalize() - pd.Timedelta(days=1)
        return yesterday + pd.Timedelta(hours=SESSION_OPENS["newyork"]), "newyork"
    name, open_hour = max(opened, key=lambda item: item[1])
    return now.normalize() + pd.Timedelta(hours=open_hour), name


def analyse(frames: dict[str, pd.DataFrame], now: pd.Timestamp | None = None) -> dict:
    """Every anchored VWAP an intraday Gold trader uses, from one frame set.

    Read on M5 by preference: fine enough that a session anchor holds a few
    hundred bars, coarse enough that a week of history is not a hundred
    thousand rows. Falls back through the ladder so a partial frame set still
    produces the references it can.
    """
    frame = None
    for timeframe in ("M5", "M15", "M1", "M30"):
        candidate = frames.get(timeframe)
        if candidate is not None and len(candidate) >= MIN_BARS:
            frame, source = candidate, timeframe
            break
    if frame is None:
        return {"available": False,
                "note": "No intraday frame long enough to anchor a VWAP."}

    now = pd.Timestamp(now) if now is not None else frame.index[-1]
    session_start, session_name = _session_start(now)

    anchors = {
        "day": anchored(frame, _day_start(now)),
        "session": anchored(frame, session_start),
        "week": anchored(frame, _week_start(now)),
    }
    available = {k: v for k, v in anchors.items() if v is not None}

    if not available:
        return {"available": False, "timeframe": source,
                "note": "Not enough bars since any anchor to compute a VWAP yet."}

    return {
        "available": True,
        "timeframe": source,
        "session": session_name,
        "anchors": available,
        "missing": [k for k, v in anchors.items() if v is None],
        "observations": _observations(available, session_name),
        "note": ("Volume-weighted on broker tick volume, which tracks activity "
                 "rather than contracts."
                 if available and next(iter(available.values()))["volume_weighted"]
                 else "The feed reported no volume, so these are unweighted "
                      "anchored means rather than true VWAPs."),
    }


def _observations(anchors: dict[str, dict], session: str) -> list[str]:
    """The readings worth putting in front of a trader, in plain words."""
    out: list[str] = []

    day = anchors.get("day")
    if day is not None:
        out.append(
            f"Price is {day['position']} on the day ({day['distance']:+.2f}, "
            f"{day['distance_sigma']:+.2f} sigma), with the day's VWAP at "
            f"{day['vwap']:.2f} and {day['slope']}.")
        if day["slope"] == "flat" and abs(day["distance_sigma"]) >= 1.0:
            out.append(
                "The day's VWAP is flat and price is at a band — in a rotating "
                "market that is where the fade back toward VWAP is offered.")
        elif day["slope"] in ("rising", "falling") and abs(day["distance_sigma"]) >= 1.0:
            direction = "up" if day["slope"] == "rising" else "down"
            out.append(
                f"The day's VWAP is {day['slope']} and price is at a band. On a "
                f"trend day price rides the band {direction} rather than "
                "reverting, so treat this as continuation until VWAP flattens.")

    current = anchors.get("session")
    if current is not None:
        out.append(
            f"Against the {session} session open, VWAP is {current['vwap']:.2f} "
            f"and price is {current['position']} "
            f"({current['distance_sigma']:+.2f} sigma over {current['bars']} bars, "
            f"{current['slope']}).")

    # The day and session anchors disagreeing is a reading in itself, and a
    # frequent one: it means this session has been travelling against the
    # balance of the day, which is where an intraday reversal comes from.
    if day is not None and current is not None:
        day_side = day["distance_sigma"] > 0
        session_side = current["distance_sigma"] > 0
        if day_side != session_side:
            out.append(
                f"Price is {'below' if not day_side else 'above'} the day's VWAP "
                f"but {'above' if session_side else 'below'} the "
                f"{session} session's. This session is trading against the "
                "balance of the day — either the start of a reversal, or a "
                "retracement that fails at the day anchor.")

    week = anchors.get("week")
    if week is not None and abs(week["distance_sigma"]) >= 2.0:
        out.append(
            f"Price is {week['position']} for the week — a {week['distance_sigma']:+.2f} "
            "sigma excursion from the weekly anchor is where positions get "
            "reduced rather than added to.")

    # Agreement between anchors is itself a reading, and a common intraday
    # setup: the day and session anchors converging on one price makes that
    # price a considerably stronger reference than either alone.
    if day is not None and current is not None:
        gap = abs(day["vwap"] - current["vwap"])
        if day["deviation"] > 0 and gap <= day["deviation"] * 0.25:
            out.append(
                f"The day and session anchors have converged around "
                f"{(day['vwap'] + current['vwap']) / 2:.2f}. Two references at "
                "one price is a stronger level than either on its own.")

    return out
