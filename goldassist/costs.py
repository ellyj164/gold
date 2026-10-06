"""Transaction costs: what the broker takes out of every trade.

Until now no module in this application subtracted anything. Every R, every
pip figure, every reward/risk ratio -- live and in backtests -- was gross, and
a gross number is not what a trader is paid. On XAUUSD the gap is not
decorative: a typical retail Gold spread is 15-30 pips against a 500-pip first
objective and a stop that is often 250, so the round turn is routinely 5-10% of
the edge and, at the margin, the difference between a setup clearing the
profile's ``min_reward_risk`` gate and failing it.

The rule this module follows is the same one the rest of the application
follows: **measure, never assume.**

* **Spread is read, not guessed.** MetaTrader returns a ``spread`` column on
  every candle -- the broker's own recorded spread for that bar -- and
  :meth:`CostModel.from_frame` uses its median. A backtest therefore pays the
  spread that actually prevailed, including the fact that it widens at the
  rollover and around releases. Live, the current bid/ask is used directly.
* **Commission and slippage default to zero.** They are account-specific and
  this module has no way to observe them, so it does not invent them. A
  spread-only account (Exness Standard, most retail Gold) is priced correctly
  by default; a raw-spread account sets ``commission_per_lot`` once in
  ``config.json`` and every figure downstream follows.
* **An unavailable cost is stated, not silently zero.** ``source`` records
  where each number came from so the interface can say so.

What is charged, and where
--------------------------

One full spread per round turn, whichever way the trade runs, because the
candle series is a *bid* series:

* A long is bought at the ask (bid + spread) and sold back at the bid, so it
  pays the spread on entry.
* A short is sold at the bid and bought back at the ask, so it pays the spread
  on exit.

Either way the round turn costs exactly one spread, which is why it is applied
once against the closed result rather than being smeared across the fill and
the exit price. Doing it that way keeps ``fill_price`` and ``exit_price``
meaning what they say -- the prices on the chart where the trade opened and
closed -- and confines the cost to the figures that are meant to be net.

Gross and net are both kept. ``result_pips`` and ``r_multiple`` stay gross and
keep their existing meaning, so stored history remains comparable; the new
``net_result_pips`` and ``net_r_multiple`` are what the statistics, the
expectancy layer and the reward/risk gate read.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict

# XAUUSD pip convention, matching signals/opportunity.py: 1 pip = 0.01 of price.
PIP = 0.01

# Contract size for a standard XAUUSD lot: 100 troy ounces. Only used to turn a
# per-lot commission into price units, and overridden from the live symbol when
# the terminal is connected.
DEFAULT_CONTRACT_SIZE = 100.0

# The smallest price increment the symbol quotes in. MT5 reports a candle's
# spread in *points*, so this is what converts that integer into a price.
# Overridden from ``symbol_info`` live; 0.01 matches a two-digit Gold feed.
DEFAULT_POINT = 0.01

# A spread reading above this many pips is treated as a data artefact rather
# than a quote. Gold's spread blows out at the rollover and around releases,
# and genuinely reaching three dollars is possible for seconds at a time -- but
# a *median* that high means the column is not in the units assumed, and
# silently charging it would gut every result.
MAX_CREDIBLE_SPREAD_PIPS = 300.0


@dataclass
class CostModel:
    """What one round turn costs, in price units."""

    spread: float = 0.0                 # price units, one full spread
    commission_per_lot: float = 0.0     # account currency, round turn, per lot
    slippage_pips: float = 0.0          # per side
    contract_size: float = DEFAULT_CONTRACT_SIZE
    point: float = DEFAULT_POINT
    source: str = "none"                # measured | live | configured | none
    note: str = ""

    # -- the number every caller actually wants ------------------------------

    @property
    def round_turn(self) -> float:
        """Total cost of opening and closing one position, in price units."""
        commission = (self.commission_per_lot / self.contract_size
                      if self.contract_size else 0.0)
        return self.spread + commission + self.slippage_pips * PIP * 2

    @property
    def round_turn_pips(self) -> float:
        return self.round_turn / PIP

    @property
    def available(self) -> bool:
        return self.source != "none"

    def net_price_move(self, gross: float) -> float:
        """A signed price gain, after costs. Always worse, never better."""
        return gross - self.round_turn

    def net_pips(self, gross_pips: float) -> float:
        return gross_pips - self.round_turn_pips

    def net_reward_risk(self, reward: float, risk: float) -> float | None:
        """Reward/risk after costs.

        Both legs move: the reward shrinks by the round turn and the risk grows
        by it, because a stop is also crossed at the worse side of the quote.
        This is the figure the profile's ``min_reward_risk`` gate reads -- a
        gross 1.5 on a 250-pip stop with a 25-pip round turn is a net 1.34, and
        the gate should be told the truth about that.
        """
        if not risk or risk <= 0:
            return None
        net_reward = max(0.0, reward - self.round_turn)
        net_risk = risk + self.round_turn
        return net_reward / net_risk

    def to_dict(self) -> dict:
        d = asdict(self)
        d["round_turn"] = round(self.round_turn, 4)
        d["round_turn_pips"] = round(self.round_turn_pips, 1)
        d["available"] = self.available
        d["headline"] = self.headline()
        return d

    def headline(self) -> str:
        if not self.available:
            return ("Transaction costs are not being modelled — every figure "
                    "below is gross.")
        parts = [f"{self.spread / PIP:.0f} pips spread"]
        if self.commission_per_lot:
            parts.append(f"{self.commission_per_lot:.2f}/lot commission")
        if self.slippage_pips:
            parts.append(f"{self.slippage_pips:.0f} pips slippage per side")
        return (f"Round turn costs {self.round_turn_pips:.0f} pips "
                f"({', '.join(parts)}) — {self.note or self.source}")

    # -- constructors --------------------------------------------------------

    @classmethod
    def disabled(cls, note: str = "") -> "CostModel":
        """The honest zero: no cost applied, and said so."""
        return cls(source="none", note=note or "no cost data available")

    @classmethod
    def from_frame(cls, frame, point: float = DEFAULT_POINT,
                   commission_per_lot: float = 0.0,
                   slippage_pips: float = 0.0,
                   contract_size: float = DEFAULT_CONTRACT_SIZE) -> "CostModel":
        """Costs measured from the broker's own recorded spread per candle.

        This is what makes a backtest's cost real rather than a parameter: the
        ``spread`` column comes from MetaTrader and records what the spread
        actually was on that bar. The median is used rather than the mean
        because the rollover hour's blowout would otherwise dominate a figure
        that is meant to describe the typical bar.
        """
        if frame is None or len(frame) == 0 or "spread" not in getattr(frame, "columns", []):
            return cls.disabled("no spread column in the candle data")

        series = frame["spread"].dropna()
        series = series[series > 0]
        if series.empty:
            return cls.disabled("the broker recorded no spread on these candles")

        spread = float(series.median()) * point
        if spread / PIP > MAX_CREDIBLE_SPREAD_PIPS:
            return cls.disabled(
                f"recorded spread of {spread / PIP:.0f} pips is not credible; "
                "costs left unmodelled rather than guessed")

        return cls(
            spread=spread,
            commission_per_lot=commission_per_lot,
            slippage_pips=slippage_pips,
            contract_size=contract_size,
            point=point,
            source="measured",
            note=f"median of {len(series)} candles of broker-recorded spread",
        )

    @classmethod
    def from_symbol(cls, details: dict | None, tick: dict | None = None,
                    commission_per_lot: float = 0.0,
                    slippage_pips: float = 0.0) -> "CostModel":
        """Costs from the live terminal: the current quote and the symbol spec."""
        details = details or {}
        point = float(details.get("point") or DEFAULT_POINT) or DEFAULT_POINT
        contract = float(details.get("contract_size") or DEFAULT_CONTRACT_SIZE)

        spread = None
        if tick and tick.get("ask") and tick.get("bid"):
            spread = float(tick["ask"]) - float(tick["bid"])
        if not spread or spread <= 0:
            return cls.disabled("the terminal served no bid/ask to measure a spread")
        if spread / PIP > MAX_CREDIBLE_SPREAD_PIPS:
            return cls.disabled(
                f"quoted spread of {spread / PIP:.0f} pips is not credible")

        return cls(
            spread=spread,
            commission_per_lot=commission_per_lot,
            slippage_pips=slippage_pips,
            contract_size=contract,
            point=point,
            source="live",
            note="current bid/ask from the terminal",
        )
