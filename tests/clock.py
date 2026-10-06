"""Keep wall-clock tests clear of the rollover rule.

Most resolution tests build their candles at "now", because the resolver
ignores candles older than the setup. Since v1.8.0 nothing fills between 16:55
and 18:00 New York and a filled trade is closed at 16:55, so those tests would
pass or fail depending on the hour the suite runs -- exactly what happened the
first time it was run, at 17:19 New York.

Tests that are *about* fills, targets and stops call :func:`outside_rollover`
at the start of their ``main``. The rule itself is tested with fixed dates in
``tests/test_realism.py``, which never calls this.

Patched inside ``main`` rather than at import: test modules import each
other's fixtures, and an import-time patch would silently disable the rule for
every module that borrows one.
"""

from __future__ import annotations

from goldassist.signals import resolve


def outside_rollover() -> None:
    resolve.in_rollover_window = lambda profile, moment: False
    resolve.rollover_deadline = lambda profile, filled_at: None
