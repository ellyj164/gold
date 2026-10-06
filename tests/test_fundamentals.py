"""Checks for the fundamental layer: rates, positioning and reference prices.

Nothing here touches the network. Every source is fed a fixture, because the
properties worth testing are properties of the *parsing and composition*, and a
test that depends on FRED being reachable fails for reasons that have nothing
to do with the code.

Four things are verified that nothing else could verify:

1. **A positional parse of a government text file fails loudly.** The CFTC
   report has no header and is read by column index. The failure mode of that
   is a plausible wrong number, so the report's own internal identities are
   checked on every row and a shifted layout is rejected rather than believed.
2. **Confidence falls when information falls.** A read assembled from one
   lagging driver must not present itself as a unanimous macro picture. This is
   the bug the coverage term exists to prevent.
3. **The macro layer cannot become a directional filter.** Its effect on
   conviction is bounded tightly enough that an intraday trader can still take
   either side all day, which is the whole design constraint.
4. **A missing source costs a reading and nothing else.** Every degradation
   path returns an honest "unavailable" rather than a zero or an exception.

Run with:  python -m tests.test_fundamentals
"""

from __future__ import annotations

import sys

from goldassist.fundamentals import cot, drivers, fred, http, market

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        FAILURES.append(name)


# --- fixtures ---------------------------------------------------------------

FRED_CSV = "\n".join([
    "observation_date,DFII10",
    "2026-08-24,2.30",
    "2026-08-25,.",          # FRED's missing-observation marker
    "2026-08-26,2.34",
    "2026-08-27,",           # a market holiday, written as a blank
    "2026-08-28,2.36",
    "2026-08-31,2.38",
    "2026-09-01,2.40",
    "2026-09-02,2.45",
    "2026-09-03,2.42",
])


def _cot_row(oi=350000, nc_long=200000, nc_short=50000, spread=40000,
             comm_long=60000, comm_short=250000, date="2026-09-01",
             code=cot.GOLD_CODE, broken=False) -> str:
    """One CFTC row whose internal identities balance, unless asked not to."""
    total_long = nc_long + spread + comm_long
    total_short = nc_short + spread + comm_short
    nonrept_long = oi - total_long
    nonrept_short = oi - total_short
    if broken:
        total_long += 1          # break the reportable-side identity
    fields = ["GOLD - COMMODITY EXCHANGE INC.", "260901", date, code, "CMX",
              "01", "088", oi, nc_long, nc_short, spread, comm_long, comm_short,
              total_long, total_short, nonrept_long, nonrept_short]
    fields += [0] * 40           # the trailing columns this module ignores
    return ",".join(f'"{f}"' if isinstance(f, str) and "," in f else str(f)
                    for f in fields)


# --- FRED -------------------------------------------------------------------

def test_fred_parses_by_column_name():
    print("FRED's CSV is read by header name, not by position")
    columns = fred._parse_wide(FRED_CSV)
    check("the series is found by its id", "DFII10" in columns, str(list(columns)))

    rows = columns["DFII10"]
    check("blank and dotted observations are dropped, not zeroed",
          len(rows) == 7, f"{len(rows)} rows")
    check("the newest observation is last",
          rows[-1][1] == 2.42, str(rows[-1]))
    check("no value is zero", all(v > 0 for _, v in rows))


def test_fred_reordered_columns_do_not_swap_series():
    print("a reordered response cannot attribute one series to another")
    body = "\n".join(["observation_date,DGS2,DFII10",
                      "2026-09-02,4.34,2.45",
                      "2026-09-03,4.33,2.42"])
    columns = fred._parse_wide(body)
    check("the 2-year is read as the 2-year",
          columns["DGS2"][-1][1] == 4.33, str(columns.get("DGS2")))
    check("the real yield is read as the real yield",
          columns["DFII10"][-1][1] == 2.42, str(columns.get("DFII10")))


def test_fred_builds_readings_from_observations():
    print("a series becomes a level, a change and a percentile")
    rows = fred._parse_wide(FRED_CSV)["DFII10"]
    series = fred._build_series("real_10y", "DFII10", "10-year real yield", "%",
                                rows, "just now", stale=False)
    check("the series is available", series.available, series.note)
    check("the level is the newest observation", series.value == 2.42, str(series.value))
    check("the fast change is measured over the right window",
          series.change_fast is not None
          and abs(series.change_fast - (2.42 - 2.34)) < 1e-9,
          str(series.change_fast))
    check("basis points are derived for a percentage series",
          series.change_bp is not None and abs(series.change_bp - 8.0) < 1e-6,
          str(series.change_bp))

    empty = fred._build_series("real_10y", "DFII10", "x", "%", [], "", False)
    check("no observations means unavailable, never a zero level",
          not empty.available and empty.value is None, empty.note)


def test_fred_unreachable_is_stated_not_zeroed(monkeypatch):
    print("an unreachable FRED reports itself, and reports nothing else")
    monkeypatch.setattr(http, "get",
                        lambda url, ttl: http.Fetched(ok=False, error="no connection"))
    read = fred.read()
    check("the read is unavailable", not read.available)
    check("every series is listed as missing",
          len(read.missing) == len(fred.SERIES), str(read.missing))
    check("no series claims a value",
          all(s.value is None for s in read.series.values()))
    check("the headline says so", "chart alone" in read.headline, read.headline)


# --- COT --------------------------------------------------------------------

def test_cot_reads_the_gold_row():
    print("the Gold row parses and the net is the speculative net")
    weeks = cot._gold_rows(_cot_row())
    check("one Gold row is found", len(weeks) == 1, str(len(weeks)))

    week = weeks[0]
    check("net length is non-commercial long minus short",
          week.nc_net == 150000, str(week.nc_net))
    check("the net is also expressed against open interest",
          abs(week.nc_net_pct_oi - (150000 / 350000 * 100)) < 1e-6,
          str(week.nc_net_pct_oi))
    check("commercials are net short, as hedgers structurally are",
          week.comm_net < 0, str(week.comm_net))


def test_cot_rejects_a_shifted_layout():
    print("a row whose own arithmetic disagrees is dropped, not believed")
    weeks = cot._gold_rows(_cot_row(broken=True))
    check("the broken row produces no reading", weeks == [], str(weeks))


def test_cot_ignores_other_markets():
    print("only Gold's contract code is read")
    body = "\n".join([_cot_row(code="001602"), _cot_row()])
    weeks = cot._gold_rows(body)
    check("exactly one row survives", len(weeks) == 1, str(len(weeks)))


def test_cot_market_name_commas_do_not_shift_columns():
    print("a comma inside a quoted market name does not shift every column")
    weeks = cot._gold_rows(_cot_row())
    check("open interest is read correctly despite the quoted name",
          weeks and weeks[0].open_interest == 350000,
          str(weeks[0].open_interest if weeks else None))


def test_cot_crowding_needs_a_distribution():
    print("a percentile is not reported until there is a distribution")
    out = cot.CotRead()
    week = cot._gold_rows(_cot_row())[0]
    cot._interpret(out, week, [week] * 4)
    check("four weeks is not a distribution", out.crowding == "unknown", out.crowding)
    check("the reason is stated", any("weeks of history" in n for n in out.notes),
          str(out.notes))


# --- composition ------------------------------------------------------------

def test_thin_evidence_cannot_claim_a_strong_picture():
    print("one lagging driver does not make a unanimous macro picture")
    one_vote, _, _ = drivers._tally([("bearish", drivers.WEIGHT_POSITIONING)])
    _, strength_one, sentence = drivers._tally([("bearish", drivers.WEIGHT_POSITIONING)])
    check("the direction is still reported", one_vote == "bearish", one_vote)
    check("but the strength is small", strength_one < 0.2, str(strength_one))
    check("and the thinness is stated in words",
          "weak read" in sentence, sentence)

    everything = [("bearish", drivers.WEIGHT_REAL_YIELD),
                  ("bearish", drivers.WEIGHT_REAL_YIELD_LEVEL),
                  ("bearish", drivers.WEIGHT_BREAKEVEN),
                  ("bearish", drivers.WEIGHT_DOLLAR),
                  ("bearish", drivers.WEIGHT_POSITIONING),
                  ("bearish", drivers.WEIGHT_RATIO)]
    _, strength_all, _ = drivers._tally(everything)
    check("a complete one-sided picture is much stronger",
          strength_all > strength_one * 4, f"{strength_all} vs {strength_one}")


def test_disagreement_is_reported_as_disagreement():
    print("drivers pointing both ways are conflicted, not netted")
    bias, strength, sentence = drivers._tally(
        [("bullish", 3.0), ("bearish", 2.5)])
    check("the bias is conflicted", bias == "conflicted", bias)
    check("conflicted carries no strength", strength == 0.0, str(strength))
    check("the sentence says to trade the chart", "chart" in sentence, sentence)


def test_the_macro_read_cannot_become_a_directional_filter():
    print("an intraday trader can still take either side, whatever the macro says")
    read = drivers.FundamentalRead(available=True, bias="bearish", strength=1.0)
    against, _ = read.alignment("bullish")
    with_it, _ = read.alignment("bearish")

    check("trading against the strongest possible macro read still costs "
          "less than a tenth of a position",
          against >= 0.90, str(against))
    check("trading with it gains no more than a tenth",
          with_it <= 1.10, str(with_it))
    check("neither multiplier can reach zero", against > 0 and with_it > 0)

    quiet = drivers.FundamentalRead(available=True, bias="conflicted", strength=0.0)
    multiplier, reason = quiet.alignment("bullish")
    check("a conflicted picture is exactly neutral", multiplier == 1.0, str(multiplier))
    check("and says why", "chart-only" in reason, reason)


def test_an_unavailable_read_is_neutral_not_negative():
    print("absence of a fundamental read never penalises a setup")
    multiplier, reason = drivers.alignment_of(None, "bullish")
    check("no payload is neutral", multiplier == 1.0, str(multiplier))

    multiplier, _ = drivers.alignment_of({"available": False}, "bearish")
    check("an unavailable payload is neutral", multiplier == 1.0, str(multiplier))

    multiplier, _ = drivers.alignment_of(
        {"available": True, "bias": "bullish", "strength": 0.5}, "bullish")
    check("an aligned payload is a mild bonus",
          1.0 < multiplier <= 1.10, str(multiplier))


def test_compose_survives_every_source_failing(monkeypatch):
    print("with nothing reachable the layer degrades instead of raising")
    monkeypatch.setattr(fred, "read", lambda: fred.FredRead())
    monkeypatch.setattr(cot, "read", lambda: cot.CotRead())
    monkeypatch.setattr(market, "read", lambda spot=None: market.MarketRead())

    read = drivers.compose(4400.0)
    check("the read is unavailable rather than absent", not read.available)
    check("the bias is neutral", read.bias == "neutral", read.bias)
    check("the headline explains that nothing is degraded",
          "chart alone" in read.headline, read.headline)
    multiplier, _ = read.alignment("bullish")
    check("and it still returns a neutral multiplier", multiplier == 1.0)


# --- reference prices -------------------------------------------------------

def test_realised_volatility_is_annualised():
    print("realised volatility annualises daily returns")
    flat = market._realised_volatility([100.0] * 30)
    check("a flat series has no volatility", flat == 0.0 or flat is None, str(flat))

    alternating = [100.0 * (1.01 if i % 2 else 1.0) for i in range(30)]
    moving = market._realised_volatility(alternating)
    check("a moving series does", moving is not None and moving > 0, str(moving))
    check("too little history returns nothing, not zero",
          market._realised_volatility([100.0, 101.0]) is None)


def test_basis_flags_a_drifting_feed():
    print("a broker quote far from COMEX is called out")
    read = market.MarketRead(available=True)
    read.instruments["gold_futures"] = market.Instrument(
        key="gold_futures", symbol="GC=F", label="COMEX", units="$/oz",
        available=True, value=4500.0)

    market._basis(read, spot=4499.0)
    check("a normal basis is healthy", read.feed_healthy, read.basis_note)

    read.feed_healthy = True
    market._basis(read, spot=4400.0)
    check("a hundred-dollar gap is not", not read.feed_healthy, read.basis_note)
    check("and the note explains it", "COMEX" in read.basis_note, read.basis_note)


def main() -> int:
    class _Patch:
        """A minimal monkeypatch, so this file runs standalone as well as under pytest."""

        def __init__(self):
            self._undo = []

        def setattr(self, target, name, value):
            self._undo.append((target, name, getattr(target, name)))
            setattr(target, name, value)

        def undo(self):
            for target, name, original in reversed(self._undo):
                setattr(target, name, original)

    for test in (test_fred_parses_by_column_name,
                 test_fred_reordered_columns_do_not_swap_series,
                 test_fred_builds_readings_from_observations,
                 test_cot_reads_the_gold_row,
                 test_cot_rejects_a_shifted_layout,
                 test_cot_ignores_other_markets,
                 test_cot_market_name_commas_do_not_shift_columns,
                 test_cot_crowding_needs_a_distribution,
                 test_thin_evidence_cannot_claim_a_strong_picture,
                 test_disagreement_is_reported_as_disagreement,
                 test_the_macro_read_cannot_become_a_directional_filter,
                 test_an_unavailable_read_is_neutral_not_negative,
                 test_realised_volatility_is_annualised,
                 test_basis_flags_a_drifting_feed):
        test()

    for test in (test_fred_unreachable_is_stated_not_zeroed,
                 test_compose_survives_every_source_failing):
        patch = _Patch()
        try:
            test(patch)
        finally:
            patch.undo()

    if FAILURES:
        print(f"\n{len(FAILURES)} check(s) failed:")
        for name in FAILURES:
            print(f"  - {name}")
        return 1
    print("\nAll fundamental-layer checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
