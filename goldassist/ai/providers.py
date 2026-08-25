"""Model providers for the online analyst: Claude, Gemini and OpenAI.

The application's analysis has always been produced twice over: a
deterministic offline narrative assembled from the engine's own measurements,
and — when a key was present — a richer reading from Claude. The offline path
is the one that has actually run on this machine, because the key was never
set, and the online path was reachable only by setting an environment
variable before launch.

This module turns that into a choice the trader makes in the interface, and
widens it to three providers.

**Every provider speaks plain HTTP through httpx.** The `anthropic` and
`openai` SDKs are both installed and both would work, but a uniform HTTP
surface means one code path to reason about, one timeout policy, one error
shape, and — the practical reason — no new packages for PyInstaller to
collect. `httpx` is already bundled.

**The division of labour does not change.** A provider receives the facts the
engine measured and explains them. It never invents a price, never overrides
the decision, and its absence never stops the analysis: the offline narrative
remains the floor, not a degraded mode.

Keys are handled as opaque strings. They are never logged, never included in
an error message, and never returned to the interface in full — see
:mod:`goldassist.ai.credentials`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)

# Long enough for a considered answer over a slow link, short enough that a
# hung provider cannot stall the analysis cycle behind it.
TIMEOUT_SECONDS = 90.0

# What each provider is called in configuration and in the interface.
ANTHROPIC = "anthropic"
GEMINI = "gemini"
OPENAI = "openai"
OFFLINE = "offline"

PROVIDERS = (ANTHROPIC, GEMINI, OPENAI)

# Defaults, all overridable from the settings panel. Model names move faster
# than this application will be rebuilt, which is exactly why the field is
# editable rather than a fixed list.
DEFAULT_MODELS = {
    ANTHROPIC: "claude-opus-5",
    GEMINI: "gemini-2.0-flash",
    OPENAI: "gpt-4o",
}

PROVIDER_LABELS = {
    ANTHROPIC: "Claude (Anthropic)",
    GEMINI: "Gemini (Google)",
    OPENAI: "OpenAI",
}

# Where each provider's key comes from when one is not stored in the app.
# Keeping these means an existing environment-variable setup keeps working.
ENV_VARS = {
    ANTHROPIC: "ANTHROPIC_API_KEY",
    GEMINI: "GEMINI_API_KEY",
    OPENAI: "OPENAI_API_KEY",
}


class ProviderError(RuntimeError):
    """A provider could not answer. Carries a message safe to show a user."""


@dataclass
class Reply:
    text: str
    provider: str
    model: str


def _redact(message: str, key: str) -> str:
    """Remove a key from anything on its way to a log or a screen.

    Providers echo request details back in error bodies, and an API key in a
    log file is a leaked API key however local the file is.
    """
    if key and len(key) > 8:
        message = message.replace(key, "***")
    return message


class Provider:
    """One model provider, reachable over HTTP."""

    name = "provider"

    def __init__(self, api_key: str, model: str | None = None,
                 max_tokens: int = 1200):
        self.api_key = (api_key or "").strip()
        self.model = (model or DEFAULT_MODELS.get(self.name) or "").strip()
        self.max_tokens = max_tokens

    # -- the two things every provider must do ------------------------------

    def complete(self, system: str, user: str,
                 history: list[dict] | None = None) -> Reply:
        raise NotImplementedError

    def complete_vision(self, system: str, user: str, image_b64: str,
                        media_type: str) -> Reply:
        """Same, with a chart screenshot attached.

        Each provider wraps an image differently -- Anthropic uses a source
        block, OpenAI a data URL, Gemini inline_data -- so this cannot be
        shared. A provider that has no vision support raises rather than
        silently answering about an image it never saw.
        """
        raise ProviderError(
            f"{PROVIDER_LABELS.get(self.name, self.name)} does not support image "
            f"analysis in this application.")

    def test(self) -> tuple[bool, str]:
        """Verify the key and model with the smallest possible real call.

        A settings panel that saves a key without checking it is a settings
        panel that reports success and then fails silently on the next
        analysis. This is what makes the Test button meaningful.
        """
        try:
            reply = self.complete(
                "You are a connection test. Reply with the single word OK.",
                "Reply with the single word OK.")
        except ProviderError as exc:
            return False, str(exc)
        except Exception as exc:                      # pragma: no cover
            return False, _redact(f"{type(exc).__name__}: {exc}", self.api_key)
        text = (reply.text or "").strip()
        return True, f"{PROVIDER_LABELS.get(self.name, self.name)} responded ({reply.model})."

    # -- shared HTTP ---------------------------------------------------------

    def _post(self, url: str, *, headers: dict, payload: dict) -> dict:
        if not self.api_key:
            raise ProviderError(f"No API key set for {PROVIDER_LABELS.get(self.name, self.name)}.")
        try:
            response = httpx.post(url, headers=headers, json=payload,
                                  timeout=TIMEOUT_SECONDS)
        except httpx.TimeoutException:
            raise ProviderError(
                f"{PROVIDER_LABELS.get(self.name, self.name)} did not respond within "
                f"{int(TIMEOUT_SECONDS)}s.") from None
        except httpx.HTTPError as exc:
            raise ProviderError(_redact(f"Could not reach the provider: {exc}",
                                        self.api_key)) from None

        if response.status_code >= 400:
            raise ProviderError(_redact(self._explain_status(response), self.api_key))
        try:
            return response.json()
        except ValueError:
            raise ProviderError("The provider returned a response that was not JSON.") from None

    def _explain_status(self, response: httpx.Response) -> str:
        """Turn an HTTP failure into something a trader can act on."""
        detail = ""
        try:
            body = response.json()
            detail = (body.get("error", {}).get("message")
                      if isinstance(body.get("error"), dict) else None) or ""
            if not detail and isinstance(body.get("error"), str):
                detail = body["error"]
        except ValueError:
            detail = (response.text or "")[:200]

        label = PROVIDER_LABELS.get(self.name, self.name)
        if response.status_code in (401, 403):
            return f"{label} rejected the API key. Check it and try again. {detail}".strip()
        if response.status_code == 404:
            return (f"{label} does not recognise the model '{self.model}'. "
                    f"Check the model name. {detail}").strip()
        if response.status_code == 429:
            return f"{label} rate-limited the request. Wait and retry. {detail}".strip()
        if response.status_code >= 500:
            return f"{label} had a server error ({response.status_code}). {detail}".strip()
        return f"{label} returned {response.status_code}. {detail}".strip()


class AnthropicProvider(Provider):
    name = ANTHROPIC
    URL = "https://api.anthropic.com/v1/messages"

    def complete(self, system, user, history=None) -> Reply:
        messages = [{"role": t["role"], "content": t["content"]}
                    for t in (history or [])[-8:]]
        messages.append({"role": "user", "content": user})
        body = self._post(
            self.URL,
            headers={"x-api-key": self.api_key,
                     "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            payload={"model": self.model, "max_tokens": self.max_tokens,
                     "system": system, "messages": messages},
        )
        parts = [b.get("text", "") for b in body.get("content", [])
                 if b.get("type") == "text"]
        text = "\n".join(p for p in parts if p).strip()
        if not text:
            raise ProviderError("Claude returned an empty response.")
        return Reply(text, self.name, body.get("model") or self.model)

    def complete_vision(self, system, user, image_b64, media_type) -> Reply:
        body = self._post(
            self.URL,
            headers={"x-api-key": self.api_key,
                     "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            payload={"model": self.model, "max_tokens": self.max_tokens,
                     "system": system,
                     "messages": [{"role": "user", "content": [
                         {"type": "image",
                          "source": {"type": "base64",
                                     "media_type": media_type,
                                     "data": image_b64}},
                         {"type": "text", "text": user},
                     ]}]},
        )
        parts = [b.get("text", "") for b in body.get("content", [])
                 if b.get("type") == "text"]
        text = "\n".join(p for p in parts if p).strip()
        if not text:
            raise ProviderError("Claude returned an empty response for the image.")
        return Reply(text, self.name, body.get("model") or self.model)


class OpenAIProvider(Provider):
    name = OPENAI
    URL = "https://api.openai.com/v1/chat/completions"

    def complete(self, system, user, history=None) -> Reply:
        messages = [{"role": "system", "content": system}]
        messages += [{"role": t["role"], "content": t["content"]}
                     for t in (history or [])[-8:]]
        messages.append({"role": "user", "content": user})
        body = self._post(
            self.URL,
            headers={"Authorization": f"Bearer {self.api_key}",
                     "content-type": "application/json"},
            payload={"model": self.model, "messages": messages,
                     "max_completion_tokens": self.max_tokens},
        )
        choices = body.get("choices") or []
        text = (choices[0].get("message", {}).get("content") or "").strip() if choices else ""
        if not text:
            raise ProviderError("OpenAI returned an empty response.")
        return Reply(text, self.name, body.get("model") or self.model)

    def complete_vision(self, system, user, image_b64, media_type) -> Reply:
        body = self._post(
            self.URL,
            headers={"Authorization": f"Bearer {self.api_key}",
                     "content-type": "application/json"},
            payload={"model": self.model,
                     "messages": [
                         {"role": "system", "content": system},
                         {"role": "user", "content": [
                             {"type": "text", "text": user},
                             {"type": "image_url", "image_url": {
                                 "url": f"data:{media_type};base64,{image_b64}"}},
                         ]}],
                     "max_completion_tokens": self.max_tokens},
        )
        choices = body.get("choices") or []
        text = (choices[0].get("message", {}).get("content") or "").strip() if choices else ""
        if not text:
            raise ProviderError("OpenAI returned an empty response for the image.")
        return Reply(text, self.name, body.get("model") or self.model)


class GeminiProvider(Provider):
    name = GEMINI
    BASE = "https://generativelanguage.googleapis.com/v1beta/models"

    def complete(self, system, user, history=None) -> Reply:
        # Gemini has no assistant/user role names in common with the others:
        # its turns are "user" and "model", and the system prompt is a
        # separate top-level field rather than a message.
        contents = []
        for turn in (history or [])[-8:]:
            role = "model" if turn.get("role") == "assistant" else "user"
            contents.append({"role": role, "parts": [{"text": turn.get("content", "")}]})
        contents.append({"role": "user", "parts": [{"text": user}]})

        # The key goes in a header, not the query string, so it cannot end up
        # in a proxy log or a redirect.
        body = self._post(
            f"{self.BASE}/{self.model}:generateContent",
            headers={"x-goog-api-key": self.api_key,
                     "content-type": "application/json"},
            payload={"contents": contents,
                     "systemInstruction": {"parts": [{"text": system}]},
                     "generationConfig": {"maxOutputTokens": self.max_tokens}},
        )
        candidates = body.get("candidates") or []
        text = ""
        if candidates:
            parts = (candidates[0].get("content") or {}).get("parts") or []
            text = "\n".join(p.get("text", "") for p in parts).strip()
        if not text:
            blocked = (body.get("promptFeedback") or {}).get("blockReason")
            raise ProviderError(
                f"Gemini returned no text (blocked: {blocked})." if blocked
                else "Gemini returned an empty response.")
        return Reply(text, self.name, self.model)

    def complete_vision(self, system, user, image_b64, media_type) -> Reply:
        body = self._post(
            f"{self.BASE}/{self.model}:generateContent",
            headers={"x-goog-api-key": self.api_key,
                     "content-type": "application/json"},
            payload={"contents": [{"role": "user", "parts": [
                         {"inline_data": {"mime_type": media_type,
                                          "data": image_b64}},
                         {"text": user},
                     ]}],
                     "systemInstruction": {"parts": [{"text": system}]},
                     "generationConfig": {"maxOutputTokens": self.max_tokens}},
        )
        candidates = body.get("candidates") or []
        text = ""
        if candidates:
            parts = (candidates[0].get("content") or {}).get("parts") or []
            text = "\n".join(p.get("text", "") for p in parts).strip()
        if not text:
            raise ProviderError("Gemini returned an empty response for the image.")
        return Reply(text, self.name, self.model)


_CLASSES = {ANTHROPIC: AnthropicProvider, GEMINI: GeminiProvider, OPENAI: OpenAIProvider}


def build(provider: str, api_key: str, model: str | None = None,
          max_tokens: int = 1200) -> Provider:
    """A provider by name. Raises for an unknown one rather than guessing."""
    cls = _CLASSES.get((provider or "").lower())
    if cls is None:
        raise ProviderError(f"Unknown provider '{provider}'. "
                            f"Choose one of: {', '.join(PROVIDERS)}.")
    return cls(api_key=api_key, model=model, max_tokens=max_tokens)
