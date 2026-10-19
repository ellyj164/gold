"""The news desk: reads the wires, tags each headline, raises the big ones.

Runs on its own thread, like the fundamental layer, and for the same reason:
a feed that takes thirty seconds to answer must never cost the analysis loop
a single cycle. The loop reads :meth:`NewsDesk.read`, which returns the last
completed picture and never blocks.

Each cycle, every three minutes:

1. Fetch every feed (cached, backed off -- see ``feeds``), store new stories.
2. Tag new stories by keyword at once, so nothing sits untagged on screen.
3. Hand untagged and keyword-tagged stories to the model in batches; the
   model's reading replaces the keyword one. With no model reachable the
   keyword tag stays, and says it is one.
4. Raise an alert for a story that is high impact *and* fresh -- published
   in the last 90 minutes. The first run after a restart reads two days of
   backlog; alerting on all of it would bury the one that matters.

Hourly it also refreshes this week's calendar (``calendar_feed``) and prunes
headlines older than a week.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Callable

from . import calendar_feed, feeds, impact

log = logging.getLogger(__name__)

POLL_SECONDS = 180.0
CALENDAR_SECONDS = 3600.0
ALERT_FRESH = timedelta(minutes=90)
BATCH = 12
BATCHES_PER_CYCLE = 3
DIGEST_HOURS = 12


class NewsDesk:
    def __init__(self, db, classify: Callable[[list[dict]], tuple[list[dict], str | None]],
                 on_breaking: Callable[[dict], None] | None = None,
                 poll_seconds: float = POLL_SECONDS):
        self.db = db
        self.classify = classify
        self.on_breaking = on_breaking
        self.poll_seconds = poll_seconds
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._calendar_at = 0.0
        self._state: dict = {"available": False, "initialising": True,
                             "note": "Reading the news wires in the background."}

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="news", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def refresh_soon(self) -> None:
        """Run a cycle now rather than at the next tick (the panel's button)."""
        self._wake.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.cycle()
            except Exception:                       # pragma: no cover - defensive
                # Same rule as the fundamentals thread: a failed cycle must
                # never kill the thread; the next one may well succeed.
                log.warning("News cycle failed", exc_info=True)
            self._wake.wait(self.poll_seconds)
            self._wake.clear()

    # -- one cycle -----------------------------------------------------------

    def cycle(self, now: datetime | None = None) -> dict:
        now = now or datetime.now(timezone.utc)
        calendar = None
        if time.time() - self._calendar_at >= CALENDAR_SECONDS:
            calendar = calendar_feed.sync()
            self._calendar_at = time.time()
            self.db.prune_news()

        headlines, health = feeds.fetch_all(now)
        new_ids = set(self.db.add_news([h.to_dict() for h in headlines]))
        for h in headlines:
            if h.id in new_ids:
                tag = impact.classify(h.title, h.summary)
                self.db.classify_news(h.id, tag["bias"], tag["impact"], tag["reason"],
                                      "keywords")

        model = None
        for _ in range(BATCHES_PER_CYCLE):
            pending = self.db.unclassified_news(BATCH)
            if not pending:
                break
            readings, model = self.classify(pending)
            if not readings:
                break           # no model reachable, or nothing usable: keep keyword tags
            for r in readings:
                self.db.classify_news(r["id"], r["bias"], r["impact"], r["reason"], model)

        self._raise_breaking(now)
        state = self._compose(now, health, model)
        if calendar is not None:
            state["calendar_sync"] = calendar
        else:
            state["calendar_sync"] = self._state.get("calendar_sync")
        with self._lock:
            self._state = state
        return state

    def _raise_breaking(self, now: datetime) -> None:
        if self.on_breaking is None:
            return
        for item in self.db.recent_news(hours=ALERT_FRESH.total_seconds() / 3600,
                                        limit=50, impact="high"):
            if item.get("alerted"):
                continue
            # Mark before raising: an alert that fails half-way must not be
            # raised again every three minutes.
            self.db.mark_news_alerted(item["id"])
            try:
                self.on_breaking(item)
            except Exception:                       # pragma: no cover - defensive
                log.warning("News alert failed", exc_info=True)

    def _compose(self, now: datetime, health: dict, model: str | None) -> dict:
        items = self.db.recent_news(hours=DIGEST_HOURS, limit=60)
        # High impact first, then medium, newest first within each: the
        # analyst reads the top ten, and those should be the ten that matter.
        rank = {"high": 0, "medium": 1, "low": 2}
        ordered = sorted(items, key=lambda i: (rank.get(i.get("impact"), 3),
                                               -_epoch(i.get("published_at"))))
        weighed = [i for i in items if i.get("impact") in ("high", "medium")]
        balance = {
            "bullish": sum(1 for i in weighed if i.get("bias") == "bullish"),
            "bearish": sum(1 for i in weighed if i.get("bias") == "bearish"),
            "neutral": sum(1 for i in weighed if i.get("bias") == "neutral"),
        }
        down = [v["name"] for v in health.values() if not v.get("ok")]
        return {
            "available": bool(items) or any(v.get("ok") for v in health.values()),
            "items": ordered,
            "balance": balance,
            "count": len(items),
            "feeds": health,
            "feeds_down": down,
            "classifier": model,
            "updated_at": now.isoformat(),
            "note": (f"{len(down)} of {len(health)} feeds not answering: {', '.join(down)}"
                     if down else None),
        }

    # -- the read ------------------------------------------------------------

    def read(self) -> dict:
        """The last completed picture. Never blocks on the network, never raises."""
        with self._lock:
            return dict(self._state)


def _epoch(stamp: str | None) -> float:
    try:
        return datetime.fromisoformat(stamp).timestamp() if stamp else 0.0
    except ValueError:
        return 0.0
