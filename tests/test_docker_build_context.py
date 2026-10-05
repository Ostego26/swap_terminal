"""No build context in this repository may carry key material to the Docker daemon.

Role: code hygiene (read-only)
Reads: docker-compose.yml, every .dockerignore it points at, and the files inside
       each build context
Writes: nothing
Can move funds: no
Mainnet-safe: yes

A CLEAN GATE, not a baseline (CLAUDE.md rule 19): the count asserted is zero, so
there is nothing to ratchet down and nothing to delete later.

WHY THIS EXISTS: THE SAME DEFECT LANDED TWICE, FROM TWO DIRECTIONS.

`swap_terminal/grc-sol-swap/abstergo_exchange/wgrc.json` is an ed25519 Solana
keypair. It is on disk, untracked, and `docker build` uploads the ENTIRE build
context to the daemon before the first instruction runs -- so a context that
contains it ships a private key into a build cache that nothing in this
repository can see or clean. No COPY line has to reference it. That was never
the hazard.

  2026-10-05, commit 2fa9972   the `abstergo` service was added with
                               `context: ./swap_terminal/grc-sol-swap/abstergo_exchange`
                               and that directory's own .dockerignore was written
                               specifically to exclude the keypair.
  2026-10-05, commit 3da5744   `harness` and `web` were added with `context: .`,
                               which reintroduced the identical hazard, because a
                               .dockerignore applies to ONE context and the
                               repository root is a different context. 82M of
                               tree, keypair inside, two builds.

Twice in one day, by the same author, for the same reason, is not a slip -- it is
a missing check. Rule 19's test for a patch applies: a comment stops the symptom
being reported to whoever reads that file; a gate stops the cause existing for
the next service nobody has written yet.

WHAT IT ASSERTS, and why this shape rather than a .dockerignore simulator.

Reimplementing Docker's ignore-pattern matcher faithfully is a second
implementation of somebody else's rule, which rule 8 says is a bug with a delay
on it: the moment the two disagree, this test passes while a build ships a key.
So it asserts something stricter and checkable instead --

    for every build context, if the context CONTAINS a file whose bytes are an
    ed25519 keypair, then that context's .dockerignore must name that file's
    path EXPLICITLY, as a literal line.

An exact path is unambiguous for both Docker and this test. A glob might be
equivalent and this will still fail on it, which is deliberate: the 2026-09-25
lesson is that `*keypair*.json` in .gitignore did not match a file called
`wgrc.json`, so "a pattern that probably covers it" is exactly the reasoning that
has already failed here once.

WHAT IT REIMPLEMENTS: NOTHING. The keypair shape test is
tests/test_no_key_material_is_tracked.looks_like_an_ed25519_keypair -- the one
place that definition lives (rule 8). A second copy here would be two answers to
"is this a key".
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from test_no_key_material_is_tracked import looks_like_an_ed25519_keypair

REPO_ROOT = Path(__file__).resolve().parent.parent
#: EVERY compose file, not just docker-compose.yml. The stateful services were
#: split into docker-compose.stateful.yml on 2026-10-05, and a gate that reads one
#: file would have silently stopped covering half the services the moment that
#: happened -- which is the shape of defect this whole file exists to prevent.
COMPOSE_FILES = sorted(REPO_ROOT.glob("docker-compose*.yml"))

#: Directories never worth walking when looking for key material in a context.
#: NOT a correctness filter -- a keypair under .git is history, not a context
#: file, and node_modules is 10k files of somebody else's code. Skipping them is
#: a speed decision and nothing is excluded that a build could ship.
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".pytest_cache", ".ruff_cache", ".venv"}


def build_contexts() -> list[tuple[str, Path, Path]]:
    """(service, context directory, the .dockerignore that applies to it).

    Docker resolves .dockerignore relative to the BUILD CONTEXT, not to the
    Dockerfile and not to the compose file. That is the whole reason this test
    exists, so it is the one thing the helper must get right.
    """
    found = []
    for compose in COMPOSE_FILES:
        spec = yaml.safe_load(compose.read_text()) or {}
        for name, service in (spec.get("services") or {}).items():
            build = service.get("build")
            if not build:
                continue
            context = build if isinstance(build, str) else build.get("context", ".")
            context_dir = (REPO_ROOT / context).resolve()
            found.append((name, context_dir, context_dir / ".dockerignore"))
    return found


def keypairs_in(context_dir: Path) -> list[Path]:
    """Every file under `context_dir` whose BYTES are an ed25519 keypair.

    By shape, not by name, for the reason the sibling test's docstring gives: a
    filename pattern is a guess about what somebody names a secret, and the guess
    has already been wrong here once.
    """
    found = []
    for path in context_dir.rglob("*"):
        if any(part in SKIP_DIRS for part in path.relative_to(context_dir).parts):
            continue
        if path.is_file() and looks_like_an_ed25519_keypair(path):
            found.append(path)
    return found


def ignored_paths(dockerignore: Path) -> set[str]:
    """The literal, non-comment lines of a .dockerignore, normalized.

    Only exact paths. See the module docstring: a glob is not accepted even when
    it would work, because "a pattern that probably covers it" is the reasoning
    that failed on 2026-09-25.
    """
    if not dockerignore.is_file():
        return set()
    lines = set()
    for raw in dockerignore.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        lines.add(line.lstrip("/").rstrip("/"))
    return lines


def test_the_compose_file_declares_at_least_one_build_context():
    """Otherwise every test below is vacuous and would pass by finding nothing.

    Rule 17: a check that examined nothing is not a pass, and it must say which.
    """
    assert COMPOSE_FILES, f"no docker-compose*.yml under {REPO_ROOT} at all"
    contexts = build_contexts()
    assert contexts, (
        f"no service in {[p.name for p in COMPOSE_FILES]} declares a `build:` context, "
        f"so the key-material gate below checked NOTHING"
    )


@pytest.mark.parametrize("service,context_dir,dockerignore", build_contexts(),
                         ids=[c[0] for c in build_contexts()])
def test_no_build_context_can_ship_an_ed25519_keypair(service, context_dir, dockerignore):
    """Every keypair inside a context must be named EXPLICITLY in that context's ignore.

    This is the test that would have caught 3da5744 before it was pushed: `harness`
    and `web` both take `context: .`, the repository root contains wgrc.json, and
    the root had no .dockerignore at all.
    """
    keypairs = keypairs_in(context_dir)
    if not keypairs:
        return
    ignored = ignored_paths(dockerignore)
    unguarded = [
        path for path in keypairs
        if str(path.relative_to(context_dir)) not in ignored
    ]
    assert not unguarded, (
        f"service {service!r} builds from {context_dir}, which contains "
        f"{len(unguarded)} ed25519 keypair(s) that {dockerignore} does not name:\n  "
        + "\n  ".join(str(p.relative_to(context_dir)) for p in unguarded)
        + "\n\n`docker build` uploads the WHOLE context to the daemon before the first"
        "\ninstruction runs, so this ships a private key into a build cache that nothing"
        "\nhere can see or clean. No COPY line has to reference it."
        "\n\nAdd the exact relative path to that .dockerignore. A glob is not accepted"
        "\neven if it would match: `*keypair*.json` in .gitignore failed to match"
        "\nwgrc.json on 2026-09-25, which is how it got committed."
    )


@pytest.mark.parametrize("service,context_dir,dockerignore", build_contexts(),
                         ids=[c[0] for c in build_contexts()])
def test_every_build_context_has_a_dockerignore_at_all(service, context_dir, dockerignore):
    """A context with no .dockerignore ships everything, and that is how this happened.

    Separate from the test above on purpose: that one passes for a context that
    happens to hold no key material today, which says nothing about tomorrow. The
    repository root held no .dockerignore when `harness` and `web` were added, and
    a 5198-file context is a standing invitation for the next secret somebody
    leaves on disk.
    """
    assert dockerignore.is_file(), (
        f"service {service!r} builds from {context_dir} with no .dockerignore, so its "
        f"build context is every file under that directory -- 46M of .git included. "
        f"Create {dockerignore}."
    )


#: The two mounts that must never be defaulted, and what a wrong value does.
#:
#: Both use compose's `${NAME:?message}` form, which makes the variable REQUIRED:
#: `docker compose config` refuses with the message rather than starting. A test
#: rather than a comment because the failure is silent in the direction that
#: matters -- a default that happens to be wrong starts a container and looks fine.
REQUIRED_MOUNT_VARIABLES = {
    # Pointed at the customer's datadir this is a SECOND DAEMON ON THE CUSTOMER'S
    # OWN WALLET, which is the exact defect the desk/customer split exists to
    # prevent, reached from the other direction.
    "GRC_DESK_DATADIR": "grc-desk",
    # swap_terminal.db is WAL-mode SQLite. A default would pick a directory whose
    # filesystem nobody checked, and WAL on FUSE/virtiofs/NFS fails by corrupting
    # rather than by erroring.
    "SWAP_DB_DIR": "web",
}


@pytest.mark.parametrize("variable,service", sorted(REQUIRED_MOUNT_VARIABLES.items()))
def test_the_highest_stakes_mounts_are_required_not_defaulted(variable, service):
    """`${VAR:?...}` and never `${VAR:-something}` for these two.

    Asserted against the compose file's TEXT rather than its parsed form, because
    what is being checked is the interpolation syntax itself -- which is exactly
    the thing yaml.safe_load() throws away.
    """
    text = "\n".join(p.read_text() for p in COMPOSE_FILES)
    assert f"${{{variable}:?" in text, (
        f"{variable} must use compose's required form ${{{variable}:?reason}} so that "
        f"`docker compose config` REFUSES when it is unset. Service {service!r} mounts "
        f"a volume at it, and a default here would be a compose file choosing which "
        f"wallet or which database to open."
    )
    assert f"${{{variable}:-" not in text, (
        f"{variable} has a DEFAULT (${{{variable}:-...}}) in one of "
        f"{[p.name for p in COMPOSE_FILES]}. A default for this mount starts a "
        f"container against a path nobody chose."
    )
