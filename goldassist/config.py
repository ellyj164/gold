"""Application configuration.

Values come from (in order of precedence): environment variables, the
user config file at ~/.goldassist/config.json, then the defaults here.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path

APP_DIR = Path(os.environ.get("GOLDASSIST_HOME", Path.home() / ".goldassist"))
CONFIG_PATH = APP_DIR / "config.json"
DB_PATH = APP_DIR / "goldassist.db"

# Timeframes the application analyses, from macro context down to confirmation.
#
# D1 and H4 set direction and mark the major zones; H1, M30 and M15 are where
# an intraday trader actually times entries inside that direction; M5 confirms.
# M30 is carried explicitly because it is one of the execution timeframes this
# method uses, not merely a step between H1 and M15.
TIMEFRAMES = ("D1", "H4", "H1", "M30", "M15", "M5", "M1")

# Timeframes a backtest replays. Deliberately excludes M1.
#
# A replay needs every timeframe to span the same period, and M1 does not: the
# terminal holds a few days of it against the several months it holds of M5.
# Including it would leave `_slice_frames` unable to serve 60 M1 candles for
# almost every step of the replay, and the backtest would return nothing at
# all. M1 earns its place in the live analysis, where "the last few days" is
# exactly the window that matters, and has no place in a six-month replay.
BACKTEST_TIMEFRAMES = ("D1", "H4", "H1", "M30", "M15", "M5")

# How long each timeframe's candle lasts, in minutes.
#
# MetaTrader indexes a candle by its *open* time, which is the single most
# dangerous thing to forget when replaying history: at 12:00 the D1 candle
# indexed 00:00 is still forming, and its high, low and close describe the
# whole day -- twelve hours of which have not happened yet. Comparing the
# index against the replay clock at all (`index <= now`, or `index < now`)
# therefore hands the engines the future. A candle is closed only once
# `open + this duration` has passed, and that is the test to use.
#
# The live path never needs this because the forming candle is always the last
# row, which `MarketFeed.frame` drops outright.
TIMEFRAME_MINUTES = {
    "D1": 1440, "H4": 240, "H1": 60, "M30": 30, "M15": 15, "M5": 5, "M1": 1,
}

# Timeframes that set the directional bias. Nothing below these may overrule
# them; a setup that disagrees with both is counter-trend by definition.
BIAS_TIMEFRAMES = ("D1", "H4")

# Timeframes an entry may be timed on.
EXECUTION_TIMEFRAMES = ("H1", "M30", "M15")

# Candles used to resolve what happened to a signal -- whether a stop or a
# target was touched between two five-second polls. Outcome resolution needs
# the finest candles available, independently of what the analysis reads.
#
# M1 is now also in TIMEFRAMES. It was originally excluded on the grounds that
# it is mostly noise, which is true of M1 *as a source of bias* and not true of
# it as a source of detail: the trader asked for every candle to be watched,
# and a stop hunt that happens and reverses inside one M5 candle is invisible
# to every engine that starts at M5. M1 carries no weight in the directional
# read (see the weights in analysis/levels.py and analysis/zones.py, where it
# is the lowest rung) -- it contributes detail, not opinion.
TRACKING_TIMEFRAME = "M1"

# Below a minute the broker has no candles at all, so these are aggregated from
# the raw tick stream by analysis/micro.py rather than downloaded.
MICRO_TIMEFRAMES = ("S5", "S15", "S30")

# How much tick history to pull for that aggregation. Fifteen minutes is enough
# for the slowest sub-minute rung to have a shape, and small enough that the
# request costs nothing on a five-second cycle.
TICK_WINDOW_SECONDS = 900

# How many candles of history each timeframe keeps in memory.
HISTORY_BARS = {
    # M1 is both the tracking timeframe and the finest analysed one, so it
    # needs enough history for structure and indicators (the 200-period EMA
    # alone wants 200 bars) as well as enough to resolve any setup still on the
    # board -- fifteen hours covers both.
    #
    # Not more than that: analysing M1 costs real time on a five-second cycle,
    # and it is the one timeframe where extra history buys nothing. 1500 bars
    # measured ~0.53s per cycle against ~0.18s for 600; 900 sits at ~0.3s,
    # which keeps a full cycle comfortably inside the refresh interval.
    "M1": 900,
    "D1": 400,
    "H4": 800,
    "H1": 1500,
    "M30": 2000,
    "M15": 2000,
    "M5": 2000,
}


@dataclass
class MT5Config:
    """Connection details for the MetaTrader 5 terminal used as data source."""

    terminal_path: str = r"C:\Program Files\MetaTrader 5 EXNESS\terminal64.exe"
    # Preferred symbol names, tried in order. Brokers suffix Gold differently
    # (XAUUSDm on Exness cent/standard accounts, XAUUSD. on some others).
    symbol_candidates: tuple[str, ...] = (
        "XAUUSD", "XAUUSDm", "XAUUSD.", "XAUUSDc", "GOLD", "GOLDm",
    )
    login: int | None = None
    password: str | None = None
    server: str | None = None


@dataclass
class AIConfig:
    """Claude API settings for the natural-language layer."""

    enabled: bool = True
    model: str = "claude-opus-5"
    api_key_env: str = "ANTHROPIC_API_KEY"
    max_tokens: int = 1200
    # When the API is unreachable the app falls back to deterministic,
    # locally generated explanations so analysis never stops.
    offline_fallback: bool = True


@dataclass
class AppConfig:
    mt5: MT5Config = field(default_factory=MT5Config)
    ai: AIConfig = field(default_factory=AIConfig)
    host: str = "127.0.0.1"
    port: int = 8765
    refresh_seconds: int = 5
    # The clock every timestamp is *shown* on. Storage stays UTC; this is
    # presentation only. See goldassist.timeutil for why it is a fixed offset.
    display_utc_offset: float = 2.0

    @classmethod
    def load(cls) -> "AppConfig":
        cfg = cls()
        if CONFIG_PATH.exists():
            raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            cfg.mt5 = MT5Config(**{**asdict(cfg.mt5), **raw.get("mt5", {})})
            cfg.ai = AIConfig(**{**asdict(cfg.ai), **raw.get("ai", {})})
            for key in ("host", "port", "refresh_seconds", "display_utc_offset"):
                if key in raw:
                    setattr(cfg, key, raw[key])
        if os.environ.get("GOLDASSIST_SYMBOL"):
            cfg.mt5.symbol_candidates = (os.environ["GOLDASSIST_SYMBOL"],)
        if os.environ.get("GOLDASSIST_MT5_PATH"):
            cfg.mt5.terminal_path = os.environ["GOLDASSIST_MT5_PATH"]
        return cfg

    def save(self) -> None:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        payload = {
            "mt5": asdict(self.mt5),
            "ai": asdict(self.ai),
            "host": self.host,
            "port": self.port,
            "refresh_seconds": self.refresh_seconds,
            "display_utc_offset": self.display_utc_offset,
        }
        CONFIG_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
