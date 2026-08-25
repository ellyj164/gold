"""Frozen-application entry point.

PyInstaller needs a real script rather than a module, and a bundled app has no
console to print a traceback into. So this wrapper does two things the plain
``python -m goldassist.app`` path does not need: it makes the working directory
irrelevant, and it turns any startup crash into a visible message box instead
of a window that never appears.
"""

from __future__ import annotations

import multiprocessing
import os
import sys
import traceback


def _show_error(title: str, message: str) -> None:
    """Surface a failure to a user who has no console to look at."""
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, message, title, 0x10)
    except Exception:
        print(f"{title}\n\n{message}", file=sys.stderr)


def _ensure_streams() -> None:
    """Guarantee sys.stdout/stderr exist.

    A windowed build has none, and a single print() anywhere in a dependency
    is then a crash. Pointing them at a throwaway buffer costs nothing and
    removes a whole class of startup failure.
    """
    import io

    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None or not hasattr(stream, "write"):
            setattr(sys, name, io.StringIO())


def main() -> int:
    # PyInstaller one-folder builds re-exec for multiprocessing; without this a
    # frozen app can spawn copies of itself instead of starting.
    multiprocessing.freeze_support()
    _ensure_streams()

    try:
        from goldassist.app import main as run_app
    except Exception:
        _show_error(
            "Gold Decision Assistant — startup failed",
            "The application could not load its own modules.\n\n"
            + traceback.format_exc(),
        )
        return 1

    try:
        return run_app(sys.argv[1:])
    except SystemExit as exc:
        return int(exc.code or 0)
    except Exception:
        from goldassist.config import APP_DIR

        _show_error(
            "Gold Decision Assistant — error",
            "The application stopped unexpectedly.\n\n"
            f"{traceback.format_exc()}\n\n"
            f"A full log is at:\n{os.path.join(str(APP_DIR), 'goldassist.log')}",
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
