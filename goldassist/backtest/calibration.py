"""What a score has historically been worth.

The score is the weight of technical evidence, never a probability. That rule
stands. But a number shown without its record invites being read as one, and a
fluent explanation of a 78 makes the 78 feel stronger than its history says it
is. So every explanation is handed the measured record of the score band the
current score sits in -- trades, win rate, average net R -- with the sample size
and the run it came from attached, and says it as history, never as odds.

Standard library only, so it can be read anywhere a stored run can.
"""

from __future__ import annotations

# Bands as ``backtest.engine._score_band`` writes them.
BANDS = ((85.0, "85-100"), (75.0, "75-85"), (65.0, "65-75"), (float("-inf"), "below 65"))

# Below this many trades a band's record is reported as too thin to lean on.
MIN_BAND_TRADES = 30


def band_of(score: float) -> str:
    for floor, label in BANDS:
        if score >= floor:
            return label
    return BANDS[-1][1]


def band_record(stats: dict, score: float, run: dict | None = None) -> dict:
    """The stored record of the band ``score`` falls in, with its caveats."""
    run = run or {}
    label = band_of(score)
    row = (stats.get("by_score_band") or {}).get(label)
    window = f"{str(run.get('start_date', ''))[:10]} to {str(run.get('end_date', ''))[:10]}"
    if not row or not row.get("setups"):
        return {"available": False, "band": label,
                "note": f"The stored backtest ({window}) has no trades in the "
                        f"{label} band."}

    n = int(row.get("setups") or 0)
    caveats: list[str] = []
    if n < MIN_BAND_TRADES:
        caveats.append(f"only {n} trades — too few to lean on")
    if stats.get("fill_model") is None:
        caveats.append("from a run made before fills were modelled realistically, "
                       "so optimistic")
    if stats.get("score_basis") != "entry":
        caveats.append("grouped on the score at close, which drifts with the "
                       "outcome, so the band ordering is overstated")

    win_rate = row.get("win_rate")
    average = row.get("net_average_r")
    if average is None:
        average = row.get("average_r") or 0.0
    text = (f"Setups scoring {label} resolved as wins {win_rate}% of the time over "
            f"{n} backtest trades ({window}), averaging {average:+.2f}R after costs. "
            "That is the history of the band, not the odds of this setup.")
    if caveats:
        text += " Caveat: " + "; ".join(caveats) + "."

    return {"available": True, "band": label, "trades": n, "win_rate": win_rate,
            "net_average_r": average, "window": window,
            "backtest_id": run.get("id"), "caveats": caveats, "note": text}
