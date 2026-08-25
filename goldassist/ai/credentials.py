"""Where the API keys live, and the rules for handling them.

The keys are stored in ``~/.goldassist/ai.json``, separate from
``config.json``. That separation is deliberate: the config file is safe to
read, copy into a bug report or paste into a chat when something is wrong,
and it must stay that way. Secrets in the same file would eventually be
shared by accident.

Three rules, enforced here rather than remembered at each call site:

**A key is never returned in full.** :func:`describe` is the only thing the
API layer may send to the interface, and it emits a masked hint
(``sk-ant···4f21``) plus a boolean. The settings panel shows whether a key is
set, not what it is. A user who wants to check a key re-enters it.

**A key is never logged.** Nothing in this module logs a value, and
:mod:`goldassist.ai.providers` strips keys out of provider error text before
it reaches a log or a screen.

**The file is written for this user only.** On Windows that means an ACL
granting the current account and nothing else; the POSIX branch sets 0600.
Best effort — a failure to tighten permissions is reported, never fatal,
because refusing to save the key would be the worse outcome.

The environment variables still work. A key set in the environment is used
when the store has none, so an existing setup keeps running untouched, and
the store takes precedence when both exist because it is the one the trader
just typed into the panel.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from pathlib import Path

from ..config import APP_DIR
from . import providers

log = logging.getLogger(__name__)

STORE_PATH = APP_DIR / "ai.json"

# Characters of a key shown either side of the mask. Enough to tell two keys
# apart, far too few to reconstruct one.
MASK_HEAD = 6
MASK_TAIL = 4


def _read() -> dict:
    if not STORE_PATH.exists():
        return {}
    try:
        data = json.loads(STORE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError) as exc:
        # Deliberately does not include the file's contents.
        log.warning("Could not read the AI key store: %s", type(exc).__name__)
        return {}


def _write(data: dict) -> None:
    STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STORE_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    _restrict(STORE_PATH)


def _restrict(path: Path) -> None:
    """Make the file readable by this user only, on either platform."""
    try:
        if sys.platform == "win32":
            user = os.environ.get("USERNAME")
            if not user:
                return
            # Break inheritance, then grant only this account.
            subprocess.run(["icacls", str(path), "/inheritance:r"],
                           capture_output=True, timeout=15, check=False)
            subprocess.run(["icacls", str(path), "/grant:r", f"{user}:F"],
                           capture_output=True, timeout=15, check=False)
        else:
            path.chmod(0o600)
    except Exception:
        # Never fatal: a key that is saved with loose permissions is still
        # better than a key the application refused to save at all.
        log.warning("Could not restrict permissions on the AI key store.")


def mask(key: str | None) -> str:
    """``sk-ant···4f21`` — enough to recognise, never enough to use."""
    key = (key or "").strip()
    if not key:
        return ""
    if len(key) <= MASK_HEAD + MASK_TAIL:
        return "···"
    return f"{key[:MASK_HEAD]}···{key[-MASK_TAIL:]}"


def get_key(provider: str) -> str:
    """The key for a provider: the stored one, else the environment's."""
    stored = (_read().get("keys") or {}).get(provider)
    if stored:
        return str(stored).strip()
    env = providers.ENV_VARS.get(provider)
    return (os.environ.get(env, "") if env else "").strip()


def key_source(provider: str) -> str:
    """Where the key came from, so the panel can say so."""
    if (_read().get("keys") or {}).get(provider):
        return "saved"
    env = providers.ENV_VARS.get(provider)
    if env and os.environ.get(env):
        return "environment"
    return "none"


def set_key(provider: str, key: str | None) -> None:
    """Store a key, or forget it when passed an empty value."""
    if provider not in providers.PROVIDERS:
        raise ValueError(f"Unknown provider '{provider}'.")
    data = _read()
    keys = dict(data.get("keys") or {})
    key = (key or "").strip()
    if key:
        keys[provider] = key
    else:
        keys.pop(provider, None)
    data["keys"] = keys
    _write(data)


def get_model(provider: str) -> str:
    stored = (_read().get("models") or {}).get(provider)
    return str(stored).strip() if stored else providers.DEFAULT_MODELS.get(provider, "")


def set_model(provider: str, model: str | None) -> None:
    data = _read()
    models = dict(data.get("models") or {})
    model = (model or "").strip()
    if model:
        models[provider] = model
    else:
        models.pop(provider, None)
    data["models"] = models
    _write(data)


def get_mode() -> str:
    """``online`` or ``offline`` — which analyst the app should use."""
    mode = str(_read().get("mode") or "").strip().lower()
    return mode if mode in ("online", "offline") else "offline"


def set_mode(mode: str) -> None:
    if mode not in ("online", "offline"):
        raise ValueError("Mode must be 'online' or 'offline'.")
    data = _read()
    data["mode"] = mode
    _write(data)


def get_provider() -> str:
    """Which provider online mode should use."""
    name = str(_read().get("provider") or "").strip().lower()
    return name if name in providers.PROVIDERS else providers.ANTHROPIC


def set_provider(provider: str) -> None:
    if provider not in providers.PROVIDERS:
        raise ValueError(f"Unknown provider '{provider}'.")
    data = _read()
    data["provider"] = provider
    _write(data)


def describe() -> dict:
    """Everything the settings panel may see. No key values, ever."""
    mode = get_mode()
    active = get_provider()
    entries = []
    for name in providers.PROVIDERS:
        source = key_source(name)
        entries.append({
            "provider": name,
            "label": providers.PROVIDER_LABELS[name],
            "model": get_model(name),
            "default_model": providers.DEFAULT_MODELS.get(name, ""),
            "has_key": source != "none",
            "key_source": source,
            "key_hint": mask(get_key(name)) if source != "none" else "",
            "env_var": providers.ENV_VARS.get(name, ""),
        })

    ready = any(e["has_key"] for e in entries if e["provider"] == active)
    return {
        "mode": mode,
        "provider": active,
        "providers": entries,
        # Whether online mode can actually run right now, and why not if it
        # cannot -- the panel should never claim to be online while falling
        # back on every cycle.
        "online_ready": ready,
        "note": ("" if ready else
                 f"Online mode needs an API key for "
                 f"{providers.PROVIDER_LABELS.get(active, active)}."),
        "store_path": str(STORE_PATH),
    }
