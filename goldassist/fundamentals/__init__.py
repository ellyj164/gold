"""The fundamental layer: why Gold is priced where it is.

Everything else in this application reads the chart. That is a complete
description of *what* Gold has done and no description at all of *why*, and for
the one instrument where the why is unusually legible, that was a real gap:
Gold has a small number of well-identified macro drivers, they are published
daily by public institutions, and none of them were being read.

The dominant one is the **10-year real yield**. Gold pays no coupon, so what it
competes with is not the nominal yield on government debt but the yield after
inflation. When real yields fall, holding a non-yielding asset costs less and
Gold rises; when they rise, the opportunity cost bites. ``trader/intermarket``
already said so in a docstring and could not act on it -- "no retail broker
carries TIPS" -- which was true of the broker and not of the world. The Federal
Reserve publishes the series daily and for free.

Four sources, each keyless, each cached, each independently optional:

``fred``
    The Federal Reserve of St. Louis. Real yields (DFII10), inflation
    breakevens (T10YIE), nominal yields, the trade-weighted dollar, the funds
    rate. The macro backbone.

``cot``
    The CFTC's weekly Commitments of Traders report. Who is actually
    positioned, and how crowded that positioning is against its own history.
    This is the one input on the list that no retail chart package carries and
    every institutional desk reads.

``market``
    COMEX futures (GC=F), Gold's implied volatility (GVZ), the real ICE dollar
    index, VIX, silver, oil. Reference prices from the venues where Gold is
    actually priced, rather than the broker's synthesised CFD quote.

``drivers``
    Composes the three into one directional read with the arithmetic shown.

Standing rules, inherited from the rest of the application and enforced here:

**A missing input is stated, never invented.** Every reading carries whether it
is available and where it came from. The network is not a dependency: with no
connection at all the layer reports itself unavailable and the technical engine
runs exactly as it did before.

**Fundamentals contextualise, they never gate.** Nothing here suppresses a
setup. The same principle the desk already follows -- context may size a trade,
it may never hide one -- applies with more force to a daily series being
consulted by an intraday method.

**Stale is labelled, not discarded.** A cached real yield from yesterday is a
better answer than no answer, because the series only updates once a day
anyway. Its age is always reported.
"""

from .drivers import FundamentalRead, GoldDrivers

__all__ = ["GoldDrivers", "FundamentalRead"]
