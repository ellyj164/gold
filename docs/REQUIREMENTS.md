# Requirements record

Every specification given for this project, in the order it was received. This
is the reference the implementation is measured against; the running record of
*how* each was satisfied is in [DEVELOPMENT-LOG.md](DEVELOPMENT-LOG.md).

---

## 1. Original specification — "AI-Powered Gold Trading Decision Assistant"

**Source:** `AI-Powered Gold Trading Decision Assistant.pdf` (29 pages, 37 sections),
the only file in the project folder at the start.

### Core intent

A Windows desktop application for analysing and supporting trading decisions on
**XAUUSD only**. Explicitly **not** an automated trading system: it must never
execute, open, modify or close trades. It acts as an intelligent trading analyst
that answers:

> What is Gold doing? Where is price? What evidence supports the current
> direction? What could happen next? What confirms the setup? What invalidates
> it? Should I BUY, SELL, or WAIT?

### Section-by-section requirements

| § | Requirement |
|---|---|
| 3 | XAUUSD exclusively; architecture may allow other instruments later |
| 4 | Not an execution platform. No trades, no order management, no guarantees |
| 5 | Multi-timeframe analysis: D1 (macro), H4 (structure), H1 (trading context), M15 (setup formation), M5 (confirmation) — compared, not analysed independently |
| 6 | Market structure engine: HH/HL/LH/LL, BOS, CHoCH, MSS, swings, continuation, weakening, reversal, range, consolidation, expansion, retracement |
| 7 | Candlestick engine: engulfing, hammer, inverted hammer, shooting star, doji, morning/evening star, inside bar, displacement, rejection, long wicks, momentum, exhaustion, breakout, failed breakout. Patterns must be judged by **location**, not in isolation |
| 8 | Indicators — trend: EMA 20/50/100/200, SMA, MACD, ADX, Supertrend; momentum: RSI, Stochastic, CCI, Williams %R, ROC; volatility: ATR, Bollinger, Keltner; volume: volume, volume MA, OBV, VWAP; other: Fibonacci retracement/extension, pivots, previous day/week high-low, session highs/lows. **Counting indicators must not drive the decision** |
| 9 | Support/resistance with a strength score from: reactions, timeframe, recency, rejection strength, volume, structure, liquidity, distance from price |
| 10 | Supply and demand zones graded on freshness, tests, departure strength, displacement, timeframe, structure and liquidity. Fresh > repeatedly tested |
| 11 | Liquidity: buy-side/sell-side, equal highs/lows, PDH/PDL, session extremes, pools, sweeps, failed breakouts. **A sweep must never be assumed to mean reversal** |
| 12 | Fair Value Gaps with direction, timeframe, range, size, creation time, fill status, tests, and relationship to structure/zones/liquidity |
| 13 | Order Blocks graded on timeframe, displacement origin, structural significance, freshness, mitigation, and FVG/liquidity relationships |
| 14 | Market regime detection before any recommendation: strong bullish/bearish trend, range, consolidation, expansion, retracement, potential reversal |
| 15 | Confluence engine combining all evidence into a setup score |
| 16 | BUY / SELL / WAIT. **WAIT is essential** — the app must never feel forced to produce a trade |
| 17 | AI assistant that interprets and explains, answering "why BUY / why WAIT / what confirms / what invalidates / which timeframe conflicts" |
| 18 | **The AI must not replace the technical engine.** The engine calculates; the AI interprets |
| 19 | Real-time monitoring with update messages when the picture changes |
| 20 | Signal history storing date, time, price, timeframe, regime, direction, score, conditions, explanation, confirmation, invalidation, result |
| 21 | Backtesting: total setups, wins, losses, win rate, average R:R, profit factor, max drawdown, average trade, consecutive wins/losses, best/worst session, best/worst regime |
| 22 | Trading journal recording the trader's own decisions and comparing AI recommendation vs trader decision vs outcome |
| 23 | Chart interface with toggleable layers: candles, EMA, S/R, supply/demand, FVG, order blocks, liquidity, BOS, CHoCH, swings, Fibonacci, VWAP, indicators |
| 24 | Dashboard with price, regime, bias, score, per-timeframe table, decision, main reasons, invalidation |
| 25 | Risk awareness: ATR, volatility, distance to levels, invalidation, targets, approximate R:R, warnings about poor positioning |
| 26 | Economic event awareness — warn about high-impact releases; **never predict their outcome** |
| 27 | Chart screenshot analysis via AI vision |
| 28 | Learning mode explaining the concepts behind the analysis |
| 29 | Personal strategy profile defining the trader's methodology |
| 30 | Architecture: desktop UI, market data layer, technical analysis engine, confluence engine, AI layer, database |
| 31 | Offline technical analysis must keep working when the AI service is unavailable |
| 32 | User-controllable alerts |
| 33 | **Core philosophy: no single indicator controls the decision.** Higher-timeframe direction + location + liquidity event + structure confirmation + price action + momentum = high-quality setup. If they conflict: WAIT |
| 37 | ANALYSE → EXPLAIN → SCORE → CONFIRM → DECIDE, not SIGNAL → CLICK → TRADE |

---

## 2. Signal Suggestion Module

**Received:** after the first build was delivered and audited.

Converts the existing analysis into **actionable intraday trading suggestions**.
Does not execute trades. Must never force a signal when conditions are unclear.

| § | Requirement |
|---|---|
| 1 | Tell the day trader: what price area to watch, what confirmation to wait for, where the setup becomes invalid, what targets exist |
| 2 | Evaluate direction, location, S/R, zones, liquidity, FVG, order blocks, structure, candles, momentum, volatility, MTF alignment, session, distance to opposing levels, reward/risk |
| 3 | BUY / SELL / WAIT. WAIT is used for insufficient, conflicting, poorly located, unconfirmed, poor-R:R, too-risky or unclear conditions — **and is a valid, important result** |
| 4–5 | Structured suggestion: setup quality, current price, preferred zone, entry trigger, invalidation, TP1/TP2/TP3, estimated R:R, plus an explanation of why that area |
| 6 | **Entry zone, not a single price.** Derived from demand, supply, FVG, order block, S/R, liquidity, Fibonacci, structure and volatility |
| 7 | **Entry trigger distinct from price arriving.** May require M5 BOS, CHoCH, rejection, engulfing, retest, momentum or liquidity reclaim — preventing entry merely because price touched a zone |
| 8 | Signal status: DEVELOPING, WATCH, IN ZONE, WAITING FOR CONFIRMATION, CONFIRMED, ACTIVE, WEAKENING, INVALIDATED, COMPLETED |
| 9 | Dynamic updating — the trader always sees the latest state of the setup |
| 10 | Score 0–100 representing confluence strength, **never presented as a win probability**. Weighting configurable and validated by backtesting |
| 11 | Quality categories: 0–39 No Setup, 40–59 Weak, 60–69 Developing, 70–79 Good, 80–89 Strong, 90–100 Very Strong. Configurable minimum score for alerts |
| 12 | Every BUY/SELL carries a clear invalidation, preferably structural rather than an arbitrary number |
| 13 | Multiple targets from previous highs/lows, liquidity, S/R, zones, Fibonacci extensions, session extremes, FVG, structure — each explained |
| 14 | Reward/risk validated against a configured minimum; downgrade or WAIT when insufficient |
| 15 | Pullback signals — recommend waiting for confirmation, not buying because price reached demand |
| 16 | Breakout signals — require close beyond, momentum, retest, structure confirmation. "WAIT FOR RETEST" |
| 17 | Reversal signals — require stronger evidence: sweep → rejection → CHoCH/MSS → FVG/OB → M5 BOS. **Never classify a single rejection candle as a confirmed reversal** |
| 18 | No-trade filter rejecting setups on HTF conflict, mid-range location, proximity to opposing levels, poor R:R, no trigger, extreme volatility, unclear structure, sub-threshold score |
| 19 | Explanation answering: why this direction, why this zone, what confirms, what invalidates, where are targets, what risks remain |
| 20 | **The AI must not invent the entry price.** The engine calculates zone, trigger, invalidation, targets, score and evidence; the AI explains them |
| 21 | Alerts on: new setup, approaching zone, entering zone, confirmation detected, BUY/SELL confirmed, weakening, invalidated, TP1/TP2 reached, completed |
| 22 | Signal history storing date, time, price, direction, setup type, entry zone, trigger, invalidation, targets, score, supporting conditions, outcome |
| 23 | Compact signal card in the interface |
| 24 | IDENTIFY → FIND ZONE → WAIT → CONFIRM → SUGGEST → MONITOR → INVALIDATE/COMPLETE. Never pressure the trader. The strongest output is sometimes "WAIT. No valid setup yet." |

---

## 3. 50-pip minimum opportunity rule

**Received:** after the Signal Suggestion Module was delivered.

Optimises the module for **day trading**, where 50 pips is a strong minimum
target for a valid intraday opportunity.

- **Pip convention:** 50 pips = **5.00** of Gold price movement (1 pip = 0.10).
- **50 pips is the minimum acceptable opportunity, not the maximum target.**

| Move to first objective | Classification |
|---|---|
| under 50 pips | Generally not a worthwhile signal |
| 50–99 pips | Good day-trading opportunity |
| 100–199 pips | Strong opportunity |
| 200+ pips | Major potential intraday opportunity |

Rules stated explicitly:

- **Never cap the target at 50 pips.** If the structure supports a larger move,
  identify and display the larger potential.
- **"50 pips available" must not mean automatic BUY/SELL.** The correct reading is
  *50+ pips of realistic movement available **and** a strong technical setup =
  signal opportunity*. Direction, entry zone, confirmation, invalidation and
  target still come from the technical analysis.
- Prioritise opportunities where the **first realistic technical target** is at
  least 50 pips away.

---

## 4. Interface and delivery requirements

Given during development:

| Requirement |
|---|
| The application must be an **installable Windows desktop app**, not only a script |
| The **signal card must be clearly visible** when a signal is suggested — not hidden behind scrolling |
| A **notification sound must play once** when a signal is ready |
| **No demo, mock, sample or synthetic features.** Everything must be live and real |
| A record of the project conversation must be kept in the project folder |

---

## 5. Trader behaviour requirements

**Received:** after the first evaluation of the finished application, in answer
to "what would make this behave like a real human trading the market?"

The application was, at that point, an accurate analyst and a poor trader. It
produced a well-reasoned suggestion and then went silent at the moment the
trade actually began. These requirements cover what a human does *around* the
analysis.

| Requirement |
|---|
| **Trade management after entry** — breakeven moves, partial exits at the first objective, structure-based trailing, and warnings when a trade has stopped working before the stop is hit |
| **Learning from the trader's own results** — closed setups grouped by setup type, session, regime, score band and day character, read back at decision time |
| **Day-character filter** — a read on whether the market is behaving well enough today for any setup to be worth its score |
| **Session view with macro context** — a persistent daily bias, revised deliberately rather than re-derived each tick, plus dollar context so a move has a "why" attached |

## 6. No limits on setups (correction)

**Received:** during implementation of section 5, on seeing daily caps and loss
limits being added.

> "Not that this system should be free to provide any setup/signal available,
> no issue as long as it is not executing any trade. The setups and signals and
> support it might provide are unlimited as long as they qualify."

**This overrides the discipline enforcement in section 5.** The application does
not execute trades, so it has no business withholding analysis from the trader.

| Rule |
|---|
| **No cap on setups per day.** No maximum, no daily loss limit, no stop-for-the-day, no cooldown after a loss |
| **No suppression of alerts** for any reason other than the score threshold that was always there |
| **No re-entry block.** A repeated idea may be *noted*, never hidden |
| Every setup that qualifies technically is built, scored, alerted and displayed |
| Context may still be shown beside a setup, and may still influence the *suggested size* — but never whether the setup appears |

## 7. Top-down intraday method

**Received:** during the same session.

> "Top-down analysis: use D1 and H4 to find the overall market direction or
> major support and resistance zones. Lower execution: drop down to H1, M30 or
> M15 charts to time your actual intraday entries in the direction of the
> H4/D1 bias. I am intraday, not swing trader. My aim is to make many trades in
> a single day."

| Rule |
|---|
| **D1 and H4 set direction** and mark the major support/resistance zones |
| **H1, M30 and M15 are execution timeframes** where the entry is timed |
| **M30 is a first-class timeframe**, not a gap between H1 and M15 |
| Entries are timed **in the direction of the H4/D1 bias**; a setup against it is counter-bias and must be labelled as such |
| The trader is **intraday, not swing**: the application must support many trades in a single day, and nothing in its design may make that harder |

## 8. The alert must announce the setup, and say where to enter

**Received:** 2026-08-18, after the terminal had run for a day without ever
making a sound.

> "BUT I AM NOT GETTING SOUND COMMUNICATING THE SET UP" …
> "SET SHOWING WHERE I CAN ENTER THE TRADE (THE ENTRY ZONE/PRICE)"

The cause was not a broken audio path. The chime was wired only to `IN ZONE`,
`WAITING FOR CONFIRMATION` and `CONFIRMED`, and no setup had ever left `WATCH` —
every one of the seventeen setups in the database sat there. The alert had
therefore never once had a reason to fire.

| Rule |
|---|
| **A setup appearing announces itself.** `WATCH` chimes, with a tone of its own — it is the state a setup actually lives in, and the trader has to know it exists in order to be waiting at the right price |
| **Every tracked setup announces**, not only the one in the banner. The book holds up to eight; hanging the alert off the banner silenced the other seven |
| **The alert carries the entry, not just the news.** Entry zone, the preferred price inside it, and the distance from spot — plus stop and first target, because an entry without them is not a trade |
| Three distinguishable tones: setup on the board, price in the zone, confirmed |
| `DEVELOPING` stays silent — it is the shape of a setup, not a setup |
| The confirmed alert stays on screen until dismissed; earlier stages clear themselves |
| **The score thresholds are not touched to produce more alerts.** `min_score` 70 came from the backtest distribution; lowering it to make noise would make the confirmed chime mean less |

## 9. A signal must be resolved, and one strategy is not enough

**Received:** 2026-08-20.

> "currently it has weakness of providing signal on the dashboard, it stays
> there even after it has hit profit or loss. note that the signal provided
> should be detected to mark it as hit profit in history or loss, and provide
> another signal based on the availability. another thing, this system should
> apply more trading strategies, indicators, technical analysis skills to
> ensure that each signal which can hit at least 50 pips in profit is captured.
> it should not depend on single trading strategy like trend continuation,
> reversal etc..., it should apply multiple strategies to capture more
> opportunities and alert for the signal available … we aim it to be the number
> one profitable."

Two separate defects, both real.

**The signal never ended.** `COMPLETED` required *every* target to be hit, which
on a three-target setup essentially never happened, so a setup that reached its
first objective stayed on the board as `ACTIVE` indefinitely. Stops were read
from spot on a five-second poll, so a wick through the level between two polls
was never seen. Nothing anywhere recorded which of the two had happened.

**One classifier produced every idea.** `classify.setup_type` picked *one* label
from an if-chain — pullback, breakout, reversal, range, continuation — and the
alternatives were discarded. Whatever else the market was offering at that
moment was invisible by construction.

| Rule |
|---|
| **A setup is resolved, not abandoned.** Reaching the target the profile banks at closes it as a win; the stop closes it as a loss; an unfilled idea that runs out of time is *untaken*, never a loss |
| **Resolution reads candles, not spot.** Every M1 bar since the last check is walked, so a wick that touched a level between polls is found |
| **Inside one bar, the adverse move is assumed first.** A resolver that guesses in its own favour produces a record that cannot be learned from |
| **Nothing counts before the fill**, and a setup only fills once it has *confirmed* — price in the zone and the trigger met. A zone touched by price that never confirmed is not a trade anyone took |
| **A decided setup leaves the board immediately**, with its result in pips and R, which is what frees the slot for the next opportunity |
| **Every strategy runs on every cycle.** Twenty-three detectors across four families — smart money, trend, range, price-action — each answering only whether *its* pattern is present |
| **Two strategies on one trade merge**, they do not duplicate. Agreement is confluence and raises the score by a bounded amount |
| **Conviction and context are scored separately.** A mean-reversion setup is judged on how good an example of itself it is, not on a trend-weighted score it was never trying to satisfy |
| **The 50-pip floor is a gate on the first objective**, and the reward/risk of the objective actually banked must clear the profile's minimum. A 1.1R winner against a 1R loser is a losing method whatever the win rate looks like |
| **A short re-admission window after a setup resolves is deduplication, not a cap.** The identical idea — same strategy, same direction, same area — is not rebuilt on unchanged evidence five seconds later. Any *different* setup, and the same one on improved evidence, is admitted immediately |
| **The backtester replays the real pipeline** — strategies, book, resolver — so what it measures is the application's own behaviour |
