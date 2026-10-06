"""The fundamental picture, composed into one read a trader can act on.

:mod:`fred` says what rates are doing, :mod:`cot` says how the money is
positioned, :mod:`market` says what movement is being priced. Each is useful
alone and none of them is a decision. This module folds the three into a single
directional read with the arithmetic shown, and hands the rest of the
application one object rather than three.

How the drivers are weighted, and why
-------------------------------------

Not equally, because they do not deserve equal weight. Gold's response to real
yields is the most reliable relationship in the complex and it is given the
most weight; positioning is a slow, weekly, lagging measure and is given least.
The weights are judgment, they are stated in one place as constants, and they
are the first thing to revisit if the read ever disagrees with the market
persistently.

The composition follows the same rule ``fred._tally`` uses: a genuine
disagreement between drivers is reported as **conflicted**, not averaged into a
false consensus. Gold spends a great deal of its time with the dollar arguing
one way and rates the other, and a trader is far better served by "these
disagree, trade the chart today" than by a net score of +0.3.

The standing constraint
-----------------------

**Nothing here gates a setup.** This is the same rule that governs
``trader/expectancy`` and ``trader/desk``, and it applies with more force to a
daily macro series consulted by an intraday method: a real yield published
yesterday afternoon has no business suppressing a liquidity sweep that is
happening now. What the fundamental read produces is a conviction multiplier
and an explanation. The technical engine decides what is a setup; this decides
how much of the account's attention it deserves.

Why it runs on its own thread
-----------------------------

The analysis loop has a five-second budget and these are network calls. A cold
cache means eleven HTTP requests, each of which may block for the timeout, and
a loop that misses its cadence stops resolving stops on time -- which is a real
cost paid for context that was never urgent. So the refresh runs on a daemon
thread and :meth:`GoldDrivers.read` returns the most recent completed snapshot
immediately, always. Before the first one lands it returns an honest
"initialising" state rather than blocking.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from . import cot, fred, market

log = logging.getLogger(__name__)

# How often the background thread recomposes the picture. The underlying TTLs
# do the real caching; this only decides how promptly a newly published series
# is noticed.
REFRESH_SECONDS = 900

# Driver weights. Real yields lead by a wide margin, positioning trails.
WEIGHT_REAL_YIELD = 3.0
WEIGHT_REAL_YIELD_LEVEL = 1.5
WEIGHT_BREAKEVEN = 1.5
WEIGHT_DOLLAR = 1.5
WEIGHT_POSITIONING = 1.0
WEIGHT_RATIO = 1.0

# Share of total weight the minority side must reach for the picture to be
# called conflicted rather than directional.
CONFLICT_SHARE = 0.35

# The total weight available if every driver reported and every one of them had
# something to say. Strength is scaled against this, so a read assembled from
# one lagging driver cannot present itself as a unanimous macro picture.
#
# This is the same discipline ``trader/expectancy.shrink`` applies to a thin
# trade sample, for the same reason. Without it, a FRED outage leaves weekly
# positioning as the only voter and the layer reports maximum conviction on the
# weakest evidence it has -- confidence rising as information falls, which is
# precisely backwards.
MAX_DRIVER_WEIGHT = (WEIGHT_REAL_YIELD + WEIGHT_REAL_YIELD_LEVEL
                     + WEIGHT_BREAKEVEN + WEIGHT_DOLLAR
                     + WEIGHT_POSITIONING + WEIGHT_RATIO)

# Bounds on what the fundamental read may do to a setup's conviction. Kept
# deliberately tight: this is daily data informing an intraday decision, and
# the failure mode to avoid is a macro opinion overwhelming what the chart is
# actually doing. At the extremes a fully aligned macro picture is worth about
# a tenth of a position, which is roughly what it is worth.
ALIGNED_MULTIPLIER = 1.10
OPPOSED_MULTIPLIER = 0.90

# A dollar move over five sessions, in percent, that counts as a push.
DOLLAR_SIGNIFICANT_PCT = 0.75

# Five-session move in the gold/silver ratio that separates a metals bid from
# a haven bid, in percent. Mirrors ``trader/intermarket.RATIO_SIGNIFICANT``.
RATIO_SIGNIFICANT = 1.5


@dataclass
class FundamentalRead:
    """The whole fundamental picture as one answer."""

    available: bool = False
    initialising: bool = False
    bias: str = "neutral"              # bullish | bearish | neutral | conflicted
    strength: float = 0.0              # 0-1, how one-sided the drivers are
    headline: str = ""
    drivers: list[str] = field(default_factory=list)
    cautions: list[str] = field(default_factory=list)
    volatility_regime: str = "unknown"
    volatility_note: str = ""
    feed_healthy: bool = True
    generated_at: str = ""
    macro: dict = field(default_factory=dict)
    positioning: dict = field(default_factory=dict)
    reference: dict = field(default_factory=dict)

    def alignment(self, direction: str) -> tuple[float, str]:
        """Conviction multiplier for a setup in ``direction``, and why.

        ``direction`` is the signal layer's own vocabulary: "bullish" or
        "bearish". Returns 1.0 and an explanation whenever the fundamental
        picture has nothing to say, which is the common case and must never
        read as a penalty.
        """
        if not self.available or self.bias in ("neutral", "conflicted"):
            reason = {
                "conflicted": "the macro drivers disagree, so this is a chart-only trade",
                "neutral": "the macro picture is quiet and is not arguing either way",
            }.get(self.bias, "no fundamental read is available")
            return 1.0, reason

        wanted = "bullish" if direction == "bullish" else "bearish"
        # Scaled by how one-sided the drivers actually are, so a marginal
        # majority does not move a position as much as a unanimous one.
        span = self.strength
        if self.bias == wanted:
            multiplier = 1.0 + (ALIGNED_MULTIPLIER - 1.0) * span
            return multiplier, (f"the macro picture is {self.bias} and this setup "
                                f"agrees with it")
        multiplier = 1.0 - (1.0 - OPPOSED_MULTIPLIER) * span
        return multiplier, (f"the macro picture is {self.bias} and this setup "
                            f"trades against it")

    def to_dict(self) -> dict:
        return {
            "available": self.available,
            "initialising": self.initialising,
            "bias": self.bias,
            "strength": round(self.strength, 2),
            "headline": self.headline,
            "drivers": self.drivers,
            "cautions": self.cautions,
            "volatility_regime": self.volatility_regime,
            "volatility_note": self.volatility_note,
            "feed_healthy": self.feed_healthy,
            "generated_at": self.generated_at,
            "macro": self.macro,
            "positioning": self.positioning,
            "reference": self.reference,
        }


class GoldDrivers:
    """Composes and caches the fundamental picture on a background thread."""

    def __init__(self, refresh_seconds: float = REFRESH_SECONDS):
        self.refresh_seconds = refresh_seconds
        self._lock = threading.Lock()
        self._read: FundamentalRead | None = None
        self._read_at: datetime | None = None
        self._spot: float | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        """Begin refreshing in the background. Safe to call more than once."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop, name="fundamentals", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._refresh()
            except Exception:  # pragma: no cover - defensive
                # A composition failure must never kill the thread: the next
                # cycle may well succeed, and a dead refresher degrades
                # silently, which is the one outcome worth engineering against.
                log.warning("Fundamental refresh failed", exc_info=True)
            self._stop.wait(self.refresh_seconds)

    def _refresh(self) -> None:
        with self._lock:
            spot = self._spot
        composed = compose(spot)
        with self._lock:
            self._read = composed
            self._read_at = datetime.now(timezone.utc)

    # -- the read ------------------------------------------------------------

    def read(self, spot: float | None = None) -> FundamentalRead:
        """The latest completed picture. Never blocks, never raises.

        ``spot`` is remembered for the next background refresh, which is what
        enables the basis check without putting a network call on the caller's
        thread.
        """
        with self._lock:
            if spot is not None and spot > 0:
                self._spot = float(spot)
            if self._read is not None:
                return self._read
        self.start()
        return FundamentalRead(
            available=False,
            initialising=True,
            headline="Reading the macro picture — rates, positioning and reference "
                     "prices are loading in the background.",
        )

    @property
    def age(self) -> timedelta | None:
        with self._lock:
            if self._read_at is None:
                return None
            return datetime.now(timezone.utc) - self._read_at


def alignment_of(payload: dict | None, direction: str) -> tuple[float, str]:
    """:meth:`FundamentalRead.alignment`, from the serialised form.

    The analysis object is JSON before it reaches most consumers -- it is
    published over a WebSocket and handed to the AI layer -- so the desk reads
    a dict, not the dataclass. This keeps the arithmetic in one place rather
    than letting a second copy of it drift on the consuming side.
    """
    if not payload or not payload.get("available"):
        return 1.0, "no fundamental read is available"
    read = FundamentalRead(
        available=True,
        bias=str(payload.get("bias", "neutral")),
        strength=float(payload.get("strength") or 0.0),
    )
    return read.alignment(direction)


def compose(spot: float | None = None) -> FundamentalRead:
    """Read every source and fold them into one picture. Blocking."""
    macro = fred.read()
    positioning = cot.read()
    reference = market.read(spot)

    out = FundamentalRead(
        generated_at=datetime.now(timezone.utc).isoformat(),
        macro=macro.to_dict(),
        positioning=positioning.to_dict(),
        reference=reference.to_dict(),
        volatility_regime=reference.vol_regime,
        volatility_note=reference.vol_note,
        feed_healthy=reference.feed_healthy,
    )
    out.available = macro.available or positioning.available or reference.available

    if not out.available:
        out.headline = ("No fundamental source could be reached. Gold is being read "
                        "on its chart alone, which is how this application worked "
                        "before this layer existed — nothing is degraded, only "
                        "unaugmented.")
        return out

    votes: list[tuple[str, float]] = []
    drivers: list[str] = []
    cautions: list[str] = []

    _rates_votes(macro, votes, drivers)
    _dollar_vote(macro, reference, votes, drivers)
    _positioning_vote(positioning, votes, drivers, cautions)
    _ratio_vote(reference, votes, drivers)

    if reference.vol_note:
        drivers.append(reference.vol_note)
    if not reference.feed_healthy and reference.basis_note:
        cautions.append(reference.basis_note)

    out.bias, out.strength, out.headline = _tally(votes)
    out.drivers = drivers
    out.cautions = cautions
    return out


def _rates_votes(macro: fred.FredRead, votes: list, drivers: list) -> None:
    """Real yields and breakevens: the dominant pair."""
    real = macro.get("real_10y")
    if real is not None:
        bp = real.change_bp
        if bp is not None and abs(bp) >= fred.REAL_YIELD_SIGNIFICANT_BP:
            votes.append(("bearish" if bp > 0 else "bullish", WEIGHT_REAL_YIELD))
        if real.percentile is not None:
            if real.percentile <= fred.EXTREME_LOW:
                votes.append(("bullish", WEIGHT_REAL_YIELD_LEVEL))
            elif real.percentile >= fred.EXTREME_HIGH:
                votes.append(("bearish", WEIGHT_REAL_YIELD_LEVEL))

    breakeven = macro.get("breakeven_10y")
    if breakeven is not None and breakeven.change_bp is not None:
        if abs(breakeven.change_bp) >= fred.REAL_YIELD_SIGNIFICANT_BP:
            votes.append(("bullish" if breakeven.change_bp > 0 else "bearish",
                          WEIGHT_BREAKEVEN))

    # The narrative comes from the macro module itself, which already phrases
    # each driver. Duplicating that here would let the two drift apart.
    drivers.extend(macro.drivers)


def _dollar_vote(macro: fred.FredRead, reference: market.MarketRead,
                 votes: list, drivers: list) -> None:
    """The dollar, preferring ICE's index and falling back to the Fed's.

    Both are read. ICE DXY is the one the market quotes and reacts to
    intraday; the Fed's broad index is the better economic measure and updates
    daily. Where both are present the ICE index is used for the vote because
    this is an intraday application, and the broad index has already spoken
    through ``macro.drivers``.
    """
    dxy = reference.get("dollar_index")
    if dxy is not None and dxy.change_5d_pct is not None:
        if abs(dxy.change_5d_pct) >= DOLLAR_SIGNIFICANT_PCT:
            votes.append(("bearish" if dxy.change_5d_pct > 0 else "bullish",
                          WEIGHT_DOLLAR))
            drivers.append(
                f"The ICE dollar index is {dxy.value:.2f}, "
                f"{dxy.change_5d_pct:+.2f}% over five sessions — "
                f"{'a headwind' if dxy.change_5d_pct > 0 else 'a tailwind'} for Gold.")
        return
    # No ICE index: the Fed's broad measure carries the vote instead, so a
    # missing reference venue costs precision rather than the whole reading.
    broad = macro.get("dollar_broad")
    if broad is not None and broad.change_fast is not None and broad.value:
        pct = broad.change_fast / (broad.value - broad.change_fast) * 100
        if abs(pct) >= 0.5:
            votes.append(("bearish" if pct > 0 else "bullish", WEIGHT_DOLLAR))


def _positioning_vote(positioning: cot.CotRead, votes: list, drivers: list,
                      cautions: list) -> None:
    """Positioning votes only at an extreme, and votes against the crowd."""
    if not positioning.available:
        return
    drivers.append(positioning.headline)
    if positioning.crowding == "crowded long":
        votes.append(("bearish", WEIGHT_POSITIONING))
        cautions.append(
            "Speculative positioning is crowded long. This does not argue for "
            "shorting Gold — it argues for banking the first objective rather "
            "than holding a runner, because the marginal buyer is already in.")
    elif positioning.crowding == "crowded short":
        votes.append(("bullish", WEIGHT_POSITIONING))
        cautions.append(
            "Speculative positioning is unusually light. There is room for buyers "
            "to return, which favours holding a winner longer than usual.")


def _ratio_vote(reference: market.MarketRead, votes: list, drivers: list) -> None:
    """The metals ratio from COMEX: a metals bid trends, a haven bid does not."""
    ratio, note = market.gold_silver_ratio(reference)
    if ratio is None:
        return
    drivers.append(note)
    gold, silver = reference.get("gold_futures"), reference.get("silver_futures")
    if gold is None or silver is None:
        return
    if gold.change_5d_pct is None or silver.change_5d_pct is None:
        return
    # Silver outpacing Gold is a broad precious-metals bid, which trends.
    spread = silver.change_5d_pct - gold.change_5d_pct
    if spread >= RATIO_SIGNIFICANT and gold.change_5d_pct > 0:
        votes.append(("bullish", WEIGHT_RATIO))
    elif spread <= -RATIO_SIGNIFICANT and gold.change_5d_pct > 0:
        # Gold rising alone is a haven bid: real, but it retraces.
        drivers.append(
            "Gold is outpacing silver, which marks this as a haven bid rather "
            "than a broad metals bid. Those moves retrace harder when the scare "
            "passes, so upside targets deserve less patience.")


def _tally(votes: list[tuple[str, float]]) -> tuple[str, float, str]:
    """Weighted votes into a bias, a strength and a sentence.

    Strength is the product of two independent things, and conflating them was
    a bug worth naming: **how one-sided** the drivers are, and **how many of
    them actually spoke**. A single weekly positioning number pointing one way
    is unanimous among the drivers that reported and is still thin evidence,
    so it must not carry the conviction of a picture where rates, the dollar
    and positioning all agree.
    """
    if not votes:
        return ("neutral", 0.0,
                "The fundamental picture is quiet: rates, the dollar and "
                "positioning are all sitting still, so today is a chart-only "
                "session.")

    bullish = sum(w for d, w in votes if d == "bullish")
    bearish = sum(w for d, w in votes if d == "bearish")
    total = bullish + bearish
    if not total:
        return ("neutral", 0.0, "The fundamental drivers cancel out exactly.")

    minority = min(bullish, bearish)
    if minority / total >= CONFLICT_SHARE:
        return ("conflicted", 0.0,
                f"The fundamental drivers disagree — {bullish:.1f} of weight "
                f"bullish against {bearish:.1f} bearish. Gold has no macro tide "
                "to lean on, so trade the chart and size normally.")

    # How lopsided, mapped from "bare majority" to "unanimous" onto 0..1 so a
    # 65/35 split does not read like a 100/0 one.
    lopsided = max(0.0, min(1.0, (abs(bullish - bearish) / total - CONFLICT_SHARE)
                            / (1.0 - CONFLICT_SHARE)))
    # How much of the available evidence is behind that verdict.
    coverage = min(1.0, total / MAX_DRIVER_WEIGHT)
    strength = lopsided * coverage

    direction = "bullish" if bullish > bearish else "bearish"
    winner, loser = max(bullish, bearish), min(bullish, bearish)
    stance = ("Longs are trading with the macro tide, shorts against it."
              if direction == "bullish" else
              "Shorts are trading with the macro tide, longs against it.")
    sentence = (f"The fundamental picture "
                f"{'supports' if direction == 'bullish' else 'is against'} Gold "
                f"({winner:.1f} of weight against {loser:.1f}). {stance}")

    # Thin coverage is stated rather than buried in a number nobody reads.
    if coverage < 0.4:
        sentence += (f" Only {total:.1f} of a possible {MAX_DRIVER_WEIGHT:.1f} "
                     "of driver weight reported, so this is a weak read — treat "
                     "it as a lean, not a thesis.")

    return (direction, strength, sentence)
