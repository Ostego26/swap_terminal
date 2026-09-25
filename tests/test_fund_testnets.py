"""fund_testnets.py's decisions, and its refusal to write a secret into a repo.

Role: test (pure functions and a filesystem guard; no daemon, no network)
Reads: fund_testnets.py
Writes: a temp directory, in the git-guard test only
Can move funds: no. The script it tests mints only test coins; these tests
      mint nothing and open no socket.
Mainnet-safe: yes
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# The path insert above has to run first: fund_testnets.py is at the project
# root, which conftest.py does not put on sys.path.
from fund_testnets import (
    COINBASE_MATURITY_BLOCKS,
    GRIDCOIN_MAINNET_RPC_PORT,
    NEGLIGIBLE_SUBSIDY,
    REGTEST_HALVING_INTERVAL,
    SOLANA_DEVNET,
    XRP_FAUCET,
    block_subsidy,
    describe_regtest_yield,
    secret_destination,
    selected_chains,
)


class Args:
    """Stands in for argparse's namespace."""

    def __init__(self, **kwargs):
        for name in ("btc", "ltc", "xrp", "sol", "grc", "xmr", "all", "wipe"):
            setattr(self, name, kwargs.get(name, False))
        self.blocks = kwargs.get("blocks", COINBASE_MATURITY_BLOCKS)


def test_nothing_selected_selects_nothing():
    """So main() can refuse with the help text rather than doing something."""
    assert selected_chains(Args()) == []


def test_one_flag_selects_exactly_that_chain():
    assert [asset for asset, _, _ in selected_chains(Args(btc=True))] == ["BTC"]
    assert [asset for asset, _, _ in selected_chains(Args(xrp=True))] == ["XRP"]


def test_all_selects_every_chain_including_gridcoin_as_a_read():
    """Six chains, and GRC is present as a READ rather than absent.

    The operator's instruction was "test coins for each one except gridcoin, I
    have plenty of testnet for it" -- which is not the same as "leave Gridcoin
    out". They then asked to SEE the balance, so GRC has a step that mints
    nothing. This test's name said the opposite until that was corrected.

    ETH stays out entirely; see the README section on why it is deferred.
    """
    assets = [asset for asset, _, _ in selected_chains(Args(all=True))]
    assert assets == ["BTC", "LTC", "XRP", "SOL", "GRC", "XMR"]
    assert "ETH" not in assets


def test_gridcoin_is_present_but_only_as_a_read():
    """The operator holds testnet GRC already and asked only to SEE it.

    So GRC has a runner (unlike XMR) but that runner mints nothing -- it makes
    two read calls. Pinned so a future "while we are here, let us mine some
    GRC too" is a deliberate change rather than a drive-by.
    """
    grc = next(entry for entry in selected_chains(Args(all=True)) if entry[0] == "GRC")
    assert callable(grc[2])
    assert "READ-ONLY" in grc[1]
    assert "mints nothing" in grc[1]


def test_monero_has_no_runner_so_it_cannot_pretend():
    """XMR's entry is None on purpose: the script EXPLAINS rather than attempts.

    A stagenet sync was measured at 33-54 hours, so a runner here would either
    block for a day or produce a wallet that cannot see its own deposit.
    """
    xmr = next(entry for entry in selected_chains(Args(all=True)) if entry[0] == "XMR")
    assert xmr[2] is None


def test_every_other_chain_has_a_runner():
    for asset, _, run in selected_chains(Args(all=True)):
        if asset != "XMR":
            assert callable(run), asset


def test_gridcoins_mainnet_port_is_named_so_the_report_can_warn():
    """15715 is mainnet. It is named so a mainnet hit can be LABELLED, not skipped.

    The network is still decided by the daemon's getinfo.testnet rather than by
    this number -- several of the operator's testnet-NAMED confs carry no
    testnet=1, because Gridcoin also takes -testnet on the command line. A
    script called fund_testnets reporting a real balance as play money would be
    the worst outcome available to it.
    """
    assert GRIDCOIN_MAINNET_RPC_PORT == 15715


def test_the_default_block_count_clears_coinbase_maturity():
    """Fewer than 101 leaves the reward immature and the balance reading zero.

    Which looks like a broken daemon rather than immature coins -- so the
    default has to be the number that actually yields spendable coins.
    """
    assert COINBASE_MATURITY_BLOCKS == 101


def test_the_hosts_are_pinned_to_test_networks():
    """No flag reaches mainnet; changing that means editing the source.

    Asserted rather than trusted, because a script that mints coins should not
    be one typo away from a mainnet endpoint.
    """
    assert "altnet.rippletest.net" in XRP_FAUCET
    assert "devnet" in SOLANA_DEVNET
    assert "mainnet" not in XRP_FAUCET and "mainnet" not in SOLANA_DEVNET
    assert "s1.ripple.com" not in XRP_FAUCET


def test_a_secret_is_never_written_inside_a_git_repository(tmp_path, monkeypatch):
    """THE GUARD THAT MATTERS, and it has a date on it.

    wgrc.json was a Solana keypair written inside this working tree, committed,
    and pushed -- which cost a key rotation and a history rewrite on
    2026-09-25. A secret path inside a repo is one `git add -A` from being
    published, so this refuses rather than trusting the .gitignore.
    """
    fake_home = tmp_path / "home"
    (fake_home / ".config" / "swap_terminal" / "keys").mkdir(parents=True)
    (fake_home / ".git").mkdir()          # a repo ABOVE the key directory
    monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))
    with pytest.raises(Exception, match="inside a git repository"):
        secret_destination("xrp")


def test_a_secret_destination_outside_any_repo_is_accepted_and_locked_down(tmp_path, monkeypatch):
    fake_home = tmp_path / "clean"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))
    destination = secret_destination("xrp")
    assert destination.parent.is_dir()
    assert oct(destination.parent.stat().st_mode)[-3:] == "700"
    assert "xrp-testnet-" in destination.name
    assert destination.suffix == ".json"


def test_two_secret_destinations_in_the_same_run_never_collide(tmp_path, monkeypatch):
    """A second call must not hand back a path the first one will write to.

    The timestamp is second-resolution and the write uses write_text(), so
    without a uniqueness step two calls inside one second would silently
    overwrite the first key with the second. An earlier version of this test
    xfailed on exactly that -- documenting a key-loss bug instead of fixing a
    two-line problem, which is precisely what rule 19 forbids.
    """
    fake_home = tmp_path / "clean2"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))
    first = secret_destination("xrp")
    first.write_text("{}")
    second = secret_destination("xrp")
    assert first != second, "a second call returned a path that would overwrite the first key"
    second.write_text("{}")
    third = secret_destination("xrp")
    assert third not in (first, second)


def test_an_existing_secret_file_is_never_overwritten(tmp_path, monkeypatch):
    """Whatever is already there could be a funded account, so it is left alone."""
    fake_home = tmp_path / "clean3"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))
    taken = secret_destination("xrp")
    taken.write_text('{"account": "do not lose me"}')
    assert secret_destination("xrp") != taken
    assert taken.read_text() == '{"account": "do not lose me"}'


# ---------------------------------------------------------------------------
# The regtest subsidy arithmetic. These reproduce the OPERATOR'S OWN numbers
# from 2026-09-25, which is what makes them a regression test rather than a
# restatement of the formula.
# ---------------------------------------------------------------------------

def test_the_halving_interval_is_the_regtest_one():
    """150, not mainnet's 210,000. Getting this wrong makes every figure wrong."""
    assert REGTEST_HALVING_INTERVAL == 150


def test_the_subsidy_reproduces_the_btc_balance_the_operator_saw():
    """Their BTC chain was at height 390 and 101 blocks yielded exactly 12.5.

    391//150 = 2 halvings, 50/4 = 12.5, and mining exactly 101 blocks matures
    exactly one coinbase. The reported balance was 12.5.
    """
    assert block_subsidy(391) == 12.5
    line = describe_regtest_yield(390, 101)
    assert "12.50000000/block" in line
    assert "2 halving(s)" in line
    assert "matures 1 reward(s)" in line


def test_the_subsidy_reproduces_the_ltc_balance_the_operator_saw():
    """Their LTC chain was 2504 deep and 101 blocks yielded 0.00076293.

    2505//150 = 16 halvings, 50/65536 = 0.00076294. The float the daemon
    reported was 0.00076293, one ulp below -- so this asserts the formula to 8
    decimals rather than exact equality with the daemon's rounding.
    """
    assert round(block_subsidy(2505), 8) == 0.00076294
    line = describe_regtest_yield(2504, 101)
    assert "16 halving(s)" in line


def test_a_negligible_subsidy_says_so_and_names_the_fix():
    """The LTC case: a bare 0.00076293 reads as a broken daemon.

    It is the same figure either way; what changes is whether the operator can
    tell an exhausted subsidy from a failure, and what to do about it.
    """
    line = describe_regtest_yield(2504, 101)
    assert "NEGLIGIBLE" in line
    assert "--wipe" in line
    assert "50/block" in line


def test_a_healthy_subsidy_does_not_cry_wolf():
    """BTC's 12.5 is plenty, so it must not carry the wipe warning."""
    line = describe_regtest_yield(390, 101)
    assert "NEGLIGIBLE" not in line
    assert "--wipe" not in line


def test_a_wiped_chain_starts_at_the_full_subsidy():
    assert block_subsidy(1) == 50.0
    assert block_subsidy(0) == 50.0
    line = describe_regtest_yield(0, 200)
    assert "50.00000000/block" in line
    assert "matures 100 reward(s)" in line


def test_mining_fewer_than_maturity_matures_nothing():
    """And says so, rather than reporting a balance of zero with no reason.

    100 blocks leaves every coinbase one confirmation short, which is the most
    confusing possible outcome: the blocks exist and the balance is zero.
    """
    line = describe_regtest_yield(0, 100)
    assert "matures 0 reward(s)" in line
    assert "= 0.00000000 spendable" in line


def test_the_negligible_threshold_is_a_whole_coin():
    """Below one coin per block, fees and dust limits start to bite in tests."""
    assert NEGLIGIBLE_SUBSIDY == 1.0
    assert block_subsidy(150 * 6 + 1) < NEGLIGIBLE_SUBSIDY   # 6 halvings = 0.78
    assert block_subsidy(150 * 5 + 1) > NEGLIGIBLE_SUBSIDY   # 5 halvings = 1.5
