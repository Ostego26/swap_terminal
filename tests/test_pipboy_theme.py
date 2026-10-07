"""Does the CRT layer stay out of the way, and do colors stay in the tokens?

Role: test / measurement (reads the stylesheet; renders nothing and starts no
      browser)
Reads: swap_terminal/static/styles.css
Writes: nothing
Can move funds: no
Mainnet-safe: yes

WHY THIS FILE EXISTS. The Pip-Boy retheme (operator, 2026-10-07: "make the theme
green black and red and use more pip boy looks from fallout") added the first
thing in this stylesheet that can break the APPLICATION rather than its looks: a
fixed full-viewport overlay for the scanlines. One missing declaration on it and
that layer sits over every control on both surfaces and swallows the clicks --
the kill switch on /admin, and whichever step a swap is on at /.

THESE ARE TEXTUAL ASSERTIONS AND I AM NAMING THAT RATHER THAN HIDING IT. The
behavioral-verification principle asks for row-level outcomes from running the
real thing, and the real thing here is a browser dispatching a click through a
stacking context -- which this container has no way to do. What these CAN hold is
the specific regression: the declaration disappearing, a color arriving outside
the tokens, or a light theme coming back. A click test belongs to whoever next
opens the page.
"""

from __future__ import annotations

import re
from pathlib import Path

CSS = (Path(__file__).resolve().parents[1] / "swap_terminal" / "static" / "styles.css").read_text()


def test_the_scanline_overlay_cannot_swallow_a_click():
    """`pointer-events: none` on the overlay, which is load-bearing and not cosmetic.

    The overlay is `position: fixed; inset: 0; z-index: 9999`, so without this it
    is in front of everything on the page. An ornament that eats input is worse
    than no ornament, and the two things it would eat are the operator's kill
    switch and a customer's answer to the step they are on.
    """
    overlay = re.search(r"body::after\s*\{(.*?)\}", CSS, re.DOTALL)
    assert overlay, "the scanline overlay is gone; if that is intended, this test goes with it"
    block = overlay.group(1)
    assert re.search(r"pointer-events:\s*none", block), (
        "the scanline overlay has no `pointer-events: none`. It is position:fixed inset:0 "
        "z-index:9999, so it now covers every button on both surfaces -- including /admin's "
        "kill switch and the ATM's step buttons."
    )
    # AND IT MUST STAY A PSEUDO-ELEMENT. A real <div> would need aria-hidden and
    # could lose it in an edit; ::after is invisible to the accessibility tree by
    # construction, which is why it is one.
    assert "<div" not in block


def test_no_light_theme_came_back():
    """One theme. A phosphor tube has no light variant (rule 9).

    The prefers-color-scheme block that used to re-point these tokens is deleted.
    Reintroducing one would be a second theme nobody asked for -- and, worse, half
    the rules would be tuned for a palette that no longer exists.
    """
    blocks = re.findall(r"@media[^{]*prefers-color-scheme[^{]*\{", CSS)
    assert not blocks, f"a prefers-color-scheme block is back: {blocks}"


def test_every_colour_outside_the_token_block_goes_through_a_token():
    """This file's own rule 8 claim, enforced rather than asserted in a comment.

    Its header says every colour is "a custom property on :root, declared once"
    because the file it replaced "spelled #2670B9 in three rules and its own shade
    of gray in five more". A retheme is exactly when that drifts: a rule tuned
    against the old blue gets a hand-picked green and the next one gets a slightly
    different green.

    THE :root BLOCK AND THE GLOW TOKENS ARE EXEMPT BY CONSTRUCTION -- they are
    where the literals are supposed to live. Everything after it must reference a
    token, with rgba() allowed for the bloom and the scanlines: those are
    translucent overlays of one colour, and `rgba(var(--x))` is not valid CSS
    without a separate channel token, which would be a token nobody reads.
    """
    after_root = CSS[CSS.index("* { box-sizing: border-box; }"):]
    literals = re.findall(r"#[0-9a-fA-F]{3,8}\b", after_root)
    assert not literals, (
        f"{len(literals)} hex colour(s) outside the token block: {sorted(set(literals))}. "
        f"Every colour belongs to a custom property on :root -- this file's header says so and "
        f"names the drift it cost last time."
    )


def test_the_state_border_treatments_survived_the_retheme():
    """Solid / dashed / double / heavy, which carry MORE of the signal now.

    The palette went from blues-and-greens to two hues, so the border weight is a
    larger share of what tells a waiting state from a slow one from a halted one.
    A retheme that kept the colours tidy and dropped these would make the page
    prettier and less readable, and it would break the file's own promise that no
    state is signaled by colour alone.
    """
    assert "dashed var(--line-strong)" in CSS or "dashed" in CSS, "the waiting treatment is gone"
    assert "double var(--slow)" in CSS, "the slow state's double border is gone"
    assert "solid var(--halted)" in CSS, "the halted state's solid rule is gone"


def test_no_two_states_share_a_colour():
    """Every state is its own hue, which the first Pip-Boy pass did NOT satisfy.

    THIS TEST ASSERTED THE DEFECT UNTIL 2026-10-07. It was
    test_slow_and_halted_are_not_the_same_colour, and its last line read

        assert token("ok") == token("working") == token("accent")

    -- pinning that settled and in-progress were the SAME green. That was a true
    description of the two-hue palette and a bad property to hold: "this swap is
    done" and "this swap is mid-payout" looked identical, and only the word and
    the border weight told them apart. The colour-alone prohibition in
    styles.css's header was being satisfied on a technicality rather than
    honoured, by a test that locked it in place.

    Operator, same day: "i do want more color but still want the main black and
    green and blue lookout." So green now means SETTLED and blue means IN MOTION
    -- the division services/swap_view.py's own status rail already makes, where
    every stage before the last is something happening rather than something
    true.

    ASSERTED AS MUTUAL DISTINCTNESS rather than as a list of pairs, so it scales:
    a seventh state added later cannot quietly borrow a sixth state's colour, and
    nobody has to remember to add a line here.
    """
    states = ("ok", "waiting", "working", "slow", "halted", "unknown")

    def token(name):
        found = re.search(rf"--{name}:\s*([^;]+);", CSS)
        assert found, f"--{name} is not declared"
        return found.group(1).strip()

    colours = {name: token(name) for name in states}
    clashes = [
        (a, b) for i, a in enumerate(states) for b in states[i + 1:]
        if colours[a] == colours[b]
    ]
    assert not clashes, (
        f"these states share a colour: {clashes}. On a palette this dark the hue is the only "
        f"thing read at a glance -- the word and the border are what a reader falls back to, not "
        f"what they use first. Colours are: {colours}"
    )

    # AND THE TWO STRUCTURAL HUES ARE THE ONES THE OPERATOR ASKED FOR: the page's
    # voice is green (`--ink`) and its accent is blue. A retheme that made the
    # accent green again would collapse settled and in-progress back together.
    assert token("ink") == token("ok"), "body text and the settled state are the same phosphor green"
    assert token("accent") == token("working"), "the accent IS the in-motion colour, not a third thing"
    assert token("accent") != token("ok"), (
        "the accent must not be the settled green -- that is the collapse this test exists to stop"
    )
