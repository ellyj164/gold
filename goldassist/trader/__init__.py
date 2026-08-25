"""The behavioural layer: how the application acts, rather than what it sees.

The analysis package measures the market and the signals package turns those
measurements into tradeable suggestions. This package covers everything a human
trader does *around* that -- reading the character of the day, holding a view,
following the top-down method, managing an open position, and remembering what
has and has not worked.

Nothing in this package withholds a setup. The application never executes a
trade, so there is nothing to protect the trader from by hiding analysis. Every
module here adds context beside the signal, and the only thing that context
drives is suggested position size.
"""

from .awareness import SessionRecord
from .character import DayCharacter
from .desk import TradingDesk
from .management import ManagedTrade, ManagementAction
from .memory import Memory
from .sessions import SessionRead
from .thesis import Thesis
from .topdown import TopDownRead

__all__ = [
    "DayCharacter",
    "ManagedTrade",
    "ManagementAction",
    "Memory",
    "SessionRead",
    "SessionRecord",
    "Thesis",
    "TopDownRead",
    "TradingDesk",
]
