"""Opportunity sizing for day trading.

The trader's rule: 500 pips is the *minimum* move worth acting on intraday --
a floor, never a ceiling. If the market structure supports 1500 or 3000 pips,
the application must show the larger opportunity rather than truncating it at
the threshold.

On XAUUSD one pip is 0.01 of price, so the 500-pip minimum is a 5.00 move --
a $5 move in Gold.

The other half of the rule matters just as much: having 500 pips available does
not make a signal. It makes a setup *eligible*. Direction, entry, confirmation
and invalidation still have to come from the technical analysis.
"""

from __future__ import annotations

# XAUUSD pip convention: 1 pip = 0.01 of price, so 500 pips = 5.00 ($5) of
# price movement.
PIP_SIZE = 0.01

# Bands from the specification, largest first so the first match wins.
OPPORTUNITY_BANDS = (
    (2000.0, "Major", "major intraday opportunity"),
    (1000.0, "Strong", "strong opportunity"),
    (500.0, "Good", "good day-trading opportunity"),
    (0.0, "Below minimum", "below the 500-pip day-trading minimum"),
)


def to_pips(price_move: float) -> float:
    """Convert a price distance into pips."""
    return abs(price_move) / PIP_SIZE


def to_price(pips: float) -> float:
    """Convert pips into a price distance."""
    return pips * PIP_SIZE


def classify(pips: float) -> tuple[str, str]:
    """Grade an available move. Returns (label, description)."""
    for threshold, label, description in OPPORTUNITY_BANDS:
        if pips >= threshold:
            return label, description
    return "Below minimum", "below the 500-pip day-trading minimum"


def assess(entry: float, targets: list, minimum_pips: float = 500.0) -> dict:
    """Describe the opportunity a set of targets represents.

    ``targets`` is a list of objects or dicts carrying a ``price``. The first
    target sets eligibility -- the rule is that the *first realistic* objective
    must clear the minimum -- while the furthest shows the full potential.
    """
    if not targets:
        return {
            "qualifies": False,
            "first_target_pips": 0.0,
            "max_pips": 0.0,
            "classification": "Below minimum",
            "description": "no technically supported objective was found",
            "minimum_pips": minimum_pips,
            "shortfall_pips": minimum_pips,
        }

    def price_of(target) -> float:
        return float(target["price"] if isinstance(target, dict) else target.price)

    distances = [to_pips(price_of(t) - entry) for t in targets]
    first = distances[0]
    largest = max(distances)

    # Eligibility is judged on the first objective; the classification reports
    # the full extent of the move, so a setup is never presented as smaller
    # than the structure actually supports.
    qualifies = first >= minimum_pips
    label, description = classify(largest if qualifies else first)

    return {
        "qualifies": qualifies,
        "first_target_pips": round(first, 1),
        "max_pips": round(largest, 1),
        "classification": label,
        "description": description,
        "minimum_pips": minimum_pips,
        "shortfall_pips": round(max(0.0, minimum_pips - first), 1),
        "headline": _headline(qualifies, first, largest, label, minimum_pips),
    }


def _headline(qualifies: bool, first: float, largest: float, label: str,
              minimum: float) -> str:
    if not qualifies:
        return (f"only {first:.0f} pips to the first objective - below the "
                f"{minimum:.0f}-pip minimum for an intraday trade")
    if largest > first * 1.2:
        return (f"{first:.0f} pips to the first objective and up to "
                f"{largest:.0f} pips if the structure extends - {label.lower()}")
    return f"{first:.0f} pips available - {label.lower()}"
