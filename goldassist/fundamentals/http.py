"""A small, polite, cached HTTP client for the public data sources.

The fundamental layer reads five public endpoints on a five-second analysis
cycle. Done naively that is both useless and rude: the underlying series are
daily or weekly, so re-fetching them at tick rate asks a public institution for
the same unchanged number seventeen thousand times a day. FRED answers the
first few and then resets the connection, which is exactly the right response
and was observed while this module was being written.

So every read goes through here, and the rules are:

**Cache on disk, keyed by URL.** The cache survives a restart, which matters
more than it sounds: the app is launched fresh most mornings, and a cold start
that has to re-fetch every series before it can show anything is a cold start
the trader waits through. Yesterday's real yield is on disk and correct.

**Serve stale rather than nothing.** When a fetch fails and a cached copy
exists, the cached copy is returned with its age attached. A number from
yesterday with "yesterday" written on it is useful; a blank panel is not. Only
when there is no copy at all does a read report itself unavailable.

**Fail once, not repeatedly.** A source that has just failed is not retried for
``FAILURE_BACKOFF``. Without this a disconnected laptop generates a failing
network call per source per cycle, each blocking for the timeout, and the
analysis loop -- which has a five-second budget -- misses its cadence entirely.

**Never raise.** Every function here returns a :class:`Fetched` describing what
happened. A fundamental source is a bonus; an exception escaping into the
analysis loop would take the chart down with it, which is precisely backwards.

The client is deliberately built on ``urllib`` from the standard library rather
than ``requests``. This application is packaged with PyInstaller, and every
third-party dependency is another thing that has to be found, bundled and
proven to work in the frozen build.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from ..config import APP_DIR

log = logging.getLogger(__name__)

CACHE_DIR = APP_DIR / "fundamentals"

# Public endpoints rate-limit an unlabelled request. This identifies the
# application honestly rather than impersonating a browser: a data provider
# that wants to block us should be able to.
#
# **No ``Accept`` header is sent, and that is deliberate.** FRED's CDN resets
# the connection on any request carrying ``Accept: */*`` -- evidently a crude
# bot signature -- while the identical request without the header is served in
# under a second. This cost an afternoon to find, because the failure presents
# as ``ConnectionResetError`` after a twenty-second stall and looks exactly
# like the rate limiting it is not. Adding an Accept header back will silently
# disable the entire macro layer.
USER_AGENT = "GoldAssistant/1.0 (gold market analysis; contact via application)"

# Seconds a request may block.
#
# Generous, because nothing waits on it: every fundamental read happens on a
# background thread with a TTL measured in hours, so a slow response costs
# patience and nothing else. FRED in particular answers the same request in
# under a second or in thirty depending on load, and a tight timeout turns its
# slow days into "the macro picture is unavailable" -- a far worse outcome than
# waiting.
TIMEOUT = 45

# One retry, after a short pause, before a URL is marked failed. Public data
# services drop connections under load and succeed immediately afterwards;
# without this, one unlucky moment costs six hours of macro context.
RETRIES = 1
RETRY_PAUSE = 2.0

# After a failure, how long before that URL is tried again. Long enough that a
# laptop with no connection stops trying, short enough that a blip recovers
# within one coffee.
FAILURE_BACKOFF = 600.0

# Bytes any single response may occupy. The CFTC weekly file is ~1.5 MB and is
# the largest thing read here; this is a guard against a redirect to something
# unbounded, not a tuning parameter.
MAX_BYTES = 32 * 1024 * 1024


@dataclass
class Fetched:
    """The outcome of one read: the body, where it came from, how old it is."""

    ok: bool
    body: str = ""
    from_cache: bool = False
    age_seconds: float = 0.0
    error: str = ""

    @property
    def stale(self) -> bool:
        """Served from cache after a failed or skipped refresh."""
        return self.ok and self.from_cache

    def age_note(self) -> str:
        """The age of this reading in the words a person would use."""
        if not self.ok:
            return "unavailable"
        seconds = self.age_seconds
        if seconds < 90:
            return "just now"
        if seconds < 5400:
            return f"{seconds / 60:.0f} minutes ago"
        if seconds < 172800:
            return f"{seconds / 3600:.0f} hours ago"
        return f"{seconds / 86400:.0f} days ago"


class _Client:
    """Shared cache and failure memory for every fundamental source."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._failed_at: dict[str, float] = {}

    # -- cache ---------------------------------------------------------------

    def _path(self, url: str) -> Path:
        # The URL is hashed rather than sanitised: query strings carry
        # characters Windows will not accept in a filename, and a hash is
        # stable across platforms in a way an escaping scheme is not.
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
        return CACHE_DIR / f"{digest}.json"

    def _read_cache(self, url: str) -> tuple[str, float] | None:
        path = self._path(url)
        try:
            if not path.exists():
                return None
            payload = json.loads(path.read_text(encoding="utf-8"))
            return payload["body"], float(payload["fetched_at"])
        except (OSError, ValueError, KeyError):
            # A truncated or hand-edited cache file is not worth a traceback;
            # it is worth re-fetching.
            log.debug("Discarding unreadable cache for %s", url, exc_info=True)
            return None

    def _write_cache(self, url: str, body: str) -> None:
        path = self._path(url)
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            # Written to a temporary name and moved, so a crash mid-write
            # cannot leave a half-file that reads as valid on next launch.
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"url": url, "fetched_at": time.time(),
                                       "body": body}), encoding="utf-8")
            tmp.replace(path)
        except OSError:
            log.debug("Could not cache %s", url, exc_info=True)

    # -- fetching ------------------------------------------------------------

    def get(self, url: str, ttl: float) -> Fetched:
        """Body of ``url``, from cache when it is younger than ``ttl``.

        ``ttl`` is how long a cached copy is considered current. It should
        match the publication cadence of the series, not the analysis cadence:
        a daily series has nothing new to say for hours at a time.
        """
        now = time.time()
        with self._lock:
            cached = self._read_cache(url)
            if cached is not None and now - cached[1] < ttl:
                return Fetched(ok=True, body=cached[0], from_cache=True,
                               age_seconds=now - cached[1])

            failed_at = self._failed_at.get(url)
            if failed_at is not None and now - failed_at < FAILURE_BACKOFF:
                if cached is not None:
                    return Fetched(ok=True, body=cached[0], from_cache=True,
                                   age_seconds=now - cached[1],
                                   error="refresh is backing off after a failure")
                return Fetched(ok=False, error="source unreachable; not retried yet")

        body, error = self._download(url)
        if body is None:
            with self._lock:
                self._failed_at[url] = now
            if cached is not None:
                return Fetched(ok=True, body=cached[0], from_cache=True,
                               age_seconds=now - cached[1], error=error)
            return Fetched(ok=False, error=error)

        with self._lock:
            self._failed_at.pop(url, None)
            self._write_cache(url, body)
        return Fetched(ok=True, body=body, from_cache=False, age_seconds=0.0)

    def _download(self, url: str) -> tuple[str | None, str]:
        request = urllib.request.Request(
            url, headers={"User-Agent": USER_AGENT})
        error = "unknown error"

        for attempt in range(RETRIES + 1):
            try:
                with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                    raw = response.read(MAX_BYTES)
                break
            except urllib.error.HTTPError as exc:
                # A refusal is an answer: the server understood and declined,
                # so asking again immediately is pointless and rude.
                return None, f"HTTP {exc.code} from the source"
            except urllib.error.URLError as exc:
                error = f"no connection ({exc.reason})"
            except Exception as exc:  # pragma: no cover - socket-level oddities
                error = f"{type(exc).__name__}: {exc}"
            if attempt < RETRIES:
                time.sleep(RETRY_PAUSE)
        else:
            return None, error

        try:
            return raw.decode("utf-8"), ""
        except UnicodeDecodeError:
            # Some endpoints answer a compressed archive when asked for a
            # multi-series export. Latin-1 never raises, and the parser above
            # will reject the content on its own terms rather than here.
            return raw.decode("latin-1", "replace"), ""


_client = _Client()


def get(url: str, ttl: float) -> Fetched:
    """Fetch ``url``, cached for ``ttl`` seconds. Never raises."""
    return _client.get(url, ttl)


def get_bytes(url: str) -> tuple[bytes | None, str]:
    """Download raw bytes, uncached, respecting the shared failure backoff.

    For the one source that is an archive rather than a document: the CFTC's
    annual history is a 14 MB zip, and putting that through the JSON body cache
    would write tens of megabytes of escaped text to disk to answer a question
    whose useful output is fifty rows. The caller extracts what it needs and
    caches *that* instead.

    The failure memory is still shared, so an unreachable CFTC does not get
    hammered here just because this path skips the body cache.
    """
    now = time.time()
    with _client._lock:
        failed_at = _client._failed_at.get(url)
        if failed_at is not None and now - failed_at < FAILURE_BACKOFF:
            return None, "source unreachable; not retried yet"

    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT * 3) as response:
            raw = response.read(MAX_BYTES)
    except urllib.error.HTTPError as exc:
        with _client._lock:
            _client._failed_at[url] = now
        return None, f"HTTP {exc.code} from the source"
    except urllib.error.URLError as exc:
        with _client._lock:
            _client._failed_at[url] = now
        return None, f"no connection ({exc.reason})"
    except Exception as exc:  # pragma: no cover - socket-level oddities
        with _client._lock:
            _client._failed_at[url] = now
        return None, f"{type(exc).__name__}: {exc}"

    with _client._lock:
        _client._failed_at.pop(url, None)
    return raw, ""


def cache_path(name: str) -> Path:
    """A named file in the fundamentals cache, for a caller's own derived data."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / name


def get_json(url: str, ttl: float) -> tuple[dict | list | None, Fetched]:
    """Fetch and parse JSON. Returns ``(None, fetched)`` on any failure."""
    fetched = get(url, ttl)
    if not fetched.ok:
        return None, fetched
    try:
        return json.loads(fetched.body), fetched
    except ValueError as exc:
        return None, Fetched(ok=False, error=f"malformed JSON from the source ({exc})")
