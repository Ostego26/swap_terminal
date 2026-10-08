#!/usr/bin/env python3
"""What each coin is CALLED and DRAWN as: its symbol, its mark, its color class.

Role: submodule (a lookup table and three functions over it)
Reads: nothing
Writes: nothing
Can move funds: no
Live-safe: yes

WHY THIS IS A MODULE AND NOT MARKUP. A currency symbol spelled in a template is
a currency symbol spelled once per template, and this system has five places a
coin is shown to somebody: step 1's chooser, step 2's chooser, the review screen,
the live swap page and the operator's lamp strip. Rule 8's whole subject is what
happens next -- they agree on the day they are written, one of them gets a new
coin and the others do not, and nothing fails because a missing symbol still
renders as an empty span.

So the table is here, `tests/test_asset_identity.py` holds it against
Config.ALLOWED_PAIRS, and every surface reads it.

-----------------------------------------------------------------------------
ONLY ONE OF THESE SYMBOLS IS A REAL CURRENCY SIGN, AND THE TABLE SAYS WHICH
-----------------------------------------------------------------------------

Operator, 2026-10-08: "use images and alt codes for each currency symbol."

The honest answer is that the alt codes mostly do not exist. Unicode has
assigned exactly one of these six a currency sign:

    BTC   U+20BF BITCOIN SIGN              an actual assigned currency symbol
    GRC   U+01E4 LATIN CAPITAL LETTER G     a LETTER, used by the project itself
          WITH STROKE
    LTC   U+0141 LATIN CAPITAL LETTER L     a LETTER, used by convention
          WITH STROKE
    ICP   U+221E INFINITY                   a MATH symbol; the Internet
                                            Computer's logo is an infinity loop
    SOL   U+25CE BULLSEYE                   a GEOMETRIC shape, community use
    XRP   U+2715 MULTIPLICATION X           a DINGBAT; Ripple's mark is an X

`official` is False for five of the six and that is recorded rather than
smoothed over, because the alternative is this file asserting that ◎ is the
Solana sign the way ₿ is the Bitcoin sign. It is not, and a reader who takes it
from here and puts it in a contract or an invoice would be wrong.

THE GRC ROW WAS WRONG WHEN THIS FILE WAS WRITTEN, AND THE CORRECTION IS THE
REASON THE ROW CARRIES ITS PROVENANCE. It read `AssetSymbol("", "", "none
exists")` under a paragraph arguing at length that GRC has no symbol "official
or conventional" and that inventing one would be "rule 17 applied to
typography". The reasoning was sound and the premise was false: the operator
supplied the citation the same day -- Gridcoin's own branding writes the project
as **Ǥridcoin.Network**, with U+01E4 standing in for the G. So the glyph was
not missing, it was unresearched, and the paragraph refusing to invent one was
doing nothing except making an unchecked absence look like a considered finding.

That is worth keeping rather than overwriting, because it is rule 17's failure
in its least obvious form: not a guess dressed as a measurement, but a GAP
dressed as a measurement. "I could not find a symbol" was written as "no symbol
exists", which is the same substitution rule 2 warns about for callers -- and the
person with the fact was one message away.

-----------------------------------------------------------------------------
COLOR IS A CSS TOKEN NAME, NEVER A HEX VALUE
-----------------------------------------------------------------------------

`color_class_for()` returns a class name (`coin-btc`), not `#f7931a`. The
palette lives in static/styles.css on :root, declared once, and that file's own
header is explicit about why: the file it replaced "spelled #2670B9 in" many
places. A hex code in Python would be a seventh place the palette lives and the
one place nobody looking at the stylesheet would find.
"""

from __future__ import annotations

from typing import NamedTuple


class AssetSymbol(NamedTuple):
    """One coin's printable symbol, and whether anybody official says so.

    `glyph` is what gets rendered. `codepoint` and `name` are for the operator
    and for this module's own test -- a bare glyph in a table is unverifiable
    by reading, because several of these are visually close to letters.
    """

    glyph: str
    codepoint: str
    name: str
    official: bool


#: Every asset this terminal trades, and how to print it.
#:
#: KEYED ON THE TICKER THIS REPOSITORY ALREADY USES, which is the same key as
#: Config.ALLOWED_PAIRS, the chain adapters and swap_terminal.db's `from_asset`.
#: A second vocabulary for display would be rule 11's cadence defect in another
#: dimension: one concept, two spellings, and a lookup that silently misses.
SYMBOLS: dict[str, AssetSymbol] = {
    "BTC": AssetSymbol("₿", "U+20BF", "BITCOIN SIGN", official=True),
    "GRC": AssetSymbol("Ǥ", "U+01E4", "LATIN CAPITAL LETTER G WITH STROKE", official=False),
    "ICP": AssetSymbol("∞", "U+221E", "INFINITY", official=False),
    "LTC": AssetSymbol("Ł", "U+0141", "LATIN CAPITAL LETTER L WITH STROKE", official=False),
    "SOL": AssetSymbol("◎", "U+25CE", "BULLSEYE", official=False),
    "XRP": AssetSymbol("✕", "U+2715", "MULTIPLICATION X", official=False),
}


def symbol_for(asset: str) -> str:
    """The glyph to print, or the ticker when no symbol exists.

    THE FALLBACK IS THE TICKER AND NOT A BLANK. An empty span is rule 14's
    blank gap -- indistinguishable between "this coin has no symbol" and "this
    lookup missed" -- and on a row of tiles it reads as a rendering fault.

    NOTHING IN SYMBOLS TAKES THIS PATH ANY MORE. GRC did until its row was
    corrected (see the header), so the branch now exists only for an asset
    somebody added to Config.ALLOWED_PAIRS without adding a row here -- and for
    a genuinely symbol-less coin, should one arrive, where an empty `glyph` is
    still the honest way to say so and the ticker is still what prints.
    """
    entry = SYMBOLS.get(asset.upper())
    if entry is None or not entry.glyph:
        return asset.upper()
    return entry.glyph


def symbol_title_for(asset: str) -> str:
    """The hover text for a symbol: what it is, and whether it is official.

    Rule 14: state what the thing means next to the thing. A reader seeing
    an infinity sign on a coin tile is entitled to learn that it is the Internet
    Computer's logo and NOT an assigned currency sign, without reading this file.
    """
    entry = SYMBOLS.get(asset.upper())
    if entry is None:
        return f"{asset.upper()}: no symbol row in services/asset_identity.SYMBOLS"
    if not entry.glyph:
        return (
            f"{asset.upper()} has no currency symbol, official or conventional, "
            "so its ticker is shown instead"
        )
    standing = (
        "an assigned Unicode currency sign"
        if entry.official
        else "used by convention; NOT an assigned currency sign"
    )
    return f"{entry.glyph} is {entry.codepoint} {entry.name} -- {standing}"


def color_class_for(asset: str) -> str:
    """The CSS class carrying this coin's accent color.

    Lowercased ticker, prefixed. The value behind it is in static/styles.css --
    see this module's header for why no hex code appears in Python.
    """
    return f"coin-{asset.lower()}"
