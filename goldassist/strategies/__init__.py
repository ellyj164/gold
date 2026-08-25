"""Strategy library.

Importing this package registers every detector. Nothing else needs to know
which modules exist -- ``detect_all`` walks the registry, so adding a strategy
is one new function with a decorator and no wiring anywhere else.
"""

from .base import (Candidate, Context, StrategyDef, ZoneHint, REGISTRY,
                   CONTEXT_WEIGHT, COUNTER, EITHER, WITH_TREND,
                   PATTERN, RANGE, SMC, TREND,
                   describe_all, detect_all, strategy)

# Import for the side effect of registration. Order sets nothing but the
# order strategies appear in the settings list.
from . import smc, trend, ranges, patterns, breakouts, reversals  # noqa: E402,F401

__all__ = [
    "Candidate", "Context", "StrategyDef", "ZoneHint", "REGISTRY",
    "CONTEXT_WEIGHT", "COUNTER", "EITHER", "WITH_TREND",
    "PATTERN", "RANGE", "SMC", "TREND",
    "describe_all", "detect_all", "strategy", "keys",
]


def keys() -> tuple[str, ...]:
    """Every registered strategy key."""
    return tuple(REGISTRY.keys())
