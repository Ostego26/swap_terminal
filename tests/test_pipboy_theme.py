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


def test_slow_and_halted_are_not_the_same_colour():
    """Two states one hue apart would be the colour-alone failure, inverted.

    The operator asked for "green black and red". Amber is the one addition, and
    it is here because `--slow` and `--halted` would otherwise both be red with
    only a border weight between them. It is also the Pip-Boy's own second
    phosphor, so it is in the world rather than imported into it -- but the reason
    it is allowed is this assertion, not the lore.
    """
    def token(name):
        found = re.search(rf"--{name}:\s*([^;]+);", CSS)
        assert found, f"--{name} is not declared"
        return found.group(1).strip()

    assert token("slow") != token("halted")
    assert token("ok") != token("halted"), "settled and halted must never share a colour"
    # And the green states agree with each other, because they ARE one state on a
    # two-hue tube: ok, working and accent all read as "this is fine".
    assert token("ok") == token("working") == token("accent")
