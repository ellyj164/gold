"""Model providers for the online analyst: NVIDIA, Claude, Gemini and OpenAI.

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
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)

# Long enough for a considered answer over a slow link, short enough that a
# hung provider cannot stall the analysis cycle behind it.
TIMEOUT_SECONDS = 90.0

# What each provider is called in configuration and in the interface.
NVIDIA = "nvidia"
ANTHROPIC = "anthropic"
GEMINI = "gemini"
OPENAI = "openai"
OFFLINE = "offline"

PROVIDERS = (NVIDIA, ANTHROPIC, GEMINI, OPENAI)

# NVIDIA's hosted catalogue (build.nvidia.com) is free and wide, and uneven:
# on any given hour one model is overloaded, another answers in two seconds,
# and a third has been retired. So NVIDIA is configured as an ordered *chain*
# rather than a model, and the first model that answers wins. The order is
# quality first, then speed: the large reasoning models lead, the small fast
# ones are the floor. Measured on this account on 2026-10-18; every entry is
# editable in the settings panel and re-tested by its Test button.
NVIDIA_TEXT_CHAIN = (
    "nvidia/nemotron-3-super-120b-a12b",
    "nvidia/nemotron-3-ultra-550b-a55b",
    "z-ai/glm-5.3",
    "deepseek-ai/deepseek-v4.1-flash",
    "stepfun-ai/step-5-preview",
    "openai/gpt-oss-20b",
    "nvidia/nemotron-3.5-lightning-30b-a3b",
)

# Models that can see. Only these are sent a chart image; a text model given
# an image either errors or, worse, answers as though it had looked.
NVIDIA_VISION_CHAIN = (
    "nvidia/nemotron-3.5-super-vl-120b-a12b",
    "meta/llama-3.2-90b-vision-instruct",
    "google/gemma-4-31b-it",
    "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning",
    "meta/llama-3.2-11b-vision-instruct",
)

# Defaults, all overridable from the settings panel. Model names move faster
# than this application will be rebuilt, which is exactly why the field is
# editable rather than a fixed list.
DEFAULT_MODELS = {
    NVIDIA: ", ".join(NVIDIA_TEXT_CHAIN),
    ANTHROPIC: "claude-opus-5",
    GEMINI: "gemini-2.0-flash",
    OPENAI: "gpt-4o",
}

# Providers whose image reading is configured separately from their text
# models. For the others the one model does both.
DEFAULT_VISION_MODELS = {
    NVIDIA: ", ".join(NVIDIA_VISION_CHAIN),
}

PROVIDER_LABELS = {
    NVIDIA: "NVIDIA (free models, automatic failover)",
    ANTHROPIC: "Claude (Anthropic)",
    GEMINI: "Gemini (Google)",
    OPENAI: "OpenAI",
}

# Where each provider's key comes from when one is not stored in the app.
# Keeping these means an existing environment-variable setup keeps working.
ENV_VARS = {
    NVIDIA: "NVIDIA_API_KEY",
    ANTHROPIC: "ANTHROPIC_API_KEY",
    GEMINI: "GEMINI_API_KEY",
    OPENAI: "OPENAI_API_KEY",
}


class ProviderError(RuntimeError):
    """A provider could not answer. Carries a message safe to show a user.

    ``status`` is the HTTP status, ``"timeout"``, ``"network"`` or
    ``"empty"`` -- what the NVIDIA chain needs to decide how long to rest a
    model, and nothing a trader has to read.
    """

    def __init__(self, message: str, status: int | str | None = None):
        super().__init__(message)
        self.status = status


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

    def _post(self, url: str, *, headers: dict, payload: dict,
              timeout: float = TIMEOUT_SECONDS) -> dict:
        if not self.api_key:
            raise ProviderError(f"No API key set for {PROVIDER_LABELS.get(self.name, self.name)}.",
                                status=401)
        try:
            response = httpx.post(url, headers=headers, json=payload,
                                  timeout=timeout)
        except httpx.TimeoutException:
            raise ProviderError(
                f"{PROVIDER_LABELS.get(self.name, self.name)} did not respond within "
                f"{int(timeout)}s.", status="timeout") from None
        except httpx.HTTPError as exc:
            raise ProviderError(_redact(f"Could not reach the provider: {exc}",
                                        self.api_key), status="network") from None

        if response.status_code >= 400:
            raise ProviderError(_redact(self._explain_status(response), self.api_key),
                                status=response.status_code)
        try:
            return response.json()
        except ValueError:
            raise ProviderError("The provider returned a response that was not JSON.",
                                status="empty") from None

    def _explain_status(self, response: httpx.Response) -> str:
        """Turn an HTTP failure into something a trader can act on."""
        detail = ""
        try:
            body = response.json()
            detail = (body.get("error", {}).get("message")
                      if isinstance(body.get("error"), dict) else None) or ""
            if not detail and isinstance(body.get("error"), str):
                detail = body["error"]
            # NVIDIA answers in the problem-details shape instead.
            if not detail and isinstance(body.get("detail"), str):
                detail = body["detail"][:200]
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


def parse_chain(value: str | None) -> list[str]:
    """``"a, b\\nc"`` -> ``["a", "b", "c"]``, order kept, duplicates dropped."""
    seen, out = set(), []
    for item in re.split(r"[,\n]", value or ""):
        item = item.strip()
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


# -- NVIDIA: a chain of free models with failover ---------------------------
#
# Health is module-level, not per instance, because the analyst rebuilds its
# provider whenever the settings change: a model that timed out a minute ago
# is just as overloaded after the trader edits a field.

_HEALTH: dict[str, dict] = {}
_HEALTH_LOCK = threading.Lock()

# How long a model rests after each kind of failure, in seconds. A rate limit
# clears quickly; an overloaded model usually stays overloaded for minutes; a
# model the catalogue no longer serves will not come back this session.
COOLDOWNS = {
    429: 60,
    "timeout": 180,
    "network": 60,
    "empty": 120,
    400: 3600,          # usually "this model does not take that input"
    404: 6 * 3600,
    410: 6 * 3600,
}
DEFAULT_COOLDOWN = 180  # 5xx and anything unforeseen

_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.S | re.I)


def _cooldown_for(status) -> int:
    return COOLDOWNS.get(status, DEFAULT_COOLDOWN)


def _record(model: str, ok: bool, *, error: str = "", status=None,
            latency: float | None = None) -> None:
    now = time.time()
    with _HEALTH_LOCK:
        entry = _HEALTH.setdefault(model, {})
        entry.update({"ok": ok, "at": now, "latency": latency,
                      "error": "" if ok else error,
                      "until": 0.0 if ok else now + _cooldown_for(status)})


def model_health(models: list[str] | None = None) -> list[dict]:
    """What the settings panel shows next to each model in the chain."""
    now = time.time()
    with _HEALTH_LOCK:
        names = models if models is not None else sorted(_HEALTH)
        out = []
        for name in names:
            entry = _HEALTH.get(name) or {}
            resting = max(0, int(entry.get("until", 0) - now))
            out.append({
                "model": name,
                "state": ("untested" if not entry
                          else "ok" if entry.get("ok")
                          else "resting" if resting else "failed"),
                "latency": round(entry["latency"], 1) if entry.get("latency") else None,
                "error": entry.get("error") or "",
                "resting_seconds": resting,
            })
        return out


def reset_health() -> None:
    """Forget every failure -- used by tests and by a fresh key."""
    with _HEALTH_LOCK:
        _HEALTH.clear()


class NvidiaProvider(Provider):
    """NVIDIA's OpenAI-compatible endpoint, tried model by model.

    ``model`` is the whole text chain, comma-separated, because that is what
    the settings panel's model field holds for this provider; the vision chain
    is configured beside it. Each call walks its chain in order, skipping any
    model still resting from a recent failure, and returns the first real
    answer. The ``Reply`` names the model that actually answered, so the
    interface never credits a reading to a model that did not write it.

    A rejected key ends the walk at once: every model shares the key, so the
    other six would only fail the same way, slower.
    """

    name = NVIDIA
    URL = "https://integrate.api.nvidia.com/v1/chat/completions"

    # Per model, then for the whole walk. A model that has not started
    # answering in 45 seconds is overloaded, and the next one is usually
    # faster than waiting; the budget stops seven slow failures from adding
    # up to a ten-minute spinner.
    TEXT_TIMEOUT = 45.0
    VISION_TIMEOUT = 75.0
    BUDGET_SECONDS = 150.0

    def __init__(self, api_key: str, model: str | None = None,
                 max_tokens: int = 1200, vision_models: str | None = None):
        super().__init__(api_key, model, max_tokens)
        self.models = parse_chain(self.model) or list(NVIDIA_TEXT_CHAIN)
        self.vision_models = (parse_chain(vision_models)
                              or list(NVIDIA_VISION_CHAIN))
        # The walk behind the last answer, for the interface's model stamp.
        self.attempts: list[dict] = []

    # -- calls ---------------------------------------------------------------

    def complete(self, system, user, history=None) -> Reply:
        messages = [{"role": "system", "content": system}]
        messages += [{"role": t["role"], "content": t["content"]}
                     for t in (history or [])[-8:]]
        messages.append({"role": "user", "content": user})
        return self._walk(self.models, messages, self.TEXT_TIMEOUT)

    def complete_vision(self, system, user, image_b64, media_type) -> Reply:
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": [
                {"type": "text", "text": user},
                {"type": "image_url", "image_url": {
                    "url": f"data:{media_type};base64,{image_b64}"}},
            ]},
        ]
        return self._walk(self.vision_models, messages, self.VISION_TIMEOUT,
                          what="image")

    def test(self) -> tuple[bool, str]:
        results = self.probe(self.models)
        working = [r["model"] for r in results if r["ok"]]
        if not working:
            first = results[0]["message"] if results else "no models configured"
            return False, f"No NVIDIA model answered. {first}"
        return True, (f"{len(working)} of {len(results)} text models answered; "
                      f"{working[0]} leads the chain.")

    def probe(self, models: list[str], *, vision: bool = False) -> list[dict]:
        """Try every model once, in parallel, and say how each did.

        The Test button runs this rather than a single call, because "the key
        works" is not the question for a chain: the question is which links of
        it are alive today. Results also feed the health table, so a model the
        probe found dead is skipped by the very next real call.
        """
        def one(model: str) -> dict:
            started = time.time()
            try:
                if vision:
                    content = [{"type": "text", "text": "What colour is this image? One word."},
                               {"type": "image_url", "image_url": {"url": _PROBE_IMAGE}}]
                else:
                    content = "Reply with the single word OK."
                body = self._post(self.URL, headers=self._headers(),
                                  payload=self._payload(model, [
                                      {"role": "user", "content": content}], 24),
                                  timeout=40.0)
                self._text(body)
            except ProviderError as exc:
                _record(model, False, error=str(exc), status=exc.status)
                return {"model": model, "ok": False, "message": str(exc),
                        "seconds": round(time.time() - started, 1)}
            latency = time.time() - started
            _record(model, True, latency=latency)
            return {"model": model, "ok": True, "message": "answered",
                    "seconds": round(latency, 1)}

        if not models:
            return []
        with ThreadPoolExecutor(max_workers=min(6, len(models))) as pool:
            return list(pool.map(one, models))

    # -- the walk ------------------------------------------------------------

    def _walk(self, models: list[str], messages: list[dict], timeout: float,
              what: str = "request") -> Reply:
        self.attempts = []
        if not models:
            raise ProviderError("No NVIDIA models are configured.", status=400)

        # Rested models go to the back rather than being dropped: if every
        # model is resting, trying one is still better than refusing outright.
        now = time.time()
        with _HEALTH_LOCK:
            resting = {m for m in models if (_HEALTH.get(m) or {}).get("until", 0) > now}
        order = [m for m in models if m not in resting] + [m for m in models if m in resting]

        deadline = now + self.BUDGET_SECONDS
        for model in order:
            remaining = deadline - time.time()
            if remaining < 5:
                self.attempts.append({"model": model, "ok": False,
                                      "error": "skipped: time budget spent"})
                continue
            started = time.time()
            try:
                body = self._post(self.URL, headers=self._headers(),
                                  payload=self._payload(model, messages, self.max_tokens),
                                  timeout=min(timeout, remaining))
                text = self._text(body)
            except ProviderError as exc:
                _record(model, False, error=str(exc), status=exc.status)
                self.attempts.append({"model": model, "ok": False, "error": str(exc)})
                log.info("NVIDIA %s failed on %s: %s", what, model, exc)
                if exc.status in (401, 403):
                    raise
                continue
            latency = time.time() - started
            _record(model, True, latency=latency)
            self.attempts.append({"model": model, "ok": True,
                                  "seconds": round(latency, 1)})
            return Reply(text, self.name, model)

        tried = "; ".join(f"{a['model'].split('/')[-1]}: {a['error'][:80]}"
                          for a in self.attempts)
        raise ProviderError(f"Every NVIDIA model failed for this {what}. {tried}",
                            status="exhausted")

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}",
                "Accept": "application/json",
                "content-type": "application/json"}

    @staticmethod
    def _payload(model: str, messages: list[dict], max_tokens: int) -> dict:
        payload = {"model": model, "messages": messages,
                   "max_tokens": max_tokens, "temperature": 0.3}
        # The Nemotron 3 family thinks by default, and on this endpoint a
        # thinking model can spend its whole budget reasoning and return an
        # empty answer. Asking for the answer directly is what makes them the
        # fast, reliable head of the chain.
        if re.match(r"nvidia/nemotron-3", model):
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        return payload

    @staticmethod
    def _text(body: dict) -> str:
        choices = body.get("choices") or []
        message = (choices[0].get("message") or {}) if choices else {}
        text = _THINK_BLOCK.sub("", message.get("content") or "").strip()
        if not text:
            # Reasoning without an answer is a failure to answer, not an
            # answer: the reasoning trace is not written for the trader.
            raise ProviderError("The model returned no answer.", status="empty")
        return text


# A 32x32 red PNG, for probing that a vision model accepts images at all
# without spending a real chart on it. (Some models refuse a 1x1 image.)
_PROBE_IMAGE = ("data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAACAAAAAgCAIAAAD8GO2j"
                "AAAAKklEQVR42mO4oKBAU8QwasGoBaMWjFowasGoBaMWjFowasGoBaMWDBULAE7Y"
                "QD2B7wk2AAAAAElFTkSuQmCC")


_CLASSES = {NVIDIA: NvidiaProvider, ANTHROPIC: AnthropicProvider,
            GEMINI: GeminiProvider, OPENAI: OpenAIProvider}


def build(provider: str, api_key: str, model: str | None = None,
          max_tokens: int = 1200, vision_models: str | None = None) -> Provider:
    """A provider by name. Raises for an unknown one rather than guessing."""
    cls = _CLASSES.get((provider or "").lower())
    if cls is None:
        raise ProviderError(f"Unknown provider '{provider}'. "
                            f"Choose one of: {', '.join(PROVIDERS)}.")
    if cls is NvidiaProvider:
        return cls(api_key=api_key, model=model, max_tokens=max_tokens,
                   vision_models=vision_models)
    return cls(api_key=api_key, model=model, max_tokens=max_tokens)
