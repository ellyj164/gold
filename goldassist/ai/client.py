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
from . import explain
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
        self.last_error: str | None = None

    # -- client management --------------------------------------------------

    @property
    def available(self) -> bool:
        """True when an API key is present and the SDK imported cleanly."""
        return self._ensure_client() is not None

    def _ensure_client(self):
        if self._checked:
            return self._client
        self._checked = True

        if not self.config.enabled:
            self.last_error = "AI layer disabled in configuration."
            return None
        if not os.environ.get(self.config.api_key_env):
            self.last_error = (
                f"{self.config.api_key_env} is not set. Running with offline "
                "explanations; set the key to enable the AI analyst."
            )
            return None
        try:
            import anthropic
        except ImportError:
            self.last_error = "The anthropic package is not installed."
            return None

        self._client = anthropic.Anthropic()
        self.last_error = None
        return self._client

    # -- core call ----------------------------------------------------------

    def _system_blocks(self, prompt: str) -> list[dict]:
        """System prompt as a cacheable block.

        The prompt is byte-identical on every request, so the cache breakpoint
        placed here is read rather than rewritten on subsequent calls.
        """
        return [{"type": "text", "text": prompt, "cache_control": {"type": "ephemeral"}}]

    def _message(self, system: str, content, max_tokens: int | None = None) -> str:
        client = self._ensure_client()
        if client is None:
            raise AIUnavailable(self.last_error or "AI unavailable")

        import anthropic

        try:
            response = client.messages.create(
                model=self.config.model,
                max_tokens=max_tokens or self.config.max_tokens,
                system=self._system_blocks(system),
                messages=[{"role": "user", "content": content}],
            )
        except anthropic.APIStatusError as exc:
            self.last_error = f"Claude API error {exc.status_code}: {exc.message}"
            raise AIUnavailable(self.last_error) from exc
        except anthropic.APIConnectionError as exc:
            self.last_error = "Could not reach the Claude API (network error)."
            raise AIUnavailable(self.last_error) from exc

        # A refused response carries no usable text; check before reading content.
        if response.stop_reason == "refusal":
            self.last_error = "The request was declined by the model's safety system."
            raise AIUnavailable(self.last_error)

        text = "".join(b.text for b in response.content if b.type == "text")
        if not text.strip():
            raise AIUnavailable("The model returned an empty response.")
        self.last_error = None
        return text.strip()

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
        messages: list[dict] = []
        for turn in (history or [])[-8:]:
            messages.append({"role": turn["role"], "content": turn["content"]})
        messages.append({"role": "user", "content": facts_prompt(facts, question)})

        import anthropic

        try:
            response = client.messages.create(
                model=self.config.model,
                max_tokens=self.config.max_tokens,
                system=self._system_blocks(SYSTEM_PROMPT),
                messages=messages,
            )
        except (anthropic.APIStatusError, anthropic.APIConnectionError) as exc:
            log.info("Chat call failed: %s", exc)
            return {
                "text": explain.answer_offline(question, decision, analysis),
                "source": "offline",
                "note": str(exc),
            }

        if response.stop_reason == "refusal":
            return {
                "text": "That question was declined by the model's safety system. "
                        "Try rephrasing it around the technical analysis.",
                "source": "refused",
            }
        text = "".join(b.text for b in response.content if b.type == "text").strip()
        return {"text": text, "source": "claude", "model": self.config.model}

    def stream_answer(self, question: str, decision: dict, analysis: dict,
                      history: list[dict] | None = None) -> Iterator[str]:
        """Streaming variant of :meth:`answer`, yielding text chunks."""
        client = self._ensure_client()
        if client is None:
            yield explain.answer_offline(question, decision, analysis)
            return

        facts = _compact_facts(decision, analysis)
        messages: list[dict] = []
        for turn in (history or [])[-8:]:
            messages.append({"role": turn["role"], "content": turn["content"]})
        messages.append({"role": "user", "content": facts_prompt(facts, question)})

        import anthropic

        try:
            with client.messages.stream(
                model=self.config.model,
                max_tokens=self.config.max_tokens,
                system=self._system_blocks(SYSTEM_PROMPT),
                messages=messages,
            ) as stream:
                for chunk in stream.text_stream:
                    yield chunk
        except (anthropic.APIStatusError, anthropic.APIConnectionError) as exc:
            log.info("Streaming chat failed: %s", exc)
            yield explain.answer_offline(question, decision, analysis)

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
                "text": "Chart image analysis needs the Claude API. Set "
                        f"{self.config.api_key_env} to enable it.",
                "source": "unavailable",
                "note": self.last_error,
            }

        content = [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": base64.standard_b64encode(image_bytes).decode("utf-8"),
                },
            },
            {"type": "text", "text": question or "Analyse this Gold chart."},
        ]
        try:
            text = self._message(CHART_VISION_PROMPT, content, max_tokens=1600)
            return {"text": text, "source": "claude"}
        except AIUnavailable as exc:
            return {"text": f"Chart analysis failed: {exc}", "source": "error"}


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
        }
    return out
