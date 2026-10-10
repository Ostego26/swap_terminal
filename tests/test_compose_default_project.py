"""A bare `docker compose up` must start the TERMINAL, and nothing that arms or tests.

Role: code hygiene (read-only)
Reads: every docker-compose*.yml in the repository root, as YAML and as text;
       swap_stack.COMPOSE_FILES / UP_SERVICES; chains/icp._COMPOSE_FILES;
       stack_authority.REPLICA_STATE_PATH
Writes: nothing
Can move funds: no. It parses YAML and compares names. It starts no container,
        runs no `docker`, opens no socket and reads no key.
Mainnet-safe: yes -- there is no network here at all.

A CLEAN GATE, not a baseline (CLAUDE.md rule 19): every count asserted is exact.

WHAT WAS MEASURED, on this checkout, before any of it changed:

    8 docker-compose*.yml files        0 declared `include:`
                                       0 declared `profiles:`
    a bare `docker compose up`         read docker-compose.yml alone and started
                                       `abstergo` + `harness` -- the Node bridge
                                       and the REGTEST HARNESS
    the terminal                       assembled only inside swap_stack.py, as
                                       ("docker-compose.yml",
                                        "docker-compose.icp.yml",
                                        "docker-compose.web.yml")
                                       with its services NAMED, because neither
                                       `abstergo` nor `harness` had a profile

So the literal command an operator types started the two services that are not
the terminal, and the terminal was reachable only through a tool that happened to
know three filenames. docker-compose.yml now declares `include:` for the other
two and both non-terminal services sit behind a profile.

WHY A TEST AND NOT A COMMENT, for each of the five properties below:

  the default set      compose decides this from `include:` plus `profiles:`
                       across four files. Nobody can read four files and be sure,
                       and the way it goes wrong is that a service JOINS the
                       default set silently -- which is how a regtest harness came
                       to start on a host holding funded testnet wallets.
  no armed overlay     the one property where being wrong is a custody change.
                       An `include:` of docker-compose.web.armed-sol.yml would arm
                       SOL for every `docker compose up` on the host, with no act
                       by anybody (rule 16: arming is the operator's decision).
  no double-apply      a file that `include:` supplies must not ALSO be passed as
                       `-f`. What compose does with the same file twice was not
                       measurable where this was written, so the invariant is that
                       nothing depends on the answer (rule 17).
  one required var     SWAP_DB_DIR, and exactly one. `include:` makes a file's
                       required variables transitive, which is the 2026-10-05
                       defect this tree split eight files over, reached through a
                       different door. One is the accepted cost; two is the defect.
  the replica volume   `docker compose down` is now one short command away from
                       the whole terminal, and it REMOVES containers. The only
                       reason that is not a repeat of 2026-10-07's destroyed
                       ledger is that the replica's dfx state sits on a NAMED
                       volume, which survives a `down` without `-v`.

NONE OF THIS RUNS DOCKER, and that bounds what it can claim. It asserts what the
compose files SAY. Whether compose then behaves as the files say -- in particular
whether an overlay may override a service that arrived through `include:` -- is
not established anywhere in this repository and is written up in
docker-compose.yml's hazard 3 and in OPEN_FINDINGS.md.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

# The parser, and the required-variable reader, from the module that already owns
# them. Rule 8: a second `_ComposeLoader` here would be a second thing that has to
# learn about compose's `!reset` / `!override` tags, and the first one learned about
# them by having a whole module stop COLLECTING when docker-compose.web.hostnet.yml
# started using one.
from test_docker_build_context import load_compose, required_variables

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import swap_stack  # noqa: E402  -- after the sys.path.insert above, same as every root tool
from swap_terminal.chains import icp as icp_module  # noqa: E402
from swap_terminal.stack_authority import REPLICA_STATE_PATH, include_entries  # noqa: E402

#: The services a bare `docker compose up` must start, and the whole of it.
#:
#: NOT READ FROM swap_stack.UP_SERVICES. That constant is one of the two things
#: being compared, and a test that derives its expectation from its subject passes
#: on a tree where both are wrong together -- the exact false pass
#: tests/test_stack_authority.py's own compose-file test warns about ("a test that
#: pins a name rather than the artifact the name resolves to").
TERMINAL_SERVICES = {"icp-replica", "web"}

#: Files that must never be reachable from an `include:`, and why each one.
#:
#: The three armed overlays are the load-bearing entries: each mounts or passes the
#: key that lets the container sign and broadcast on one chain. hostnet replaces the
#: container's network mode, so it is an ALTERNATIVE rather than an addition, and
#: grc-desk carries a second required variable (GRC_DESK_DATADIR) which including
#: would make mandatory for every command.
NEVER_INCLUDED = {
    "docker-compose.web.armed-grc.yml",
    "docker-compose.web.armed-sol.yml",
    "docker-compose.web.armed-xrp.yml",
    "docker-compose.web.hostnet.yml",
    "docker-compose.grc-desk.yml",
}

#: The one variable the default project is allowed to require.
#:
#: ONE AND NOT ZERO, deliberately. docker-compose.web.yml's `${SWAP_DB_DIR:?...}` is
#: a guard that must not move -- a default there would be a compose file choosing
#: which swap database the terminal opens, and the two candidate defaults are both
#: wrong in a way nobody would notice (an empty database looks exactly like a
#: working one with no swaps in it). Including that file makes the variable required
#: for a bare `up`, which is a hard failure with a readable message, which is
#: correct. A SECOND one arriving the same way is not.
DEFAULT_PROJECT_REQUIRES = {"SWAP_DB_DIR"}


def compose_files() -> list[Path]:
    """Every docker-compose*.yml in the root, sorted. Globbed on purpose here."""
    return sorted(REPO_ROOT.glob("docker-compose*.yml"))


def included_by(path: Path) -> list[str]:
    """The filenames `path` declares in its own top-level `include:`. Not recursive.

    Compose's `include` accepts a bare string, or a mapping with a `path` key whose
    value is itself a string or a list. All three are read, because a gate that only
    understands the spelling in use today stops covering the file the moment
    somebody uses another one -- and the two spellings mean the same thing, so the
    drift would be invisible.
    """
    spec = load_compose(path)
    names: list[str] = []
    for entry in spec.get("include") or []:
        if isinstance(entry, str):
            names.append(entry)
        elif isinstance(entry, dict):
            target = entry.get("path")
            if isinstance(target, str):
                names.append(target)
            elif isinstance(target, list):
                names.extend(str(one) for one in target)
    return names


def include_closure(name: str) -> set[str]:
    """Every file reachable from `name` through `include:`, TRANSITIVELY, name excluded.

    Transitive because one level is not the property. docker-compose.web.yml could
    include an armed overlay tomorrow and a one-level check over
    docker-compose.yml would read clean while every `docker compose up` on the host
    armed a chain.

    Cycle-safe: compose would reject a cycle itself, but a test that hangs on a
    malformed file reports "the suite stopped" instead of "this file is wrong".
    """
    seen: set[str] = set()
    pending = [name]
    while pending:
        current = pending.pop()
        for child in included_by(REPO_ROOT / current):
            if child not in seen:
                seen.add(child)
                pending.append(child)
    return seen


def default_project_services() -> dict[str, dict]:
    """{service: definition} for a bare `docker compose up`, profiles excluded.

    THE RULE COMPOSE APPLIES, and the reason this is three lines rather than a
    parse of one file: a service runs by default if and only if it declares no
    `profiles:` key, or declares an empty one. Everything else needs `--profile` or
    to be named. So the answer spans docker-compose.yml plus its include closure,
    and no single file holds it.
    """
    services: dict[str, dict] = {}
    for name in ["docker-compose.yml", *sorted(include_closure("docker-compose.yml"))]:
        spec = load_compose(REPO_ROOT / name)
        for service, declared in (spec.get("services") or {}).items():
            body = declared or {}
            if not body.get("profiles"):
                services[service] = body
    return services


def profiled_services() -> dict[str, list[str]]:
    """{service: its profiles} for every service in the default project's files."""
    found: dict[str, list[str]] = {}
    for name in ["docker-compose.yml", *sorted(include_closure("docker-compose.yml"))]:
        spec = load_compose(REPO_ROOT / name)
        for service, body in (spec.get("services") or {}).items():
            profiles = (body or {}).get("profiles") or []
            if profiles:
                found[service] = list(profiles)
    return found


# ---------------------------------------------------------------------------
# WHAT A BARE `docker compose up` STARTS
# ---------------------------------------------------------------------------


def test_the_base_file_includes_the_icp_and_web_files():
    """The declaration itself, and that both targets are on disk.

    BOTH HALVES, because a name that resolves to nothing is the defect this
    repository has already paid for: docker-compose.yml's header told the operator
    to pass `-f docker-compose.stateful.yml` for three days after that file stopped
    existing, and docker exits non-zero on the first file it cannot open before it
    reads a single service. An `include:` of a missing file fails the same way, one
    layer less visibly, because nobody typed the name.
    """
    declared = included_by(REPO_ROOT / "docker-compose.yml")
    assert declared == ["docker-compose.icp.yml", "docker-compose.web.yml"], (
        f"docker-compose.yml's include: is {declared}. A bare `docker compose up` reads "
        f"only this file and whatever it includes, so these two names are what makes the "
        f"default project the terminal rather than the Node bridge and the test harness."
    )
    for name in declared:
        assert (REPO_ROOT / name).is_file(), (
            f"docker-compose.yml includes {name}, which is not on disk under {REPO_ROOT}. "
            f"Present: {sorted(p.name for p in compose_files())}"
        )


def test_a_bare_up_starts_exactly_the_terminal():
    """`web` and `icp-replica`, and nothing else. The whole point of the change.

    THE EXACT SET IN BOTH DIRECTIONS. Asserting only that `web` is present would
    pass on the tree as it was measured on 2026-10-10, where `abstergo` and
    `harness` came up as well -- and the harness brings regtest bitcoind and
    litecoind up on a host whose other daemons hold funded testnet wallets.
    """
    started = set(default_project_services())
    assert started == TERMINAL_SERVICES, (
        f"a bare `docker compose up` would start {sorted(started)}, and the terminal is "
        f"{sorted(TERMINAL_SERVICES)}.\n"
        f"missing: {sorted(TERMINAL_SERVICES - started) or '(none)'}\n"
        f"extra:   {sorted(started - TERMINAL_SERVICES) or '(none)'}\n"
        "A service joins this set by being in docker-compose.yml or in a file it "
        "includes, with no `profiles:` key. Give anything that is not the terminal a "
        "profile of its own name rather than relying on nobody typing a bare `up`."
    )


@pytest.mark.parametrize("service", ["abstergo", "harness"])
def test_the_two_services_that_are_not_the_terminal_are_behind_a_profile(service):
    """Opt-in, by a profile, named after the service.

    MEASURED 2026-10-07 and worked around in the wrong place: swap_stack.py's
    UP_SERVICES comment records that neither of these carried a `profiles:` key, so
    the first `swap_stack.py up` would have built and started a TEST HARNESS on a
    host holding real testnet wallets, plus an exchange container nothing in this
    stack talks to. Naming the services in `up` fixed the one command that read
    that constant. It did nothing for `docker compose up`, which is the command the
    operator actually types -- rule 19's test for a patch: it stopped the symptom
    for one caller and left the cause in place for every other.
    """
    profiles = profiled_services()
    assert service in profiles, (
        f"{service} declares no `profiles:`, so a bare `docker compose up` starts it. "
        f"That is how a regtest harness comes up on a host with funded wallets on it."
    )
    assert profiles[service] == [service], (
        f"{service}'s profiles are {profiles[service]}. The convention is ONE profile "
        f"per service, named after the service: an operator who knows the service name "
        f"knows the profile, the two cannot drift, and arming or starting one of these "
        f"never drags the other along -- which is the same reason the three armed "
        f"overlays are three files rather than one."
    )
    assert service not in TERMINAL_SERVICES, (
        f"{service} is in TERMINAL_SERVICES and also profiled, which cannot both be right"
    )


def test_no_profile_name_collides_with_a_service_in_the_default_set():
    """`--profile web` must not be a thing anybody can type and expect something.

    A profile sharing a name with a service that always runs is a command-line
    token with two readings, and the operator finds out which one compose picked by
    watching what starts.
    """
    for service, profiles in profiled_services().items():
        overlap = set(profiles) & TERMINAL_SERVICES
        assert not overlap, (
            f"{service} declares profile(s) {sorted(overlap)}, which are also the names of "
            f"services that start by default. Pick a name that reads as one thing."
        )


# ---------------------------------------------------------------------------
# WHAT `include:` MUST NEVER REACH -- the custody half
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("overlay", sorted(NEVER_INCLUDED))
def test_no_compose_file_includes_an_armed_or_alternative_overlay(overlay):
    """Arming is an act. `include:` would make it a default.

    THE THREE armed-* FILES ARE THE ENTRIES THAT MATTER. Each one mounts or passes
    the key that lets the web container sign and broadcast on one chain, and
    docker-compose.web.armed-sol.yml's own header states the principle: "compose
    only reads it when it is named with -f". An `include:` would delete that
    sentence's meaning -- every `docker compose up` on the host would arm the chain,
    with no act by anybody and nothing in shell history to show it (rule 16: arming
    is live posture and the operator's decision).

    hostnet and grc-desk are here for weaker reasons that are still reasons:
    hostnet REPLACES the network mode rather than adding to it, and grc-desk
    requires GRC_DESK_DATADIR, which including would make mandatory for every
    command that reads docker-compose.yml.

    CHECKED FROM EVERY FILE, not only from docker-compose.yml, because the closure
    is what compose resolves: an include two levels down arms the chain exactly as
    hard as one at the top.
    """
    assert (REPO_ROOT / overlay).is_file(), (
        f"{overlay} is not on disk; this gate names files that must stay unincluded, "
        f"so a rename has to be reflected here rather than passing by absence"
    )
    for compose in compose_files():
        reachable = include_closure(compose.name)
        assert overlay not in reachable, (
            f"{compose.name} reaches {overlay} through `include:` "
            f"(closure: {sorted(reachable)}). Remove it: an overlay is named on the "
            f"command line, and for the armed files that naming IS the custody decision."
        )


def test_the_default_project_names_no_signing_variable():
    """And the included files must not pass one either -- the same property, by value.

    THE ABOVE TEST IS ABOUT FILENAMES AND THIS IS ABOUT CONTENT, which is the gap a
    rename would walk through: copying an armed overlay's `environment:` line into
    docker-compose.web.yml arms the container without any file being included at
    all. docker-compose.web.yml's own header says "SIGNING MATERIAL IS ABSENT, ALL
    OF IT" and lists the names; this is that sentence as a check.

    THE NAMES ARE ASSERTED ABSENT, NOT THEIR VALUES READ. Nothing here opens `.env`,
    a keypair file or the process environment (CLAUDE.md's chain-safety rules), and
    a variable NAME is not a secret -- these five are printed in the compose headers
    and in supervisor.py's own status output.
    """
    signing = (
        "SOL_PAYOUT_KEYPAIR_PATH",
        "SOL_PAYOUT_KEYPAIR_HOST_FILE",
        "SOLANA_PAYER_KEYPAIR_PATH",
        "XRP_PAYOUT_SECRET_SEED",
        "ST_ADAPTOR_FUNDING_SEED",
        "GRIDCOIN_WALLET_PASSPHRASE",
    )
    for name in ["docker-compose.yml", *sorted(include_closure("docker-compose.yml"))]:
        body = (REPO_ROOT / name).read_text(encoding="utf-8")
        code = "\n".join(
            line for line in body.splitlines() if not line.strip().startswith("#")
        )
        for variable in signing:
            assert variable not in code, (
                f"{name} is in the default project and names {variable} outside a comment. "
                f"A bare `docker compose up` would then arm whatever that variable is set "
                f"to in whatever shell ran it -- armed by inheritance rather than by "
                f"decision, which is what the separate armed-* files exist to prevent."
            )


# ---------------------------------------------------------------------------
# NO FILE IS APPLIED TWICE -- the invariant that replaces a guess about compose
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "where,names",
    [
        ("swap_stack.COMPOSE_FILES", tuple(swap_stack.COMPOSE_FILES)),
        ("chains/icp._COMPOSE_FILES", tuple(p.name for p in icp_module._COMPOSE_FILES)),
    ],
)
def test_no_command_passes_a_file_that_include_already_supplies(where, names):
    """A `-f` for an included file would hand compose the same file twice.

    WHAT IS NOT KNOWN, and this gate exists because of it rather than in spite of
    it: whether compose accepts a resource arriving both through `include:` and
    through a later `-f`, errors on the conflict, or silently keeps one of the two.
    That was not measured -- running `docker` was forbidden where this change was
    made, `docker compose config` included -- so rule 17 forbids writing it down
    either way. What CAN be made true is that no command in this tree depends on
    the answer, and that is checkable without docker.

    BOTH CALL SITES, in one parametrize. swap_stack.py passed three files and
    chains/icp.py passed two; both were reduced to docker-compose.yml alone on
    2026-10-10, and they are the two places that build a `-f` list at all. A list
    that grows a second entry for a file `include:` supplies fails here rather than
    on the operator's host.
    """
    assert names, f"{where} is empty; a compose command with no -f reads nothing"
    for name in names:
        others = [other for other in names if other != name]
        for other in others:
            reachable = include_closure(other)
            assert name not in reachable, (
                f"{where} passes both {other} and {name}, and {other} already includes "
                f"{name} (closure: {sorted(reachable)}). Drop {name}: what compose makes "
                f"of one file arriving twice is not established anywhere in this tree."
            )


def test_the_files_every_command_passes_exist_on_disk():
    """Same property the old three-file gate had, now over one file per call site."""
    for where, names in (
        ("swap_stack.COMPOSE_FILES", tuple(swap_stack.COMPOSE_FILES)),
        ("chains/icp._COMPOSE_FILES", tuple(p.name for p in icp_module._COMPOSE_FILES)),
    ):
        missing = [name for name in names if not (REPO_ROOT / name).is_file()]
        assert not missing, (
            f"{where} names {missing}, not on disk under {REPO_ROOT}. docker exits "
            f"non-zero on the first `-f` it cannot open, before it reads any service."
        )


def test_up_services_is_exactly_the_default_project():
    """The named list and the compose files must agree, or one of them is a lie.

    swap_stack.py keeps naming its services rather than relying on the profiles
    (its UP_SERVICES comment says why: `up` is a CHOICE of deployment, and the
    banner prints a list rather than an inference). Keeping both means keeping two
    statements of one fact, which rule 8 calls a bug with a delay on it -- so this
    is the delay removed. Adding a service to the default project without adding it
    to `up`, or the reverse, fails here.
    """
    assert set(swap_stack.UP_SERVICES) == set(default_project_services()), (
        f"swap_stack.UP_SERVICES is {sorted(swap_stack.UP_SERVICES)} and a bare "
        f"`docker compose up` starts {sorted(default_project_services())}. One of the two "
        f"moved without the other, and the banner `up` prints names the stale one."
    )


@pytest.mark.parametrize("compose", [p.name for p in sorted(REPO_ROOT.glob("docker-compose*.yml"))])
def test_the_hand_parser_status_uses_agrees_with_the_real_yaml_parser(compose):
    """`swap_stack.py status` cannot import yaml, so it has a second parser. They must agree.

    MEASURED: PyYAML is in requirements-dev.txt and NOT in requirements.txt, whose
    entry says in its own words that it is there to parse compose files "for
    tests/test_docker_build_context.py". requirements.txt was built from an AST walk
    of every third-party top-level import under swap_terminal/, so yaml's absence
    from it is a measurement, not an omission -- and `swap_stack.py` is a tool an
    operator runs against a live stack, which must not die on a dev dependency.

    SO THERE ARE TWO PARSERS, which is rule 8's shape exactly: "two copies of one
    rule is not redundancy, it is a bug with a delay on it. The copies agree on the
    day they are written and drift from then on, and the drift is invisible." This
    is the delay removed -- not by deleting one, because each is correct for its
    caller, but by asserting the thing rule 8 says to assert when they genuinely
    differ: that they still answer the same question the same way.

    OVER EVERY COMPOSE FILE, including the seven that declare no `include:` at all.
    A parser that returned a filename for a file with no include block would be the
    over-reading direction, and for an armed overlay that is the worst available
    false positive.
    """
    path = REPO_ROOT / compose
    by_yaml = included_by(path)
    by_hand = include_entries(path.read_text(encoding="utf-8"))
    assert by_hand == by_yaml, (
        f"{compose}: stack_authority.include_entries() reads {by_hand} and the YAML parser "
        f"reads {by_yaml}. The hand parser labels a line in `swap_stack.py status`; the YAML "
        f"one backs the gates in this file. They have drifted, and the report is the half "
        f"that will be believed."
    )


# ---------------------------------------------------------------------------
# HAZARD 2 -- THE REQUIRED VARIABLE A BARE `up` NOW HITS
# ---------------------------------------------------------------------------


def test_the_default_project_requires_exactly_the_one_variable():
    """SWAP_DB_DIR, and nothing else. The bound on an accepted cost.

    `include:` MAKES REQUIRED VARIABLES TRANSITIVE. Compose interpolates every
    variable in every file it reads -- includes among them -- before it selects a
    service, so including docker-compose.web.yml makes SWAP_DB_DIR required for
    every command that reads docker-compose.yml, down to
    `up -d grc-desk`, a service that has nothing to do with the swap database.

    THAT IS THE 2026-10-05 DEFECT RETURNING THROUGH A DIFFERENT DOOR. It landed
    twice in one day then: four services in one file made `up -d abstergo` fail on
    two variables it does not use, and the fix -- one required variable per file --
    is what the eight files in this root are a consequence of.

    SO ONE IS THE ACCEPTED COST AND TWO IS THE DEFECT. The guard itself does not
    move: a default for SWAP_DB_DIR would be a compose file choosing which swap
    database the terminal opens, and `./runtime/web` would serve an EMPTY database
    that looks exactly like a working one with no swaps in it. A command that stops
    and names the variable is the only option that cannot silently point the
    terminal at the wrong money.
    """
    required: set[str] = set()
    for name in ["docker-compose.yml", *sorted(include_closure("docker-compose.yml"))]:
        required |= required_variables(REPO_ROOT / name)
    assert required == DEFAULT_PROJECT_REQUIRES, (
        f"the default project requires {sorted(required)}; the one accepted requirement is "
        f"{sorted(DEFAULT_PROJECT_REQUIRES)}.\n"
        "Every command that reads docker-compose.yml must supply ALL of these, even to "
        "start a service that uses none of them. If a new one arrived through `include:`, "
        "the fix is to stop including that file -- not to add it here, and not to give the "
        "variable a default."
    )


def test_the_required_variables_message_is_self_describing():
    """A bare `up` in a fresh checkout fails here, and the message is the whole UI.

    Rule 14: "pasted output has to be self-describing a day later, because it
    usually is read a day later." Compose prints this string with no file, no line
    number and no comment around it, to a reader who typed `docker compose up` and
    has opened nothing. The message it replaced ended "after running the stat -f
    check in the comment above", and "above" was only on the screen of someone who
    had typed `-f docker-compose.web.yml` themselves -- which, since `include:`, is
    nobody.

    A HARD FAILURE IS CORRECT HERE. This asserts that it does not READ like a bug.
    """
    text = (REPO_ROOT / "docker-compose.web.yml").read_text(encoding="utf-8")
    message = re.search(r"\$\{SWAP_DB_DIR:\?([^}]*)\}", text)
    assert message, "SWAP_DB_DIR is no longer declared with a `:?reason` message at all"
    said = message.group(1)
    for expected, why in (
        ("SWAP_DB_DIR", "the variable to set"),
        ("docker-compose.web.yml", "which file declares it, since compose names none"),
        ("docker compose up", "why they are seeing it after a command that named no file"),
        ("swap_terminal.db", "what the directory must hold"),
        ("stat -f", "the filesystem check that must happen BEFORE a WAL database is mounted"),
        ("virtiofs", "a filesystem on which WAL fails by corrupting rather than erroring"),
    ):
        assert expected in said, (
            f"the SWAP_DB_DIR message does not mention {expected!r} ({why}). "
            f"It reads: {said!r}"
        )
    assert "}" not in said, (
        "compose's `${VAR:?message}` ends at the first closing brace, so a `}` in the "
        "prose truncates the sentence and leaves the rest in the YAML as garbage"
    )


# ---------------------------------------------------------------------------
# HAZARD 1 -- `docker compose down` IS NOW ONE SHORT COMMAND, AND IT REMOVES
# CONTAINERS
# ---------------------------------------------------------------------------


def test_the_replica_state_is_on_a_named_volume_that_a_down_keeps():
    """The one mechanical thing standing between a bare `down` and a destroyed ledger.

    WHAT THIS CHANGE DID: it made `docker compose down` reach the whole terminal in
    one command with no flags. `down` REMOVES containers, and on 2026-10-07 that
    took the ICP ledger canister holding 1000 LICP with it, because the replica's
    dfx state was on the container's WRITABLE LAYER. 2026-10-08 cost three
    canisters and ~998.9498 LICP the same way, through an image rebuild.

    WHAT MAKES IT SURVIVABLE NOW: 28de99c mounted
    `icp-replica-data:/root/.local/share/dfx` -- a NAMED volume over the directory
    an inspection inside the running container measured at 180M holding
    `network/local/<hash>/state`. `docker compose down` without `-v` removes
    containers and keeps named volumes.

    SO THIS ASSERTS THE THREE THINGS THAT HAVE TO HOLD TOGETHER: the state path is
    mounted, its source is a NAMED volume and not a path on the container's layer,
    and that volume is DECLARED at the top level of the same file -- an undeclared
    name is an error compose raises rather than a volume it invents, and the error
    would arrive at `up` time with the ledger already gone.

    WHAT IT DOES NOT ESTABLISH, and the distinction is the whole of rule 17: that
    the replica's tECDSA key material lives under this path. The measurement that
    showed the SAME canister id with the SAME `dfx_test_key` producing two
    DIFFERENT public keys across one recreation was taken 2026-10-07 at 15:58 UTC,
    about three hours BEFORE this volume existed, so it describes a topology that
    is gone -- and nothing has re-measured it. A reason to believe the key now
    survives is not a reading of it. docker-compose.yml's hazard 1 carries the
    three-command check that would settle it, and OPEN_FINDINGS.md carries it as
    open work.
    """
    spec = load_compose(REPO_ROOT / "docker-compose.icp.yml")
    service = (spec.get("services") or {}).get("icp-replica") or {}
    sources = {}
    for mount in service.get("volumes") or []:
        if isinstance(mount, str) and ":" in mount:
            source, destination = mount.rsplit(":", 1)
            sources[destination] = source
        elif isinstance(mount, dict):
            sources[str(mount.get("target"))] = str(mount.get("source"))

    assert REPLICA_STATE_PATH in sources, (
        f"nothing is mounted at {REPLICA_STATE_PATH} in docker-compose.icp.yml; its mount "
        f"destinations are {sorted(sources)}. With no durable mount there, the replica's "
        f"canister state is on the container's writable layer, and `docker compose down` "
        f"-- now one flagless command away from the whole terminal -- destroys it."
    )
    source = sources[REPLICA_STATE_PATH]
    assert not source.startswith((".", "/", "$")), (
        f"{REPLICA_STATE_PATH} is mounted from {source!r}, which is a host path or an "
        f"interpolated value rather than a named volume. A bind survives a `down` too, so "
        f"this is not a correctness failure on its own -- but docker-compose.icp.yml's own "
        f"comment says why a bind is wrong here: it would put 180M of replica state under "
        f"the operator's checkout, where `git clean -xdf` destroys the ledger."
    )
    declared = spec.get("volumes") or {}
    assert source in declared, (
        f"{REPLICA_STATE_PATH} is mounted from the volume {source!r}, which "
        f"docker-compose.icp.yml does not declare at the top level (it declares "
        f"{sorted(declared)}). Compose raises an error for an undeclared volume rather "
        f"than creating one, and that error arrives at `up` time -- after the `down`."
    )
