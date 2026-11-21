"""Every third-party package the application imports must be DECLARED, at any nesting depth.

Role: hygiene gate (read-only)
Reads: requirements.txt, swap_terminal/requirements.txt, requirements-dev.txt,
       docker/*.Dockerfile, every .py under swap_terminal/
Writes: nothing
Can move funds: no
Mainnet-safe: yes

=============================================================================
WHY THIS EXISTS, and it is the same defect twice with a better hiding place
=============================================================================

swap_terminal/requirements.txt's own header records the first occurrence, measured
2026-09-24: the file "declared TWO packages (Flask and requests) while the tree
imported EIGHT", and the consequence was not theoretical -- a clean checkout could
not collect the suite, because `import bech32` ran at module level in
modules/atomic_htlc_scripts.py. The fix was to walk the AST and declare what was
found. The method it names is the part that matters here: "collecting every
TOP-LEVEL import".

IT HAPPENED AGAIN, and the top-level restriction is exactly why. Measured
2026-11-21, every third-party import under swap_terminal/ at any depth:

    base58  bech32  ecdsa        imported at MODULE level   all three declared
    xrpl    nacl    httpx        imported ONLY inside       NONE of the three
                                 functions                  declared

The three function-local ones live in chains/xrp.py, chains/xrp_submit.py and
chains/xrp_signing.py, each marked `# noqa: PLC0415 -- checked: optional
dependency`. They are function-local ON PURPOSE: a deployment with no XRP
configured must refuse cleanly rather than fail at import. That same choice made
them invisible to the audit meant to catch them, and an AST walk that stops at
column zero cannot see a dependency deliberately placed below it.

WHAT IT COST, measured the same day by building a venv with exactly the command
requirements-dev.txt used to document (`-r swap_terminal/requirements.txt -r
requirements-dev.txt`) and running this repository's own ship gate:

    24 failed, 5513 passed, 17 skipped
    regressed 24   disappeared 329   newly skipped 2   exit 1

The 329 are `pytest.importorskip` working correctly -- the SOL and XRP modules
skipped themselves, collection exited 0, and the suite simply got smaller with no
error anywhere. The 24 are the half that is not merely smaller:
test_the_XRP_PAYOUT_BLOCKER_IS_NOT_THE_RESERVE asserts WHY a payout is blocked, and
with the library absent the reason changes. So the documented setup command could
not pass the gate, and nothing said so.

NOT A VERSION PROBLEM, ruled out rather than assumed. The same venv with the ROOT
requirements.txt and pytest 8.4.2 -- the far side of requirements-dev.txt's own
`pytest>=8.0,<10.0` -- collected 5874 node ids byte-identical to pytest 9.2.0's and
returned exit 0 with every group at zero. The pytest major is irrelevant; the
requirements file was the whole of it.

=============================================================================
WHAT THIS GATE DOES AND DOES NOT ASSERT
=============================================================================

It pins the AUTHORITATIVE file -- the one the images install and the one
requirements-dev.txt now documents -- and asserts it covers every import at any
depth. It does NOT assert that swap_terminal/requirements.txt covers them, because
it still does not: that file is short by those same three, and adding packages to a
list whose stated purpose is a minimal install on a host holding wallet RPC
credentials is a deployment decision and the operator's (rule 16). That remainder is
NAMED WORK and not a baseline (rule 19): nothing here tolerates it, no count is
allowed, and the gate below asserts ZERO missing against the file that actually runs.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import pytest
from prose import dockerfile_without_comments

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
APP = REPOSITORY_ROOT / "swap_terminal"
DOCKER_DIR = REPOSITORY_ROOT / "docker"

#: Import name -> the distribution that provides it, for the cases where they differ.
#: Only these need an entry; everything else is compared under its own name, case
#: insensitively, because `Flask` is declared capitalized and imported lowercase.
#:
#: NOT BUILT FROM importlib.metadata.packages_distributions(), which would have been
#: shorter and is the wrong trade here: it can only map what is INSTALLED, so the
#: gate's verdict would depend on the machine running it -- and a test that asserts a
#: property of its own host is the defect five tests in this suite were fixed for
#: (tests/test_chain_balances.py's no_conf_route, tests/test_configure_env.py's
#: no_harness_conf, and tests/test_supervisor_restart.py's seeded verdict). A static
#: table is the same answer on every machine.
IMPORT_TO_DISTRIBUTION = {
    "xrpl": "xrpl-py",
    "nacl": "PyNaCl",
    "Crypto": "pycryptodome",
    "yaml": "PyYAML",
    "dotenv": "python-dotenv",
    "dateutil": "python-dateutil",
    "bs4": "beautifulsoup4",
}

#: The documented install command, which lives inside a COMMENT -- requirements-dev.txt
#: is entirely comments apart from three requirement lines. The `-r requirements-dev.txt`
#: tail is required by this pattern on purpose: the same file now also discusses
#: `pip install -r requirements.txt` in prose when describing swap_terminal/README.md,
#: and a looser pattern would match that sentence instead. tests/prose.py's header
#: counts ten occurrences of a gate matching its own explanation; this is the eleventh
#: place it could have happened, and exactly_one() below is why it did not.
DOCUMENTED_INSTALL = re.compile(
    r"^#\s+pip install -r (?P<runtime>\S+) -r requirements-dev\.txt\s*$", re.MULTILINE
)

#: `COPY <src> <dst>` of a requirements file inside a Dockerfile. Every compose file
#: in this repository builds with `context: .`, so <src> is repo-root-relative and is
#: directly comparable to the documented path.
DOCKERFILE_COPY = re.compile(r"^COPY\s+(?P<src>\S*requirements[\w.-]*\.txt)\s", re.MULTILINE)


def declared_distributions(path: Path) -> set[str]:
    """The distribution names a requirements file declares, lowercased."""
    out: set[str] = set()
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        out.add(re.split(r"[=<>!~\[;]", line)[0].strip().lower())
    return out


def local_module_names() -> set[str]:
    """Everything importable rootlessly from inside this repository.

    The application imports its own modules without a package prefix (`from config
    import Config`), which CLAUDE.md rule 10 names as the layout gap, so a bare
    `import supervisor` is NOT a third-party import and must not be demanded of a
    requirements file.
    """
    names = {p.stem for p in APP.rglob("*.py")}
    names |= {p.name for p in APP.rglob("*") if p.is_dir()}
    return names | {"swap_terminal", "tests", "conftest", "prose", "param_ids"}


def third_party_imports() -> dict[str, set[str]]:
    """{import name: {"path:line", ...}} for every import at ANY nesting depth.

    ast.walk() rather than a scan of module-level nodes, which is the whole point of
    this file: the three that went missing are inside function bodies.
    """
    local = local_module_names()
    found: dict[str, set[str]] = {}
    for path in sorted(APP.rglob("*.py")):
        if ".venv" in path.parts or "node_modules" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:  # pragma: no cover -- a syntax error is another test's job
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                # level > 0 is an explicit relative import, which is never third-party.
                names = [node.module.split(".")[0]] if node.module and node.level == 0 else []
            else:
                continue
            for name in names:
                if name in sys.stdlib_module_names or name in local or name.startswith("_"):
                    continue
                found.setdefault(name, set()).add(f"{path.relative_to(REPOSITORY_ROOT)}:{node.lineno}")
    return found


def documented_runtime_requirements() -> str:
    """The runtime requirements path requirements-dev.txt tells a developer to install."""
    text = (REPOSITORY_ROOT / "requirements-dev.txt").read_text()
    matches = DOCUMENTED_INSTALL.findall(text)
    assert len(matches) == 1, (
        f"requirements-dev.txt contains {len(matches)} lines matching the documented install "
        f"command {matches}. This gate reads that line to learn which requirements file a "
        f"developer is told to install, so more than one makes the answer ambiguous and zero "
        f"means the instruction was reworded past the pattern -- fix the file or the pattern, "
        f"and do not let this test silently measure the wrong sentence"
    )
    return matches[0]


def image_runtime_requirements() -> set[str]:
    """Every requirements path the docker/ images COPY in, repo-root-relative."""
    out: set[str] = set()
    for dockerfile in sorted(DOCKER_DIR.glob("*.Dockerfile")):
        text = dockerfile_without_comments(dockerfile.read_text())
        out.update(DOCKERFILE_COPY.findall(text))
    return out


def test_the_documented_dev_install_names_the_file_the_IMAGES_install():
    """One answer to "what does this application depend on", not two.

    MUTATION CAUGHT, and it is the exact state of the tree before 2026-11-21: the
    documented command named swap_terminal/requirements.txt (11 packages) while every
    image COPYs the root requirements.txt (13). Two answers drifted apart with nothing
    failing, and the developer-facing one was the one missing three packages the
    application imports.

    DERIVED FROM BOTH SOURCES rather than restating either: the documented path comes
    out of requirements-dev.txt's own instruction line, and the installed path out of
    the Dockerfiles' COPY lines with their comments stripped.
    """
    documented = documented_runtime_requirements()
    installed = image_runtime_requirements()
    assert installed, (
        "no docker/*.Dockerfile COPYs a requirements file, so this gate cannot establish "
        "which dependency set production actually installs"
    )
    assert installed == {documented}, (
        f"requirements-dev.txt documents `pip install -r {documented}` but the images install "
        f"{sorted(installed)}. Whichever is right, a developer and a container must not be "
        f"given two different answers about what this application depends on -- that is how "
        f"xrpl-py, PyNaCl and httpx came to be absent from one of them for weeks"
    )


def test_every_third_party_import_at_ANY_DEPTH_is_declared():
    """A CLEAN GATE at zero: no import of a package the authoritative file omits.

    THE ASSERTION IS THE ZERO and there is no baseline (rule 19). The file checked is
    the one the documented command names, which the test above pins to the one the
    images install.

    MUTATION CAUGHT: add `import solders` inside any function in chains/solana.py and
    this fails naming the file and line, where the top-level AST walk that built the
    requirements lists would not have seen it at all.
    """
    runtime = REPOSITORY_ROOT / documented_runtime_requirements()
    have = declared_distributions(runtime)
    imports = third_party_imports()
    assert imports, "no third-party import was found at all, so this gate would assert nothing"

    missing = {
        name: (IMPORT_TO_DISTRIBUTION.get(name, name), sorted(sites))
        for name, sites in sorted(imports.items())
        if IMPORT_TO_DISTRIBUTION.get(name, name).lower() not in have
    }
    assert missing == {}, (
        f"{runtime.name} declares {len(have)} distributions and the application imports "
        f"packages it does not cover: "
        + "; ".join(f"{name} -> {dist} at {sites[0]}" for name, (dist, sites) in missing.items())
        + ". An undeclared dependency does not fail at install time, when it would be cheap -- "
        "it fails on whichever machine happens not to have it. If the import is optional and "
        "guarded, it is still a dependency of the SUITE: measured 2026-11-21, the three that "
        "were missing cost 24 failures and 329 disappeared tests"
    )


@pytest.mark.parametrize(
    ("module", "distribution"),
    [("xrpl", "xrpl-py"), ("nacl", "PyNaCl"), ("httpx", "httpx")],
)
def test_the_three_that_a_TOP_LEVEL_walk_could_not_see_are_declared(module, distribution):
    """Named individually, because the general gate above would not say WHICH returned.

    These are pinned by name rather than left to the sweep because they are the
    measured instance: each is imported only inside a function body, each was absent
    from the list built by a top-level walk, and together they are what made the
    documented setup command fail the ship gate. A regression here is not a new class
    of defect -- it is this one coming back, and the message should say so.
    """
    imports = third_party_imports()
    assert module in imports, (
        f"nothing under swap_terminal/ imports {module} any more. If that is deliberate, delete "
        f"this parameter and say so; if it is not, an XRP or SOL code path lost its import"
    )
    sites = sorted(imports[module])
    assert all(":" in site for site in sites)
    have = declared_distributions(REPOSITORY_ROOT / documented_runtime_requirements())
    assert distribution.lower() in have, (
        f"{module} is imported at {sites[0]} but {distribution} is not declared in "
        f"{documented_runtime_requirements()}"
    )
