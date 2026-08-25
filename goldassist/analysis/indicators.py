"""Technical indicator library.

Everything is computed with pandas/numpy so the app has no native TA
dependency. Each function takes the canonical OHLC frame and returns either a
Series or a frame aligned to the same index.

Indicators are treated as *supporting evidence* elsewhere in the app, never as
standalone signals -- see :mod:`goldassist.decision.confluence`.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

# --- trend -----------------------------------------------------------------


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False, min_periods=period).mean()


def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(period).mean()


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    line = ema(close, fast) - ema(close, slow)
    sig = line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    return pd.DataFrame({"macd": line, "signal": sig, "hist": line - sig})


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift()
    return pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    # Wilder smoothing, the definition used by MT5 and most charting platforms.
    return true_range(df).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def adx(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    up = df["high"].diff()
    down = -df["low"].diff()
    plus_dm = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)
    atr_ = atr(df, period)
    alpha = 1 / period
    plus_di = 100 * pd.Series(plus_dm, index=df.index).ewm(alpha=alpha, adjust=False).mean() / atr_
    minus_di = 100 * pd.Series(minus_dm, index=df.index).ewm(alpha=alpha, adjust=False).mean() / atr_
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return pd.DataFrame(
        {"adx": dx.ewm(alpha=alpha, adjust=False).mean(), "plus_di": plus_di, "minus_di": minus_di}
    )


def supertrend(df: pd.DataFrame, period: int = 10, multiplier: float = 3.0) -> pd.DataFrame:
    hl2 = (df["high"] + df["low"]) / 2
    band = multiplier * atr(df, period)
    upper, lower = hl2 + band, hl2 - band
    close = df["close"].to_numpy()
    up_arr, lo_arr = upper.to_numpy(), lower.to_numpy()
    final_up = np.full(len(df), np.nan)
    final_lo = np.full(len(df), np.nan)
    direction = np.ones(len(df))  # 1 = bullish, -1 = bearish

    for i in range(len(df)):
        if i == 0 or np.isnan(up_arr[i]) or np.isnan(final_up[i - 1]):
            final_up[i], final_lo[i] = up_arr[i], lo_arr[i]
            continue
        # Bands only ratchet in the direction that protects the trend.
        final_up[i] = (
            up_arr[i]
            if (up_arr[i] < final_up[i - 1] or close[i - 1] > final_up[i - 1])
            else final_up[i - 1]
        )
        final_lo[i] = (
            lo_arr[i]
            if (lo_arr[i] > final_lo[i - 1] or close[i - 1] < final_lo[i - 1])
            else final_lo[i - 1]
        )
        if close[i] > final_up[i - 1]:
            direction[i] = 1
        elif close[i] < final_lo[i - 1]:
            direction[i] = -1
        else:
            direction[i] = direction[i - 1]

    line = np.where(direction == 1, final_lo, final_up)
    return pd.DataFrame({"supertrend": line, "direction": direction}, index=df.index)


# --- momentum --------------------------------------------------------------


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()

    rs = gain / loss.replace(0, np.nan)
    out = 100 - 100 / (1 + rs)
    # Zero average loss makes rs undefined. That is not a neutral reading --
    # it is the maximum: an unbroken run of up-candles is RSI 100. Filling it
    # with 50 (the previous behaviour) silently hid the strongest momentum.
    out = out.mask((loss == 0) & (gain > 0), 100.0)
    out = out.mask((loss == 0) & (gain == 0), 50.0)
    return out.fillna(50)


def stochastic(df: pd.DataFrame, k: int = 14, d: int = 3, smooth: int = 3) -> pd.DataFrame:
    low = df["low"].rolling(k).min()
    high = df["high"].rolling(k).max()
    raw = 100 * (df["close"] - low) / (high - low).replace(0, np.nan)
    k_line = raw.rolling(smooth).mean()
    return pd.DataFrame({"k": k_line, "d": k_line.rolling(d).mean()})


def cci(df: pd.DataFrame, period: int = 20) -> pd.Series:
    tp = (df["high"] + df["low"] + df["close"]) / 3
    ma = tp.rolling(period).mean()
    mad = tp.rolling(period).apply(lambda x: np.abs(x - x.mean()).mean(), raw=True)
    return (tp - ma) / (0.015 * mad.replace(0, np.nan))


def williams_r(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high = df["high"].rolling(period).max()
    low = df["low"].rolling(period).min()
    return -100 * (high - df["close"]) / (high - low).replace(0, np.nan)


def roc(close: pd.Series, period: int = 12) -> pd.Series:
    return 100 * (close / close.shift(period) - 1)


# --- volatility ------------------------------------------------------------


def bollinger(close: pd.Series, period: int = 20, std: float = 2.0) -> pd.DataFrame:
    mid = close.rolling(period).mean()
    dev = close.rolling(period).std(ddof=0)
    upper, lower = mid + std * dev, mid - std * dev
    return pd.DataFrame(
        {"upper": upper, "middle": mid, "lower": lower, "width": (upper - lower) / mid * 100}
    )


def keltner(df: pd.DataFrame, period: int = 20, multiplier: float = 2.0) -> pd.DataFrame:
    mid = ema(df["close"], period)
    band = multiplier * atr(df, period)
    return pd.DataFrame({"upper": mid + band, "middle": mid, "lower": mid - band})


# --- volume ----------------------------------------------------------------


def obv(df: pd.DataFrame) -> pd.Series:
    direction = np.sign(df["close"].diff()).fillna(0)
    return (direction * df["volume"]).cumsum()


def vwap(df: pd.DataFrame, anchor: str = "D") -> pd.Series:
    """Volume-weighted average price, re-anchored each session (UTC day)."""
    tp = (df["high"] + df["low"] + df["close"]) / 3
    # floor() keeps the index tz-aware; to_period() would drop the timezone.
    groups = df.index.floor("D") if anchor == "D" else df.index.floor(anchor)
    pv = (tp * df["volume"]).groupby(groups).cumsum()
    vol = df["volume"].groupby(groups).cumsum().replace(0, np.nan)
    return pv / vol


def volume_ma(df: pd.DataFrame, period: int = 20) -> pd.Series:
    return df["volume"].rolling(period).mean()


# --- aggregate snapshot ----------------------------------------------------


def snapshot(df: pd.DataFrame) -> dict:
    """Compute every indicator and reduce each to a bias reading.

    Returns ``{name: {"value": ..., "bias": "bullish"|"bearish"|"neutral",
    "note": str}}``. The confluence engine consumes the biases; the dashboard
    shows the notes.
    """
    close = df["close"]
    last = float(close.iloc[-1])
    out: dict[str, dict] = {}

    emas = {p: ema(close, p) for p in (20, 50, 100, 200)}
    above = [p for p, s in emas.items() if _num(s.iloc[-1]) is not None and last > s.iloc[-1]]
    stacked_bull = _stacked(emas, ascending=True)
    stacked_bear = _stacked(emas, ascending=False)
    out["ema"] = {
        "value": {f"ema{p}": _num(s.iloc[-1]) for p, s in emas.items()},
        "bias": "bullish" if len(above) >= 3 and stacked_bull
        else "bearish" if len(above) <= 1 and stacked_bear
        else "neutral",
        "note": f"price above {len(above)}/4 EMAs"
        + (" (bullish stack)" if stacked_bull else " (bearish stack)" if stacked_bear else ""),
    }

    m = macd(close)
    hist = _num(m["hist"].iloc[-1])
    out["macd"] = {
        "value": hist,
        "bias": "bullish" if hist and hist > 0 and m["macd"].iloc[-1] > m["signal"].iloc[-1]
        else "bearish" if hist and hist < 0 and m["macd"].iloc[-1] < m["signal"].iloc[-1]
        else "neutral",
        "note": f"histogram {hist:+.2f}" if hist is not None else "n/a",
    }

    a = adx(df)
    adx_v = _num(a["adx"].iloc[-1])
    trending = bool(adx_v and adx_v > 20)
    out["adx"] = {
        "value": adx_v,
        "bias": "bullish" if trending and a["plus_di"].iloc[-1] > a["minus_di"].iloc[-1]
        else "bearish" if trending and a["plus_di"].iloc[-1] < a["minus_di"].iloc[-1]
        else "neutral",
        "note": f"ADX {adx_v:.1f} ({'trending' if trending else 'weak trend'})" if adx_v else "n/a",
        "trending": trending,
    }

    st = supertrend(df)
    st_dir = float(st["direction"].iloc[-1])
    out["supertrend"] = {
        "value": _num(st["supertrend"].iloc[-1]),
        "bias": "bullish" if st_dir > 0 else "bearish",
        "note": "trailing below price" if st_dir > 0 else "trailing above price",
    }

    r = _num(rsi(close).iloc[-1])
    out["rsi"] = {
        "value": r,
        "bias": "bullish" if r and r > 55 else "bearish" if r and r < 45 else "neutral",
        "note": (f"RSI {r:.1f}" + (" (overbought)" if r > 70 else " (oversold)" if r < 30 else ""))
        if r else "n/a",
    }

    s = stochastic(df)
    k_v, d_v = _num(s["k"].iloc[-1]), _num(s["d"].iloc[-1])
    out["stochastic"] = {
        "value": k_v,
        "bias": "bullish" if k_v and d_v and k_v > d_v and k_v < 80
        else "bearish" if k_v and d_v and k_v < d_v and k_v > 20
        else "neutral",
        "note": f"%K {k_v:.1f}" if k_v else "n/a",
    }

    c_v = _num(cci(df).iloc[-1])
    out["cci"] = {
        "value": c_v,
        "bias": "bullish" if c_v and c_v > 100 else "bearish" if c_v and c_v < -100 else "neutral",
        "note": f"CCI {c_v:.0f}" if c_v else "n/a",
    }

    w_v = _num(williams_r(df).iloc[-1])
    out["williams_r"] = {
        "value": w_v,
        "bias": "bullish" if w_v and w_v > -50 else "bearish" if w_v and w_v < -50 else "neutral",
        "note": f"%R {w_v:.0f}" if w_v else "n/a",
    }

    roc_v = _num(roc(close).iloc[-1])
    out["roc"] = {
        "value": roc_v,
        "bias": "bullish" if roc_v and roc_v > 0.1 else "bearish" if roc_v and roc_v < -0.1 else "neutral",
        "note": f"{roc_v:+.2f}% over 12 candles" if roc_v is not None else "n/a",
    }

    atr_series = atr(df)
    atr_v = _num(atr_series.iloc[-1])
    atr_median = _num(atr_series.tail(100).median())
    out["atr"] = {
        "value": atr_v,
        "bias": "neutral",
        "note": f"ATR {atr_v:.2f} ({atr_v / last * 100:.2f}% of price)" if atr_v else "n/a",
        "elevated": bool(atr_v and atr_median and atr_v > atr_median * 1.4),
        "median": atr_median,
    }

    bb = bollinger(close)
    bb_width = _num(bb["width"].iloc[-1])
    out["bollinger"] = {
        "value": bb_width,
        "bias": "bullish" if last > bb["upper"].iloc[-1] else "bearish" if last < bb["lower"].iloc[-1] else "neutral",
        "note": f"band width {bb_width:.2f}%" if bb_width else "n/a",
        "squeeze": _is_squeeze(bb),
    }

    kc = keltner(df)
    outside = last > kc["upper"].iloc[-1] or last < kc["lower"].iloc[-1]
    out["keltner"] = {
        "value": _num(kc["upper"].iloc[-1]),
        "bias": "bullish" if last > kc["upper"].iloc[-1] else "bearish" if last < kc["lower"].iloc[-1] else "neutral",
        "note": "price outside channel" if outside else "price inside channel",
    }

    o = obv(df)
    obv_slope = _num(o.diff().tail(10).mean())
    out["obv"] = {
        "value": _num(o.iloc[-1]),
        "bias": "bullish" if obv_slope and obv_slope > 0 else "bearish" if obv_slope and obv_slope < 0 else "neutral",
        "note": "on-balance volume rising" if obv_slope and obv_slope > 0 else "on-balance volume falling",
    }

    vwap_v = _num(vwap(df).iloc[-1])
    out["vwap"] = {
        "value": vwap_v,
        "bias": "bullish" if vwap_v and last > vwap_v else "bearish" if vwap_v else "neutral",
        "note": f"price {'above' if vwap_v and last > vwap_v else 'below'} session VWAP" if vwap_v else "n/a",
    }

    vma = _num(volume_ma(df).iloc[-1])
    cur_vol = _num(df["volume"].iloc[-1])
    out["volume"] = {
        "value": cur_vol,
        "bias": "neutral",
        "note": f"{cur_vol / vma:.2f}x 20-candle average" if vma and cur_vol else "n/a",
        "expanding": bool(vma and cur_vol and cur_vol > vma * 1.3),
    }

    return out


def _stacked(emas: dict[int, pd.Series], ascending: bool) -> bool:
    """True when the EMA ribbon is fully ordered (20>50>100>200 or reverse)."""
    pairs = ((20, 50), (50, 100), (100, 200))
    checks = []
    for a, b in pairs:
        va, vb = _num(emas[a].iloc[-1]), _num(emas[b].iloc[-1])
        if va is None or vb is None:
            continue
        checks.append(va > vb if ascending else va < vb)
    return bool(checks) and all(checks)


def _is_squeeze(bb: pd.DataFrame) -> bool:
    """Band width in the lowest quintile of the last 100 candles."""
    width = bb["width"].tail(100).dropna()
    if len(width) < 20:
        return False
    return bool(width.iloc[-1] <= width.quantile(0.2))


def _num(value) -> float | None:
    """NaN-safe float conversion; JSON cannot carry NaN."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return None if np.isnan(f) or np.isinf(f) else round(f, 6)


def chart_series(df: pd.DataFrame) -> dict:
    """Indicator lines the chart overlays, as JSON-safe point lists."""

    def pts(series: pd.Series) -> list[dict]:
        return [
            {"time": int(ts.timestamp()), "value": round(float(v), 3)}
            for ts, v in series.dropna().items()
        ]

    m = macd(df["close"])
    bb = bollinger(df["close"])
    return {
        "ema20": pts(ema(df["close"], 20)),
        "ema50": pts(ema(df["close"], 50)),
        "ema100": pts(ema(df["close"], 100)),
        "ema200": pts(ema(df["close"], 200)),
        "vwap": pts(vwap(df)),
        "bb_upper": pts(bb["upper"]),
        "bb_lower": pts(bb["lower"]),
        "rsi": pts(rsi(df["close"])),
        "macd": pts(m["macd"]),
        "macd_signal": pts(m["signal"]),
        "macd_hist": pts(m["hist"]),
        "atr": pts(atr(df)),
    }
