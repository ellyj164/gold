"""Checks for the NVIDIA model chain, the chart read and the signal audit.

No network: ``httpx.post`` is replaced by a script of canned responses, one
per model, so each property below is tested against exactly the failure it is
about.

**The first model that answers wins, and the reply names it.** A timeout, a
rate limit, a retired model and an empty answer each move on to the next
model; the interface's stamp must credit the model that actually wrote it.

**A failed model rests.** The next call does not wait on it again first.

**A rejected key stops the walk.** Every model shares the key.

**The chart read never fails the button.** No vision model -> facts-only read;
no online model at all -> the engine's own narrative.

**The signal audit sees what the signal recorded.** Its reasons and the
agreeing strategies' evidence reach the model, and offline it still shows them.

Run with:  python -m tests.test_ai_chain
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import httpx

from goldassist.ai import client as ai_client
from goldassist.ai import credentials, providers
from goldassist.config import AIConfig

FAILURES: list[str] = []

KEY = "nvapi-TEST000000000000000000000000000000"


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        FAILURES.append(name)


def isolate() -> Path:
    path = Path(tempfile.mkdtemp(prefix="goldassist-ai-")) / "ai.json"
    credentials.STORE_PATH = path
    providers.reset_health()
    return path


class FakeResponse:
    def __init__(self, status: int, body: dict):
        self.status_code = status
        self._body = body
        self.text = json.dumps(body)

    def json(self):
        return self._body


def answer(text: str) -> FakeResponse:
    return FakeResponse(200, {"choices": [{"message": {"content": text}}]})


class Script:
    """Stands in for httpx.post: one scripted outcome per model."""

    def __init__(self, outcomes: dict):
        self.outcomes = outcomes
        self.calls: list[dict] = []

    def __call__(self, url, headers=None, json=None, timeout=None):
        self.calls.append(json)
        outcome = self.outcomes.get(json["model"], answer("default answer"))
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def patch(script: Script):
    original = providers.httpx.post
    providers.httpx.post = script
    return original


# --- the chain ---------------------------------------------------------------

def test_failover_order_and_credit():
    print("nvidia chain: failover")
    isolate()
    chain = ["m/one", "m/two", "m/three", "m/four"]
    script = Script({
        "m/one": httpx.ReadTimeout("slow"),
        "m/two": FakeResponse(429, {"detail": "rate limited"}),
        "m/three": answer("<think>private reasoning</think>Structure is bullish on H4."),
    })
    original = patch(script)
    try:
        nv = providers.NvidiaProvider(KEY, model=", ".join(chain))
        reply = nv.complete("system", "user")
    finally:
        providers.httpx.post = original

    check("the first model that answers wins", reply.model == "m/three", reply.model)
    check("the model's hidden reasoning is stripped",
          reply.text == "Structure is bullish on H4.", reply.text)
    check("later models are not called", [c["model"] for c in script.calls]
          == ["m/one", "m/two", "m/three"], str([c["model"] for c in script.calls]))
    check("the walk is reported", [a["ok"] for a in nv.attempts] == [False, False, True])

    health = {h["model"]: h for h in providers.model_health(chain)}
    check("a timed-out model rests", health["m/one"]["state"] == "resting")
    check("a rate-limited model rests", health["m/two"]["state"] == "resting")
    check("the answering model is marked ok", health["m/three"]["state"] == "ok")
    check("an untried model says so", health["m/four"]["state"] == "untested")


def test_resting_models_go_last():
    print("nvidia chain: a resting model is not waited on first")
    isolate()
    providers._record("m/one", False, error="timeout", status="timeout")
    script = Script({})
    original = patch(script)
    try:
        reply = providers.NvidiaProvider(KEY, model="m/one, m/two").complete("s", "u")
    finally:
        providers.httpx.post = original
    check("the healthy model is tried first", script.calls[0]["model"] == "m/two")
    check("and answers", reply.model == "m/two")


def test_everything_resting_still_tries():
    print("nvidia chain: all resting is not a refusal")
    isolate()
    for m in ("m/one", "m/two"):
        providers._record(m, False, error="timeout", status="timeout")
    script = Script({})
    original = patch(script)
    try:
        reply = providers.NvidiaProvider(KEY, model="m/one, m/two").complete("s", "u")
    finally:
        providers.httpx.post = original
    check("a resting model is still tried when nothing else is left",
          reply.model == "m/one", reply.model)


def test_rejected_key_stops_the_walk():
    print("nvidia chain: a bad key ends the walk at once")
    isolate()
    script = Script({"m/one": FakeResponse(401, {"detail": "Unauthorized"})})
    original = patch(script)
    try:
        nv = providers.NvidiaProvider(KEY, model="m/one, m/two, m/three")
        try:
            nv.complete("s", "u")
            raised = None
        except providers.ProviderError as exc:
            raised = exc
    finally:
        providers.httpx.post = original
    check("it raises", raised is not None)
    check("as a key problem", raised is not None and raised.status == 401)
    check("without trying the other models", len(script.calls) == 1, str(len(script.calls)))
    check("and never with the key in the message",
          raised is None or KEY not in str(raised))


def test_empty_answers_and_exhaustion():
    print("nvidia chain: empty answers fail over; exhaustion is explained")
    isolate()
    script = Script({
        "m/one": answer(""),
        "m/two": answer("<think>only thinking</think>"),
        "m/three": FakeResponse(410, {"detail": "model retired"}),
    })
    original = patch(script)
    try:
        try:
            providers.NvidiaProvider(KEY, model="m/one, m/two, m/three").complete("s", "u")
            raised = None
        except providers.ProviderError as exc:
            raised = exc
    finally:
        providers.httpx.post = original
    check("an all-failed chain raises", raised is not None)
    check("and says it was exhausted", raised is not None and raised.status == "exhausted")
    check("naming what each model did",
          raised is not None and "two" in str(raised) and "retired" in str(raised),
          str(raised))
    retired = {h["model"]: h for h in providers.model_health(["m/three"])}["m/three"]
    check("a retired model rests for hours", retired["resting_seconds"] > 3600)


def test_payload_shape():
    print("nvidia chain: what is sent")
    isolate()
    script = Script({})
    original = patch(script)
    try:
        nv = providers.NvidiaProvider(
            KEY, model="nvidia/nemotron-3-super-120b-a12b, z-ai/glm-5.3",
            vision_models="v/see-one, v/see-two")
        nv.complete("s", "u")
        nv.complete_vision("s", "u", "QUJD", "image/jpeg")
    finally:
        providers.httpx.post = original
    text_call, vision_call = script.calls
    check("Nemotron 3 is asked to answer without thinking",
          text_call.get("chat_template_kwargs") == {"enable_thinking": False})
    check("the image goes to the vision chain", vision_call["model"] == "v/see-one")
    content = vision_call["messages"][-1]["content"]
    check("as an OpenAI-style data URL",
          any(part.get("type") == "image_url"
              and part["image_url"]["url"].startswith("data:image/jpeg;base64,")
              for part in content))
    other = providers.NvidiaProvider._payload("z-ai/glm-5.3", [], 10)
    check("other models get no Nemotron switch", "chat_template_kwargs" not in other)


def test_settings_carry_both_chains():
    print("credentials: NVIDIA chains are stored and described")
    isolate()
    credentials.set_key("nvidia", KEY)
    credentials.set_model("nvidia", "a/one, a/two")
    credentials.set_vision_models("nvidia", "v/one")
    described = credentials.describe()
    nv = next(p for p in described["providers"] if p["provider"] == "nvidia")
    check("NVIDIA is described as a chain", nv["chain"] is True)
    check("its vision chain is reported", nv["vision_models"] == "v/one")
    check("with a health row per model",
          [h["model"] for h in nv["health"]["text"]] == ["a/one", "a/two"])
    check("the key never appears", KEY not in json.dumps(described))
    credentials.set_vision_models("nvidia", "")
    check("clearing the vision chain restores the default",
          credentials.get_vision_models("nvidia") == providers.DEFAULT_VISION_MODELS["nvidia"])
    check("parse_chain keeps order and drops duplicates",
          providers.parse_chain("a, b\nc, a,, ") == ["a", "b", "c"])


# --- the analyst ---------------------------------------------------------------

# The analyst-layer suite's fixtures: a realistic engine output, with Gold
# at 4660.00, so these checks run against the shape the engine really emits.
from tests.test_ai_settings import analysis_fixture, decision_fixture  # noqa: E402


def online(chain: str, vision: str) -> ai_client.AIAnalyst:
    isolate()
    credentials.set_provider("nvidia")
    credentials.set_mode("online")
    credentials.set_key("nvidia", KEY)
    credentials.set_model("nvidia", chain)
    credentials.set_vision_models("nvidia", vision)
    return ai_client.AIAnalyst(AIConfig())


def test_chart_read_falls_back_to_facts():
    print("chart read: no vision model -> facts-only read")
    analyst = online("t/one", "v/one, v/two")
    script = Script({
        "v/one": FakeResponse(503, {"detail": "overloaded"}),
        "v/two": httpx.ReadTimeout("slow"),
        "t/one": answer("**Read** H1 is bearish below 4160.00."),
    })
    original = patch(script)
    try:
        result = analyst.chart_insight("H1", decision_fixture(), analysis_fixture(),
                                       image_bytes=b"\xff\xd8fakejpeg",
                                       media_type="image/jpeg")
    finally:
        providers.httpx.post = original
    check("the read still arrives", "bearish" in result["text"], result.get("text", ""))
    check("from the text chain", result.get("model") == "t/one", str(result.get("model")))
    check("without claiming to have seen the image", result.get("used_image") is False)
    check("and it says so", "measurements only" in (result.get("note") or ""))
    check("the facts were in the prompt",
          "4660.0" in script.calls[-1]["messages"][-1]["content"])


def test_chart_read_uses_the_image():
    print("chart read: a vision model reads the chart")
    analyst = online("t/one", "v/one")
    script = Script({"v/one": answer("Impulsive bearish leg into H1 demand.")})
    original = patch(script)
    try:
        result = analyst.chart_insight("H1", decision_fixture(), analysis_fixture(),
                                       image_bytes=b"\xff\xd8fakejpeg")
    finally:
        providers.httpx.post = original
    check("the vision model answers", result.get("model") == "v/one")
    check("and is credited with the image", result.get("used_image") is True)
    check("the facts travel with the image",
          "4660.0" in script.calls[0]["messages"][-1]["content"][0]["text"])


def test_chart_read_offline():
    print("chart read: offline still gives the engine's narrative")
    isolate()
    analyst = ai_client.AIAnalyst(AIConfig())
    result = analyst.chart_insight("H1", decision_fixture(), analysis_fixture())
    check("offline is reported as such", result["source"] == "offline")
    check("with text", len(result["text"]) > 50, str(len(result["text"])))


def signal_fixture() -> dict:
    return {
        "id": 7, "uid": "044584926e05", "strategy": "sweep_reversal",
        "strategy_name": "Liquidity Sweep Reversal", "direction": "bearish",
        "action": "SELL", "fired_local": "2026-10-16 14:37", "score": 78.0,
        "entry_low": 4154.0, "entry_high": 4156.5, "stop": 4161.2, "tp1": 4142.0,
        "trigger_text": "M5 bearish structure shift after a liquidity sweep",
        "reasons": ["M5 buy-side sweep at 4155.58", "structure shifted after the sweep"],
        "confirmations": [{"key": "round_number", "name": "Round Number Reaction",
                           "evidence": ["price is at the 4155 handle"]}],
        "card": {"entry_rationale": "retest of the swept high",
                 "risks": ["NFP in 3 hours"],
                 "history": [{"time": "t", "status": "ARMED", "score": 74, "note": "armed"}],
                 "features": {"large": "left out"}},
    }


def test_signal_audit_sends_the_record():
    print("signal audit: the recorded facts reach the model")
    analyst = online("t/one", "v/one")
    script = Script({"t/one": answer("**Why it fired** ...")})
    original = patch(script)
    try:
        result = analyst.review_signal(signal_fixture(), decision_fixture(),
                                       analysis_fixture(), question="Is it still valid?")
    finally:
        providers.httpx.post = original
    sent = script.calls[0]["messages"][-1]["content"]
    check("it answers", result.get("model") == "t/one")
    check("the reasons are sent", "M5 buy-side sweep at 4155.58" in sent)
    check("the agreeing strategy's evidence is sent", "4155 handle" in sent)
    check("the flagged risks are sent", "NFP in 3 hours" in sent)
    check("the market now is sent", "4660.0" in sent)
    check("the trader's question is sent", "Is it still valid?" in sent)
    check("feature vectors are left out", "left out" not in sent)


def test_signal_audit_offline():
    print("signal audit: offline shows the signal's own record")
    isolate()
    analyst = ai_client.AIAnalyst(AIConfig())
    result = analyst.review_signal(signal_fixture(), decision_fixture(), analysis_fixture())
    check("offline is reported", result["source"] == "offline")
    check("the reasons are listed", "4155.58" in result["text"], result["text"])
    check("the agreeing strategy is listed", "Round Number Reaction" in result["text"])


def main() -> int:
    for test in (test_failover_order_and_credit, test_resting_models_go_last,
                 test_everything_resting_still_tries, test_rejected_key_stops_the_walk,
                 test_empty_answers_and_exhaustion, test_payload_shape,
                 test_settings_carry_both_chains, test_chart_read_falls_back_to_facts,
                 test_chart_read_uses_the_image, test_chart_read_offline,
                 test_signal_audit_sends_the_record, test_signal_audit_offline):
        test()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) failed:")
        for name in FAILURES:
            print(f"  - {name}")
        return 1
    print("All AI chain checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
