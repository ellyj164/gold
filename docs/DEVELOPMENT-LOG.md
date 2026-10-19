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

## Session 4 — the 500-pip day-trading rule

Implemented in `goldassist/signals/opportunity.py` with `PIP_SIZE = 0.01`, so
500 pips is a 5.00 ($5) move, exactly as specified.

> **Convention correction.** This section originally read `PIP_SIZE = 0.10` with
> a 50-pip floor. The price threshold it enforced — a 5.00 move — was already
> right; only the pip convention was. The trader's own convention is 1 pip =
> 0.01, so the same $5 floor is **500 pips**. Every pip figure the application
> prints is now on that convention, and every price-space threshold in the
> strategy detectors was rescaled alongside it so no filter loosened or tightened.

Verified against the specification's own worked example:

```
Entry 4400 → TP1 4405 = 500 pips, TP2 4415 = 1500 pips, TP3 4430 = 3000 pips
Assessment: MAJOR, qualifies = True
"500 pips to the first objective and up to 3000 pips if the structure extends"
```

Eligibility is judged on the **first** objective; the grade reports the **full**
potential, so a setup is never presented as smaller than the structure supports.

The floor is applied at two reference points that answer different questions:
the decision engine measures from spot ("is there a worthwhile move from here?")
and the suggestion from the entry zone ("from where I would actually get in?").

`min_target_pips` is a per-profile setting (default 500.0), so a scalper can
lower it.

**Open observation:** with a 500-pip ($5) floor this engine will rarely reject a
setup on distance alone — Gold's ATR and zone-based entries mean most valid
pullbacks clear $5 comfortably. The floor will bite mainly on breakout and range
setups. If it never fires, raising it to 800–1000 pips ($8–$10) would make it a
real filter rather than a formality. That is a settings change, not a code change.

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


---

## v1.3.0 — the measuring instrument (2026-09-06)

An audit of the whole tree, asked for as "is any of this hard-coded, and what
is missing?". The reassuring half of the answer: **nothing fabricates market
data.** `data/source.py` raises rather than substituting when MT5 has nothing;
`trader/macro.py` and `trader/intermarket.py` both refuse to invent a reading
when a symbol is absent; there is no `random`, no mock, no synthetic candle
anywhere in the tree.

The unreassuring half: every engine reads a *real* measurement and then
converts it to a score through coefficients nobody ever fitted. `_grade_sweep`
is the pattern in miniature — `wick_depth_atr` and `reclaimed_bars` are
measured off live candles, and the `40.0`, `30.0`, `7.0` and `20.0` that turn
them into a number are judgment. The same is true of the seven pillar weights,
every penalty magnitude, RSI 55/45, ADX 20, the session quality table and all
32 `min_conviction` thresholds.

That is not dishonest, and the README always said so. It is unmeasured — and
the reason it stayed unmeasured turned out to be arithmetic, not neglect.

### The instrument was too slow to take a reading

`DEFAULT_MAX_EVALUATIONS` was 600. One evaluation runs the full multi-timeframe
analysis at roughly two seconds, so 600 of them cover about six days of market.
Six days cannot validate 32 detectors across six sessions, four score bands and
two directions: every cell holds single digits, and a single-digit cell is
noise wearing a number. The step was silently widened to fit, which under-fills,
because arming needs a confirmation seen at an evaluation instant.

So the strategy library was never measured, the weights were never fitted, and
the central claim rested on nothing. The database agreed: 3 fired signals, 0
closed outcomes, 0 backtests.

The ceiling is now 20,000 and `backtest/parallel.py` splits the work across
cores to reach it. A replay is stateful — the book carries setups between steps
— so chunks cannot simply be handed out: a chunk starting at noon has no memory
of the setup identified at eleven, and would both miss that trade and admit a
duplicate of it. Every chunk therefore warms up `OVERLAP_STEPS` before the
range it owns and records only trades entering inside it. `engine.run` was
split into `plan` / `replay` / `compile_stats` so the merge happens on raw
trades and raw counters, with nothing averaged yet — a parallel run and a
sequential run of the same history produce the same figures.

The overlap is wasted work by design, so chunks are only created when they are
worth at least four warm-ups. Eight chunks of 300 steps with 300 steps of
overlap each is 2400 wasted evaluations to save 2100 — slower than not forking.

### No transaction costs anywhere

Every R and pip figure, live and backtest, was gross. On this account the round
turn is **26 pips** against a 500-pip first objective and a typical 250-pip
stop: 5% of the target and over 10% of the risk.

The spread is measured, not assumed. MetaTrader records a `spread` column on
every candle it serves, so a backtest pays what the broker actually charged on
those bars; live, the bid/ask is read directly. On XAUUSDm the two agree
exactly — 260 points at a 0.001 point size, 0.26 of price, 26 pips. Commission
and slippage cannot be observed from a feed, so they default to zero and are
reported as unmodelled rather than invented.

Three decisions inside that are worth recording:

- **The round turn is charged once, against the result** — not smeared across
  the fill and the exit. A long buys the ask and sells the bid, a short does
  the reverse, and the candle series is a bid series either way, so the round
  trip is exactly one spread whichever way the trade ran. Charging it this way
  also leaves `fill_price` and `exit_price` meaning what they say.
- **Net R keeps the gross denominator.** Inflating the risk by the round turn
  as well is arithmetically defensible and useless: numerator and denominator
  grow together, a stop-out comes to exactly -1.00R net as well as gross, and
  the cost vanishes from the one number the statistics are built on. The test
  caught this — it was written expecting a deeper loss and got -1.0 vs -1.0.
  Fixed denominator, so a stop-out reads -1.10R, which is what the account
  experienced.
- **The reward/risk gate reads the net ratio**, and there both legs *do* move:
  reward shrinks by the round turn and risk grows by it, because a long enters
  at the ask while its stop sits on the bid. A gross 1.52 is a net 1.29, and a
  gate reading the gross figure passes trades the method is meant to refuse.

Strategies are now ranked on net R. Costs are charged per trade, so a gross
ranking flatters whichever detector takes the most of them.

### The event layer had never once fired

`analysis/events.py` reads `~/.goldassist/calendar.json`, and that file did not
exist — the app directory held only the database and the log. So the news
blackout penalty in `confluence.py` had never fired on this installation, and
setups were being scored straight through FOMC and CPI. Running
`calendar_sync` wrote 44 events.

It also expired silently. FOMC and CPI are carried as published tables, one
year at a time; payrolls are rule-derived and continue forever. Crossing into
an uncovered year would have left the app reporting "no releases due", which
reads as an all-clear. `events.coverage()` now reports how far the calendar
reaches and warns before it runs out.

### Sessions drifted an hour, twice a year

`trader/sessions.py` defined its windows as fixed UTC hours — calibrated for
summer. London and New York change clocks on different dates and Tokyo not at
all, so no fixed offset describes all three. In January the New York equity
open falls at 14:30 UTC while the window meant to name it still sat at
13:30-15:00, and the "US data window" stopped covering the 08:30 Eastern
releases it exists for.

`calendar_sync` had already solved this properly for events; sessions now use
the same `ZoneInfo` mechanism and the two agree by construction. The refactor
is verifiable rather than merely plausible: resolved for an August date it
reproduces the old constants exactly — 23/7/12/16/19/21 — and for January it
shifts each by the hour it should.

### A report that printed almost nothing

`backtest/__main__._report` was reading the outer `{"stats": ..., "trades": ...}`
wrapper as if it were the statistics, so "Trades" printed a list of trade dicts
and every other figure printed nothing at all. The per-strategy table read `n`
and the summariser writes `setups`. Both fixed, and the report now shows gross
and net side by side.

### Added to the standing principles

11. **Cost is part of the measurement.** A gross figure is not what the
    account is paid, and a method whose edge disappears once the broker is
    paid never had one. Measure the spread from the feed; never assume it.
12. **An instrument too slow to take a reading is the finding.** When a
    parameter has gone unvalidated for months, check whether validating it was
    ever actually possible before blaming the parameter.

### Two faults the first real run exposed

Both were invisible to the unit tests and obvious the moment the thing was
pointed at ten months of live Exness data.

**The point size was assumed.** MetaTrader records a candle's spread in
*points*, so converting it to a price needs the symbol's tick size, and
`cost_model_for` defaulted to 0.01. XAUUSDm quotes to three decimals — point
0.001 — so the first run announced a round turn of **240 pips** instead of 24.
The credibility guard did not catch it: 240 pips is implausible for Gold but
sat under the 300-pip ceiling, which is a reminder that a sanity bound wide
enough never to fire on real data is also wide enough to miss a factor of ten.

The fix is to ask the terminal (`symbol_details()["point"]`) where there is one,
and to *infer* the precision from the quotes where there is not — never to
assume. Ten months of M5 now price the round turn at 24 pips and the live
bid/ask at 26, which agree.

**The replay got slower the more history it was given.** `_slice_frames` asked
`df.index + duration <= now`, which allocates a fresh DatetimeIndex and a full
boolean array over every candle of every timeframe on every step. With M5 at
60,000 bars that is hundreds of thousands of comparisons per evaluation, and it
scales with the history — exactly backwards for a tool whose whole purpose is
to replay more of it.

The index is sorted and the duration constant, so the condition rearranges to
`index <= now - duration` and `searchsorted` answers it in log time. Measured
over the real frames: **71.5 ms/step to 1.9 ms/step, a 38x speedup**, with the
candle indices identical across 200 sampled moments. Nothing about causality
changed — the boundary is the same one, found a different way.

### Proving the split, and the split being wrong

The claim the parallel runner rests on — that cutting a stateful replay into
chunks does not change its answer — is not self-evident and was not taken on
faith. 900 evaluations of the real history were replayed twice, once as a
single sequential pass and once cut in two with warm-up, and the trades
compared field by field.

**119 trades against 119, identical** on entry time, strategy, direction, entry
price, stop, gross R and net R. The overlap does what it is there for.

The same run also showed the split was **0.86x — slower than not forking**.
Equal ranges are not equal work: only the first chunk begins at the start of
the window, so only the first has no warm-up to pay. Two equal halves of 900
gave one worker 450 evaluations and the other 750, and wall time is set by the
slower one — a theoretical ceiling of 1.2x before process startup, which the
startup then ate.

Chunks are now balanced by *work* rather than by range: the first chunk is
given a larger own-range, by exactly the warm-up every other chunk must pay, so
each worker performs `(S + (N-1)V) / N` evaluations. For the 4,959-step window
across three workers that is 1,853 apiece against 4,959 sequential — and the
unit test asserts the spread between workers is zero rather than trusting the
arithmetic.

`MIN_CHUNK_OVERLAP_RATIO` would have caught the pathological case anyway: 900
steps is below the 1,200 a three-way split needs to be worth its own warm-up,
so the CLI would have run it sequentially. The test forced the split precisely
to find out what happens when the guard is bypassed.

### A history cache, because MetaTrader serves one client

Every replay re-downloaded 137,700 candles, took a minute over it, and held the
terminal for the duration — which meant a replay blocked the live application
*and* every other replay, so two runs could not be compared without an hour of
serial waiting. `history.save_cache` / `load_cache` write the frames once;
`--cache` replays from the file with the terminal free. Pickle rather than CSV
because the frames are timezone-aware and a round trip that quietly drops a
timezone would corrupt the causality the whole replay depends on.

The point size is asked of the terminal when one is open and inferred from the
quoted precision when not. On the cached run the inference independently
returned 0.001 — the same value `symbol_details()` reports.

### The first valid measurement

4,959 evaluations over ten months of XAUUSD (2025-10-31 to 2026-09-04),
Default XAUUSD profile, step 4, three workers, 86 minutes. Backtest #1 — the
first row in that table that is worth keeping.

```
Trades 738   Win rate 48.0%   Profit factor  1.73 gross / 1.67 net
Average R      +0.369 gross / +0.347 net      Total R  +272.5 / +255.9
Costs paid     17,640 pips = 16.6R, 6.1% of gross profit
Max drawdown   16.81R net    Max consecutive losses  8
```

**The finding that matters is not the profit factor. It is this:**

```
score 85-100   n= 28   avg +0.755R   win 60.7%
score 75-85    n=370   avg +0.536R   win 53.2%
score 65-75    n=321   avg +0.127R   win 42.1%
score below 65 n= 19   avg -0.225R   win 26.3%
```

The score band is monotonic in outcome. Every coefficient behind that number
was chosen by judgment and none had ever been checked; the ordering they
produce turns out to be real. That is the first evidence this project has that
the confluence engine is measuring something rather than merely computing
something.

Secondary reads worth keeping:

- **Positive in 11 of 12 months.** The one negative is August 2026 at -4.74R
  over 74 trades. The edge is not one lucky fortnight.
- **Not concentrated.** The best ten trades are 18% of the profit and the best
  5% are 48% — normal for laddered targets, not a single outlier holding the
  result up.
- **Mean win +1.80R against mean loss -0.99R** at a 48% win rate. Internally
  consistent with TP1 sitting near 1.8R and the stop at 1R, which is what the
  profile asks for, so the arithmetic is not hiding anything.
- **The best regimes are consolidation (+0.590) and potential reversal
  (+0.471), not the trending ones** (+0.205 bullish, +0.222 bearish). Gold rose
  across this window, so a long-bias artefact was the thing to suspect; the
  regime split argues against it.
- **Round Number Reaction is 36% of all trades** (269) at a 39.4% win rate and
  +0.132R. It carries the most volume and the least quality — the first
  candidate for a tighter `min_conviction`.
- **Volatility Squeeze is the only clearly bad detector** (-0.581R over 7
  trades) — and at n=7 the shrinkage in `expectancy.py` would correctly refuse
  to act on it.

### What this result is not

- **In-sample.** One window, one profile, no walk-forward split. The
  coefficients were not fitted to this data, which helps, but they were not
  validated against held-out data either.
- **A 9.2% fill rate.** 738 filled against 7,286 unfilled. That is the step-4
  stride: arming needs a confirmation seen at an evaluation instant, and
  sampling hourly misses most of them. The sample is therefore biased toward
  setups that happened to be visible at the sampled moments, and a step-1 run
  is needed before the fill rate means anything.
- **Still missing slippage.** Spread and commission are charged; slippage is
  not. At 738 trades a single pip per side is ~1,476 pips, about 4.6R.
- **Not a claim about the future.** It is a claim that the engine's own score
  ranks its own setups correctly over one ten-month sample.

### Measured speedup

The equivalence run put sequential replay at 1.69s per evaluation, so this run
would have taken roughly 140 minutes on one core. It took 86 — about **1.6x on
three workers of a four-core machine**, against a theoretical 2.68x. The gap is
the warm-up overhead plus running three workers on four logical cores while the
parent merges. Worth knowing before promising anyone a linear speedup.

### The aggregate was not enough

The monotonic score-band table is the kind of result that should be distrusted
until it is cut up, because one strong stretch dragging an average produces
exactly the same table. `backtest/stability.py` splits a stored run into
contiguous folds and asks the question of each. It re-reads trades a replay
already wrote, so it costs nothing and can be pointed at any stored run:
`python -m goldassist.backtest --stability`.

```
band          fold 1      fold 2      fold 3      fold 4
75-85     n= 92 +0.511  n=101 +0.617  n= 94 +0.688  n= 83 +0.292
65-75     n= 79 +0.214  n= 72 +0.072  n= 80 +0.142  n= 90 +0.082
```

**75-85 beat 65-75 in 4 of 4 folds.** The ordering is a property of the engine,
not of this ten months.

The more useful finding is the one that stops a bad decision. Read as an
aggregate, the 65-75 band's +0.127R looks like a candidate for exclusion, and
the obvious move is to raise `min_score` from 70 to 75. The folds show that
band **positive in all four**: those setups are weaker, not losing, and cutting
them would have discarded +36.6R of real profit over the window.

That is the empirical case for standing principle 5b — conviction sizes a
trade, it never gates one — arrived at from the other direction. The
architecture was already right; now there is evidence rather than an argument.

Two things the module refuses to do, both learned here:

- **Reason from a thin band.** The 85-100 band holds five to nine trades per
  fold and swings from -0.17R to +1.58R. It is printed with a `?` and excluded
  from the verdict. `MIN_FOLD_SAMPLE` is 8 to match `expectancy.MIN_SAMPLE`
  deliberately — the two answer the same question, and disagreeing about it
  would be indefensible.
- **Invent a verdict.** With no fold carrying a usable sample on both sides it
  says "not enough per-fold evidence" rather than comparing noise.

Round Number Reaction, the highest-volume detector, went +0.241 / +0.170 /
+0.184 / **-0.071** across the folds. Three of four positive and the most
recent negative — not enough to condemn it, enough to watch it.

Fold 4 (2026-06-23 onward) is the weakest overall at +0.189R against +0.368,
+0.362 and +0.470 before it. Whether that is regime or noise is not answerable
from one window, and it is the first thing a second window should be asked.

### The honest stride, and what it cost the earlier conclusion

`--last-days` narrows the *evaluated* range without touching the history behind
it, which is what makes a step-1 run affordable — trimming the frames instead
would leave D1 short of the 260 bars every engine needs. 4,049 evaluations at
step 1 over the final 60 days, backtest #2.

**The 9.2% fill rate was not a stride artefact.** Quadrupling the sampling
density moved it to 12.2%. Most setups the engine identifies genuinely never
fill: price does not come back to the zone, or the setup expires waiting. That
is a real property of the method, not a measurement error, and it is the more
useful reading — an engine that proposes eight ideas for every one that trades
is doing something different from one that fills most of what it finds.

**But the stride was hiding trades.** Over the same window:

```
step 4   n=141   netR= +14.82   avg=+0.105R   win 42.6%
step 1   n=298   netR= +65.03   avg=+0.218R   win 41.9%
```

Twice the trades *and* twice the average R. The wide stride was not merely
sampling less, it was sampling worse — arming needs a confirmation seen at an
evaluation instant, and the ones an hourly clock catches are not a fair sample
of the ones a fifteen-minute clock catches. The ten-month step-4 figures should
therefore be read as a floor on trade count, and any future calibration run has
to be step 1.

### The finding that stops the obvious next move

The plan after backtest #1 was to tighten `Round Number Reaction`, the
highest-volume and lowest-quality detector. Comparing the two runs says: not on
this evidence.

```
strategy                     10mo n   avgR    60d n   avgR    
Round Number Reaction           269  +0.132      72  +0.085
Order Block Mitigation           91  +0.411      31  +0.548
Liquidity Sweep Reversal         83  +0.376      73  +0.150
Imbalance Rebalance              81  +0.649      30  +0.162
Rejection At Level               56  +0.256      35  +0.357
VWAP Pullback                    22  +0.872       9  -0.127   SIGN FLIP
```

**Only 6 of 26 detectors carry 8 trades in both windows at all**, and one of
those six changes sign. Imbalance Rebalance — the best strategy in the ten-month
run — drops from +0.649R to +0.162R. Volatility Squeeze, the *worst* in run #1
at -0.581R over 7 trades, comes back +6.10R over 6.

The strategy library is too fragmented to evaluate per-strategy on ten months of
Gold. Thirty-two detectors splitting 738 trades leaves a median of a dozen
trades each, and a dozen trades is a coin flip with a decimal point.

So the two results are not equally trustworthy, and the difference is sample
size, not sophistication:

- **The score-band ordering pools every strategy** — 321 to 370 trades per band
  per fold — and held in 4 of 4 folds. That is a finding.
- **Per-strategy expectancy** rests on 12 trades a detector. That is not, and
  `expectancy.SHRINK_K = 25` was the right call: at n=30 an estimate is already
  pulled to 55% of face value.

Nothing per-strategy gets tuned until there is an order of magnitude more of it.


---

## Session N — the fundamental layer, the intraday auction, and a fill that was too good

Asked for: an audit of the system against a global competitive institution, then
the improvements that audit implied. Risk management explicitly out of scope —
this application produces signals and analysis, it does not execute.

### The audit finding that mattered most

Backtest #1 implies an annualised Sharpe of 4.63. Institutional intraday
systematic strategies in liquid macro run 1.0–2.5. A 4.6 is not a good result;
it is a signal that a measurement is optimistic, and the job was to find which.

Four candidates were identified. One turned out to be a real bug.

### Fills could arrive before the evidence that justified them

`signals/engine.update` arms a setup from the analysis at the caller's clock,
then calls `resolve.apply_bars`, which walks **every unwalked bar** — reaching
back to the previous cycle. Live that window is five seconds and the behaviour
is correct. In a replay it is a full evaluation step, so a setup confirming at
12:00 could be filled on the 11:05 bar, at a price only available *before* the
confirmation existed. By construction that fill is better than anything the
method could have obtained.

`resolve.apply_bars` now passes `may_fill` per bar, false for anything that
closed at or before `armed_at`. Those bars are still walked — they can still
invalidate an idea, and an entered setup still resolves through them — only the
entry is withheld. Live behaviour is unchanged because `apply_price` runs
immediately afterwards with the current quote.

One deliberate concession: a setup carrying `armed=True` with no `armed_at`
(restored from a row written before the column existed) is allowed to fill.
Refusing would turn a missing timestamp into a setup that can never enter and
never resolve. Degrading to the older, looser behaviour is recoverable;
degrading to silence is not.

### The drawdown figure was describing the trade list, not the account

`max_drawdown_r` is a cumulative sum in exit order. That cannot see a period
when three positions were open and all underwater, because none of them had
closed yet. Measured concurrency: **peak 4-5 positions, 23% of in-market time
carrying two or more.**

`backtest/rigour.py` marks the equity curve to market using each trade's stored
MAE. Backtest #1's drawdown is **32.31R, not 16.81R**. Backtest #2's is 24.75R,
not 10.28R. Roughly double in both cases, and the larger figure is the one that
describes the experience.

MAE and MFE are now carried on `BacktestTrade`. The resolver had been tracking
both on every bar since it was written and nothing read them back.

### Significance, tested twice because the trades overlap

Trade-level t on backtest #1 is +6.27, which overstates itself: overlapping
positions are not independent draws. Aggregated to monthly totals the
clustering is removed and t is +4.63 across 11/12 positive months. The edge
survives aggregation, which is the answer worth having. Backtest #2 has only
three months and reports itself as untestable rather than borrowing the
trade-level confidence.

### Slippage turned out not to matter

Reported as a sensitivity table rather than a guessed constant. At 3 pips per
side backtest #1 loses 1.6% of its edge. The stops are wide enough to absorb
it, and knowing that is worth more than a fabricated number would be.

### Walk-forward: the finding that contradicts an earlier conclusion

`backtest/walkforward.py` walks anchored train/test pairs and grades decisions
made only from the training window.

Selecting the better half of the strategy library on training-window expectancy
**beat trading everything in 3 of 3 splits on backtest #1**, by +0.132R per
trade. On backtest #2 it helped in 2 of 3 with one large negative. The earlier
conclusion — that the library is too fragmented to select on — was drawn from
two overlapping windows compared by eye; this measures it properly and the
answer is "mixed, and worth re-testing on more history", not "no".

The score-band ordering held out-of-sample with a rank correlation of +1.00, on
a single testable split. `MIN_SPLITS_FOR_VERDICT` refuses to call that
evidence: one window is an observation, and "held in all 1 splits" is both bad
prose and bad statistics.

### The fundamental layer

`trader/intermarket` carried the note "real yields are Gold's dominant driver
and this read is therefore blind to it". True of MetaTrader, not true of the
world. Four keyless public sources now fill it:

| Source | What it answers |
|---|---|
| FRED | 10y and 5y real yields, breakevens, 5y5y forward, the nominal curve, the broad dollar |
| CFTC | Weekly Commitments of Traders — who is positioned and how crowded |
| Yahoo | COMEX GC=F, GVZ implied volatility, ICE DXY, silver, VIX, oil |
| composed | One directional read with the arithmetic shown |

Live at the time of writing: the 10-year real yield is 2.42%, the **97th
percentile of two years** — the opportunity cost of holding Gold is unusually
high. Speculators are net long 228,124 contracts, 54.9% of open interest, the
**94th percentile of stored history** — crowded long. Neither is available from
any chart package.

The COMEX reference also buys a data-integrity check nothing else could
provide: the broker quote against the front month. The whole application rests
on that one feed being honest and until now nothing verified it.

Three findings from building it, all worth keeping:

- **FRED's CDN resets any connection carrying `Accept: */*`.** The identical
  request without the header is served in under a second. This cost an
  afternoon: the failure presents as `ConnectionResetError` after a
  twenty-second stall and looks exactly like the rate limiting it is not.
  Adding an Accept header back will silently disable the entire macro layer.
- **One bounded request per series beats a multi-series join.** FRED ignores
  the date bound on a multi-series export, so the join runs from 1962 and
  returns 535 KB in seven seconds against 9 KB in one for a bounded single
  series. Seven small requests move an eighth of the bytes.
- **The CFTC file is positional and headerless.** The failure mode of a
  positional parse is a plausible wrong number, so every row is checked against
  the report's own identities — reportable longs must sum, and reportable plus
  non-reportable must equal open interest. A shifted layout is rejected rather
  than believed.

### Confidence must not rise as information falls

The first composed read returned `strength: 1.0` from a single lagging driver:
FRED was unreachable, positioning was the only voter, and unanimity among one
voter was being reported as a unanimous macro picture.

Strength is now the product of **how one-sided** the drivers are and **how many
of them spoke**, scaled against the total weight available. The same read now
returns 0.11 and says so in words. This is the same discipline
`trader/expectancy.shrink` applies to a thin trade sample.

### The macro layer cannot become a directional filter

Standing constraint, and the one most worth stating: this is daily data
informing an intraday decision. At full strength a completely one-sided macro
picture moves conviction by a tenth; on a typical day it is worth two or three
per cent. It can never gate a setup. A bearish macro read still lets a long be
taken every hour of the day, which is the whole point for a trader who takes
many trades and needs to profit from moves in both directions.

### The intraday auction

Three new analysis engines, all cross-timeframe, together 1.2% of a cached
analysis cycle:

**`analysis/vwap.py`** — anchored VWAP with volume-weighted standard-deviation
bands, at the day, session and week anchors. Anchored rather than rolling
because the useful reference is an event: from that moment the average is the
real cost basis of everyone who has participated since. The slope is reported
beside the bands because it decides which of two opposite trades a band touch
justifies — against a flat anchor the touch is a fade, against a sloping one
price rides the band and the same touch is a continuation.

**`analysis/auction.py`** — prior day high/low/close, the Asian range, the
initial balance, range extension, and how much of a typical daily range has
been spent. Six known prices are available at the open rather than discovered
afterwards, and each is tested repeatedly within one session in both
directions.

**`analysis/persistence.py`** — the variance ratio (Lo-MacKinlay, with the
heteroskedasticity-robust z-statistic, because Gold's volatility clusters hard
and the homoskedastic form reads that clustering as trend), the Hurst exponent
by rescaled range, and Kaufman's efficiency ratio. Three unrelated estimators,
because agreement between them is worth far more than any one alone and
disagreement is a warning that none should be leaned on.

`regime.py` already classified the market with ADX and Bollinger width. Those
are heuristics that describe the recent path without ever testing whether it is
distinguishable from a random walk. This does.

It picks the **playbook**, not the direction — which is what lets a trader
profit from any market change rather than only from the ones that suit a fixed
style. On the current sample it reports "mean-reverting, low confidence" with
Hurst and the variance ratio openly disagreeing, which is the honest answer.

### Three new detectors, and a gate that is deliberately not one

`strategies/auction.py` adds `vwap_band_fade`, `vwap_reclaim` and
`ib_extension`.

The first of those was very nearly a silent regression. It was written as
`vwap_reversion` — a key the library **already used**, for an ATR-stretch
strategy in `trend.py`. `REGISTRY[key] = ...` overwrites, `auction` imports
last, so the existing detector would simply have stopped existing, along with
every stored outcome attributed to its key, while the registry count still
looked plausible. `base.strategy` now raises on a duplicate key.

The two are genuinely different instruments for a similar idea: the existing
one fades a stretch measured in ATR from the *indicator* VWAP, the new one
fades an excursion measured in standard deviations from the *anchored* day
VWAP, and only when the anchor is flat. Where they agree the book merges them
into one setup carrying both arguments, which is a better outcome than either
having been deleted in favour of the other.

`vwap_band_fade` and `vwap_reclaim` are opposite trades and both are correct in
different regimes. The obvious design lets `persistence` decide which may fire.
That would be the same mistake the standing rule warns against: the regime
estimate is itself uncertain, it changes within a session, and a detector
silenced by it produces nothing on precisely the days the estimate is wrong.

So the regime **sizes** these candidates and never suppresses them. A reversion
setup in a trending market still appears, with its conviction cut to a quarter
and the reason stated on the card.

### Tests

108 to 143. The five that matter most: a bar closing before `armed_at` cannot
fill; a bar after it fills normally; an unarmed setup can still be invalidated;
a missing `armed_at` degrades to the old behaviour rather than to silence; and
the macro read cannot move conviction by more than a tenth in either direction.


---

## v1.8.0 — the live record disagreed with the backtest (2026-09-12)

Asked for: a check of an external gap analysis of the original specification,
then the improvements still open. The analysis was written against the PDF, so
about half its gaps (entry/stop/target, a net reward/risk gate, score
calibration, capped pillars, the look-ahead fix) had already been closed. What
it did not have was the first live week.

### The finding

| | Backtest #2 (step 1) | Live, 7–11 Sep |
|---|---|---|
| Filled trades | 298 | 18 |
| Win rate | 41.9% | 22% |
| Result | +0.22R per trade | −8.26R total |
| Trades reaching +2R | — | 0 of 18 |

Drawing 18 trades from the backtest 5,000 times puts the live total at the 1st
(backtest #1) and 2nd (backtest #2) percentile. The trades are clustered — four
longs in 23 minutes on the 9th, four shorts after CPI on the 11th — so the true
band is wider and a binomial on the win rate alone gives ~7%. A warning, not a
verdict. Enough to go looking, and what turned up were three ways the replay was
kinder than the market.

### 1. Fills at the best price in the bar

`resolve.step` filled an armed setup that entered its zone without reaching the
preferred price at `min(max(low, zone.low), zone.high)` — the **bar's low** for a
long. That is the one price in the bar no order can count on, and `_fill` then
measured risk from it, shrinking the denominator every R is divided by. Live, the
same rule met a quote rather than a bar: all 21 live fills came in at or worse
than the preferred price, averaging 0.13R worse. The backtest's entire edge is
+0.22R per trade.

`_entry_fill` now gives the preferred price only when the bar reaches it (a limit,
never credited better), and otherwise the first in-zone price the bar offered —
its open if it opened inside, else the zone edge it crossed. Live behaviour for a
quote is unchanged.

### 2. The news clock was the wall clock

`mtf.analyse` called `events.upcoming()` with no `now`. Live that is fine. In a
replay of March it reads September's calendar, so **no historical release was
ever penalised in any backtest**. It now reads the analysis clock — the newest
candle's *close*, not the open MetaTrader labels it with.

It also only looked ahead. CPI printed at 12:30 UTC on 2026-09-11 and four shorts
filled at 12:40, 12:57, 13:11 and 13:25: three losses. `last_high_impact` and a
`news_aftermath` penalty (60 minutes, per profile) cover the hour after.

Not fixed, and said so in the README: the calendar has no FOMC or CPI dates
before January 2026. The late-2025 dates were moved by the government shutdown,
and a guessed calendar is worse than a known gap.

### 3. Trades held through the rollover

`trade_expiry_minutes = 720` from a 16:00 UTC fill is 04:00 the next morning, for
a trader who never holds overnight. Filled trades now close five minutes before
the 17:00 New York rollover, at the open of the bar that crosses the deadline,
and nothing fills from then until an hour after, when the spread has settled.
New York time via `ZoneInfo`, so it moves with daylight saving exactly as the
broker's rollover does; verified 21:55 UTC in January and 20:55 in July.

### Record integrity: filled trades lost on restart

Three of the 21 live fills were `unresolved`. `book.restore` dropped any setup
older than `trade_expiry_minutes`, filled or not, so a trade left running over a
restart lost its result. Filled trades now come back for up to seven days, and
`service._catch_up_restored` walks the M1 history from where the resolver stopped
on the first connected cycle. If the broker's history does not reach back that
far the trade is *not* walked — skipping the gap could turn a stop inside it into
a win — and stays `unresolved`, which is the truth.

### Correlated exposure

Four cards on one move are one bet. `book._mark_exposure` tells each setup how
many filled same-direction trades are on nearby (within 2 ATR or 90 minutes), puts
that on the card's risks, and the desk sizes the banner setup at `1/(1+n)`. Sizing,
not gating — standing principle 5b holds. `exposure_at_fill` is frozen on every
trade and the backtest reports `by_exposure`, so whether stacked trades earn less
is measured rather than assumed.

### Breakeven, as a hypothesis

Three live losers went +1.08R, +1.28R and +1.44R before stopping at −1R. The desk
has always *advised* breakeven at 1R; the record never applied it.
`breakeven_in_record` makes the resolver do it, from the bar after the excursion
(the same bar would assume favourable-first). **Off by default**: it also turns
winners that dip back to entry into scratches, and only a replay can net the two.

### The measurement layer the analysis asked for

- **Evidence per trade.** `Suggestion.features` records each pillar's share of its
  maximum and each penalty, at build and again at arming; `BacktestTrade` carries
  it.
- **`--fit`** (`backtest/fit.py`): L2 logistic regression of "did it pay" on that
  evidence, anchored walk-forward, AUC against the judged score on each test fold.
  Suggested `weight_overrides`, bounded 0.5–1.5, are printed only if the fit wins
  a majority of folds, and never applied. Standard library only.
- **Monte Carlo** in `rigour.py`: 5,000 reshuffles, fixed seed, 50/95/99th
  percentile drawdown and losing streak. Backtest #2's historical 10.3R booked
  drawdown sits against a reshuffled 95th percentile of 21.6R.
- **`--live-check`**: the forward test above, with a stated caveat below 50 live
  trades because clustering narrows the band.
- **`--reserve-holdout` / `--holdout`**: a slice every run stops before, replayable
  once, recorded in settings.
- **Variant count** printed with every run.
- **Score record in every explanation.** `backtest/calibration.py` hands the AI and
  the offline narrative the stored record of the current score band — trades, win
  rate, net R, window — with caveats for thin bands and for runs made before the
  fill fix. The prompt now requires it, as history and never as odds, plus the
  single strongest argument against any BUY or SELL.

### Deliberately not done

- **Bid/ask on the path.** Shifting ask-side triggers by a spread while also
  charging a round turn double-counts it, and doing it properly changes what every
  stored R means. ~3% of a typical stop; documented instead.
- **A definitions appendix** (the analysis's G6). The definitions exist in code;
  writing them out is worth doing but changes no number.
- **Regime hysteresis and exchange volume.** Open.

### Verification — partial, and why

Windows Application Control blocked numpy's DLL for every Python on the machine
during this session, so nothing importing pandas could run: not the suite, not a
backtest, not `build.bat`. What *was* run: the resolver's fill, rollover and
breakeven logic (17 checks, pandas stubbed — `step` never touches it), the events
clock, the weight fit on synthetic data where the true weights are known, Monte
Carlo, the forward check and the score record (21 checks), all passing, plus a
byte-compile of every changed module. `tests/test_realism.py` holds all of it in
the suite's own form, with fixed 2030 dates so no check depends on the hour it runs.

**Before anything else once numpy loads:** run the full suite, then
`python -m goldassist.backtest --cache --step 1 --last-days 60`, then
`--live-check` against the new run. Backtests #1 and #2 are superseded.

### Verified, and the first honest measurement (2026-09-13)

The numpy block was Smart App Control (policy `{0283ac0f…}`), which began
refusing the unsigned `_multiarray_umath` pyd after a policy refresh at 12:59 on
2026-09-12 — the same file ships inside the packaged app, so the installed build
was broken too. The trader turned it off. Full suite then passed once four older
test modules were made independent of the hour (`tests/clock.py` — they had
failed only because the suite ran at 17:19 New York, inside the new rollover
window). The first build failed on a OneDrive-locked `dist\GoldAssistant`; the
retry passed all 82 packaged checks.

Backtest #3 — same 60 days as #2, step 1, the v1.8.0 resolver:

```
            #2 (old fills)    #3 (v1.8.0)
trades          298               248
win rate       41.9%             38.7%
avg net R     +0.218            +0.070
total net R   +65.03            +17.34
PF net          1.37              1.12
trade-level t     —               +0.79  (not significant)
MTM drawdown   24.75R            31.24R
```

About two-thirds of the apparent edge was the fill model. What is left is not
distinguishable from zero on 248 trades. The live week sits at the 3rd percentile
of #3 — closer than the 1st/2nd of #1 and #2, still outside the band.

What the honest run changes:

- **The 65–75 band loses in all four folds** (−0.41, −0.21, −0.16, −0.30R;
  −29.3R over 102 trades) while 75–85 makes +34.4R. The v1.3.0 finding that
  65–75 was "weaker, not losing" and must not be cut came from the optimistic
  fills and does not survive them. Raising `min_score` to 75 is now the leading
  candidate — but these 60 days have now been looked at, so the change has to be
  confirmed on data that was not.
- **TP1 is further than these trades travel.** Median MFE 1.12R; 55% reach 1R,
  39% reach 1.5R, 24% reach 2R, against TP1 averaging ~1.86R.
- **Liquidity Sweep Reversal** is +1.45R gross, −1.01R net over 64 trades — it
  only works before costs, and is 9 of the 18 live trades.
- **Stacked trades** earn +0.049R against +0.076R standalone (54 vs 194 trades);
  in the expected direction, too small a sample to size from.
- **The rollover rule** closed 24 trades at +0.24R average — it is not cutting
  winners.
- **The weight fit** beat the judged score in 1 of 4 folds. The judged weights
  stay; 248 trades is too few to fit seven pillars.
- **Fold 4 (20 Aug – 3 Sep) lost −11.2R**, the same stretch the live week
  continued into.

### The score-band tables were measuring the outcome (2026-09-13)

"Default v2" (min_score 75, Liquidity Sweep Reversal off) was tested against the
default on the 60 days before 2026-07-06, which suggested neither change:

```
                     Default #5     Default v2 #4
trades                  221             111
avg net R             +0.172          +0.208
total net R           +37.97          +23.11
PF net                  1.32            1.40
max drawdown R         24.58            6.90
```

Better per trade and far smaller drawdown, less in total. But v2 still recorded
**36 trades in the 65–75 band** with a minimum score of 75 — and that exposed the
fault. `BacktestTrade.score` was the suggestion's score **at close**. The score is
re-measured every cycle after the fill and drifts with the outcome (losers −1.8 to
−3.8 points, winners ~0). Grouping on it sorts losers into low bands after the
fact. Rebuilt from the conviction and context recorded at arming:

```
                   by closing score           by ENTRY score
#3 Jul–Sep   65-75 −0.287  75-85 +0.285   65-75 +0.094  75-85 +0.056  85+ +0.003
#5 May–Jul   65-75 −0.107  75-85 +0.319   65-75 +0.119  75-85 +0.190  85+ +0.404
```

At entry the judged score barely ranks trades — AUC 0.43–0.58 — and on #3 not at
all. **Every score-band table this project has published, including the v1.3.0
"monotonic in outcome" finding and the stability folds, grouped on the closing
score and overstated the ordering.** The v1.3.0 argument against raising
`min_score` and today's argument for it were both read off that artefact.

Out of sample, the sweep rule failed outright: Liquidity Sweep Reversal made
+19.3R over 48 trades on May–July. Stacked trades, on the other hand, confirmed:
−0.128R against +0.230R standalone (36 vs 185).

Fixed: `Suggestion.entry_score` is frozen at arming; `BacktestTrade.score` and the
live outcome record use it, with `final_score` kept beside it; stored runs carry
`score_basis: "entry"` and the score record warns on runs that do not. "Default v2"
is withdrawn.

**The weight fit, against a fair comparison.** Its earlier loss (1 of 4 folds) was
to the closing score, which carries the drift. Against the entry score, on the 469
trades of #5 then #3 in time order, the fit out-ranked the judged score in **4 of 4**
folds (AUC 0.53–0.65 against 0.43–0.54), and its top third beat its bottom third in
every fold. It raises structure confirmation and liquidity sweeps and lowers the
other five pillars. That is **"Fitted v3"**, rescaled so the maximum stays 100, and
it is being replayed against the default on 2026-03-06 → 2026-05-05 — history
neither the fit nor any decision so far has seen.

### Fitted v3 on unseen history, and what held across three windows

2026-03-05 → 2026-05-03, which neither the fit nor any decision had seen:

```
                     Default #6     Fitted v3 #7
trades                  214             234
avg net R             +0.201          +0.185
total net R           +43.00          +43.28
PF net                  1.38            1.34
t                      +2.12           +2.02
```

A tie. The fit's out-ranking of the judged score on May–September did not carry
to March–May, so by the rule set before the run, **Fitted v3 is withdrawn** and
the judged weights stay. On this window the judged score *did* rank by entry
score (65-75 +0.112, 75-85 +0.318, 85+ +0.326), as it did on May–July and did not
on July–September: two of three, weak and unstable, not a finding to tune on.

Pooling the default over the three honest, non-overlapping windows (#6, #5, #3):

```
                           n      avg net R    total      t
all trades               683       +0.144     +98.31   +2.73
standalone               544       +0.211    +114.62   +3.50
stacked on an open trade 139       −0.117     −16.31   −1.11
```

Two conclusions survive everything this session tried to break:

1. **The default has a small positive edge after costs** on March–September —
   +0.14R per trade, t 2.7 — decaying across the windows (+0.20, +0.17, +0.07)
   and not yet confirmed by the live record (18 trades, −8.26R).
2. **Stacked exposure is where it goes to lose.** Trades filled while a
   same-direction trade was already open nearby returned −0.12R; standalone
   trades +0.21R. The direction held in all three windows. The desk's `1/(1+n)`
   sizing, built before any of this was measured, is the right shape; a trader
   who simply skips stacked setups would have kept +114.6R instead of +98.3R.

### Added to the standing principles

13. **The live record is the acceptance test.** A backtest the live record falls
    outside of is describing a different system, however good its statistics.
14. **A fill is the price on offer, not the best price in the bar.** Any
    simulated execution that would have beaten every live fill is wrong.
15. **Every clock in a replay is the replay's clock.** One wall-clock read left
    a whole layer inert in every backtest ever run.

---

## v1.9.0 — skip stacked trades, date outcomes by their exit (2026-10-01)

### What the live record said (2026-09-07 → 2026-10-01)

51 filled trades, −10.0R. Since v1.8.0 (2026-09-14): 32 trades, −0.76R
(−0.02R/trade), inside the noise of the backtest's +0.07 to +0.20R. Stacked
trades were still the losing group live: −0.18R against +0.18R standalone.

### Built

1. **Stacked setups are skipped, not halved** (`StrategyProfile.skip_stacked`,
   default on). The resolver refuses a fill while a same-direction trade on the
   same move (entries within 2 ATR, or filled within 90 minutes) is open at
   that candle. The setup is retired as untaken with `exit_reason = "skipped"`;
   the desk shows "skip — same move already on" with a zero size. The card
   still shows and alerts. Backtests report `skipped_stacked`.
2. **`setup_outcomes.closed_at` is the exit time.** It was the time the row was
   written, so a trade resolved on the morning after a restart counted in the
   next day's loss count, losing streak and cooldown. Old filled rows are
   redated from `fired_signals.closed_at` on startup (51 rows on the live DB).
3. **Agreement is recorded at the fill** (`confirmations_at_fill`, on the
   suggestion, the outcome row and every backtest trade; grouped as
   `by_confirmations_at_fill`).

### A finding that did not survive

Grouped on the closing confirmation count, 5+ confirmations looked strongly
predictive: +0.27R against +0.02R in all three backtest windows, +0.47R
against −0.53R live. It is the closing-score artefact again. Agreement keeps
arriving after the fill and winners stay open longer: live winners gained 2.6
confirmations after filling, losers 1.1. Reconstructed at the fill from the
alert log, the live split shrinks to +0.07R (n=8) against −0.25R. **No sizing
rule is built on confirmations** until a replay groups on the at-fill count.

### Still open

The skip-on / skip-off replay of the last 60 cached days (Jul 6 → Sep 3) was
stopped by low system memory before producing results. It is the next step.


---

## v1.10.0 — NVIDIA model chain, AI chart read, signal audit (2026-10-19)

### What was asked

An AI that reads the chart's structure with real market intelligence, can be
asked *why* a specific signal fired and whether those facts still hold, and
runs on the free NVIDIA models (build.nvidia.com) with automatic failover
from one model to the next.

### Built

1. **NVIDIA provider as a chain** (`ai/providers.py`, `NvidiaProvider`). The
   model field holds an ordered list; a separate vision list holds the models
   allowed to see an image. Each call walks the list and the first real answer
   wins. A failed model rests: 60 s after a rate limit, 3 min after a timeout
   or server error, 6 h when the catalogue has retired it. A rejected key ends
   the walk at once (every model shares it). Per-model timeout 45 s (75 s with
   an image), 150 s for the whole walk. Nemotron 3 models are sent
   `enable_thinking: false`, without which they can spend the budget thinking
   and return nothing; `<think>` blocks are stripped and an empty answer
   counts as a failure. NVIDIA is now the default provider.
2. **Settings show the chain.** Test probes every text and vision model in
   parallel and lists which answer today, which rest and why. A new key clears
   the health table.
3. **AI chart read** (toolbar button → `/api/ai/chart`). Sends the chart as
   drawn on screen (JPEG, shrunk under NVIDIA's ~180 KB inline limit) *and* the
   engine's facts plus the desk digest. The prompt splits the work: numbers
   only from the facts, shape only from the picture. Top-down D1→M5, then
   scenarios with triggers and invalidation, the engine's view, the risks. No
   vision model → read from the facts, labelled as such; offline → the
   engine's own narrative.
4. **Signal audit** ("Ask AI" on each Signals-tab row and on the live signal
   card → `/api/ai/signal`). Sends what the signal *recorded* — trigger,
   reasons, the agreeing strategies' own evidence, entry rationale, flagged
   risks, status history, result — next to the market now, and asks for each
   fact to be marked Still holds / Changed / Cannot verify. Follow-up
   questions keep the conversation. Offline it shows the signal's own record.
5. `desk_digest` existed and was never called; both new reads now send it.

### Fixed on the way

- `/api/ask`, `/api/teach` and `/api/chart-image` ran the model call on the
  event loop, freezing price updates for up to 90 s. All model calls now run
  in a worker thread.
- `_timeframe_digest` indexed every block directly; a timeframe missing one
  raised KeyError and took the online explanation down. Missing blocks are
  now absent instead.
- Uploaded screenshots are shrunk before upload (NVIDIA rejects large inline
  images). AI text renders headings/bold/lists, escaped first.
- **The check()-style test suites could not fail under pytest**: failures went
  into `FAILURES` and nothing read it. `tests/conftest.py` now fails the test
  that added to it. With it on, all 175 tests pass.

### Not yet verified

The default chain order comes from the 2026-10-18 probe on the server
account. Which models answer — the vision models above all, which had never
been probed — is only known once a key is saved here and Test is pressed.

---

## v1.11.0 — news desk and the full US calendar (2026-10-19)

### What was found

The release-warning layer had never run on this machine: `calendar.json` had
not been generated, so `events.upcoming` reported no calendar and the
8-point news penalty in `confluence` could never fire. Generated with
`calendar_sync` (44 events, FOMC/CPI/payrolls). It only carries those three
families, and 2027's FOMC and CPI dates are not in it yet.

### Built

1. **`goldassist/news/`** — a background desk like the fundamentals thread:
   - `feeds`: ten public RSS feeds (Google News gold phrases, InvestingLive,
     FXStreet, Fed speeches and policy press releases, CNBC, MarketWatch,
     Investing.com). Broad feeds keep only gold-relevant headlines; chart-idea
     posts, rupee jewellery prices and personal-finance columns are excluded
     everywhere. 30 newest per feed, 48 h, one row per story (id = hash of
     the normalised title). Kitco's RSS is gone (404) and Investing.com's
     metals feed has been frozen since August; both left out.
   - `impact`: keyword fallback tagger, labelled "keywords". Another
     economy's data or currency story is low impact unless it names gold or
     the Fed (Canada CPI is not US CPI).
   - Model tagging via `AIAnalyst.classify_headlines` (NVIDIA chain, own
     provider instance so it never races the trader's calls). Strict JSON;
     each item validated, invented ids or values dropped.
   - Alerts: high impact *and* published in the last 90 minutes, once.
2. **`calendar_feed`**: ForexFactory's weekly JSON, US only, medium/high,
   written hourly to `calendar_live.json`. `events.load()` merges it with the
   verified file, one entry per minute (CPI's three rows + "US CPI (October)"
   = one entry, details kept). So PPI, PCE, claims, PMIs now also drive the
   release penalty and warnings. No actuals: the feed has none.
3. `analysis["news"]` (top 15, tagged) → `news_digest` in the analyst facts;
   prompts say it is context, never a signal, and the signal audit checks for
   high-impact headlines since the signal fired.
4. **News tab**: filters (important/high/bullish/bearish/all), who tagged
   (model or keywords), feed health, this week's calendar with forecast /
   previous; toast + tab dot on breaking news.

### Not changed, deliberately

News does not move the score. It has never been tested against outcomes,
and a headline-driven score is exactly the kind of untested lever the live
record has already punished.
