"""Desktop entry point.

Starts the local API server on a background thread and opens the terminal in a
native window (Edge WebView2 via pywebview). Falls back to the default browser
when no webview runtime is available, and can be run headless with --no-window
for development.

Usage:
    python -m goldassist.app                 # desktop window
    python -m goldassist.app --browser       # open in the default browser
    python -m goldassist.app --no-window     # server only
"""

from __future__ import annotations

import argparse
import logging
import os
import socket
import sys
import threading
import time
import webbrowser

import uvicorn

from . import timeutil
from .api.server import create_app
from .config import APP_DIR, AppConfig

log = logging.getLogger(__name__)


def _configure_logging(verbose: bool) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    _trim_log(APP_DIR / "goldassist.log")

    # The log file is the only handler guaranteed to work: a windowed build has
    # no console, and attaching a StreamHandler to a None stdout raises.
    handlers: list[logging.Handler] = [
        logging.FileHandler(APP_DIR / "goldassist.log", encoding="utf-8")
    ]
    if sys.stdout is not None and getattr(sys.stdout, "write", None):
        handlers.insert(0, logging.StreamHandler(sys.stdout))

    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=handlers,
    )
    # uvicorn's access log is noise for a single-user local app that polls
    # itself every five seconds. Note this has to be re-applied *after*
    # uvicorn.Config is constructed -- see _quieten_uvicorn.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


def _quieten_uvicorn() -> None:
    """Undo uvicorn's own logger configuration.

    ``uvicorn.Config`` sets ``uvicorn.access`` back to the server's log level
    when it is constructed, overriding what ``_configure_logging`` asked for.
    The result was an access line for every poll of every endpoint, five
    seconds apart, all day: a ten-megabyte log in which a real error was
    unfindable. Called after the Config exists, this makes the intent stick.
    """
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


def _trim_log(path, limit_bytes: int = 8 * 1024 * 1024) -> None:
    """Keep the log from growing without bound across sessions.

    A desktop app that runs all day has no logrotate behind it. Rather than
    lose the history entirely, the current file is rolled to `.1` once it gets
    large and a fresh one is started.
    """
    try:
        if path.exists() and path.stat().st_size > limit_bytes:
            previous = path.with_suffix(path.suffix + ".1")
            previous.unlink(missing_ok=True)
            path.rename(previous)
    except OSError:
        pass


def _free_port(host: str, preferred: int) -> int:
    """Use the configured port when free, otherwise let the OS choose one."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((host, preferred))
            return preferred
        except OSError:
            pass
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def _wait_until_serving(host: str, port: int, thread: threading.Thread,
                        timeout: float = 240.0) -> bool:
    """Block until the server accepts connections.

    Startup is not instant: the service connects to MetaTrader 5, downloads a
    history window for five timeframes, and runs a full analysis before uvicorn
    binds the port. On a cold terminal that can take a couple of minutes, so the
    ceiling here is generous.

    The real stop condition is the server thread dying -- that means startup
    failed and no amount of further waiting will help.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not thread.is_alive():
            return False
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.4)
            if sock.connect_ex((host, port)) == 0:
                return True
        time.sleep(0.3)
    return False


# The window title is also the handle a second launch uses to find the first
# one, so it is defined once here rather than inline at create_window.
WINDOW_TITLE = "XAUUSD \u2014 AI Trading Decision Assistant"

# Any process holding this name is an already-running copy of the application.
MUTEX_NAME = "Global\\GoldDecisionAssistant.SingleInstance"


def _claim_single_instance():
    """Return a mutex handle if this is the only copy running, else None.

    Why this exists: the application is a *server* with a window in front of
    it, not a plain window. Launching it twice therefore does not just show two
    windows -- it starts a second uvicorn, a second MetaTrader 5 session and a
    second analysis loop, and because port 8765 is already taken the second one
    binds a random high port instead. That is where a stray
    ``http://127.0.0.1:61774`` comes from: not a browser fallback, but a second
    server nobody meant to start. Two analysis loops also means two copies of
    every signal written to one database.

    Holding a named mutex for the lifetime of the process makes the second
    launch cheap and obvious: it finds the first window, raises it, and exits.
    """
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return None

    ERROR_ALREADY_EXISTS = 183
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    handle = kernel32.CreateMutexW(None, True, MUTEX_NAME)
    if not handle:
        return None
    if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        return None
    return handle


def _focus_existing_window() -> bool:
    """Bring the already-running copy to the front. True if one was found."""
    try:
        import ctypes

        user32 = ctypes.windll.user32
        hwnd = user32.FindWindowW(None, WINDOW_TITLE)
        if not hwnd:
            return False
        SW_RESTORE = 9
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)
        user32.SetForegroundWindow(hwnd)
        return True
    except Exception:
        return False


def _open_window(url: str) -> bool:
    """Open the native desktop window. False if no runtime could do it.

    Every failure is logged with the reason. Previously a webview that could
    not start fell through to ``webbrowser.open`` in silence, which is how an
    application that is supposed to be a standalone window ends up presenting
    itself as a URL in a browser tab with nothing anywhere explaining why.
    """
    try:
        import webview
    except ImportError:
        log.warning("pywebview is not installed; falling back to the browser. "
                    "Run: pip install pywebview")
        return False

    # WebView2 blocks audio until the user interacts with the page, which would
    # mute the first signal alert -- the one that matters most. This is a local,
    # single-user app showing only its own content, so letting it chime
    # unprompted is safe.
    os.environ.setdefault(
        "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS",
        "--autoplay-policy=no-user-gesture-required",
    )

    try:
        from webview.guilib import initialize

        backend = initialize()
        log.info("Desktop window backend: %s",
                 getattr(backend, "renderer", backend))
    except Exception as exc:
        log.error(
            "No desktop window runtime is available (%s). The usual cause on "
            "Windows is a missing Microsoft Edge WebView2 Runtime, available "
            "from https://developer.microsoft.com/microsoft-edge/webview2/. "
            "Opening in the browser instead.", exc)
        return False

    try:
        webview.create_window(
            WINDOW_TITLE,
            url,
            width=1680,
            height=980,
            min_size=(1180, 720),
            background_color="#0b0e14",
        )
        webview.start()
        return True
    except Exception:
        log.exception("The desktop window closed with an error.")
        return False


def _startup_error(message: str) -> None:
    """Show a startup failure to a user who has no console to read."""
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(
            None, message, "Gold Decision Assistant", 0x10
        )
    except Exception:
        print(message, file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AI Gold Trading Decision Assistant")
    parser.add_argument("--browser", action="store_true",
                        help="Open in the default browser instead of a desktop window")
    parser.add_argument("--no-window", action="store_true",
                        help="Run the server only, without opening any UI")
    parser.add_argument("--port", type=int, default=None, help="Override the listening port")
    parser.add_argument("--verbose", action="store_true", help="Debug logging")
    args = parser.parse_args(argv)

    _configure_logging(args.verbose)

    # Refuse to be the second copy. Checked before anything expensive: the
    # point is to not open a second MT5 session or a second server at all.
    lock = _claim_single_instance()
    if lock is None and not args.no_window:
        if _focus_existing_window():
            log.info("Already running; brought the existing window to the front.")
        else:
            _startup_error(
                "Gold Decision Assistant is already running.\n\n"
                "Look for its window, or for its icon in the taskbar.")
        return 0

    config = AppConfig.load()
    timeutil.configure(config.display_utc_offset)
    host = config.host
    port = _free_port(host, args.port or config.port)
    url = f"http://{host}:{port}"
    if port != (args.port or config.port):
        log.warning(
            "Port %s was already in use, so this instance is on %s. Something "
            "else is holding the usual port.", args.port or config.port, port)

    app = create_app(config)
    # log_config=None is load-bearing: uvicorn's default configuration builds a
    # colourised formatter that calls sys.stdout.isatty(), which is fatal in a
    # windowed build where stdout is None. Our own handlers are already set up.
    server = uvicorn.Server(
        uvicorn.Config(app, host=host, port=port, log_level="info", log_config=None)
    )
    _quieten_uvicorn()
    thread = threading.Thread(target=server.run, name="uvicorn", daemon=True)
    thread.start()

    log.info("Starting up: connecting to MetaTrader 5 and priming history...")
    if not _wait_until_serving(host, port, thread):
        log.error(
            "The application could not start. The most common cause is that the "
            "MetaTrader 5 terminal is not running or not logged in. Full log: %s",
            APP_DIR / "goldassist.log",
        )
        _startup_error(
            "The application could not start.\n\n"
            "Check that MetaTrader 5 is open and logged in, then try again.\n\n"
            f"Full log:\n{APP_DIR / 'goldassist.log'}"
        )
        return 1

    log.info("Gold Decision Assistant serving at %s", url)

    if args.no_window:
        try:
            while thread.is_alive():
                thread.join(timeout=1)
        except KeyboardInterrupt:
            pass
        return 0

    if not args.browser and _open_window(url):
        return 0

    if not args.browser:
        log.warning("Opening in the default browser at %s. The desktop window "
                    "could not be used; the reason is logged above.", url)
    webbrowser.open(url)
    try:
        while thread.is_alive():
            thread.join(timeout=1)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
