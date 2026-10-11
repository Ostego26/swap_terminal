"""GRC's confirmation threshold is spelled in three files under TWO names. Hold them equal.

Role: test (read-only; reads three source files as text and config.Config)
Reads: swap_terminal/config.py, docker-compose.yml,
       swap_terminal/grc-sol-swap/abstergo_exchange/server.js
Writes: nothing
Can move funds: no
Mainnet-safe: yes -- no socket, no subprocess, no database.

=============================================================================
TWO NAMES FOR ONE NUMBER, AND THE SECOND ONE RELEASES FUNDS
=============================================================================

CLAUDE.md rule 11's fourth practical test is the one this file exists for: "Does
the identifier mean the same thing in Python and in JavaScript? The Flask app and
the Express server both name chains; if they disagree about what `GRC` implies,
one of them is wrong and neither will say so."

Measured 2026-10-11 by grepping the whole tree for both spellings:

    GRC_MIN_CONFIRMATIONS        config.py:608   _env_int(..., "6")
                                 read by services/swap_service.
                                 get_min_confirmations(), copied onto
                                 swaps.min_confirmations at creation
    GRIDCOIN_MIN_CONFIRMATIONS   server.js:88    Number(... || 6)
                                 read by server.js:348, which THROWS below the
                                 threshold and otherwise proceeds to pay SOL out
                                 against a Gridcoin deposit
                                 docker-compose.yml:411  "${...:-6}"

So there are two knobs and one question. docker-compose.yml's own header already
noticed -- "GRIDCOIN_MIN_CONFIRMATIONS=6, which agrees with BOTH places that
already state it -- server.js:88 and config.py's GRC_MIN_CONFIRMATIONS" -- and
nothing held that agreement. Grepped tests/ for either name in a confirmation
context: no test compared them. A noticed invariant with no gate is rule 8's bug
with a delay on it, and the delay here is however long until somebody acts on
config.py's own measurement.

THAT MEASUREMENT IS THE REASON THIS IS LIKELY TO DRIFT RATHER THAN A HYPOTHETICAL.
config.py:574-607 records, from the operator's live testnet desk on 2026-10-05,
that GRIDCOIN'S OWN WALLET DOES NOT COUNT AN INCOMING PAYMENT UNTIL DEPTH 10 --
`IsConfirmed()` wants 10 and `GetBalance()` skips a transaction that is
`!IsConfirmed() && !fFromMe`. It then declines to change the 6, correctly,
because a deposit threshold is live posture (rule 16). An operator who later acts
on that paragraph will set GRC_MIN_CONFIRMATIONS and will have no reason to think
a second variable exists -- and the Express server would keep releasing SOL at 6.

WHAT THIS TEST CAN AND CANNOT HOLD, said plainly (rule 17). It compares the three
SOURCE DEFAULTS, so it fails when somebody edits one file and not the others. It
CANNOT see an operator exporting one variable and not the other: that is a
runtime fact about a host, and nothing in a test suite can reach it. The runtime
half is named in the report rather than pretended away -- what would close it is
the Express server reading its threshold from the same authority the Flask app
does, which is a change to a fund-releasing path and the operator's (rule 16).

THE DEFAULTS ARE READ FROM SOURCE AND NOT FROM Config, deliberately.
`Config.GRC_MIN_CONFIRMATIONS` resolves the environment at import, so a suite run
on a host that exports it would compare a host's choice against two literals and
fail for the wrong reason. The subject is what the three files SAY when nobody
has chosen.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from config import Config

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

#: Where each default lives and the pattern that reads it. The pattern captures the
#: NUMBER, so a test failure prints the two values rather than "a regex did not
#: match" -- and each one anchors on its own file's syntax rather than on a loose
#: search for the digit 6, which would match any 6 anywhere in a 35,000-line compose
#: file.
THRESHOLD_SITES = {
    "swap_terminal/config.py (GRC_MIN_CONFIRMATIONS, the Flask authority)":
        r'GRC_MIN_CONFIRMATIONS\s*=\s*_env_int\(\s*"GRC_MIN_CONFIRMATIONS"\s*,\s*"(\d+)"\s*\)',
    "swap_terminal/grc-sol-swap/abstergo_exchange/server.js (GRIDCOIN_MIN_CONFIRMATIONS, the Express gate that releases SOL)":
        r"GRIDCOIN_MIN_CONFIRMATIONS\s*=\s*Number\(\s*process\.env\.GRIDCOIN_MIN_CONFIRMATIONS\s*\|\|\s*(\d+)\s*\)",
    "docker-compose.yml (what the abstergo service is given)":
        r'GRIDCOIN_MIN_CONFIRMATIONS:\s*"\$\{GRIDCOIN_MIN_CONFIRMATIONS:-(\d+)\}"',
}

#: The path each site lives in, split out so a missing FILE and a missing PATTERN are
#: different failures. A test that reported "pattern not found" for a file somebody
#: moved would send a reader looking at the wrong thing.
SITE_PATHS = {
    label: REPOSITORY_ROOT / label.split(" (")[0] for label in THRESHOLD_SITES
}


def read_default(label: str) -> int:
    path = SITE_PATHS[label]
    assert path.exists(), (
        f"{path} does not exist, so this gate cannot compare GRC's confirmation threshold "
        f"across the suites that use it. If that file moved, this test moves with it rather "
        f"than being deleted -- the invariant is the agreement, not the path"
    )
    found = re.search(THRESHOLD_SITES[label], path.read_text())
    assert found is not None, (
        f"{path}: no GRC confirmation default matched. Either the spelling changed or the "
        f"default was removed; both need a human, because a gate that silently stops "
        f"matching reports clean forever (CLAUDE.md: 'the denominator is the patterns, not "
        f"the language')"
    )
    return int(found.group(1))


@pytest.mark.parametrize("label", list(THRESHOLD_SITES))
def test_each_site_still_states_a_threshold(label):
    """The non-vacuity floor. A gate asserting an agreement is loudest when it can read.

    THIS IS NOT PADDING. The failure mode of the test below is that one of its three
    regexes stops matching and `read_default` raises -- which looks like a failure, so
    that is fine -- but the failure mode of a CHANGED pattern plus a RELAXED assertion
    is a green test over three files it no longer reads. Asserting each site
    individually means a failure names the file.
    """
    assert read_default(label) > 0


def test_the_python_and_the_javascript_default_to_the_same_depth():
    """THE INVARIANT. One question, two variable names, and they must answer alike.

    MUTATION, run 2026-10-11: change server.js:88's fallback from 6 to 10 (the depth
    Gridcoin's own wallet uses, which is the realistic drift and not an arbitrary
    one). This fails and names both values. Restored; the file is unchanged by this
    agent, and `python3 -m ruff check` does not cover grc-sol-swap at all
    (pyproject.toml excludes it), so a drift there has no other gate.
    """
    defaults = {label: read_default(label) for label in THRESHOLD_SITES}

    assert len(set(defaults.values())) == 1, (
        "GRC's confirmation threshold disagrees between the suites that read it, and the "
        "Express server's copy is the one that releases SOL against a Gridcoin deposit:\n"
        + "\n".join(f"    {value}  {label}" for label, value in defaults.items())
        + "\nThese are TWO ENVIRONMENT VARIABLES (GRC_MIN_CONFIRMATIONS for Python, "
        "GRIDCOIN_MIN_CONFIRMATIONS for Node and compose), so changing one does not change "
        "the other. Rule 11: one vocabulary, derived in one place, applied identically."
    )


def test_the_configured_value_is_the_one_the_measurement_was_taken_against():
    """Config resolves to 6 in a default environment, which is what config.py documents.

    SEPARATE FROM THE AGREEMENT ABOVE, and reading Config rather than source, because
    this asserts something different: that the number a swap is actually created with
    is the number the 2026-10-05 measurement was taken against. config.py's paragraph
    says at length that 6 credits a GRC deposit four blocks before Gridcoin's own
    wallet counts it, and that the gap understates payout capacity rather than
    releasing early. That whole argument is about the value 6.

    IT IS SKIPPED RATHER THAN FAILED when the environment overrides it, because an
    operator who has deliberately raised the threshold has not broken anything -- and
    a test that failed on their host would be this suite objecting to a decision rule
    16 reserves for them.
    """
    source_default = read_default(
        "swap_terminal/config.py (GRC_MIN_CONFIRMATIONS, the Flask authority)"
    )
    if source_default != Config.GRC_MIN_CONFIRMATIONS:
        pytest.skip(
            f"GRC_MIN_CONFIRMATIONS is overridden in this environment "
            f"({Config.GRC_MIN_CONFIRMATIONS} against a source default of {source_default}), "
            f"which is the operator's call"
        )
    assert Config.GRC_MIN_CONFIRMATIONS == 6, (
        "config.py's measurement of the 6-versus-10 gap, and chains/gridcoin.py's note on "
        "what it costs, are both written about the value 6"
    )
