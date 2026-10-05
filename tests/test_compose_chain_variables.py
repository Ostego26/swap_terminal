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

#: What counts as a CHAIN CREDENTIAL, which is the family this gate covers.
#: Deliberately narrow: SWAP_DB_PATH and GUNICORN_WORKERS are not chain settings
#: and a gate that demanded symmetry over everything would be wrong rather than
#: strict.
_CHAIN_SHAPED = re.compile(r"_RPC_|_HOT_WALLET$")


def config_reads() -> set[str]:
    return {name for name in _ENV_READ.findall(CONFIG_PY.read_text()) if _CHAIN_SHAPED.search(name)}


def compose_passes() -> set[str]:
    return set(_COMPOSE_KEY.findall(WEB_COMPOSE.read_text()))


def test_the_parse_found_something():
    """Otherwise both assertions below pass by comparing two empty sets (rule 17)."""
    reads = config_reads()
    passes = compose_passes()
    assert len(reads) >= 10, (
        f"only {len(reads)} chain variables parsed out of {CONFIG_PY.name}: {sorted(reads)}. "
        f"Either the _env() pattern no longer matches or the file was restructured, and "
        f"this gate is now checking nothing."
    )
    assert len(passes) >= 10, (
        f"only {len(passes)} environment keys parsed out of {WEB_COMPOSE.name}. The "
        f"indentation this pattern depends on may have changed."
    )


def test_every_chain_variable_config_reads_is_passed_to_the_container():
    """The direction that produced "0 of 20 directions can be quoted right now"."""
    missing = sorted(config_reads() - compose_passes())
    assert not missing, (
        f"config.py reads these chain variables and {WEB_COMPOSE.name} does not pass them:\n  "
        + "\n  ".join(missing)
        + "\n\nInside the container each arrives UNSET, which config.py treats as an"
        "\nunconfigured chain -- so the adapter is not built, the pair reports unavailable,"
        "\nand the UI says 0 of 20 with every daemon reachable and every credential"
        "\nexported. A *_RPC_WALLET left out is worse: the adapter IS built, against the"
        "\ndaemon's default wallet instead of desk_hot."
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
