"""JSON normalisation.

numpy scalars, pandas timestamps and NaN all travel freely through the analysis
engines but none of them survive ``json.dumps``. Every payload leaving the app
(API response, WebSocket frame, database row) passes through :func:`safe`.
"""

from __future__ import annotations

import math
from datetime import date, datetime

import numpy as np
import pandas as pd


def safe(obj):
    """Recursively convert a value into something ``json.dumps`` accepts."""
    if obj is None or isinstance(obj, (str, bool)):
        return obj
    if isinstance(obj, (int, np.integer)):
        return int(obj)
    if isinstance(obj, (float, np.floating)):
        f = float(obj)
        # JSON has no NaN/Infinity; null is the honest representation.
        return None if math.isnan(f) or math.isinf(f) else f
    if isinstance(obj, np.bool_):
        return bool(obj)
    if isinstance(obj, (pd.Timestamp, datetime)):
        return obj.isoformat()
    if isinstance(obj, date):
        return obj.isoformat()
    if isinstance(obj, np.ndarray):
        return [safe(x) for x in obj.tolist()]
    if isinstance(obj, dict):
        return {str(k): safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [safe(x) for x in obj]
    if pd.isna(obj) if np.isscalar(obj) else False:
        return None
    return obj
