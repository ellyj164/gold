"""Checks for the analyst layer: providers, key handling and the narrative.

Three properties are pinned harder than the rest.

**A key never leaves the machine except to its own provider.** Nothing the API
returns to the interface may contain a key, and nothing a provider puts in an
error message may either. Providers echo request detail back in error bodies,
so this is not hypothetical.

**Offline is a mode, not a failure.** The offline analyst reads the live
engine output and must produce a real reading with no key present. It is the
floor the application stands on, not a degraded state.

**The analysis must reference what the engine measured.** The narrative went
several versions explaining setups while ignoring the volume profile, the
dealing range and the breakers that the scoring engine was already using —
which is how "regenerate" came to produce the same paragraph every time.

Run with:  python -m tests.test_ai_settings
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from goldassist.ai import credentials, explain, providers

FAILURES: list[str] = []

SECRET = "sk-ant-SECRET0000000000000000deadbeef"


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        FAILURES.append(name)


def isolate() -> Path:
    """Point the key store at a scratch file, never the trader's real one."""
    path = Path(tempfile.mkdtemp(prefix="goldassist-ai-")) / "ai.json"
    credentials.STORE_PATH = path
    return path


# --- key handling ----------------------------------------------------------

def test_keys_never_escape():
    print("credentials: a key never leaves this machine")
    store = isolate()

    credentials.set_provider("anthropic")
    credentials.set_key("anthropic", SECRET)
    described = credentials.describe()

    blob = json.dumps(described)
    check("describe() never contains the key", SECRET not in blob)
    check("describe() reports that a key is set",
          described["providers"][0]["has_key"])
    check("the hint is a mask, not the key",
          SECRET not in described["providers"][0]["key_hint"]
          and "···" in described["providers"][0]["key_hint"])
    check("the key is readable back for actual use",
          credentials.get_key("anthropic") == SECRET)
    check("the store file is named in the settings", described["store_path"])
    check("the key really is on disk", SECRET in store.read_text(encoding="utf-8"))

    # An error from a provider must not carry the key onward to a log.
    dirty = f"request failed for key {SECRET} at endpoint"
    check("provider errors are redacted",
          SECRET not in providers._redact(dirty, SECRET))

    credentials.set_key("anthropic", "")
    check("clearing a key removes it",
          not credentials.describe()["providers"][0]["has_key"])
    check("a cleared key is gone from disk",
          SECRET not in store.read_text(encoding="utf-8"))


def test_mode_and_provider():
    print("credentials: mode and provider")
    isolate()
    check("the default mode is offline", credentials.get_mode() == "offline")
    check("the default provider is Claude", credentials.get_provider() == "anthropic")

    credentials.set_mode("online")
    credentials.set_provider("gemini")
    check("mode persists", credentials.get_mode() == "online")
    check("provider persists", credentials.get_provider() == "gemini")

    for bad, setter in (("sideways", credentials.set_mode),
                        ("hotdog", credentials.set_provider)):
        try:
            setter(bad)
            check(f"'{bad}' is rejected", False, "no error raised")
        except ValueError:
            check(f"'{bad}' is rejected", True)

    # Online with no key must report itself as not ready, rather than
    # claiming to be online and falling back on every single cycle.
    described = credentials.describe()
    check("online without a key is not ready", not described["online_ready"])
    check("and says why", "API key" in described["note"], described["note"])

    credentials.set_key("gemini", "AIza-test-key-000000")
    check("online with a key is ready", credentials.describe()["online_ready"])


def test_models_are_editable():
    print("credentials: models")
    isolate()
    for name in providers.PROVIDERS:
        check(f"{name} has a default model",
              bool(credentials.get_model(name)), credentials.get_model(name))
    credentials.set_model("openai", "gpt-5-some-future-name")
    check("a model can be overridden",
          credentials.get_model("openai") == "gpt-5-some-future-name")
    credentials.set_model("openai", "")
    check("clearing restores the default",
          credentials.get_model("openai") == providers.DEFAULT_MODELS["openai"])


# --- providers -------------------------------------------------------------

def test_provider_construction():
    print("providers: construction")
    for name in providers.PROVIDERS:
        p = providers.build(name, "test-key")
        check(f"{name} builds", p.name == name)
        check(f"{name} takes the default model when none is given",
              p.model == providers.DEFAULT_MODELS[name], p.model)
        check(f"{name} can explain a chart image",
              "complete_vision" in type(p).__dict__)

    try:
        providers.build("wrongprovider", "k")
        check("an unknown provider is refused", False, "no error raised")
    except providers.ProviderError as exc:
        check("an unknown provider is refused", "Unknown provider" in str(exc))

    ok, message = providers.build("openai", "").test()
    check("a missing key fails before any network call", not ok)
    check("and says which provider needs one", "OpenAI" in message, message)


# --- the offline analyst ---------------------------------------------------

def analysis_fixture() -> dict:
    """An analysis carrying the layers the narrative used to ignore."""
    return {
        "price": 4660.0,
        "regime": {"regime": "strong bullish trend"},
        "alignment": {"direction": "bullish", "agreement": 1.0,
                      "reads": {"H1": "bullish", "M15": "bullish"}},
        "levels": {}, "liquidity": {},
        "events": {
            "available": True,
            "next_high_impact": {"title": "US CPI (August)", "minutes_until": 45},
        },
        "timeframes": {
            "M15": {
                "volume_profile": {
                    "available": True, "poc": 4638.88, "val": 4600.0, "vah": 4680.0,
                    "location": "inside value",
                    "meaning": "inside the value area, where the market considers price fair",
                    "nearest_low_volume_node": 4620.5,
                },
                "dealing_range": {
                    "available": True, "low": 4605.2, "high": 4668.3,
                    "equilibrium": 4636.75, "position": 0.87,
                    "zone": "premium", "favours": "bearish",
                },
                "breakers": {
                    "available": True, "count": 1,
                    "nearest_bullish": {"bottom": 4633.75, "top": 4650.2,
                                        "price_inside": False},
                },
            },
        },
    }


def decision_fixture() -> dict:
    return {
        "decision": "WAIT", "direction": "bullish", "score": 53.0, "price": 4660.0,
        "opposing_score": 21.0,
        "regime": {"regime": "strong bullish trend"},
        "confluence": {"bullish": {"evidence": [], "penalties": []}},
        "waiting_for": ["price reacting from a supply/demand zone"],
        "invalidation": {"description": "a close below 4634.07"},
        "targets": [{"price": 4697.11, "basis": "H4 resistance"}],
        "risk": {"reward_risk": 1.8, "warnings": []},
    }


def test_offline_narrative_reads_the_new_layers():
    print("offline analyst: relevance")
    text = explain.decision_narrative(decision_fixture(), analysis_fixture())

    check("it produces a real reading with no API key", len(text) > 400, str(len(text)))

    # Volume profile: where the market actually traded.
    check("it reports the point of control", "4638.88" in text)
    check("it reports the value area", "4600.00" in text and "4680.00" in text)
    check("it names the thin-volume gap", "4620.50" in text, text[:0])

    # Premium/discount: whether this is a good price at all.
    check("it prices the entry against the dealing range", "4605.20" in text)
    check("it names the half price sits in", "premium" in text)
    check("it warns when the price is the wrong half for the direction",
          "wrong half" in text, "no premium/discount warning")

    # Breakers.
    check("it reports a breaker", "4633.75" in text)

    # Events outrank the technical read.
    check("it warns about an imminent release", "US CPI (August)" in text)
    check("it says why that matters", "spreads widen" in text)

    # The standing rule: never a probability of profit, never an instruction.
    check("it still refuses to claim a probability",
          "not a probability of profit" in text)


def test_event_warning_has_a_horizon():
    print("offline analyst: the event horizon")
    analysis = analysis_fixture()

    analysis["events"]["next_high_impact"]["minutes_until"] = 45
    check("a release 45 minutes out is warned about",
          explain._event_sentence(analysis) is not None)

    # Far enough away and the warning is noise rather than information.
    analysis["events"]["next_high_impact"]["minutes_until"] = 3000
    check("a release two days out is not warned about",
          explain._event_sentence(analysis) is None)

    analysis["events"]["available"] = False
    check("no calendar means no warning, not a crash",
          explain._event_sentence(analysis) is None)


def test_narrative_survives_a_bare_analysis():
    print("offline analyst: degradation")
    # Every new layer absent -- an older database, a cold start, a timeframe
    # that could not be analysed. The narrative must still be produced.
    bare = {"price": 4660.0, "regime": {"regime": "ranging"},
            "alignment": {}, "levels": {}, "liquidity": {}, "timeframes": {}}
    text = explain.decision_narrative(decision_fixture(), bare)
    check("a narrative is still produced", len(text) > 100, str(len(text)))
    check("it does not print None", "None" not in text)
    check("location sentences are simply omitted",
          explain._location_sentences(bare, "bullish") == [])


def main() -> int:
    test_keys_never_escape()
    test_mode_and_provider()
    test_models_are_editable()
    test_provider_construction()
    test_offline_narrative_reads_the_new_layers()
    test_event_warning_has_a_horizon()
    test_narrative_survives_a_bare_analysis()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) failed:")
        for name in FAILURES:
            print(f"  - {name}")
        return 1
    print("All analyst-layer checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
