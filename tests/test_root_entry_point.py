"""The one copy of the entry-point loader, and the six decisions a mutation can break.

Role: tests (read-only against the real tree, plus a fake root pytest built)
Reads: conftest.root_entry_point, suite_baseline.py, operator_panel.py, and files
        under tmp_path
Writes: files under tmp_path only
Can move funds: no. It imports modules by path. No socket, no adapter, no wallet.
Mainnet-safe: yes
Live-safe: yes

WHY THIS FILE EXISTS, and it is the shape this repository keeps paying for. Five test
files each carried a hand-written `spec_from_file_location` loader; rule 8 says merge
them and let the survivor own the concept, which `conftest.root_entry_point()` now does
for NINE call sites across seven files (counted 2026-10-10, not estimated:
`grep -c 'root_entry_point("'` over the seven). The survivor had no tests. That is the merge
leaving its own product as the single untested thing every caller depends on -- so a
mutation in it reports as thirty AttributeErrors in files that are about something else
entirely, which is exactly how the unchecked-`spec.loader` hole survived in all five
copies long enough to be diagnosed four separate times.

WHAT IS NOT TESTED HERE, said rather than left as a gap (rule 17). The helper's third
guard, `spec.loader is None`, is UNREACHED by every real path I could construct.
Measured 2026-10-10 on four of them:

    tests/              (a directory)     -> spec is None
    README.md           (exists, no loader claims it)
                                          -> spec is None
    nope.py             (does not exist)  -> ModuleSpec(loader=<SourceFileLoader>)
    docker/             (a directory)     -> spec is None

`ModuleSpec.loader` is declared Optional and the typecheckers that found the hole in all
five copies were right that it must be handled, but nothing in this tree produces one.
So that branch is defensive, is not covered, and no test here pretends otherwise -- a
test asserting the SOURCE contains the check would be "the SQL text contains X", which
BEHAVIORAL_VERIFICATION_PRINCIPLE forbids outright.

The third line of that table is also the one fact that made the merge better than any of
the five copies: a path that DOES NOT EXIST still produces a perfectly good spec, so
`spec is None` never fires for the realistic failure of a rule 10 entry point, which is
that somebody moved it.
"""

from __future__ import annotations

import sys

import conftest
import pytest
from conftest import root_entry_point


def test_a_moved_entry_point_names_THE_FILE_and_not_a_NoneType_attribute():
    """The guard all five hand-written copies lacked, and the only one that fires on a move.

    A rule 10 entry point is named by PATH, so no import graph points at it: renaming
    `operator_panel.py` breaks nothing at import time and reports here. What the five
    copies produced was `AttributeError: 'NoneType' object has no attribute 'loader'`
    -- or, once the None guards were added, a spec that sailed past both of them,
    because a nonexistent path yields a usable spec (see this module's docstring).
    """
    with pytest.raises(FileNotFoundError) as raised:
        root_entry_point("operator_panel_that_somebody_moved.py")
    message = str(raised.value)
    assert "operator_panel_that_somebody_moved.py" in message, "the path, or the reader cannot look"
    assert "renamed or removed" in message, "and WHY, which is the part a stack trace cannot say"
    assert "no import graph points at it" in message, (
        "and why nothing else would have caught it -- rule 2's reason, at the point of failure"
    )


@pytest.mark.parametrize("relative", ["tests", "docker", "README.md"])
def test_a_path_that_is_not_a_loadable_source_module_is_refused_by_name(relative):
    """The second guard, reached by three real paths rather than a contrived one.

    Two directories and a file that exists but that no loader claims. All three are
    cases where `spec_from_file_location` returns None, and the message has to name the
    path -- these are the ones a typo in a call site produces.
    """
    with pytest.raises(ImportError) as raised:
        root_entry_point(relative)
    assert "no import spec" in str(raised.value)
    assert relative in str(raised.value)


def test_two_names_give_TWO_module_objects_for_ONE_file():
    """Why `module_name` is a parameter at all, rather than always the path's stem.

    tests/test_operator_panel.py loads operator_panel.py as "operator_panel_entry" and
    tests/test_solana_payout.py loads THE SAME FILE as "operator_panel_entry_sol", on
    purpose, so the two test files get independent module objects and neither can
    observe the other's monkeypatching. Flattening both to the stem while merging the
    five copies would have silently made them one module -- a change no test in either
    file would have reported, since both would still have found their attributes.
    """
    first = root_entry_point("operator_panel.py", "panel_under_two_names_a")
    second = root_entry_point("operator_panel.py", "panel_under_two_names_b")
    try:
        assert first is not second, "two names, two module objects"
        assert first.__file__ == second.__file__, "and it is the same file on disk"
        assert sys.modules["panel_under_two_names_a"] is first
        assert sys.modules["panel_under_two_names_b"] is second, (
            "each at its own sys.modules key, which is what keeps them independent"
        )
    finally:
        for name in ("panel_under_two_names_a", "panel_under_two_names_b"):
            sys.modules.pop(name, None)


def test_a_dataclass_in_an_entry_point_LOADS_which_it_did_not_before_registration():
    """The incident, against the real file that found it: suite_baseline.py.

    `dataclasses._process_class()` checks for KW_ONLY through `_is_type()`, which does
    `sys.modules.get(cls.__module__).__dict__` -- so a module executed WITHOUT being
    registered first raises, from inside dataclasses.py, naming neither the entry point
    nor the cause. Measured 2026-10-10 by running the pre-fix sequence by hand against
    this same file:

        spec = spec_from_file_location("sb_unregistered", "suite_baseline.py")
        module_from_spec(spec); spec.loader.exec_module(module)
        -> AttributeError: 'NoneType' object has no attribute '__dict__'

    Four root entry points declare a `@dataclass` (atomic_swap.py, atomic_swap_xrp.py,
    suite_baseline.py, swap_terminal_desktop.py), so this is not an exotic case -- it is
    the one the first caller hit. The assertion is that the dataclass is USABLE, not
    merely that the import returned.
    """
    module = root_entry_point("suite_baseline.py", "suite_baseline_dataclass_probe")
    try:
        diff = module.compare({"a::t": module.PASSED}, {"a::t": module.PASSED})
        assert diff.regressed == (), "the frozen dataclass is constructed and read, not just defined"
    finally:
        sys.modules.pop("suite_baseline_dataclass_probe", None)


def test_the_module_is_registered_BEFORE_its_own_body_runs(tmp_path, monkeypatch):
    """Not merely registered -- registered FIRST, which is what the dataclass case needs.

    The dataclass test above proves the registration happens; it cannot prove the
    ORDER, because `compare()` is called after the load either way. This one asks the
    module's own body, at the moment it runs, whether it can see itself.

    THE ROOT IS REDIRECTED AT A tmp_path rather than a file being written into the
    repository, and that is the only way to ask this: the helper resolves against
    `conftest.REPO_ROOT` on purpose (a relative path is the point -- one caller loads
    out of `docker/`), and no real entry point in this tree raises or introspects at
    import. Patching the root patches an INPUT, not the logic under test.
    """
    monkeypatch.setattr(conftest, "REPO_ROOT", tmp_path)
    (tmp_path / "sees_itself.py").write_text(
        "import sys\nSAW_ITSELF = sys.modules.get(__name__) is not None\n",
        encoding="utf-8",
    )
    module = root_entry_point("sees_itself.py")
    try:
        assert module.SAW_ITSELF, (
            "the module body found itself in sys.modules, which is what dataclasses, "
            "typing.get_type_hints() and pickle all need and none of them says so"
        )
    finally:
        sys.modules.pop("sees_itself", None)


def test_a_FAILED_exec_leaves_NOTHING_in_sys_modules(tmp_path, monkeypatch):
    """A half-executed module left behind is worse than no module at all.

    The next importer gets it WITHOUT the exception and reads partial definitions as the
    real thing -- a failure that has moved one import away from its cause. Registering
    before exec (the test above) is what creates this hazard, so the cleanup is part of
    the same change rather than a separate nicety.

    Same tmp_path root for the same reason: nothing in this tree raises at import.
    """
    monkeypatch.setattr(conftest, "REPO_ROOT", tmp_path)
    (tmp_path / "dies_halfway.py").write_text(
        "HALF = 'defined before the failure'\nraise RuntimeError('the entry point is broken')\n",
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="the entry point is broken"):
        root_entry_point("dies_halfway.py")
    assert "dies_halfway" not in sys.modules, (
        "the key is gone, so the next caller gets the RuntimeError again rather than a "
        "module object carrying HALF and nothing else"
    )


def test_it_inserts_NOTHING_into_sys_path(tmp_path, monkeypatch):
    """A removal rather than an omission, and the five copies disagreed about it.

    Two of them did `sys.path.insert(0, root / "swap_terminal")` before loading --
    which tests/conftest.py already does at import, before any test module exists, so
    those two lines were dead. The other three did not. Nothing distinguished them; it
    was drift, and the merge resolved it by dropping the line.

    THE ENTRY POINT IS SYNTHETIC, AND THE FIRST VERSION OF THIS TEST WAS WRONG BECAUSE IT
    WAS NOT. Measured 2026-10-10, after it failed: EVERY root entry point in this tree
    does its own `sys.path.insert(0, <root>/swap_terminal)` as its first real statement --
    suite_baseline.py:80, operator_panel.py:59, reclaim_funding.py:52,
    grc_htlc_verify.py:79 -- because a root file cannot import this repository's modules
    without it (rule 10's layout gap, and the reason E402 is excluded from the lint
    ratchet). So loading any REAL entry point mutates sys.path no matter what the helper
    does, and asserting on sys.path after one measures the module, not the loader. A file
    that touches nothing is the only way to isolate the question.
    """
    monkeypatch.setattr(conftest, "REPO_ROOT", tmp_path)
    (tmp_path / "touches_nothing.py").write_text("LOADED = True\n", encoding="utf-8")
    before = list(sys.path)
    module = root_entry_point("touches_nothing.py")
    try:
        assert module.LOADED, "it did load, so the comparison below is about the loader"
        assert sys.path == before, "loading an entry point is not a reason to mutate sys.path"
    finally:
        sys.modules.pop("touches_nothing", None)
