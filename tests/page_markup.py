"""How the customer page's direction tiles are matched, in ONE place.

Role: test helper (no tests of its own; imported by the page tests)
Reads: nothing. It is a regular expression and a function over a string.
Writes: nothing
Can move funds: no
Mainnet-safe: yes

WHY THIS FILE EXISTS. On 2026-10-07 a direction tile gained `data-from`/`data-to`
attributes so the new coin lamps could filter the grid, and SEVEN tests across THREE
files failed at once -- every one of them because its own copy of

    r'<li class="swaptile swaptile-([a-z]+)">...'

required the closing angle bracket to follow the class immediately. Not one of them
was testing where that bracket is. Three copies of one pattern, drifting all at once
instead of slowly, which is rule 8 with the delay removed.

It is also CLAUDE.md's "verify by behavior, never by literal text" showing up in HTML:
what those tests hold is "one tile per direction, naming the direction, carrying an
explained marker". The markup is how the page happens to express that today.

So: one pattern, tolerant of attributes, and `tile_states()` for the common question.
A fourth copy is now a diff a reviewer can see.
"""

from __future__ import annotations

import re

#: One direction tile: the state class, then everything inside the <li>.
#:
#: `[^>]*` after the class so any attribute may be added to the tile without touching
#: this again -- which is exactly what happened the day this was extracted.
TILE = r'<li class="swaptile swaptile-([a-z]+)"[^>]*>(.*?)</li>'

#: A tile's state and the direction LABEL inside it, for tests that care which pair
#: got which marker rather than what else the tile holds.
TILE_WITH_LABEL = (
    r'<li class="swaptile swaptile-([a-z]+)"[^>]*>\s*<span class="pair-label">(.*?)</span>'
)


def tile_states(body: str) -> list[tuple[str, str]]:
    """Every (state, inner html) pair the page rendered, in page order."""
    return re.findall(TILE, body, flags=re.DOTALL)


def tile_detail(body: str, from_asset: str, to_asset: str) -> tuple[str, str] | None:
    """(state, inner html) for ONE direction's tile, or None if the page drew none.

    WHY THIS EXISTS RATHER THAN A CALLER PAIRING tile_for() WITH tile_states(). The
    first version of that pairing, written 2026-10-07, looked like

        next((held for found, held in tile_states(body)
              if f'data-from="{from_asset}"' in body and found == state), "")

    and the `in body` test is true for EVERY tile on the page, so it returned the first
    tile sharing a STATE with the one asked for. On a page where several directions are
    offline -- which is the page that test runs against -- it asserted on a different
    tile than the one it named, and passed. A false pass in a test about a specific
    pair being specifically offline.

    One match, anchored on the data attributes, returning both halves, so a caller
    cannot pair them wrongly.
    """
    found = re.search(
        rf'<li class="swaptile swaptile-([a-z]+)"[^>]*\bdata-from="{from_asset}"'
        rf'[^>]*\bdata-to="{to_asset}"[^>]*>(.*?)</li>',
        body,
        flags=re.DOTALL,
    )
    return (found.group(1), found.group(2)) if found else None


def tile_for(body: str, from_asset: str, to_asset: str) -> str | None:
    """The state class of one direction's tile, or None if the page drew no tile for it.

    Matches on the DATA ATTRIBUTES rather than on the rendered arrow, because the
    label is an HTML entity (`&#8594;`) that a caller has to remember to unescape --
    and a test that forgets reports "no tile" for a tile that is right there. The data
    attributes exist for the lamp filter and are exactly the direction, unescaped.
    """
    found = re.search(
        rf'<li class="swaptile swaptile-([a-z]+)"[^>]*\bdata-from="{from_asset}"'
        rf'[^>]*\bdata-to="{to_asset}"',
        body,
    )
    return found.group(1) if found else None
