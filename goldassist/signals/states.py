"""Lifecycle states, in one place because three modules need them.

The states a setup moves through, in order:

    DEVELOPING -> WATCH -> IN ZONE -> WAITING FOR CONFIRMATION -> CONFIRMED
                                                               -> ACTIVE
                                                               -> terminal

and the terminal states, which are the point of the whole exercise:

``TARGET HIT``   the trade reached the objective it was closed at -- a win
``COMPLETED``    every target was reached -- a win, in full
``STOPPED OUT``  the stop was hit after the entry filled -- a loss (or a
                 scratch, when the stop had already been moved to breakeven)
``INVALIDATED``  the level that had to hold broke before the entry ever filled
``EXPIRED``      the setup ran out of time, or its premise disappeared

The first three describe a trade. The last two describe an idea that never
became one, and are recorded as *untaken* rather than as losses -- counting
them as losses would make the strategy record meaningless.
"""

from __future__ import annotations

DEVELOPING = "DEVELOPING"
WATCH = "WATCH"
IN_ZONE = "IN ZONE"
WAITING_FOR_CONFIRMATION = "WAITING FOR CONFIRMATION"
CONFIRMED = "CONFIRMED"
ACTIVE = "ACTIVE"
WEAKENING = "WEAKENING"

TARGET_HIT = "TARGET HIT"
COMPLETED = "COMPLETED"
STOPPED_OUT = "STOPPED OUT"
INVALIDATED = "INVALIDATED"
EXPIRED = "EXPIRED"

TERMINAL = frozenset({TARGET_HIT, COMPLETED, STOPPED_OUT, INVALIDATED, EXPIRED})
WINNING = frozenset({TARGET_HIT, COMPLETED})
LOSING = frozenset({STOPPED_OUT})
# Terminal without a trade ever having existed.
UNTAKEN_STATES = frozenset({INVALIDATED, EXPIRED})

# Statuses that mean the trader may be acting on this setup right now.
PROTECTED = frozenset({IN_ZONE, WAITING_FOR_CONFIRMATION, CONFIRMED, ACTIVE})

# Lifecycle ordering, used to decide which setup deserves the banner.
ATTENTION_ORDER = {
    ACTIVE: 6,
    CONFIRMED: 5,
    WAITING_FOR_CONFIRMATION: 4,
    IN_ZONE: 3,
    WATCH: 2,
    DEVELOPING: 1,
    WEAKENING: 0,
}

# The order the interface draws the progress rail in.
STAGES = (DEVELOPING, WATCH, IN_ZONE, WAITING_FOR_CONFIRMATION, CONFIRMED,
          ACTIVE, TARGET_HIT)
