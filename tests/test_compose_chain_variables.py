"""The compose files must pass the chain variables config.py actually reads.

Role: code hygiene (read-only)
Reads: swap_terminal/config.py and every docker-compose*.yml, as text
Writes: nothing
Can move funds: no
Mainnet-safe: yes

A CLEAN GATE, not a baseline (CLAUDE.md rule 19).

WHY THIS EXISTS: THE SAME MISSPELLING, THREE TIMES IN ONE DAY.

swap_terminal/gridcoin_credentials.py exists because ONE Gridcoin RPC password is
read under four different spellings in this tree, and its docstring names the
symptom precisely: "an HTTP 401 that reads as 'the wallet is broken' rather than
'you spelled the variable differently over here'".

  2026-10-05, first    an acceptance block used GRC_RPC_PASSWORD. The real name
                       is GRC_RPC_PASS. A fifth spelling, invented on the spot.
  2026-10-05, second   docker-compose.web.yml passed BTC_RPC_PASSWORD,
                       LTC_RPC_PASSWORD and GRC_RPC_PASSWORD -- the same error
                       for three chains at once.
  2026-10-05, third    the same file passed no *_RPC_WALLET at all, which is how
                       the BTC and LTC adapters find wallet=desk_hot.

The second one is the measurement that matters. All three daemons were REACHABLE
from inside the container and every credential was exported in the operator's
shell, and the server still said:

    BTC  {'adapter_built': False, 'missing_settings': ['BTC_RPC_PASS']}
    GRC  {'adapter_built': False, 'missing_settings': ['GRC_RPC_PASS']}
    LTC  {'adapter_built': False, 'missing_settings': ['LTC_RPC_PASS']}

so the UI read "0 of 20 directions can be quoted right now" with nothing actually
wrong but a name. A MISSPELLED PASS-THROUGH IS INDISTINGUISHABLE FROM AN
UNCONFIGURED CHAIN, because the value arrives empty either way, and config.py's
fail-quiet-for-an-unconfigured-chain behavior is correct and must stay.

The *_RPC_WALLET omission is the worse shape of the two and would not have shown
up as "0 of 20". An unconfigured chain fails loudly; a chain configured against
the DEFAULT wallet instead of desk_hot builds an adapter, quotes, watches the
wrong wallet for deposits and pays out of it.

WHAT IT ASSERTS, in both directions, over the chain-credential family only:

  - every chain variable config.py reads is passed by the web compose file
  - every chain-shaped variable that compose passes is one config.py reads, so a
    plausible-looking name that nothing consumes cannot sit there looking
    configured

Compose legitimately passes plenty that config.py never reads -- SWAP_DB_PATH,
GUNICORN_WORKERS, ST_WORKER_RUN_DIR -- so the second direction is scoped to the
family by pattern rather than asserted over everything.

PARSED FROM TEXT, and this is the one place that is right rather than lazy:
config.py reads its environment AT IMPORT (its own docstring says so, because
`Config` is a class body), so importing it to ask what it reads would bake in
this process's environment and answer a different question. The names are what
is being checked, and the names are in the source.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PY = REPO_ROOT / "swap_terminal" / "config.py"
WEB_COMPOSE = REPO_ROOT / "docker-compose.web.yml"

#: config.py's own accessors. Matched by name so a new `_env_*` helper is included
#: without this pattern being touched.
_ENV_READ = re.compile(r'_env(?:_int|_float|_bool|_str)?\(\s*"([A-Z_][A-Z0-9_]*)"')

#: A compose `environment:` key, at the service's own indentation.
_COMPOSE_KEY = re.compile(r"^      ([A-Z_][A-Z0-9_]*):", re.MULTILINE)

#: What counts as a CHAIN CREDENTIAL. Kept for the reverse direction only -- a
#: chain-shaped name compose passes that config.py never reads.
_CHAIN_SHAPED = re.compile(r"_RPC_|_HOT_WALLET$")

#: config.py reads NO signing material, VERIFIED 2026-10-05: SOL_PAYOUT_KEYPAIR_PATH,
#: XRP_PAYOUT_SECRET_SEED, GRIDCOIN_WALLET_PASSPHRASE and ST_ADAPTOR_FUNDING_SEED all
#: live in services/ and none appears in config.py. That is what makes the forward
#: assertion below safe to state as EVERYTHING config.py reads, with no allowlist:
#: complete coverage of config.py arms nothing, so there is no tension between the
#: gate and the unarmed posture the compose files deliberately keep.
#:
#: If a future config.py starts reading a signing variable, this list is the thing to
#: revisit -- and the test below will start demanding compose pass it, which is the
#: loud failure you want rather than a quiet arming.
_SIGNING_VARIABLES = (
    "SOL_PAYOUT_KEYPAIR_PATH",
    "XRP_PAYOUT_SECRET_SEED",
    "GRIDCOIN_WALLET_PASSPHRASE",
    "ST_ADAPTOR_FUNDING_SEED",
)


def config_reads() -> set[str]:
    """EVERY name config.py reads through _env(), not just the chain family.

    THE NARROW VERSION OF THIS FUNCTION IS WHY THE GATE AGREED WITH THE DEFECT.
    It filtered on `_RPC_|_HOT_WALLET`, which is the same wrong idea the compose
    file had -- so both missed the same NINETEEN variables and the suite was green.
    A check derived from the same assumption as the thing it checks will always
    agree with it (rule 8), and that is worse than no check because it certifies.

    Measured 2026-10-05: config.py reads 44 variables; the compose file passed 30
    of them under the narrow rule, and the nineteen it did not include
    SOL_DEPOSIT_ACCOUNT and XRP_DEPOSIT_ACCOUNT (why SOL and XRP read UNAVAILABLE
    as a SOURCE), every *_NETWORK_FEE_RESERVE (the `cannot_quote` condition that
    pair_view.py's own docstring records as having badged GRC -> XRP AVAILABLE for
    a quote that then refused), every *_MIN_CONFIRMATIONS, and DEFAULT_FEE_BPS,
    AMOUNT_TOLERANCE_PCT and SMALL_SWAP_MANUAL_REVIEW_USD -- which decide what a
    customer is quoted and when a swap is held for a human.
    """
    return set(_ENV_READ.findall(CONFIG_PY.read_text()))


def compose_passes() -> set[str]:
    return set(_COMPOSE_KEY.findall(WEB_COMPOSE.read_text()))


def test_the_parse_found_something():
    """Otherwise both assertions below pass by comparing two empty sets (rule 17)."""
    reads = config_reads()
    passes = compose_passes()
    assert len(reads) >= 40, (
        f"only {len(reads)} variables parsed out of {CONFIG_PY.name}: {sorted(reads)}. "
        f"Either the _env() pattern no longer matches or the file was restructured, and "
        f"this gate is now checking nothing."
    )
    assert len(passes) >= 10, (
        f"only {len(passes)} environment keys parsed out of {WEB_COMPOSE.name}. The "
        f"indentation this pattern depends on may have changed."
    )


def test_config_py_reads_no_signing_material():
    """The premise the forward assertion rests on, checked rather than assumed.

    If this fails, "compose must pass everything config.py reads" has become a rule
    that would ARM the container, and the next test must gain an explicit allowlist
    before it is obeyed. Stated as its own test so that change is forced to be
    deliberate instead of arriving as a mechanical fix to a failing assertion.
    """
    reads = config_reads()
    armed = sorted(name for name in _SIGNING_VARIABLES if name in reads)
    assert not armed, (
        f"config.py now reads signing material: {armed}. The next test requires compose "
        f"to pass everything config.py reads, which would arm the container. Add an "
        f"explicit, justified exclusion there -- do not just make this pass."
    )


def test_every_variable_config_reads_is_passed_to_the_container():
    """EVERYTHING config.py reads, which is the assertion the narrow version missed.

    The *_RPC_PASS misspelling produced "0 of 20 directions can be quoted right
    now" -- loud. The nineteen this now covers are mostly the quiet kind: a
    container quoting at a different DEFAULT_FEE_BPS or holding at a different
    AMOUNT_TOLERANCE_PCT than the host it is meant to replace, with nothing
    anywhere saying the two disagree.
    """
    missing = sorted(config_reads() - compose_passes())
    assert not missing, (
        f"config.py reads these and {WEB_COMPOSE.name} does not pass them:\n  "
        + "\n  ".join(missing)
        + "\n\nInside the container each arrives UNSET and config.py falls back to its"
        "\nbuilt-in default, so the container runs on settings the host does not use."
        "\nThe loud failures are the chain credentials -- no adapter, pair unavailable,"
        "\n'0 of 20'. The QUIET ones are worse: a *_RPC_WALLET left out builds an adapter"
        "\nagainst the daemon's default wallet instead of desk_hot, and a DEFAULT_FEE_BPS"
        "\nor AMOUNT_TOLERANCE_PCT left out quotes and holds at a different number than"
        "\nthe host, with nothing reporting the difference."
    )


def test_every_chain_shaped_variable_compose_passes_is_one_config_reads():
    """The direction that CAUSED it: BTC_RPC_PASSWORD, a name nothing consumes.

    A plausible-looking pass-through that no reader exists for is worse than an
    omission, because it makes the file look complete. Nothing reports it: compose
    sets it, the process carries it, and no code ever asks.
    """
    unread = sorted(name for name in compose_passes() - config_reads() if _CHAIN_SHAPED.search(name))
    assert not unread, (
        f"{WEB_COMPOSE.name} passes these chain-shaped variables and config.py reads none "
        f"of them:\n  " + "\n  ".join(unread)
        + "\n\nCheck the spelling against config.py. *_RPC_PASS and not *_RPC_PASSWORD was"
        "\nthe 2026-10-05 defect, and swap_terminal/gridcoin_credentials.py documents four"
        "\nspellings of that one secret already in this tree."
    )


@pytest.mark.parametrize("asset", ["BTC", "LTC", "GRC"])
def test_each_rpc_chain_gets_its_wallet_variable(asset):
    """*_RPC_WALLET by name, because its absence does not look like a failure.

    Parametrized per asset rather than asserted as a set, so a failure names the
    chain whose deposits would be watched on the wrong wallet.
    """
    name = f"{asset}_RPC_WALLET"
    assert name in config_reads(), (
        f"{name} is no longer read by config.py; this test is pinning a variable that "
        f"does not exist and should be deleted with it (rule 2)"
    )
    assert name in compose_passes(), (
        f"{WEB_COMPOSE.name} does not pass {name}. The host deployment runs {asset} "
        f"against wallet=desk_hot; without this the container's adapter talks to the "
        f"daemon's DEFAULT wallet, builds successfully, and then watches the wrong wallet "
        f"for deposits and pays out of it."
    )


def test_no_chain_endpoint_defaults_to_the_CONTAINERS_own_loopback():
    """A `${VAR:-}` whose config.py default is 127.0.0.1 is a guaranteed failure inside a container.

    MEASURED 2026-10-07 from the customer UI, after the stack moved into docker. An
    ICP->GRC quote priced correctly and create_swap refused:

        could not validate address with GRC daemon at 127.0.0.1:25779 ...
        [Errno 111] Connection refused

    config.py defaults BTC_RPC_HOST, LTC_RPC_HOST and GRC_RPC_HOST to 127.0.0.1,
    which is right on the host and is the CONTAINER inside a container. The compose
    file passed all three through empty, so "unset" did not mean "use the default" --
    it meant "point at a daemon that is not here".

    What makes this worth a gate rather than a one-line fix: the same compose file
    already carried a comment, eighty lines above, spelling the exact export an
    operator needed. It was correct and it was not enough. A file that documents a
    value it could supply makes the operator do by hand what it already knows -- and
    nothing failed until a customer-facing create_swap did.

    The property is derived from config.py rather than from a list of variable names
    typed here: any endpoint variable whose fallback is loopback must not reach the
    container empty. A seventh chain is covered the day it is added.
    """
    compose = (Path(__file__).resolve().parents[1] / "docker-compose.web.yml").read_text()
    config_source = (Path(__file__).resolve().parents[1] / "swap_terminal" / "config.py").read_text()

    loopback_defaults = set(
        re.findall(r'_env\(\s*"([A-Z0-9_]+)"\s*,\s*"127\.0\.0\.1"\s*\)', config_source)
    )
    assert loopback_defaults, (
        "no variable in config.py defaults to 127.0.0.1 any more, so this gate has no subject -- "
        "delete it rather than leaving a test that passes by measuring nothing (rule 19)"
    )

    passed_empty = sorted(
        name for name in loopback_defaults
        if f'{name}: "${{{name}:-}}"' in compose
    )
    # AND THE HOST'S OWN VARIABLE MUST NOT REACH THE CONTAINER AT ALL, which is the
    # half d5168a0 missed. Defaulting to host.docker.internal only helps when the
    # variable is UNSET, and the operator's .env sets GRC_RPC_HOST=127.0.0.1 --
    # correctly, since that IS right for the host deployment. The explicit value won
    # and the container pointed at itself; measured 2026-10-07 when create_swap said
    # "could not validate address with GRC daemon at 127.0.0.1:25779".
    #
    # One name whose correct value depends on where the process runs is rule 8's
    # collision in an environment variable, so the container reads a DIFFERENT name.
    inherits_the_hosts_value = sorted(
        name for name in loopback_defaults
        if f'{name}: "${{{name}:-' in compose
    )
    assert not inherits_the_hosts_value, (
        f"{inherits_the_hosts_value} are passed to the container from the SAME variable the host "
        f"uses, so a .env that correctly sets 127.0.0.1 for the host makes the container point at "
        f"itself -- a default cannot override an explicitly set value. Read CONTAINER_<NAME> instead."
    )
    assert not passed_empty, (
        f"{passed_empty} default to 127.0.0.1 in config.py and reach the container empty, so each "
        f"one points the container at its own loopback. Default them in docker-compose.web.yml to "
        f"host.docker.internal (the extra_hosts entry already resolves it), or to the compose "
        f"service name if the daemon moves into a container."
    )


def test_the_HOSTNET_overlay_points_ICP_at_LOOPBACK_not_at_the_service_name():
    """Under `network_mode: host` a compose service name resolves to nothing.

    docker-compose.web.hostnet.yml exists because the chain daemons live on the host
    and the docker bridge cannot reach them -- measured 2026-10-05 and recorded in that
    file's header: host.docker.internal resolved to 172.17.0.1 and all four daemons
    still timed out, because traffic from the bridge is DROPPED by the default firewall
    posture, which is a different failure from refused.

    That file predates ICP. Under the bridge, ICP_DFX_NETWORK_URL is
    http://icp-replica:4943 and correct, because both services share the default
    compose network. Under host networking the container is not on that network at all,
    so the service name is unresolvable and the one chain that is NOT on the host would
    be the only one the overlay left broken.

    The replica publishes 127.0.0.1:4943, and under host networking that loopback is
    the host's own -- the same substitution that makes the chain daemons work.
    """
    overlay = (Path(__file__).resolve().parents[1] / "docker-compose.web.hostnet.yml").read_text()

    assert "network_mode: host" in overlay, (
        "this overlay no longer uses host networking, so what this test pins has moved"
    )
    assert "ICP_DFX_NETWORK_URL" in overlay, (
        "the hostnet overlay does not override ICP_DFX_NETWORK_URL, so it inherits "
        "http://icp-replica:4943 from docker-compose.web.yml -- a service name that "
        "resolves to nothing outside the compose network"
    )
    icp_line = next(line for line in overlay.splitlines() if "ICP_DFX_NETWORK_URL:" in line)
    assert "127.0.0.1" in icp_line, f"the override must be loopback, not a service name: {icp_line.strip()}"
    assert "icp-replica" not in icp_line, (
        f"the overlay still names the compose service, which does not resolve here: {icp_line.strip()}"
    )
