"""FastAPI application.

Serves the terminal UI and a JSON API over the analysis service, plus a
WebSocket that pushes live updates so the dashboard never polls.

Binds to 127.0.0.1 by default: this is a local desktop application, not a
network service.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .. import timeutil
from ..backtest import engine as backtest_engine
from ..config import AppConfig, MICRO_TIMEFRAMES, TIMEFRAMES
from ..decision import profile as profile_module
from ..jsonsafe import safe
from .service import AnalysisService

log = logging.getLogger(__name__)

UI_DIR = Path(__file__).resolve().parent.parent / "ui"


def create_app(config: AppConfig | None = None) -> FastAPI:
    config = config or AppConfig.load()
    # Set the display clock before anything can stamp a timestamp with it.
    # Done here as well as in app.py so a server started directly -- by a test,
    # a script, or `python -m goldassist.api` -- reads the same clock as the
    # desktop app rather than silently falling back to the default.
    timeutil.configure(config.display_utc_offset)
    service = AnalysisService(config)
    app = FastAPI(title="AI Gold Trading Decision Assistant", version="0.1.0")
    app.state.service = service

    @app.on_event("startup")
    def _startup() -> None:
        try:
            service.start()
        except Exception as exc:  # surfaced in the UI rather than crashing the app
            log.exception("Startup failed")
            service.status["error"] = str(exc)

    @app.on_event("shutdown")
    def _shutdown() -> None:
        service.stop()

    # -- pages -------------------------------------------------------------

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(UI_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=UI_DIR / "static"), name="static")

    # -- live state --------------------------------------------------------

    @app.get("/api/status")
    def status() -> dict:
        return safe(service.status)

    @app.get("/api/snapshot")
    def snapshot() -> dict:
        return service.snapshot()

    @app.get("/api/dashboard")
    def dashboard() -> dict:
        return service.dashboard()

    @app.get("/api/chart/{timeframe}")
    def chart(timeframe: str, bars: int = Query(300, ge=50, le=2000)) -> dict:
        if timeframe not in TIMEFRAMES:
            raise HTTPException(400, f"Unsupported timeframe: {timeframe}")
        try:
            return service.chart(timeframe, bars)
        except Exception as exc:
            raise HTTPException(503, str(exc)) from exc

    @app.get("/api/signal")
    def current_signal() -> dict:
        """The setup deserving attention, plus every other one in play."""
        suggestion = service.suggestion
        return safe({
            "suggestion": suggestion.to_dict() if suggestion else None,
            "book": service.book.to_dict(),
            "profile": {
                "min_score": service.profile.min_score,
                "min_reward_risk": service.profile.min_reward_risk,
                "min_alert_score": service.profile.min_alert_score,
            },
        })

    @app.get("/api/setups")
    def setups() -> dict:
        """Every setup currently tracked, plus the ones that have finished.

        Nothing caps how many appear: the application never trades, so a setup
        that qualifies technically is always shown.
        """
        return safe(service.book.to_dict())

    @app.get("/api/strategies")
    def strategy_library() -> dict:
        """Every registered strategy, which are active, and how each has done."""
        from .. import strategies as strategy_library_module

        enabled = getattr(service.profile, "enabled_strategies", None)
        registry = strategy_library_module.describe_all()
        stats = {row["strategy"]: row for row in service.db.strategy_stats()}
        live: dict[str, int] = service.book.counts().get("by_strategy", {})

        for entry in registry:
            entry["enabled"] = enabled is None or entry["key"] in enabled
            entry["live"] = live.get(entry["key"], 0)
            entry["record"] = stats.get(entry["key"])
        return safe({
            "strategies": registry,
            "enabled": list(enabled) if enabled else None,
            "profile": service.profile.name,
        })

    @app.get("/api/setups/{setup_id}")
    def setup_detail(setup_id: str) -> dict:
        found = service.book.find(setup_id)
        if found is None:
            raise HTTPException(404, "No such setup.")
        return safe(found.to_dict())

    # -- the trader behaviour layer ----------------------------------------

    @app.get("/api/trader")
    def trader() -> dict:
        """Session, day character, top-down read, view, record and management."""
        if not service.trader:
            return {"ready": False}
        return safe({"ready": True, **service.trader})

    @app.get("/api/outcomes")
    def outcomes(limit: int = Query(200, ge=1, le=1000)) -> dict:
        """Closed setups, with what each was actually worth.

        ``summary`` totals only the setups that filled: an idea that expired
        without an entry is neither a win nor a loss and must not dilute either.
        """
        rows = service.db.setup_outcomes(limit)
        taken = [r for r in rows if r.get("entered")]
        wins = [r for r in taken if r.get("outcome") == "win"]
        losses = [r for r in taken if r.get("outcome") == "loss"]
        decided = len(wins) + len(losses)
        return safe({
            "outcomes": rows,
            "session_book": service.book.results(),
            "summary": {
                "recorded": len(rows),
                "taken": len(taken),
                "untaken": len(rows) - len(taken),
                "wins": len(wins),
                "losses": len(losses),
                "scratches": len(taken) - decided,
                "pips": round(sum(float(r.get("result_pips") or 0) for r in taken), 1),
                "r": round(sum(float(r.get("r_multiple") or 0) for r in taken), 2),
                "win_rate": round(len(wins) / decided * 100, 1) if decided else None,
            },
            "strategies": service.db.strategy_stats(),
        })

    @app.get("/api/memory")
    def trading_memory() -> dict:
        """The trader's own record, grouped the way a trader would group it."""
        return safe(service.desk.memory.report().to_dict())

    @app.get("/api/thesis")
    def session_view() -> dict:
        view = service.desk.thesis
        if view is None:
            return {"ready": False}
        return safe({"ready": True, **view.to_dict()})

    @app.post("/api/refresh")
    def refresh() -> dict:
        return safe(service.refresh(force=True))

    # -- AI ----------------------------------------------------------------

    @app.post("/api/ask")
    async def ask(request: Request) -> dict:
        body = await request.json()
        question = (body.get("question") or "").strip()
        if not question:
            raise HTTPException(400, "A question is required.")
        return safe(service.ask(question, body.get("history")))

    @app.post("/api/teach")
    async def teach(request: Request) -> dict:
        body = await request.json()
        concept = (body.get("concept") or "").strip()
        if not concept:
            raise HTTPException(400, "A concept is required.")
        return safe(service.teach(concept))

    @app.post("/api/explain")
    def explain() -> dict:
        snap = service.snapshot()
        if not snap.get("ready"):
            raise HTTPException(503, "Analysis is not ready yet.")
        result = service.ai.explain_decision(service.decision, service.analysis)
        service.explanation = result
        return safe(result)

    @app.post("/api/chart-image")
    async def chart_image(file: UploadFile, question: str = Query(None)) -> dict:
        data = await file.read()
        if len(data) > 8 * 1024 * 1024:
            raise HTTPException(413, "Image must be 8MB or smaller.")
        media_type = file.content_type or "image/png"
        if media_type not in ("image/png", "image/jpeg", "image/gif", "image/webp"):
            raise HTTPException(400, f"Unsupported image type: {media_type}")
        return safe(service.ai.analyse_chart_image(data, media_type, question))

    # -- profiles ----------------------------------------------------------

    @app.get("/api/profiles")
    def profiles() -> dict:
        return safe({
            "active": service.profile.name,
            "profiles": [p.to_dict() for p in profile_module.load_profiles().values()],
        })

    @app.post("/api/profiles/active")
    async def set_active_profile(request: Request) -> dict:
        body = await request.json()
        name = body.get("name")
        if name not in profile_module.load_profiles():
            raise HTTPException(404, f"No such profile: {name}")
        return safe(service.set_profile(name))

    @app.post("/api/profiles")
    async def save_profile(request: Request) -> dict:
        body = await request.json()
        try:
            prof = profile_module.StrategyProfile.from_dict(body)
        except (TypeError, ValueError) as exc:
            raise HTTPException(400, f"Invalid profile: {exc}") from exc
        profile_module.save_profile(prof)
        return safe(prof.to_dict())

    @app.get("/api/tape")
    def tape() -> dict:
        """The sub-minute read: what price is doing between M5 closes."""
        analysis = service.analysis or {}
        return safe({
            "micro": analysis.get("micro") or {},
            "timeframes": list(TIMEFRAMES),
            "micro_timeframes": list(MICRO_TIMEFRAMES),
        })

    # -- the signal log ----------------------------------------------------

    @app.get("/api/clock")
    def clock() -> dict:
        """The clock the interface should render every timestamp on."""
        return safe({
            "offset": timeutil.offset_label(),
            "offset_hours": timeutil.offset_hours(),
            "now": timeutil.describe(timeutil.now_utc()),
        })

    @app.get("/api/fired")
    def fired(limit: int = Query(300, ge=1, le=1000),
              date: str | None = None,
              session: str | None = None,
              strategy: str | None = None,
              direction: str | None = None,
              outcome: str | None = None) -> dict:
        """Every signal this system has fired, newest first.

        Distinct from /api/signals, which is the decision log, and from
        /api/outcomes, which only knows about setups that have finished.
        """
        return safe({
            "signals": service.db.fired_signals(
                limit=limit, date=date, session=session, strategy=strategy,
                direction=direction, outcome=outcome),
            "summary": service.db.fired_summary(date),
            "dates": service.db.fired_dates(),
            "clock": timeutil.offset_label(),
            "sessions": timeutil.session_options(),
        })

    # -- history, journal, alerts ------------------------------------------

    @app.get("/api/signals")
    def signals(limit: int = Query(100, ge=1, le=500),
                decision: str | None = None) -> dict:
        return safe({"signals": service.db.recent_signals(limit, decision)})

    @app.get("/api/signals/{signal_id}")
    def signal_record(signal_id: int) -> dict:
        record = service.db.signal(signal_id)
        if not record:
            raise HTTPException(404, "No such signal.")
        return safe(record)

    @app.post("/api/signals/{signal_id}/result")
    async def resolve_signal(signal_id: int, request: Request) -> dict:
        body = await request.json()
        result = body.get("result")
        if result not in ("win", "loss", "breakeven", "untaken", "open"):
            raise HTTPException(400, "Invalid result value.")
        service.db.resolve_signal(signal_id, result, body.get("pips"))
        return {"ok": True}

    @app.get("/api/journal")
    def journal() -> dict:
        return safe({
            "entries": service.db.journal_entries(),
            "stats": service.db.journal_stats(),
        })

    @app.post("/api/journal")
    async def add_journal(request: Request) -> dict:
        body = await request.json()
        entry_id = service.db.add_journal_entry(body)
        return {"id": entry_id}

    @app.patch("/api/journal/{entry_id}")
    async def update_journal(entry_id: int, request: Request) -> dict:
        service.db.update_journal_entry(entry_id, await request.json())
        return {"ok": True}

    @app.get("/api/alerts")
    def alerts(unseen: bool = False) -> dict:
        return safe({"alerts": service.db.alerts(unseen_only=unseen)})

    @app.post("/api/alerts/seen")
    def mark_seen() -> dict:
        service.db.mark_alerts_seen()
        return {"ok": True}

    # -- backtesting -------------------------------------------------------

    @app.get("/api/backtests")
    def backtests() -> dict:
        return safe({"backtests": service.db.backtests()})

    @app.get("/api/backtests/{backtest_id}")
    def backtest_detail(backtest_id: int) -> dict:
        record = service.db.backtest(backtest_id)
        if not record:
            raise HTTPException(404, "No such backtest.")
        return safe(record)

    @app.post("/api/backtests")
    async def run_backtest(request: Request) -> dict:
        body = await request.json()
        name = body.get("profile") or service.profile.name
        prof = profile_module.get(name)
        step = int(body.get("step", 8))
        timeframe = body.get("timeframe", "M15")

        # Run off the event loop: a replay takes minutes, not milliseconds.
        def _run():
            frames = service.feed.frames()
            return backtest_engine.run(frames, prof, step_timeframe=timeframe, step=step)

        try:
            result = await asyncio.to_thread(_run)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

        backtest_id = service.db.save_backtest(
            prof.name, result["stats"], result["trades"], timeframe=timeframe
        )
        return safe({"id": backtest_id, **result})

    # -- settings ----------------------------------------------------------

    @app.get("/api/settings")
    def get_settings() -> dict:
        return safe(service.db.all_settings())

    @app.post("/api/settings")
    async def save_settings(request: Request) -> dict:
        for key, value in (await request.json()).items():
            service.db.set_setting(key, value)
        return {"ok": True}

    # -- live push ---------------------------------------------------------

    @app.websocket("/ws")
    async def websocket(ws: WebSocket) -> None:
        await ws.accept()
        queue: asyncio.Queue = asyncio.Queue(maxsize=64)
        loop = asyncio.get_running_loop()

        # The analysis loop runs on a worker thread; bounce its messages onto
        # the event loop before they reach the socket.
        class ThreadQueue:
            def put_nowait(self, message):
                loop.call_soon_threadsafe(queue.put_nowait, message)

        bridge = ThreadQueue()
        service.subscribe(bridge)
        try:
            await ws.send_json(service.snapshot())
            while True:
                message = await queue.get()
                await ws.send_json(message)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            service.unsubscribe(bridge)

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> JSONResponse:
        log.exception("Unhandled error on %s", request.url.path)
        return JSONResponse(status_code=500, content={"error": str(exc)})

    return app
