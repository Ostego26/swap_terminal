#!/usr/bin/env python3
"""The one table of coin symbols, held against the coins this terminal trades.

Role: file (entry point -- `python3 -m pytest tests/test_asset_identity.py`)
Reads: swap_terminal/services/asset_identity.py and Config.ALLOWED_PAIRS
Writes: nothing
Can move funds: no
Live-safe: yes

WHY A TEST FOR A LOOKUP TABLE. Five surfaces show a coin to somebody -- step 1's
chooser, step 2's chooser, the review screen, the live swap page and the
operator's lamp strip. A symbol spelled in one template is a symbol the other
four do not have, and nothing fails: a missing glyph renders as an empty span.
That is rule 8's subject, and this is the gate that makes the table the only
place a coin's symbol exists.

THE ROW THAT WAS WRONG, because it is why these tests check PROVENANCE and not
just presence. GRC shipped as `AssetSymbol("", "", "none exists")`, under a
paragraph arguing that inventing a glyph would be "rule 17 applied to
typography". The reasoning was sound and the premise was false -- Gridcoin's own
branding writes the project as Ǥridcoin.Network, U+01E4 -- so the file was
asserting an absence it had never checked. An unresearched gap in the voice of a
finding is the failure rule 17 is about, and it is harder to see than a guess.
"""

from __future__ import annotations

import unicodedata

import pytest
from config import Config
from services.asset_identity import (
    SYMBOLS,
    color_class_for,
    symbol_for,
    symbol_title_for,
)


def traded_assets() -> set[str]:
    """Every asset that appears on either side of an allowed pair.

    READ FROM Config.ALLOWED_PAIRS rather than listed here, so adding a coin to
    the terminal fails this file until the coin has a symbol row -- which is the
    only mechanism that keeps one table authoritative over five surfaces.

    MEASURED SHAPE: a set of 30 (from, to) tuples. The first version of this
    function also handled a `"BTC->GRC"` string form, with a branch and an
    isinstance check carrying a noqa -- for a shape Config has never used. That
    is rule 17 in a test helper: a parser written for a guessed representation,
    which cannot fail and cannot be right, and whose dead branch would be read
    by the next person as evidence that both forms occur.
    """
    assets: set[str] = set()
    for from_asset, to_asset in Config.ALLOWED_PAIRS:
        assets.add(str(from_asset).upper())
        assets.add(str(to_asset).upper())
    return assets


def test_every_traded_asset_has_a_symbol_row():
    """A coin on the ATM with no row renders its ticker and nobody notices."""
    traded = traded_assets()
    assert traded, (
        "parsed no assets out of Config.ALLOWED_PAIRS -- if its shape changed, follow it "
        "here rather than deleting this test, because then nothing holds the table against "
        "the coins actually traded"
    )
    missing = sorted(traded - set(SYMBOLS))
    assert not missing, (
        f"{missing} are traded and have no row in services/asset_identity.SYMBOLS. "
        "symbol_for() falls back to the ticker, so the tile still renders and the only "
        "symptom is one coin looking different from the other five."
    )


def test_no_row_exists_for_a_coin_nobody_trades():
    """The other direction: a row for a coin that is gone is dead code (rule 9)."""
    extra = sorted(set(SYMBOLS) - traded_assets())
    assert not extra, (
        f"{extra} have symbol rows and are not in Config.ALLOWED_PAIRS. Either the coin was "
        "retired and its row outlived it, or a pair was removed and this row is the last "
        "thing that still believes in it."
    )


@pytest.mark.parametrize("asset", sorted(SYMBOLS))
def test_every_glyph_is_the_codepoint_and_the_name_the_row_claims(asset):
    """The row's own provenance must describe its own glyph.

    A glyph in a table is unverifiable by reading -- Ǥ, Ł and ₿ are all one
    character and two of them are letters -- so each row carries the codepoint
    and the Unicode name, and this checks the three against each other. A
    corrected or mistyped glyph that keeps the old codepoint is caught here
    rather than by somebody noticing a tile looks wrong.
    """
    entry = SYMBOLS[asset]
    if not entry.glyph:
        # A genuinely symbol-less coin is allowed and must say so in both fields.
        assert not entry.codepoint, f"{asset} has no glyph but claims codepoint {entry.codepoint}"
        return

    assert len(entry.glyph) == 1, f"{asset}'s glyph is {len(entry.glyph)} characters"
    assert entry.codepoint == f"U+{ord(entry.glyph):04X}", (
        f"{asset}: glyph {entry.glyph!r} is U+{ord(entry.glyph):04X}, row says {entry.codepoint}"
    )
    assert unicodedata.name(entry.glyph) == entry.name, (
        f"{asset}: U+{ord(entry.glyph):04X} is {unicodedata.name(entry.glyph)!r}, "
        f"row says {entry.name!r}"
    )


def test_only_bitcoin_is_claimed_as_an_assigned_currency_sign():
    """`official` is a claim about the world and exactly one row may make it.

    Unicode has assigned one of these six a currency sign. The other five are
    letters, a math symbol, a geometric shape and a dingbat, used by convention
    -- and symbol_title_for() tells the reader which, on hover, because somebody
    taking ◎ from this page as "the Solana sign" the way ₿ is the Bitcoin sign
    would be wrong.

    Pinned rather than left to a reviewer, because flipping a flag is a one-word
    change that turns a convention into an assertion and no other test would
    notice.
    """
    official = sorted(a for a, entry in SYMBOLS.items() if entry.official)
    assert official == ["BTC"], (
        f"{official} are flagged as assigned Unicode currency signs. Only U+20BF BITCOIN "
        "SIGN is one. If Unicode has since assigned another, cite it in the row's comment "
        "in the same commit -- `official` is a statement about the standard, not about how "
        "widely a glyph is used."
    )


def test_gridcoin_carries_the_stroked_G_its_own_branding_uses():
    """The corrected row, pinned at the value the operator supplied.

    Gridcoin writes itself Ǥridcoin.Network. This file previously recorded that
    GRC had no symbol "official or conventional" -- a gap written in the voice of
    a finding -- and the specific value is pinned here so a future pass cannot
    quietly return it to the ticker fallback on the same reasoning.
    """
    assert symbol_for("GRC") == "Ǥ", (
        "GRC must render U+01E4 LATIN CAPITAL LETTER G WITH STROKE, the glyph Gridcoin's "
        "own branding uses"
    )
    assert not SYMBOLS["GRC"].official, "Ǥ is a Latin letter, not an assigned currency sign"
    assert "convention" in symbol_title_for("GRC"), (
        "the hover text must say the glyph is conventional rather than assigned"
    )


def test_an_unknown_asset_falls_back_to_its_ticker_and_says_so():
    """Rule 14: a blank is not a result, and the fallback must be legible.

    Reached only by a coin somebody added to the terminal without adding a row
    (which test_every_traded_asset_has_a_symbol_row fails on), so this pins the
    behaviour of the branch rather than a state the tree is in.
    """
    assert symbol_for("DOGE") == "DOGE"
    assert symbol_for("doge") == "DOGE", "the fallback must be the ticker, upper-cased"
    assert "no symbol row" in symbol_title_for("DOGE"), (
        "an unknown asset's hover text must name the table it is missing from, not render "
        "an empty tooltip"
    )


def test_the_colour_class_is_a_class_name_and_never_a_colour():
    """The palette lives on :root in styles.css, declared once.

    A hex value returned from Python would be a second place the palette lives
    and the one place nobody reading the stylesheet would find -- which is the
    drift that file's own header says it was written to end.
    """
    for asset in SYMBOLS:
        klass = color_class_for(asset)
        assert klass == f"coin-{asset.lower()}", klass
        assert "#" not in klass, f"{klass} looks like a colour; this must return a class name"
