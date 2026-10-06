"""The live analysis service.

Owns the background loop that keeps the analysis current: refresh candles ->
re-run the engines -> re-decide -> diff against the previous decision -> emit
alerts and record the signal. Everything the UI shows is read from the state
this service maintains.

Kept separate from the HTTP layer so the loop can be driven from a script or a
test without starting a web server.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from datetime import datetime, timezone

from ..ai import AIAnalyst
from ..analysis import indicators, micro, mtf
from ..config import AppConfig
from ..costs import CostModel
from ..data import DataUnavailable, MarketFeed, MT5Source
from ..decision import engine as decision_engine
from ..decision import profile as profile_module
from ..jsonsafe import safe
from ..storage import Database
from ..trader import TradingDesk
from ..fundamentals import GoldDrivers
from .. import signals

log = logging.getLogger(__name__)

# How long a measured spread is reused before the terminal is asked again.
# The spread moves tick to tick; a setup should be priced against what it
# normally is rather than whatever it was at the instant the setup was built.
COST_REFRESH_SECONDS = 300

# Most M1 candles fetched to resolve a trade resumed after a restart: seven
# days, matching ``book.RESTORE_FILLED_DAYS`` and well inside MetaTrader's
# limit of roughly 65,000 bars per request.
CATCH_UP_MAX_BARS = 7 * 1440


def _epoch(stamp) -> int:
    """Seconds since the epoch for an ISO stamp, or 0 when it cannot be read."""
    if not stamp:
        return 0
    try:
        moment = datetime.fromisoformat(str(stamp))
    except ValueError:
        return 0
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return int(moment.timestamp())


class AnalysisService:
    def __init__(self, config: AppConfig | None = None):
        self.config = config or AppConfig.load()
        self.source = MT5Source(self.config.mt5)
        self.feed = MarketFeed(self.source)
        self.db = Database()
        self.ai = AIAnalyst(self.config.ai)

        self.profile = profile_module.get(self.db.get_setting("active_profile"))
        self.analysis: dict | None = None
        self.decision: dict | None = None
        self.explanation: dict | None = None
        # Every setup currently in play. Carried across ticks so the trader
        # sees setups evolve rather than a fresh opinion each cycle, and held
        # as a book rather than a single slot so a live setup can never stop a
        # new one being found.
        self.book = signals.SuggestionBook()
        # The behavioural layer: session, day character, the standing view,
        # trade management and the trader's own record.
        self.desk = TradingDesk(self.db, self.source)
        self.trader: dict = {}
        # Why Gold is priced where it is: real yields, positioning, implied
        # volatility and the COMEX reference. Refreshes on its own daemon
        # thread -- these are network calls against daily series and must never
        # sit on the five-second analysis cycle.
        self.drivers = GoldDrivers()
        self.updates: deque[dict] = deque(maxlen=100)
        self.status = {
            "connected": False,
            "symbol": None,
            "last_update": None,
            "error": None,
            "ai_available": False,
            "ai_note": None,
        }

        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._subscribers: list = []
        self._last_signal_id: int | None = None
        self._last_recorded_decision: str | None = None
        # The trader's own per-strategy record, re-read when a setup closes
        # rather than on every tick: it changes only when one does.
        self._edge: dict = {}
        self._edge_dirty = True
        # What the broker takes out of a trade, measured from the live quote.
        # Re-read on a slow cadence rather than every cycle: the spread moves
        # constantly, but a setup should be priced against what the spread
        # normally is, not against whatever it happened to be at the instant
        # the setup was built.
        self._costs = None
        self._costs_at: datetime | None = None
        # Consecutive failed cycles, reset by the first successful one.
        self._reconnect_attempts = 0
        # Filled trades resumed from the previous run, whose missed candles
        # have not been walked yet. Drained on the first cycle with a terminal.
        self._catch_up: set[str] = set()

        self._resume_book()

    def health(self) -> dict:
        """Is this thing actually working? One answer, checked not assumed.

        Every item here is something that has silently failed at least once in
        this project's history: the calendar file that never existed so the
        news blackout never fired; the yield symbol this broker does not carry,
        leaving the intermarket read blind to Gold's dominant driver; the feed
        that failed on every timeframe while the header said "connected".

        A check reports what it found, not what it hoped. ``ok`` False with a
        stated reason is a useful answer; a missing check is not.
        """
        from ..analysis import events

        checks: list[dict] = []

        def add(name: str, ok: bool, detail: str, critical: bool = False) -> None:
            checks.append({"check": name, "ok": bool(ok), "detail": detail,
                           "critical": critical})

        add("MetaTrader connection", bool(self.status.get("connected")),
            f"{self.source.symbol or 'no symbol'} — "
            + (self.status.get("error") or "feed healthy"),
            critical=True)

        frames = self.feed.frames() if self.feed else {}
        thin = [tf for tf, df in frames.items() if len(df) < 250]
        add("Candle history", bool(frames) and not thin,
            (f"{len(frames)} timeframes cached"
             + (f"; thin: {', '.join(thin)}" if thin else "; all deep enough"))
            if frames else "no candles cached",
            critical=True)

        costs = self.costs
        add("Transaction costs", costs.available, costs.headline())

        upcoming = events.upcoming()
        coverage = upcoming.get("coverage") or {}
        add("Economic calendar", bool(upcoming.get("available"))
            and not coverage.get("stale"),
            coverage.get("warning")
            or f"reaches {str(coverage.get('last_event'))[:10]}")

        dollar = getattr(self.desk, "dollar", None)
        symbol = getattr(dollar, "_symbol", None) if dollar else None
        add("Dollar context", bool(symbol),
            f"reading {symbol}" if symbol else "no dollar index on this broker")

        cross = getattr(self.desk, "intermarket", None)
        resolved = getattr(cross, "_symbols", {}) if cross else {}
        missing = [k for k, v in resolved.items() if not v]
        add("Intermarket context", bool(resolved) and not missing,
            ("all resolved" if resolved and not missing
             else f"missing: {', '.join(missing)}" if missing
             else "not resolved yet"))

        # "online" is the wrong word for an analyst that is unavailable, and
        # `last_error` is None when offline mode was *chosen* rather than
        # failed -- so the two together were reporting a deliberate offline
        # setting as a warning that said "online".
        add("AI analyst", self.ai.available,
            self.ai.last_error
            or ("Claude analyst online" if self.ai.available
                else "offline explanations (deterministic, no API key in use)"))

        critical_failures = [c for c in checks if c["critical"] and not c["ok"]]
        return {
            "ok": not critical_failures,
            "checks": checks,
            "failing": [c["check"] for c in checks if not c["ok"]],
            "summary": ("Everything the application depends on is working."
                        if all(c["ok"] for c in checks)
                        else "Working, with degraded features: "
                             + ", ".join(c["check"] for c in checks if not c["ok"])
                        if not critical_failures
                        else "Not usable: "
                             + ", ".join(c["check"] for c in critical_failures)),
        }

    def _handle_disconnect(self) -> None:
        """Every timeframe failed: drop the connection and rebuild it.

        MetaTrader serves one client at a time, so the usual cause is the
        terminal being closed, restarted, or claimed by another process (a
        command-line backtest, most often). The IPC handle is then dead and no
        amount of retrying it will help -- ``shutdown`` then ``connect``
        re-establishes the channel, and priming refills the history that went
        stale while the feed was down.
        """
        # Reconnecting launches the terminal if it is gone. Never do that for
        # an application that is on its way out.
        if self._stop.is_set():
            return
        self._reconnect_attempts += 1
        with self._lock:
            self.status["connected"] = False
            self.status["error"] = (
                "Lost the MetaTrader connection — reconnecting. "
                "If this persists, check the terminal is open and logged in, "
                "and that no backtest is holding it."
            )
        log.warning("Feed lost every timeframe; reconnecting (attempt %d)",
                    self._reconnect_attempts)

        try:
            self.source.shutdown()
        except Exception:
            log.debug("Shutdown during reconnect failed", exc_info=True)

        try:
            self.source.connect()
            self.feed.prime()
        except Exception as exc:
            log.warning("Reconnect failed: %s", exc)
            with self._lock:
                self.status["error"] = f"MetaTrader is not reachable: {exc}"
            return

        # The cost model was measured against the old connection's quotes.
        self._costs = None
        self._costs_at = None
        with self._lock:
            self.status.update(connected=True, symbol=self.source.symbol,
                               error=None)
        log.info("Reconnected to MetaTrader after %d attempt(s)",
                 self._reconnect_attempts)
        self._reconnect_attempts = 0
        self._publish({"type": "status", "status": safe(self.status)})

    def _cost_model(self):
        """The current transaction-cost model, refreshed every few minutes."""
        now = datetime.now(timezone.utc)
        if (self._costs is not None and self._costs_at is not None
                and (now - self._costs_at).total_seconds() < COST_REFRESH_SECONDS):
            return self._costs

        if not self.config.costs.enabled:
            self._costs = CostModel.disabled(
                "cost modelling is switched off in config.json")
            self._costs_at = now
            return self._costs

        try:
            model = CostModel.from_symbol(
                self.source.symbol_details(),
                self.source.tick(),
                commission_per_lot=self.config.costs.commission_per_lot,
                slippage_pips=self.config.costs.slippage_pips,
            )
        except Exception:
            log.debug("Could not read a live spread", exc_info=True)
            model = CostModel.disabled("the terminal did not serve a quote")

        self._costs = model
        self._costs_at = now
        return model

    @property
    def costs(self):
        """The cost model, for callers outside the analysis loop."""
        return self._cost_model()

    def _resume_book(self) -> None:
        """Reload setups that were live when the application last closed.

        The book is in memory, so before this every restart abandoned whatever
        was on the board: nothing resolved those setups, and their log rows
        stayed open for good. The ones that had already filled took their
        results with them, which is the half that mattered -- the record the
        learning layer reads is built from filled trades that closed.

        Setups still inside their expiry window come back and carry on being
        tracked. Everything else is reconciled in the log rather than revived,
        so the signal history stops accumulating rows that are open only
        because nobody was watching.
        """
        try:
            cards = self.db.open_fired_signals()
            restored = self.book.restore(cards, self.profile)
            stranded = self.db.close_stranded_signals(
                keep={s.id for s in restored})
        except Exception:
            log.warning("Could not resume the setup book", exc_info=True)
            return
        if restored:
            log.info("Resumed %d live setup(s) from the previous session",
                     len(restored))
        self._catch_up = {s.id for s in restored if s.entered}
        if stranded:
            log.info("Closed %d stranded signal log row(s) as expired", stranded)

    def _score_record(self, score) -> dict | None:
        """The stored backtest record of the score band ``score`` falls in.

        Read from the most recent stored run and cached, because it changes
        only when a new backtest is saved. States its own sample size, window
        and fill model: a band average over eight trades, or from a run made
        under the old best-price-of-the-bar fills, must say so beside the
        number rather than being quoted as though it were settled.
        """
        if score is None:
            return None
        now = time.monotonic()
        cached = getattr(self, "_record_cache", None)
        if cached is None or now - cached[0] > 600:
            run = None
            try:
                stored = self.db.backtests(limit=1)
                run = stored[0] if stored else None
            except Exception:
                log.warning("Could not read the stored backtest record", exc_info=True)
            self._record_cache = (now, run)
        run = self._record_cache[1]
        if not run:
            return {"available": False,
                    "note": "No backtest has been stored, so this score has no "
                            "measured record yet."}
        stats = run.get("stats") or {}
        if isinstance(stats, str):
            import json
            try:
                stats = json.loads(stats)
            except ValueError:
                stats = {}
        from ..backtest import calibration
        return calibration.band_record(stats, float(score), run)

    def _catch_up_restored(self) -> None:
        """Walk the candles a resumed trade missed while the application was shut.

        Runs once, on the first cycle that has a terminal -- the book is
        restored in ``__init__``, before MetaTrader is connected. Each filled
        trade is handed the M1 history from where its resolver stopped to now,
        so its stop, target or rollover close is found on the candles it
        actually happened on. The live tracking frame holds fifteen hours; a
        trade left over a weekend needs more, so the history is fetched here.

        If the broker's history does not reach back to where the resolver
        stopped, the trade is not walked at all. Walking only the bars that
        exist would skip the gap -- and a stop hit inside it would come out as a
        win. It is dropped and its log row closed ``unresolved``, which is the
        truth: its result cannot be known from the data available.
        """
        pending, self._catch_up = self._catch_up, set()
        if not pending:
            return
        from ..config import TRACKING_TIMEFRAME, TIMEFRAME_MINUTES

        now = datetime.now(timezone.utc)
        bar = TIMEFRAME_MINUTES.get(TRACKING_TIMEFRAME, 1)
        dropped: list[signals.Suggestion] = []
        for suggestion in list(self.book.live):
            if suggestion.id not in pending or not suggestion.entered:
                continue
            since = suggestion.resolved_through or _epoch(suggestion.filled_at)
            if not since:
                dropped.append(suggestion)
                continue
            count = int((now.timestamp() - since) / (60 * bar)) + 10
            count = max(10, min(count, CATCH_UP_MAX_BARS))
            try:
                bars = self.source.candles(TRACKING_TIMEFRAME, count)
            except Exception:
                log.warning("Could not fetch history to resolve resumed trade %s",
                            suggestion.id, exc_info=True)
                dropped.append(suggestion)
                continue
            # Closed candles only: the forming one is applied as a quote by the
            # normal cycle a moment later.
            closes = bars.index.map(lambda t: t.timestamp() + 60 * bar)
            bars = bars[closes <= now.timestamp()]
            if bars.empty or bars.index[0].timestamp() > since + 60 * bar:
                log.warning("History for resumed trade %s starts after the point "
                            "it was last resolved to; leaving it unresolved",
                            suggestion.id)
                dropped.append(suggestion)
                continue
            if getattr(suggestion, "costs", None) is None:
                suggestion.costs = self._cost_model()
            events = signals.resolve.apply_bars(suggestion, bars, self.profile)
            for message in events:
                self._signal_alert(suggestion, f"While the application was closed: {message}")
            log.info("Resumed trade %s walked %d missed candle(s): %s",
                     suggestion.id, len(bars), suggestion.status)

        if dropped:
            self.book.live[:] = [s for s in self.book.live if s not in dropped]
            try:
                self.db.close_stranded_signals(keep={s.id for s in self.book.live})
            except Exception:
                log.warning("Could not close unresolvable resumed trades", exc_info=True)

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        """Connect, prime history, run the first analysis, then loop."""
        # Started before the terminal connection, deliberately: the first macro
        # refresh takes a few seconds of network and there is no reason for it
        # to happen inside the first analysis cycle. By the time MT5 has primed
        # its history the fundamental picture is usually already there.
        self.drivers.start()
        self.source.connect()
        self.feed.prime()
        self.status.update(connected=True, symbol=self.source.symbol, error=None)
        self.refresh(force=True)
        self._thread = threading.Thread(target=self._loop, name="analysis-loop", daemon=True)
        self._thread.start()
        log.info("Analysis service started on %s", self.source.symbol)

    def stop(self) -> None:
        self._stop.set()
        # Before the join, not after: a cycle already inside a reconnect must
        # not be allowed to start the terminal again while we wait for it.
        self.source.close_for_good()
        self.drivers.stop()
        if self._thread:
            self._thread.join(timeout=5)
        self.source.shutdown()
        self.status["connected"] = False

    def _loop(self) -> None:
        while not self._stop.wait(self.config.refresh_seconds):
            try:
                self.refresh()
            except DataUnavailable as exc:
                self.status["error"] = str(exc)
                log.warning("Data unavailable: %s", exc)
            except Exception:  # keep the loop alive; a bad tick must not kill it
                log.exception("Analysis cycle failed")
                self.status["error"] = "Analysis cycle failed; see log."

    # -- the analysis cycle -------------------------------------------------

    def refresh(self, force: bool = False) -> dict:
        """One full cycle. Returns the new decision."""
        closed = self.feed.refresh()
        new_candle = any(closed.values())

        # A cycle in which every timeframe failed is a dead terminal
        # connection, not a slow one. Reconnecting is the only thing that
        # recovers it -- and until this existed, the loop simply retried a
        # channel that was gone, once every five seconds, indefinitely, while
        # the header went on claiming the feed was healthy.
        if self.feed.all_failed:
            self._handle_disconnect()
            return

        # Re-analyse on every tick so price and score stay live, but only treat
        # a closed candle as a structural event worth recording.
        tick = self._safe_tick()
        analysis = mtf.analyse(self.feed.frames(), tick)
        # The tape: sub-minute detail aggregated from the raw tick stream.
        # Attached to the analysis rather than run alongside it so that every
        # consumer -- strategies, the interface, the AI layer -- sees one
        # object describing the market from ticks to the daily candle.
        analysis["micro"] = self._read_tape(analysis)
        # The macro picture, attached the same way and for the same reason.
        # `read` never blocks: it returns the last completed background
        # refresh, or an "initialising" state before the first one lands.
        analysis["fundamentals"] = self.drivers.read(
            analysis.get("price")).to_dict()
        decision = decision_engine.decide(analysis, self.profile)
        # What a score like this one has actually been worth, so no explanation
        # -- model or offline -- can present the number without its record.
        decision["score_record"] = self._score_record(decision.get("score"))

        with self._lock:
            previous = self.decision
            self.analysis = analysis
            self.decision = decision
            self.status["last_update"] = datetime.now(timezone.utc).isoformat()
            self.status["error"] = self.feed.last_error
            self.status["ai_available"] = self.ai.available
            self.status["ai_note"] = self.ai.last_error
            # A successful cycle is the only proof the connection is alive, so
            # it is the only thing that may assert it.
            self.status["connected"] = True
            self.status["symbol"] = self.source.symbol
            self._reconnect_attempts = 0

        self._track_suggestion(analysis, decision)

        # The behavioural layer runs afterwards, when there is a current setup
        # to manage and to look up in the trader's own record.
        self._assess_desk(analysis, decision)

        change = decision_engine.diff(previous, decision)
        if change:
            self._on_change(change, decision, analysis, new_candle or force)

        self._publish({"type": "analysis", "decision": safe(decision),
                       "status": safe(self.status),
                       # The tape moves every cycle and is the one reading
                       # whose whole value is being current, so it rides the
                       # live push rather than waiting for a snapshot.
                       "micro": safe(analysis.get("micro") or {}),
                       "fundamentals": safe(analysis.get("fundamentals") or {}),
                       "trader": safe(self.trader),
                       "book": safe(self.book.to_dict()),
                       "suggestion": safe(self.suggestion.to_dict())
                       if self.suggestion else None})
        return decision

    def _read_tape(self, analysis: dict) -> dict:
        """Aggregate the tick stream into a sub-minute reading.

        Isolated behind its own try/except because it is the only part of the
        cycle that depends on the broker serving tick history, which not every
        account does. Losing the tape must never cost the analysis.
        """
        try:
            ticks = self.feed.refresh_ticks()
            if ticks is None or ticks.empty:
                return micro.unavailable(
                    "this account's feed is not serving tick history")
            return micro.read(ticks, float(analysis.get("atr") or 0.0))
        except Exception:
            log.warning("Tape reading failed", exc_info=True)
            return micro.unavailable("the tape could not be read this cycle")

    def _assess_desk(self, analysis: dict, decision: dict) -> None:
        """Run the behavioural layer and publish anything new it noticed."""
        try:
            state = self.desk.assess(analysis, decision, self.book,
                                     self.feed.frames(), self.profile)
        except Exception:
            log.exception("Trading desk assessment failed")
            return

        with self._lock:
            self.trader = state

        for message in state.get("events", []):
            self._desk_update(message)

    def _track_suggestion(self, analysis: dict, decision: dict) -> None:
        """Advance every tracked setup and admit any new ones.

        Nothing here caps how many setups a day may produce. The application
        never executes a trade, so every setup that qualifies technically is
        built, alerted and shown -- from every strategy, in both directions,
        for as long as the evidence for each holds up.

        The book is given three things it cannot get for itself: the M1 candles
        that let it resolve fills, targets and stops accurately; the analysis
        frames some strategies measure ranges from; and the trader's own record
        per strategy.
        """
        if self._catch_up:
            self._catch_up_restored()

        result = self.book.sync(
            analysis, decision, self.profile,
            bars=self.feed.tracking_frame(),
            frames=self.feed.frames(),
            edge=self._strategy_edge(),
            costs=self._cost_model(),
        )

        # The signal log: every setup is written the moment it is identified,
        # with the trader's clock and the session frozen onto it. This is
        # deliberately unconditional -- it is not filtered by the alert
        # threshold and not deferred until the setup closes, because the
        # question it answers is "what did the system call today?", and a log
        # that omits the quiet calls cannot answer it.
        for suggestion in result["fired"]:
            self._log_fired(suggestion)

        # The write half of the feedback loop: setups that finished on this
        # cycle are recorded with what they were actually worth, so the
        # learning layer has something to read back.
        for suggestion in result["finished"]:
            self._record_outcome(suggestion, analysis)
            self._update_fired(suggestion)

        for suggestion, message in result["events"]:
            self._signal_alert(suggestion, message)
            # Progress on the log row: armed, filled, or a stage change worth
            # seeing next to the time it fired.
            self._update_fired(suggestion)

    def _log_fired(self, suggestion: signals.Suggestion) -> None:
        """Write a newly identified setup to the signal log.

        A failure here must never interrupt the analysis cycle: losing a log
        row is a smaller problem than a loop that dies holding a live setup.
        """
        try:
            self.db.record_fired_signal(suggestion.to_dict(), suggestion.persist())
        except Exception:
            log.warning("Could not log fired signal %s", suggestion.id,
                        exc_info=True)

    def _update_fired(self, suggestion: signals.Suggestion) -> None:
        try:
            self.db.update_fired_signal(suggestion.to_dict(), suggestion.persist())
        except Exception:
            log.warning("Could not update fired signal %s", suggestion.id,
                        exc_info=True)

    def _strategy_edge(self) -> dict:
        """The per-strategy record, re-read only when a setup has closed."""
        if self._edge_dirty:
            try:
                self._edge = self.desk.memory.strategy_edge()
            except Exception:
                log.warning("Could not read the strategy record", exc_info=True)
                self._edge = {}
            self._edge_dirty = False
        return self._edge

    def _record_outcome(self, suggestion: signals.Suggestion, analysis: dict) -> None:
        self._edge_dirty = True
        try:
            record = self.desk.on_terminated(suggestion, analysis)
        except Exception:
            log.exception("Could not record the finished setup")
            return
        if not record:
            return
        outcome = record.get("outcome", "untaken")
        if outcome == "untaken":
            self._desk_update(
                f"{suggestion.setup_label} ({suggestion.strategy_name}) ended "
                f"{suggestion.status.lower()} without ever filling — nothing was "
                "risked.")
        else:
            self._desk_update(
                f"{suggestion.setup_label} ({suggestion.strategy_name}) closed "
                f"{outcome.upper()}: {suggestion.result_pips:+.0f} pips, "
                f"{suggestion.r_multiple:+.2f}R, on {suggestion.exit_reason}. "
                "Recorded to history.")

    def _desk_update(self, message: str) -> None:
        """Put a behavioural observation on the live feed."""
        entry = {
            "time": datetime.now(timezone.utc).isoformat(),
            "message": message,
            "decision": (self.decision or {}).get("decision", "WAIT"),
            "score": (self.decision or {}).get("score", 0),
            "price": (self.decision or {}).get("price", 0),
        }
        with self._lock:
            self.updates.appendleft(entry)
        self.db.add_alert("desk", message, {})
        self._publish({"type": "desk", "update": entry, "trader": safe(self.trader)})

    def _signal_alert(self, suggestion: signals.Suggestion, message: str) -> None:
        """Record a signal-stage alert, respecting the profile's threshold."""
        # Stage changes that end a setup are always worth knowing about, even
        # on a low score; everything else respects the alert threshold.
        always = suggestion.status in signals.TERMINAL_STATES
        if not always and suggestion.score < self.profile.min_alert_score:
            return
        text = f"{suggestion.direction.upper()} {suggestion.setup_label}: {message}"
        self.db.add_alert("signal", text, {
            "signal_id": suggestion.id,
            "status": suggestion.status,
            "score": suggestion.score,
            "price": suggestion.price,
        })
        entry = {
            "time": suggestion.updated_at or suggestion.created_at,
            "message": text,
            "decision": suggestion.action,
            "score": suggestion.score,
            "price": suggestion.price,
        }
        with self._lock:
            self.updates.appendleft(entry)
        self._publish({"type": "signal", "update": entry,
                       "suggestion": safe(suggestion.to_dict())})

    def _on_change(self, change: dict, decision: dict, analysis: dict,
                   record: bool) -> None:
        """Handle a material change: log it, alert, and persist the signal."""
        entry = {
            "time": datetime.now(timezone.utc).isoformat(),
            "message": change["message"],
            "decision": decision["decision"],
            "score": decision["score"],
            "price": decision["price"],
        }
        with self._lock:
            self.updates.appendleft(entry)

        for message in change.get("changes", [change["message"]]):
            self.db.add_alert("decision", message, {"score": decision["score"],
                                                    "price": decision["price"]})

        # Record a signal when the decision itself changes, not on every score
        # wobble, so the history stays a record of setups rather than of ticks.
        if record and decision["decision"] != self._last_recorded_decision:
            explanation = self.ai.explain_decision(decision, analysis)
            with self._lock:
                self.explanation = explanation
            self._last_signal_id = self.db.record_signal(
                decision, analysis, explanation.get("text"),
                suggestion=self.suggestion.to_dict() if self.suggestion else None,
            )
            self._last_recorded_decision = decision["decision"]

        self._publish({"type": "update", "update": entry})

    def _safe_tick(self) -> dict | None:
        try:
            return self.source.tick()
        except DataUnavailable:
            return None

    # -- accessors used by the API -----------------------------------------

    @property
    def suggestion(self) -> signals.Suggestion | None:
        """The setup most deserving attention, for callers that want just one."""
        return self.book.primary

    def snapshot(self) -> dict:
        """Everything the dashboard needs in one payload."""
        with self._lock:
            if self.decision is None or self.analysis is None:
                return {"ready": False, "status": safe(self.status)}
            return safe({
                "ready": True,
                "status": self.status,
                "decision": self.decision,
                "analysis": self.analysis,
                "explanation": self.explanation,
                "updates": list(self.updates)[:20],
                "profile": self.profile.to_dict(),
                "signal_id": self._last_signal_id,
                "suggestion": self.suggestion.to_dict() if self.suggestion else None,
                "book": self.book.to_dict(),
                "trader": self.trader,
            })

    def dashboard(self) -> dict:
        """The condensed table from the specification's dashboard section."""
        with self._lock:
            decision, analysis = self.decision, self.analysis
        if not decision or not analysis:
            return {"ready": False}

        rows = []
        for tf, data in analysis["timeframes"].items():
            rows.append({"label": tf, "value": data["bias"],
                         "detail": data["structure"]["state"]})
        h1 = analysis["timeframes"].get("H1", {})
        liq = analysis["liquidity"]
        rows += [
            {"label": "Structure", "value": h1.get("structure", {}).get("trend", "-"),
             "detail": h1.get("structure", {}).get("state", "")},
            {"label": "Liquidity", "value": liq["bias"],
             "detail": (f"{liq['latest_sweep']['timeframe']} sweep at "
                        f"{liq['latest_sweep']['level']:.2f}")
             if liq.get("latest_sweep") else "no recent sweep"},
            {"label": "Supply/Demand", "value": h1.get("zones", {}).get("bias", "-"),
             "detail": h1.get("zones", {}).get("location", "")},
            {"label": "FVG", "value": h1.get("fvg", {}).get("bias", "-"),
             "detail": f"{h1.get('fvg', {}).get('count', 0)} detected"},
            {"label": "Order Block", "value": h1.get("order_blocks", {}).get("bias", "-"),
             "detail": f"{h1.get('order_blocks', {}).get('count', 0)} detected"},
        ]
        for name in ("rsi", "macd", "adx", "atr"):
            ind = (h1.get("indicators") or {}).get(name)
            if ind:
                rows.append({"label": name.upper(), "value": ind["bias"],
                             "detail": ind["note"]})

        return safe({
            "ready": True,
            "symbol": self.status.get("symbol", "XAUUSD"),
            "price": decision["price"],
            "regime": decision["regime"]["regime"],
            "bias": decision["direction"],
            "score": decision["score"],
            "decision": decision["decision"],
            "headline": decision["headline"],
            "rows": rows,
            "reasons": decision["reasons"],
            "waiting_for": decision["waiting_for"],
            "invalidation": decision["invalidation"],
            "targets": decision["targets"],
            "checklist": decision["confirmation_checklist"],
            "warnings": decision["risk"]["warnings"],
            "reward_risk": decision["risk"]["reward_risk"],
        })

    def chart(self, timeframe: str, bars: int = 300) -> dict:
        """Candles plus every analytical layer, ready for the chart."""
        # Include the still-forming candle so the chart tracks live price. The
        # analysis engines deliberately do not see it -- structure and patterns
        # only mean something once a candle has closed -- but a terminal that
        # lags the market by a whole candle is wrong for the trader.
        df = self.feed.frame(timeframe, closed_only=False).tail(bars)
        with self._lock:
            analysis = self.analysis
        tf_data = (analysis or {}).get("timeframes", {}).get(timeframe, {})

        candles = [
            {
                "time": int(ts.timestamp()),
                "open": round(float(r["open"]), 3),
                "high": round(float(r["high"]), 3),
                "low": round(float(r["low"]), 3),
                "close": round(float(r["close"]), 3),
                "volume": float(r["volume"]),
            }
            for ts, r in df.iterrows()
        ]

        # A chart showing every detected object is unreadable. Only areas and
        # levels within a few ATR of price can affect the current decision, so
        # the rest is filtered out here rather than drawn and ignored.
        price = float(df["close"].iloc[-1])
        atr_value = float(tf_data.get("atr") or 0) or price * 0.002
        reach = atr_value * 12

        def near(items, limit, key=lambda i: i.get("quality", 0)):
            relevant = [
                i for i in items
                if abs(((i.get("top", 0) + i.get("bottom", 0)) / 2) - price) <= reach
            ]
            relevant.sort(key=key, reverse=True)
            return relevant[:limit]

        liquidity = dict((analysis or {}).get("liquidity", {}))
        liquidity["pools"] = [
            pool for pool in liquidity.get("pools", [])
            if abs(pool["price"] - price) <= reach
        ][:8]

        return safe({
            "timeframe": timeframe,
            "candles": candles,
            "series": indicators.chart_series(df),
            "structure": tf_data.get("structure", {}),
            "zones": near((tf_data.get("zones") or {}).get("active", []), 6),
            "fvg": near((tf_data.get("fvg") or {}).get("active", []), 5),
            "order_blocks": near((tf_data.get("order_blocks") or {}).get("active", []), 5),
            "levels": [
                lvl for lvl in (analysis or {}).get("levels", {}).get("levels", [])
                if abs(lvl["price"] - price) <= reach
            ][:10],
            "liquidity": liquidity,
            "fibonacci": (analysis or {}).get("levels", {}).get("fibonacci", {}),
        })

    def set_profile(self, name: str) -> dict:
        self.profile = profile_module.get(name)
        self.db.set_setting("active_profile", self.profile.name)
        self.refresh(force=True)
        return self.profile.to_dict()

    def ask(self, question: str, history: list[dict] | None = None) -> dict:
        with self._lock:
            decision, analysis = self.decision, self.analysis
        if not decision:
            return {"text": "Analysis is not ready yet.", "source": "system"}
        return self.ai.answer(question, decision, analysis, history)

    def teach(self, concept: str) -> dict:
        with self._lock:
            analysis = self.analysis
        return self.ai.teach(concept, analysis)

    # -- websocket fan-out --------------------------------------------------

    def subscribe(self, queue) -> None:
        self._subscribers.append(queue)

    def unsubscribe(self, queue) -> None:
        if queue in self._subscribers:
            self._subscribers.remove(queue)

    def _publish(self, message: dict) -> None:
        dead = []
        for queue in self._subscribers:
            try:
                queue.put_nowait(message)
            except Exception:
                dead.append(queue)
        for queue in dead:
            self.unsubscribe(queue)
