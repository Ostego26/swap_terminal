"""Which chain an RPC port belongs to, and that no default guesses mainnet.

Role: test (pure functions plus one construction check; opens no socket)
Reads: swap_terminal/network_target.py, config.py, chains/registry.py
Writes: nothing
Can move funds: no
Mainnet-safe: yes

These exist because of a defect the entire 705-test suite passed straight
through. On 2026-09-26 the operator reported "we're still pulling from grc
mainnet wallet and not the testnet wallet", and they were right:
config.Config.RPC defaulted GRC_RPC_PORT to 15715, Gridcoin MAINNET. BTC
defaulted to 8332 and LTC to 9332, the same. chains/registry.build_adapters()
constructed all three unconditionally, and
services/payout_service.refresh_wallet_inventory() calls get_balance() on every
adapter every cycle -- so the operator's live staking wallet was polled on a
loop by a terminal meant to be on testnet.

Nothing failed. Nothing warned. No test noticed, and the suite still passed at
705 after the fix, because no test had ever asserted which chain an adapter was
pointed at. That absence is what this file is for.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "swap_terminal"))

from chains.registry import build_adapters
from network_target import (
    CHAIN_PORTS,
    UNCONFIGURED_PORT,
    NetworkTargetUnconfigured,
    classify,
    configuring_variable,
    describe,
    mainnet_chains,
    require_configured,
    startup_lines,
)

# The ports that were the defaults, and the chain each belongs to. Written out
# rather than read from CHAIN_PORTS: a test that derives its expected value from
# the table it is checking cannot catch a wrong entry in that table.
MAINNET_PORTS = {"BTC": 8332, "LTC": 9332, "GRC": 15715}


@pytest.mark.parametrize(("chain", "port"), sorted(MAINNET_PORTS.items()))
def test_each_old_default_is_recognized_as_mainnet(chain, port):
    """The three numbers that were the defaults must each be named MAINNET."""
    assert classify(chain, port) == "MAINNET"


@pytest.mark.parametrize(("chain", "port"), [
    ("BTC", 18443), ("BTC", 18332), ("LTC", 19443), ("LTC", 19332),
    ("GRC", 25779), ("GRC", 25715), ("GRC", 9876),
])
def test_the_test_ports_are_recognized_as_test(chain, port):
    """Including all four Gridcoin ports found in the operator's own conf files.

    gridcoin_credentials.py knew only 25779 while transactions.py knew three, so
    25715 was a test chain to one file and unknown to the other. One table now.
    """
    assert classify(chain, port) == "TEST"


def test_unconfigured_is_its_own_answer_and_not_mainnet():
    """The whole bug in one assertion.

    The old code turned "nothing was set" INTO "use mainnet". These must be
    different answers, because one is a configuration state and the other is a
    decision to touch real money.
    """
    assert classify("GRC", UNCONFIGURED_PORT) == "UNCONFIGURED"
    assert classify("GRC", UNCONFIGURED_PORT) != "MAINNET"


def test_an_unknown_port_is_not_reported_as_safe():
    """Rule 17: a port with no convention means we did not establish the chain.

    Any daemon can be started on any -rpcport, so an unrecognized port may well
    be a mainnet daemon. Reporting it as "not mainnet" would be a guess in the
    voice of a measurement, which is the failure this repo keeps paying for.
    """
    assert classify("GRC", 34567) == "UNRECOGNIZED"
    assert "NOT established" in describe("GRC", "127.0.0.1", 34567)


def test_a_mainnet_target_says_real_money_where_a_human_will_see_it():
    """Rule 14: state what the number MEANS next to the number.

    A bare `port=15715` requires the reader to know Gridcoin's port table. The
    operator did not learn this from a log line; they learned it from money being
    in the wrong wallet.
    """
    line = describe("GRC", "127.0.0.1", 15715)

    assert "MAINNET" in line
    assert "REAL MONEY" in line


def test_an_unconfigured_chain_names_the_variable_that_would_fix_it():
    """"Connection refused" sends an operator to restart a daemon that was fine."""
    line = describe("GRC", "127.0.0.1", UNCONFIGURED_PORT)

    assert "GRC_RPC_PORT" in line
    assert "25779" in line, "the line must name the test port, not just say it is unset"


def test_mainnet_chains_names_them_rather_than_returning_a_flag():
    """"Something is on mainnet" sends an operator hunting; naming it does not."""
    rpc = {"BTC": {"port": 18443}, "LTC": {"port": 9332}, "GRC": {"port": 15715}}

    assert mainnet_chains(rpc) == ["GRC", "LTC"]


def test_no_configured_chain_means_no_mainnet_chain():
    rpc = {chain: {"port": UNCONFIGURED_PORT} for chain in CHAIN_PORTS}

    assert mainnet_chains(rpc) == []


def test_every_chain_gets_a_banner_line_even_when_unset():
    """Rule 14: never let an empty result print nothing.

    A chain missing from the banner is indistinguishable from a banner that
    forgot it, and legible absence is the entire point of the banner.
    """
    lines = startup_lines({})

    for chain in (*CHAIN_PORTS, "SOL", "XRP", "XMR"):
        assert any(line.startswith(chain) for line in lines), f"{chain} has no banner line"


def test_require_configured_refuses_rather_than_defaulting():
    with pytest.raises(NetworkTargetUnconfigured, match="will not guess"):
        require_configured("GRC", UNCONFIGURED_PORT)

    assert require_configured("GRC", 25779) == 25779


# --- the defaults themselves, which is where the defect actually lived --------

def test_no_chain_default_points_at_mainnet_with_a_clean_environment():
    """THE REGRESSION. Fails against the pre-fix config.py.

    Imported inside the test and with the three variables removed, because
    config.Config reads the environment in its CLASS BODY -- the values are baked
    in at import, which is why tests/conftest.py sets SWAP_DB_PATH before
    importing anything. Setting them afterwards would be too late.
    """
    script = (
        "import sys; sys.path.insert(0, 'swap_terminal');"
        "from config import Config;"
        "from network_target import mainnet_chains;"
        "print(sorted(mainnet_chains(Config.RPC)))"
    )
    environment = {k: v for k, v in os.environ.items()
                   if k not in ("BTC_RPC_PORT", "LTC_RPC_PORT", "GRC_RPC_PORT")}
    # A subprocess, not monkeypatch: Config reads the environment in its class
    # body, so the values are baked in at import and this test is about what a
    # FRESH process does with nothing set. Fixed argv, no shell, no user input.
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=True, capture_output=True, text=True,
        cwd=str(Path(__file__).resolve().parent.parent),
        env=environment,
    )

    assert result.stdout.strip() == "[]", (
        f"a clean environment configured mainnet chains: {result.stdout.strip()}"
    )


# --- the registry guard, which is what stops a mainnet adapter being built ----

def bitcoin_shaped(port):
    """The six keys RPCAdapter.__init__ takes. build_adapters splats this."""
    return {"user": "", "password": "", "host": "127.0.0.1", "port": port,
            "wallet": "", "timeout": 30.0}


def test_an_unconfigured_chain_builds_no_adapter_at_all():
    """The second half of the fix, and it needs its own test.

    Defaulting the port to 0 is not sufficient on its own: build_adapters() used
    to construct BTC, LTC and GRC unconditionally, so a port of 0 would have
    produced three adapters aimed at nothing, and
    payout_service.refresh_wallet_inventory() would log a warning for each on
    every cycle -- the cries-wolf log the registry's own header says it left
    Solana out to avoid.

    Verified by mutation 2026-09-26: with the guard removed, this builds
    ['BTC', 'GRC', 'LTC'] on port 0.
    """
    built = build_adapters({chain: bitcoin_shaped(0) for chain in ("BTC", "LTC", "GRC")})

    assert built == {}, f"unconfigured chains built adapters: {sorted(built)}"


def test_a_configured_test_chain_does_build_an_adapter():
    """The guard must not be so strict that nothing works.

    Paired with the test above deliberately: a guard that refuses everything
    passes the negative test and is still broken, which is how "fail closed"
    turns into "fail always" without anyone noticing.
    """
    built = build_adapters({"GRC": bitcoin_shaped(25779)})

    assert sorted(built) == ["GRC"]


# --- which variable makes a chain reachable -----------------------------------

def test_configuring_variable_matches_what_config_py_actually_reads():
    """Asserted against config.py's SOURCE, not against a list written here.

    The point of configuring_variable() is that three places -- the workers'
    startup banner, the swap page's pair list, and create_swap()'s refusal -- name
    the same environment variable without three copies of the string. A test that
    spelled the six names again would be a fourth copy, and would pass while the
    real variable was renamed.

    So this reads config.py and requires each returned name to appear there as an
    os.getenv() call. Rule 17: the thing that would show it false is to go look.
    """
    config_source = (Path(__file__).resolve().parent.parent / "swap_terminal" / "config.py").read_text()

    for asset in ("BTC", "LTC", "GRC", "XRP", "SOL", "XMR"):
        variable = configuring_variable(asset)
        assert f'os.getenv("{variable}"' in config_source, (
            f"configuring_variable({asset!r}) returned {variable!r}, which config.py never reads"
        )


def test_the_three_bitcoin_derived_chains_come_from_chain_ports():
    """Not a second table. CHAIN_PORTS already holds their variable names."""
    for asset in ("BTC", "LTC", "GRC"):
        assert configuring_variable(asset) == CHAIN_PORTS[asset].port_variable


def test_the_two_url_chains_and_monero_are_not_port_variables_by_accident():
    """SOL and XRP are configured by a URL, so <ASSET>_RPC_PORT would be wrong.

    MUTATION: drop _ENDPOINT_VARIABLES and let the fallback answer. SOL and XRP
    then read SOL_RPC_PORT and XRP_RPC_PORT, neither of which config.py contains,
    and the page would tell an operator to set a variable that does nothing. XMR
    IS a port and is in the same table only because monero-wallet-rpc has no
    conventional port to classify against -- chains/registry.py says so at its
    construction site.
    """
    assert configuring_variable("SOL") == "SOL_RPC_URL"
    assert configuring_variable("XRP") == "XRP_RPC_URL"
    assert configuring_variable("XMR") == "XMR_RPC_PORT"


def test_an_unknown_chain_gets_a_plausible_name_rather_than_raising():
    """A chain added to ALLOWED_PAIRS before it is added here must not raise.

    Raising would put a KeyError on the page, which is the exact failure this
    function was written to stop: `No swap was created: 'GRC'` was
    str(KeyError("GRC")). The fallback matches what describe() and
    require_configured() already do for an unknown chain.
    """
    assert configuring_variable("DOGE") == "DOGE_RPC_PORT"
