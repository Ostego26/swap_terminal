"""An env var set to nothing is not an env var nobody set, and a report must say which.

Role: behavioral test (read-only)
Reads: config.env_variable_state, workers.common.db_path_source, and os.environ
       through monkeypatch
Writes: nothing
Can send orders: no
Live-safe: yes. No socket, no database, no subprocess, and every environment
       change is monkeypatch-scoped.

WHY THIS FILE EXISTS. workers/common.db_path_source() was created on 2026-10-01
because a report fabricated a provenance: it printed the correct database path and
attributed it to SWAP_DB_PATH in a shell that did not have SWAP_DB_PATH. The fix
gave it a third answer. On 2026-10-10 the same function was measured against all
FOUR shell states and the same defect was still in it, one case further in:

    SWAP_DB_PATH in the shell       path reported   it said
    unset entirely                  the default     "IS NOT SET in this shell"   true
    set to /data/real.db            /data/real.db   "SWAP_DB_PATH"               true
    SET BUT EMPTY (SWAP_DB_PATH=)   the default     "IS NOT SET in this shell"   FALSE
    set to whitespace               the default     "IS NOT SET in this shell"   FALSE

The path is right in all four. The provenance is a fabrication in two: the variable
IS set, and the sentence says it is not. An operator who checks that against their
own `env` finds it disagreeing and cannot tell which half is wrong -- which is the
exact sentence the function's own docstring opens with.

AND THE FUNCTION HAD NO DIRECT TESTS. The only mention of it in tests/ was inside a
docstring in test_show_fees.py, which exercised three of the states through one
caller's header_lines(). Nothing called it. The branch that was wrong was the branch
nothing asked about, which is the ordinary way this happens.

`os.getenv` is why: it returns "" both for a variable nobody set and for
`export FOO=`, so the two states are indistinguishable unless something looks. That
is the whole subject of the comment block above config._env(), which exists because
a single empty variable used to take down every entry point in this project on
`int("")`.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import config
import pytest
from config import (
    ENV_SET,
    ENV_SET_BUT_EMPTY,
    ENV_UNSET,
    ENV_VARIABLE_STATES,
    env_variable_state,
)
from workers.common import DB_PATH_VARIABLE, db_path_source

#: A name nothing in this project reads, so the leaf tests cannot be perturbed by
#: a real setting and cannot perturb one.
_PROBE = "SWAP_TERMINAL_TEST_PROBE_VARIABLE"


# ---------------------------------------------------------------------------
# THE LEAF DECISION. Three states, and the middle one is the point.
# ---------------------------------------------------------------------------

def test_an_UNSET_variable_reads_as_unset(monkeypatch):
    monkeypatch.delenv(_PROBE, raising=False)
    assert env_variable_state(_PROBE) == ENV_UNSET


def test_a_variable_with_a_REAL_VALUE_reads_as_set(monkeypatch):
    monkeypatch.setenv(_PROBE, "/data/real.db")
    assert env_variable_state(_PROBE) == ENV_SET


@pytest.mark.parametrize("empty", ["", " ", "   ", "\t", "\n", " \t\n "])
def test_a_SET_BUT_EMPTY_variable_is_its_OWN_state_and_not_unset(monkeypatch, empty):
    """The state os.getenv destroys, in every spelling an operator can produce.

    `export FOO=` is the common one; a trailing newline from a `read` or a heredoc
    is the one chains/xrp_payout_seed.py records; and `export FOO=" "` is the same
    mistake with a space in it, which is why _env() strips as well as testing for
    emptiness and why this does too.
    """
    monkeypatch.setenv(_PROBE, empty)
    assert env_variable_state(_PROBE) == ENV_SET_BUT_EMPTY
    assert env_variable_state(_PROBE) != ENV_UNSET, (
        "the variable IS set -- `env | grep` shows it, and a report that says "
        "otherwise disagrees with the operator's own shell"
    )


def test_the_THREE_STATES_ARE_DISTINCT_and_the_tuple_is_total(monkeypatch):
    """A caller can assert it covered every state, which is what the tuple is for.

    Asserted by producing all three from real os.environ states and checking the set
    of what came back is exactly ENV_VARIABLE_STATES -- not by reading the tuple,
    which would pass even if one state were unreachable.
    """
    seen = set()
    monkeypatch.delenv(_PROBE, raising=False)
    seen.add(env_variable_state(_PROBE))
    monkeypatch.setenv(_PROBE, "")
    seen.add(env_variable_state(_PROBE))
    monkeypatch.setenv(_PROBE, "something")
    seen.add(env_variable_state(_PROBE))
    assert seen == set(ENV_VARIABLE_STATES), (
        f"every state in ENV_VARIABLE_STATES must be reachable from a real environment "
        f"and all three distinct; produced {sorted(seen)}"
    )
    assert len(ENV_VARIABLE_STATES) == 3


def test_env_variable_state_READS_OS_ENVIRON_AT_CALL_TIME_not_import_time(monkeypatch):
    """config.py reads the environment at IMPORT time; this must not.

    config.py's own header says "Reads: the process environment at IMPORT time", and
    Config.DB_PATH is frozen there. A provenance function that froze with it would
    describe the shell that started the process rather than the shell now -- and
    db_path_source()'s whole job is to describe the shell the operator is looking at.
    """
    monkeypatch.setenv(_PROBE, "first")
    assert env_variable_state(_PROBE) == ENV_SET
    monkeypatch.delenv(_PROBE)
    assert env_variable_state(_PROBE) == ENV_UNSET, "it re-read, rather than caching"


def test_it_returns_a_STATE_and_never_the_VALUE(monkeypatch):
    """The value belongs to _env. Returning it here would make two ways to read one
    variable, and the fallback rule would then live in two places."""
    monkeypatch.setenv(_PROBE, "/data/secret-looking-path.db")
    assert env_variable_state(_PROBE) == ENV_SET
    assert "/data" not in env_variable_state(_PROBE)


# ---------------------------------------------------------------------------
# THE CALLER THIS WAS ADDED FOR. Four shell states, three sentences.
# ---------------------------------------------------------------------------

def test_the_FLAG_wins_over_every_environment_state(monkeypatch):
    """`--db` is observed, not inferred, which was the 2026-10-01 fix. Unchanged.

    Asserted under a SET variable as well as an unset one, because the two-case
    version's bug was exactly that `--db` pointed at the default value reported as
    the environment.
    """
    monkeypatch.setenv(DB_PATH_VARIABLE, "/data/from-the-environment.db")
    assert db_path_source("/data/from-the-flag.db") == "--db"
    monkeypatch.delenv(DB_PATH_VARIABLE, raising=False)
    assert db_path_source("/data/from-the-flag.db") == "--db"
    assert db_path_source(explicit_db="/data/from-the-flag.db") == "--db"


def test_a_REAL_environment_value_is_named_as_the_source(monkeypatch):
    monkeypatch.setenv(DB_PATH_VARIABLE, "/data/real.db")
    assert db_path_source() == DB_PATH_VARIABLE


def test_an_UNSET_variable_says_NOT_SET_and_names_the_default(monkeypatch):
    monkeypatch.delenv(DB_PATH_VARIABLE, raising=False)
    said = db_path_source()
    assert "the built-in default" in said
    assert "IS NOT SET in this shell" in said
    assert "SET BUT EMPTY" not in said


@pytest.mark.parametrize("empty", ["", "   ", "\n"])
def test_a_SET_BUT_EMPTY_variable_does_NOT_claim_the_variable_is_unset(monkeypatch, empty):
    """THE DEFECT. This is the assertion that was missing, and it is the whole file.

    The old sentence said "SWAP_DB_PATH IS NOT SET in this shell" for a shell where
    `env | grep SWAP_DB_PATH` prints the variable. The remedy it implies -- export
    it -- is also wrong, because the operator already did: what they need to know is
    that the export carried no value and the default applied anyway.
    """
    monkeypatch.setenv(DB_PATH_VARIABLE, empty)
    said = db_path_source()
    assert "IS NOT SET in this shell" not in said, (
        "the variable IS set. Saying otherwise is the fabricated provenance this "
        "function was created to stop, one case further in"
    )
    assert "IS SET BUT EMPTY in this shell" in said
    assert "the built-in default" in said, "and the path really is the default, so that stays"
    assert f"`export {DB_PATH_VARIABLE}=`" in said, (
        "it names the exact thing the operator typed, so they can see it did nothing"
    )


def test_the_THREE_SENTENCES_ARE_ALL_DIFFERENT_across_the_four_shell_states(monkeypatch):
    """Four states, three distinct answers, and the count is the assertion.

    Rule 14: "did nothing" must not look like "did work" -- here, two different
    shell mistakes must not read as one. A change that collapsed any two of these
    would pass every test above that looks at one state in isolation.
    """
    answers = {}
    monkeypatch.delenv(DB_PATH_VARIABLE, raising=False)
    answers["unset"] = db_path_source()
    monkeypatch.setenv(DB_PATH_VARIABLE, "/data/real.db")
    answers["set"] = db_path_source()
    monkeypatch.setenv(DB_PATH_VARIABLE, "")
    answers["set-but-empty"] = db_path_source()
    monkeypatch.setenv(DB_PATH_VARIABLE, "  ")
    answers["whitespace"] = db_path_source()

    assert answers["set-but-empty"] == answers["whitespace"], (
        "`export FOO=` and `export FOO=' '` are the same mistake and get the same sentence"
    )
    assert len(set(answers.values())) == 3, (
        f"four shell states must produce exactly three distinct sentences, got "
        f"{len(set(answers.values()))}: {answers}"
    )
    assert answers["unset"] != answers["set-but-empty"], (
        "the two that the OLD function collapsed. This is the regression guard"
    )


def test_the_default_sentences_BOTH_warn_that_the_WORKERS_may_read_another_file(monkeypatch):
    """The half that is the same for both default cases, so a refactor cannot drop it.

    The workers read whatever SWAP_DB_PATH named in the shell that STARTED them. A
    report describing the reader's shell has to say so, or the operator concludes
    the running workers are on the file they are looking at.
    """
    for value in (None, ""):
        monkeypatch.delenv(DB_PATH_VARIABLE, raising=False)
        if value is not None:
            monkeypatch.setenv(DB_PATH_VARIABLE, value)
        said = db_path_source()
        assert "the shell that STARTED them" in said
        assert "may be a different file" in said


# ---------------------------------------------------------------------------
# THE SIGNATURE. The unread parameter is gone and must not come back.
# ---------------------------------------------------------------------------

def test_db_path_source_TAKES_NO_PATH_because_it_never_read_one():
    """`db_path` was the first parameter and the body never loaded it.

    Proven by ast.walk before removal: `loaded: ['explicit_db']`,
    `NEVER READ: ['db_path']`. Ruff cannot find this -- ARG is not in pyproject's
    selected set -- so a test is the only thing that holds it.

    IT MATTERED BEYOND TIDINESS. A reader seeing `db_path_source(db_path,
    explicit_db)` reasonably concludes the path participates, which is precisely the
    inference the two-case version made and this docstring records as the bug. And
    nothing stopped a caller passing a path the function never looks at and getting
    a confident provenance claim about it.
    """
    params = list(inspect.signature(db_path_source).parameters)
    assert params == ["explicit_db"], (
        f"expected the flag's value alone, got {params}. A path parameter here invites a "
        f"reader to believe the path is compared -- it is not, and it never was"
    )


def test_EVERY_CALLER_IN_THE_TREE_passes_at_most_the_flag():
    """Nine call sites, checked by AST rather than by remembering to update them.

    Counted over the real tree because rule 2's guard is that an import graph does
    not see every reference -- and here the thing that would break is a positional
    argument in a file nobody re-read.
    """
    root = Path(__file__).resolve().parent.parent
    skip = {".git", "__pycache__", ".ruff_cache", ".pytest_cache", "node_modules", "venv"}
    offenders, found = [], 0
    for path in sorted(root.rglob("*.py")):
        if skip & set(path.parts):
            continue
        try:
            tree = ast.parse(path.read_text(), filename=str(path))
        except SyntaxError:
            # Reported, not skipped: "I could not look" is not "there is nothing
            # there" (rule 2). A file mid-edit shows up here by name.
            offenders.append(f"{path.relative_to(root)} (UNPARSEABLE -- not searched)")
            continue
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "db_path_source"):
                continue
            found += 1
            if len(node.args) > 1 or any(k.arg != "explicit_db" for k in node.keywords):
                offenders.append(f"{path.relative_to(root)}:{node.lineno}")
    assert not offenders, f"call sites passing more than the flag: {offenders}"
    assert found >= 9, (
        f"expected at least the 9 known call sites, found {found} -- if this dropped, a "
        f"caller stopped reporting its provenance rather than being fixed"
    )


def test_config_is_the_ONE_place_the_state_test_lives():
    """It must not be re-inlined here. Four spellings already exist; this is not a fifth.

    `db_path_source` calls config.env_variable_state(). A future edit that replaced
    that with its own `os.environ.get(...).strip()` would restore the duplication
    rule 19 calls the defect, and would do it invisibly because the behavior would
    be identical on the day it was written.
    """
    body = inspect.getsource(db_path_source)
    _doc, _, code = body.partition('"""')
    code = code.partition('"""')[2]
    assert "env_variable_state(" in code, "the shared decision is called, not copied"
    assert "os.environ" not in code and "os.getenv" not in code, (
        "the state test belongs in config.env_variable_state(), which names all four "
        "sites that already spell it -- including gunicorn.conf.py, which spells it wrong"
    )
    assert callable(config.env_variable_state)
