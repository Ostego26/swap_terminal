"""swap_terminal/architecture.py must agree with the compose files and the code.

Role: submodule (tests)
Reads: every docker-compose*.yml, swap_terminal/architecture.py, the three payout
       credential modules, chains/registry.py and icp/dfx.json
Writes: nothing
Can move funds: no. Nothing here constructs an adapter that sends, opens a socket, or
       reads a credential -- it compares VARIABLE NAMES against the modules that
       declare them
Mainnet-safe: yes, and network-independent

WHY IT EXISTS. architecture.py is a description of this system's structure, and a
description of structure is the single most reliable thing in a repository to go stale
-- the same session that wrote it corrected five such descriptions, every one of them
authored by someone who had just measured the thing they got wrong:

    docker-compose.web.yml   stop_grace_period's justifying comment, off by 3x on its
                             own arithmetic
    docker-compose.yml       "exactly two variables carry a default" where six do
    address_authority.py     "SOL is NOT in Config.ALLOWED_PAIRS" -- a destination on
                             five pairs since 2026-10-06
    armed-icp.yml            "no payout has ever completed for ICP", hours after the
                             call site was fixed
    docker-compose.yml       "COMPOSE_FILE lives in a file this repository must never
                             read or write", after configure_env.py started writing it

So architecture.py is worth exactly as much as this file, and not a line more. Every
assertion below DERIVES the fact from its source rather than restating it: a reader who
trusts architecture.py is trusting this test, which is the only kind of trust a
document about structure can earn.

NO ASSERTION HERE READS A COMMENT. That is the discipline the five failures above all
broke, and CLAUDE.md states it: "Never accept 'the code contains a check for X' as
evidence X is enforced."
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest
import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "swap_terminal"))

import architecture  # noqa: E402


class _ComposeLoader(yaml.SafeLoader):
    """SafeLoader that tolerates compose's own !reset / !override tags."""


_ComposeLoader.add_multi_constructor("!", lambda _l, _s, node: node)


def load_compose(path: Path) -> dict:
    """Parse one compose file. Returns {} for an empty one."""
    return yaml.load(path.read_text(), Loader=_ComposeLoader) or {}  # noqa: S506 -- _ComposeLoader derives from SafeLoader; the only addition returns the plain node for compose's own tags


def compose_files() -> list[Path]:
    """Every compose file in the repository root, which is where they all live."""
    return sorted(REPOSITORY_ROOT.glob("docker-compose*.yml"))


def declared_services() -> dict[str, str]:
    """{service: the file that DECLARES it}, where declaring means carrying build or image.

    THE DISCRIMINATOR IS `build`/`image` AND NOT THE FILENAME. The armed overlays and
    hostnet all have a `services: web:` block, so a filename-based or key-based scan
    would report five declarations of `web`. What separates them is that an overlay
    patches environment and volumes onto a service someone else built.
    """
    found: dict[str, str] = {}
    for path in compose_files():
        for name, body in (load_compose(path).get("services") or {}).items():
            if "build" in (body or {}) or "image" in (body or {}):
                assert name not in found, (
                    f"{name} is declared in both {found[name]} and {path.name}; this test's "
                    f"build/image discriminator no longer separates declarations from overlays"
                )
                found[name] = path.name
    return found


def overlay_files() -> set[str]:
    """Compose files that only PATCH services, declaring none of their own."""
    overlays = set()
    for path in compose_files():
        services = load_compose(path).get("services") or {}
        if services and not any("build" in (b or {}) or "image" in (b or {}) for b in services.values()):
            overlays.add(path.name)
    return overlays


def included_files() -> list[str]:
    """What docker-compose.yml pulls in with `include:`, transitively."""
    seen: list[str] = []
    queue = ["docker-compose.yml"]
    while queue:
        current = queue.pop(0)
        for entry in load_compose(REPOSITORY_ROOT / current).get("include") or []:
            name = entry if isinstance(entry, str) else (entry or {}).get("path", "")
            if name and name not in seen:
                seen.append(name)
                queue.append(name)
    return seen


def test_the_service_set_is_the_one_the_compose_files_declare():
    """architecture.SERVICES must name every declared service and no others.

    MUTATION: delete the grc-desk entry from SERVICES and this fails naming it. Add a
    service to any compose file without recording it and this fails the other way,
    which is the direction that matters -- a service nobody wrote down is a service
    nobody reasons about.
    """
    declared = declared_services()
    assert set(architecture.SERVICES) == set(declared), (
        f"architecture.SERVICES names {sorted(architecture.SERVICES)} and the compose files "
        f"declare {sorted(declared)}"
    )


def test_each_service_records_the_file_that_declares_it():
    """`declared_in` must be the file carrying that service's build:, not a guess."""
    declared = declared_services()
    wrong = {
        name: (entry.declared_in, declared[name])
        for name, entry in architecture.SERVICES.items()
        if entry.declared_in != declared[name]
    }
    assert wrong == {}, f"recorded vs actual declaring file: {wrong}"


def test_each_services_profile_is_the_one_compose_gates_it_with():
    """A profile is what makes a service opt-in, so a wrong one inverts the whole claim."""
    actual = {}
    for path in compose_files():
        for name, body in (load_compose(path).get("services") or {}).items():
            profiles = (body or {}).get("profiles") or []
            if profiles:
                actual[name] = profiles[0]
    recorded = {name: e.profile for name, e in architecture.SERVICES.items() if e.profile}
    assert recorded == actual, f"recorded profiles {recorded} != compose's {actual}"


def test_starts_by_default_is_derived_and_not_asserted():
    """`starts_by_default` must equal: declared in a file a bare `up` reads, AND no profile.

    THIS IS THE ONE THE OPERATOR ASKED FOR. "What does `docker compose up` actually
    start" took a mid-conversation read of `include:` and `profiles:` from scratch on
    2026-10-11, which is the fourth time the deployment shape had to be re-derived. It
    is derived here rather than recorded, and architecture.py's flag is checked against
    the derivation.

    A BARE `up` READS docker-compose.yml PLUS WHAT IT include:s, and nothing else. The
    COMPOSE_FILE line in .env can add more -- that is how the armed overlays are
    reached and why a fresh checkout is armed for GRC -- but it adds OVERLAYS of `web`
    rather than new services, so it cannot change this answer. That is asserted
    separately below.
    """
    readable = {"docker-compose.yml", *included_files()}
    declared = declared_services()
    for name, entry in architecture.SERVICES.items():
        derived = declared[name] in readable and not entry.profile
        assert entry.starts_by_default == derived, (
            f"{name}: recorded starts_by_default={entry.starts_by_default}, derived {derived} "
            f"(declared in {declared[name]}, which a bare `up` "
            f"{'reads' if declared[name] in readable else 'does NOT read'}; "
            f"profile {entry.profile or '(none)'})"
        )


def test_the_include_list_is_what_docker_compose_yml_declares():
    """architecture.INCLUDED must be compose's own include:, transitively."""
    assert list(architecture.INCLUDED) == included_files(), (
        f"recorded {list(architecture.INCLUDED)} != declared {included_files()}"
    )


def test_every_overlay_file_is_recorded_and_declares_no_service():
    """architecture.OVERLAYS must be exactly the files that patch without declaring.

    Without this an overlay added for a sixth chain would be invisible here while
    being, by construction, a file that arms something.
    """
    assert set(architecture.OVERLAYS) == overlay_files(), (
        f"recorded overlays {sorted(architecture.OVERLAYS)} != actual {sorted(overlay_files())}"
    )


def test_no_overlay_introduces_a_service_that_bypasses_the_default_set():
    """An overlay must only patch services the declared set already contains.

    This is what makes test_starts_by_default_is_derived_and_not_asserted complete: it
    reasons about `include:` alone, which is only sound while the COMPOSE_FILE route
    cannot introduce a service of its own.
    """
    declared = set(declared_services())
    for name in architecture.OVERLAYS:
        patched = set(load_compose(REPOSITORY_ROOT / name).get("services") or {})
        unknown = patched - declared
        assert unknown == set(), (
            f"{name} patches {sorted(unknown)}, which no compose file declares -- so the "
            f"COMPOSE_FILE route can introduce a service and the default-set derivation "
            f"above is no longer complete"
        )


def test_the_web_containers_process_count_is_derived_from_its_three_sources():
    """WEB_PROCESS_COUNT must equal what the Dockerfile, gunicorn and the supervisor say.

    WHY THIS TEST EXISTS, and it is the shortest-lived wrong comment in the repository.
    architecture.py's `processes` field carried "four processes, not one" for exactly
    one commit, beside a six-entry tuple describing eight processes -- in the module
    written because the architecture had been reconstructed from memory four times.
    Rule 16's wrong-comment bug, committed by the file whose job is to prevent it.

    SO THE COUNT IS DERIVED FROM THREE DIFFERENT PLACES rather than typed anywhere:
    raising GUNICORN_WORKERS, adding a fourth supervised worker, or changing the
    Dockerfile's entrypoint chain each fail this with the arithmetic in the message.

    ENTRIES AND PROCESSES ARE DIFFERENT NUMBERS and conflating them is what went wrong.
    The tuple has one entry per process GROUP -- its gunicorn line covers a master plus
    its children -- so the entry count is deliberately not asserted against the process
    count. Both are checked, separately.

    MUTATION: set WEB_PROCESS_COUNT to 6 (the entry count, which is the mistake this
    replaces) and it fails; set gunicorn.conf.py's workers to 4 and it fails too.
    """
    import re  # noqa: PLC0415 -- used only here

    dockerfile = (REPOSITORY_ROOT / "docker" / "web.Dockerfile").read_text()
    assert 'ENTRYPOINT ["dumb-init"' in dockerfile, (
        "web.Dockerfile's ENTRYPOINT is no longer dumb-init, so the process chain this "
        "count describes has changed shape"
    )
    assert "web_workers_entrypoint.py" in dockerfile, "the CMD no longer runs the entrypoint script"
    pid1_chain = 2  # dumb-init, then the entrypoint script it execs into

    gunicorn_conf = (REPOSITORY_ROOT / "gunicorn.conf.py").read_text()
    match = re.search(r'^workers\s*=\s*int\(os\.getenv\("GUNICORN_WORKERS",\s*"(\d+)"\)\)',
                      gunicorn_conf, re.MULTILINE)
    assert match, "could not read gunicorn's default worker count out of gunicorn.conf.py"
    gunicorn_processes = 1 + int(match.group(1))  # the master, plus its sync workers

    import supervisor  # noqa: PLC0415 -- after the sys.path insert at module scope

    swap_workers = len(supervisor.worker_commands())

    derived = pid1_chain + gunicorn_processes + swap_workers
    assert derived == architecture.WEB_PROCESS_COUNT, (
        f"architecture.WEB_PROCESS_COUNT is {architecture.WEB_PROCESS_COUNT} and the sources "
        f"give {derived}: {pid1_chain} for the dumb-init/entrypoint chain, "
        f"{gunicorn_processes} for gunicorn (1 master + {match.group(1)} workers), and "
        f"{swap_workers} supervised workers"
    )


def test_the_process_tuple_names_every_supervised_worker():
    """Separately from the count: each supervised worker must appear by name in the tuple.

    The count above would still pass if the tuple named the wrong three workers, and the
    tuple is what a reader actually consults. Derived from
    supervisor.worker_commands(), which is the registry the container really starts
    them from.
    """
    import supervisor  # noqa: PLC0415 -- after the sys.path insert at module scope

    listed = " ".join(architecture.SERVICES["web"].processes)
    missing = [name for name in supervisor.worker_commands() if name not in listed]
    assert missing == [], (
        f"{missing} are started in the web container and not named in "
        f"architecture.SERVICES['web'].processes"
    )


def test_the_custody_table_covers_exactly_the_assets_the_registry_can_build():
    """CUSTODY must name every chain an adapter exists for, and no others.

    Derived from chains/registry.ADAPTERS plus the per-chain branches rather than from a
    list here, so a seventh chain arrives as a red test (rule 11: one vocabulary, and
    every consumer follows automatically).
    """
    from chains.registry import build_adapters  # noqa: PLC0415 -- after the sys.path insert at module scope
    from test_every_adapter_names_its_own_asset import (  # noqa: PLC0415 -- one home for the all-six mapping
        FULLY_CONFIGURED,
    )

    buildable = set(build_adapters(FULLY_CONFIGURED))
    assert set(architecture.CUSTODY) == buildable, (
        f"architecture.CUSTODY names {sorted(architecture.CUSTODY)} and the registry can build "
        f"{sorted(buildable)}"
    )


@pytest.mark.parametrize("asset", sorted(architecture.CUSTODY))
def test_each_recorded_credential_variable_is_the_one_its_module_declares(asset):
    """The variable name must come from the module that reads it, not from this table.

    NAMES ONLY. Nothing here opens a keypair, a seed or a PEM -- the modules below
    expose the variable NAME as a constant precisely so a caller can report on presence
    without touching a value, and this test uses that same seam.
    """
    declared = {
        "SOL": ("chains.solana_payout_keypair", "KEYPAIR_PATH_VARIABLE"),
        "XRP": ("chains.xrp_payout_seed", "SIGNING_SEED_ENV_VAR"),
        "ICP": ("chains.icp_payout_identity", "PAYOUT_IDENTITY_VARIABLE"),
    }
    recorded = architecture.CUSTODY[asset].credential_variable
    if asset not in declared:
        # The daemon chains. GRC carries a passphrase variable for the wallet UNLOCK,
        # which is not signing material; BTC and LTC carry none at all.
        assert architecture.CUSTODY[asset].signer == "daemon", (
            f"{asset} has no credential module and is not recorded as daemon-signed"
        )
        return
    module_name, constant = declared[asset]
    module = __import__(module_name, fromlist=[constant])
    assert recorded == getattr(module, constant), (
        f"{asset}: architecture.py records {recorded!r}, {module_name}.{constant} is "
        f"{getattr(module, constant)!r}"
    )


@pytest.mark.parametrize("asset", sorted(architecture.CUSTODY))
def test_can_spend_from_matches_how_the_adapter_actually_decides_it(asset):
    """"class" means inherited from base.RPCAdapter; "derived" means assigned in __init__.

    Measured with an AST walk over the adapter module rather than by importing and
    looking at the value, because the value depends on the environment and the QUESTION
    is about where it comes from. A chain recorded as "class" that quietly started
    deriving -- or the reverse -- changes whether a missing credential shows up as an
    unpayable chain or as a payout that fails after the deposit is taken.
    """
    import ast  # noqa: PLC0415 -- used only by this test

    module_file = {
        "BTC": "bitcoin.py", "LTC": "litecoin.py", "GRC": "gridcoin.py",
        "SOL": "solana.py", "XRP": "xrp.py", "ICP": "icp.py",
    }[asset]
    tree = ast.parse((REPOSITORY_ROOT / "swap_terminal" / "chains" / module_file).read_text())
    assigns_per_instance = any(
        isinstance(node, ast.Attribute) and node.attr == "can_spend" and isinstance(node.ctx, ast.Store)
        for node in ast.walk(tree)
    )
    expected = "derived" if assigns_per_instance else "class"
    assert architecture.CUSTODY[asset].can_spend_from == expected, (
        f"{asset}: architecture.py records can_spend_from="
        f"{architecture.CUSTODY[asset].can_spend_from!r}, chains/{module_file} "
        f"{'assigns self.can_spend in __init__' if assigns_per_instance else 'does not assign it'}"
    )


def test_the_keyring_table_names_every_canister_dfx_would_deploy():
    """KEYRING must cover icp/dfx.json's canisters -- the rust pair AND the ledger."""
    dfx = json.loads((REPOSITORY_ROOT / "icp" / "dfx.json").read_text())
    assert set(architecture.KEYRING) == set(dfx.get("canisters") or {}), (
        f"architecture.KEYRING names {sorted(architecture.KEYRING)}, icp/dfx.json declares "
        f"{sorted(dfx.get('canisters') or {})}"
    )


def test_the_keyring_assets_are_the_curve_tables_own_assets():
    """KEYRING_ASSETS must equal modules/keyring_paths.CURVE_FOR_ASSET's keys.

    ICP's absence is the thing a reader looks for first, and it is load-bearing: a
    canister's ICP identity is its own principal, so there is no threshold key to
    derive. Asserted against the table rather than restated, so adding a sixth asset
    to the keyring cannot leave this file describing five.
    """
    from modules.keyring_paths import CURVE_FOR_ASSET  # noqa: PLC0415 -- after the sys.path insert at module scope

    assert set(architecture.KEYRING_ASSETS) == set(CURVE_FOR_ASSET), (
        f"architecture.KEYRING_ASSETS is {sorted(architecture.KEYRING_ASSETS)}, "
        f"CURVE_FOR_ASSET covers {sorted(CURVE_FOR_ASSET)}"
    )


def test_the_derivation_scheme_shape_matches_the_function_that_builds_it():
    """DERIVATION_SCHEME must have the arity and the literal prefix the real path does.

    The placeholders cannot be compared, so what is checked is everything that is NOT a
    placeholder: the length, and the two constant leading elements. A scheme that grew a
    sixth element or renamed its prefix would change every custody address derived from
    it, and this file would still be describing the old one.
    """
    from modules.keyring_paths import (  # noqa: PLC0415 -- after the sys.path insert at module scope
        TESTNET,
        derivation_path,
    )

    real = derivation_path("BTC", TESTNET)
    assert len(real) == len(architecture.DERIVATION_SCHEME), (
        f"derivation_path() returns {len(real)} elements, DERIVATION_SCHEME describes "
        f"{len(architecture.DERIVATION_SCHEME)}: {real!r}"
    )
    for index in (0, 1):
        assert real[index] == architecture.DERIVATION_SCHEME[index], (
            f"element {index}: derivation_path() gives {real[index]!r}, DERIVATION_SCHEME "
            f"says {architecture.DERIVATION_SCHEME[index]!r}"
        )


def test_the_gridcoin_passphrase_split_is_gone_from_the_code():
    """No CODE may read the historical second name any more. Prose may still discuss it.

    THIS TEST WAS THE INVERSE ONE COMMIT AGO and its own docstring predicted the flip:
    "if the list is EMPTY the split was fixed and the note should go." The split WAS
    fixed -- chains/wallet_lock.passphrase_env_var() is now the one resolver -- so the
    assertion is now that nothing reads GRC_WALLET_PASSPHRASE rather than that exactly
    one module does.

    PROSE IS STRIPPED BEFORE SCANNING, AND THAT IS THE WHOLE DIFFICULTY. The first
    version matched the literal anywhere in a file's text and therefore counted
    modules/atomic_grc_client.py (six mentions, all comments recording the history) and
    chains/wallet_lock.py (whose resolver docstring names the old variable to explain
    the collision) as readers. Both SHOULD name it: deleting the explanation to satisfy
    a grep makes the file worse for the reader in order to make a test green.

    code_without_prose() IS IMPORTED RATHER THAN COPIED (rule 8). It already exists for
    exactly this failure, and its docstring records the same bug on the same day against
    a different gate -- a file that named `conf_fallback_settings` in a comment
    explaining why it deliberately does NOT call it. That is five occurrences of
    prose-as-code in this repository; the sixth should also import this function.

    MUTATION: put `os.environ.get("GRC_WALLET_PASSPHRASE", "")` back into
    modules/atomic_grc_client.py's default argument and this fails naming that file.
    Add a comment mentioning the name anywhere and it still passes, which is the point.
    """
    from test_daemon_conf import (  # noqa: PLC0415 -- one home for the prose stripper
        code_without_prose,
    )

    name = architecture.GRC_PASSPHRASE_SECOND_NAME
    pattern = re.compile(rf"\b{re.escape(name)}\b")

    # architecture.py IS EXCLUDED, AND PROSE-STRIPPING CANNOT DO IT. The name is held
    # there as GRC_PASSPHRASE_SECOND_NAME = "GRC_WALLET_PASSPHRASE" -- a real assignment,
    # which is code by any definition, and recording the historical name is that
    # constant's entire job. So the recorder is excluded by PATH while every other file
    # is filtered by prose, and the two mechanisms are different on purpose rather than
    # one being a weaker version of the other.
    recorder = REPOSITORY_ROOT / "swap_terminal" / "architecture.py"
    readers = sorted(
        path.relative_to(REPOSITORY_ROOT).as_posix()
        for path in (REPOSITORY_ROOT / "swap_terminal").rglob("*.py")
        if "__pycache__" not in path.parts
        and path != recorder
        and pattern.search(code_without_prose(path))
    )
    assert readers == [], (
        f"{name} is read by CODE in {readers}. It is the historical name; "
        f"chains/wallet_lock.passphrase_env_var() is the one place that decides which "
        f"variable a chain's passphrase lives in, and a second reader reintroduces the "
        f"split where one suite asks for a name no deployment sets."
    )


def test_the_resolver_the_split_was_fixed_with_is_the_one_both_suites_use():
    """Both the HTLC driver and the HTLC client must go through passphrase_env_var().

    Guards the other direction from the test above: that one proves the OLD name is
    gone, this proves the NEW mechanism is actually what replaced it rather than each
    suite having independently hardcoded the configured name -- which would pass the
    absence check and leave two places to keep in step.
    """
    from test_daemon_conf import code_without_prose  # noqa: PLC0415 -- same single home

    for relative in ("atomic_swap.py", "swap_terminal/modules/atomic_grc_client.py"):
        code = code_without_prose(REPOSITORY_ROOT / relative)
        assert "passphrase_env_var" in code, (
            f"{relative} does not call chains/wallet_lock.passphrase_env_var(), so it is "
            f"deciding the passphrase variable's name for itself"
        )


