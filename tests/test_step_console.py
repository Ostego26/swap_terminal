#!/usr/bin/env python3
"""Two classes are named Console and they disagree about what a verdict is.

Role: test (behavioral verification of swap_terminal/step_console.py against
      swap_terminal/regtest/console.py's verdict constants)
Reads: both Console classes and regtest/console.py's OK/FAIL/SKIP/XFAIL
Writes: nothing but captured stdout
Can move funds: no. Nothing here opens a socket, reads a key or touches a chain.
Mainnet-safe: yes
Live-safe: yes

WHY THIS FILE EXISTS, AND IT IS NOT A TYPING TEST.

    step_console.Console.check(label, got, expected, ok: bool)        -> bool
    regtest.console.Console.check(label, got, expected, outcome: str) -> str

Same class name, same method name, same arity, same first three parameters.
regtest/console.py's verdicts are STRINGS -- OK = "OK", FAIL = "FAIL",
SKIP = "SKIP", XFAIL = "XFAIL" -- and every non-empty string is truthy. Before
2026-10-09, handing one of those to THIS Console printed "OK  ", appended a pass
to self.results, and made summary() return exit code 0. A FAIL produced a clean
exit, silently.

MEASURED THE SAME DAY, and it is latent rather than live: 19 files import a
Console, 8 this one and 11 that one, and NO file imports both. All 82 check()
calls in the 8 files pass a real boolean. What makes it worth a guard is that the
two populations are one copy-paste apart and nothing in either file looks wrong.

step_console.py's module header had already done rule 8's cross-reference -- it
names regtest/console.py and says why they are not merged -- but it named the
HARMLESS difference (chain prefixes, a ChainConfig a single-chain verifier has no
use for) and not this one. A note that tells a reader the other copy exists, and
describes the difference that costs nothing instead of the one that costs an exit
code, is the wrong-comment bug (rule 16).
"""

import pytest
from regtest.console import FAIL, OK, SKIP, XFAIL
from step_console import Console

# NO sys.path.insert AND NO E402 SUPPRESSION HERE, DELIBERATELY. Most test files in
# this suite carry both, and copying them was the first draft -- ruff's RUF100
# then reported that suppression as UNUSED, which is how I learned tests/conftest.py
# already puts swap_terminal/ on the path for every test in this directory. A
# suppression for a finding that does not fire is rule 19's shape exactly: it
# quiets nothing and teaches the next reader that the marker is decoration.


def test_every_regtest_verdict_constant_is_truthy_which_is_the_whole_hazard():
    """The premise, asserted rather than assumed.

    If one of these were ever falsy the guard below would be arguing against
    something that cannot happen, and this test would be the one that said so.
    """
    for verdict in (OK, FAIL, SKIP, XFAIL):
        assert isinstance(verdict, str), f"{verdict!r} is not a string; the hazard has changed shape"
        assert verdict, f"{verdict!r} is falsy, so passing it to a bool parameter would not silently pass"


@pytest.mark.parametrize("verdict", [OK, FAIL, SKIP, XFAIL])
def test_the_other_consoles_verdicts_are_refused_by_name(verdict, capsys):
    """A string verdict raises, and the message says where it probably came from.

    ALL FOUR, not just FAIL. FAIL is the one that costs money-shaped trust, but
    SKIP and XFAIL are the ones rule 13 is about -- "did nothing" reporting the
    same way as "did work". An operator reading OK beside a step that was skipped
    has been told the opposite of what happened.
    """
    console = Console(total_steps=1)
    with pytest.raises(TypeError) as raised:
        console.check("the label", "got", "expected", verdict)

    message = str(raised.value)
    assert "the label" in message, "the refusal must name which check was wrong"
    assert repr(verdict) in message, "the refusal must show the value it was given"
    assert "regtest" in message, "the refusal must point at where that value comes from"
    assert console.results == [], "a refused check must not be recorded as a result"
    assert "OK  " not in capsys.readouterr().out, "a refused check must not print a pass"


def test_a_real_bool_still_works_both_ways(capsys):
    """The guard must not have cost the 82 honest call sites anything."""
    console = Console(total_steps=1)
    assert console.check("passing", 1, 1, True) is True
    assert console.check("failing", 1, 2, False) is False
    assert console.results == [("passing", True), ("failing", False)]

    printed = capsys.readouterr().out
    assert "OK    passing" in printed
    assert "FAIL  failing" in printed


def test_a_truthy_non_verdict_is_refused_too():
    """Not a special case for four strings -- anything that is not a verdict.

    `1` and `"yes"` are the shapes a hurried caller writes, and `1 == True` in
    Python, so an isinstance check would have let the integer through:
    isinstance(True, int) is True, isinstance(1, bool) is False. The guard is
    identity against the two singletons for exactly that reason.
    """
    console = Console(total_steps=1)
    for wrong in (1, 0, "yes", "", None, [], ["anything"]):
        with pytest.raises(TypeError):
            console.check("label", "got", "expected", wrong)
    assert console.results == []


def test_the_summary_exit_code_is_what_the_hazard_was_about(capsys):
    """The consequence, end to end: a refused verdict never reaches the exit code.

    summary() returns 1 if any recorded result is False. The defect was that a
    FAIL string recorded a PASS, so a verifier that failed exited 0 -- which is
    rule 13's "treat skipped plus success in the same output as a defect in the
    output", one layer down in the thing that produces the output.
    """
    console = Console(total_steps=1)
    console.check("a real failure", 1, 2, False)
    assert console.summary() == 1, "a recorded failure must make the exit code non-zero"

    clean = Console(total_steps=1)
    clean.check("a real pass", 1, 1, True)
    assert clean.summary() == 0
    assert "unexpected failures: (none)" in capsys.readouterr().out
