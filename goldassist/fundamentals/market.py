"""Reference prices from the venues where Gold is actually priced.

Everything else in this application reads one broker's synthesised XAUUSD
quote. That quote is what the trader can deal on, so it is the right thing to
analyse -- but it is not where Gold's price is discovered. Discovery happens in
COMEX futures and the London OTC market, and a CFD is a derivative of that with
a spread and a funding cost wrapped around it. Reading the underlying venue
gives three things the CFD cannot:

**The basis.** The broker's quote against the COMEX front month. Normally a
small, stable spread reflecting carry. When it moves, something is happening --
either in the market or in the broker's pricing -- and a quote drifting from
the underlying is the single most important data-integrity check available. The
whole application rests on that one feed being honest; until now nothing
checked it.

**Implied volatility.** The CBOE publishes GVZ, the Gold VIX: the market's
priced expectation of how far Gold will move over the next thirty days. Set
against volatility actually realised, it answers the question every intraday
trader is implicitly asking at the open -- is today likely to trend or to chop?
Implied running well above realised is the market paying up for movement it has
not yet seen, which historically precedes range expansion. Implied below
realised is the opposite. No indicator computed from price alone can say this,
because it is not in the price.

**A real dollar index.** ``trader/macro`` falls back to inverting EURUSD when
the broker carries no index, which is a single-currency proxy for a
six-currency basket. ICE's DXY is available directly.

Silver, oil and VIX are carried for the intermarket regime read, from the same
source and for the same reason: the broker may not carry them, and the regime
question does not stop mattering when it does not.

Everything here is a *reference*. No signal is generated from it, no entry is
timed on it, and a source that cannot be reached costs a reading and nothing
else. The delayed quote these endpoints serve is entirely adequate for a basis
check and a volatility percentile, and it is not adequate for anything faster --
which is why nothing faster is attempted.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import http

log = logging.getLogger(__name__)

CHART_URL = ("https://query1.finance.yahoo.com/v8/finance/chart/"
             "{symbol}?range={range}&interval=1d")

# Fifteen minutes. These are reference prices consulted for context, and the
# endpoint serves a delayed quote in any case, so a faster refresh buys nothing
# but load on somebody else's server.
TTL = 900

# Trading days pulled. Enough for a 60-day volatility percentile and a
# 20-day realised-volatility window with room to spare.
RANGE = "6mo"

INSTRUMENTS = {
    "gold_futures": ("GC%3DF", "COMEX Gold front month", "$/oz"),
    "gold_iv": ("%5EGVZ", "Gold implied volatility (GVZ)", "%"),
    "dollar_index": ("DX-Y.NYB", "ICE Dollar Index", "index"),
    "silver_futures": ("SI%3DF", "COMEX Silver", "$/oz"),
    "vix": ("%5EVIX", "VIX", "%"),
    "oil": ("CL%3DF", "WTI Crude", "$/bbl"),
    "treasury_10y": ("%5ETNX", "10-year Treasury yield", "%"),
}

# Trading days in the realised-volatility window. Twenty is one month and is
# the conventional comparison for a thirty-calendar-day implied measure.
REALISED_WINDOW = 20

# Annualisation factor for daily returns.
TRADING_DAYS = 252

# How far the broker quote may sit from the COMEX front month before it is
# worth remarking on, in dollars. Gold's futures basis is normally a few
# dollars of carry; beyond this something has changed.
BASIS_NOTABLE = 25.0

# Implied/realised ratio bounds. Above the first, the market is paying up for
# movement; below the second, it is discounting it.
IV_RICH = 1.25
IV_CHEAP = 0.85


@dataclass
class Instrument:
    key: str
    symbol: str
    label: str
    units: str
    available: bool = False
    value: float | None = None
    change_1d_pct: float | None = None
    change_5d_pct: float | None = None
    percentile: float | None = None
    closes: list[float] = field(default_factory=list, repr=False)
    age_note: str = ""
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "symbol": self.symbol.replace("%3D", "=").replace("%5E", "^"),
            "label": self.label,
            "units": self.units,
            "available": self.available,
            "value": round(self.value, 4) if self.value is not None else None,
            "change_1d_pct": (round(self.change_1d_pct, 2)
                              if self.change_1d_pct is not None else None),
            "change_5d_pct": (round(self.change_5d_pct, 2)
                              if self.change_5d_pct is not None else None),
            "percentile": round(self.percentile, 0) if self.percentile is not None else None,
            "age_note": self.age_note,
            "note": self.note,
        }


@dataclass
class MarketRead:
    available: bool = False
    instruments: dict[str, Instrument] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)
    basis: float | None = None
    basis_note: str = ""
    feed_healthy: bool = True
    implied_vol: float | None = None
    realised_vol: float | None = None
    vol_ratio: float | None = None
    vol_regime: str = "unknown"      # expansion likely | compression likely | normal
    vol_note: str = ""
    notes: list[str] = field(default_factory=list)

    def get(self, key: str) -> Instrument | None:
        found = self.instruments.get(key)
        return found if found is not None and found.available else None

    def to_dict(self) -> dict:
        return {
            "available": self.available,
            "instruments": {k: v.to_dict() for k, v in self.instruments.items()},
            "missing": self.missing,
            "basis": round(self.basis, 2) if self.basis is not None else None,
            "basis_note": self.basis_note,
            "feed_healthy": self.feed_healthy,
            "implied_vol": round(self.implied_vol, 1) if self.implied_vol is not None else None,
            "realised_vol": round(self.realised_vol, 1) if self.realised_vol is not None else None,
            "vol_ratio": round(self.vol_ratio, 2) if self.vol_ratio is not None else None,
            "vol_regime": self.vol_regime,
            "vol_note": self.vol_note,
            "notes": self.notes,
        }


def _read_instrument(key: str, symbol: str, label: str, units: str) -> Instrument:
    out = Instrument(key=key, symbol=symbol, label=label, units=units)
    payload, fetched = http.get_json(
        CHART_URL.format(symbol=symbol, range=RANGE), TTL)
    out.age_note = fetched.age_note()

    if payload is None:
        out.note = f"unavailable — {fetched.error}"
        return out

    try:
        result = payload["chart"]["result"][0]
        raw = result["indicators"]["quote"][0]["close"]
    except (KeyError, IndexError, TypeError):
        out.note = "the source returned no usable series"
        return out

    closes = [float(c) for c in raw if c is not None]
    if len(closes) < 5:
        out.note = "not enough history in the response"
        return out

    out.available = True
    out.closes = closes
    out.value = closes[-1]
    out.change_1d_pct = (closes[-1] / closes[-2] - 1) * 100 if closes[-2] else None
    if len(closes) > 5 and closes[-6]:
        out.change_5d_pct = (closes[-1] / closes[-6] - 1) * 100
    if len(closes) >= 30:
        below = sum(1 for c in closes if c < closes[-1])
        out.percentile = below / len(closes) * 100.0

    out.note = f"{out.value:,.2f} ({out.change_1d_pct:+.2f}% on the day)" \
        if out.change_1d_pct is not None else f"{out.value:,.2f}"
    if fetched.stale:
        out.note += f", cached {out.age_note}"
    return out


def _realised_volatility(closes: list[float]) -> float | None:
    """Annualised close-to-close volatility, in percent.

    Close-to-close rather than a range estimator because the comparison is
    against an implied volatility that is itself quoted on a close-to-close
    convention. Using a Parkinson or Garman-Klass estimator here would be more
    efficient and would answer a different question than GVZ is asking.
    """
    if len(closes) < REALISED_WINDOW + 1:
        return None
    window = closes[-(REALISED_WINDOW + 1):]
    returns = []
    for previous, current in zip(window, window[1:]):
        if previous > 0 and current > 0:
            returns.append((current / previous) - 1.0)
    if len(returns) < 5:
        return None
    mean = sum(returns) / len(returns)
    variance = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    return (variance ** 0.5) * (TRADING_DAYS ** 0.5) * 100.0


def read(spot: float | None = None) -> MarketRead:
    """Reference prices, the basis against ``spot``, and the volatility read.

    ``spot`` is the broker's current Gold quote. Passing it enables the basis
    check; omitting it simply leaves that reading out.
    """
    out = MarketRead()
    for key, (symbol, label, units) in INSTRUMENTS.items():
        instrument = _read_instrument(key, symbol, label, units)
        out.instruments[key] = instrument
        if not instrument.available:
            out.missing.append(label)

    out.available = any(i.available for i in out.instruments.values())
    if not out.available:
        out.notes.append("No reference venue could be reached; Gold is being read "
                         "on the broker quote alone.")
        return out

    _basis(out, spot)
    _volatility(out)
    return out


def _basis(out: MarketRead, spot: float | None) -> None:
    """The broker quote against COMEX, as a data-integrity check."""
    futures = out.get("gold_futures")
    if futures is None or futures.value is None:
        out.basis_note = ("No COMEX reference available, so the broker quote "
                          "cannot be checked against the underlying market.")
        return
    if spot is None or spot <= 0:
        out.basis_note = (f"COMEX front month is {futures.value:,.2f}. "
                          "No broker quote supplied to compare against.")
        return

    out.basis = futures.value - spot
    if abs(out.basis) <= BASIS_NOTABLE:
        out.basis_note = (
            f"The broker quote ({spot:,.2f}) sits {out.basis:+.2f} from COMEX "
            f"({futures.value:,.2f}) — normal carry, and the feed is tracking "
            "the underlying market.")
        return

    out.feed_healthy = False
    out.basis_note = (
        f"The broker quote ({spot:,.2f}) is {out.basis:+.2f} from the COMEX "
        f"front month ({futures.value:,.2f}), well beyond the ${BASIS_NOTABLE:.0f} "
        "normal carry. Either the reference is stale — it is a delayed quote and "
        "is not updated at the weekend — or the feed has drifted. Worth checking "
        "before trusting a level to the cent.")
    out.notes.append(out.basis_note)


def _volatility(out: MarketRead) -> None:
    """Implied against realised: is movement being priced richly or cheaply?"""
    implied = out.get("gold_iv")
    futures = out.get("gold_futures")
    if implied is None or implied.value is None:
        out.vol_note = ("Gold implied volatility (GVZ) is unavailable, so there is "
                        "no read on what the options market expects.")
        return

    out.implied_vol = implied.value
    if futures is not None:
        out.realised_vol = _realised_volatility(futures.closes)

    if not out.realised_vol:
        out.vol_note = (f"Implied volatility is {out.implied_vol:.1f}%"
                        + (f", the {implied.percentile:.0f}th percentile of six months."
                           if implied.percentile is not None else "."))
        return

    out.vol_ratio = out.implied_vol / out.realised_vol
    percentile = (f" (the {implied.percentile:.0f}th percentile of six months)"
                  if implied.percentile is not None else "")

    if out.vol_ratio >= IV_RICH:
        out.vol_regime = "expansion likely"
        out.vol_note = (
            f"Implied volatility is {out.implied_vol:.1f}%{percentile} against "
            f"{out.realised_vol:.1f}% realised — a ratio of {out.vol_ratio:.2f}. "
            "The options market is paying up for movement Gold has not yet made, "
            "which favours breakout and continuation setups over fading a range.")
    elif out.vol_ratio <= IV_CHEAP:
        out.vol_regime = "compression likely"
        out.vol_note = (
            f"Implied volatility is {out.implied_vol:.1f}%{percentile} against "
            f"{out.realised_vol:.1f}% realised — a ratio of {out.vol_ratio:.2f}. "
            "Gold has been moving more than the options market expects it to keep "
            "moving, which favours mean reversion and range rotation over chasing "
            "a breakout.")
    else:
        out.vol_regime = "normal"
        out.vol_note = (
            f"Implied volatility is {out.implied_vol:.1f}%{percentile} against "
            f"{out.realised_vol:.1f}% realised — in line, so volatility is not "
            "arguing for or against any particular setup type today.")
    out.notes.append(out.vol_note)


def gold_silver_ratio(out: MarketRead) -> tuple[float | None, str]:
    """The metals ratio from the futures venue, when both legs are present."""
    gold, silver = out.get("gold_futures"), out.get("silver_futures")
    if gold is None or silver is None or not silver.value:
        return None, ""
    ratio = gold.value / silver.value
    if len(gold.closes) > 5 and len(silver.closes) > 5 and silver.closes[-6]:
        previous = gold.closes[-6] / silver.closes[-6]
        change = (ratio / previous - 1) * 100 if previous else 0.0
        return ratio, (f"The gold/silver ratio is {ratio:.1f}, {change:+.1f}% over "
                       "five sessions, measured on COMEX rather than on CFDs.")
    return ratio, f"The gold/silver ratio is {ratio:.1f}, measured on COMEX."
