# AI-Powered Gold Trading Decision Assistant (XAUUSD)

A Windows desktop application that analyses Gold across five timeframes, scores the
technical evidence, and produces a **BUY / SELL / WAIT** decision with the reasoning
behind it — what supports the setup, what argues against it, what would confirm it,
and what would invalidate it.

**It never trades.** The application does not open, modify, or close positions, does
not connect to your account for execution, and does not give financial advice. A setup
score measures the weight of technical evidence, not a probability of profit.

---

## Running it

There are three ways, easiest first.

### 1. Install it (recommended)

Run `dist\GoldAssistant-Setup-1.0.0.exe`. It installs per-user — no administrator
prompt — and gives you a Start Menu entry, an optional desktop shortcut, and an
uninstaller in Add/Remove Programs. After that, launch it like any other app.

No Python needed on the machine: the installer bundles its own.

### 2. Portable — no install

Copy the `dist\GoldAssistant` folder anywhere (a USB stick works) and run
`GoldAssistant.exe`.

### 3. From source

```bash
python -m venv --system-site-packages .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m goldassist.app
```

Or just double-click `Gold Assistant.bat`.

### Before you launch, either way

**MetaTrader 5 must be open and logged in.** The application reads Gold prices
from the running terminal; without it there is nothing to analyse. If MT5 is
closed you will see a connection error in the header rather than a silent failure.

**The first launch is slow.** Before the window appears the app connects to MT5,
downloads a history window for all five timeframes, and runs a full analysis.
On a cold terminal that can take a minute or two; afterwards it starts in seconds.

**Optional — enable the AI analyst.** Set `ANTHROPIC_API_KEY` in your Windows
environment variables and restart the app. Without it everything still works;
explanations are generated locally instead, and the header says
"offline explanations" rather than "AI analyst online".

### Command-line options

| Option | Effect |
|---|---|
| *(none)* | Opens the desktop window |
| `--browser` | Opens in your default browser instead |
| `--no-window` | Runs the server only, no UI |
| `--port 9000` | Use a different port |
| `--verbose` | Debug logging |

`GoldAssistant-debug.exe` is the same application with a console attached — run
that first if anything misbehaves.

### It is a standalone window, and only one of it

The application opens in its own desktop window (Edge WebView2), not a browser tab.

Only one copy may run at a time. Launching it again finds the window that is already
open and raises it. This matters more than it sounds: the application is a *server* with
a window in front of it, so a second launch does not merely show a second window — it
starts a second MetaTrader 5 session and a second analysis loop, and because port 8765
is already taken it binds a random high port instead. A stray `http://127.0.0.1:61774`
is that: a second server nobody meant to start, with two analysis loops writing the same
signals into one database.

If the window cannot open — almost always a missing
[Edge WebView2 Runtime](https://developer.microsoft.com/microsoft-edge/webview2/) — it
falls back to the browser and says why in `goldassist.log`. It no longer does so
silently.

---

## Building the installer yourself

```bash
packaging\build.bat
```

Needs PyInstaller (installed automatically) and Inno Setup
(`winget install JRSoftware.InnoSetup`). Output lands in `dist\`.

---

## How it is put together

The central design rule from the specification: **the technical engine measures, the AI
explains.** The AI never invents a signal and never sees raw price — it receives the
facts the engine computed and turns them into language.

```
MetaTrader 5  ->  Market feed  ->  Analysis engines  ->  Confluence  ->  Decision
                                        |                                  |
                                        +----------> facts ------> AI analyst
                                        |                                  |
                                        v                                  v
                                  Setup book  ------------------->  Trading desk
                                (every live setup)          (context, conviction,
                                        |                    trade management)
                                        v
                        SQLite  <--  signals, outcomes, journal, backtests
```

| Module | Responsibility |
|---|---|
| `goldassist/data/` | MT5 connection, candle cache, live ticks |
| `goldassist/analysis/` | Indicators, market structure, candlesticks, S/R, supply & demand, liquidity, FVG, order blocks, regime, anchored VWAP, session auction, statistical persistence, multi-timeframe assembly |
| `goldassist/fundamentals/` | Real yields, inflation breakevens, the dollar, CFTC positioning, COMEX reference prices and implied volatility — why Gold is priced where it is |
| `goldassist/decision/` | Confluence scoring, BUY/SELL/WAIT engine, risk, strategy profiles |
| `goldassist/strategies/` | The strategy library: 23 independent detectors across smart money, trend, range and price action |
| `goldassist/signals/` | Entry zones, triggers, lifecycle, bar-accurate resolution, and the setup book that tracks every live setup |
| `goldassist/trader/` | The behaviour layer: top-down method, day character, session view, trade management, outcome memory, dollar context |
| `goldassist/ai/` | Claude analyst, chart-image vision, deterministic offline explanations |
| `goldassist/backtest/` | Historical replay through the live engines, plus statistical rigour and out-of-sample walk-forward |
| `goldassist/storage/` | Signal history, trading journal, backtests, settings |
| `goldassist/costs.py` | Transaction costs — spread measured from the feed, commission and slippage from config |
| `goldassist/api/` | Local FastAPI server, analysis loop, WebSocket push |
| `goldassist/ui/` | Trading terminal (custom canvas chart + panels) |

### Timeframe roles

| Timeframe | Job |
|---|---|
| D1 | macro context — major trend, major levels, big liquidity |
| H4 | primary market structure and directional bias |
| H1 | trading context — current trend, zones, setup areas |
| M30 | intraday execution — timing entries inside the H4/D1 direction |
| M15 | setup formation — sweeps, micro structure, entry area |
| M5 | confirmation — BOS, rejection, retest, momentum |
| M1 | detail — the sweep, wick or reversal that happens *inside* an M5 candle |
| S30 / S15 / S5 | the tape — aggregated from raw ticks, because no broker serves sub-minute candles |

Every candle on that ladder is read on every cycle. The three sub-minute rungs are
built from the tick stream by `analysis/micro.py` rather than downloaded, and they are
the answer to a specific blind spot: between two M5 closes lie five minutes in which a
level can be swept, the spread can blow out and momentum can turn over, none of which a
closed-candle engine sees until it is already over.

The tape never produces a signal of its own. It qualifies setups the candle engines
have already found — it may move a candidate's conviction by at most four points — and
it is shown on the terminal as an observation. What price did in the last thirty seconds
is worth something for timing an entry and almost nothing for choosing a direction.

Reading a deeper ladder costs nothing extra, because the per-timeframe analysis is now
cached on the newest closed candle. The engines only ever see *closed* candles, so
between two closes their input is identical and so is their output — the daily analysis
was being recomputed 17,280 times a day for the same answer. With the cache a full cycle
runs in about half the time it took before M1 and the tape were added at all.

M1 carries the lowest weight of any analysed timeframe (0.12 against the daily's 1.00
for levels) for the same reason: it contributes detail, not opinion. Backtesting
deliberately excludes it — the terminal holds a few days of M1 against several months of
M5, and a replay needs every timeframe to span the same period.

### How a setup is scored

Seven pillars, 100 points total. No single pillar can produce a setup:

| Pillar | Max |
|---|---|
| Higher-timeframe direction | 18 |
| Trading-timeframe structure | 12 |
| Price location (zone / order block / FVG) | 20 |
| Liquidity sweep | 14 |
| Structure confirmation | 18 |
| Price-action confirmation | 8 |
| Momentum & trend indicators | 10 |

Penalties are then subtracted — an opposing level in the way, higher-timeframe conflict,
elevated volatility, an extended entry, a non-trending regime, imminent high-impact news.
A *critical* penalty blocks entry regardless of score, which is how "everything is bullish
but price sits under major resistance" correctly becomes **WAIT**.

> **Location is one pillar, not three.** Supply/demand zones, order blocks and FVGs measure
> the same thing — that price has arrived somewhere significant. Scoring them separately
> was measured against ten months of Gold and could not reach 70/100 even once; as a single
> pillar the scale behaves as intended.

### Why Gold is priced where it is

The chart says *what* Gold has done. It cannot say *why*, and for the one
instrument where the why is unusually legible that was a real gap. Four keyless
public sources fill it, each cached, each independently optional, none of them
a dependency — with no connection at all the technical engine runs exactly as
it did before.

| Reading | Source | Why it matters |
|---|---|---|
| **10-year real yield** | FRED (`DFII10`) | The dominant driver. Gold pays no coupon, so what it competes with is the yield *after* inflation. Read as a level and as a weekly change |
| Inflation breakevens, 5y5y forward | FRED | Separates "inflation hedge" buying from "rate cut" buying — the two behave differently |
| Nominal curve, broad dollar | FRED | The 2s10s shape, and a better dollar measure than inverting EURUSD |
| **CFTC positioning** | Weekly COT | How crowded speculative length is against its own history. The one input no retail chart package carries |
| **Implied volatility (GVZ)** | CBOE | What the options market expects, against what Gold has actually realised. Not derivable from price |
| COMEX front month | `GC=F` | The venue Gold is actually priced on — and a check that the broker's quote has not drifted from it |

Three rules govern the whole layer:

- **A missing input is stated, never invented.** Every reading carries whether
  it is available and where it came from. Stale is labelled with its age, not
  discarded — yesterday's real yield is a better answer than none, because the
  series only updates once a day anyway.
- **Confidence falls when information falls.** The composed strength is the
  product of how one-sided the drivers are *and* how many of them reported. A
  read assembled from one lagging source cannot present itself as a unanimous
  macro picture.
- **Fundamentals contextualise; they never gate.** At full strength a
  completely one-sided macro picture moves conviction by a tenth. It can never
  suppress a setup, in either direction. This is daily data informing an
  intraday decision, and a macro opinion quietly becoming an all-day
  directional filter is the failure mode the bound exists to prevent.

### The intraday auction

Three engines that read the day's own reference prices rather than price shape:

**Anchored VWAP** at the day, session and week, with volume-weighted standard
deviation bands. Anchored rather than rolling, because the useful reference is
an event — from that moment the average is the real cost basis of everyone who
has participated since. The anchor's *slope* is reported beside the bands
because it decides which of two opposite trades a band touch justifies: against
a flat anchor the touch is a fade, against a sloping one price rides the band
and the same touch is a continuation.

**Session structure** — prior day high/low/close, the Asian range, the initial
balance, whether price has extended beyond it, and how much of a typical daily
range is left. Six known prices before any pattern has been detected at all,
each tested repeatedly within a session and in both directions.

**Statistical persistence** — the Lo-MacKinlay variance ratio with a
heteroskedasticity-robust z-statistic, the Hurst exponent, and Kaufman's
efficiency ratio. `regime.py` classifies with ADX and Bollinger width; those
describe the recent path without ever testing whether it is distinguishable
from a random walk. This does, with three unrelated estimators, and reports
their disagreement rather than forcing a verdict.

It picks the **playbook, not the direction**: trade breakouts and let runners
go in a persistent regime, fade the band extremes and bank the first objective
in a reverting one. The regime changes within a session — Gold routinely
reverts through Asia and trends through London — and the same setup deserves
opposite treatment on either side of that switch.

Three detectors trade against these references: `vwap_band_fade`,
`vwap_reclaim` and `ib_extension`. The library already had two VWAP strategies
(`vwap_pullback` and `vwap_reversion`) built on the *indicator* VWAP and an ATR
stretch; these read the *anchored* VWAP and measure excursion in standard
deviations, and only fade when the anchor is flat. Where the two agree the book
merges them into one setup with both arguments attached, which is what
confluence means.

`vwap_band_fade` and `vwap_reclaim` are opposite trades and both are correct in
different regimes, so the regime **sizes** them and never silences them — a
fade setup in a trending market still appears, with its conviction cut to a
quarter and the reason on the card.

### Checking a backtest is not luck

```bash
python -m goldassist.backtest --rigour         # significance, drawdown, costs
python -m goldassist.backtest --walk-forward   # would it have been usable in advance?
python -m goldassist.backtest --stability      # did the ordering hold across folds?
```

`--rigour` reports four things the summary alone cannot:

- **Significance at two scales.** Trades overlap — this method runs up to five
  positions at once — so the trade-level t-statistic overstates its own
  confidence. The test is run again on monthly totals, where the clustering is
  aggregated away, and the monthly figure is the one to believe.
- **Mark-to-market drawdown.** The reported figure is a cumulative sum in exit
  order, which cannot see a period when three positions were open and all
  underwater. Marked to market it is roughly **double**: 32.3R rather than
  16.8R on the ten-month run. That is what the account went through.
- **Concurrency.** Every R figure assumes one unit of risk per trade. This
  measures whether that assumption holds.
- **Cost sensitivity.** Slippage cannot be observed from a candle feed, so
  instead of guessing a constant this reports how much the result moves at
  several. On this method, wide structural stops make it 1.6% of the edge.

`--walk-forward` grades decisions made only from a training window against the
test window that decision could not see — the difference between an ordering
that was consistent in hindsight and one that would have been usable in advance.

```bash
python -m goldassist.backtest --live-check        # is the live record inside the backtest's band?
python -m goldassist.backtest --fit               # do fitted evidence weights beat the judged score?
python -m goldassist.backtest --reserve-holdout 45    # set aside the last 45 days, once
python -m goldassist.backtest --holdout           # replay that holdout, once
```

`--live-check` is the acceptance test the rest answer to. It draws as many trades
from the backtest as the live record holds, thousands of times, and reports where
the live total falls. **On the first live week it fell at the 1st–2nd percentile
of backtests #1 and #2** — with 18 clustered trades that is a warning, not a
verdict, but it is the reason v1.8.0 exists.

`--fit` regresses whether each trade paid on the evidence it armed on, walk-forward,
and prints suggested `weight_overrides` only if the fit ranks out-of-sample trades
better than the judged score. It never applies them. Runs stored before v1.8.0
carry no evidence per trade and cannot be fitted.

`--rigour` and every new run now also print a **Monte Carlo** band: the same
trades reshuffled 5,000 times, so the drawdown to plan for is the 95th percentile
rather than the one ordering that happened. Each run also says how many variants
have already been run over its window, because the best of many is optimistic by
construction.

### Strategy profiles

`Default XAUUSD`, `Strict Smart Money`, `Trend Continuation`, `Reversal Hunter`,
`Maximum Coverage`, `High Conviction` — each sets which timeframes carry bias, which
conditions are mandatory, the score a setup must reach, **which strategies are enabled**,
and where the trade is banked. Profiles are editable and saved to
`~/.goldassist/profiles.json`.

| Setting | What it changes |
|---|---|
| `enabled_strategies` | Which detectors may produce setups. Unset means all 23 — `Trend Continuation` and `Reversal Hunter` each run their own subset |
| `close_at_target` | Which objective banks the trade. `1` closes at TP1, honouring the 500-pip rule; `2` lets a runner go with the stop moved to breakeven behind it |
| `min_reward_risk` | A gate, not a note: the objective actually banked must clear it |
| `setup_expiry_minutes` / `trade_expiry_minutes` | When an unfilled idea, or a trade going nowhere, is retired |

`Maximum Coverage` is the configuration that surfaces the most 500-pip opportunities per
day: every strategy, a low watch threshold, banked at the first objective.
`High Conviction` is the opposite trade-off — fewer setups, each held for a runner.

---

## Signal suggestions

The analysis answers *what is Gold doing*. The signal module answers *what would I
actually trade* — and it never forces a signal when conditions are unclear.

Each tracked setup is one evolving scenario, not a fresh opinion each tick:

| Field | Meaning |
|---|---|
| **Setup type** | Pullback, Breakout, Reversal, Continuation or Range Rotation |
| **Entry zone** | A price *area*, built where demand/supply, order blocks, FVGs, S/R and the Fibonacci band overlap — with every contributing area named |
| **Preferred entry** | The best fill inside the zone, biased to the deep side where risk is smallest |
| **Entry trigger** | What must happen to confirm — stricter for a reversal than a pullback |
| **Invalidation** | Structural, and measured **from the zone**, not from spot |
| **TP1 / TP2 / TP3** | Laddered objectives, each with its own basis and R:R |
| **Score & quality** | 0–100 graded No Setup → Weak → Developing → Good → Strong → Very Strong |
| **Strategy** | Which of the 23 detectors found it, plus any others that agree |
| **Status** | DEVELOPING → WATCH → IN ZONE → WAITING FOR CONFIRMATION → CONFIRMED → ACTIVE → **TARGET HIT / STOPPED OUT / EXPIRED / INVALIDATED**, or WEAKENING |

Three rules the state machine enforces, all easy to get wrong:

- **Confirmation only counts once price is in the zone.** A trigger firing while price
  is still far away does not make a setup tradeable — that is chasing.
- **A setup only fills once it has confirmed.** Price touching an entry zone that never
  confirmed is not a trade anyone took, and counting it as one fills the record with
  trades the method forbids.
- **Targets only count after the fill.** A setup created while price is already beyond
  TP1 has achieved nothing, because nobody was in the trade.

Alerts fire on stage changes (new setup, entering the zone, confirmation, target
banked, stopped out, weakening, invalidation), gated by the profile's
`min_alert_score` — except results and terminal states, which always announce.

---

### The strategy library

A market rarely offers one kind of trade. At any moment Gold may be pulling back into
H1 demand, leaving an unmitigated M15 order block above it, sweeping yesterday's low
and breaking the Asian range — four different trades. The application used to see one
of them, because a single classifier picked one label from an if-chain.

Every detector now runs on every cycle and answers only whether *its* pattern is
present:

| Family | Strategies |
|---|---|
| **Smart money** (6) | Supply/demand reaction · Order block mitigation · Imbalance (FVG) rebalance · Break-of-structure retest · Liquidity sweep reversal · Draw on liquidity |
| **Trend** (9) | Confluence read (the original engine) · EMA ribbon pullback · Supertrend retest · VWAP pullback · VWAP reversion · Momentum expansion · **Trendline break retest** · **Flag continuation** · **Channel rail** |
| **Range** (8) | Range edge fade · Range breakout retest · Asian range break · Volatility squeeze · Pivot reaction · **Opening range break** · **Triangle break** · **Compression break** |
| **Price action** (9) | RSI divergence · Double top/bottom · Round-number reaction · Fibonacci pocket · Rejection at level · Previous-day extreme · **Head and shoulders** · **Exhaustion climax** · **Session gap fill** |

Thirty-two detectors, all running every cycle. The nine in bold cover the setups that
form *before* a range is recognisable as one — a session's first hour, a flag hanging
off an impulse, a triangle squeezing shut, a trendline that has held four times, a
channel whose lower rail is a trade on every touch. None of those is a horizontal
envelope, so the range detectors could never have found them.

Each measures its own geometry off the candles and is allowed to be silent. A trendline
is fitted through real swing points and discarded if the fit is poor; a head and
shoulders has to pass a symmetry test, because any three swings resemble one if the
tolerances are loose enough.

Two scores are kept apart on purpose. **Conviction** is the strategy's own read — how
good an example of *this pattern* the setup is. **Context** is the confluence score for
that direction. The final score blends them, weighted by how much the strategy depends
on the higher timeframes agreeing (0.50 for trend-following, 0.25 for a deliberate
fade). Scoring a sweep reversal on a trend-weighted scale, which is what a single score
does, buries every counter-trend opportunity the market offers.

When two strategies point at the same area in the same direction they **merge** into one
setup carrying both arguments and a bounded score boost. That is what confluence means,
and it keeps the board from showing five names for one trade.

`GET /api/strategies` lists the library, which are enabled for the active profile, how
many are live, and what each has actually paid.

---

### The signal log — what the system actually called

The **Signals** tab is the record of every signal ever fired, written the moment it is
identified rather than when it closes.

It exists because neither of the other two records could answer "what did this system
call today?". The decision history records the top-level verdict, and only when that
verdict *changes* — so a morning that produced eleven setups from nine strategies could
leave a single row saying BUY. Setup results are complete but are written at
termination, so a signal that fired an hour ago and is still running appears nowhere.

Each row carries the whole life of one signal: the date, the time it fired, the session
it fired into, the strategy that found it, the entry zone, the stop, TP1, the pips on
offer, the reward/risk, the score — and then, as it happens, when it filled and how it
closed. Filter by day, side, session, or result.

**Times are the trader's clock, GMT+2, everywhere.** Storage stays UTC — that is the
right internal convention and none of it changed — but nothing is ever *shown* in UTC
again: not the signal cards, not the log, not the alert tape, not the chart's own time
axis. The clock in the top bar shows the current time and the live session beside it.

The time and session on a signal are frozen at the moment it fires and are never
recomputed. A setup found at the London open is a London-open setup for the rest of its
life, however long it takes to resolve; recomputing it later would let a signal quietly
change its own story after the fact. The display offset is configurable
(`display_utc_offset` in `config.json`) and is a fixed offset rather than a named zone,
so it cannot shift twice a year relative to the London and New York opens the method is
built around.

`GET /api/fired` serves the log; `GET /api/clock` serves the display clock.

---

### How a signal ends

The signal used to stay on the dashboard after price had already decided it. Now every
tracked setup is resolved:

| Ending | Meaning | Recorded as |
|---|---|---|
| **TARGET HIT** | the objective the profile banks at was reached | win, in pips and R |
| **COMPLETED** | every target was reached | win, in full |
| **STOPPED OUT** | the stop was hit after the fill | loss — or a scratch if the stop had already moved to breakeven |
| **INVALIDATED** | the level that had to hold broke before the entry filled | untaken |
| **EXPIRED** | the setup ran out of time, or its premise disappeared | untaken |

Resolution reads **candles, not spot**. Every M1 bar since the last check is walked, so
a wick that touched a stop between two five-second polls is found; the live quote is
applied on top as a partial bar so nothing waits for a close. Where one bar contains
both the stop and the target, the stop is assumed — a resolver that guesses in its own
favour produces a record that cannot be learned from.

A decided setup leaves the board immediately, carrying its result, and appears in
*Resolved today* and in the history tab. **That is what frees the slot for the next
opportunity.** The only restraint on re-admission is that the *identical* idea — same
strategy, same direction, same area — is not rebuilt on unchanged evidence for 20
minutes. Any different setup, and the same one on improved evidence, is admitted at
once.

---

### The 500-pip day-trading rule

On XAUUSD one pip is 0.01 of price, so **500 pips is a 5.00 move — a $5 move in
Gold**. Five hundred pips is the smallest opportunity worth taking intraday — a
floor, never a ceiling.

| First objective | Grade |
|---|---|
| under 500 pips ($5) | Below minimum — generally not worth a signal |
| 500–999 pips ($5–$10) | Good day-trading opportunity |
| 1000–1999 pips ($10–$20) | Strong opportunity |
| 2000+ pips ($20+) | Major intraday opportunity |

Three things this does *not* do:

- **It never caps a target at 500 pips.** Eligibility is judged on the first objective;
  the grade reports the *full* potential, so a setup with 500 pips to TP1 and 3000 to TP3
  is presented as Major, not as a 500-pip trade.
- **It never turns distance into a signal.** 500 pips being available makes a setup
  *eligible*; direction, entry zone, confirmation and invalidation still come from the
  technical analysis. Both must hold.
- **It never hides a small setup silently.** When nothing clears the floor the card still
  shows the best available objectives, flagged, with the shortfall stated.

The minimum is per-profile (), so a scalper can lower it.

---

### What the broker takes

Every figure this application produced used to be gross. Nothing subtracted a spread, a
commission or a slip, which on XAUUSD is not a rounding error: on this account the round
turn is **26 pips against a 500-pip first objective** — 5% of the target, and over 10% of a
typical 250-pip stop.

The spread is **measured, never assumed**. MetaTrader records the spread on every candle it
serves, so a backtest pays what the broker actually charged on those bars, rollover blowouts
included; live, the current bid/ask is read directly. The two agree to the pip on this
account. Commission and slippage cannot be observed from a data feed at all, so they default
to zero and are reported as unmodelled rather than invented — set them in `config.json` if
you are on a raw-spread account:

```json
{ "costs": { "commission_per_lot": 7.0, "slippage_pips": 1.0, "enabled": true } }
```

Gross and net are both kept, side by side, because the difference between them is itself
the finding: a method with a gross expectancy of +0.15R and a net expectancy of −0.02R does
not have a small edge, it has none, and only showing both makes that visible.

- `result_pips` and `r_multiple` stay gross and keep their original meaning, so stored
  history remains comparable across the change.
- `net_result_pips` and `net_r_multiple` are what the statistics and the reward/risk gate
  read. Net R uses the *same* denominator as gross — the initial stop distance — so a
  stop-out reads as −1.10R rather than −1.00R. Inflating both sides instead would make a
  stop-out come to exactly −1.00R net as well as gross, and the cost would vanish from the
  one number the statistics are built on.
- `min_reward_risk` is now applied to the **net** ratio. A gross 1.52 on a 250-pip stop is a
  net 1.29, and a gate reading the gross figure passes trades the method is meant to refuse.
- Backtests rank strategies on net R, not gross. Costs are charged per trade, so a gross
  ranking flatters whichever detector takes the most of them.
- `fill_price` and `exit_price` are untouched — they remain the prices on the chart where
  the trade opened and closed. The round turn is charged once against the result instead,
  which is also the correct model: a long buys the ask and sells the bid, a short does the
  reverse, and the candle series is a bid series either way.

---

## Trading like a trader, not just analysing like one

Analysis is roughly a third of what a discretionary trader actually does. The
`goldassist/trader/` package covers the rest.

**The method is stated explicitly.** D1 and H4 set direction and mark the major
zones; H1, M30 and M15 are where the entry is timed. Every setup is labelled
*with bias*, *counter-bias* or *no bias* against the D1/H4 read, and the panel
shows which execution timeframe the entry is currently being timed on.

**The day is read before the setup is.** Directional efficiency, whether levels
are being respected or cut through, how often structure has reversed, and
volatility against its own median combine into a tradeability score. The same
pullback into demand is a trade on a clean day and a donation on a choppy one;
the chart looks identical either way.

**A view is held, not re-derived.** A session view is written down once a day
with its rationale, its key levels and what would make it wrong. It is revised
only when the higher timeframes have disagreed for a sustained period *and* an
H4/H1 structure break confirms the new direction. Disagreement below that bar is
shown as a *challenge*, because a view being tested is itself information.

**The application no longer goes silent after the entry.** Once price trades
into an entry zone it tracks the position: breakeven once the trade has paid for
itself, a partial at the first objective, a trailing stop that follows structure
rather than a fixed distance, and warnings when a trade has stalled or given
back most of what it offered — before the stop is hit.

**It remembers what has worked.** Every tracked setup is recorded when it
finishes, grouped by setup type, session, regime, score band, direction and day
character, and read back at decision time. Below eight closed setups in a group
it says it does not know rather than inventing a trend, and every line states
its own sample size.

**It knows why Gold is moving.** A dollar index is read from the same terminal
(falling back to EURUSD inverted). The useful cases are the divergences: Gold
rising while the dollar rises is a different market from Gold rising on dollar
weakness.

### Conviction sizes the trade — it never gates one

All of the above feeds a single **conviction** multiplier, which maps to a
suggested fraction of normal risk (full / half / quarter). It is deliberately
separate from the technical score, which remains exactly what it was: the weight
of technical evidence.

> **Nothing in this layer withholds a setup.** There is no cap on setups per
> day, no daily loss limit, no stop-for-the-day, no cooldown and no re-entry
> block. The application never executes a trade, so it has no business hiding
> analysis from you. A repeated idea or a losing run is *noted* beside the
> setup; the setup still appears.

### Many setups at once

The setup book tracks up to eight setups simultaneously, in both directions,
admitting any candidate whose side clears the profile's watch score. A live
setup can never stop a new one being found — which it previously could, since a
single tracked slot meant an unresolved WATCH setup silently suppressed
everything behind it.

Duplicates are dropped (an overlapping zone in the same direction is the same
idea re-derived), and the banner shows whichever setup is furthest along its
lifecycle, so a CONFIRMED 72 outranks a DEVELOPING 84.

## The AI layer

With `ANTHROPIC_API_KEY` set, the analyst explains decisions, answers questions
("What exactly am I waiting for?", "Which timeframe is conflicting?"), teaches concepts,
and reads uploaded chart screenshots.

Without a key **the application still works**: every explanation falls back to a
deterministic generator that reads the same facts. The header shows which mode is active.

---

## Backtesting

Replays history through the **whole live pipeline** — analysis, decision, the strategy
library, the setup book and the resolver — on a replay clock, so what is measured is the
application's own behaviour rather than a simplified stand-in for it. Setups are resolved
against the finest timeframe in the data.

**Causality.** Candles are filtered on their *close* time, not their index. MetaTrader
labels a candle with the moment it opened, so at noon the daily candle indexed 00:00 is
still forming and its high, low and close describe a day that is half unwritten. Until
v1.2.0 the replay compared the index against the clock and handed every engine that
forming candle — up to twenty-four hours of future data per step. Backtests produced
before v1.2.0 are invalid and were discarded.

Reported: setups, win rate, average R, total pips, profit factor, max drawdown, streaks,
and breakdowns by strategy, family, exit reason, session, regime, direction and score band.
`unfilled_setups` is reported alongside, because it is the honest measure of how many ideas
never became trades. When nothing triggers, the score distribution and the reasons it
waited are reported, so a quiet result is diagnosable rather than mysterious.

**Read the stride.** One evaluation runs the full multi-timeframe analysis (~2 seconds), so
a long replay widens its step to stay inside the evaluation cap. A wide stride under-fills:
setups arm on a confirmation seen at an evaluation instant, and sampling every third hour
misses most of them. `step` and `unfilled_setups` in the results tell you how much to trust
the fill rate.

**It runs across every core.** That two-second evaluation used to cap a replay at 600
evaluations — about six days of market, which cannot validate 32 detectors across six
sessions and four score bands, because every cell holds single digits. The ceiling is now
20,000, and `goldassist/backtest/parallel.py` splits the history into contiguous chunks,
one per core, to reach it.

The split is exact rather than approximate. A replay is stateful — the setup book carries
setups between steps — so chunks cannot simply be handed out: a chunk beginning at noon
has no memory of the setup identified at eleven, and would both miss that trade and admit
a duplicate of it. Every chunk therefore starts 300 evaluations *before* the range it owns,
purely to build book state, and records only trades whose entry falls inside its own range.
The overlap is wasted work by design; it is the price of the parallelism.

**This was verified, not assumed.** 900 evaluations of real history were replayed twice —
once sequentially, once split in two — and compared field by field: **119 trades against
119, identical** on entry time, strategy, direction, entry price, stop, gross R and net R.

Chunks are balanced by *work*, not by range. Only the first chunk starts at the beginning
of the window, so only the first has no warm-up to pay; splitting 900 evaluations into two
equal halves gives one worker 450 and the other 750, and wall time is set by the slower.
Measured, that came to **0.86× — slower than not forking at all**. The first chunk is now
given a larger range by exactly the warm-up the others pay, so every worker performs
`(S + (N−1)·300) / N` evaluations — 1,853 each for a 4,959-step window across three cores.

Runs shorter than four warm-ups per chunk are not split, for the same reason.

```bash
python -m goldassist.backtest --step 4 --max-evaluations 12000
python -m goldassist.backtest --cache             # replay from saved history
python -m goldassist.backtest --workers 1         # force sequential
```

**`--cache` is worth knowing about.** Every replay used to re-download 137,700 candles and
hold the terminal for the duration, so a replay blocked the live application *and* every
other replay. The first run now saves the frames to `~/.goldassist/backtest-history.pkl`;
`--cache` replays from that file with MetaTrader free, and `--refresh-cache` re-downloads.
Without the cache, close the Gold Assistant window first — MT5 serves one client at a time.
The in-app backtest (`POST /api/backtests`) stays sequential for the same reason.

History depth is bounded by what your MT5 terminal holds. Open each timeframe's chart and
scroll back to make the terminal download more.

### Does the result hold?

A single monotonic score-band table is weaker evidence than it looks: one strong stretch
can drag an average into the right order and leave the impression that the score ranks
setups correctly when it only did so in March. That distinction decides what may be done
with the result — an ordering that holds in every independent period may be calibrated
against; one that appears only in the total may not.

```bash
python -m goldassist.backtest --stability          # most recent run
python -m goldassist.backtest --stability 1 --folds 6
```

It re-reads a stored run's trades and asks the same question of each fold. Nothing is
re-executed, so it is free and repeatable on any stored backtest. Two rules it follows:

- **Sample size sits beside every figure.** Bands with fewer than 8 trades in a fold are
  marked `?` and excluded from the verdict — the 85-100 band swings from −0.17R to +1.58R
  on five to nine trades, and a table of bare averages would invite the wrong conclusion.
- **A weak band is not a losing band.** 65-75 averaged +0.127R overall, which reads like a
  candidate for exclusion until the folds show it positive in *all four*. Raising
  `min_score` to 75 would have discarded +36.6R of real profit. Weak setups belong in the
  sizing layer, not on the cutting-room floor — which is what the application already
  does, and now has evidence for.

  That is the empirical case for the standing rule that **conviction sizes a trade and
  never gates one.**

---

## Data and files

Everything lives in `~/.goldassist/`:

| File | Contents |
|---|---|
| `goldassist.db` | the signal log, setup outcomes, decision history, journal, backtests, alerts, settings |
| `config.json` | MT5 path, symbol, AI model, port |
| `profiles.json` | your strategy profiles |
| `goldassist.log` | application log (rolled to `.log.1` past 8 MB) |
| `calendar.json` | economic releases, generated by `python -m goldassist.analysis.calendar_sync` |
| `backtest-history.pkl` | cached deep history, so a replay need not hold the terminal |

The Gold symbol is auto-detected (`XAUUSD`, `XAUUSDm`, …). Override with
`GOLDASSIST_SYMBOL`; override the terminal path with `GOLDASSIST_MT5_PATH`.

---

## Limitations

- Requires a running, logged-in MT5 terminal for data.
- Backtests use candle ranges, not ticks: when a candle touches both the stop and the
  target, the loss is assumed first.
- Volume is broker tick volume, not exchange volume.
- The economic calendar carries FOMC, CPI and payroll releases, generated by
  `python -m goldassist.analysis.calendar_sync`. PPI, retail sales, GDP and PCE are
  *not* included — their dates are neither rule-derivable nor to hand, and a calendar
  that guesses is worse than one with a known gap. Add them by hand to
  `~/.goldassist/calendar.json`; hand-added entries survive regeneration.

  FOMC and CPI dates are carried as published tables, one year at a time, so the file
  has an end date; payrolls are rule-derived and continue indefinitely. `events.coverage()`
  reports how far the calendar still reaches and warns before it runs out, because
  "no releases due" and "this file expired in December" must never read alike.
- **Backtests #1 and #2 are optimistic and must be re-run.** Until v1.8.0 an armed
  setup that entered its zone without reaching the preferred price filled at the
  bar's low (a short at its high) — the single best price in the bar, which no
  order gets; live fills averaged 0.13R worse than the preferred price. The replay
  also read the economic calendar on the wall clock, so no historical release was
  ever penalised, and trades were carried through the rollover. Every figure below
  predates those fixes. Re-run with `--cache --step 1` before trusting any of it;
  stored runs made before the fix are labelled as such wherever the score record
  quotes them.
- **The calendar has no FOMC or CPI dates before January 2026**, so the first two
  months of the current history window run without a news penalty. The late-2025
  release dates were moved by the US government shutdown and are not guessed.
- **Bid/ask is not modelled on the path.** The candle series is bid; a buy limit
  really fills when the *ask* reaches it, and a short's stop triggers a spread
  earlier. One round-turn spread is charged against every result, which covers
  the cost but not the handful of extra stop-outs and missed fills. Small against
  a 600–1,000-pip stop; not zero.
- **The strategy library has now been backtested once.** 4,959 evaluations over ten
  months (2025-10-31 → 2026-09-04), 738 trades, net profit factor 1.67, and — the result
  that matters — the score band is **monotonic in outcome**: 85-100 averages +0.755R,
  75-85 +0.536R, 65-75 +0.127R, below 65 −0.225R. The confluence engine ranks its own
  setups correctly.

  Cut into four contiguous folds, **75-85 beat 65-75 in 4 of 4** — the ordering is a
  property of the engine, not of one strong stretch. See "Does the result hold?" below.

  That is still one window, one profile, in-sample. It is not a claim about the future
  and not a substitute for walk-forward validation. Treat it as the first evidence.
- **Most setups never fill, and that is real.** 9.2% of identified setups filled at a
  step-4 stride; a step-1 run over the same data moved it only to 12.2%. Price does not
  come back to the zone, or the setup expires waiting. The engine proposes roughly eight
  ideas for every one that trades.
- **The stride was hiding trades, though.** Over the same window, step 1 found 298 trades
  at +0.218R against step 4's 141 at +0.105R — twice the trades *and* twice the average R.
  Arming needs a confirmation seen at an evaluation instant, and an hourly clock does not
  catch a fair sample of them. **Any calibration run must use `--step 1`.**
- **Per-strategy figures are not yet trustworthy.** Only 6 of 26 detectors carry 8 trades
  in both tested windows, and one of those six changes sign between them (VWAP Pullback,
  +0.872R → −0.127R). Imbalance Rebalance, the best detector over ten months at +0.649R,
  comes back at +0.162R over sixty days. Thirty-two detectors splitting 738 trades leaves
  a median of a dozen each. Nothing per-strategy should be tuned on this much data — which
  is exactly what `expectancy.SHRINK_K` already assumes.
- **Conviction weights, day-character thresholds and the pillar weights are reasoned
  rather than fitted.** The measurements feeding them are real — every zone quality,
  sweep depth and structure displacement is computed from live candles — but the
  coefficients that turn a measurement into a score were chosen by judgment, not
  derived from Gold's own behaviour. They have not been walk-forward validated.
- The outcome memory needs closed setups before it can say anything. On a fresh
  install it will correctly report that it does not know.
- Swap/financing is not modelled, and since v1.8.0 does not need to be: every
  filled trade is closed five minutes before the 17:00 New York rollover
  (`flat_before_rollover`), and nothing fills from then until the spread settles an
  hour later. Switch it off per profile to hold through.

---

## Project records

Requirements and development history are kept in [docs/](docs/):

- [docs/REQUIREMENTS.md](docs/REQUIREMENTS.md) — every specification given
- [docs/DEVELOPMENT-LOG.md](docs/DEVELOPMENT-LOG.md) — decisions, bugs found, and why
