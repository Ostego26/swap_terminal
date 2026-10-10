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

import ast
import inspect
import pathlib

import pytest
from conftest import TranscriptConsole
from regtest.console import FAIL, OK, SKIP, XFAIL
from step_console import Console
from test_swap_runners_report_completion import RecordingConsole

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


# ---------------------------------------------------------------------------
# THE SHARED RECORDER, added 2026-10-10 with conftest.TranscriptConsole.
#
# Three test files each defined a character-identical recorder, and two of the
# three carried this as a COMMENT inside check():
#
#     # step_console.Console.check's real signature. A recorder with fewer
#     # arguments would silently accept a call the real Console rejects.
#
# A claim in a comment is checked by whoever reads it. These are the same claim
# as assertions, in the file whose whole subject is two Consoles that look
# identical and are not.
# ---------------------------------------------------------------------------


def _shape(sig: inspect.Signature) -> list[tuple[str, object, bool]]:
    """A signature as (name, kind, is-required) per parameter. Three fields, three reasons.

    NAME, because a checker matches parameter names for anything not positional-only --
    step_console.StepNarrator's docstring records that `say(self, *_args, **_kwargs)` and
    a recorder naming it `line` both fail to satisfy `say(self, text: str)`.

    KIND, because a parameter that is positional-only in one and normal in the other
    accepts a keyword call in one and raises in the other.

    IS-REQUIRED, AND THIS ONE IS HERE BECAUSE ITS ABSENCE SURVIVED A MUTATION. The first
    version of this helper compared names and kinds only, and `def check(self, label, got,
    expected, ok=None)` passed clean -- a recorder that accepts `check(label, got,
    expected)` with no verdict at all, which is a TypeError against the real Console. That
    is word for word the hazard the comment this test replaced described ("a recorder with
    fewer arguments would silently accept a call the real Console rejects"), so a test that
    missed it was testing the easy half.

    ANNOTATIONS ARE DELIBERATELY ABSENT from the tuple -- see the test below for why.
    """
    return [(p.name, p.kind, p.default is inspect.Parameter.empty) for p in sig.parameters.values()]


@pytest.mark.parametrize("method", ["say", "check"])
def test_the_shared_recorder_matches_the_real_Consoles_signature(method):
    """Parameter NAMES, not just arity, because the names are what a checker matches.

    step_console.StepReporter is positional-only (`/`) for exactly three of check's
    four parameters, and its docstring records why: pyright matches parameter names for
    a normal parameter, so a recorder written `def say(self, *_args, **_kwargs)` does
    not satisfy `say(self, text: str)` and one naming it `line` does not either. `ok` is
    deliberately NOT positional-only there, which means its name is load-bearing.

    Compared against the concrete Console rather than the Protocol, because that is what
    the deleted comment claimed and it is the stronger statement: a stub matching
    Console's full parameter list satisfies every rung of the ladder above it, and a stub
    matching only the Protocol could still be refused by a call site that passes a
    keyword Console accepts.

    ANNOTATIONS ARE NOT COMPARED. Console.check takes `ok: object` so it can REFUSE a
    non-bool at runtime -- the refusal the rest of this file is about -- while the
    recorder records whatever it is handed. Asserting annotation equality would demand
    the recorder re-implement that guard, which is not its job.
    """
    real = inspect.signature(getattr(Console, method))
    stub = inspect.signature(getattr(TranscriptConsole, method))
    assert _shape(stub) == _shape(real), (
        f"TranscriptConsole.{method}{stub} does not match Console.{method}{real}. A recorder "
        f"with different parameters silently accepts a call the real Console rejects, or is "
        f"refused by a call site the real Console accepts -- and the tests using it would "
        f"pass either way, because they never run against the real Console"
    )


def test_the_shared_recorder_RETURNS_the_verdict_it_was_handed():
    """A recorder returning None turns every `if console.check(...)` into a false.

    Console.check returns its verdict and callers branch on it. This is the one piece of
    BEHAVIOR the signature test above cannot see, and getting it wrong does not raise --
    it makes the branch not taken, which is a test passing for a reason unrelated to its
    claim.
    """
    recorder = TranscriptConsole()
    assert recorder.check("a pass", 1, 1, True) is True
    assert recorder.check("a failure", 1, 2, False) is False
    assert "CHECK a pass got=1 expected=1 ok=True" in recorder.text()
    assert "CHECK a failure got=1 expected=2 ok=False" in recorder.text(), (
        "and both are in the transcript, in order, which is what the three files that "
        "shared this class assert on"
    )


def test_the_shared_recorder_is_NOT_a_Console_and_never_becomes_one():
    """The invariant step_console.StepNarrator's docstring argues at length for.

    "The fix is NOT to make those recorders subclass Console. Their whole value is
    failing loudly the day a production function reaches for something new; inheriting
    would have the shipped Console answer instead, silently."

    Making TranscriptConsole a subclass would pass the signature test above -- it would
    inherit the signatures -- and would quietly give eighteen call sites a console that
    PRINTS, banners, steps and owns a stopwatch. This is the assertion that notices.
    """
    assert not issubclass(TranscriptConsole, Console)
    for printing in ("banner", "step", "summary", "elapsed"):
        assert not hasattr(TranscriptConsole, printing), (
            f"TranscriptConsole grew {printing}(), which means it is drifting toward being a "
            f"Console. The recorder for the five-member runner surface already exists and is "
            f"tests/test_swap_runners_report_completion.py::RecordingConsole"
        )


def test_the_two_recorders_DISAGREE_about_check_and_that_is_the_point():
    """Pinned so a later merge cannot flatten them without a test saying so.

    conftest.TranscriptConsole and tests/test_swap_runners_report_completion.py's
    RecordingConsole are both console recorders and are NOT the same thing:

        TranscriptConsole   check() goes into `lines`, with say(), as text
        RecordingConsole    check() goes into `results`; `lines` holds NO checks

    Both docstrings name the other, which is rule 8's requirement for a difference that
    is deliberate. This is that requirement as an assertion -- an assertion written
    against one and moved to the other passes or fails for the wrong reason, and the two
    were one name apart until 2026-10-10, when the first version of TranscriptConsole was
    called RecordingConsole.

    RecordingConsole IS IMPORTED AT MODULE SCOPE, from a test module, which one docstring
    in this suite used to argue against on the grounds that it "would make the two files'
    collection order matter". It does not: conftest.py puts tests/ on sys.path for every
    test in this directory, so `from test_swap_runners_report_completion import ...` is an
    ordinary import that resolves whether or not pytest has collected that file yet.
    tests/test_solana_payout.py has imported tests/test_solana_adapter this way since
    before today. What WOULD matter is sharing a helper this way rather than asserting on
    one -- a shared helper belongs in conftest.py, which is where TranscriptConsole is.
    """
    transcript = TranscriptConsole()
    transcript.say("a line")
    transcript.check("a label", 1, 2, False)
    assert len(transcript.lines) == 2, "the say AND the check"

    session = RecordingConsole()
    session.say("a line")
    session.check("a label", 1, 2, False)
    assert session.lines == ["a line"], "the say only -- the check is not a line here"
    assert session.results == [("a label", False)], "it is a verdict, which is what summary() reads"
    assert session.summary() == 1, "and the exit code TranscriptConsole does not model at all"


# ---------------------------------------------------------------------------
# THE CLEAN GATE, added 2026-10-10 the moment the count reached zero.
#
# It lives in THIS file because its subject is this file's subject: console
# classes that look identical and are not. CLAUDE.md rule 19 is explicit that a
# ratchet -- a per-file baseline a check compares against, so a NEW violation
# fails while the existing backlog is tolerated -- "is not a place to put work
# down", and that "a ratchet that reaches zero gets DELETED... a clean gate is
# the goal." This is the goal rather than the way station: it holds no list of
# tolerated sites and nothing can be excused by being added to it.
# ---------------------------------------------------------------------------

#: The members that make a class a console stub rather than some other test double.
_CONSOLE_MEMBERS = frozenset({"say", "check", "step", "banner", "summary", "elapsed", "text"})


def _console_stub_bodies() -> dict[str, list[str]]:
    """Every console-shaped stub in tests/, keyed by its STRUCTURE with docstrings stripped.

    Structure rather than text, via `ast.unparse`, because two copies that differ only in
    whitespace, a comment or a docstring are the same copy -- and because the three this
    gate was written for differed in exactly that much: two were named `_Recorder` and the
    third `_PricingRecorder`, whose docstring read "Same shape as the other recorders here".

    BASE-LESS ONLY. A class WITH a base is either a real class or a subclass of one, and
    step_console.StepNarrator's docstring argues at length that a console stub must never
    subclass Console; a stub that does is a different defect and is caught by
    test_the_shared_recorder_is_NOT_a_Console_and_never_becomes_one above.

    RECURSIVELY, though tests/ is flat today. Measured 2026-10-10: no subdirectories at
    all under tests/, and every .py in it matches `tests/*.py`. A non-recursive glob would
    therefore pass every test written today and silently stop covering the first file
    anybody puts in a subdirectory -- a hole that opens without anything failing, which is
    the shape this whole file is about.
    """
    bodies: dict[str, list[str]] = {}
    for path in sorted(pathlib.Path(__file__).parent.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.ClassDef) or node.bases:
                continue
            if not ({n.name for n in node.body if isinstance(n, ast.FunctionDef)} & _CONSOLE_MEMBERS):
                continue
            body = [n for n in node.body
                    if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)
                            and isinstance(n.value.value, str))]
            key = ast.unparse(ast.fix_missing_locations(
                ast.ClassDef(name="_", bases=[], keywords=[], body=body,
                             decorator_list=[], type_params=[])))
            bodies.setdefault(key, []).append(f"{path.name}:{node.lineno} {node.name}")
    return bodies


def test_the_suite_HAS_console_stubs_so_the_gate_below_is_not_vacuous():
    """The gate asserts an absence, and an absence is what a broken scanner also reports.

    Measured 2026-10-10: 17 console-shaped stub definitions across tests/, 17 distinct
    bodies. If `_console_stub_bodies()` stopped finding classes -- a changed member list,
    an ast API change, a wrong glob -- the gate would pass loudest at the moment it had
    stopped checking anything. The floor is deliberately well under 17 so ordinary work
    does not trip it; it only catches the scanner returning nothing much.
    """
    found = _console_stub_bodies()
    assert sum(len(v) for v in found.values()) >= 10, (
        f"the scanner found only {sum(len(v) for v in found.values())} console stubs in tests/, "
        f"where 17 were measured when this was written -- the gate below is asserting an "
        f"absence it can no longer see"
    )


def test_no_two_console_stubs_in_tests_SHARE_A_BODY():
    """Zero, with no allowlist. The duplication this closes cost four separate diagnoses.

    Measured 2026-10-10, before and after, by unparsing every base-less class in tests/:

        before   22 console-stub definitions, 19 distinct bodies, 2 bodies at >1 site
                   3x  _Recorder / _Recorder / _PricingRecorder   (three files)
                   2x  _QuietRun / _QuietRun                      (one file, 11 lines apart)
        after    17 definitions, 17 distinct bodies, 0 bodies at >1 site

    WHAT THIS GATE DOES NOT COVER, said rather than left to be discovered. It is console
    stubs only. The same scan over EVERY base-less class in tests/ reads 178 definitions,
    163 distinct bodies and 8 bodies still at more than one site -- LockedWallet x2,
    _Throttled/Response, NeverExits/Alive, StubResponse x2, Payer/Signs/CanSign x4,
    CannotSign x2, _Key x7 and Holder x2. Those are named work (OPEN_FINDINGS), not a
    baseline: widening this gate to all classes means clearing them first, because a gate
    that fails on the day it is written is a gate somebody deletes, and a gate that
    tolerates a list of exceptions is the ratchet rule 19 forbids.
    """
    shared = {body: sites for body, sites in _console_stub_bodies().items() if len(sites) > 1}
    assert not shared, (
        "two or more console stubs in tests/ have the same body:\n"
        + "\n".join(f"  {len(sites)}x  " + " | ".join(sites) for sites in shared.values())
        + "\n\nPut the shared one in tests/conftest.py beside TranscriptConsole, or make the "
          "difference real and name the other site in a comment at BOTH (rule 8). Three "
          "copies of one recorder is how one unchecked `spec.loader` came to be diagnosed "
          "four separate times in this repository."
    )
