#!/usr/bin/env python3
"""Every class the ATM flow emits has a CSS rule. A clean gate, not a baseline.

Role: file (entry point -- `python3 -m pytest tests/test_atm_surface_is_styled.py`)
Reads: swap_terminal/templates/*.html and swap_terminal/static/*.css
Writes: nothing
Can move funds: no
Live-safe: yes

WHY THIS EXISTS, measured 2026-10-08.

The operator asked for "more color" on the ATM screen. The screen was not short
of colour -- the whole surface had no stylesheet. Read across every class the
templates emit against every rule in static/*.css, 49 class names in the tree
had no rule anywhere and 28 of them were the ATM's: the step strip, the
question, the hint, the error box, the answers-so-far summary, the back button,
the amount screen's three ceiling readings, the review list, the counterparty
warning, the commit button.

Nothing failed. An unstyled element renders -- as unstyled text -- so the flow
worked, the tests passed, and the only report was the operator pasting their own
screen back with

    BTCSOME 2/5 out

in it, two labels run together inside a button nobody had styled.

THE MECHANISM, because it is the reusable part. admin.html's lamp strip says
`class="lamp lamp-{{ key }}"` and the ATM's chooser said
`class="atm-choice lamp-{{ key }}"`. The state modifier matched; the base class
did not. `.lamp` is what declares `grid-template-areas: "dot asset" "word word"
"counts counts"`, so every `grid-area` on the children pointed at a grid that
did not exist. One widget, two spellings, drifting silently -- rule 8's subject
exactly, in a pair of languages neither of which type-checks the other.

NO BASELINE, AND THAT IS RULE 19 RATHER THAN AMBITION. A per-file baseline here
would make the check green over the 28 defects it was written to find, and
"nothing came off any of them until somebody was told to" is what that rule
records about every ratchet in the sibling repository. The ATM templates are the
files this change touched (rule 12), they are clean, and the gate holds them at
clean.

WHAT IS NOT COVERED, named so it is work rather than a surprise. Seven class
names on four other surfaces are still unstyled: base.html's `brand-text`,
_badges.html's `badge-word`, _copy_field.html's `copy-word`, admin.html's
`empty-what`/`reason-cell`/`row-reason`, swap.html's `wallet-entry-unusable`,
and `live` in _swap_live.html and swap_not_found_fragment.html. They are outside
this change's files, so sweeping them would be the large-diff-no-benefit trade
rule 12 refuses -- and they are listed here, and in styles.css's ATM block, so
the next person in those files has the list.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from services.asset_identity import SYMBOLS, color_class_for

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = REPO_ROOT / "swap_terminal" / "templates"
STATIC = REPO_ROOT / "swap_terminal" / "static"

#: The ATM flow: its shell and every step partial it includes.
#:
#: NAMED EXPLICITLY rather than globbed as `_atm_*`. A glob would silently
#: start covering a partial somebody adds -- which sounds like a feature and is
#: how a clean gate turns into a failing one on unrelated work, which rule 19
#: calls "a ratchet somebody deletes". A new ATM screen joins this list in the
#: commit that adds it.
ATM_TEMPLATES = (
    "atm.html",
    "_atm_from_asset.html",
    "_atm_to_asset.html",
    "_atm_amount.html",
    "_atm_payout_address.html",
    "_atm_confirm.html",
    "_atm_deposit.html",
    "_asset_mark.html",
)

#: A Jinja expression or statement anywhere inside a class attribute.
#:
#: STRIPPED BEFORE SPLITTING, because `class="atm-choice lamp lamp-{{ lamp.key }}
#: {{ lamp.color_class }}"` is three literal classes and two expressions, and
#: the identifiers inside the expressions are not class names. A first pass of
#: this measurement counted `lamp.key`, `if`, `else` and `'lamp-all'` as
#: unstyled classes and reported 153 where the real number was 49 -- a
#: measurement whose denominator was wrong, which rule 3 is about.
_JINJA = re.compile(r"\{\{.*?\}\}|\{%.*?%\}", re.DOTALL)

#: `class="..."` on any element.
_CLASS_ATTR = re.compile(r'class="([^"]*)"')

#: A class SELECTOR in a stylesheet. Deliberately loose -- it matches
#: `.atm-step-done` in a selector and would also match one inside a comment,
#: which is the safe direction to be wrong in: a false POSITIVE here means the
#: gate accepts a class whose rule is only mentioned in prose, where a false
#: negative would fail the suite over a styled element.
_CSS_CLASS = re.compile(r"\.([A-Za-z][A-Za-z0-9_-]*)")


def styled_class_names() -> set[str]:
    """Every class name with at least one rule, across EVERY stylesheet.

    All three, not just styles.css: `killsw-*` lives in kill_switch.css and
    `grcproof-*` in grc_address_proof.css, and reading one file would report
    two whole surfaces as unstyled.
    """
    names: set[str] = set()
    sheets = sorted(STATIC.glob("*.css"))
    assert sheets, f"no stylesheet under {STATIC} at all -- this gate cannot mean anything"
    for sheet in sheets:
        names |= set(_CSS_CLASS.findall(sheet.read_text()))
    return names


def emitted_class_names(template: Path) -> set[str]:
    """The LITERAL class names one template emits, Jinja removed."""
    emitted: set[str] = set()
    for match in _CLASS_ATTR.finditer(template.read_text()):
        literal = _JINJA.sub(" ", match.group(1))
        emitted |= {token for token in literal.split() if token}
    return emitted


def unstyled_in(template: Path, styled: set[str]) -> list[str]:
    """Which of a template's classes have no rule. Handles the DYNAMIC ones.

    A class attribute routinely interpolates the tail of a name --
    `class="atm-step atm-step-{{ entry.state }}"` -- and stripping the Jinja
    leaves the bare prefix `atm-step-`. The runtime values are not knowable from
    the source, so a literal lookup for `atm-step-` finds nothing and reports a
    perfectly styled element as unstyled, which this test did on its first run.

    So a token ending in `-` is matched as a PREFIX: some styled class must
    start with it. That is weaker than checking every value -- `atm-step-future`
    could be dropped and `atm-step-done` would carry the prefix -- and it is the
    strongest claim the source supports, which is the honest place to stop
    (rule 17). The three-state strip is pinned exactly by
    `the_step_strip_styles_all_three_states` below, where the values ARE known.
    """
    missing = []
    for name in sorted(emitted_class_names(template)):
        if name.endswith("-"):
            if not any(candidate.startswith(name) for candidate in styled):
                missing.append(f"{name}* (a dynamic class; NO rule matches the prefix)")
        elif name not in styled:
            missing.append(name)
    return missing


@pytest.mark.parametrize("name", ATM_TEMPLATES)
def test_every_class_the_atm_emits_has_a_rule(name):
    """One parametrization per template, so a failure names the file."""
    template = TEMPLATES / name
    assert template.is_file(), (
        f"{template} is missing. If an ATM screen was renamed or removed, update "
        "ATM_TEMPLATES in the same commit -- this list is deliberately explicit."
    )

    unstyled = unstyled_in(template, styled_class_names())
    assert not unstyled, (
        f"{name} emits {unstyled}, which no stylesheet under {STATIC} has a rule for.\n"
        "An unstyled element RENDERS -- as unstyled text -- so nothing else in this suite "
        "will tell you. That is how the whole ATM flow shipped without a stylesheet: the "
        "operator found it by pasting their own screen back with 'BTCSOME 2/5 out' in it.\n"
        "Add the rule. Do NOT add the class to an exclusion list: there isn't one, on "
        "purpose (rule 19)."
    )


def test_the_tiles_OWN_class_declares_every_area_its_children_use():
    """`.atm-choice` must place its own children. Nothing may be borrowed.

    THIS TEST REPLACED ONE THAT BLESSED THE WRONG FIX, and the replacement is
    the point. The first version asserted the choosers set the `lamp` base
    class -- because `.lamp` declares `grid-template-areas` and the tile's spans
    carry `grid-area`, so borrowing it made the unstyled tiles lay out. A
    mutation check then stripped `grid-template-areas` from `.lamp` and NOTHING
    FAILED: `.atm-choice` had grown its own grid (with the extra `mark` row) in
    the same change, so `.lamp` was never load-bearing here. What the markup
    actually had was two `display:grid` declarations and two
    `grid-template-areas` on one element, resolved by whichever rule sat later
    in the file.

    A surviving mutation is usually a weak test. This one was a weak FIX, and
    the test was what made it look strong -- so `lamp` came off the tiles and
    this asserts the thing that has to be true instead: the class the tile
    actually carries declares every area the tile's children are placed into.

    Parsed out of `.atm-choice`'s own rule block rather than the whole
    stylesheet, because "some grid somewhere declares `dot`" is exactly the
    claim that let the borrowed grid pass.
    """
    css = (STATIC / "styles.css").read_text()
    start = css.find(".atm-choice {")
    assert start != -1, "no .atm-choice rule block in styles.css at all"
    block = css[start : css.index("\n}", start)]

    assert "display: grid" in block, ".atm-choice must declare its own grid, not borrow one"
    areas = re.search(r"grid-template-areas:([^;]+);", block)
    assert areas, ".atm-choice declares no grid-template-areas, so every grid-area is inert"
    declared = set(re.findall(r"[a-z-]+", areas.group(1)))

    # Exactly the areas the two choosers place children into.
    for area in ("mark", "dot", "asset", "word", "counts"):
        assert area in declared, (
            f".atm-choice's grid has no `{area}` area, but a tile child is placed into it "
            f"with grid-area. Declared: {sorted(declared)}. The child gets auto-placed and "
            "the tile silently stops being a layout -- which is what produced "
            "'BTCSOME 2/5 out' on the operator's screen."
        )


def test_the_tiles_do_not_ALSO_take_the_operator_strips_grid():
    """Two grids on one element is a source-order coin flip. Pin it at one.

    `.lamp` (admin.html's strip) and `.atm-choice` both declare `display:grid`
    and `grid-template-areas`, with DIFFERENT areas -- `.lamp` has no `mark`
    row. An element carrying both gets whichever rule the file happens to put
    last, so a reorder of this stylesheet would move the ATM's tiles without
    anybody touching them.
    """
    for name in ("_atm_from_asset.html", "_atm_to_asset.html"):
        emitted = emitted_class_names(TEMPLATES / name)
        assert "atm-choice" in emitted, f"{name}'s tiles are no longer .atm-choice"
        assert "lamp" not in emitted, (
            f"{name} sets BOTH `atm-choice` and `lamp`, and both declare a grid with "
            "different areas. `.atm-choice` is the tile's own layout and `.lamp` is the "
            "operator strip's; which one wins is decided by source order in styles.css. "
            "The state is carried by `lamp-<state>`, which is colour only -- that one stays."
        )


def test_the_step_strip_styles_all_three_states():
    """The prefix check above cannot see these, and the strip is the whole flow.

    `atm-step-{{ entry.state }}` takes exactly three values -- services/wizard
    decides done/current/future -- so unlike a general dynamic class these ARE
    knowable, and a missing one means a step in the strip renders identically to
    its neighbours. That is the "did nothing looks like did work" failure (rule
    14) applied to the one widget whose entire job is saying where you are.
    """
    css = "\n".join(sheet.read_text() for sheet in sorted(STATIC.glob("*.css")))
    for state in ("done", "current", "future"):
        assert f".atm-step-{state}" in css, (
            f"no rule for .atm-step-{state}. The strip has three states and this one would "
            "render the same as the others, so the page would show no position at all."
        )


def test_each_coin_has_an_accent_rule_and_a_token_behind_it():
    """A coin class with no rule is a tile whose mark inherits the page's green.

    services/asset_identity.color_class_for() mints `coin-<ticker>` for whatever
    asset it is handed, so the class always EXISTS in the markup and a missing
    rule is silent -- six tiles in one colour, which is the state the per-coin
    palette was added to leave. Checked against SYMBOLS rather than a list
    retyped here, so adding a coin to that table fails this until its colour
    exists (rule 8: one place, everything derived from it).
    """
    css = "\n".join(sheet.read_text() for sheet in sorted(STATIC.glob("*.css")))
    for asset in SYMBOLS:
        klass = color_class_for(asset)
        assert f".{klass} " in css or f".{klass}," in css or f".{klass}." in css, (
            f"{asset} is in services/asset_identity.SYMBOLS and nothing styles .{klass}, so "
            "its mark and symbol inherit the page's phosphor green and the tile is "
            "indistinguishable from every other coin."
        )
        assert f"--{klass}:" in css, (
            f"no --{klass} token on :root. The palette is declared once there (styles.css's "
            "own header), so a hex value spelled in the rule instead would be the drift that "
            "file exists to prevent."
        )
