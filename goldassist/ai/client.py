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
import logging
import os
from typing import Iterator

from ..config import AIConfig
from . import credentials, explain, providers
from .prompts import (
    CHART_VISION_PROMPT,
    LEARNING_SYSTEM_PROMPT,
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
        signature = (mode, provider, key, credentials.get_model(provider))
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
                max_tokens=self.config.max_tokens)
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
                       models=None) -> dict:
        """Persist settings from the panel and re-resolve the client."""
        if provider is not None:
            credentials.set_provider(provider)
        if mode is not None:
            credentials.set_mode(mode)
        for name, value in (keys or {}).items():
            credentials.set_key(name, value)
        for name, value in (models or {}).items():
            credentials.set_model(name, value)
        # Force the next call to rebuild against what was just saved.
        self._checked = False
        return self.settings()

    def test_provider(self, provider: str, api_key: str | None = None,
                      model: str | None = None) -> dict:
        """Try a real call, so the panel can say whether a key works."""
        key = (api_key or "").strip() or credentials.get_key(provider)
        if not key:
            return {"ok": False,
                    "message": f"No API key set for "
                               f"{providers.PROVIDER_LABELS.get(provider, provider)}."}
        try:
            client = providers.build(provider, key,
                                     model=model or credentials.get_model(provider),
                                     max_tokens=64)
        except providers.ProviderError as exc:
            return {"ok": False, "message": str(exc)}
        ok, message = client.test()
        return {"ok": ok, "message": message}

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
            return {"text": text, "source": "claude", "model": self.config.model}
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
        out[tf] = {
            "role": data.get("role"),
            "bias": data.get("bias"),
            "regime": (data.get("regime") or {}).get("regime"),
            "structure": {
                "trend": data["structure"]["trend"],
                "state": data["structure"]["state"],
                "labels": data["structure"].get("labels"),
                "last_event": data["structure"].get("last_event"),
                "next_bullish_break": data["structure"].get("next_bullish_break"),
                "next_bearish_break": data["structure"].get("next_bearish_break"),
            },
            "candles": {
                "bias": data["candles"]["bias"],
                "latest": data["candles"].get("latest"),
            },
            "zones": {
                "location": data["zones"]["location"],
                "nearest_demand": data["zones"].get("nearest_demand"),
                "nearest_supply": data["zones"].get("nearest_supply"),
            },
            "fvg": {
                "bias": data["fvg"]["bias"],
                "nearest_bullish": data["fvg"].get("nearest_bullish"),
                "nearest_bearish": data["fvg"].get("nearest_bearish"),
            },
            "order_blocks": {
                "bias": data["order_blocks"]["bias"],
                "nearest_bullish": data["order_blocks"].get("nearest_bullish"),
                "nearest_bearish": data["order_blocks"].get("nearest_bearish"),
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
