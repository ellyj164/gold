"""The offline reading of a headline: a keyword fallback, labelled as one.

The real tagging is done by a model (see ``AIAnalyst.classify_headlines``).
This exists so that with no key, or with every model resting, a headline about
an FOMC decision is still marked high impact and one about a hawkish surprise
still leans bearish -- and so that the panel can say the tag came from keyword
matching, which it always does. A keyword rule cannot read "less hawkish than
feared", and must not be presented as though it could.

The direction rules encode the standard transmission to gold, not a forecast:
lower real yields, a weaker dollar and fear are supportive; higher yields, a
stronger dollar and risk appetite weigh. Where a headline carries both, or
neither, the answer is neutral.
"""

from __future__ import annotations

import re

HIGH = re.compile(
    r"\b(fomc|rate decision|fed (raises|cuts|holds|hikes)|powell|"
    r"cpi|pce|nonfarm|non-farm|payrolls?|jobs report|"
    r"war|invasion|missile|nuclear|attack|ceasefire|"
    r"central banks? (buy|bought|purchas)\w*|record high|all-time high)\b", re.I)

MEDIUM = re.compile(
    r"\b(fed|federal reserve|rates?|yields?|treasur(y|ies)|inflation|ppi|"
    r"dollar|dxy|gdp|jobless|unemployment|recession|tariffs?|sanctions?|"
    r"gold|bullion|geopolitic\w*|safe[- ]haven|iran|israel|russia|ukraine|china|taiwan)\b", re.I)

BULLISH = re.compile(
    r"\b(rate cuts?|cuts? rates?|dovish|easing|pause|weaker dollar|dollar (falls|slides|slumps|weakens|drops)|"
    r"yields? (fall|drop|slide|ease|decline)|soft(er)? (cpi|inflation|jobs|data)|"
    r"jobless claims (rise|jump)|unemployment (rises|jumps)|recession|slowdown|(softer|cooler|weaker) than expected|"
    r"war|invasion|missile|attack|escalat\w*|tensions?|crisis|safe[- ]haven|haven demand|"
    r"central banks? (buy|bought|purchas)\w*|gold (rises|rallies|climbs|jumps|surges|hits record|gains))\b", re.I)

BEARISH = re.compile(
    r"\b(rate hikes?|hikes? rates?|hawkish|tightening|higher for longer|stronger dollar|"
    r"dollar (rises|climbs|jumps|strengthens|rallies|gains)|"
    r"yields? (rise|jump|climb|surge|spike)|hot(ter)? (cpi|inflation)|strong(er)? (jobs|payrolls|data)|"
    r"(hotter|stronger) than expected|ceasefire|peace (deal|talks)|de-?escalat\w*|risk[- ]on|"
    r"gold (falls|slides|drops|slumps|tumbles|declines|loses))\b", re.I)


# Another economy's data or currency. "Canada CPI" is not US CPI, and an
# AUD/USD story mentions the dollar without being about it. Such a headline is
# low impact for gold unless it also names gold or the Fed directly.
FOREIGN = re.compile(
    r"\b(canad\w*|cad|australi\w*|aussie|aud|new zealand|kiwi|nzd|uk|british|britain|"
    r"pound|sterling|gbp|euro(zone)?|eur|ecb|german\w*|french|france|japan\w*|yen|jpy|boj|"
    r"swiss|franc|chf|snb|turk\w*|lira|india\w*|rupee|rbi|mexic\w*|peso|rba|rbnz|boe)\b",
    re.I)
GOLD_OR_FED = re.compile(r"\b(gold|xau|bullion|fed|fomc|powell|federal reserve)\b", re.I)


def classify(title: str, summary: str = "") -> dict:
    """``{bias, impact, reason, classified_by: "keywords"}`` for one headline."""
    text = f"{title} {summary[:300]}"
    if FOREIGN.search(title) and not GOLD_OR_FED.search(title):
        return {"bias": "neutral", "impact": "low",
                "reason": "keyword match: another economy's story, no direct channel to gold",
                "classified_by": "keywords"}
    impact = "high" if HIGH.search(title) else "medium" if MEDIUM.search(text) else "low"

    up = BULLISH.findall(text)
    down = BEARISH.findall(text)
    if up and not down:
        bias, reason = "bullish", "keyword match: typically supportive of gold"
    elif down and not up:
        bias, reason = "bearish", "keyword match: typically weighs on gold"
    else:
        bias, reason = "neutral", ("keyword match: mixed signals" if up and down
                                   else "keyword match: no clear direction for gold")
    return {"bias": bias, "impact": impact, "reason": reason, "classified_by": "keywords"}
