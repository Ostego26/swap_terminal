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

import re
from pathlib import Path

import pytest
import yaml
from test_no_key_material_is_tracked import looks_like_an_ed25519_keypair

REPO_ROOT = Path(__file__).resolve().parent.parent
class _ComposeLoader(yaml.SafeLoader):
    """SafeLoader that tolerates the Compose spec's own YAML tags.

    The Compose specification defines `!reset` and `!override` for withdrawing or
    replacing a key inherited from an earlier -f file. yaml.safe_load() refuses an
    unrecognized tag outright, so the moment docker-compose.web.hostnet.yml used
    `ports: !reset null` -- which is the only mechanism that actually removes an
    inherited publish, an empty list does not -- this whole module stopped
    COLLECTING: "ConstructorError ... could not determine a constructor for the
    tag '!reset'", and every assertion in it went from passing to not running.

    A gate that cannot parse the files it guards is worse than no gate, because
    "1 error during collection" in a long suite reads like an environment problem
    rather than like unguarded compose files.

    The tag's VALUE is what matters here and its semantics do not: this module asks
    which build contexts exist and which variables are required, and `!reset null`
    on a ports key answers neither. So unknown tags resolve to their underlying
    node and nothing pretends to interpret them.
    """


# tag_suffix is unused and must stay: PyYAML's add_multi_constructor calls this
# with exactly three arguments. Not marked `noqa: ARG001` -- that rule is not
# enabled for tests/, so the directive would suppress nothing and RUF100 said so.
def _ignore_unknown_tag(loader, tag_suffix, node):
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    return loader.construct_mapping(node)


_ComposeLoader.add_multi_constructor("", _ignore_unknown_tag)


def load_compose(path: Path) -> dict:
    """Parse a compose file, tags and all. Returns {} for an empty file."""
    return yaml.load(path.read_text(), Loader=_ComposeLoader) or {}  # noqa: S506 -- _ComposeLoader derives from SafeLoader; the only addition is a constructor that returns the plain node for compose's own !reset / !override tags


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
        spec = load_compose(compose)
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


#: Matches compose's required-variable form, `${NAME:?message}`.
_REQUIRED_VAR = re.compile(r"\$\{([A-Z_][A-Z0-9_]*):\?")


def required_variables(compose: Path) -> set[str]:
    """The `${VAR:?}` names compose will actually interpolate, comments excluded.

    COMMENTS MUST BE STRIPPED FIRST, and the first version of this did not do it.
    These files explain the rule they obey, and the explanation necessarily writes
    `${VAR:?reason}` as prose -- so the gate matched its own documentation and
    reported a required variable literally named VAR, failing every file including
    the base. Found by running it: the test went red on a tree that was correct.

    A gate that cannot tell a file's code from a file's comments about its code is
    the rule-8 defect it was written to prevent, wearing the shape of a regex.
    """
    names: set[str] = set()
    for raw in compose.read_text().splitlines():
        line = raw.strip()
        if line.startswith("#"):
            continue
        # A trailing ` #` comment on a real line, e.g. `PORT: "5000"  # why`.
        code = raw.split(" #", 1)[0]
        names.update(_REQUIRED_VAR.findall(code))
    return names


@pytest.mark.parametrize("compose", COMPOSE_FILES, ids=[p.name for p in COMPOSE_FILES])
def test_each_compose_file_has_at_most_one_required_variable(compose):
    """AT MOST ONE `${VAR:?}` PER FILE. This defect landed twice in one day.

    `docker compose` interpolates EVERY variable in EVERY file it is given, before
    it looks at which service was named. A required variable therefore taxes every
    command that includes its file, not just the commands that use its service.

      first time, 2026-10-05   all four services were in docker-compose.yml, so
                               the guards on `web` and `grc-desk` made
                               `docker compose up -d abstergo` fail with two
                               errors about variables abstergo does not use. The
                               two lowest-risk increments could not be started at
                               all.
      second time, SAME DAY    `web` and `grc-desk` were moved out together, into
                               ONE file. So `docker compose -f docker-compose.yml
                               -f docker-compose.stateful.yml up -d web` demanded
                               GRC_DESK_DATADIR. Measured on the operator's host:
                               the command started nothing and curl answered
                               HTTP 000.

    Splitting "the stateful ones" as a group was still grouping by the wrong
    thing. The grouping that matters is not what a service IS, it is which
    variables a command will be forced to supply -- so the unit is one required
    variable per file.

    The guards themselves are not negotiable and do not move: a default for
    GRC_DESK_DATADIR would be a compose file choosing which wallet to open, and
    pointed at the customer's datadir it is a second daemon on the customer's own
    wallet. The fix is always to split the file, never to soften the guard.
    """
    required = required_variables(compose)
    assert len(required) <= 1, (
        f"{compose.name} requires {len(required)} different variables: {sorted(required)}.\n\n"
        "Every `docker compose` command that includes this file must supply ALL of "
        "them, even to start a service that uses none of them -- compose interpolates "
        "the whole file before it selects a service. Move each required variable's "
        "service into its own docker-compose.<name>.yml. Do NOT give any of them a "
        "default to make this pass: the defaults are what these guards exist to "
        "prevent."
    )


def test_no_two_overlay_files_require_different_variables_of_each_other():
    """And the base file must require nothing, so it composes with every overlay.

    docker-compose.yml is in every command by design -- it holds the services with
    no required variable and the shared definitions. A required variable there is
    the first version of this defect exactly: it would tax every overlay and every
    service, including the ones that need nothing.
    """
    base = REPO_ROOT / "docker-compose.yml"
    assert base in COMPOSE_FILES, f"no {base.name} among {[p.name for p in COMPOSE_FILES]}"
    required = required_variables(base)
    assert not required, (
        f"{base.name} requires {sorted(required)}. It is included in every compose "
        f"command, so anything required here is required to start EVERY service -- "
        f"which is how `docker compose up -d abstergo` came to fail on a Gridcoin "
        f"datadir variable. Move that service to its own overlay file."
    )


def bind_mounts() -> list[tuple[str, str, str]]:
    """(compose file, service, the mount spec) for every host-path bind mount.

    Named volumes are excluded by the leading-dot test: a source beginning with
    "." is a path on the host, anything else is a volume name. Only the former can
    have the inode problem below.
    """
    found = []
    for compose in COMPOSE_FILES:
        spec = load_compose(compose)
        for name, service in (spec.get("services") or {}).items():
            for mount in service.get("volumes") or []:
                if isinstance(mount, str) and mount.startswith("."):
                    found.append((compose.name, name, mount))
                elif isinstance(mount, dict) and str(mount.get("source", "")).startswith("."):
                    found.append((compose.name, name, f"{mount['source']}:{mount.get('target')}"))
    return found


def test_no_compose_file_bind_mounts_a_single_file():
    """A single-file bind mount follows the INODE and goes stale on every `git pull`.

    A CLEAN GATE, not a baseline (rule 19): the count asserted is zero.

    MEASURED ON THE LIVE HOST, 2026-10-06. docker-compose.icp.yml mounted
    ./dfx.json, ./Cargo.toml and ./Cargo.lock individually, because dfx.json was
    at the repository root and the root cannot be mounted wholesale -- it holds the
    Python tree and a Solana keypair. Then a pull added a canister to dfx.json:

        container  /repo/dfx.json  inode 40898326  259 bytes  grep -c icp_ledger_canister -> 0
        host       ./dfx.json      inode 40898329  704 bytes  grep -c icp_ledger_canister -> 1

    `git pull` does not edit a file in place. It writes a new file and renames it
    over the old, so the inode changes and a file bind mount keeps resolving to the
    inode it captured at container start. The container had been reading a
    dfx.json that no longer existed on disk.

    THE ONLY SYMPTOM was `dfx deploy icp_ledger_canister` reporting "Canister
    'icp_ledger_canister' not found in dfx.json" -- about a file that plainly
    declared it. Nothing crashed, nothing warned, and it would have recurred on
    every single pull. That is rule 13's shape, and rule 13's own instruction is
    the fix: verify the artifact, not the deploy.

    The resolution was structural -- dfx.json and the Cargo workspace moved into
    icp/ so the mount is a directory, which resolves names on each access. This
    gate exists because the next person to need "just one config file" in a
    container will reach for exactly the mount that broke, and nothing about it
    looks wrong.

    IT CHECKS THE FILESYSTEM, not the spelling. A path is a file or it is not, and
    a name that merely looks like one ("./icp" has no extension and is a
    directory; "./docker-compose.web.yml" has three dots and is a file) cannot be
    told apart any other way. A source that does not exist is reported too: a
    bind mount Docker would silently create as an empty directory is its own
    defect, and scoring it as a pass is what rule 17 calls examining nothing.
    """
    mounts = bind_mounts()
    assert mounts, (
        "no compose file in this repository bind-mounts a host path at all, so this test "
        "checked NOTHING (rule 17)"
    )
    offenders = []
    for compose, service, mount in mounts:
        source = REPO_ROOT / mount.split(":")[0]
        if source.is_file():
            offenders.append(f"{compose}:{service} mounts the FILE {mount}")
        elif not source.exists():
            offenders.append(f"{compose}:{service} mounts {mount}, which does not exist on the host")
    assert not offenders, (
        "single-file bind mounts go stale on `git pull` because they follow the inode -- mount "
        "the containing DIRECTORY instead:\n" + "\n".join(f"  {line}" for line in offenders)
    )
