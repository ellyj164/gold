"""FastAPI application.

Serves the terminal UI and a JSON API over the analysis service, plus a
WebSocket that pushes live updates so the dashboard never polls.

Binds to 127.0.0.1 by default: this is a local desktop application, not a
network service.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import (FastAPI, File, Form, HTTPException, Query, Request, UploadFile,
                     WebSocket, WebSocketDisconnect)
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from datetime import datetime, timezone

from .. import timeutil
from .. import __version__
from ..analysis import events
from ..ai import providers as ai_providers
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

    # Startup/shutdown as a lifespan rather than @app.on_event: Starlette 1.0
    # removed the event-handler API outright. `service.start()` only spawns a
    # daemon thread, so calling both hooks inline here keeps the previous
    # semantics -- Starlette ran sync on_event handlers on the loop too.
    @asynccontextmanager
    async def lifespan(_: FastAPI):
        try:
            service.start()
        except Exception as exc:  # surfaced in the UI rather than crashing the app
            log.exception("Startup failed")
            service.status["error"] = str(exc)
        try:
            yield
        finally:
            service.stop()

    app = FastAPI(title="AI Gold Trading Decision Assistant", version="0.1.0",
                  lifespan=lifespan)
    app.state.service = service

    # -- pages -------------------------------------------------------------

    @app.get("/")
    def index() -> HTMLResponse:
        """The terminal page, with its assets stamped for this build.

        `index.html` links `/static/styles.css` and `/static/app.js` by bare
        path. `StaticFiles` sends an ETag but no `Cache-Control`, and a
        browser is free to reuse a bare-path asset from cache without
        revalidating -- which it does. The result is that an updated app
        renders with the *previous* build's stylesheet and script until the
        cache happens to expire.

        This is not theoretical: it happened while verifying the signal-popup
        change. The new CSS was present in the installed file and served
        correctly, and the page still laid the popup out with the old rules,
        because the browser never asked for the new one.

        Appending the version turns every release into a new URL, so an
        upgrade can never render half-old. Within a release the URL is stable
        and caching still does its job.
        """
        html = (UI_DIR / "index.html").read_text(encoding="utf-8")
        for asset in ("styles.css", "chart.js", "app.js"):
            html = html.replace(f"/static/{asset}", f"/static/{asset}?v={__version__}")
        return HTMLResponse(html, headers={"Cache-Control": "no-cache"})

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
        disabled = set(getattr(service.profile, "disabled_strategies", ()) or ())
        registry = strategy_library_module.describe_all()
        stats = {row["strategy"]: row for row in service.db.strategy_stats()}
        live: dict[str, int] = service.book.counts().get("by_strategy", {})

        for entry in registry:
            entry["enabled"] = ((enabled is None or entry["key"] in enabled)
                                and entry["key"] not in disabled)
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
        # Model calls block for seconds to minutes: never on the event loop.
        return safe(await run_in_threadpool(service.ask, question, body.get("history")))

    @app.post("/api/teach")
    async def teach(request: Request) -> dict:
        body = await request.json()
        concept = (body.get("concept") or "").strip()
        if not concept:
            raise HTTPException(400, "A concept is required.")
        return safe(await run_in_threadpool(service.teach, concept))

    @app.post("/api/explain")
    def explain(refresh: bool = Query(True)) -> dict:
        """Re-analyse and re-explain.

        `refresh` runs a fresh analysis cycle first, which is what makes the
        Regenerate button mean something. Without it the offline analyst --
        a pure function of the decision and the analysis -- returned a
        byte-identical string, so pressing Regenerate appeared to do nothing.
        It *was* regenerating; there was simply nothing new to regenerate
        from. Now the numbers are current, and the stamp below proves it ran.
        """
        if refresh:
            try:
                service.refresh(force=True)
            except Exception as exc:
                log.warning("Refresh before explain failed: %s", exc)

        snap = service.snapshot()
        if not snap.get("ready"):
            raise HTTPException(503, "Analysis is not ready yet.")

        result = dict(service.ai.explain_decision(service.decision, service.analysis))
        result["generated_at"] = timeutil.stamp(datetime.now(timezone.utc).isoformat())
        result["price"] = service.analysis.get("price")
        result["mode"] = service.ai.settings().get("active", "offline")
        service.explanation = result
        return safe(result)

    # -- the analyst's own settings ----------------------------------------

    @app.get("/api/ai/settings")
    def ai_settings() -> dict:
        """Provider, mode and which keys are present. Never a key itself."""
        return safe(service.ai.settings())

    @app.post("/api/ai/settings")
    async def save_ai_settings(request: Request) -> dict:
        body = await request.json()
        mode = body.get("mode")
        provider = body.get("provider")
        if mode is not None and mode not in ("online", "offline"):
            raise HTTPException(400, "Mode must be 'online' or 'offline'.")
        if provider is not None and provider not in ai_providers.PROVIDERS:
            raise HTTPException(
                400, f"Provider must be one of: {', '.join(ai_providers.PROVIDERS)}.")
        try:
            return safe(service.ai.apply_settings(
                mode=mode, provider=provider,
                keys=body.get("keys") or {}, models=body.get("models") or {},
                vision_models=body.get("vision_models") or {}))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/ai/test")
    async def test_ai(request: Request) -> dict:
        """Make a real call, so the panel can prove a key works."""
        body = await request.json()
        provider = body.get("provider")
        if provider not in ai_providers.PROVIDERS:
            raise HTTPException(
                400, f"Provider must be one of: {', '.join(ai_providers.PROVIDERS)}.")
        # Probing a whole NVIDIA chain takes tens of seconds; off the event
        # loop, so the live feed keeps flowing while it runs.
        return safe(await run_in_threadpool(
            service.ai.test_provider, provider, api_key=body.get("api_key"),
            model=body.get("model"), vision_models=body.get("vision_models")))

    def _image_type(file: UploadFile, data: bytes) -> str:
        if len(data) > 8 * 1024 * 1024:
            raise HTTPException(413, "Image must be 8MB or smaller.")
        media_type = file.content_type or "image/png"
        if media_type not in ("image/png", "image/jpeg", "image/gif", "image/webp"):
            raise HTTPException(400, f"Unsupported image type: {media_type}")
        return media_type

    @app.post("/api/chart-image")
    async def chart_image(file: UploadFile, question: str = Query(None)) -> dict:
        data = await file.read()
        media_type = _image_type(file, data)
        # A model call can take a minute. Awaited in a worker thread rather
        # than run here, where it froze every price update until it returned.
        return safe(await run_in_threadpool(
            service.ai.analyse_chart_image, data, media_type, question))

    @app.post("/api/ai/chart")
    async def ai_chart(timeframe: str = Form("H1"), question: str = Form(""),
                       file: UploadFile | None = File(None)) -> dict:
        """The AI desk read of the live chart, with its screenshot if sent."""
        if timeframe not in TIMEFRAMES and timeframe not in MICRO_TIMEFRAMES:
            raise HTTPException(400, f"Unknown timeframe: {timeframe}")
        data, media_type = None, "image/jpeg"
        if file is not None:
            data = await file.read()
            media_type = _image_type(file, data)
        return safe(await run_in_threadpool(
            service.chart_insight, timeframe, data or None, media_type,
            question.strip() or None))

    @app.post("/api/ai/signal")
    async def ai_signal(request: Request) -> dict:
        """Why a logged signal fired, and whether its facts still hold.

        Takes ``id`` (a row of the signal log) or ``uid`` (the live signal
        card's suggestion), plus an optional follow-up ``question``.
        """
        body = await request.json()
        signal_id = body.get("id")
        uid = (body.get("uid") or "").strip() or None
        if signal_id is None and not uid:
            raise HTTPException(400, "A signal id or uid is required.")
        try:
            signal_id = int(signal_id) if signal_id is not None else None
        except (TypeError, ValueError):
            raise HTTPException(400, "The signal id must be a number.") from None
        question = (body.get("question") or "").strip() or None
        result = await run_in_threadpool(
            service.review_signal, signal_id, uid, question, body.get("history"))
        if result is None:
            raise HTTPException(404, "No such signal in the log.")
        return safe(result)

    # -- news --------------------------------------------------------------

    @app.get("/api/news")
    def news(hours: float = Query(24, ge=1, le=168),
             impact: str | None = None, bias: str | None = None,
             limit: int = Query(150, ge=1, le=500)) -> dict:
        """Tagged headlines, the desk's state, and the week's calendar."""
        if impact not in (None, "", "high", "medium", "low"):
            raise HTTPException(400, "impact must be high, medium or low.")
        if bias not in (None, "", "bullish", "bearish", "neutral"):
            raise HTTPException(400, "bias must be bullish, bearish or neutral.")
        state = service.news.read()
        state.pop("items", None)
        week = events.upcoming(horizon_hours=24 * 7)
        # The whole week, not the eight the analysis carries.
        now = datetime.now(timezone.utc)
        ahead = [e for e in events.load()
                 if 0 <= (e["time"] - now).total_seconds() <= 7 * 86400]
        return safe({
            "items": service.db.recent_news(hours=hours, limit=limit,
                                            impact=impact or None, bias=bias or None),
            "state": state,
            "calendar": {
                "next_high_impact": week.get("next_high_impact"),
                "last_high_impact": week.get("last_high_impact"),
                "coverage": week.get("coverage"),
                "events": [{"title": e["title"], "impact": e["impact"],
                            "time": e["time"].isoformat(),
                            "forecast": e.get("forecast"), "previous": e.get("previous"),
                            "details": e.get("details"), "source": e.get("source")}
                           for e in ahead],
            },
            "clock": timeutil.offset_label(),
        })

    @app.post("/api/news/refresh")
    def refresh_news() -> dict:
        """Read the wires now instead of at the next three-minute tick."""
        service.news.refresh_soon()
        return {"ok": True}

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

    @app.get("/api/health")
    def health() -> dict:
        """Whether every dependency is actually working, checked not assumed."""
        return safe(service.health())

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
        #
        # Sequential on purpose. The parallel runner forks worker processes,
        # and this path runs inside the live application while it holds the
        # MetaTrader connection -- MT5 serves one client at a time. The command
        # line (`python -m goldassist.backtest`) is where a deep parallel
        # replay belongs, with the terminal to itself.
        def _run():
            frames = service.feed.frames()
            return backtest_engine.run(
                frames, prof, step_timeframe=timeframe, step=step,
                costs=service.costs)

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
