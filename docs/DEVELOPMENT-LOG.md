# Development log

A running record of what was asked, what was built, what went wrong and why each
decision was made. Requirements themselves are in
[REQUIREMENTS.md](REQUIREMENTS.md).

Dates are the working session; the project began 2026-08-17 from a folder
containing nothing but the specification PDF.

---

## Session 1 — building the analysis application

### Decisions taken up front

| Decision | Choice | Reasoning |
|---|---|---|
| Market data | MetaTrader 5 terminal via the `MetaTrader5` Python package | Already installed, real broker data, all five timeframes, live ticks, free. Confirmed against the live Exness account; Gold resolves as `XAUUSDm` |
| AI layer | Claude API (`claude-opus-5`) | Best explanation quality; falls back to deterministic local explanations when unavailable |
| UI | Python core + FastAPI on localhost + HTML/JS terminal in a native pywebview window | Professional chart look, light footprint, also openable in a browser |
| Charting | **Custom canvas renderer**, not a charting library | The layers this app needs are mostly *regions* — supply/demand, FVG, order blocks — which general charting libraries do not draw natively |

### Environment findings

- A plain `pip install pandas` stalled for many minutes on this connection, so the
  virtual environment was created with `--system-site-packages` to reuse the
  system numpy/pandas.
- MetaTrader 5 rejects any `copy_rates` request above roughly **65,535 bars**
  with "Invalid params".
- M5 history is the binding constraint: the terminal holds about 10 months of M5
  against ~2 years of M15/H1.

### Bugs found and fixed

1. **A level above price was labelled "support".** Level roles are now assigned
   from current price — anything above acts as resistance, anything below as
   support — before merging.
2. **numpy scalars leaked into JSON.** Added `goldassist/jsonsafe.py`, applied at
   every API and database boundary.
3. **Trend read "ranging" on clean trends.** The label sequence alone was too
   strict; the last confirmed BOS/MSS is now used as a tiebreaker.
4. **A bearish pattern was listed as bullish evidence.** The confluence engine
   took the highest-weighted candle pattern regardless of direction; it now
   selects the best pattern *in the direction being scored*.
5. **Nonsense reward/risk (0.12:1).** Targets closer than 0.75 ATR are where price
   already is, not somewhere it is travelling to, and are now excluded.
6. **Level strength inflated to a misleading 100/100** by compounding merge
   bonuses. Capped at 92.

### The scoring calibration finding

The first full backtest — 585 evaluations across 10 months — produced **zero
setups**. The maximum score ever reached was **67.3** against a threshold of 70.

The cause was structural, not conservatism: the catalogue split "location" into
three separate buckets (supply/demand 12 + FVG 8 + order block 8 = 28 points)
that rarely fire together, so a genuinely good setup could only ever collect a
fraction of the available points.

**Fix:** collapse them into a single 20-point *location* pillar — best component
carries it, a second agreeing component adds a bounded bonus. Momentum and trend
indicators were likewise merged into one pillar. The catalogue now mirrors the
six forms of evidence §33 of the specification names.

Result: score distribution moved from mean 32.5 / max 67.3 to mean 42.0 / max
82.1, and the engine began producing setups.

> **Lesson recorded for future tuning:** if scoring is ever re-weighted, re-run
> the backtest and read `stats.score_distribution` rather than assuming a
> threshold is reasonable.

### Backtest honesty note

An intermediate run reported 7 setups, 42.9% win rate, **profit factor 7.7**.
That figure was produced *before* two engine bugs were fixed and rested almost
entirely on a single 24.6R outlier. After the fixes the same window gave
**6 setups, 33.3% win rate, profit factor 0.92** — approximately breakeven.

Six setups is not a statistically meaningful sample either way. The earlier
number should not be quoted.

### Two further engine bugs, caught by the test suite

7. **RSI returned 50 on an unbroken run of up-candles.** A zero average loss made
   the ratio undefined and it was being filled with "neutral", hiding the
   strongest momentum readings. Now correctly returns 100.
8. **Swing detection rejected any tied high or low.** Equal highs and equal lows
   are precisely what the liquidity engine exists to find, so double-tops were
   invisible to it. Ties now resolve to the first occurrence.

---

## Session 2 — packaging as an installable desktop app

Built with PyInstaller (one-folder) plus Inno Setup, driven by
`packaging\build.bat`.

One-folder rather than one-file: pandas and numpy make a single-exe build unpack
~120 MB to a temp directory on every launch, costing seconds of startup and
tripping antivirus heuristics.

### Packaging bugs

9. **Startup gave up after 25 seconds** while MetaTrader was still priming
   history — and would have failed on *first* launch specifically, the slowest
   launch there is. The wait is now bounded by whether the server thread is
   alive rather than a fixed guess, and a genuine failure shows a message box
   (a windowed app has no console to print into).

10. **The windowed build crashed on launch** with
    `AttributeError: 'NoneType' object has no attribute 'isatty'`.

    In a `--noconsole` build `sys.stdout` is `None`, and uvicorn's default log
    configuration builds a colourised formatter that calls `sys.stdout.isatty()`.
    Fixed in three places: `log_config=None` on `uvicorn.Config`, a guarded
    `StreamHandler`, and real stream objects installed in the launcher before
    anything imports.

    **Root cause of the miss:** only `GoldAssistant-debug.exe` (which has a real
    console) had been smoke-tested. `tests/test_packaged.py` now tests
    **`GoldAssistant.exe` first and by name**, and `build.bat` fails the build if
    it does not pass.

---

## Session 3 — the Signal Suggestion Module

### Audit result

Measured against the new specification, the existing app satisfied roughly
**40%**. It produced a well-reasoned *decision* but not a *tradeable suggestion*.
Missing entirely: entry zone, lifecycle status, setup-type classification,
quality bands, signal card, staged alerts.

### What was built

`goldassist/signals/` — `entry.py` (zone from overlapping technical areas),
`classify.py` (setup type and trigger), `risk_levels.py` (zone-relative
invalidation and targets), `opportunity.py` (pip sizing), `engine.py`
(suggestion, lifecycle, tracking).

### Bugs found, all with one root cause

The decision engine reasons from **spot price**; a suggestion must reason from
the **entry zone**. Three consequences:

11. **Invalidation landed on the wrong side of the entry**, producing a reported
    reward/risk of **1:19** — a number that would badly mislead a trader.
    Invalidation and targets are now measured from the zone.
12. **Status reported CONFIRMED while price was 15 points from the entry zone** —
    the exact chase §7 warns against. Confirmation now requires price to be in
    the zone.
13. **Status reported ACTIVE on a setup nobody entered**, because spot was
    already past TP1. Targets now count only after price has genuinely traded
    into the zone (`entered` flag).

### Design choices worth recording

- **Location is one pillar, not three** (see Session 1) — the same reasoning
  applies to the entry zone, which takes the best overlapping area rather than
  summing separate ones.
- **A failing setup is explained, not hidden.** When nothing clears the pip
  floor, the card still shows the best available objectives, flagged, with the
  shortfall stated — rather than vanishing with no reason given.
- **Reward/risk is measured from the preferred entry, not spot**, because the
  suggestion is to enter in the zone.

---

## Session 4 — the 50-pip day-trading rule

Implemented in `goldassist/signals/opportunity.py` with `PIP_SIZE = 0.10`, so
50 pips is a 5.00 move, exactly as specified.

Verified against the specification's own worked example:

```
Entry 4400 → TP1 4405 = 50 pips, TP2 4415 = 150 pips, TP3 4430 = 300 pips
Assessment: MAJOR, qualifies = True
"50 pips to the first objective and up to 300 pips if the structure extends"
```

Eligibility is judged on the **first** objective; the grade reports the **full**
potential, so a setup is never presented as smaller than the structure supports.

The floor is applied at two reference points that answer different questions:
the decision engine measures from spot ("is there a worthwhile move from here?")
and the suggestion from the entry zone ("from where I would actually get in?").

`min_target_pips` is a per-profile setting (default 50.0), so a scalper can
lower it.

**Open observation:** with a 50-pip floor this engine will rarely reject a setup
on distance alone — Gold's ATR and zone-based entries mean most valid pullbacks
clear 50 pips comfortably. The floor will bite mainly on breakout and range
setups. If it never fires, raising it to 80–100 would make it a real filter
rather than a formality. That is a settings change, not a code change.

---

## Session 5 — visibility, sound, and removing a testing artefact

### Signal card visibility

**Reported:** the signal card could not be seen.

**Cause:** the card lived in the side panel, which collapses *below* the chart at
narrower window widths, putting it off-screen until scrolled.

**Fix:** a persistent **signal banner** placed outside the tab views, directly
under the tab bar. It is therefore visible from every tab without scrolling, and
shows direction, setup type, status, entry zone, trigger state, invalidation,
TP1 with pips, R:R, score and the opportunity grade. A "details" button jumps to
the full card. A confirmed setup gets a highlighted, briefly pulsing treatment.

### Alert sound

Synthesised with the Web Audio API rather than shipped as an audio file —
nothing extra to bundle, and it works offline in the packaged app. Two rising
notes, distinct from system sounds.

- Played **once per (setup, status)** via a `Set` guard, so a chime never repeats
  while the same setup sits in the same state tick after tick.
- Louder two-tone chime on **CONFIRMED**; a softer one when price enters the zone.
- A 🔔 toggle in the header, persisted to `localStorage`.
- Browsers block audio before user interaction, so the audio context state is
  surfaced on the toggle — a chime that silently never plays would be worse than
  no chime at all.
- The desktop window sets `--autoplay-policy=no-user-gesture-required` for
  WebView2 so the *first* alert — the one that matters most — is not swallowed.

### Removing a testing artefact

**Reported:** a profile named "Sensitive (demo)" was visible in the app, and the
application must contain no demo features.

That profile was created solely to force a setup to appear for visual
verification of the banner, and should never have been left behind. It has been
deleted from `profiles.json`, the active profile reset to **Default XAUUSD**, and
the codebase audited for any demo/mock/sample/simulated language — the only
remaining matches are legitimate HTML input placeholders.

The four profiles the application offers are all real strategies:
Default XAUUSD, Strict Smart Money, Trend Continuation, Reversal Hunter.

---

---

## Session 6 — making it behave like a trader

The application analysed well and traded badly. Six questions a human answers
that no engine here answered:

| Question | Module built |
|---|---|
| Does this obey my top-down method? | `trader/topdown.py` |
| Is the market behaving well enough today? | `trader/character.py` |
| What has already happened this session? | `trader/awareness.py` |
| What is my view, and is it still intact? | `trader/thesis.py` |
| Has this kind of setup worked for me before? | `trader/memory.py` |
| What do I do now that I am in? | `trader/management.py` |
| Why is Gold moving? | `trader/macro.py` |

`trader/desk.py` composes all seven into one conviction figure and a suggested
position size.

### The correction that reshaped the session

The first implementation included a discipline layer: maximum setups per day,
a daily loss limit in R, a stop after consecutive losses, a cooldown after a
loss, and a re-entry block. It was wrong, and the instruction that corrected it
is worth recording in full:

> "The setups and signals and support it might provide are unlimited as long as
> they qualify."

The reasoning is sound and it is now a standing principle: **the application
does not execute trades, so it has no business withholding analysis.** A cap
protects a trader from their own hand on the mouse; there is no hand on the
mouse here. All of it was deleted — `trader/discipline.py` removed entirely,
`allow_new_setups`/`allow_alerts` gating stripped from the service, and the six
limit fields removed from `StrategyProfile`.

What survived is `trader/awareness.py`, which computes the same counters and
presents them as **observations** rather than restrictions. "Four losses in a
row today" is useful to know before sizing the next trade. It is not a reason
to hide the next trade.

`tests/test_trader.py` now pins this: it asserts that no gating field
(`allow_new_setups`, `allow_alerts`, `restrictions`, `blocked`) exists anywhere
in the published state, so the enforcement cannot come back by accident.

### The single-setup bottleneck

The stated aim is **many trades in a single day**. The service tracked exactly
one suggestion at a time, and a live non-terminal suggestion *prevented a new
one from being created*. A WATCH setup at 4380 could silently suppress a
confirmed setup at 4395 for the rest of the morning.

> **The cap on trades per day was never a rule anyone wrote down — it was an
> accident of the data structure.** This is the more instructive of the two
> limits found this session, because nothing in the code announced it.

`signals/book.py` replaces the single slot with a `SuggestionBook` that tracks
up to eight setups at once, in both directions, admitting a candidate whenever
its side clears the profile's watch score. Two things stop it becoming noise:

- **Deduplication** — a candidate overlapping a live setup in the same
  direction and of the same type is the same idea re-derived, not a second
  opportunity.
- **Attention ordering** — the banner shows the setup furthest along the
  lifecycle, then the highest score, so a CONFIRMED 72 outranks a DEVELOPING
  84. The confirmed one is the trade that is actually available.

`TradingDesk` manages a trade per entered setup for the same reason: an intraday
trader can easily have two positions running at once.

### M30 as a first-class timeframe

M30 was named as an execution timeframe, so it was added to `TIMEFRAMES` and
then to the six engines that carry their own timeframe weighting tables or scan
tuples — liquidity, order blocks, zones, confluence, entry and classify. A
timeframe missing from those falls back to a default weight and is skipped by
the scans, which would have made M30 a second-class citizen in exactly the
engines that matter for timing an entry.

`config.py` now also names `BIAS_TIMEFRAMES` and `EXECUTION_TIMEFRAMES`
explicitly, so the method is stated in one place rather than implied by six
weighting tables.

### Design choices worth recording

- **Conviction sizes the trade; it never gates one.** The technical score
  remains the weight of technical evidence exactly as before. Conviction is a
  separate multiplier beside it, and the only thing it drives is a suggested
  fraction of normal risk (full / half / quarter).
- **The memory refuses to draw conclusions from small samples.** Below eight
  closed setups in a group it says so rather than rounding three trades up into
  a trend, and every line it does produce states its own `n`.
- **A counter-bias setup is labelled, not blocked.** Intraday reversals off a
  major H4 zone are a real trade, and the Reversal Hunter profile exists to find
  them. The trader should know they are taking one, not discover it afterwards.
- **The session view is deliberately sticky.** It is revised only when the
  higher timeframes have disagreed for at least `thesis_revision_minutes` *and*
  an H4/H1 structure break has confirmed the new direction. Disagreement below
  that bar is shown as a *challenge*, because "my view is being tested" is
  itself information.
- **Outcomes are recorded automatically.** `setup_outcomes` is written when a
  tracked setup terminates, so the learning layer fills up whether or not the
  trader keeps a journal. Without this write half, the memory module would have
  had nothing to read.

### Bugs found and fixed

14. **`thesis.review()` measured elapsed time against the wall clock** while
    accepting an explicit `moment` argument, so the revision rule could never
    be exercised deterministically — and a replay would have measured the wrong
    interval. Elapsed time is now measured against the moment under review.
15. **The chart endpoint validated timeframes against a hardcoded tuple**
    rather than `TIMEFRAMES`, so M30 charts returned HTTP 400 while M30 data
    was being analysed perfectly well. It now validates against the one list.

### Verified live

Against the real Exness terminal (`XAUUSDm`), 2026-08-17 21:53 UTC:

```
timeframes primed : D1 H4 H1 M30 M15 M5   (M30: 1999 bars)
top-down          : with bias | D1/H4 bullish, timing on M30
day character     : STAND ASIDE 31.3  (efficiency 0.12, 6 structure flips,
                                       session quality 0.10 — rollover)
dollar            : DXYm resolved; -0.03% today, gold +1.05% — textbook inverse
conviction        : 0.45 -> quarter risk
```

The dollar index resolved to the broker's real `DXYm` rather than the EURUSD
fallback. The STAND ASIDE read at 21:53 UTC is correct — that is the rollover
hour — and it correctly did **not** suppress anything.

## Session 7 — the alert that never fired

### Three instances, one MT5

**Reported:** the desktop app opened at `http://127.0.0.1:64593/` instead of the
usual address.

Three copies of `GoldAssistant.exe` were running. `_free_port` falls back to an
OS-chosen port when 8765 is taken, silently, so every launch after the first
landed somewhere random. Two of the three had been started from the Start Menu's
*"open in browser"* shortcut, which passes `--browser` — that, not a broken
webview, is why the native window "stopped opening".

The real damage was not cosmetic: MT5 serves one IPC client at a time, so the
three instances knocked each other off the terminal and the feed logged
`-10001: IPC send failed` for every timeframe, every few seconds. The charts on
screen were stale. Stopping the duplicates restored a clean feed.

### The alert had never once had a reason to fire

**Reported:** no sound was ever heard announcing a setup.

**Cause:** not the audio path. `announce()` was wired to `IN ZONE`,
`WAITING FOR CONFIRMATION` and `CONFIRMED` — and **no setup had ever left
`WATCH`**. All seventeen setups in the database, across every session, sat in
that one state. `_status_for` only advances once price trades *into* the entry
zone, and price had not come back to a zone while the app was running.

A second, independent defect: `announce()` was called from `renderBanner()`,
which receives only the leading setup. The `SuggestionBook` tracks up to eight,
so the other seven could reach CONFIRMED in silence.

**Fix:**

- `WATCH` now announces, with a tone of its own — a soft double-tap on one note.
  It is the state a setup actually lives in.
- `announceAll()` walks the primary suggestion *and* every live setup in the
  book, deduplicated by id, on every update path.
- Three distinguishable tones: 466/466 for a setup appearing, 520→660 for price
  reaching the zone, 660→990 and louder for confirmation.
- An **alert toast** carries what the chime cannot: the entry zone, the preferred
  price inside it in the largest type on the card, how far spot is from it in
  both dollars and ATR, and the stop and first target. Clicking it opens the full
  signal card. A confirmed alert stays until dismissed; earlier stages clear
  after 20s.
- The desktop notification gained the same entry information.

Thresholds were deliberately **not** touched. `min_score` 70 came from the
backtest score distribution; lowering it to generate more alerts would have made
the confirmed chime mean less.

**Verified** against live data with a setup on the board: WATCH, BUY Bullish
Continuation 60/100, entry zone 4382.10–4385.73, preferred 4383.37, spot 7.95
above it — the toast rendered exactly that.

---

---

## 2026-08-20 — Signal resolution, and a strategy library instead of a classifier

Two defects, reported together, with one shared cause: the pipeline had a
beginning and a middle but no end, and it had one idea per direction where a
desk has several.

### The signal never ended

`COMPLETED` required every target to be hit. On a three-target setup that
essentially never happens, so a signal that reached TP1 stayed on the dashboard
as `ACTIVE` for the rest of the session. Stops were read from `analysis["price"]`
on a five-second poll, so a wick through the level between two polls was simply
never seen, and nothing anywhere recorded which of profit or loss had arrived.

`signals/resolve.py` is the fix. Fills, targets and stops are decided by walking
every M1 candle since the last check -- `MarketFeed` now carries a *tracking
timeframe* that no analysis engine sees, because M1 is noise to a structure
engine and the only honest granularity for a resolver. The live quote is applied
on top as a partial bar so an outcome is never a minute late. Where one bar
contains both the stop and the target, the stop is assumed: a resolver that
guesses in its own favour builds a record that flatters the strategy and cannot
be learned from.

Three decisions inside that are worth keeping:

* **Arming.** A setup fills only after it has *confirmed* -- price in the zone
  and the trigger met. The old `entered` flag was set by price touching the zone,
  which counted trades the method explicitly forbids taking. Arming reads
  `visited` rather than `price_inside`, so a candle that wicked into the zone and
  left still arms the setup; judging it on spot alone worked at a five-second
  poll and nowhere else.
* **A filled trade is immune to the engine changing its mind.** Direction flips
  used to retire live setups, producing outcome records that were neither wins
  nor losses. Now only the stop, the target or time closes a filled trade.
* **Untaken is not a loss.** An idea that expired or invalidated before it filled
  is recorded as untaken. Counting those as losses would make the strategy
  record meaningless -- and there are many more of them than there are trades.

### One classifier became twenty-three detectors

`classify.setup_type` picked *one* label from an if-chain and threw the rest
away, so at most two setups (one per direction) could ever exist. `strategies/`
replaces that with a registry: every detector runs every cycle and answers only
whether its own pattern is present. Smart money (zone reaction, order block
mitigation, imbalance fill, BOS retest, sweep reversal, draw on liquidity),
trend (the original confluence read, EMA ribbon, Supertrend, VWAP pullback and
reversion, momentum expansion), range (edge fade, range and Asian-range breaks,
squeeze, pivots) and price action (divergence, double top/bottom, round numbers,
Fibonacci pocket, rejection at level, previous-day extremes).

The scoring change that makes the library work: **conviction and context are
kept apart**. A strategy scores how good an example of *itself* a setup is; the
confluence engine scores the direction. The blend weights context by how much
the strategy depends on the higher timeframes agreeing -- 0.50 for a
trend-following strategy, 0.25 for one deliberately fading the move. Feeding a
sweep reversal a trend-weighted score, as the old single-score design did, buried
every counter-trend idea the market offered. On synthetic-but-realistic data the
confluence read alone scored 23/100 and produced nothing; the library found
eight candidates and admitted three, all above 1.5R.

Two guards keep the extra volume honest rather than noisy. Reward/risk is now a
**gate**: the objective the profile actually banks at must clear
`min_reward_risk`, because a 1.1R winner against a 1R loser loses money whatever
the win rate looks like. And a strategy-supplied stop closer than 0.25x ATR to
the entry is widened -- an 11-pip stop on a market with a 16.00 ATR is not a
stop, it is a donation.

Agreement merges instead of duplicating: two detectors pointing at the same area
in the same direction produce one setup carrying both arguments and a bounded
score boost. That is what confluence means, and it stops the board filling with
five names for one trade.

### The backtester now replays the application

Previously it entered at spot the instant the decision engine said BUY, testing
neither entry zones, nor triggers, nor the lifecycle, nor management -- and with
no deduplication, one persistent signal became twenty separate losses. It now
drives the real `SuggestionBook` on a replay clock (`now` is threaded through
`book.sync` -> `engine.update` -> `resolve`), so a backtest trade is a trade the
application would actually have shown. `unfilled_setups` is reported alongside
the results, because it is the honest measure of how much a wide replay stride
distorts the fill rate.

**Known cost, unchanged by this work:** `mtf.analyse` takes ~2.2s per step, which
is what forces the evaluation cap to widen the stride. The strategy library and
resolution together add ~0.04s per step. If replay density ever matters more than
it does today, the profile points at `liquidity.detect_sweeps` and
`structure.detect_events` boxing timestamps row by row.

### Interface

The board now has two halves. Live setups carry the strategy that found them and
who agrees with it; a *Resolved today* panel carries the ones price has decided,
with a session tape of W/L, pips and R. The history tab leads with setup results
and a per-strategy record rather than a list of decisions. Results announce
themselves with their own tones -- a rising three-note for a target banked, a
falling pair for a stop -- because the outcome is the one moment the setup is no
longer anywhere on the live board.

---

## Standing principles for this project

1. **The engine measures; the AI explains.** The AI never invents a price, a
   level or a signal.
2. **WAIT is a first-class answer.** Never manufacture a setup.
3. **A score is the weight of technical evidence, never a probability of profit.**
4. **The application never trades.** No order placement, ever.
5. **No demo, mock or synthetic data.** Everything live and real.
5b. **Never withhold a qualifying setup.** The application does not
   execute trades, so it has no business capping, blocking or hiding
   analysis. Context may size a trade; it may never gate one.
6. **Test the artefact the user actually runs** — the windowed binary, not the
   convenient one.
7. **When a number looks impressive, check what produced it** before reporting it.

---

## v1.2.0 — measurement, participation, and pricing the entry (2026-08-25)

Seven pieces of work, driven by an audit that put the project at 55/100:
strong engineering (~80), weak validation (~30). The engineering was ahead of
the evidence, and nothing here adds a feature that does not either close that
gap or fill a genuine blind spot.

### The look-ahead fault, and why the obvious fix was wrong

The backtester sliced history with `df.index <= now`. The first diagnosis was
that this should be `<`. **That is also wrong**, and the distinction matters
more than the bug: MetaTrader labels a candle with the moment it *opened*, so
at 12:00 the D1 candle indexed 00:00 passes both tests while containing
twelve hours that have not happened. The test has to be on the candle's
*close* time — `index + duration <= now` — and `config.TIMEFRAME_MINUTES` now
carries the durations. The same fault, in a sharper form, let the resolver
fill on a bar that had not begun.

Every backtest stored before this is invalid.

### What the analysis could not see

- **Participation.** Every location engine read price *shape* — a swing, a
  zone, an imbalance, a block. None read volume. `analysis/volumeprofile.py`
  adds volume-by-price, the POC, the value area, high/low-volume nodes and a
  path-obstruction check, and feeds the 20-point location pillar.
- **Why Gold is moving.** `macro.py` reads the dollar and stops.
  `trader/intermarket.py` adds the gold/silver ratio, a nominal yield proxy
  and an equity read, which is what separates a *monetary* Gold move (silver
  leads, the ratio falls, it trends) from a *haven* one (silver lags, the
  ratio rises, it retraces hard). The two are identical on a Gold chart.
- **Whether the price is any good.** `analysis/arrays.py` adds the dealing
  range, premium/discount/equilibrium, the 62–79% optimal-entry band, and
  breaker blocks — an order block that failed and flipped sign.
- **Releases.** `analysis/events.py` had always read a calendar file that was
  never written, so the entire event layer had been inert since it shipped.
  `analysis/calendar_sync.py` generates it from published FOMC and CPI dates
  and the first-Friday payroll rule, converting Eastern to UTC through the tz
  database so the DST shift is right.

### Pricing an entry on the timeframe it is timed on

The first version of the premium/discount penalty read H4 and H1. That is a
swing-scale answer to an intraday question, and it was wrong twice over:

- This method times entries on H1, M30 and M15, so the range an entry is
  priced against is the range of the timeframe it was timed on. Judging an
  M15 entry against the H4 swing range marks half of every trending day as
  "premium" and penalises exactly the intraday continuations the application
  exists to find.
- H4 disagreement is *already* charged by `htf_conflict`. Reading it here too
  billed one disagreement twice.

And a second-order fault the fix exposed: a market trending up lives in the
upper half of its own range — that is what an uptrend *is*. On the finest
execution timeframe that is close to permanent, so the penalty would have
taxed every continuation. Premium/discount is a mean-reversion idea and now
applies only where it means something: a range, or a counter-trend entry at a
bad price. A trending execution timeframe is exempt.

### The record was measuring the wrong rows

`trader/expectancy.py` is now the single owner of what the record says.

- **`entered = 1` is not the test; `fill_price IS NOT NULL` is.** Two
  pre-migration rows carry `entered = 1` with no fill, no exit and +3.5R
  each, and they were the only reason the stored total ever read positive.
- **Duplicates were counted as trades.** The double-instance bug wrote every
  setup twice for two days. Dedup is by strategy, direction and price *within
  twenty minutes* — the bug's copies land seconds apart, a genuine re-entry
  at the same level comes hours later and must survive.
- **Sample size was ignored.** Estimates are shrunk toward "no edge" by
  `n/(n+25)` instead of switching on at a cliff of eight trades.
- **Silent and never-filled detectors are different failures.** Thirteen have
  never produced a setup (broken or mis-calibrated); ten produce setups that
  never fill (a zone-placement problem). Same symptom, opposite fixes.

Neither is disabled. Standing principle 5b holds: context sizes a setup and
never hides one.

Clean live record: **25 trades, −2.52R, 36% wins**, and about **512 trades**
needed to tell that average from zero.

### Conviction was saturated

Additive-and-clamped, so with 32 detectors agreeing it pinned at exactly
100.0 on a third of all setups and could no longer rank them — measured, the
better-than-median half returned −0.001R against −0.046R for the rest.
Confirmations now take a share of the *remaining headroom*, and a detector's
own read seeds at 96. Gates are checked before the compression, so nothing is
withheld by it.

### A test suite that assumes detectors are broken

`tests/test_analysis_v12.py` gives every new detector a purpose-built series
and asserts it is found. This is not ceremony — three detectors have shipped
silent in this codebase already. Writing it caught two more:

- the breaker detector demanded four swings when the pattern is three, and
  was therefore silent on every series containing exactly one breaker;
- low-volume nodes reported distribution *tails* (price never went there) as
  gaps (price went through quickly) — opposite meanings for a target sitting
  beyond one.

### Also

- `python -m goldassist.backtest` finally calls `history.load`, the deep
  loader that was written, exported and never invoked. It refuses to run
  while GoldAssistant.exe holds the MT5 connection.
- `build.bat` runs the packaged smoke test *before* the installer. It was
  after, and the no-Inno early exit took it with it, so the one gate that
  catches windowed-build-only failures never ran on the machine that builds.
- Version read from `goldassist.__version__` instead of typed in four places.
- The project has git history for the first time.

### Added to the standing principles

8. **A new detector that never fires is the default outcome, not the
   exception.** Prove each one fires on a purpose-built series before
   believing it works.
9. **Sample size is part of the measurement.** An average over three trades
   is noise with a decimal point; shrink every estimate toward "no edge" in
   proportion to the evidence behind it.
10. **Price an entry against the timeframe it was timed on.** This method is
   intraday; a swing-scale range must not veto an intraday trade.
