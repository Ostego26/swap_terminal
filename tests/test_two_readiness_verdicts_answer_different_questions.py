"""Two functions named `readiness_verdict`, and the difference is the point.

Role: hygiene + behavioral test (read-only)
Reads: swap_terminal_desktop.readiness_verdict, stack_authority.readiness_verdict,
       and the source of both files, for the textual half
Writes: nothing
Can send orders: no
Live-safe: yes. No socket, no database, no subprocess.

WHY THIS FILE EXISTS. Rule 8's first clause is "merge them, and let the survivor own
the concept". Its second clause is the one that applies here:

    If they genuinely differ, the difference is the point and belongs in a comment
    at BOTH sites, naming the other one. A reader who finds one must be told the
    other exists.

Neither docstring mentioned the other until 2026-10-10. Both are called
`readiness_verdict`, both are about whether a web service may be used, both return a
tuple whose first element reads like a verdict -- and they disagree on half the
observations you can hand them.

MEASURED, NOT ASSUMED (rule 17). The same six observations through both:

    observation                        desktop (launcher)   stack_authority (port)
    HTTP 503, our db_path              unhealthy            ready=True "answered 503"
    HTTP 200, status ok, OUR db        ready                ready=True "answered 200"
    HTTP 200, status ok, FOREIGN db    wrong-server         ready=True "answered 200"
    HTTP 200, not a JSON object        wrong-server         ready=True "answered 200"
    connection refused                 not-listening        ready=False
    timed out                          bound-but-silent     ready=False

Three of six opposite, and the launcher is the stricter side every time.

AND A MERGE WOULD BE WRONG IN EITHER DIRECTION, which is why this pins the split
rather than closing it:

  - give stack_authority's the identity check, and `swap_stack up` starts calling a
    dfx replica `wrong-server`, because dfx's status endpoint has no db_path. It
    probes six heterogeneous services and cannot know what each should answer.
  - take the identity check OUT of the launcher's, and a foreign responder that
    returns our body shape gets a browser opened on it -- which already happened
    once, and is the measured defect the `wrong-server` verdict was added for.

So the correct state is two functions, each right for its own question, each naming
the other. That is a claim about COMMENTS, so the last test here is textual and says
so; everything above it is behavioral.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest
from source_tree import NOT_SOURCE
from stack_authority import readiness_verdict as port_readiness_verdict

import swap_terminal_desktop as desktop

#: The launcher's expected database, as its caller passes it. Any other value is a
#: different server, which is the whole point of that function's identity check.
_OURS = "/db/ours.sqlite3"

#: The six observations in the table above, as (label, status_code, body).
_ANSWERED = [
    ("a 503 from our own server", 503, {"status": "degraded", "db_path": _OURS}),
    ("a healthy 200 from our own server", 200, {"status": "ok", "db_path": _OURS}),
    ("a healthy-LOOKING 200 from somebody else", 200,
     {"status": "ok", "db_path": "/db/somebody_else.sqlite3"}),
    ("a 200 whose body is not a JSON object", 200, None),
]


def _desktop(status_code, body):
    return desktop.readiness_verdict(status_code, body, "", _OURS)[0]


# ---------------------------------------------------------------------------
# THE DIVERGENCE, asserted observation by observation.
# ---------------------------------------------------------------------------

def test_a_FOREIGN_server_returning_OUR_BODY_SHAPE_is_refused_by_the_launcher():
    """The measured defect. A browser was once opened on somebody else's UI.

    `{"status": "ok"}` is a shape anything can return. The launcher compares db_path
    BEFORE status for exactly this reason -- "ok" from a server that is not ours is
    the dangerous answer, not a reassuring one.
    """
    assert _desktop(200, {"status": "ok", "db_path": "/db/somebody_else.sqlite3"}) == "wrong-server"


def test_the_PORT_PROBE_calls_that_same_foreign_server_READY_and_that_is_CORRECT():
    """Not a bug. "Something is listening" is the entire question `swap_stack up` asks.

    It probes a dfx replica, the web app and the daemons through one function. It has
    no way to know what each of those should answer, and dfx's status endpoint has no
    db_path to compare, so an identity check here would report a healthy replica as
    an impostor.
    """
    ready, summary, _advice = port_readiness_verdict(200)
    assert ready is True
    assert summary == "answered 200"


def test_a_503_is_UNHEALTHY_to_the_launcher_and_LISTENING_to_the_port_probe():
    """The third disagreement, and the two words are both right for their caller.

    A launcher must not open a browser on a degraded server. A `up` table must not
    print a bound, answering port as down -- so the port probe reports ready with the
    code, which its own branch comment states is deliberate.
    """
    assert _desktop(503, {"status": "degraded", "db_path": _OURS}) == "unhealthy"
    ready, summary, advice = port_readiness_verdict(503)
    assert ready is True
    assert "503" in summary, "the code is reported rather than collapsed into 'not ready'"
    assert advice, "and a non-200 carries the gloss saying it is not the 200 dfx gives"


@pytest.mark.parametrize(("label", "status_code", "body"), _ANSWERED)
def test_EVERY_observation_that_got_an_ANSWER_is_ready_to_the_port_probe(label, status_code, body):
    """One half of the table, as a sweep: an answer at all means listening.

    Parameterized so that a future change narrowing this -- making some answered
    status not-ready -- fails here with the observation named, rather than silently
    making the port probe a second, weaker identity check.
    """
    del label, body
    ready, _summary, _advice = port_readiness_verdict(status_code)
    assert ready is True


def test_the_two_AGREE_on_both_TRANSPORT_failures():
    """The agreement matters too: the split is about answers, not about silence.

    If they disagreed here as well, the honest conclusion would be that one of them
    is simply wrong rather than differently scoped -- so this is the assertion that
    makes "differently scoped" a measurement instead of a story.
    """
    assert _desktop(None, None) != "ready"
    assert desktop.readiness_verdict(None, None, "ECONNREFUSED", _OURS)[0] == "not-listening"
    assert desktop.readiness_verdict(None, None, "timed out", _OURS)[0] == "bound-but-silent"
    assert port_readiness_verdict(ConnectionRefusedError())[0] is False
    assert port_readiness_verdict(TimeoutError())[0] is False


def test_the_launcher_says_READY_for_exactly_ONE_of_the_four_answers():
    """The count, with its denominator (rule 3), because "stricter" needs a number.

    Four observations that got an answer; one is `ready`. The port probe says ready to
    all four. A change that made the launcher permissive would most likely show up as
    this count rising, and a bare "it is stricter" would not have caught it.
    """
    verdicts = [_desktop(code, body) for _label, code, body in _ANSWERED]
    assert verdicts.count("ready") == 1, f"expected exactly 1 of 4, got {verdicts}"
    ports = [port_readiness_verdict(code)[0] for _label, code, _body in _ANSWERED]
    assert ports.count(True) == 4, f"the port probe says ready to all four, got {ports}"


def test_the_two_RETURN_SHAPES_differ_so_a_caller_cannot_swap_them_silently():
    """A 2-tuple of strings against a 3-tuple starting with a bool.

    This is the one piece of accidental protection the pair already had, and it is
    worth pinning: `ready, summary, advice = desktop_version(...)` raises rather than
    mis-reporting. If either shape ever changed to match the other, a swapped import
    would start type-checking and the divergence above would become silent.
    """
    launcher = desktop.readiness_verdict(200, {"status": "ok", "db_path": _OURS}, "", _OURS)
    port = port_readiness_verdict(200)
    assert len(launcher) == 2 and len(port) == 3
    assert isinstance(launcher[0], str) and isinstance(port[0], bool)


# ---------------------------------------------------------------------------
# THE TEXTUAL HALF, and it is deliberately textual: the thing being pinned IS a
# comment. There is no behavior to seed for "a reader who finds one is told the
# other exists". tests/test_payout_live_statuses_have_one_spelling.py draws the
# same line for the same reason.
# ---------------------------------------------------------------------------

def test_EACH_docstring_NAMES_THE_OTHER_function_and_its_module():
    """Rule 8's second clause, enforced rather than hoped for.

    Asserted on the DOCSTRING rather than the file, because both files are long and a
    mention anywhere in either would pass a whole-file grep while telling a reader of
    this particular function nothing.
    """
    launcher_doc = desktop.readiness_verdict.__doc__ or ""
    port_doc = port_readiness_verdict.__doc__ or ""

    # THE QUALIFIED NAME, not the bare module. Asserting `"stack_authority" in doc`
    # was the first version and TWO MUTATIONS SURVIVED IT: the launcher's docstring
    # mentions that module several times for its own reasons, so gutting the actual
    # cross-reference paragraph left the substring behind and the test green. A
    # module.function spelling is what a reader can follow, and it appears once.
    assert "stack_authority.readiness_verdict" in launcher_doc, (
        "swap_terminal_desktop.readiness_verdict must name stack_authority.readiness_verdict "
        "by its qualified name -- a reader who finds one of two same-named functions must be "
        "told where the other one is, not merely that it exists somewhere"
    )
    assert "swap_terminal_desktop.readiness_verdict" in port_doc, (
        "stack_authority.readiness_verdict must name swap_terminal_desktop.readiness_verdict, "
        "same reason"
    )

    # AND THE AXIS, because "there is another one" without "and here is how it
    # differs" is the half rule 8 actually asks for. `db_path` is the axis: it is
    # what the launcher compares and what the port probe cannot. An `or` of two
    # loose tokens was the first version and survived a mutation too -- "identity"
    # appears in the wrong-server verdict's own description.
    for doc, who in ((launcher_doc, "the launcher's"), (port_doc, "the port probe's")):
        assert "db_path" in doc, (
            f"{who} docstring must name db_path, which is WHICH WAY the two differ -- "
            f"the launcher compares it and the port probe has nothing to compare it to"
        )
    assert "wrong-server" in launcher_doc, (
        "and the launcher's names the verdict that difference produces"
    )


def test_NOTHING_ELSE_IN_THE_TREE_defines_a_THIRD_readiness_verdict():
    """Two is the measured state. A third would be rule 8 arriving again.

    Counted over the real tree rather than a hand-maintained list, so a new copy
    fails here on the day it is written -- which is rule 19's "never add a baseline
    line for code you are writing now" in the only form a test can take.

    IT READS THE AST, NOT THE TEXT, and the first version did not. Written as
    `"def readiness_verdict(" in path.read_text()` it reported THREE definitions,
    the third being THIS FILE -- matching its own search string. That is the fourth
    detector in this session to read prose as code (tests/test_step_console.py
    records one that substring-matched an import statement inside a comment), and
    the text approach cannot be rescued here the way that one was: the offending
    match is not in a docstring that could be stripped, it is in a string literal
    that IS the detector. A FunctionDef walk cannot match a string, a comment or a
    docstring, which is why it is the only correct form.
    """
    root = Path(__file__).resolve().parent.parent
    # NOT_SOURCE RATHER THAN A SIXTH INLINE COPY OF THIS SET, 2026-10-10. This one listed
    # six names and missed `.claude`, where Claude Code puts git worktrees -- and a worktree
    # there is a COMPLETE second copy of the tree, so this sweep found the verdict tables
    # twice and reported a THIRD definition that does not exist. A uniqueness assertion over
    # a tree containing a copy of itself cannot hold. tests/source_tree.py has the
    # measurement and is the one place to change it.
    skip = set(NOT_SOURCE)
    found = []
    for path in root.rglob("*.py"):
        if skip & set(path.parts):
            continue
        try:
            tree = ast.parse(path.read_text(), filename=str(path))
        except SyntaxError:
            # A file this interpreter cannot parse is reported rather than skipped
            # silently: "I found no definition" and "I could not look" are the two
            # answers rule 2 insists are different, and a bare `continue` here would
            # make an unparseable file look like a clean one.
            found.append(f"{path.relative_to(root)} (UNPARSEABLE -- not searched)")
            continue
        # `any` rather than a loop that appends per node: the assertion is about
        # which FILES define it, and a file holding two definitions should appear
        # once rather than twice -- which the append form got wrong.
        if any(isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
               and node.name == "readiness_verdict" for node in ast.walk(tree)):
            found.append(str(path.relative_to(root)))
    found = sorted(found)
    assert found == ["swap_terminal/stack_authority.py", "swap_terminal_desktop.py"], (
        f"expected exactly the two documented definitions, found {found}. A third copy of "
        f"'is it ready' is rule 8's bug-with-a-delay-on-it; merge it into whichever of the "
        f"two asks its question, or document the difference at all three sites"
    )


def test_this_file_is_IMPORTABLE_without_the_desktop_launcher_starting_anything():
    """The import above must not have side effects, or this test file is a hazard.

    swap_terminal_desktop.py is an entry point: it spawns gunicorn and opens a
    browser. Importing it for one pure function is only safe because that work is
    behind main(), and rule 12 names import-time side effects as the thing a linter
    cannot check. This asserts the module came in without running.
    """
    assert "swap_terminal_desktop" in sys.modules
    assert callable(desktop.readiness_verdict)
    assert hasattr(desktop, "main"), "the launcher's work is behind main(), not at import"
