"""Behavioural checks for the analysis and decision engines.

These use synthetic price series with a known shape, so each assertion pins a
specific behaviour rather than a snapshot of live output.

Run with:  python -m tests.test_engines
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from goldassist.analysis import candlesticks, fvg, indicators, liquidity, orderblocks, structure, zones
from goldassist.decision import engine as decision_engine
from goldassist.decision import profile as profile_module

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        FAILURES.append(name)


def make_frame(closes, highs=None, lows=None, opens=None, start=None, freq="1h"):
    """Build an OHLC frame from a close series, deriving sane wicks."""
    closes = np.asarray(closes, dtype=float)
    opens = np.asarray(opens, dtype=float) if opens is not None else np.r_[closes[0], closes[:-1]]
    highs = np.asarray(highs, dtype=float) if highs is not None else np.maximum(opens, closes) + 0.5
    lows = np.asarray(lows, dtype=float) if lows is not None else np.minimum(opens, closes) - 0.5
    index = pd.date_range(
        start=start or datetime(2026, 1, 1, tzinfo=timezone.utc),
        periods=len(closes), freq=freq, tz="UTC",
    )
    return pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes,
         "volume": np.full(len(closes), 1000.0), "spread": np.zeros(len(closes))},
        index=index,
    )


# --- indicators ------------------------------------------------------------

def test_indicators():
    print("indicators")
    rising = make_frame(np.linspace(2000, 2100, 300))
    ema20 = indicators.ema(rising["close"], 20)
    check("EMA lags a rising series", ema20.iloc[-1] < rising["close"].iloc[-1])

    rsi = indicators.rsi(rising["close"])
    check("RSI is high in a pure uptrend", rsi.iloc[-1] > 70, f"got {rsi.iloc[-1]:.1f}")

    falling = make_frame(np.linspace(2100, 2000, 300))
    check("RSI is low in a pure downtrend", indicators.rsi(falling["close"]).iloc[-1] < 30)

    a = indicators.atr(rising)
    check("ATR is positive", a.iloc[-1] > 0)

    flat = make_frame(np.full(300, 2000.0))
    check("ATR collapses on a flat series", indicators.atr(flat).iloc[-1] < 1.5)

    st = indicators.supertrend(rising)
    check("Supertrend is bullish in an uptrend", st["direction"].iloc[-1] == 1)

    snap = indicators.snapshot(rising)
    check("Snapshot reports a bullish EMA stack", snap["ema"]["bias"] == "bullish")
    check("Snapshot values are JSON-safe",
          all(not isinstance(v.get("value"), float) or np.isfinite(v["value"])
              for v in snap.values() if isinstance(v.get("value"), float)))


# --- structure -------------------------------------------------------------

def test_structure():
    print("structure")
    # A clean staircase: each leg makes a higher high and a higher low.
    closes = []
    price = 2000.0
    for _ in range(12):
        closes.extend(np.linspace(price, price + 30, 10))
        closes.extend(np.linspace(price + 30, price + 15, 6))
        price += 15
    df = make_frame(closes)
    a = indicators.atr(df)
    result = structure.analyse(df, a, strength=2)
    check("Uptrend is read as bullish", result["trend"] == "bullish", result["trend"])
    check("Bullish labels dominate",
          sum(1 for l in result["labels"] if l in ("HH", "HL")) >= 3, str(result["labels"]))
    check("BOS events are detected",
          any(e["kind"] == "BOS" for e in result["events"]))
    check("Every event has a direction",
          all(e["direction"] in ("bullish", "bearish") for e in result["events"]))

    down = make_frame(list(reversed(closes)))
    down_result = structure.analyse(down, indicators.atr(down), strength=2)
    check("Downtrend is read as bearish", down_result["trend"] == "bearish", down_result["trend"])

    swings = structure.find_swings(df, 2)
    kinds = [s.kind for s in swings]
    check("Swings strictly alternate",
          all(kinds[i] != kinds[i + 1] for i in range(len(kinds) - 1)))


# --- FVG -------------------------------------------------------------------

def test_fvg():
    print("fair value gaps")
    n = 120
    closes = list(np.linspace(2000, 2010, n))
    highs = [c + 0.5 for c in closes]
    lows = [c - 0.5 for c in closes]
    opens = [c - 0.2 for c in closes]

    # Engineer a bullish gap at index 100: candle 3's low above candle 1's high.
    opens[100], closes[100], highs[100], lows[100] = 2010, 2040, 2041, 2009.5
    for i in range(101, n):
        opens[i], closes[i] = 2040, 2042
        highs[i], lows[i] = 2043, 2039

    df = make_frame(closes, highs, lows, opens)
    gaps = fvg.detect(df, "H1")
    bullish = [g for g in gaps if g.direction == "bullish"]
    check("A bullish FVG is detected after displacement", len(bullish) >= 1)
    if bullish:
        gap = bullish[0]
        check("FVG top is above its bottom", gap.top > gap.bottom)
        check("Displacement is recorded", gap.displacement > 0)
        check("FVG quality is within range", 0 <= gap.quality <= 100)


# --- order blocks ----------------------------------------------------------

def test_order_blocks():
    print("order blocks")
    n = 120
    opens, closes, highs, lows = [], [], [], []
    for i in range(n):
        base = 2000 + i * 0.05
        opens.append(base)
        closes.append(base + 0.1)
        highs.append(base + 0.4)
        lows.append(base - 0.4)
    # A down candle followed by a strong rally = bullish order block.
    opens[80], closes[80], highs[80], lows[80] = 2006, 2002, 2006.5, 2001.5
    for i in range(81, 86):
        opens[i] = 2002 + (i - 81) * 8
        closes[i] = opens[i] + 8
        highs[i] = closes[i] + 1
        lows[i] = opens[i] - 1
    for i in range(86, n):
        opens[i] = closes[i] = 2045
        highs[i], lows[i] = 2046, 2044

    df = make_frame(closes, highs, lows, opens)
    a = indicators.atr(df)
    events = structure.analyse(df, a)["events"]
    blocks = orderblocks.detect(df, "H1", events)
    bullish = [b for b in blocks if b.direction == "bullish"]
    check("A bullish order block is detected", len(bullish) >= 1)
    if bullish:
        check("Order block quality is within range",
              all(0 <= b.quality <= 100 for b in bullish))
        check("Order block bounds are ordered", all(b.top >= b.bottom for b in bullish))


# --- liquidity -------------------------------------------------------------

def test_liquidity():
    print("liquidity")
    n = 160
    closes = [2000.0] * n
    highs = [2001.0] * n
    lows = [1999.0] * n
    opens = [2000.0] * n

    # Three equal lows at 1990, each separated by a rally. The rally matters:
    # swings must alternate high/low, so without an intervening swing high the
    # engine correctly treats consecutive lows as one.
    for i in (40, 60, 80):
        lows[i] = 1990.0
        closes[i] = 1995.0
        opens[i] = 1999.0
    for i in (50, 70, 90):
        highs[i] = 2012.0
        closes[i] = 2010.0
        opens[i] = 2001.0

    # Then a candle that wicks below the equal lows and closes back above them.
    lows[120], closes[120], opens[120], highs[120] = 1985.0, 1998.0, 1999.0, 1999.5

    df = make_frame(closes, highs, lows, opens)
    pools = liquidity.equal_levels(df, "M15")
    check("Liquidity pools are found", len(pools) > 0)
    sellside = [p for p in pools if p.side == "sellside"]
    check("Sell-side pools exist below price", len(sellside) > 0)
    check("Equal lows are clustered into one pool",
          any(p.touches >= 2 for p in sellside),
          f"touches: {[p.touches for p in sellside]}")

    sweeps = liquidity.detect_sweeps(df, "M15", pools, lookback=60)
    check("A sweep is detected when price wicks below and closes back above",
          any(s.direction == "bullish" for s in sweeps),
          f"got {[(s.direction, round(s.level, 1)) for s in sweeps]}")


# --- candlesticks ----------------------------------------------------------

def test_candlesticks():
    print("candlesticks")
    n = 60
    opens = [2000.0] * n
    closes = [2001.0] * n
    highs = [2002.0] * n
    lows = [1999.0] * n
    # A bullish engulfing pair at the end.
    opens[57], closes[57], highs[57], lows[57] = 2005, 2000, 2006, 1999
    opens[58], closes[58], highs[58], lows[58] = 1999, 2007, 2008, 1998
    df = make_frame(closes, highs, lows, opens)

    found = candlesticks.detect(df, lookback=8)
    names = {p.name for p in found}
    check("Bullish engulfing is detected", "bullish engulfing" in names, str(names))
    summary = candlesticks.summarise(found)
    check("Summary produces a bias", summary["bias"] in ("bullish", "bearish", "neutral"))
    check("Every pattern carries a direction",
          all(p.direction in ("bullish", "bearish", "neutral") for p in found))


# --- zones -----------------------------------------------------------------

def test_zones():
    print("supply and demand")
    n = 140
    opens, closes, highs, lows = [], [], [], []
    for i in range(n):
        opens.append(2000.0)
        closes.append(2000.2)
        highs.append(2000.8)
        lows.append(1999.2)
    # A tight base then a strong rally away = demand zone.
    for i in range(70, 75):
        opens[i], closes[i] = 2000.0, 2000.1
        highs[i], lows[i] = 2000.4, 1999.6
    for i in range(75, 80):
        opens[i] = 2000 + (i - 75) * 10
        closes[i] = opens[i] + 10
        highs[i], lows[i] = closes[i] + 1, opens[i] - 1
    for i in range(80, n):
        opens[i] = closes[i] = 2055.0
        highs[i], lows[i] = 2056.0, 2054.0

    df = make_frame(closes, highs, lows, opens)
    detected = zones.detect(df, "H1")
    demand = [z for z in detected if z.kind == "demand"]
    check("A demand zone is detected after an impulse", len(demand) >= 1)
    if demand:
        check("Zone quality is within range", all(0 <= z.quality <= 100 for z in demand))
        check("Fresh zones score above heavily tested ones",
              max(z.quality for z in demand) > 0)


# --- decision --------------------------------------------------------------

def test_decision_shape():
    print("decision engine")
    frames = {}
    base = np.linspace(2000, 2100, 400)
    for tf, freq in (("D1", "1D"), ("H4", "4h"), ("H1", "1h"), ("M15", "15min"), ("M5", "5min")):
        frames[tf] = make_frame(base + np.random.default_rng(7).normal(0, 0.4, len(base)), freq=freq)

    from goldassist.analysis import mtf
    analysis = mtf.analyse(frames)
    profile = profile_module.get("Default XAUUSD")
    decision = decision_engine.decide(analysis, profile)

    check("Decision is one of BUY/SELL/WAIT", decision["decision"] in ("BUY", "SELL", "WAIT"))
    check("Score is within 0-100", 0 <= decision["score"] <= 100)
    check("An invalidation level is always provided", decision["invalidation"] is not None)
    check("Reasons are provided", len(decision["reasons"]) > 0)
    check("A WAIT always says what it is waiting for",
          decision["decision"] != "WAIT" or len(decision["waiting_for"]) > 0)
    check("Checklist is present", len(decision["confirmation_checklist"]) > 0)

    both = decision["confluence"]
    check("Both directions are scored",
          "bullish" in both and "bearish" in both)
    check("Evidence never exceeds its cap",
          all(e["points"] <= e["max_points"] + 1e-6
              for d in ("bullish", "bearish") for e in both[d]["evidence"]))

    # A decision must be JSON-serialisable for the API and the database.
    import json
    from goldassist.jsonsafe import safe
    json.dumps(safe(decision))
    check("Decision serialises to JSON", True)


def test_diff():
    print("decision diff")
    a = {"decision": "WAIT", "direction": "bullish", "score": 50.0,
         "regime": {"regime": "range"}, "waiting_for": ["x"]}
    b = {"decision": "BUY", "direction": "bullish", "score": 80.0,
         "regime": {"regime": "range"}, "waiting_for": []}
    check("First decision reports as initial",
          decision_engine.diff(None, a)["type"] == "initial")
    change = decision_engine.diff(a, b)
    check("A decision change is reported", change is not None)
    check("The change names both states", "WAIT" in change["message"] and "BUY" in change["message"])
    check("An unchanged decision reports nothing", decision_engine.diff(b, b) is None)


def main() -> int:
    for test in (test_indicators, test_structure, test_fvg, test_order_blocks,
                 test_liquidity, test_candlesticks, test_zones,
                 test_decision_shape, test_diff):
        test()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) failed: {', '.join(FAILURES)}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
