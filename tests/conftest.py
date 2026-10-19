"""Make the check()-style suites fail under pytest when a check fails.

Most test modules here record failures with ``check()`` into a module-level
``FAILURES`` list and report them from ``main()`` when run directly. Under
pytest nothing read that list, so a failed check printed FAIL and the test
still passed. This fixture fails the test whose checks added to it.
"""

import pytest


@pytest.fixture(autouse=True)
def _checks_must_pass(request):
    failures = getattr(request.module, "FAILURES", None)
    before = len(failures) if isinstance(failures, list) else 0
    yield
    if isinstance(failures, list) and len(failures) > before:
        pytest.fail("check(s) failed: " + "; ".join(failures[before:]), pytrace=False)
