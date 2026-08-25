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
from ..data import DataUnavailable, MarketFeed, MT5Source
from ..decision import engine as decision_engine
from ..decision import profile as profile_module
from ..jsonsafe import safe
from ..storage import Database
from ..trader import TradingDesk
from .. import signals

log = logging.getLogger(__name__)


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

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        """Connect, prime history, run the first analysis, then loop."""
        self.source.connect()
        self.feed.prime()
        self.status.update(connected=True, symbol=self.source.symbol, error=None)
        self.refresh(force=True)
        self._thread = threading.Thread(target=self._loop, name="analysis-loop", daemon=True)
        self._thread.start()
        log.info("Analysis service started on %s", self.source.symbol)

    def stop(self) -> None:
        self._stop.set()
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

        # Re-analyse on every tick so price and score stay live, but only treat
        # a closed candle as a structural event worth recording.
        tick = self._safe_tick()
        analysis = mtf.analyse(self.feed.frames(), tick)
        # The tape: sub-minute detail aggregated from the raw tick stream.
        # Attached to the analysis rather than run alongside it so that every
        # consumer -- strategies, the interface, the AI layer -- sees one
        # object describing the market from ticks to the daily candle.
        analysis["micro"] = self._read_tape(analysis)
        decision = decision_engine.decide(analysis, self.profile)

        with self._lock:
            previous = self.decision
            self.analysis = analysis
            self.decision = decision
            self.status["last_update"] = datetime.now(timezone.utc).isoformat()
            self.status["error"] = self.feed.last_error
            self.status["ai_available"] = self.ai.available
            self.status["ai_note"] = self.ai.last_error

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
        result = self.book.sync(
            analysis, decision, self.profile,
            bars=self.feed.tracking_frame(),
            frames=self.feed.frames(),
            edge=self._strategy_edge(),
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
            self.db.record_fired_signal(suggestion.to_dict())
        except Exception:
            log.warning("Could not log fired signal %s", suggestion.id,
                        exc_info=True)

    def _update_fired(self, suggestion: signals.Suggestion) -> None:
        try:
            self.db.update_fired_signal(suggestion.to_dict())
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
