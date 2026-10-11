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

    # =====================================================================
    # COMMENTS ARE STRIPPED FIRST, AND THIS GATE FAILED ON ITS OWN DOCUMENTATION
    # =====================================================================
    #
    # IT WAS NOT WHAT FIXED THE RED, and that is recorded because the wrong diagnosis is
    # the instructive part. The gate reported `['#fff']` and there is a `#fff` quoted
    # inside the comment at --qr-quiet-zone -- "IT WAS A BARE `#fff` IN THE RULE UNTIL
    # 2026-10-07" -- which looked like the whole story. It was not: that comment sits
    # BEFORE the token block, which `after_root` already excludes. The real offender was
    # `color: var(--paper, #fff)` in .rescue-button:hover, where `--paper` is declared
    # nowhere, so the rule was a hard-coded white. Stripping comments did not and could
    # not have fixed it.
    #
    # THE STRIP STAYS ANYWAY, on its own merit: a gate that fails on the note documenting
    # its own rule is a gate somebody deletes, and the next hand-picked green then goes in
    # unnoticed. It is a hazard that had not fired yet rather than the one that had.
    #
    # MEASURED: red at 30512de, before today's commits, so the .rescue-button defect was
    # introduced earlier the same day rather than by this batch -- and it was mine.
    #
    # THE SAME DEFECT AND THE SAME FIX ONE FILE OVER (rule 8).
    # tests/test_env_example_covers_every_compose_variable.referenced_variables() excludes
    # comment lines, and its own comment says why: "these compose files carry long
    # explanatory comments that QUOTE example references ... Counting them would put two
    # variables that do not exist into the required set." Identical shape -- prose that
    # names the forbidden thing in order to forbid it. CLAUDE.md rule 18 does it too,
    # quoting the British spellings it bans, and says so.
    #
    # NON-GREEDY AND re.DOTALL: CSS has only /* ... */ comments, and `.*?` with re.DOTALL stops at
    # the first close rather than swallowing every rule between the first comment's open
    # and the last one's close -- which would make this gate pass by scanning almost
    # nothing, the failure mode that matters most for a check like this.
    scannable = re.sub(r"/\*.*?\*/", "", after_root, flags=re.DOTALL)
    literals = re.findall(r"#[0-9a-fA-F]{3,8}\b", scannable)
    assert not literals, (
        f"{len(literals)} hex colour(s) outside the token block: {sorted(set(literals))}. "
        f"Every colour belongs to a custom property on :root -- this file's header says so and "
        f"names the drift it cost last time."
    )

    # AND THE STRIP MUST NOT HAVE EATEN THE FILE, which is the half that keeps this honest:
    # a regex that removed everything would make the assertion above vacuous and the gate
    # would read green forever. Asserted as a proportion rather than a byte count so it
    # does not need editing every time a comment is added.
    assert len(scannable) > len(after_root) * 0.3, (
        f"stripping comments left {len(scannable)} of {len(after_root)} characters, so this gate "
        f"is scanning almost nothing and would pass whatever the rules contain"
    )
    assert "var(--" in scannable, "the stripped text contains no token reference at all"


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
