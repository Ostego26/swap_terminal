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
import io
import pathlib

import pytest
from conftest import TranscriptConsole

# BOTH Consoles, ALIASED, in the one file whose subject is that they are different.
# `Console` is step_console's -- the one under test -- and the other is deliberately
# NOT called Console here: two classes of that name in one module is the confusion
# this file exists to pin, and reproducing it in the test would make the test's own
# assertions ambiguous about which class they are about.
from regtest.console import FAIL, OK, SKIP, XFAIL
from regtest.console import Console as RegtestConsole
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


# ---------------------------------------------------------------------------
# THE MIRROR REFUSAL, 2026-10-10. C16 hardened step_console against a verdict
# STRING arriving where a bool belongs. The other direction -- a BOOL arriving
# where a verdict string belongs -- was left open, and this file's own opening
# paragraph is what predicted it: the two populations are "one copy-paste apart".
#
# It finally happened. testnet_wallets.py, written the same day, imported
# `from regtest.console import FAIL, OK, Console` and then wrote a step_console-style
# bool into check(). Its own test caught it, which is why this is a guard rather
# than an incident.
# ---------------------------------------------------------------------------


def test_a_BOOL_is_refused_by_the_OTHER_Console_too():
    """Symmetry. Both crossings now raise, and before this one silently printed `0`.

    MEASURED before the guard existed, which is why the numbers are in
    regtest/console.py's docstring rather than paraphrased here: counts[FAIL] stayed
    0, `failures` stayed empty, a phantom `False` key was added to the tally, and the
    line read `0` where a verdict belongs. A failed check invisible to summary() and
    to any exit code read off counts[FAIL].
    """
    console = RegtestConsole(1, stream=io.StringIO())
    for crossed in (True, False, 1, 0, None, "PASS"):
        with pytest.raises(TypeError) as raised:
            console.check("crossed", "got", "want", crossed)
        assert "not one of" in str(raised.value)
        assert "step_console" in str(raised.value), "and it names where the value came from"
    assert console.counts == {OK: 0, FAIL: 0, XFAIL: 0, SKIP: 0}, (
        "no refused call may leave a phantom key in the tally -- that was half the defect"
    )


@pytest.mark.parametrize("verdict", ["OK", "FAIL", "SKIP", "XFAIL"])
def test_the_four_REAL_verdicts_still_pass_and_still_tally(verdict):
    """The guard must not have cost the 61 call sites that were already correct.

    Measured 2026-10-10: 8 files import regtest.console and pass 61 verdict strings
    and zero bools, so this guard fixes NO existing call. That makes "it changed
    nothing for them" the property to assert -- a guard that broke the correct
    population would be a worse defect than the one it closes.
    """
    console = RegtestConsole(1, stream=io.StringIO())
    assert console.check("fine", 1, 1, verdict) == verdict, "it returns the outcome, as before"
    assert console.counts[verdict] == 1


def test_a_FAILED_check_is_named_in_failures_which_is_what_a_bool_lost():
    """The consequence, end to end: the exit code and the named failure both depend on it.

    summary() reads counts and failures. A bool reached neither, so a run with a
    failed check reported FAIL=0 and "unexpected failures: (none)" -- which is rule
    13's "treat skipped plus success in the same output as a defect in the output",
    one layer down in the thing that produces the output.
    """
    stream = io.StringIO()
    console = RegtestConsole(1, stream=stream)
    console.check("a real failure", 1, 2, FAIL)
    assert console.counts[FAIL] == 1
    assert console.failures and "a real failure" in console.failures[0]


# ---------------------------------------------------------------------------
# THE CONFORMANCE GATE, 2026-10-10, after the SECOND crossing in one file.
#
# The two classes named Console differ in THREE methods, not one:
#
#     step_console          regtest.console
#     check(..., ok: bool)  check(..., outcome: str)      -> different TYPE
#     step(number, title)   step(number, chain, title)    -> different ARITY
#     summary() -> int      summary() -> None             -> different RETURN
#
# C16 closed the first for one direction. C57 closed it for the other. Then I
# wrote the SECOND crossing into testnet_wallets.py and it reached the operator's
# terminal as `TypeError: Console.step() missing 1 required positional argument`,
# because every test in that file called a decision function directly and nothing
# ran main(). Fixing the line would leave the third trap armed -- and the third is
# the worst of them: `return console.summary()` against regtest.console returns
# None, so SystemExit(None) exits 0 and a failing run reports success. That is
# C16's incident a third time.
#
# So this checks CALLS against the class the file actually imports, for both
# populations, rather than closing one method at a time.
# ---------------------------------------------------------------------------

_CONSOLE_CLASSES = {"step_console": Console, "regtest.console": RegtestConsole}


def _console_module_of(tree: ast.Module) -> str | None:
    """Which Console a file IMPORTS, read from its import statements. None if neither.

    THE IMPORT STATEMENTS, NOT THE TEXT, AND THE FIRST VERSION GOT THAT WRONG -- one
    minute after I fixed the identical defect in test_daemon_conf.py's two gates. It
    substring-matched "from step_console import" anywhere in the file, so
    testnet_wallets.py -- which NAMES step_console in a comment explaining that its
    step() has a different arity -- was classified as a step_console file and the gate
    reported its correct 3-argument call as wrong.

    That is the third time today a check of mine matched prose adjacent to its claim
    rather than the thing claimed (C44 was the first, test_daemon_conf's gates the
    second), and the second time I wrote the defect into the fix for it. An AST read of
    the import nodes cannot be fooled by a comment, a docstring, or a string literal.

    STILL NOT AN IMPORT OF THE FILE ITSELF: this runs over every .py in the tree and
    importing them all would execute module-level code in forty of them.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module == "step_console":
                return "step_console"
            if node.module == "regtest.console":
                return "regtest.console"
            if node.module == "regtest" and any(a.name == "console" for a in node.names):
                return "regtest.console"
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "step_console":
                    return "step_console"
                if alias.name in ("regtest.console", "swap_terminal.regtest.console"):
                    return "regtest.console"
    return None


def _console_calls(tree: ast.Module) -> list[tuple[int, str, int, tuple[str, ...]]]:
    """Every `console.<method>(...)` call in a file, as (line, method, positionals, keywords)."""
    calls = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "console"):
            continue
        keywords = tuple(k.arg for k in node.keywords if k.arg)
        calls.append((node.lineno, node.func.attr, len(node.args), keywords))
    return calls


def test_every_console_call_MATCHES_THE_CLASS_ITS_FILE_IMPORTS():
    """The gate. Arity and keyword names, against the real signature, tree-wide.

    WHAT IT WOULD HAVE CAUGHT, and did not exist to: `console.step(number, title)`
    in a file importing regtest.console, whose step() takes (number, chain, title).
    It reached the operator as a TypeError on the first line of real work.

    IT DOES NOT CHECK TYPES, only shapes, and that boundary is deliberate: check()'s
    bool-versus-string crossing is a TYPE error, it is not visible in an AST, and it
    is already refused at RUNTIME by both classes (C16 and C57). Arity and keyword
    names are what a static read can establish, so that is what this claims.

    FILES THAT IMPORT NEITHER ARE SKIPPED rather than guessed at. A `console`
    parameter in a file importing no Console is a stub or a protocol, and asserting
    against a class it never names would be this test inventing a fact.
    """
    root = pathlib.Path(__file__).resolve().parent.parent
    wrong, checked, files = [], 0, 0
    for path in sorted(root.rglob("*.py")):
        if ".git" in path.parts or "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        module = _console_module_of(tree)
        if module is None:
            continue
        cls = _CONSOLE_CLASSES[module]
        files += 1
        for line, method, positional, keywords in _console_calls(tree):
            member = getattr(cls, method, None)
            if member is None or not callable(member):
                wrong.append(f"{path.relative_to(root)}:{line} console.{method}() "
                             f"does not exist on {module}.Console")
                continue
            try:
                inspect.signature(member).bind(None, *range(positional),
                                               **dict.fromkeys(keywords))
            except TypeError as error:
                wrong.append(
                    f"{path.relative_to(root)}:{line} console.{method}("
                    f"{positional} positional{', ' + ', '.join(keywords) if keywords else ''}) "
                    f"does not fit {module}.Console.{method}{inspect.signature(member)} -- {error}")
            checked += 1
    assert files >= 10, (
        f"only {files} files import a Console; 14 did when this was written, so the scan has "
        f"stopped finding them and this gate is asserting an absence it can no longer see"
    )
    assert checked >= 100, f"only {checked} console calls were checked; 200+ existed when written"
    assert not wrong, (
        f"{len(wrong)} console call(s) do not fit the Console their own file imports. The two "
        f"classes named Console differ in check()'s verdict TYPE, step()'s ARITY and summary()'s "
        f"RETURN, and they are one copy-paste apart:\n  " + "\n  ".join(wrong))


def test_NOTHING_returns_regtest_consoles_summary_as_an_exit_code():
    """The third trap, and the only one of the three that is silent.

    step_console.summary() returns 1 if anything failed, so `return console.summary()`
    is an exit code. regtest.console.summary() returns None, so the same line gives
    SystemExit(None) -- which exits 0. A run with failures reporting success, which is
    C16's incident in a third costume.

    Checked as a SHAPE rather than left to the arity gate above, because both
    summary() methods take no arguments and bind identically: nothing about the call
    is wrong, only what is done with the answer.
    """
    root = pathlib.Path(__file__).resolve().parent.parent
    offenders = []
    for path in sorted(root.rglob("*.py")):
        if ".git" in path.parts or "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if _console_module_of(tree) != "regtest.console":
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Return) or node.value is None:
                continue
            if (isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Attribute)
                    and node.value.func.attr == "summary"
                    and isinstance(node.value.func.value, ast.Name)
                    and node.value.func.value.id == "console"):
                offenders.append(f"{path.relative_to(root)}:{node.lineno}")
    assert not offenders, (
        "these return regtest.console.Console.summary() as a value, and it returns None -- so a "
        "`raise SystemExit(main())` built on it EXITS 0 on a run that failed. step_console's "
        "summary() returns an int and is the one that may be returned:\n  " + "\n  ".join(offenders))
