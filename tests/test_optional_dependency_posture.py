"""The signing libraries are optional ON PURPOSE, and that posture is now pinned.

Role: hygiene gate (read-only)
Reads: requirements.txt, swap_terminal/requirements.txt, every .py under swap_terminal/
Writes: nothing
Can move funds: no
Mainnet-safe: yes

=============================================================================
WHY THIS EXISTS: A SESSION READ THE POSTURE AS DRIFT AND NEARLY DELETED IT
=============================================================================

There are two runtime requirements files and they declare different sets. On
2026-11-21 that was diagnosed as rule 8 duplication -- two answers to one
question, drifted apart -- and the proposed fix was to merge them into the larger
one. That diagnosis was WRONG and the merge would have destroyed a deliberate
security property.

The posture is stated in the code, in four places, and none of them is a
requirements file:

  chains/xrp.py:1786     "xrpl-py is an OPTIONAL dependency, chains/registry.py
                         imports this module unconditionally, and a module-level
                         import would make a signing library mandatory in order
                         to start a read-only deposit watcher."
  chains/solana_signing.py:514
                         the same judgment for PyNaCl, naming chains/registry.py
                         importing chains/solana.py unconditionally
  chains/base.py:380     the Flask app does not drag the atomic-swap
                         dependencies in, "as a deployment property"
  tests/test_xrp_escrow_payloads.py:29
                         xrpl-py "is an OPTIONAL dependency here ... so this
                         skips when it is absent"

So the minimal set is not a stale copy of the full one. It is the set for a host
that WATCHES DEPOSITS AND CANNOT SIGN, and the omission of the signing libraries
is the point of it. Merging the files would have put a signing library onto that
host, which is the opposite of what every comment above asks for.

WHAT WAS ACTUALLY WRONG, and it was one thing: requirements-dev.txt told a
DEVELOPER to install the deployment-minimal set. A developer needs the full one,
because the suite needs it -- measured 2026-11-21, 24 failed and 329 disappeared
without it. That is fixed where it belongs, in the documented command, and
tests/test_declared_dependencies_cover_every_import.py holds it.

THE LESSON THIS FILE IS, and why it is a test rather than another paragraph: the
intent lived only in prose, scattered across four files, none of them the file a
reader would open to ask "why are these lists different?". Prose about structure
goes stale silently -- which is the argument swap_terminal/architecture.py exists
to make -- and here it did worse than go stale: it stayed correct and went
UNREAD, and a reader who had measured everything else still got it backwards. A
description of structure is worth what its test is worth.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from test_declared_dependencies_cover_every_import import (
    IMPORT_TO_DISTRIBUTION,
    declared_distributions,
    third_party_imports,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
FULL = REPOSITORY_ROOT / "requirements.txt"
MINIMAL = REPOSITORY_ROOT / "swap_terminal" / "requirements.txt"

#: import name -> distribution, for the libraries that are optional BY DESIGN, each
#: with the file that records the judgment. Not derived: this is the design decision
#: itself, and a derivation would just be this table computed from the lists it is
#: supposed to check.
OPTIONAL_BY_DESIGN = {
    "xrpl": ("xrpl-py", "swap_terminal/chains/xrp.py"),
    "nacl": ("PyNaCl", "swap_terminal/chains/solana_signing.py"),
    "httpx": ("httpx", "swap_terminal/chains/xrp.py"),
}

#: Declared in a requirements file but imported by nothing, legitimately. gunicorn
#: RUNS the application (`gunicorn -c gunicorn.conf.py wsgi:app`) rather than being
#: imported by it, so it is a real runtime dependency with no import site.
DECLARED_BUT_NOT_IMPORTED = {"gunicorn"}


@pytest.mark.parametrize("module", sorted(OPTIONAL_BY_DESIGN))
def test_an_optional_library_is_in_the_FULL_set_and_NOT_in_the_minimal_one(module):
    """Both halves, because either one alone is a different system.

    MUTATION CAUGHT, and it is the mistake this file was written after. Add xrpl-py
    to swap_terminal/requirements.txt -- the obvious "fix" for two lists that differ
    -- and this fails, naming the file that explains why the omission is deliberate.
    Remove it from requirements.txt and this fails the other way, because the suite
    and any host that actually pays XRP out do need it.
    """
    distribution, cited_by = OPTIONAL_BY_DESIGN[module]
    full = declared_distributions(FULL)
    minimal = declared_distributions(MINIMAL)

    assert distribution.lower() in full, (
        f"{distribution} is not declared in requirements.txt, which is the set a host that PAYS "
        f"must install and the set the test suite needs -- measured 2026-11-21, without it 24 "
        f"tests fail and 329 disappear"
    )
    assert distribution.lower() not in minimal, (
        f"{distribution} is declared in swap_terminal/requirements.txt, which is the MINIMAL set "
        f"for a host that watches deposits and cannot sign. {cited_by} records why that is "
        f"deliberate: a module-level import would make a signing library mandatory in order to "
        f"start a read-only deposit watcher. If the posture has genuinely changed, change it "
        f"THERE first and say so -- do not merge the lists because they differ"
    )


@pytest.mark.parametrize("module", sorted(OPTIONAL_BY_DESIGN))
def test_an_optional_library_is_NEVER_imported_at_MODULE_level(module):
    """The invariant the whole posture rests on, asserted rather than assumed.

    Leaving a library out of a requirements file does not make it optional. What
    makes it optional is that nothing imports it at import time: chains/registry.py
    imports chains/solana.py and chains/xrp.py UNCONDITIONALLY, so a single
    module-level `import xrpl` anywhere in that chain makes the package mandatory to
    start a read-only deposit watcher, and the minimal set stops being installable.

    MUTATION CAUGHT: hoist any of the `# noqa: PLC0415 -- checked: optional
    dependency` imports in chains/xrp.py, chains/xrp_submit.py,
    chains/xrp_signing.py or chains/solana_signing.py to module scope. ruff's PLC0415
    would stop complaining, the suite would still pass everywhere else, and the
    deposit watcher would have acquired a hard dependency on a signing library. This
    is the only assertion that notices.
    """
    at_module_level = third_party_imports(scope="module")
    nested = third_party_imports(scope="nested")

    assert module not in at_module_level, (
        f"{module} is imported at MODULE level at {min(at_module_level[module])}. That makes "
        f"it mandatory in order to import the package, so a host installing only "
        f"swap_terminal/requirements.txt can no longer start -- and the file that omits it is "
        f"correct, not out of date"
    )
    assert module in nested, (
        f"nothing under swap_terminal/ imports {module} inside a function any more. If the code "
        f"genuinely stopped using it, drop it from OPTIONAL_BY_DESIGN and from requirements.txt "
        f"in the same change; a package declared for nobody is the defect pycryptodome was"
    )


def test_the_MINIMAL_set_covers_every_import_that_runs_at_IMPORT_time():
    """A read-only host must be able to import the application with the minimal set.

    This is the minimal file's actual contract, and it is the one worth testing: not
    "it lists everything" -- it deliberately does not -- but "it lists everything
    needed to IMPORT the tree". Every module-level third-party import must be in it,
    because those run before any guard can refuse.

    MUTATION CAUGHT, and it is the 2026-09-24 defect this file's sibling records: a
    module-level `import bech32` in modules/atomic_htlc_scripts.py with bech32
    undeclared meant a clean checkout could not even collect the suite.
    """
    minimal = declared_distributions(MINIMAL)
    module_level = third_party_imports(scope="module")
    # THROUGH IMPORT_TO_DISTRIBUTION, for the third time in this file's first hour.
    # Comparing an IMPORT name against a DECLARED distribution name only works where
    # they happen to be spelled the same: `dotenv` is declared as python-dotenv, so a
    # raw comparison reported swap_terminal/transactions.py:57 as an undeclared
    # module-level import of a package that has been declared all along. The table is
    # one import away and there is no excuse for a local copy or a bare comparison.
    missing = {
        name: min(sites)
        for name, sites in sorted(module_level.items())
        if IMPORT_TO_DISTRIBUTION.get(name, name).lower() not in minimal
    }
    assert missing == {}, (
        f"swap_terminal/requirements.txt omits packages imported at IMPORT TIME: {missing}. These "
        f"run before any optional-dependency guard can refuse, so a host with only the minimal "
        f"set cannot import the tree at all -- which is how the bech32 gap was found"
    )


@pytest.mark.parametrize("requirements", [FULL, MINIMAL], ids=["full", "minimal"])
def test_no_requirements_file_declares_a_package_NOTHING_imports(requirements):
    """A CLEAN GATE at zero: no declaration outlives the code that needed it.

    MEASURED 2026-11-21 and this is why the gate exists: swap_terminal/requirements.txt
    declared pycryptodome under thirty lines naming chains/monero_keys.py as its only
    importer and "the 26 tests in tests/test_monero_keys.py". Neither file existed, no
    file matching *monero* existed, and nothing in the tree imported `Crypto`. A
    package was being installed onto a host holding wallet RPC credentials, kept alive
    by an argument that was correct and about deleted code -- rule 16's wrong comment
    at its most convincing, because thirty measured lines are harder to doubt than one.

    The allowlist is one entry and is justified at its definition, not here.
    """
    declared = declared_distributions(requirements)
    # IMPORT_TO_DISTRIBUTION IS IMPORTED, NOT RESTATED. The first version of this
    # test inlined a two-entry map ({"xrpl": ..., "nacl": ...}) and therefore reported
    # python-dotenv as an orphan in BOTH files, because `dotenv` maps to it and the
    # inline map did not know that. Two copies of one table, disagreeing within the
    # hour (rule 8).
    imported = {
        IMPORT_TO_DISTRIBUTION.get(name, name).lower() for name in third_party_imports()
    }
    orphans = sorted(declared - imported - DECLARED_BUT_NOT_IMPORTED)
    assert orphans == [], (
        f"{requirements.relative_to(REPOSITORY_ROOT)} declares {orphans}, which nothing under "
        f"swap_terminal/ imports at any nesting depth. Either the importer was culled and the "
        f"declaration outlived it, or the package runs the application rather than being imported "
        f"-- if the second, add it to DECLARED_BUT_NOT_IMPORTED with the reason"
    )
