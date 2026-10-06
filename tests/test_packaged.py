"""Smoke test for the packaged application.

Exists because of a bug that shipped: the console build (GoldAssistant-debug.exe)
was tested and passed, while the windowed build crashed on launch. In a
--noconsole PyInstaller build ``sys.stdout`` is ``None``, and uvicorn's default
log formatter calls ``sys.stdout.isatty()``.

So this test targets **GoldAssistant.exe** specifically — the binary users
actually double-click. Run it after every build.

Run with:  python -m tests.test_packaged
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import tempfile
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
BUNDLE = PROJECT / "dist" / "GoldAssistant"

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        FAILURES.append(name)


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def get(url: str, timeout: float = 20.0):
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return response.status, response.read()


def wait_for(url: str, process: subprocess.Popen, timeout: float = 180.0) -> bool:
    """Poll until the app answers, or it dies trying."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if process.poll() is not None:
            return False
        try:
            status, _ = get(url, timeout=3)
            if status == 200:
                return True
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
            pass
        time.sleep(1.0)
    return False


def run_binary(exe: Path, label: str) -> None:
    print(f"{label}  ({exe.name})")
    if not exe.exists():
        check(f"{label}: binary exists", False, f"missing {exe}")
        return

    port = free_port()
    base = f"http://127.0.0.1:{port}"

    # Give the build under test its own GOLDASSIST_HOME.
    #
    # Without this the test runs a full analysis service -- two of them, since
    # both builds are exercised -- against the trader's real database, and
    # every setup they identify is written into the live signal log as though
    # it had been called for real. Two concurrent services also duplicate each
    # other's rows, which is exactly the pollution the single-instance guard
    # exists to prevent in normal use.
    home = Path(tempfile.mkdtemp(prefix="goldassist-test-"))
    env = {**os.environ, "GOLDASSIST_HOME": str(home)}

    process = subprocess.Popen(
        [str(exe), "--no-window", "--port", str(port)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
    )

    try:
        started = wait_for(f"{base}/api/status", process)
        if not started:
            output = ""
            if process.poll() is not None:
                output = (process.stdout.read() or b"").decode("utf-8", "replace")[-1500:]
            check(f"{label}: starts and serves", False, output)
            return
        check(f"{label}: starts and serves", True)

        status, body = get(f"{base}/api/status")
        payload = json.loads(body)
        check(f"{label}: reports MT5 connection", payload.get("connected") is True,
              str(payload.get("error")))

        status, body = get(f"{base}/api/dashboard")
        dashboard = json.loads(body)
        check(f"{label}: produces a decision",
              dashboard.get("decision") in ("BUY", "SELL", "WAIT"),
              str(dashboard)[:200])
        check(f"{label}: dashboard has rows", len(dashboard.get("rows", [])) > 5)

        status, body = get(f"{base}/")
        check(f"{label}: serves the UI", status == 200 and b"<canvas" in body)

        for asset in ("styles.css", "app.js", "chart.js"):
            status, body = get(f"{base}/static/{asset}")
            check(f"{label}: serves {asset}", status == 200 and len(body) > 1000)

        status, body = get(f"{base}/api/chart/H1?bars=120")
        chart = json.loads(body)
        check(f"{label}: chart data present", len(chart.get("candles", [])) > 50)

        status, body = get(f"{base}/api/chart/M30?bars=120")
        m30 = json.loads(body)
        check(f"{label}: M30 is a served timeframe",
              status == 200 and len(m30.get("candles", [])) > 50,
              f"status {status}")

        status, body = get(f"{base}/api/setups")
        setups = json.loads(body)
        check(f"{label}: the setup book is served",
              status == 200 and "live" in setups and "counts" in setups,
              str(setups)[:200])

        status, body = get(f"{base}/api/chart/M1?bars=120")
        m1 = json.loads(body)
        check(f"{label}: M1 is a served timeframe",
              status == 200 and len(m1.get("candles", [])) > 50,
              f"status {status}")

        status, body = get(f"{base}/api/strategies")
        library = json.loads(body)
        check(f"{label}: the strategy library is served",
              status == 200 and len(library.get("strategies", [])) >= 32,
              str(len(library.get("strategies", []))))
        check(f"{label}: every strategy declares a family and an alignment",
              all(s_.get("family") and s_.get("alignment")
                  for s_ in library.get("strategies", [])))

        # The clock, the signal log and the tape: everything the Signals tab
        # and the terminal's timing read depend on.
        status, body = get(f"{base}/api/clock")
        clock = json.loads(body)
        check(f"{label}: the display clock is served",
              status == 200 and clock.get("offset", "").startswith("GMT"),
              str(clock)[:200])
        check(f"{label}: the clock knows the session",
              bool((clock.get("now") or {}).get("session_label")),
              str(clock.get("now"))[:200])

        # The timezone database, which the bundle has to carry.
        #
        # Session windows are anchored to Tokyo, London and New York wall-clock
        # time and converted through `zoneinfo` at runtime, so a frozen build
        # without `tzdata` would fail to name a single session -- and would do
        # it only in the packaged app, which is exactly the class of failure
        # this suite exists to catch. `/api/clock` is the cheapest endpoint
        # that forces the conversion.
        session_label = (clock.get("now") or {}).get("session_label") or ""
        check(f"{label}: the timezone database is bundled",
              session_label and session_label.lower() != "unknown",
              f"session_label={session_label!r} — if this is empty, tzdata is "
              f"missing from the bundle")

        status, body = get(f"{base}/api/health")
        health = json.loads(body)
        check(f"{label}: the health check is served",
              status == 200 and "checks" in health and "summary" in health,
              str(health)[:200])
        check(f"{label}: it reports on the MetaTrader connection",
              any(c.get("check") == "MetaTrader connection"
                  for c in health.get("checks", [])),
              str(health.get("checks"))[:200])
        # Costs are the figures every R depends on; a packaged build that
        # cannot measure them would silently report gross numbers as net.
        cost_check = next((c for c in health.get("checks", [])
                           if c.get("check") == "Transaction costs"), None)
        check(f"{label}: transaction costs are measured in the packaged build",
              cost_check is not None and cost_check.get("ok"),
              str(cost_check))

        status, body = get(f"{base}/api/fired")
        fired = json.loads(body)
        check(f"{label}: the signal log is served",
              status == 200 and "signals" in fired and "summary" in fired,
              str(fired)[:200])
        check(f"{label}: the log offers session filters",
              len(fired.get("sessions", [])) >= 5,
              str(fired.get("sessions"))[:200])
        for row in fired.get("signals", [])[:3]:
            check(f"{label}: a logged signal carries its fired time and session",
                  bool(row.get("fired_clock")) and bool(row.get("session_label")),
                  str(row)[:200])

        status, body = get(f"{base}/api/tape")
        tape = json.loads(body)
        check(f"{label}: the tape is served",
              status == 200 and "micro" in tape, str(tape)[:200])
        check(f"{label}: M1 is in the analysed ladder",
              "M1" in tape.get("timeframes", []), str(tape.get("timeframes")))
        # A live account should be serving ticks; if it is not, the reading
        # must at least say so rather than reporting silent zeroes.
        micro_read = tape.get("micro") or {}
        check(f"{label}: the tape is either readable or explains why",
              micro_read.get("available") or bool(micro_read.get("reason")),
              str(micro_read)[:200])

        status, body = get(f"{base}/api/outcomes")
        outcomes = json.loads(body)
        check(f"{label}: setup results are served",
              status == 200 and "summary" in outcomes and "session_book" in outcomes,
              str(outcomes)[:200])

        status, body = get(f"{base}/api/setups")
        board = json.loads(body)
        check(f"{label}: the board reports resolved setups too",
              "retired" in board and "results" in board, str(board)[:200])

        status, body = get(f"{base}/api/trader")
        trader = json.loads(body)
        check(f"{label}: the trading desk reports", status == 200,
              f"status {status}")
        if trader.get("ready"):
            for section in ("session", "character", "topdown", "record",
                            "thesis", "memory", "management", "verdict"):
                check(f"{label}: desk publishes '{section}'", section in trader)
            # The correction that shaped this layer, pinned in the packaged
            # build: nothing may gate a setup.
            check(f"{label}: no setup gating in the packaged build",
                  "trade_allowed" not in trader.get("verdict", {})
                  and "restrictions" not in trader.get("record", {}),
                  str(trader.get("verdict"))[:200])

        status, body = get(f"{base}/api/memory")
        check(f"{label}: the outcome record is served",
              status == 200 and "min_sample" in json.loads(body),
              f"status {status}")

    finally:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()


def main() -> int:
    if not BUNDLE.exists():
        print(f"No build found at {BUNDLE}.")
        print("Build it first:  packaging\\build.bat")
        return 1

    # The windowed binary is the one users run, and the one with no stdout.
    # It is tested first and deliberately not skipped.
    run_binary(BUNDLE / "GoldAssistant.exe", "windowed build")
    run_binary(BUNDLE / "GoldAssistant-debug.exe", "console build")

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) failed: {', '.join(FAILURES)}")
        return 1
    print("Packaged application verified.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
