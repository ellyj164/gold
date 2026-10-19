"""Claude-backed analyst layer.

Sits *above* the technical engine, never inside it. The engine computes facts;
this module turns them into natural language, answers the trader's questions,
and reads uploaded chart screenshots.

If the API is unreachable or no key is configured, every method falls back to
the deterministic generator in :mod:`goldassist.ai.explain`, so the application
keeps working offline exactly as the specification requires.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
from typing import Iterator

from ..config import AIConfig
from . import credentials, explain, providers
from .prompts import (
    CHART_INSIGHT_PROMPT,
    CHART_VISION_PROMPT,
    LEARNING_SYSTEM_PROMPT,
    NEWS_CLASSIFY_PROMPT,
    SIGNAL_REVIEW_PROMPT,
    SYSTEM_PROMPT,
    facts_prompt,
)

log = logging.getLogger(__name__)


class AIUnavailable(RuntimeError):
    """Raised internally when the API cannot be reached; callers see a fallback."""


class AIAnalyst:
    def __init__(self, config: AIConfig):
        self.config = config
        self._client = None
        self._checked = False
        # What the client was last built from. A change here rebuilds it.
        self._signature: tuple | None = None
        self.last_error: str | None = None

    # -- client management --------------------------------------------------

    @property
    def available(self) -> bool:
        """True when an API key is present and the SDK imported cleanly."""
        return self._ensure_client() is not None

    def _ensure_client(self):
        """The provider to use right now, or None to run offline.

        Rebuilt whenever the stored settings change rather than cached for the
        life of the process: the trader can switch provider or paste a key in
        the settings panel mid-session, and the very next explanation has to
        honour it. The old version resolved a client once, on first use, so a
        key added after launch did nothing until a restart.
        """
        mode = credentials.get_mode()
        provider = credentials.get_provider()
        key = credentials.get_key(provider)
        signature = (mode, provider, key, credentials.get_model(provider),
                     credentials.get_vision_models(provider))
        if self._checked and signature == self._signature:
            return self._client

        self._checked = True
        self._signature = signature
        self._client = None

        if not self.config.enabled:
            self.last_error = "AI layer disabled in configuration."
            return None
        if mode != "online":
            # Not an error. Offline is a supported mode, not a failure, and
            # saying so keeps the status line honest.
            self.last_error = None
            return None
        if not key:
            self.last_error = (
                f"Online mode is selected but no API key is set for "
                f"{providers.PROVIDER_LABELS.get(provider, provider)}. "
                f"Add one in Settings, or switch to offline analysis."
            )
            return None

        try:
            self._client = providers.build(
                provider, key,
                model=credentials.get_model(provider),
                max_tokens=self.config.max_tokens,
                vision_models=credentials.get_vision_models(provider))
        except providers.ProviderError as exc:
            self.last_error = str(exc)
            return None

        self.last_error = None
        return self._client

    def settings(self) -> dict:
        """What the interface shows in the settings panel."""
        described = credentials.describe()
        # Resolving the client is what turns "a key is present" into "online
        # will actually work", so the panel reports the tested truth.
        client = self._ensure_client()
        described["active"] = "online" if client is not None else "offline"
        described["note"] = self.last_error or described.get("note", "")
        return described

    def apply_settings(self, *, mode=None, provider=None, keys=None,
                       models=None, vision_models=None) -> dict:
        """Persist settings from the panel and re-resolve the client."""
        if provider is not None:
            credentials.set_provider(provider)
        if mode is not None:
            credentials.set_mode(mode)
        for name, value in (keys or {}).items():
            credentials.set_key(name, value)
            if name == providers.NVIDIA and value:
                # A new key is a fresh start: failures under the old one
                # (a 401 above all) say nothing about this one.
                providers.reset_health()
        for name, value in (models or {}).items():
            credentials.set_model(name, value)
        for name, value in (vision_models or {}).items():
            credentials.set_vision_models(name, value)
        # Force the next call to rebuild against what was just saved.
        self._checked = False
        return self.settings()

    def test_provider(self, provider: str, api_key: str | None = None,
                      model: str | None = None,
                      vision_models: str | None = None) -> dict:
        """Try a real call, so the panel can say whether a key works.

        For NVIDIA it probes every link of both chains, so the answer is not
        just "the key works" but which models are alive today.
        """
        key = (api_key or "").strip() or credentials.get_key(provider)
        if not key:
            return {"ok": False,
                    "message": f"No API key set for "
                               f"{providers.PROVIDER_LABELS.get(provider, provider)}."}
        try:
            client = providers.build(
                provider, key,
                model=model or credentials.get_model(provider),
                max_tokens=64,
                vision_models=vision_models or credentials.get_vision_models(provider))
        except providers.ProviderError as exc:
            return {"ok": False, "message": str(exc)}

        if isinstance(client, providers.NvidiaProvider):
            text = client.probe(client.models)
            vision = client.probe(client.vision_models, vision=True)
            alive = [r for r in text if r["ok"]]
            seeing = [r for r in vision if r["ok"]]
            if not alive:
                message = f"No NVIDIA text model answered. {text[0]['message'] if text else ''}"
            else:
                message = (f"{len(alive)} of {len(text)} text models and "
                           f"{len(seeing)} of {len(vision)} vision models answered.")
            return {"ok": bool(alive), "message": message.strip(),
                    "models": {"text": text, "vision": vision}}

        ok, message = client.test()
        return {"ok": ok, "message": message}

    def _stamp(self, client) -> dict:
        """Which models were tried for the last answer, for the interface."""
        attempts = getattr(client, "attempts", None) or []
        failed = [a["model"] for a in attempts if not a.get("ok")]
        return {"fallbacks": failed} if failed else {}

    # -- core call ----------------------------------------------------------

    def _message(self, system: str, content, max_tokens: int | None = None) -> str:
        """One completion from whichever provider is configured."""
        client = self._ensure_client()
        if client is None:
            raise AIUnavailable(self.last_error or "Offline analysis is selected.")

        if max_tokens:
            client.max_tokens = max_tokens
        try:
            reply = client.complete(system, content)
        except providers.ProviderError as exc:
            self.last_error = str(exc)
            raise AIUnavailable(self.last_error) from exc
        except Exception as exc:                       # pragma: no cover
            self.last_error = f"{type(exc).__name__} talking to the provider."
            raise AIUnavailable(self.last_error) from exc

        self.last_error = None
        self.last_model = reply.model
        self.last_provider = reply.provider
        return reply.text

    # -- public API ---------------------------------------------------------

    def explain_decision(self, decision: dict, analysis: dict) -> dict:
        """Narrative explanation of the current BUY/SELL/WAIT decision."""
        facts = _compact_facts(decision, analysis)
        try:
            text = self._message(SYSTEM_PROMPT, facts_prompt(facts))
            # "claude" is the interface's tag for "an online model wrote
            # this"; the model field says which one actually did.
            return {"text": text, "source": "claude",
                    "model": getattr(self, "last_model", None) or self.config.model}
        except AIUnavailable as exc:
            if not self.config.offline_fallback:
                raise
            log.info("Falling back to offline explanation: %s", exc)
            return {
                "text": explain.decision_narrative(decision, analysis),
                "source": "offline",
                "note": str(exc),
            }

    def answer(self, question: str, decision: dict, analysis: dict,
               history: list[dict] | None = None) -> dict:
        """Answer a trader question about the current market state."""
        facts = _compact_facts(decision, analysis)
        client = self._ensure_client()
        if client is None:
            return {
                "text": explain.answer_offline(question, decision, analysis),
                "source": "offline",
                "note": self.last_error,
            }

        # Conversation history goes before the facts turn so the stable system
        # prompt stays at the front of the prefix.
        history_turns = [{"role": t["role"], "content": t["content"]}
                         for t in (history or [])[-8:]]
        try:
            reply = client.complete(SYSTEM_PROMPT,
                                    facts_prompt(facts, question),
                                    history=history_turns)
        except providers.ProviderError as exc:
            log.info("Chat call failed: %s", exc)
            self.last_error = str(exc)
            return {
                "text": explain.answer_offline(question, decision, analysis),
                "source": "offline",
                "note": str(exc),
            }
        self.last_error = None
        return {"text": reply.text, "source": reply.provider, "model": reply.model}

    def teach(self, concept: str, analysis: dict | None = None) -> dict:
        """Learning mode: explain a concept, with a live example where possible."""
        content = f"Explain this trading concept: {concept}"
        if analysis:
            facts = {"timeframes": _timeframe_digest(analysis),
                     "liquidity": analysis.get("liquidity", {}).get("latest_sweep"),
                     "regime": analysis.get("regime")}
            content = facts_prompt(facts, content)
        try:
            text = self._message(LEARNING_SYSTEM_PROMPT, content, max_tokens=1400)
            return {"text": text, "source": "claude"}
        except AIUnavailable as exc:
            return {"text": explain.teach_offline(concept), "source": "offline",
                    "note": str(exc)}

    def analyse_chart_image(self, image_bytes: bytes, media_type: str,
                            question: str | None = None) -> dict:
        """Vision analysis of an uploaded Gold chart screenshot."""
        client = self._ensure_client()
        if client is None:
            return {
                "text": ("Reading a chart image needs an online model. Open "
                         "Settings, choose a provider, add its API key and set "
                         "the analysis mode to online. The offline analyst "
                         "reads the live feed directly and does not need an "
                         "image."),
                "source": "unavailable",
                "note": self.last_error,
            }

        image_b64 = base64.standard_b64encode(image_bytes).decode("utf-8")
        try:
            client.max_tokens = 1600
            reply = client.complete_vision(
                CHART_VISION_PROMPT,
                question or "Analyse this Gold chart.",
                image_b64, media_type)
            return {"text": reply.text, "source": reply.provider,
                    "model": reply.model}
        except providers.ProviderError as exc:
            return {"text": f"Chart analysis failed: {exc}", "source": "error"}
        except Exception as exc:                        # pragma: no cover
            log.warning("Chart image analysis failed", exc_info=True)
            return {"text": f"Chart analysis failed: {type(exc).__name__}",
                    "source": "error"}

    def chart_insight(self, timeframe: str, decision: dict, analysis: dict,
                      trader: dict | None = None, image_bytes: bytes | None = None,
                      media_type: str = "image/jpeg",
                      question: str | None = None) -> dict:
        """A desk read of the live chart: the picture plus the measured facts.

        The image is the trader's own chart as drawn on screen, so a vision
        model sees the same structure the trader is looking at, while every
        number still comes from the engine. When no vision model can read the
        image -- all resting, or none configured -- the read is made from the
        facts alone and says so, rather than failing the button.
        """
        facts = _compact_facts(decision, analysis)
        facts["chart_timeframe"] = timeframe
        facts["desk"] = desk_digest(trader)
        ask = (f"Read the {timeframe} chart. Trader's question: {question}"
               if question else
               f"Give the desk read of the {timeframe} chart, top-down.")

        client = self._ensure_client()
        if client is None:
            return {
                "text": explain.decision_narrative(decision, analysis),
                "source": "offline",
                "note": (self.last_error or "Offline analysis is selected.")
                        + " The chart read needs an online model; this is the "
                          "engine's own narrative.",
                "used_image": False,
            }

        client.max_tokens = 1800
        note = None
        if image_bytes:
            image_b64 = base64.standard_b64encode(image_bytes).decode("utf-8")
            try:
                reply = client.complete_vision(
                    CHART_INSIGHT_PROMPT, facts_prompt(facts, ask),
                    image_b64, media_type)
                self.last_error = None
                return {"text": reply.text, "source": reply.provider,
                        "model": reply.model, "used_image": True,
                        **self._stamp(client)}
            except providers.ProviderError as exc:
                if exc.status in (401, 403):
                    self.last_error = str(exc)
                    return {"text": f"Chart read failed: {exc}", "source": "error"}
                log.info("No vision model read the chart; using facts only: %s", exc)
                note = "No vision model could read the chart image just now, so this read is from the engine's measurements only."
            except Exception as exc:                    # pragma: no cover
                log.warning("Chart vision call failed", exc_info=True)
                note = f"The chart image could not be read ({type(exc).__name__}); this read is from the engine's measurements only."

        try:
            reply = client.complete(CHART_INSIGHT_PROMPT, facts_prompt(facts, ask))
        except providers.ProviderError as exc:
            self.last_error = str(exc)
            return {"text": explain.decision_narrative(decision, analysis),
                    "source": "offline", "used_image": False,
                    "note": f"Every online model failed ({exc}). This is the engine's own narrative."}
        self.last_error = None
        result = {"text": reply.text, "source": reply.provider,
                  "model": reply.model, "used_image": False, **self._stamp(client)}
        if note:
            result["note"] = note
        return result

    def classify_headlines(self, items: list[dict]) -> tuple[list[dict], str | None]:
        """Model readings for a batch of headlines.

        Returns ``(readings, model)``. ``readings`` holds one dict per item the
        model tagged validly -- ``{id, bias, impact, reason}`` -- and simply
        omits any it did not, so a half-malformed answer still lands the good
        half. An empty list with ``model`` None means no model was reachable,
        and the caller keeps its keyword tags.
        """
        if self._ensure_client() is None or not items:
            return [], None
        # Its own provider instance: this runs on the news thread, and the
        # shared one has its max_tokens set per call by whatever the trader is
        # asking at that moment. Model health stays shared (module-level).
        provider = credentials.get_provider()
        try:
            client = providers.build(
                provider, credentials.get_key(provider),
                model=credentials.get_model(provider),
                vision_models=credentials.get_vision_models(provider))
        except providers.ProviderError:
            return [], None
        lines = []
        for item in items:
            summary = (item.get("summary") or "")[:220]
            lines.append(json.dumps({"id": item["id"], "source": item.get("source_name"),
                                     "headline": item["title"], "summary": summary},
                                    ensure_ascii=False))
        client.max_tokens = 60 + 70 * len(items)
        try:
            reply = client.complete(NEWS_CLASSIFY_PROMPT,
                                    "Headlines, one JSON object per line:\n" + "\n".join(lines))
        except providers.ProviderError as exc:
            self.last_error = str(exc)
            return [], None
        return _parse_readings(reply.text, {i["id"] for i in items}), reply.model

    def review_signal(self, signal: dict, decision: dict | None,
                      analysis: dict | None, trader: dict | None = None,
                      question: str | None = None,
                      history: list[dict] | None = None) -> dict:
        """Why a signal fired, and whether its reasons still hold now."""
        facts = {
            "signal": signal_digest(signal),
            "market_now": _compact_facts(decision, analysis) if decision and analysis else None,
            "desk": desk_digest(trader),
        }
        ask = (question or "Audit this signal: why it fired, and check each of "
                           "its facts against the market now.")

        client = self._ensure_client()
        if client is None:
            return {"text": signal_offline(signal), "source": "offline",
                    "note": (self.last_error or "Offline analysis is selected.")
                            + " An online model is needed to check the facts "
                              "against the market; this is the signal's own record."}

        client.max_tokens = 1600
        try:
            reply = client.complete(
                SIGNAL_REVIEW_PROMPT, facts_prompt(facts, ask),
                history=[{"role": t["role"], "content": t["content"]}
                         for t in (history or [])[-6:]])
        except providers.ProviderError as exc:
            self.last_error = str(exc)
            return {"text": signal_offline(signal), "source": "offline",
                    "note": f"Every online model failed ({exc}). This is the signal's own record."}
        self.last_error = None
        return {"text": reply.text, "source": reply.provider, "model": reply.model,
                **self._stamp(client)}


# --- news ----------------------------------------------------------------------

_ARRAY = re.compile(r"\[.*\]", re.S)


def _parse_readings(text: str, ids: set[str]) -> list[dict]:
    """The model's JSON array, validated item by item.

    Models wrap JSON in code fences or a sentence despite being told not to,
    so the outermost array is cut out first. Each item must name an id that
    was actually sent and use the allowed values; anything else is dropped
    rather than guessed at.
    """
    match = _ARRAY.search(text or "")
    if not match:
        return []
    try:
        data = json.loads(match.group(0))
    except ValueError:
        return []
    out = []
    for row in data if isinstance(data, list) else []:
        if not isinstance(row, dict) or row.get("id") not in ids:
            continue
        bias = str(row.get("bias", "")).lower()
        impact = str(row.get("impact", "")).lower()
        if bias not in ("bullish", "bearish", "neutral") or impact not in ("high", "medium", "low"):
            continue
        out.append({"id": row["id"], "bias": bias, "impact": impact,
                    "reason": str(row.get("reason") or "")[:160]})
    return out


def news_digest(news: dict | None) -> dict | None:
    """Recent headlines as the analyst should read them: context, not signal."""
    if not news or not news.get("items"):
        return None
    return {
        "note": ("Headlines tagged for their bearing on gold by a classifier. Context "
                 "for the read, not a signal; never treat a headline as more than it says."),
        "balance_12h": news.get("balance"),
        "headlines": [
            {k: item.get(k) for k in ("published_at", "source_name", "title", "bias",
                                      "impact", "reason")}
            for item in news["items"][:10]
        ],
    }


# --- signals -------------------------------------------------------------------

def signal_digest(signal: dict) -> dict:
    """One fired signal, as the auditing model should read it.

    ``signal`` is a ``fired_signals`` row with its ``card`` parsed. The flat
    columns carry what fired and how it ended; the card adds the reasoning the
    strategy recorded -- rationale, the agreeing strategies' own evidence, the
    risks it flagged, the status history -- which is exactly what "check the
    facts" means. Feature vectors and drawing data are left out.
    """
    card = signal.get("card") if isinstance(signal.get("card"), dict) else {}

    def pick(*keys):
        for key in keys:
            value = signal.get(key)
            if value in (None, "", []):
                value = card.get(key)
            if value not in (None, "", []):
                return value
        return None

    history = card.get("history") or []
    return {
        "strategy": pick("strategy_name", "strategy"),
        "family": pick("family"),
        "direction": pick("direction"),
        "action": pick("action"),
        "setup": pick("setup_label"),
        "fired": pick("fired_local", "fired_at"),
        "session": pick("session_label", "fired_session_label"),
        "price_when_fired": pick("price"),
        "status_now": pick("last_status", "status"),
        "score": pick("score"),
        "peak_score": card.get("peak_score"),
        "conviction": pick("conviction"),
        "context_score": pick("context_score"),
        "quality": pick("quality"),
        "regime": pick("regime", "regime_at_entry"),
        "alignment": card.get("alignment"),
        "trigger": pick("trigger_text", "trigger"),
        "reasons": pick("reasons"),
        "agreeing_strategies": pick("confirmations"),
        "entry_rationale": card.get("entry_rationale"),
        "entry_zone": card.get("entry_zone") or {
            "low": signal.get("entry_low"), "high": signal.get("entry_high"),
            "preferred": signal.get("entry_preferred")},
        "stop": pick("stop", "invalidation"),
        "targets": card.get("targets") or [signal.get(k) for k in ("tp1", "tp2", "tp3")
                                            if signal.get(k) is not None],
        "reward_risk": pick("reward_risk"),
        "first_target_pips": signal.get("first_target_pips"),
        "costs": card.get("costs"),
        "risks_flagged": card.get("risks"),
        "exposure": card.get("exposure_at_fill") or card.get("exposure"),
        "status_history": [
            {"time": h.get("time"), "status": h.get("status"),
             "score": h.get("score"), "note": h.get("note")}
            for h in history[-10:]] or None,
        "result": {
            "filled_at": pick("filled_at"),
            "fill_price": pick("fill_price"),
            "outcome": pick("outcome"),
            "exit_reason": pick("exit_reason"),
            "exit_price": pick("exit_price"),
            "r_multiple": pick("r_multiple"),
            "result_pips": pick("result_pips"),
            "mfe_pips": card.get("mfe_pips"),
            "mae_pips": card.get("mae_pips"),
        } if pick("filled_at", "outcome") else None,
    }


def signal_offline(signal: dict) -> str:
    """The signal's own record, readable, for when no model can be reached."""
    d = signal_digest(signal)
    side = "BUY" if d.get("direction") == "bullish" else "SELL"
    lines = [f"{d.get('strategy') or 'Signal'} — {side}, fired {d.get('fired') or '—'}"
             f" ({d.get('session') or 'session unknown'}), score {d.get('score') or '—'}."]
    if d.get("trigger"):
        lines.append(f"Trigger: {d['trigger']}.")
    reasons = d.get("reasons")
    if isinstance(reasons, list) and reasons:
        lines.append("Why it fired:\n" + "\n".join(f"- {r}" for r in reasons))
    agreeing = d.get("agreeing_strategies")
    if isinstance(agreeing, list) and agreeing:
        rows = []
        for a in agreeing:
            if isinstance(a, dict):
                evidence = "; ".join(a.get("evidence") or [])
                rows.append(f"- {a.get('name') or a.get('key')}: {evidence or 'agreed'}")
        if rows:
            lines.append("Also agreeing:\n" + "\n".join(rows))
    zone = d.get("entry_zone") or {}
    lines.append(f"Entry zone {zone.get('low') or '—'}–{zone.get('high') or '—'}, "
                 f"stop {d.get('stop') or '—'}, reward/risk {d.get('reward_risk') or '—'}. "
                 f"Status now: {d.get('status_now') or '—'}.")
    return "\n\n".join(lines)


# --- fact compaction -------------------------------------------------------

# The full analysis object is large and mostly chart-drawing detail. Only the
# decision-relevant subset is sent to the model, which keeps the prompt small
# and the reasoning focused.
def _compact_facts(decision: dict, analysis: dict) -> dict:
    return {
        "symbol": "XAUUSD",
        "price": analysis.get("price"),
        "generated_at": analysis.get("generated_at"),
        "regime": analysis.get("regime"),
        "alignment": analysis.get("alignment"),
        "decision": {
            "action": decision.get("decision"),
            "direction": decision.get("direction"),
            "score": decision.get("score"),
            "opposing_score": decision.get("opposing_score"),
            "headline": decision.get("headline"),
            "reasons": decision.get("reasons"),
            "waiting_for": decision.get("waiting_for"),
            "profile": decision.get("profile"),
        },
        # What this score band has historically been worth, with its sample
        # size and caveats. The explanation must state it; see the prompt.
        "score_record": decision.get("score_record"),
        "evidence": (decision.get("confluence", {})
                     .get(decision.get("direction", "bullish"), {})
                     .get("evidence", [])),
        "penalties": (decision.get("confluence", {})
                      .get(decision.get("direction", "bullish"), {})
                      .get("penalties", [])),
        "risk": {
            "invalidation": decision.get("invalidation"),
            "targets": decision.get("targets"),
            "reward_risk": (decision.get("risk") or {}).get("reward_risk"),
            "warnings": (decision.get("risk") or {}).get("warnings"),
            "atr": (decision.get("risk") or {}).get("atr"),
        },
        "levels": {
            "nearest_resistance": analysis.get("levels", {}).get("nearest_resistance"),
            "nearest_support": analysis.get("levels", {}).get("nearest_support"),
            "strongest_resistance": analysis.get("levels", {}).get("strongest_resistance"),
            "strongest_support": analysis.get("levels", {}).get("strongest_support"),
            "pivots": analysis.get("levels", {}).get("pivots"),
        },
        "liquidity": {
            "bias": analysis.get("liquidity", {}).get("bias"),
            "latest_sweep": analysis.get("liquidity", {}).get("latest_sweep"),
            "target": analysis.get("liquidity", {}).get("target"),
            "nearest_buyside": analysis.get("liquidity", {}).get("nearest_buyside"),
            "nearest_sellside": analysis.get("liquidity", {}).get("nearest_sellside"),
        },
        "timeframes": _timeframe_digest(analysis),
        # Sub-minute tape: what price is doing right now, as distinct from
        # what the last closed candle said.
        "tape": _tape_digest(analysis),
        # Releases. Gold moves further on an FOMC statement than on any
        # technical setup, and until v1.2.0 the analyst was never told one was
        # coming.
        "events": analysis.get("events"),
        # Where the day's business has been done, and how stretched price is
        # from it. The analyst was describing structure without ever knowing
        # the reference every desk measures itself against.
        "vwap": _vwap_digest(analysis),
        # The day's clock-derived references and what shape the session is
        # taking. Trend day or rotation changes what the same setup is worth.
        "session": _auction_digest(analysis),
        # Whether moves are currently persisting or reverting, and how
        # confident that is. Decides which playbook fits, not which direction.
        "persistence": _persistence_digest(analysis),
        # Why Gold is priced where it is: real yields, positioning, implied
        # volatility. The analyst could previously explain what the chart did
        # and never why, which is the half a trader cannot get elsewhere.
        "fundamentals": _fundamental_digest(analysis),
        # What the wires have said in the last hours, each headline already
        # tagged for its channel to gold. The calendar says what is scheduled;
        # this is everything that was not.
        "news": news_digest(analysis.get("news")),
    }


def _vwap_digest(analysis: dict) -> dict | None:
    """The anchors, compacted: a level, a slope and a distance each."""
    read = analysis.get("vwap") or {}
    if not read.get("available"):
        return None
    anchors = {}
    for name, anchor in (read.get("anchors") or {}).items():
        anchors[name] = {"vwap": anchor.get("vwap"),
                         "position": anchor.get("position"),
                         "sigma": anchor.get("distance_sigma"),
                         "slope": anchor.get("slope"),
                         "bands": anchor.get("bands")}
    return {"session": read.get("session"), "anchors": anchors,
            "observations": read.get("observations")}


def _auction_digest(analysis: dict) -> dict | None:
    """Prior day, overnight range, initial balance, and how spent the day is."""
    read = analysis.get("auction") or {}
    if not read.get("available"):
        return None
    return {
        "prior_day": read.get("prior_day"),
        "asia_range": read.get("asia"),
        "today_range": read.get("today"),
        "initial_balance": read.get("initial_balance"),
        "extension": (read.get("extension") or {}).get("direction"),
        "range_used": read.get("range_used"),
        "day_type": read.get("day_type"),
        # Which of those references price is sitting on right now, which is the
        # difference between a list of levels and a reason to look at the chart.
        "testing_now": read.get("tests"),
        # `observations` is deliberately omitted. Unlike the other blocks here,
        # every sentence in it is derived from fields already in this digest --
        # the day type, the range-used note and `testing_now` carry the same
        # readings in the form the model needs. Sending both roughly doubled
        # the session block for no added information.
    }


def _persistence_digest(analysis: dict) -> dict | None:
    read = analysis.get("persistence") or {}
    if not read.get("available"):
        return None
    return {k: read.get(k) for k in
            ("regime", "confidence", "hurst", "efficiency_ratio", "playbook",
             "observations") if k in read}


def _fundamental_digest(analysis: dict) -> dict | None:
    """The macro picture, with the caveats that must travel with it.

    Deliberately carries ``drivers`` and ``cautions`` as written rather than
    re-deriving them here: those sentences already say what each number means
    and what it does not, and a second phrasing of the same reading is a second
    chance to phrase it wrongly.
    """
    read = analysis.get("fundamentals") or {}
    if not read.get("available"):
        return None
    reference = read.get("reference") or {}
    return {
        "bias": read.get("bias"),
        "strength": read.get("strength"),
        "headline": read.get("headline"),
        "drivers": read.get("drivers"),
        "cautions": read.get("cautions"),
        "volatility_regime": read.get("volatility_regime"),
        "implied_vs_realised_vol": {
            "implied": reference.get("implied_vol"),
            "realised": reference.get("realised_vol"),
            "ratio": reference.get("vol_ratio"),
        },
        "positioning": {
            k: (read.get("positioning") or {}).get(k)
            for k in ("net", "net_pct_oi", "percentile", "crowding",
                      "report_date", "age_days")
        },
        "feed_healthy": read.get("feed_healthy"),
    }


def _tape_digest(analysis: dict) -> dict | None:
    micro = analysis.get("micro") or {}
    if not micro or micro.get("available") is False:
        return None
    return {k: micro.get(k) for k in
            ("state", "note", "velocity", "spread_ratio", "spread_wide",
             "activity", "agrees_with") if k in micro}


def desk_digest(trader: dict | None) -> dict | None:
    """The behavioural layer, in the shape the analyst should read it.

    The trader state is large and mostly for the interface. What matters to an
    explanation is *why this move is happening and whether it lasts* -- the
    metals regime separates a monetary rally from a haven one -- and what the
    system's own measured record says, which is the only thing in the building
    that is evidence rather than opinion.
    """
    if not trader:
        return None
    cross = trader.get("intermarket") or {}
    edge = trader.get("expectancy") or {}
    overall = edge.get("overall") or {}
    return {
        "session": (trader.get("session") or {}).get("name"),
        "day_character": (trader.get("character") or {}).get("headline"),
        "dollar": (trader.get("macro") or {}).get("headline"),
        "metals_regime": {
            "regime": cross.get("regime"),
            "headline": cross.get("headline"),
            "cautions": cross.get("cautions"),
        } if cross.get("available") else None,
        "measured_record": {
            "trades": overall.get("n"),
            "total_r": overall.get("total_r"),
            "win_rate": overall.get("win_rate"),
            "headline": edge.get("headline"),
        } if overall.get("n") else None,
        "conviction": (trader.get("verdict") or {}).get("conviction"),
        "suggested_risk": (trader.get("verdict") or {}).get("risk_label"),
    }


def _timeframe_digest(analysis: dict) -> dict:
    """Per-timeframe summary: the readings, not the full drawing data."""
    out = {}
    for tf, data in (analysis.get("timeframes") or {}).items():
        # A timeframe without enough candles comes back without some blocks.
        # Indexing them directly turned that into a KeyError that took every
        # online explanation down with it; a missing block is now just absent.
        structure = data.get("structure") or {}
        candles = data.get("candles") or {}
        zones = data.get("zones") or {}
        fvg = data.get("fvg") or {}
        order_blocks = data.get("order_blocks") or {}
        out[tf] = {
            "role": data.get("role"),
            "bias": data.get("bias"),
            "regime": (data.get("regime") or {}).get("regime"),
            "structure": {
                "trend": structure.get("trend"),
                "state": structure.get("state"),
                "labels": structure.get("labels"),
                "last_event": structure.get("last_event"),
                "next_bullish_break": structure.get("next_bullish_break"),
                "next_bearish_break": structure.get("next_bearish_break"),
            },
            "candles": {
                "bias": candles.get("bias"),
                "latest": candles.get("latest"),
            },
            "zones": {
                "location": zones.get("location"),
                "nearest_demand": zones.get("nearest_demand"),
                "nearest_supply": zones.get("nearest_supply"),
            },
            "fvg": {
                "bias": fvg.get("bias"),
                "nearest_bullish": fvg.get("nearest_bullish"),
                "nearest_bearish": fvg.get("nearest_bearish"),
            },
            "order_blocks": {
                "bias": order_blocks.get("bias"),
                "nearest_bullish": order_blocks.get("nearest_bullish"),
                "nearest_bearish": order_blocks.get("nearest_bearish"),
            },
            "indicators": {k: v.get("note") for k, v in (data.get("indicators") or {}).items()},
            # The v1.2.0 layers. Without these the analyst could not see where
            # the market actually traded, whether the price was a good one, or
            # that a failed level had flipped -- and it was explaining setups
            # that the scoring engine had already judged using all three.
            "volume_profile": data.get("volume_profile"),
            "dealing_range": data.get("dealing_range"),
            "breakers": data.get("breakers"),
        }
    return out
